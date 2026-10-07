"""Smart human-handoff recovery (Tushar, 06-Oct-2026).

Plain English:
- When Tanya herself decides a person should take over (config handoff_human_actions, e.g. "A team member will
  join you shortly"), the chat goes to HUMAN mode and a recovery deadline starts:
    business hours (business_hours_start..end, IST)  -> handoff_recovery_day_minutes   (10 min)
    outside business hours                          -> handoff_recovery_night_hours    (12 h)
- An agent REPLY in the chat before the deadline keeps HUMAN mode (Tanya never comes back on her own then).
  An agent merely ASSIGNED to the chat is recorded but is not enough.
- At the deadline, with no agent reply (checked in the CRM itself, in case a staff webhook was lost):
  Tanya posts FX-32 (day) / FX-33 (night), HUMAN mode ends, and if the customer wrote while waiting, Tanya
  answers his latest message.
- State: Redis (tanya:handoff:{conv}, tanya:handoff_due) for the live check; every step is copied to MySQL
  (orch_handoffs + orch_audit) by the persister. After a Redis loss the open handoffs are re-seeded from MySQL.
"""
import sys
import time
from datetime import timedelta

from .content_pack import PACK
from .settings import S
from .timeutil import hm, iso, parse


# ------------------------------------------------------------------ business hours
def in_business_hours(now) -> bool:
    sh, sm = hm(S.get("business_hours_start", "07:00"))
    eh, em = hm(S.get("business_hours_end", "19:00"))
    t = now.hour * 60 + now.minute
    return sh * 60 + sm <= t < eh * 60 + em


def next_business_start(now):
    sh, sm = hm(S.get("business_hours_start", "07:00"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    return start if now < start else start + timedelta(days=1)


def handoff_deadline(now):
    """(period, deadline) for a handoff started now."""
    if in_business_hours(now):
        return "day", now + timedelta(minutes=S.get("handoff_recovery_day_minutes", 10))
    return "night", now + timedelta(hours=S.get("handoff_recovery_night_hours", 12))


def callback_due(now):
    """Business hours: now + callback_sla_minutes. Otherwise: next business-day start + callback_sla_minutes."""
    sla = timedelta(minutes=S.get("callback_sla_minutes", 10))
    return now + sla if in_business_hours(now) else next_business_start(now) + sla


# ------------------------------------------------------------------ handoff lifecycle
def _audit(store, entity_id, event, actor, now, **detail):
    store.emit([{"type": "audit", "entity": "handoff", "entity_id": entity_id, "event": event, "actor": actor,
                 "detail": detail, "user_id": detail.get("user_id") or "-", "at": iso(now)}])


def start_handoff(store, conversation_id, user_id, action, reason, now, last_message_id=None):
    """Tanya hands the chat to staff. Idempotent: an open handoff of this chat is kept, not restarted."""
    conv = str(conversation_id)
    h = store.handoff_get(conv)
    if h and h.get("status") == "open":
        return h
    period, due = handoff_deadline(now)
    data = {"handoff_id": f"HO-{conv}-{int(now.timestamp())}", "conversation_id": conv, "user_id": str(user_id),
            "action": action, "reason": reason, "period": period, "started_at": iso(now), "due_at": iso(due),
            "status": "open", "last_message_id": str(last_message_id or "")}
    hours = max(S.get("human_mode_release_hours", 12), (due - now).total_seconds() / 3600 + 1)
    store.set_human_flag(conv, hours, now)                     # HUMAN at once; the deadline below ends it
    store.handoff_start(conv, data, due.timestamp())
    store.staff_wait_start(conv, user_id, now)                 # staff-silent alert for the team lead (5 min)
    store.emit([{"type": "handoff", "op": "start", "at": iso(now), **data},
                {"type": "mode", "user_id": str(user_id), "at": iso(now), "conversation_id": conv, "mode": "HUMAN",
                 "by": "tanya_handoff"}])
    _audit(store, data["handoff_id"], "started", "tanya", now, user_id=str(user_id), conversation_id=conv,
           action=action, reason=reason, period=period, due_at=iso(due))
    print(f"[handoff] START conv={conv} action={action} reason={reason} period={period} due={iso(due)}",
          file=sys.stderr, flush=True)
    return data


def agent_replied(store, conversation_id, now, agent_id=""):
    """A staff member answered: HUMAN mode stays, the recovery deadline is cancelled."""
    conv = str(conversation_id)
    h = store.handoff_get(conv)
    if not h or h.get("status") != "open":
        return False
    store.handoff_end(conv, "agent_replied")
    store.handoff_update(conv, agent_replied_at=iso(now), agent_replied_id=str(agent_id or ""))
    store.emit([{"type": "handoff", "op": "update", "handoff_id": h["handoff_id"], "status": "agent_replied",
                 "agent_replied_at": iso(now), "agent_id": str(agent_id or ""), "at": iso(now),
                 "user_id": h.get("user_id")}])
    _audit(store, h["handoff_id"], "agent_replied", f"agent:{agent_id}", now, user_id=h.get("user_id"),
           conversation_id=conv)
    return True


def customer_waiting_again(store, conversation_id, user_id, now, message_id):
    """The customer writes while the chat is HUMAN but no deadline is running any more (an agent already replied,
    or staff took the chat over). The same rule as a fresh handoff applies to THIS message: if no staff member
    answers it within the day (10 min) / night (12 h) window, Tanya takes the chat back and answers it
    (07-Oct-2026: after one agent reply the chat stayed HUMAN for good and later questions were never answered)."""
    conv = str(conversation_id)
    h = store.handoff_get(conv)
    if h and h.get("status") == "open":
        return None                                           # a deadline is already running for this chat
    return start_handoff(store, conv, user_id, "CUSTOMER_WAITING", "R-WAIT-AFTER-STAFF", now,
                         last_message_id=message_id)


def released_by_staff(store, conversation_id, now, how):
    """Staff closed the chat / typed #bot during an open handoff."""
    h = store.handoff_get(str(conversation_id))
    if h and h.get("status") in ("open", "released"):
        store.handoff_end(str(conversation_id), "released")
        store.emit([{"type": "handoff", "op": "update", "handoff_id": h["handoff_id"], "status": "released",
                     "at": iso(now), "user_id": h.get("user_id")}])
        _audit(store, h["handoff_id"], "released_by_staff", "staff", now, how=how, user_id=h.get("user_id"),
               conversation_id=str(conversation_id))


def _customer_waiting(adapter, conv, owner, after_id):
    """Customer messages written while waiting for staff (after the handoff message), oldest first."""
    try:
        after = int(after_id or 0)
    except ValueError:
        after = 0
    out = []
    for m in adapter.get_conversation(conv, limit=60):
        if int(m.get("id", 0)) > after and str(m.get("user_id")) == str(owner) \
                and str(m.get("user_type")) not in ("agent", "admin", "bot"):
            out.append(m)
    return out


def recovery_check(store, adapter, intake=None, now=None):
    """Run every few seconds (reconcile process). Returns the conversations given back to Tanya."""
    from .timeutil import now as tnow
    now = now or tnow()
    recovered = []
    for conv in store.handoff_due(now.timestamp()):
        h = store.handoff_get(conv)
        if not h or h.get("status") != "open":
            store.handoff_end(conv, (h or {}).get("status") or "gone")
            continue
        # 1. the CRM is the truth: an agent reply whose webhook was lost still keeps HUMAN mode
        try:
            if adapter.staff_replied_after(conv, h.get("last_message_id")):
                agent_replied(store, conv, now, agent_id="(seen in CRM)")
                continue
            agent = adapter.conversation_agent(conv)
        except Exception as e:                                  # CRM unreachable: try again next run
            print(f"[handoff] check deferred conv={conv}: {type(e).__name__}", file=sys.stderr, flush=True)
            continue
        if agent and agent != h.get("agent_assigned_id"):
            store.handoff_update(conv, agent_assigned_id=agent, agent_assigned_at=iso(now))
            store.emit([{"type": "handoff", "op": "update", "handoff_id": h["handoff_id"], "agent_assigned_id": agent,
                         "agent_assigned_at": iso(now), "at": iso(now), "user_id": h.get("user_id")}])
            _audit(store, h["handoff_id"], "agent_assigned_no_reply", "crm", now, agent_id=agent,
                   user_id=h.get("user_id"), conversation_id=conv)
        # 2. recovery message, posted once (marker survives a crash between post and release)
        if not h.get("recovery_posted"):
            rec = store.get(h.get("user_id")) or {}
            lang = (rec.get("profile") or {}).get("language", "hinglish")
            line = "FX-32" if h.get("period") == "day" else "FX-33"
            try:
                crm_id = adapter.post_message(conv, PACK.fixed(line, lang))
            except Exception as e:
                print(f"[handoff] recovery post failed conv={conv}: {type(e).__name__} - retry", file=sys.stderr,
                      flush=True)
                continue
            store.handoff_update(conv, recovery_posted=str(crm_id or "1"), recovery_line=line)
            h["recovery_posted"] = str(crm_id or "1")
        # 3. Tanya takes the chat back
        store.release_conversation(conv, now)
        store.handoff_end(conv, "recovered")
        store.emit([{"type": "handoff", "op": "update", "handoff_id": h["handoff_id"], "status": "recovered",
                     "recovered_at": iso(now), "recovery_message_id": h.get("recovery_posted", ""),
                     "agent_assigned_id": agent or "", "at": iso(now), "user_id": h.get("user_id")},
                    {"type": "mode", "user_id": h.get("user_id"), "at": iso(now), "conversation_id": conv,
                     "mode": "BOT", "by": "handoff_recovery"},
                    {"type": "callback_event", "conversation_id": conv, "event": "recovered_by_tanya", "at": iso(now),
                     "user_id": h.get("user_id")}])
        _audit(store, h["handoff_id"], "recovered_by_tanya", "tanya", now, user_id=h.get("user_id"),
               conversation_id=conv, period=h.get("period"), agent_assigned=agent or "",
               waited_s=int(now.timestamp() - parse(h["started_at"]).timestamp()))
        store.summary_touch(conv, h.get("user_id"), now.timestamp(), force=True)
        # 4. answer what the customer wrote while waiting (his latest message), so nothing is left unanswered
        if intake is not None:
            try:
                after_id = h.get("last_message_id")
                if h.get("action") == "CUSTOMER_WAITING" and str(after_id or "").isdigit():
                    after_id = int(after_id) - 1       # the message that started this wait is itself unanswered
                waiting = _customer_waiting(adapter, conv, h.get("user_id"), after_id)
            except Exception:
                waiting = []
            if waiting:
                m = waiting[-1]
                intake.enqueue({"event_id": f"{m['id']}-resume", "kind": "user_message", "user_id": h.get("user_id"),
                                "conversation_id": conv, "text": str(m.get("message", "")),
                                "received_ms": int(time.time() * 1000), "source": "handoff_recovery"})
        print(f"[handoff] RECOVERED conv={conv} period={h.get('period')} agent_assigned={agent or '-'}",
              file=sys.stderr, flush=True)
        recovered.append(conv)
    return recovered


def reseed_from_db(store, conn):
    """After a Redis loss: put every open handoff in MySQL back into the Redis deadline set."""
    n = 0
    with conn.cursor() as c:
        c.execute("SELECT handoff_id, conversation_id, user_id, action, reason, period, started_at, due_at, "
                  "last_message_id FROM orch_handoffs WHERE status='open'")
        rows = c.fetchall()
    for hid, conv, uid, action, reason, period, started, due, last_id in rows:
        h = store.handoff_get(conv)
        if h and h.get("handoff_id") == hid:
            continue
        from .timeutil import IST
        st = started.replace(tzinfo=IST) if started.tzinfo is None else started
        du = due.replace(tzinfo=IST) if due.tzinfo is None else due
        store.handoff_start(conv, {"handoff_id": hid, "conversation_id": conv, "user_id": uid, "action": action,
                                   "reason": reason, "period": period, "started_at": iso(st), "due_at": iso(du),
                                   "status": "open", "last_message_id": last_id or ""}, du.timestamp())
        from .timeutil import now as tnow
        left_h = max(0.0, (du - tnow()).total_seconds() / 3600)
        store.set_human_flag(conv, left_h + 1, tnow())          # the HUMAN flag was lost with Redis too
        n += 1
    return n
