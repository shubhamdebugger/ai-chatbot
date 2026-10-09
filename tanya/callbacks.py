"""Tanya callbacks: every promise of contact becomes a tracked task with an SLA (Tushar, 06-Oct-2026).

Plain English:
- Created when Tanya promises contact: the fixed lines that say the team / a senior will call or get back
  (PROMISE_LINES), or an AI-written reply that says the same (PROMISE_RX). One open callback per kind per customer.
- Due: business hours -> created + callback_sla_minutes (10); after hours -> next business-day start + 10 min.
- Status, recomputed by the SLA monitor from the CRM itself:
    pending      no agent assigned to the chat
    assigned     an agent is assigned but has not written in the chat since the promise
    in_progress  an agent has written in the chat since the promise
    overdue      past due and not in progress / completed  -> one alert (orch_alerts) + notifier
    completed    an agent pressed "Mark done" in the CRM page (Tanya Callbacks)
- Every change is written to orch_audit (created, assigned, reassigned, status changes, overdue_notified, completed,
  recovered_by_tanya).
"""
import json
import os
import re
import sys

PROMISE_LINES = {"FX-05": "team_followup", "FX-06": "account_question", "FX-07": "person", "FX-08": "person",
                 "FX-10": "complaint", "FX-13": "call_preference", "FX-14": "purchase", "FX-19": "support_case"}
PROMISE_RX = re.compile(
    r"(team|senior|colleague|expert|agent|counsell?or)[^.?!]{0,60}\b(call|contact|reach out|get back|connect)"
    r"|\b(call|contact)\b[^.?!]{0,30}\b(karenge|karega|karegi|kar lenge)\b"
    r"|callback", re.I)
OPEN = ("pending", "assigned", "in_progress", "overdue")


def promise_in(bubbles):
    """(kind, promise text) if this reply promises contact by the team, else (None, None)."""
    for b in bubbles or []:
        if b.get("id") in PROMISE_LINES:
            return PROMISE_LINES[b["id"]], b.get("text", "")
    for b in bubbles or []:
        if b.get("kind") == "ai" and PROMISE_RX.search(b.get("text", "")):
            return "team_followup", b.get("text", "")
    return None, None


def connect():
    from .workers import Persister
    return Persister._connect()


def _audit(c, cb_id, event, actor, detail, at):
    c.execute("INSERT INTO orch_audit (entity,entity_id,event,actor,detail,at) VALUES ('callback',%s,%s,%s,%s,%s)",
              (cb_id, event, actor, json.dumps(detail, ensure_ascii=False, default=str), at))


def notify(kind, payload):
    """Team lead / operations admin notification. orch_alerts is always written (dashboard + reports); a chat or
    e-mail bridge can subscribe through NOTIFY_WEBHOOK_URL (JSON POST, best effort)."""
    print(f"[notify] {kind} {json.dumps(payload, ensure_ascii=False, default=str)[:300]}", file=sys.stderr, flush=True)
    url = os.environ.get("NOTIFY_WEBHOOK_URL", "")
    if url:
        try:
            import httpx
            httpx.post(url, json={"kind": kind, **payload}, timeout=5)
        except Exception as e:
            print(f"[notify] webhook failed {type(e).__name__}", file=sys.stderr, flush=True)


def _slot(raw):
    try:
        slot = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})
    except ValueError:
        return {}
    return slot if isinstance(slot, dict) else {}


def _owner(slot, stored):
    """Agent who owns a callback: the admin's pick in the CRM (slot.agent_id) wins over the stored/inherited one."""
    picked = str(slot.get("agent_id") or "")
    return picked if picked not in ("", "0") else str(stored or "")


def _inherit(c, cb_id, agent, now_s):
    """Give an owner-less callback the chat's agent, once. Never overwrites: the CRM admin changes owners."""
    n = c.execute("UPDATE orch_callbacks SET assigned_agent_id=%s WHERE callback_id=%s AND COALESCE(assigned_agent_id,'')=''",
                  (agent, cb_id))
    if n:
        _audit(c, cb_id, "assigned", "system", {"agent_id": agent, "source": "chat"}, now_s)
    return bool(n)


def _inherit_voice(c, adapter, now_s, days):
    """Voice callbacks have no conversation_id column (the chat is in slot.sb_conversation_id): same inherit rule."""
    c.execute("SELECT callback_id, assigned_agent_id, slot FROM orch_callbacks WHERE callback_id LIKE 'CB-V-%%' "
              "AND state IN ('requested','booked') AND COALESCE(status,'pending') <> 'completed' "
              "AND (conversation_id IS NULL OR conversation_id = '') AND requested_at >= NOW() - INTERVAL %s DAY", (days,))
    for cb_id, stored, raw in c.fetchall():
        slot = _slot(raw)
        conv = str(slot.get("sb_conversation_id") or "")
        if _owner(slot, stored) or slot.get("agent_cleared") or not conv.isdigit():
            continue
        try:
            agent = adapter.conversation_agent(conv)
        except Exception as e:
            print(f"[callbacks] CRM read failed conv={conv}: {type(e).__name__}", file=sys.stderr, flush=True)
            continue
        if agent:
            _inherit(c, cb_id, agent, now_s)


def sla_check(conn, adapter, now, days=7):
    """Recompute status of every open callback from the CRM. Returns the list of status changes.
    Owner rule: a callback without an owner takes the chat's agent (once, unless an admin cleared it);
    a callback with an owner keeps it until an admin reassigns it in the CRM."""
    from .timeutil import iso
    now_s = iso(now)[:19].replace("T", " ")
    changes = []
    with conn.cursor() as c:
        c.execute("SELECT callback_id, user_id, conversation_id, kind, COALESCE(status,'pending'), due_at, "
                  "assigned_agent_id, source_message_id, overdue_notified_at, slot FROM orch_callbacks "
                  "WHERE COALESCE(status,'pending') <> 'completed' AND requested_at >= NOW() - INTERVAL %s DAY "
                  "AND conversation_id IS NOT NULL AND conversation_id <> ''", (days,))
        rows = c.fetchall()
        agents, replied_cache = {}, {}
        for cb_id, uid, conv, kind, status, due, agent_prev, src, notified, slot_raw in rows:
            try:
                if conv not in agents:
                    agents[conv] = adapter.conversation_agent(conv)
                agent = agents[conv]
                key = (conv, src)
                if key not in replied_cache:
                    replied_cache[key] = adapter.staff_replied_after(conv, src) if src else False
                replied = replied_cache[key]
            except Exception as e:                       # CRM unreachable: keep the old status, try next run
                print(f"[callbacks] CRM read failed conv={conv}: {type(e).__name__}", file=sys.stderr, flush=True)
                continue
            slot = _slot(slot_raw)
            owner = _owner(slot, agent_prev)
            if not owner and agent and not slot.get("agent_cleared") and _inherit(c, cb_id, agent, now_s):
                owner = agent
            new = "in_progress" if replied else ("assigned" if owner else "pending")
            if new != "in_progress" and due is not None and str(due) < now_s:
                new = "overdue"
            if new != status:
                c.execute("UPDATE orch_callbacks SET status=%s WHERE callback_id=%s", (new, cb_id))
                _audit(c, cb_id, f"status:{new}", "sla_monitor", {"from": status, "to": new}, now_s)
                changes.append((cb_id, status, new))
            if new == "overdue" and not notified:
                payload = {"callback_id": cb_id, "user_id": uid, "conversation_id": conv, "kind": kind,
                           "due_at": str(due), "agent_id": owner or ""}
                c.execute("INSERT INTO orch_alerts (user_id,kind,detail,at) VALUES (%s,'callback_overdue',%s,%s)",
                          (uid, json.dumps(payload, default=str), now_s))
                c.execute("UPDATE orch_callbacks SET overdue_notified_at=%s WHERE callback_id=%s", (now_s, cb_id))
                _audit(c, cb_id, "overdue_notified", "sla_monitor", {"to": ["team_lead", "operations_admin"]}, now_s)
                notify("callback_overdue", payload)
        _inherit_voice(c, adapter, now_s, days)
    conn.commit()
    return changes


# ------------------------------------------------------------------ dashboard API (gateway /crm/callbacks)
COLS = ("callback_id", "user_id", "conversation_id", "kind", "reason", "promise", "when_text", "requested_at",
        "due_at", "status", "assigned_agent_id", "completed_at", "completed_by", "recovered_at", "updated_at")


def list_callbacks(conn, view="open", days=7):
    where = {"open": "COALESCE(status,'pending') IN ('pending','assigned','in_progress')",
             "overdue": "status='overdue'",
             "done": "status='completed'",
             "all": "1=1"}.get(view, "1=1")
    with conn.cursor() as c:
        c.execute(f"SELECT {', '.join(COLS)} FROM orch_callbacks WHERE {where} AND conversation_id IS NOT NULL "
                  "AND requested_at >= NOW() - INTERVAL %s DAY ORDER BY COALESCE(due_at, requested_at) ASC", (days,))
        out = [dict(zip(COLS, [str(v) if v is not None else None for v in r])) for r in c.fetchall()]
        c.execute("SELECT COALESCE(status,'pending'), COUNT(*) FROM orch_callbacks WHERE conversation_id IS NOT NULL "
                  "AND requested_at >= NOW() - INTERVAL %s DAY GROUP BY 1", (days,))
        counts = {k: v for k, v in c.fetchall()}
    conn.commit()
    return {"rows": out, "counts": counts}


def mark_done(conn, cb_id, actor, note=""):
    from .timeutil import iso, now as tnow
    at = iso(tnow())[:19].replace("T", " ")
    with conn.cursor() as c:
        n = c.execute("UPDATE orch_callbacks SET status='completed', completed_at=%s, completed_by=%s "
                      "WHERE callback_id=%s AND COALESCE(status,'pending') <> 'completed'", (at, actor, cb_id))
        if n:
            _audit(c, cb_id, "completed", actor, {"note": note}, at)
    conn.commit()
    return bool(n)


def audit_for(conn, cb_id):
    with conn.cursor() as c:
        c.execute("SELECT event, actor, detail, at FROM orch_audit WHERE entity='callback' AND entity_id=%s ORDER BY id",
                  (cb_id,))
        rows = [{"event": e, "actor": a, "detail": d, "at": str(t)} for e, a, d, t in c.fetchall()]
    conn.commit()
    return rows
