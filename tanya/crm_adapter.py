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

    def get_user(self, user_id: str) -> dict:
        """Profile fields, including DPDP consent status and trial dates (A5, B3)."""
        raise NotImplementedError

    def read_notes(self, user_id: str) -> list:
        """Agent notes (read only) and Tanya's own brief (A4)."""
        raise NotImplementedError

    def write_tanya_brief(self, user_id: str, conversation_id: str, text: str) -> str:
        """Create or update ONLY Tanya's own note record (never an agent's). Returns note id."""
        raise NotImplementedError

    def hand_to_human(self, conversation_id: str, reason: str) -> bool:
        """Assign / flag the conversation for staff (department or agent, per A8)."""
        raise NotImplementedError


class ConsoleAdapter(CRMAdapter):
    """Dev console: an in-memory 'CRM' so the brain can be tested without the real one."""

    def __init__(self):
        self.outbox = {}      # conversation_id -> list of posted texts
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

    def get_user(self, user_id):
        return {}

    def read_notes(self, user_id):
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
        r = httpx.post(self.url, data={"token": self.token, "function": self.FN[fn], **params}, timeout=10)
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
        if fn != "message-sent":                            # TO CONFIRM (A2): event names
            return IntakeEvent(str(data.get("id", "")), "status_change", str(data.get("user_id", "")),
                               str(data.get("conversation_id", "")), "", payload)
        sender = str(data.get("user_id", ""))
        if sender == self.tanya_agent:
            return None                                     # our own reply echoed back — ignore
        kind = "staff_message" if str(data.get("user_type", "")) in ("agent", "admin") else "user_message"
        return IntakeEvent(event_id=str(data.get("message_id") or data.get("id")), kind=kind,
                           user_id=str(data.get("conversation_user_id") or sender),
                           conversation_id=str(data.get("conversation_id")),
                           text=str(data.get("message", "")), raw=payload)

    def post_message(self, conversation_id, text):
        res = self._call("send", user_id=self.tanya_agent, conversation_id=conversation_id, message=text)
        return str(res.get("id", res) if isinstance(res, dict) else res)

    def get_conversation(self, conversation_id, limit=30):
        res = self._call("conversation", conversation_id=conversation_id)
        msgs = res.get("messages", []) if isinstance(res, dict) else []
        return msgs[-limit:]

    def get_user(self, user_id):
        return self._call("user", user_id=user_id, extra="true")   # TO CONFIRM (A5): field names

    def read_notes(self, user_id):
        return self._call("notes_list", user_id=user_id)             # TO CONFIRM (A4)

    def write_tanya_brief(self, user_id, conversation_id, text):
        """Only Tanya's own note. The note id is kept in her memory; she never edits another id."""
        # TODO (CODER D, A4): if separate note records are supported → add once, then update by own id;
        # else write to a user extra field reserved for Tanya (Architecture §9.3.2).
        res = self._call("note_add", conversation_id=conversation_id, user_id=self.tanya_agent,
                         name="Ms Tanya — Lead Brief (auto)", message=text)
        return str(res)

    def hand_to_human(self, conversation_id, reason):
        self._call("department", conversation_id=conversation_id, department=self.human_dept)
        return True


def make_adapter():
    return SupportBoardAdapter() if S.env("CRM_MODE", "console") == "supportboard" else ConsoleAdapter()
