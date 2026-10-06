"""CRM adapter — the ONLY code that talks to the PHP CRM (Support Board based, TG CRM v3.8.4).

Plain English:
- The CRM code is not changed. It talks to us through its Webhook setting (CRM → us),
  and we talk to it through its api.php with a server-side token (us → CRM).
- ConsoleAdapter: a stand-in used by the dev console and tests (no CRM needed).
- SupportBoardAdapter: the real one. Function names and payload fields marked
  'TO CONFIRM (A1/A2)' must be checked against CODER D's real request/response samples
  before go-live. There is deliberately NO delete function (Architecture §9.3):
  Tanya can never remove an agent's note.

PLUMBING OWNER: CODER D (+ JUNIOR B). Checklist in docs/JUNIOR_TASKS.md, section CRM.
"""
import hmac
from dataclasses import dataclass, field

import httpx

from .settings import S

# keep-alive pool to the CRM: every bubble is 1-2 api.php calls, a new TCP connection each time added up
CRM_HTTP = httpx.Client(limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=120))
SUMMARY_NOTE_NAME = "Conversation Summary"   # the one CRM note Tanya keeps per conversation (06-Oct-2026)
CLOSED_STATUS = ("3", "4")          # Support Board conversation status: 3 = archived (closed), 4 = trash
BOT_COMMANDS = ("#bot",)            # a staff message that is exactly this hands the chat back to Tanya


@dataclass
class IntakeEvent:
    """One thing that happened, in our own shape (whatever the CRM's format)."""
    event_id: str
    kind: str                 # user_message | staff_message | status_change | app_open
    user_id: str
    conversation_id: str
    text: str = ""
    raw: dict = field(default_factory=dict)


class CRMAdapter:
    """What the rest of the code may ask of the CRM. Nothing else."""

    def parse_webhook(self, payload: dict, headers: dict):
        raise NotImplementedError

    def post_message(self, conversation_id: str, text: str) -> str:
        """Post Tanya's reply into the chat thread (as the Tanya agent account). Returns message id."""
        raise NotImplementedError

    def get_conversation(self, conversation_id: str, limit: int = 30) -> list:
        raise NotImplementedError

    def staff_replied_after(self, conversation_id: str, after_message_id) -> bool:
        """True if a staff member (agent/admin, not Tanya) wrote in this chat after the given CRM message.
        Read from the CRM itself just before posting, so a staff reply whose webhook has not arrived yet
        still stops Tanya (pressure test K3 / PT6)."""
        return False

    def recent_conversations(self, since_utc: str) -> list:
        """Conversations with any message after since_utc ('YYYY-MM-DD HH:MM:SS', CRM time = UTC), each with its
        latest message. Used by the reconciler to find customer messages whose webhook never arrived."""
        return []

    def own_message_saved(self, conversation_id: str, after_message_id, text: str) -> bool:
        """True if Tanya's message with this text is already in the chat after the given CRM message:
        an earlier post was saved by the CRM even though its response was lost (PT4 / Spec 8.2 item 6)."""
        return False

    def get_user(self, user_id: str) -> dict:
        """Profile fields, including DPDP consent status and trial dates (A5, B3)."""
        raise NotImplementedError

    def read_notes(self, conversation_id: str) -> list:
        """Agent notes (read only) and Tanya's own brief for a conversation."""
        raise NotImplementedError

    def write_tanya_brief(self, user_id: str, conversation_id: str, text: str) -> str:
        """Create or update ONLY Tanya's own note record (never an agent's). Returns note id."""
        raise NotImplementedError

    def conversation_agent(self, conversation_id: str) -> str:
        """Id of the agent the CRM assigned to this conversation ('' if none)."""
        return ""

    def write_summary_note(self, conversation_id: str, text: str) -> str:
        """Create or update the single 'Conversation Summary' note of this conversation. Returns the note id."""
        raise NotImplementedError

    def hand_to_human(self, conversation_id: str, reason: str) -> bool:
        """Assign / flag the conversation for staff (department or agent, per A8)."""
        raise NotImplementedError


class ConsoleAdapter(CRMAdapter):
    """Dev console: an in-memory 'CRM' so the brain can be tested without the real one."""

    def __init__(self):
        self.outbox = {}      # conversation_id -> list of posted texts
        self.notes = {}       # conversation_id -> {note_id: text}
        self.briefs = {}      # user_id -> text
        self.handoffs = []

    def parse_webhook(self, payload, headers):
        return IntakeEvent(payload.get("event_id", ""), payload.get("kind", "user_message"),
                           payload["user_id"], payload.get("conversation_id", payload["user_id"]),
                           payload.get("text", ""), payload)

    def post_message(self, conversation_id, text):
        self.outbox.setdefault(conversation_id, []).append(text)
        return f"console-{len(self.outbox[conversation_id])}"

    def get_conversation(self, conversation_id, limit=30):
        return self.outbox.get(conversation_id, [])[-limit:]

    def own_message_saved(self, conversation_id, after_message_id, text):
        return text in self.outbox.get(conversation_id, [])

    def write_summary_note(self, conversation_id, text):
        self.notes.setdefault(conversation_id, {})["summary"] = text
        return "summary"

    def get_user(self, user_id):
        return {}

    def read_notes(self, conversation_id: str):
        return []

    def write_tanya_brief(self, user_id, conversation_id, text):
        self.briefs[user_id] = text
        return f"brief-{user_id}"

    def hand_to_human(self, conversation_id, reason):
        self.handoffs.append((conversation_id, reason))
        return True


class SupportBoardAdapter(CRMAdapter):
    """Real CRM through api.php.  Needs in .env: CRM_API_URL, CRM_API_TOKEN, CRM_WEBHOOK_SECRET,
    TANYA_AGENT_ID (A3), CRM_HUMAN_DEPARTMENT_ID (A8)."""

    # TO CONFIRM (A1): exact api.php function names in TG CRM v3.8.4
    FN = {
        "send": "send-message",
        "conversation": "get-conversation",
        "new_conversations": "get-new-conversations",
        "user": "get-user",
        "update_user": "update-user",
        "department": "update-conversation-department",
        "status": "update-conversation-status",
        "notes_list": "get-notes",        # TO CONFIRM (A4) — may not exist; then brief goes to a user extra field
        "note_add": "add-note",           # TO CONFIRM (A4)
        "note_update": "update-note",     # TO CONFIRM (A4) — only with Tanya's own note id
    }

    def __init__(self):
        self.url = S.env("CRM_API_URL")
        self.token = S.env("CRM_API_TOKEN")
        self.secret = S.env("CRM_WEBHOOK_SECRET")
        self.tanya_agent = S.env("TANYA_AGENT_ID")
        self.human_dept = S.env("CRM_HUMAN_DEPARTMENT_ID")

    def _call(self, fn, **params):
        r = CRM_HTTP.post(self.url, data={"token": self.token, "function": self.FN[fn], **params}, timeout=10)
        r.raise_for_status()
        j = r.json()
        # TO CONFIRM (A1): success/error shape. Support Board commonly returns {"success": true, "response": ...}
        if isinstance(j, dict) and j.get("success") is False:
            raise RuntimeError(f"CRM {fn} failed: {str(j)[:200]}")
        return j.get("response", j) if isinstance(j, dict) else j

    def parse_webhook(self, payload, headers):
        """TO CONFIRM (A2): payload shape. Expected: {"function": "message-sent", "key": "...", "data": {...}}."""
        key = payload.get("key") or headers.get("x-webhook-secret", "")
        if not self.secret or not hmac.compare_digest(str(key), self.secret):
            return None                                     # not from our CRM
        fn = payload.get("function", "")
        data = payload.get("data", {}) or {}
        if fn == "conversation-status-updated" and str(data.get("status_code")) in CLOSED_STATUS:
            # staff archived / deleted the chat (crm functions_messages.php:285-327): HUMAN mode ends (v4 §7)
            return IntakeEvent("", "conversation_closed", "", str(data.get("conversation_id", "")), "", payload)
        if fn != "message-sent":                            # every other CRM event: nothing to do
            return IntakeEvent(str(data.get("id", "")), "status_change", str(data.get("user_id", "")),
                               str(data.get("conversation_id", "")), "", payload)
        sender = str(data.get("user_id", ""))
        conv_user = str(data.get("conversation_user_id") or sender)
        if sender and sender == self.tanya_agent:
            return None                                     # our own reply echoed back — ignore
        msg_id = str(data.get("message_id") or data.get("id") or "")
        if not msg_id.isdigit():
            # G5: never invent an id ("None" would make unrelated events duplicates of each other) and never guess
            # the author: rejected + alert; the reconciler still finds the message by its real CRM id
            return IntakeEvent("", "invalid_event", conv_user, str(data.get("conversation_id", "")), "", payload)
        is_staff = (sender != conv_user) or (str(data.get("user_type", "")) in ("agent", "admin"))
        text = str(data.get("message", ""))
        if is_staff and text.strip().lower() in BOT_COMMANDS:
            # staff hands the chat back to Tanya (v4 §7: staff types #bot)
            return IntakeEvent(msg_id, "release_to_bot", conv_user, str(data.get("conversation_id")), "", payload)
        kind = "staff_message" if is_staff else "user_message"
        return IntakeEvent(event_id=msg_id, kind=kind,
                           user_id=conv_user,
                           conversation_id=str(data.get("conversation_id")),
                           text=str(data.get("message", "")), raw=payload)

    def post_message(self, conversation_id, text):
        res = self._call("send", user_id=self.tanya_agent, conversation_id=conversation_id, message=text)
        return str(res.get("id", res) if isinstance(res, dict) else res)

    def get_conversation(self, conversation_id, limit=30):
        res = self._call("conversation", conversation_id=conversation_id)
        msgs = res.get("messages", []) if isinstance(res, dict) else []
        return msgs[-limit:]

    def conversation_agent(self, conversation_id):
        res = self._call("conversation", conversation_id=conversation_id)
        details = res.get("details", {}) if isinstance(res, dict) else {}
        agent = details.get("agent_id")
        return "" if agent in (None, "", "0", 0, -1, "-1") else str(agent)

    def write_summary_note(self, conversation_id, text):
        """One note named 'Conversation Summary' per conversation, updated in place (never a pile of notes)."""
        for n in self.read_notes(conversation_id) or []:
            if isinstance(n, dict) and n.get("name") == SUMMARY_NOTE_NAME:
                self._call("note_update", conversation_id=conversation_id, user_id=self.tanya_agent,
                           note_id=n.get("id"), message=text)
                return str(n.get("id"))
        res = self._call("note_add", conversation_id=conversation_id, user_id=self.tanya_agent,
                         name=SUMMARY_NOTE_NAME, message=text)
        return str(res)

    def recent_conversations(self, since_utc):
        res = self._call("new_conversations", datetime=since_utc)
        return res if isinstance(res, list) else []

    def staff_replied_after(self, conversation_id, after_message_id):
        try:
            after = int(after_message_id)
        except (TypeError, ValueError):
            return False                                    # no CRM message id (e.g. app_open): nothing to compare
        for m in self.get_conversation(conversation_id, limit=30):
            # a REPLY has words or a file: Support Board's own hidden events (e.g. the empty
            # "conversation-department-update" message the API user leaves when Tanya hands over) are not replies
            has_content = bool(str(m.get("message") or "").strip()) or str(m.get("attachments") or "") not in ("", "[]")
            if (has_content and str(m.get("user_type")) in ("agent", "admin") and str(m.get("user_id")) != self.tanya_agent
                    and int(m.get("id", 0)) > after):
                return True
        return False

    def own_message_saved(self, conversation_id, after_message_id, text):
        try:
            after = int(after_message_id)
        except (TypeError, ValueError):
            after = 0
        norm = lambda s: " ".join(str(s or "").split())     # the CRM strips \r, \t and extra blank lines
        return any(str(m.get("user_id")) == self.tanya_agent and int(m.get("id", 0)) > after
                   and norm(m.get("message")) == norm(text) for m in self.get_conversation(conversation_id, limit=30))

    def get_user(self, user_id):
        return self._call("user", user_id=user_id, extra="true")   # TO CONFIRM (A5): field names

    def read_notes(self, conversation_id: str):
        if not conversation_id:
            return []
        res = self._call("notes_list", conversation_id=conversation_id)
        return res if isinstance(res, list) else []

    def write_tanya_brief(self, user_id, conversation_id, text):
        """Only Tanya's own note. Updates existing brief note if present, else creates new."""
        notes = self.read_notes(conversation_id)
        existing_note_id = None
        if isinstance(notes, list):
            for n in notes:
                if isinstance(n, dict) and n.get("name") == "Ms Tanya — Lead Brief (auto)":
                    existing_note_id = n.get("id")
                    break
        if existing_note_id:
            res = self._call("note_update", conversation_id=conversation_id, user_id=self.tanya_agent,
                             note_id=existing_note_id, message=text)
            return str(existing_note_id)
        else:
            res = self._call("note_add", conversation_id=conversation_id, user_id=self.tanya_agent,
                             name="Ms Tanya — Lead Brief (auto)", message=text)
            return str(res)

    def hand_to_human(self, conversation_id, reason):
        self._call("department", conversation_id=conversation_id, department=self.human_dept)
        return True


def make_adapter():
    return SupportBoardAdapter() if S.env("CRM_MODE", "console") == "supportboard" else ConsoleAdapter()
