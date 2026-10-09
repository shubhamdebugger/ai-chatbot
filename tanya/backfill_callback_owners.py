"""One-off, run BEFORE the CRM page stops reading the chat's agent (09-Oct-2026).

Until now an open callback showed the agent of its chat when nobody was stored. The CRM no longer does that: the owner
is what is stored (admin's pick in slot.agent_id, else orch_callbacks.assigned_agent_id). This stores the owner the old
way for every open callback that has none, so no agent loses a callback. Callbacks whose chat has no agent stay
unassigned (admin assigns). Nothing is overwritten.

    python -m tanya.backfill_callback_owners            # dry run: prints what it would store
    python -m tanya.backfill_callback_owners --apply    # stores it (audit event 'assigned', actor 'migration')
"""
import sys

from .callbacks import _audit, _owner, _slot, connect


def run(apply=False, days=30):
    from .crm_adapter import make_adapter
    from .timeutil import iso, now as tnow
    adapter = make_adapter()
    now_s = iso(tnow())[:19].replace("T", " ")
    conn = connect()
    todo, stay = [], []
    with conn.cursor() as c:
        c.execute("SELECT callback_id, user_id, conversation_id, assigned_agent_id, slot FROM orch_callbacks "
                  "WHERE COALESCE(status,'pending') <> 'completed' AND state IN ('requested','booked') "
                  "AND requested_at >= NOW() - INTERVAL %s DAY", (days,))
        for cb_id, uid, conv, stored, raw in c.fetchall():
            slot = _slot(raw)
            if _owner(slot, stored) or slot.get("agent_cleared"):
                continue
            conv = str(conv or slot.get("sb_conversation_id") or "")
            agent = adapter.conversation_agent(conv) if conv.isdigit() else ""
            (todo if agent else stay).append((cb_id, uid, conv, agent))
        for cb_id, uid, conv, agent in todo:
            print(f"{'STORE' if apply else 'would store'} agent {agent} on {cb_id} (user {uid}, chat {conv})")
            if apply:
                c.execute("UPDATE orch_callbacks SET assigned_agent_id=%s WHERE callback_id=%s AND COALESCE(assigned_agent_id,'')=''",
                          (agent, cb_id))
                _audit(c, cb_id, "assigned", "migration", {"agent_id": agent, "source": "chat"}, now_s)
        for cb_id, uid, conv, _ in stay:
            print(f"stays unassigned (chat {conv or '-'} has no agent): {cb_id} (user {uid})")
    if apply:
        conn.commit()
    print(f"{len(todo)} {'stored' if apply else 'to store'}, {len(stay)} left for the admin"
          + ("" if apply else " — dry run, add --apply to store"))


if __name__ == "__main__":
    run("--apply" in sys.argv)
