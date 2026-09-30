"""AI-C09 Next-Best-Action — the Lead Brief and the Agent Short-Call Card.

Plain English: what the sales agent needs, built by CODE from the memory record
(no AI cost, nothing invented). Every line carries its date, day and time.
- Lead Brief (Architecture §9.2 note 2): written to the CRM as Ms Tanya's own protected note.
- Agent Short-Call Card (Architecture §13.18): shown with every booked call.
"""
from . import memory_model as mm
from .content_pack import PACK
from .settings import S
from .timeutil import parse, stamp


def lead_brief(rec, now, temp, evidence) -> str:
    p = rec["profile"]
    day = mm.trial_day(rec, now)
    lines = [f"MS TANYA — LEAD BRIEF (auto) · {p.get('name') or rec['user_id']} · updated {stamp(now)}",
             f"Temperature: {temp}" + (f" — {'; '.join(evidence)}" if evidence else ""),
             f"Trial day {day} of {S.get('trial_length_days', 3)} · consent: {'yes' if p.get('consent') else 'NO'}",
             "Engagement: chat only — app events unknown"]
    if rec["facts"]:
        lines.append("In his words / known facts:")
        for k, f in rec["facts"].items():
            who = {"chat": "told Tanya", "agent_note": "agent note", "crm_field": "CRM"}.get(f["source"], f["source"])
            words = f' "{f["his_words"]}"' if f.get("his_words") else ""
            lines.append(f"  - {k}: {f['value']}{words} ({who}, {stamp(parse(f['at']))})")
    open_conf = [c for c in rec["conflicts"] if c["status"] == "open"]
    if open_conf and S.get("contradictions_visible_to_agents", False):   # shadow mode until proven (§9.4.7)
        lines.append("Check on the call:")
        for c in open_conf:
            lines.append(f"  - {c['field']}: agent says '{c['agent_value']}' · he told Tanya '{c['customer_value']}' "
                         f"({stamp(parse(c['at']))})")
    plan, why = PACK.plan_for_profile(rec["facts"])
    if plan:
        lines.append(f"Plan by profile table: {plan['plan_name']} ₹{int(float(plan['price_inr'])):,} ({plan['status']})")
    if rec["journey"].get("last_promise"):
        lines.append(f"Last promise: {rec['journey']['last_promise']}")
    if rec["cases"]:
        lines.append("Cases: " + "; ".join(f"{c['case_no']} {c['kind']} {c['status']}" for c in rec["cases"]))
    if rec["callbacks"]:
        lines.append("Callbacks: " + "; ".join(f"{c['kind']} {c['state']} ({c['when_text']})" for c in rec["callbacks"]))
    pref = rec["signals"].get("prefers_call")
    lines.append("Channel preference: " + (f"CALL — \"{pref['evidence']}\"" if pref else "chat"))
    return "\n".join(lines)


def agent_card(rec, now, purpose="") -> dict:
    """Agent Short-Call Card — the call is as precise as Tanya would be (Architecture §13.18)."""
    p = rec["profile"]
    plan, why = PACK.plan_for_profile(rec["facts"])
    return {
        "purpose": purpose or "decision point / his request for a call",
        "time_box": "5–7 minutes",
        "who": f"{p.get('name')} · trial day {mm.trial_day(rec, now)} · language: "
               f"{(rec['facts'].get('language_pref') or {}).get('value') or p.get('language')}",
        "do_not_ask": sorted(mm.known_fields(rec)),
        "in_his_words": [f"{k}: \"{v['his_words']}\" ({stamp(parse(v['at']))})"
                         for k, v in rec["facts"].items() if v.get("his_words")],
        "covered_in_chat": {"last_promise": rec["journey"].get("last_promise"),
                            "value_cards_shown": rec["journey"].get("cards_shown", [])},
        "plan_to_mention": f"{plan['plan_name']} — price and inclusions from the pricing source only" if plan else "none",
        "compliance": ["No trade calls or market views", "No return or accuracy promises",
                       "SEBI registration is not a guarantee", "The call is recorded"],
        "close": "One next step: plan link, 'think and continue in chat', or a second call only if he asks",
    }
