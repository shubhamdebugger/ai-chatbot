"""AI-C03 Sales Chat Orchestrator — one turn of Ms Tanya, as a chain of steps ('nodes').

Plain English — the steps, in order (Architecture v3.2 §5.2):
  load       → his record from memory (Redis in production), new session / new day, mask private numbers
  gate       → HUMAN mode? kill switch? injection? spend ceiling?  (may stop here or give a fixed line)
  understand → labels with evidence (AI, fast model)
  decide     → ONE action by the priority table (code)
  retrieve   → approved knowledge / plan / earlier messages
  compose    → fixed lines + Tanya's written reply (AI)
  guard      → SEBI net + AI check + emoji rule + disclaimer
  after      → counters, facts, promise, cases, temperature, Lead Brief, trace, cost, save, events
Every node takes the 'state' dict and returns it. graph.py wires them (LangGraph or plain loop).
"""
import re

from . import memory_model as mm
from . import policy
from .brief import lead_brief
from .content_pack import PACK
from .decider import Decision, FIXED_ONLY, COUNTS_AS_EDUCATION, NO_EMOJI, decide
from .guard_input import injection, mask
from .guard_output import ai_check, amounts, approved_text_for, emoji_rule, net
from .handoff import open_case, request_callback, two_slots, when_phrase
from .knowledge import is_placeholder
from .prompts import REPLY_SCHEMA, build
from .scoring import temperature
from .settings import S
from .timeutil import iso, parse, stamp
from .understand import defaults, understand

USEFUL_ANSWERS = {"ANSWER_EDUCATION", "ANSWER", "ANSWER_SUPPORT", "ANSWER_PRICE", "ANSWER_ONLY"}


def _ev(st, etype, **data):
    st["events"].append({"type": etype, "user_id": st["user_id"], "at": iso(st["now"]), **data})


def _usage(st, res):
    st["llm_calls"].append({"purpose": res.purpose, "provider": res.provider, "model": res.model,
                            "in": res.input_tokens, "out": res.output_tokens, "ms": res.ms,
                            "cost_inr": res.cost_inr, "ok": res.ok, "error": res.error})


# ------------------------------------------------------------------ load
def n_load(st):
    now, store, uid = st["now"], st["store"], st["user_id"]
    rec = store.get(uid)
    st["cold_start"] = False
    if rec is None:
        seed = st["seed_fn"](uid)
        if seed is None:                       # not in memory and not loaded yet (Architecture §7.7.5)
            seed, st["cold_start"] = {"consent": False}, True
        rec = mm.new_record(uid, seed, now)
    if st.get("conversation_id") and str(st["conversation_id"]) != str(uid):
        rec["conversation_id"] = str(st["conversation_id"])   # the CRM thread he wrote in (HUMAN flag, notes)
    st["rec"] = rec
    st["new_session"] = mm.ensure_session(rec, now)
    mm.ensure_day(rec, now)
    if st["new_session"]:
        _ev(st, "session_start", session=rec["session"]["id"])
    if st["kind"] == "message":
        masked, kinds = mask(st["text"])
        st["masked"], st["masked_kinds"], st["injection"] = masked, kinds, injection(st["text"])
        st["msg_no"] = mm.add_message(rec, "user", masked, now, meta={"masked": kinds, "event_id": st.get("event_id")})
        rec["session"]["user_msgs"] += 1
        rec["counters"]["user_msgs_total"] += 1
        _ev(st, "message", role="user", n=st["msg_no"], text=masked, event_id=st.get("event_id"))
    else:
        st["masked"], st["masked_kinds"], st["injection"], st["msg_no"] = "", [], False, None
    return st


# ------------------------------------------------------------------ gate
def n_gate(st):
    rec, now = st["rec"], st["now"]
    g, line, reason = policy.gate(rec, st["store"], now, st["injection"], st.get("masked", ""))
    st["limited"] = st["store"].killswitch() == "limited"
    if st["kind"] == "app_open" and g == policy.GATE_GO:
        last_greet = rec["journey"].get("greeted_at")
        if last_greet and (now - parse(last_greet)).total_seconds() < S.get("greeting_min_gap_hours", 3) * 3600:
            g, reason = policy.GATE_SILENT, "GREETED_RECENTLY"
    st["gate"], st["gate_line"], st["gate_reason"] = g, line, reason
    if g == policy.GATE_FIXED:
        st["decision"] = Decision("FIXED_GATE", reason, fixed_line=line)
        st["labels"] = defaults(st["masked"])
    return st


def route_after_gate(st):
    return {"go": "understand", "fixed": "compose", "silent": "after"}[st["gate"]]


# ------------------------------------------------------------------ understand
def n_understand(st):
    rec, now = st["rec"], st["now"]
    if st["kind"] != "message":
        st["labels"] = defaults("")
        st["labels"]["language"] = rec["profile"].get("language", "hinglish")
        st["labels"]["failed"] = False
        return st
    history = mm.history_for_prompt(rec)[:-1]
    labels, res = understand(st["llm"], st["masked"], history)
    _usage(st, res)
    st["labels"] = labels
    L = labels["labels"]
    sess = rec["session"]
    rec["profile"]["language"] = labels["language"]
    # --- session counters (this message included) ---
    if L["small_talk"]["on"]:
        sess["small_talk"] += 1
    if L["abuse"]["on"]:
        sess["abuse"] += 1
    if L["flirting"]["on"]:
        sess["flirt"] += 1
    if L["not_helpful"]["on"] or labels.get("failed"):
        sess["failed"] += 1
    # --- facts and signals: only with consent (DPDP agreement in the app) ---
    if rec["profile"].get("consent"):
        for f in labels["new_facts"]:
            mm.set_fact(rec, f["field"], f["value"], f["his_words"], "chat", st["msg_no"], now, True, f["confidence"])
            _ev(st, "fact", field=f["field"], value=f["value"], his_words=f["his_words"], msg_no=st["msg_no"],
                source="chat", confidence=f["confidence"])
        for sig in ("interest", "purchase_intent", "timing_objection", "prefers_call"):
            if L[sig]["on"]:
                mm.set_signal(rec, sig, now, L[sig]["evidence"])
                _ev(st, "signal", name=sig, evidence=L[sig]["evidence"])
    return st


# ------------------------------------------------------------------ decide
def n_decide(st):
    rec = st["rec"]
    if st["kind"] == "app_open":
        st["decision"] = Decision("GREETING", "R-GREET")
    else:
        st["decision"] = decide(st["labels"], rec, st["now"], st.get("limited", False))
    if st["decision"].action == "PAUSE_SELLING":
        rec["session"]["selling_paused"] = True
    return st


# ------------------------------------------------------------------ retrieve
_STOP = set("hai hain kya ka ki ke ko se me mein aur ya the tha thi ho hoon main aap mujhe mera meri is us ye yeh wo woh "
            "the a an is are to of in on for and or i you me my what how".split())


def n_retrieve(st):
    d, rec = st["decision"], st["rec"]
    st["hits"], st["plan_row"], st["why_line"], st["past"] = [], None, "", []
    q = " ".join([st["masked"]] + st["labels"].get("topics", []))
    K = st["kb"]
    if d.action == "REFUSE_AND_TEACH":
        # always the Logic · Risk · Exit lesson first, then the closest lesson to his question
        base = K.search("logic risk exit checklist before any trade", top_k=1, categories=["Lesson"])
        more = [h for h in K.search(q, top_k=2, categories=["Lesson"]) if not base or h["chunk_id"] != base[0]["chunk_id"]]
        st["hits"] = (base + more)[:2]
    elif d.action in ("ANSWER_EDUCATION", "ANSWER", "ANSWER_ONLY", "LOG_GRIEVANCE", "GREETING"):
        st["hits"] = K.search(q) if q.strip() else []
    elif d.action == "ANSWER_SUPPORT":
        st["hits"] = K.search(q, categories=["FAQ"])
        if not st["hits"] or is_placeholder(st["hits"][0]):
            # no approved answer → never guess: a case goes to the team (FX-19)
            st["decision"] = Decision("SUPPORT_CASE", "R17-NOAPPROVED", fixed_line="FX-19")
    elif d.action == "ANSWER_PRICE":
        st["plan_row"], st["why_line"] = PACK.plan_for_profile(rec["facts"])
    if st["labels"]["labels"].get("refers_to_past", {}).get("on"):
        words = [w for w in re.findall(r"\w+", st["masked"].lower()) if w not in _STOP and len(w) > 2]
        for m in reversed(rec["messages"][:-11]):
            if any(w in m["text"].lower() for w in words):
                st["past"].append(f"{m['at'][:16].replace('T', ' ')} {m['role']}: {m['text'][:200]}")
            if len(st["past"]) >= 3:
                break
    return st


# ------------------------------------------------------------------ compose
def n_compose(st):
    rec, now, d = st["rec"], st["now"], st["decision"]
    lang = st["labels"].get("language") or rec["profile"].get("language", "hinglish")
    name = rec["profile"].get("name") or ""
    st["bubbles"], st["ai_data"] = [], {}
    B = st["bubbles"]
    # 1. Disclosure before anything else, once in his life (FX-01)
    if not rec["journey"]["disclosed"]:
        B.append({"id": "FX-01", "kind": "fixed", "text": PACK.fixed("FX-01", lang, name=name)})
        rec["journey"]["disclosed"] = True
    # 2. The fixed line for this action, with values filled by code
    fx = d.fixed_line
    if d.action in ("HAND_OVER_PERSON",):
        fx = "FX-08" if policy.in_calling_hours(now) else "FX-07"
    if fx:
        vals = {"name": name, "limit": S.get("education_questions_per_day", 30)}
        if fx == "FX-10":
            vals["case_no"] = open_case(rec, st["store"], "grievance", st["masked"], now)
            _ev(st, "case", case_no=vals["case_no"], kind="grievance", text=st["masked"])
        if fx == "FX-19":
            vals["case_no"] = open_case(rec, st["store"], "support", st["masked"], now)
            _ev(st, "case", case_no=vals["case_no"], kind="support", text=st["masked"])
        if fx in ("FX-07", "FX-08", "FX-14"):
            vals["when"] = when_phrase(now, lang)
        if fx in ("FX-07", "FX-08"):
            cb = request_callback(rec, "person", now, vals["when"])
            _ev(st, "callback", **cb)
        if fx == "FX-14":
            plan, _ = PACK.plan_for_profile(rec["facts"])
            vals["plan_line"] = (f"{plan['plan_name']} — ₹{int(float(plan['price_inr'])):,} ({plan['duration']}). "
                                 if plan else "")
            cb = request_callback(rec, "purchase", now, vals["when"])
            _ev(st, "callback", **cb)
        if fx == "FX-13":
            slots, phrases = two_slots(now, lang)
            vals["slot_1"], vals["slot_2"] = phrases
            cb = request_callback(rec, "call_preference", now, " / ".join(phrases), [iso(s) for s in slots])
            _ev(st, "callback", **cb)
        if fx == "FX-09":
            rec["journey"]["consent_line_given"] = True
        B.append({"id": fx, "kind": "fixed", "text": PACK.fixed(fx, lang, **vals)})
    # 3. Tanya's own words, unless the action is fixed-only
    if d.action not in FIXED_ONLY and d.action not in ("SUPPORT_CASE", "FIXED_GATE"):
        system, ex_ids = build(d.action, rec, st["labels"], mm.trial_day(rec, now), st["hits"], d.addon,
                               d.addon_detail, st.get("plan_row"), st.get("why_line"))
        if st.get("past"):
            system = system.replace("TASK: REPLY", "EARLIER MESSAGES (quote with their date if he asks):\n" +
                                    "\n".join(st["past"]) + "\n\nTASK: REPLY")
        msgs = mm.history_for_prompt(rec)
        if st["kind"] == "app_open":
            msgs = msgs + [{"role": "user", "content": "(he opened the app)"}]
        res = st["llm"].call("reply", d.tier, system, msgs, json_mode=True,
                             temperature=S.get("temperature_reply", 0.4),
                             timeout=S.get("timeout_reply_seconds", 40), max_tokens=900, schema=REPLY_SCHEMA)
        _usage(st, res)
        st["golden_used"] = ex_ids
        reply = (res.data or {}).get("reply", "").strip() if res.ok else ""
        if reply:
            st["ai_data"] = res.data
            B.append({"id": "AI", "kind": "ai", "text": reply})
        else:
            rec["session"]["failed"] += 1
            B.append({"id": "FX-12", "kind": "fixed", "text": PACK.fixed("FX-12", lang)})
            st["notes"] = st.get("notes", []) + [f"reply call failed: {res.error}"]
    # 4. Chat-first line after the first useful answer in his trial (FX-02)
    if d.action in USEFUL_ANSWERS and not rec["journey"]["chat_first_given"] and rec["profile"].get("consent"):
        B.append({"id": "FX-02", "kind": "fixed", "text": PACK.fixed("FX-02", lang)})
        rec["journey"]["chat_first_given"] = True
    return st


# ------------------------------------------------------------------ guard
def n_guard(st):
    rec, d = st["rec"], st["decision"]
    lang = st["labels"].get("language", "hinglish")
    st["guard"] = {"net_blocked": False, "net_hits": [], "warnings": [], "ai_pass": None, "ai_problems": [],
                   "replaced": False}
    ai = [b for b in st["bubbles"] if b["kind"] == "ai"]
    if ai:
        b = ai[0]
        allowed = PACK.allowed_amounts()
        for m in rec["messages"]:
            if m["role"] == "user":
                allowed |= amounts(m["text"])
        for h in st.get("hits", []):
            allowed |= amounts(h["text"])
        blocked, hits, warns = net(b["text"], allowed)
        st["guard"].update(net_blocked=blocked, net_hits=hits, warnings=warns)
        if not blocked:
            ok, problems, res = ai_check(st["llm"], b["text"], approved_text_for(st.get("hits"), d.action))
            _usage(st, res)
            st["guard"].update(ai_pass=ok, ai_problems=problems)
        if blocked or not st["guard"]["ai_pass"]:
            st["guard"]["replaced"] = True
            rec["session"]["failed"] += 1
            st["ai_data"] = {}
            b.update(id="FX-12", kind="fixed", text=PACK.fixed("FX-12", lang), blocked_text=b["text"])
        else:
            b["text"] = emoji_rule(b["text"], allowed=d.action not in NO_EMOJI)
    # the fixed disclaimer after education or a refusal, once per session (FX-17)
    if d.action in ("ANSWER_EDUCATION", "REFUSE_AND_TEACH") and not rec["session"]["disclaimer_shown"] \
            and any(b["kind"] == "ai" for b in st["bubbles"]):
        st["bubbles"].append({"id": "FX-17", "kind": "fixed", "text": PACK.fixed("FX-17", lang)})
        rec["session"]["disclaimer_shown"] = bool(S.get("disclaimer_once_per_session", True))
    return st


# ------------------------------------------------------------------ after
def n_after(st):
    rec, now, store = st["rec"], st["now"], st["store"]
    d = st.get("decision") or Decision("NONE", st.get("gate_reason", ""))
    st.setdefault("bubbles", [])
    st.setdefault("guard", {})
    st.setdefault("labels", defaults(st.get("masked", "")))
    delivered_ai = any(b["kind"] == "ai" for b in st["bubbles"])
    data = st.get("ai_data") or {}
    # counters and journey
    if d.action in COUNTS_AS_EDUCATION and delivered_ai:
        rec["counters"]["education_used"] += 1
    if data.get("asked_field") and d.addon == "ASK_MISSING_FACT":
        rec["session"]["profile_q"] += 1
    if d.addon == "SHOW_VALUE_CARD" and delivered_ai:
        rec["journey"]["cards_shown"].append(d.addon_detail["card"])
        _ev(st, "value_card", card=d.addon_detail["card"])
    if data.get("last_promise"):
        rec["journey"]["last_promise"] = data["last_promise"]
        rec["journey"]["last_promise_at"] = iso(now)
    if st["kind"] == "app_open" and st["bubbles"]:
        rec["journey"]["greeted_at"] = iso(now)
    # her messages, word for word
    for b in st["bubbles"]:
        n = mm.add_message(rec, "assistant", b["text"], now, meta={"id": b["id"], "action": d.action})
        _ev(st, "message", role="assistant", n=n, text=b["text"], line_id=b["id"], action=d.action)
    # temperature and brief (code only)
    temp, evidence = temperature(rec, now)
    rec["last_temperature"] = {"label": temp, "evidence": evidence, "at": iso(now)}
    st["brief"] = lead_brief(rec, now, temp, evidence)
    # cost ledger
    cost = round(sum(c["cost_inr"] for c in st["llm_calls"]), 4)
    today_total = store.ledger_add(cost, now) if cost else store.ledger_get(now)
    # trace (AI-C15)
    L = st["labels"]["labels"]
    st["trace"] = {
        "at": stamp(now), "user_id": st["user_id"], "kind": st["kind"], "session": rec["session"]["id"],
        "trial_day": mm.trial_day(rec, now), "gate": st.get("gate_reason"),
        "masked": st.get("masked_kinds"), "injection": st.get("injection"),
        "language": st["labels"].get("language"), "mood": st["labels"].get("mood"),
        "labels_on": {k: v["evidence"] for k, v in L.items() if v["on"]},
        "understand_failed": st["labels"].get("failed", False),
        "new_facts": st["labels"].get("new_facts", []),
        "action": d.action, "reason": d.reason, "addon": d.addon, "addon_detail": d.addon_detail,
        "tier": d.tier, "knowledge": [f"{h['doc_id']} ({h['score']})" for h in st.get("hits", [])],
        "golden": st.get("golden_used", []), "guard": st["guard"],
        "bubbles": [b["id"] for b in st["bubbles"]],
        "temperature": temp, "counters": {"education_used": rec["counters"]["education_used"],
                                          "education_limit": S.get("education_questions_per_day", 30),
                                          "failed_this_session": rec["session"]["failed"],
                                          "profile_q_this_session": rec["session"]["profile_q"]},
        "suppressed": mm.suppression(rec)[1], "llm_calls": st["llm_calls"], "cost_inr": cost,
        "spend_today_inr": round(today_total, 2), "spend_alert": policy.spend_alert(store, now),
        "content_version": PACK.version_string(), "notes": st.get("notes", []), "cold_start": st.get("cold_start"),
    }
    _ev(st, "trace", trace=st["trace"])
    _ev(st, "state", version=rec["version"] + 1, temperature=temp, mode=rec["mode"]["state"],
        journey=rec["journey"], counters=rec["counters"])
    if st["kind"] == "message" or st["bubbles"]:
        _ev(st, "brief", text=st["brief"])
    store.save(rec)
    store.emit(st["events"])
    return st
