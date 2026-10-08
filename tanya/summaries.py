"""Conversation Summary in the CRM notes (Tushar, 06-Oct-2026).

Plain English:
- One summary per CONVERSATION (never per customer account), shown in the CRM conversation's Notes as a single
  note named "Conversation Summary": "Last Updated: <IST time>" + "Summary: <text>". Updated in place.
- Not after every message. A conversation becomes due when (whichever first):
    summary_every_messages (5) new messages since the last summary, or
    its oldest unsummarised message is summary_max_age_minutes (10) old, or
    a forced event: Tanya handed over, recovery, chat closed.
- Only for chats Tanya took part in (08-Oct-2026): a conversation with no Tanya message (customer not on the
  dashboard's AI list, staff-only chat) gets no summary and no note; it is dropped without an AI call.
- Built from the CRM's own copy of this conversation (customer, Tanya and staff messages), by one cheap AI call
  in the sessions process — never in the chat path, never blocking a reply.
- Stored in MySQL (orch_conv_summaries, via the persister) and in the CRM note. If generation or the CRM write
  fails, the previous summary stays and the conversation is retried after summary_retry_seconds.
"""
import re
import sys

from .settings import S
from .timeutil import iso

SYSTEM = """You write the CRM summary of ONE support-chat conversation between a customer of TG Level (a SEBI-registered
trading-education company), its AI assistant Tanya and human staff. Agents read it instead of the whole chat.
Write in simple, natural HINGLISH (Hindi in Roman script mixed with English words, the way Indian support agents
write), e.g. "Customer ne options trading seekhne ke baare mein poocha. Tanya ne risk basics samjhaye."
STRICT LIMIT: at most 40 words in total, 1-3 short sentences. Keep it simple and crisp. Cover only what matters:
customer kya chahta hai, Tanya ne kya bataya/offer kiya, handoff hua ya nahi, abhi status kya hai
(e.g. team ka wait, agent ne reply kiya, Tanya ke saath wapas, resolved).
Never include phone numbers, e-mails or payment details. Only facts from the transcript. No headings, no bullets."""


def cap_words(text, limit=None):
    """Hard limit on the summary length (06-Oct-2026: max 40 words). Cuts at the last full sentence inside the
    limit when there is one, else at the word limit with an ellipsis."""
    limit = limit or S.get("summary_max_words", 40)
    words = text.split()
    if len(words) <= limit:
        return text
    cut = " ".join(words[:limit])
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if cut.endswith((".", "?", "!")):
        return cut
    return cut[:end + 1] if end > len(cut) // 2 else cut.rstrip(",;:-") + "…"


HANDOFF_WORDS = {"open": "handed to the team and waiting for an agent", "agent_replied": "an agent has replied",
                 "recovered": "no agent replied in time, so Tanya resumed", "released": "staff handed it back to Tanya"}


def _plain(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(html or ""))).strip()


def _is_tanya(m, tanya_id):
    return str(m.get("user_id")) == str(tanya_id) or str(m.get("user_type")) == "bot"


def tanya_took_part(messages, tanya_id):
    """True if Tanya wrote at least one message in this conversation."""
    return any(_is_tanya(m, tanya_id) and _plain(m.get("message")) for m in messages)


def transcript(messages, tanya_id):
    lines = []
    for m in messages:
        ut = str(m.get("user_type"))
        who = "Tanya" if _is_tanya(m, tanya_id) else ("Agent" if ut in ("agent", "admin") else "Customer")
        text = _plain(m.get("message"))
        if text:
            lines.append(f"{who}: {text[:400]}")
    return "\n".join(lines[-60:])


def due(state, now_epoch):
    if state.get("retry_after", 0) > now_epoch:
        return False
    if state.get("force"):
        return True
    if state.get("count", 0) >= S.get("summary_every_messages", 5):
        return True
    return state.get("count", 0) > 0 and now_epoch - state.get("first", now_epoch) >= S.get("summary_max_age_minutes", 10) * 60


def note_text(summary, now):
    return f"Last Updated: {now.strftime('%d %b %Y, %I:%M %p')} IST\nSummary: {summary}"


def summarize_conversation(store, llm, adapter, conv, user_id, now):
    """Build + store one summary. Returns None (nothing written) when Tanya never chatted in this conversation.
    Raises on failure (caller keeps the old summary and retries)."""
    from .crm_adapter import SUMMARY_NOTE_NAME
    msgs = adapter.get_conversation(conv, limit=80)
    tanya = getattr(adapter, "tanya_agent", "2")
    if not tanya_took_part(msgs, tanya):
        return None
    text = transcript(msgs, tanya)
    if not text:
        raise ValueError("empty conversation")
    h = store.handoff_get(conv) if hasattr(store, "handoff_get") else None
    status_hint = f"\n\n(System status: handoff {HANDOFF_WORDS.get(h.get('status'), h.get('status'))}.)" if h else ""
    res = llm.call("summary", "fast", SYSTEM, [{"role": "user", "content": text + status_hint}], temperature=0.0,
                   timeout=S.get("timeout_summary_seconds", 25), max_tokens=160)
    summary = cap_words(_plain(res.text)) if res.ok else ""
    if not summary:
        raise RuntimeError(f"summary call failed: {res.error}")
    note_id = adapter.write_summary_note(conv, note_text(summary, now))
    last_id = max([int(m.get("id", 0)) for m in msgs if str(m.get("id", "")).isdigit()] or [0])
    store.emit([{"type": "conv_summary", "conversation_id": str(conv), "user_id": str(user_id or "-"),
                 "summary": summary, "msg_count": len(msgs), "last_message_id": str(last_id),
                 "crm_note_id": str(note_id), "note_name": SUMMARY_NOTE_NAME, "model": res.model,
                 "at": iso(now)}])
    return summary


def summarize_due(store, llm, adapter, now=None):
    """Run every minute (sessions process). Returns the conversations summarised."""
    from .timeutil import now as tnow
    now = now or tnow()
    done = []
    for conv, state in store.summary_dirty().items():
        if getattr(adapter, "numeric_ids", False) and not str(conv).isdigit():
            store.summary_clear(conv)        # not a CRM chat (dev/console id): the CRM would 500 on it forever
            print(f"[summary] dropped conv={conv}: not a CRM conversation id", file=sys.stderr, flush=True)
            continue
        if not due(state, now.timestamp()):
            continue
        try:
            if summarize_conversation(store, llm, adapter, conv, state.get("user_id"), now) is None:
                store.summary_clear(conv)    # Tanya never chatted here: no summary note for this conversation
                print(f"[summary] skipped conv={conv}: no Tanya message", file=sys.stderr, flush=True)
                continue
            store.summary_clear(conv)
            done.append(conv)
            print(f"[summary] updated conv={conv} after {state.get('count', 0)} msgs", file=sys.stderr, flush=True)
        except Exception as e:
            store.summary_backoff(conv, now.timestamp() + S.get("summary_retry_seconds", 120))
            store.emit([{"type": "alert", "user_id": state.get("user_id") or "-", "at": iso(now),
                         "kind": "summary_failed", "conversation_id": conv, "detail": f"{type(e).__name__}: {e}"[:200]}])
            print(f"[summary] FAILED conv={conv}: {type(e).__name__}: {str(e)[:120]} - previous summary kept",
                  file=sys.stderr, flush=True)
    return done
