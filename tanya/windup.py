"""AI-C19 Wind-up — one plan call at windup_trigger_pct of a lead's token budget, then one query per turn.

Plain English:
- The moment his usage first passes windup_trigger_pct (and he is not buying or complaining),
  ONE AI call writes a small plan: a recap of this lead's own conversation, his still-open doubt,
  and 3 useful questions. It is saved to tanya:windup_plan:{lead_id}.
- From then on every reply ends with the next unused question — one per turn. When they are all
  used she keeps chatting normally, with a gentle recap tone, until the budget ends.
- The call may fail → 3 generic warm queries from fixed_lines.json (FX-33), no AI needed.
The plan never mentions tokens, limits, time, endings or leaving: he must never feel she has to go.
"""
from datetime import timedelta

from .content_pack import PACK
from .settings import S
from .timeutil import iso

WINDUP_SYSTEM = """TASK: WINDUP
You are Ms Tanya, TG Level's AI assistant. Read ONLY this lead's recent conversation below and write
a small wind-up plan she can use in her next replies. You do not reply to him.
Return ONLY a JSON object with this shape:
{"recap": "<1-2 short lines: what he asked and what he learned>",
 "open_doubt": "<his question that is still unanswered, or empty>",
 "wrapup_queries": ["<question 1>", "<question 2>", "<question 3>"]}

Rules:
- Use ONLY this conversation. Never invent facts, promises, prices or numbers.
- Same language, script and tone as him: Hinglish if he writes Hinglish, Hindi in Devanagari,
  English if he writes English. Warm, simple, 'aap', feminine first person.
- NEVER mention tokens, limits, quotas, time running out, endings, goodbyes or that anyone has
  to leave. Never sound like you have to go; never say this is the last message.
- Each wrapup_query must offer something useful: re-explain a doubt in simpler words, a short recap
  of what he learned, the next small step, or connecting him with the advisor team.
- 3 short, natural questions — one idea each."""

SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"recap": {"type": "string"}, "open_doubt": {"type": "string"},
                         "wrapup_queries": {"type": "array", "items": {"type": "string"}}},
          "required": ["recap", "open_doubt", "wrapup_queries"]}


def fallback_queries(lang: str) -> list:
    """2-3 generic warm queries from fixed_lines.json (FX-33) — used when the plan call fails."""
    q = (PACK.fixed_lines.get("FX-33") or {}).get("queries") or {}
    out = q.get(lang) or q.get("hinglish") or q.get("english") or []
    return [str(x) for x in out][:3]


def build(st):
    """ONE AI call for this lead's last windup_context_minutes. Returns (plan, LLMResult).

    The caller records the result in st['llm_calls'], so its tokens are counted like any AI call."""
    rec, now = st["rec"], st["now"]
    cutoff = iso(now - timedelta(minutes=S.get("windup_context_minutes", 10)))
    msgs = [m for m in rec["messages"] if m.get("delivered", True) and m["at"] >= cutoff]
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in msgs) or "(he has not written anything yet)"
    res = st["llm"].call("windup", "fast", WINDUP_SYSTEM, [{"role": "user", "content": convo}],
                         json_mode=True, temperature=0.0,
                         timeout=S.get("timeout_understand_seconds", 20), max_tokens=500, schema=SCHEMA)
    plan = None
    if res.ok:                                    # json_mode has already stripped ``` fences
        data = res.data or {}
        queries = [str(q).strip() for q in (data.get("wrapup_queries") or []) if str(q).strip()]
        if queries:
            plan = {"recap": str(data.get("recap", ""))[:400], "open_doubt": str(data.get("open_doubt", ""))[:400],
                    "wrapup_queries": queries[:3], "used": 0, "at": iso(now)}
    if plan is None:
        lang = rec["profile"].get("language") or "hinglish"
        plan = {"recap": "", "open_doubt": "", "wrapup_queries": fallback_queries(lang),
                "used": 0, "at": iso(now), "fallback": True}
    return plan, res


def hint(st):
    """(instruction, query) for this turn's reply prompt — the next unused query, one per turn.
    An empty query means they are all used: keep chatting, gently recapping."""
    plan = st["store"].windup_plan_get(st["user_id"])
    if not plan:
        return "", ""
    queries = plan.get("wrapup_queries") or []
    used = int(plan.get("used", 0))
    if used < len(queries):
        q = queries[used]
        return "Answer the user fully first. Then naturally end with this question: " + q, q
    return ("Answer the user fully first. Keep the chat warm and normal, and when it fits gently "
            "recap what he has learned so far."), ""


def mark_used(st):
    """Count the query as shown — called from n_after only when the reply was really sent."""
    plan = st["store"].windup_plan_get(st["user_id"])
    if not plan:
        return
    used = int(plan.get("used", 0))
    if used < len(plan.get("wrapup_queries") or []):
        plan["used"] = used + 1
        st["store"].windup_plan_save(st["user_id"], plan)
