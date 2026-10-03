"""The turn as a graph — LangGraph when installed (Backbone: 'LangGraph for bounded workflows'),
or the same steps in a plain loop.

Plain English: the eight steps of turn.py are the 'nodes'. The only branch is after
the gate: go on to understand, jump to compose for a fixed line, or stop (silent).
Code decides every branch — the AI never picks the path. USE_LANGGRAPH=0 in .env
forces the plain loop; both give the same result (tests check this).
"""
import time
from typing import Any, TypedDict

from . import turn
from .settings import S

ORDER = ["load", "gate", "understand", "decide", "retrieve", "compose", "guard", "after"]
def _timed(name, fn):
    """Measure each step of the turn (node_ms in the trace and the worker's [turn] log line)."""
    def run(st):
        t0 = time.perf_counter()
        out = fn(st)
        ms = dict(out.get("node_ms") or {})
        ms[name] = int((time.perf_counter() - t0) * 1000)
        out["node_ms"] = ms
        return out
    return run


NODES = {k: _timed(k, f) for k, f in {
    "load": turn.n_load, "gate": turn.n_gate, "understand": turn.n_understand, "decide": turn.n_decide,
    "retrieve": turn.n_retrieve, "compose": turn.n_compose, "guard": turn.n_guard, "after": turn.n_after}.items()}


class TurnState(TypedDict, total=False):
    """Everything one turn carries from step to step."""
    user_id: str
    kind: str
    text: str
    event_id: str
    conversation_id: str
    now: Any
    store: Any
    llm: Any
    kb: Any
    seed_fn: Any
    rec: dict
    masked: str
    masked_kinds: list
    injection: bool
    msg_no: Any
    new_session: bool
    cold_start: bool
    gate: str
    gate_line: Any
    gate_reason: str
    smalltalk: str
    node_ms: dict
    limited: bool
    labels: dict
    decision: Any
    hits: list
    plan_row: Any
    why_line: str
    past: list
    bubbles: list
    ai_data: dict
    golden_used: list
    guard: dict
    notes: list
    llm_calls: list
    events: list
    brief: str
    trace: dict


def _langgraph_app():
    try:
        from langgraph.graph import END, START, StateGraph
    except Exception:
        return None
    g = StateGraph(TurnState)
    for name, fn in NODES.items():
        g.add_node(name, fn)
    g.add_edge(START, "load")
    g.add_edge("load", "gate")
    g.add_conditional_edges("gate", turn.route_after_gate,
                            {"understand": "understand", "compose": "compose", "after": "after"})
    g.add_edge("understand", "decide")
    g.add_edge("decide", "retrieve")
    g.add_edge("retrieve", "compose")
    g.add_edge("compose", "guard")
    g.add_edge("guard", "after")
    g.add_edge("after", END)
    return g.compile()


_APP = None


def run_plain(state):
    """The same path as the graph, as a simple loop."""
    state = NODES["load"](state)
    state = NODES["gate"](state)
    nxt = turn.route_after_gate(state)
    if nxt == "understand":
        for n in ("understand", "decide", "retrieve", "compose", "guard"):
            state = NODES[n](state)
    elif nxt == "compose":
        for n in ("compose", "guard"):
            state = NODES[n](state)
    return NODES["after"](state)


def run_turn(state):
    """Run one turn. state needs: user_id, kind, text, now, store, llm, kb, seed_fn."""
    global _APP
    state.setdefault("llm_calls", [])
    state.setdefault("events", [])
    state.setdefault("hits", [])
    use_lg = S.env("USE_LANGGRAPH", "1") != "0"
    if use_lg:
        if _APP is None:
            _APP = _langgraph_app() or False
        if _APP:
            return _APP.invoke(state)
    return run_plain(state)
