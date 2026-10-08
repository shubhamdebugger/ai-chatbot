"""Out-of-scope accuracy eval — the real decision (keyword bypass + understand classifier + knowledge similarity).

    python tests/eval_oos.py [path.jsonl]          accuracy report (one understand call + one embedding per message)
    python tests/eval_oos.py --timeout-check       how many query embeddings miss the live 1.5 s budget

Needs PROVIDER and EMBEDDING_PROVIDER with keys in .env.
Each line: {"text": ..., "label": "in_scope" | "oos" | "borderline", "lang": "en" | "hi" | "hinglish"}.
Only "oos" is a positive; borderline (finance-adjacent) messages must NOT be marked out of scope.
Modes, exactly as the turn runs them (tanya/turn.py n_understand + decider):
  (a) two-signal: bypass → classifier yes AND similarity < threshold → decider
  (b) locked:     bypass → similarity < floor (no understand call) → else the two-signal rule
Every what-if (threshold, floor, keyword list) is recomputed from the same per-message results — no new AI calls.
"""
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
TIMEOUT_CHECK = "--timeout-check" in sys.argv
if not TIMEOUT_CHECK:
    # measuring accuracy, not serving: give the query embedding time instead of the live 1.5 s budget (this process only)
    os.environ.setdefault("EMBEDDING_TIMEOUT_SECONDS", "15")

from tanya import memory_model as mm       # noqa: E402
from tanya import oos                       # noqa: E402
from tanya import timeutil                  # noqa: E402
from tanya.decider import decide            # noqa: E402
from tanya.knowledge import KnowledgeIndex  # noqa: E402
from tanya.llm import LLM                   # noqa: E402
from tanya.settings import S                # noqa: E402
from tanya.understand import understand     # noqa: E402

NOW = timeutil.now()
REC = mm.new_record("EVAL", {"consent": True, "trial_start_days_ago": 5}, NOW)
mm.ensure_session(REC, NOW)
mm.ensure_day(REC, NOW)
LANGS = ("en", "hinglish", "hi")
BASE_KW = list(S.get("oos_bypass_keywords", oos.DEFAULT_BYPASS))
# item 4: proposed additions (report only)
ADD_KW = ["credit score", "repo rate", "interest rate", "loan", "insurance", "gdp", "saving", "savings",
          "बचत", "ब्याज", "बीमा", "लोन"]
# item 5: single words replaced by phrases (report only)
REPLACE_OUT = {"agent", "trade", "sip"}
REPLACE_IN = ["agent se baat", "tg support", "sip investment", "sip kya", "trade setup", "trading"]


def run_one(llm, kb, row):
    text = row["text"]
    sim = kb.relevance(text)
    labels, res = understand(llm, text, [])
    return dict(row, sim=sim, classifier=labels["labels"]["out_of_scope"]["on"], labels=labels,
                understand_ok=res.ok)


class Cfg:
    def __init__(self, th=None, floor=None, kw=None, name=""):
        self.th = oos.threshold() if th is None else th
        self.floor = oos.floor() if floor is None else floor
        self.kw = BASE_KW if kw is None else kw
        self.name = name
        self._hit = {}

    def hit(self, r):
        if r["text"] not in self._hit:
            self._hit[r["text"]] = oos.bypass_hit(r["text"], self.kw)
        return self._hit[r["text"]]


def two_signal(r, c):
    """bypass → both signals → the decider (a Row 2–7 or 9–19 label keeps its own row)."""
    on = not c.hit(r) and r["classifier"] and r["sim"] is not None and r["sim"] < c.th
    return decide(dict(r["labels"], oos={"on": on}), REC, NOW).action == "OUT_OF_SCOPE"


def locked(r, c):
    """bypass → similarity below the floor: out of scope with no understand call → else the two-signal rule."""
    if c.hit(r):
        return False
    if r["sim"] is not None and r["sim"] < c.floor:
        return True
    return two_signal(r, c)


def locked_calls_understand(r, c):
    return not c.hit(r) and not (r["sim"] is not None and r["sim"] < c.floor)


def metrics(rows, fn, c):
    tp = sum(1 for r in rows if fn(r, c) and r["label"] == "oos")
    fp = sum(1 for r in rows if fn(r, c) and r["label"] != "oos")
    fn_ = sum(1 for r in rows if not fn(r, c) and r["label"] == "oos")
    p = tp / (tp + fp) if tp + fp else 1.0
    rc = tp / (tp + fn_) if tp + fn_ else 0.0
    return p, rc, tp, fp, fn_


def fmt(m):
    return f"P={m[0]:6.1%} R={m[1]:6.1%} (TP={m[2]} FP={m[3]} FN={m[4]})"


def by_lang(rows, fn, c, indent="      "):
    for lang in LANGS:
        print(f"{indent}{lang:9} {fmt(metrics([r for r in rows if r['lang'] == lang], fn, c))}")


def fps(rows, fn, c):
    return [r for r in rows if fn(r, c) and r["label"] != "oos"]


def show(r, c):
    s = "None" if r["sim"] is None else f"{r['sim']:.3f}"
    return f"[{r['label']:10} {r['lang']:8}] sim={s} clf={r['classifier']!s:5} bypass={c.hit(r) or '-'} | {r['text']}"


def dist(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return "n/a"
    q = lambda f: v[min(len(v) - 1, int(f * (len(v) - 1)))]
    return (f"n={len(v):3}  min={v[0]:.3f}  p10={q(.1):.3f}  median={statistics.median(v):.3f}  "
            f"p90={q(.9):.3f}  max={v[-1]:.3f}")


def timeout_check(data):
    """Item 6: the live budget (EMBEDDING_TIMEOUT_SECONDS, default 1.5 s), one message at a time, fresh cache.
    This is the locked-mode case (embedding before the understand call, no prefetch overlap)."""
    budget = float(S.env("EMBEDDING_TIMEOUT_SECONDS", "1.5"))
    kb = KnowledgeIndex()
    kb.qdrant = False
    out = []
    for row in data:
        kb.__dict__.pop("_qcache", None)
        t0 = time.perf_counter()
        sim = kb.relevance(row["text"])
        out.append(dict(row, sim=sim, ms=int((time.perf_counter() - t0) * 1000)))
    ms = sorted(r["ms"] for r in out)
    miss = [r for r in out if r["sim"] is None]
    print(f"budget={budget}s  messages={len(out)}  timed out / failed={len(miss)} ({len(miss) / len(out):.1%})")
    print(f"latency ms: median={statistics.median(ms)}  p90={ms[int(.9 * (len(ms) - 1))]}  "
          f"p99={ms[int(.99 * (len(ms) - 1))]}  max={ms[-1]}")
    for label in ("in_scope", "borderline", "oos"):
        print(f"   {label:10} timed out: {sum(1 for r in miss if r['label'] == label)}")
    off = [r for r in miss if r["label"] == "oos" and not oos.bypass_hit(r["text"])]
    print(f"off-topic messages that would get a full answer because of a timeout (fail-open): {len(off)}")
    for r in off:
        print(f"   [{r['lang']}] {r['ms']} ms | {r['text']}")


def main():
    args = [a for a in sys.argv[1:] if a != "--timeout-check"]
    path = Path(args[0]) if args else ROOT / "tests" / "data" / "oos_eval.jsonl"
    data = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if TIMEOUT_CHECK:
        return timeout_check(data)
    llm, kb = LLM(), KnowledgeIndex()
    kb.qdrant = False                       # the same cosine, from the in-memory vectors
    base = Cfg(name="current config")
    print(f"provider={S.provider}  embeddings={'on' if kb.has_vectors else 'OFF'}  messages={len(data)}  "
          f"threshold={base.th}  locked floor={base.floor}")
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(lambda r: run_one(llm, kb, r), data))
    missing = [r["text"] for r in rows if r["sim"] is None]
    failed = [r["text"] for r in rows if not r["understand_ok"]]
    if missing or failed:
        print(f"WARNING: no similarity for {len(missing)}, understand failed for {len(failed)} (counted as in-scope)")

    print("\n== 1. Current config (target P>=95%, R>=70%)")
    for name, fn in (("(a) two-signal", two_signal), ("(b) locked    ", locked)):
        print(f"{name}  {fmt(metrics(rows, fn, base))}")
        by_lang(rows, fn, base)

    off = [r for r in rows if r["label"] == "oos"]
    calls = [r for r in off if locked_calls_understand(r, base)]
    print(f"\n== 2. Locked mode: off-topic messages that still make the understand call: "
          f"{len(calls)}/{len(off)} ({len(calls) / len(off):.1%}); skipped (sim < {base.floor}): "
          f"{sum(1 for r in off if r['sim'] is not None and r['sim'] < base.floor)}")
    for lang in LANGS:
        o = [r for r in off if r["lang"] == lang]
        print(f"      {lang:9} {sum(1 for r in o if locked_calls_understand(r, base))}/{len(o)}")

    print("\n== 3. Threshold 0.30 vs 0.35 (floor unchanged)")
    c30, c35 = Cfg(th=0.30), Cfg(th=0.35)
    for name, fn in (("two-signal", two_signal), ("locked", locked)):
        print(f"   {name}")
        print(f"      {'all':9} 0.30 {fmt(metrics(rows, fn, c30))}   |  0.35 {fmt(metrics(rows, fn, c35))}")
        for lang in LANGS:
            sub = [r for r in rows if r["lang"] == lang]
            print(f"      {lang:9} 0.30 {fmt(metrics(sub, fn, c30))}   |  0.35 {fmt(metrics(sub, fn, c35))}")
    new35 = [r for r in fps(rows, two_signal, c35) if r not in fps(rows, two_signal, c30)]
    print(f"   new two-signal false positives at 0.35: " + ("; ".join(show(r, c35) for r in new35) or "none"))

    print("\n== 4. What-if: add " + ", ".join(ADD_KW))
    plus = Cfg(kw=BASE_KW + ADD_KW)
    for name, fn in (("two-signal", two_signal), ("locked", locked)):
        fixed = [r for r in fps(rows, fn, base) if r not in fps(rows, fn, plus)]
        print(f"   {name:10} before {fmt(metrics(rows, fn, base))}  after {fmt(metrics(rows, fn, plus))}")
        print(f"              fixes: " + ("; ".join(f"{r['text']} ('{plus.hit(r)}')" for r in fixed) or "none"))
    hits = [r for r in rows if r["label"] == "oos" and plus.hit(r) and not base.hit(r)]
    print(f"   off-topic messages hitting the new words: " + ("; ".join(f"{r['text']} ('{plus.hit(r)}')" for r in hits)
                                                          or "none"))

    print("\n== 5. What-if: agent / trade / sip → " + ", ".join(REPLACE_IN))
    repl = Cfg(kw=[k for k in BASE_KW if k not in REPLACE_OUT] + REPLACE_IN)
    for name, fn in (("two-signal", two_signal), ("locked", locked)):
        print(f"   {name:10} before {fmt(metrics(rows, fn, base))}  after {fmt(metrics(rows, fn, repl))}")
    changed = [r for r in rows if bool(base.hit(r)) != bool(repl.hit(r))]
    for r in changed:
        print(f"   bypass {base.hit(r) or '-'} → {repl.hit(r) or '-'}: {show(r, repl)}  "
              f"two-signal OOS={two_signal(r, repl)} locked OOS={locked(r, repl)}")
    if not changed:
        print("   no message changes bypass status")

    for name, fn in (("two-signal", two_signal), ("locked", locked)):
        print(f"\n== False positives — {name}, current config ({len(fps(rows, fn, base))})")
        for r in fps(rows, fn, base):
            print("   " + show(r, base))
        fns = [r for r in rows if not fn(r, base) and r["label"] == "oos"]
        print(f"   missed off-topic ({len(fns)}): " + "; ".join(f"{r['text']} (sim={r['sim']}, clf={r['classifier']})"
                                                             for r in fns))

    print("\n== Classifier")
    raw = lambda r, c: r["classifier"]
    clf_dec = lambda r, c: decide(dict(r["labels"], oos={"on": r["classifier"]}), REC, NOW).action == "OUT_OF_SCOPE"
    print(f"   raw label only    {fmt(metrics(rows, raw, base))}")
    print(f"   label + decide()  {fmt(metrics(rows, clf_dec, base))}")
    print(f"   off-topic messages hitting the bypass: " +
          ("; ".join(f"{r['text']} ('{base.hit(r)}')" for r in off if base.hit(r)) or "none"))

    print("\n== Similarity (max cosine) distribution")
    for label in ("in_scope", "borderline", "oos"):
        print(f"  {label}")
        for lang in LANGS:
            print(f"     {lang:9} " + dist([r["sim"] for r in rows if r["label"] == label and r["lang"] == lang]))
        print(f"     {'all':9} " + dist([r["sim"] for r in rows if r["label"] == label]))

    print("\n== Sweep (evidence only; config.json is not changed)")
    print("   threshold (floor fixed)   two-signal            locked")
    for t in (0.25, 0.28, 0.30, 0.32, 0.35, 0.40):
        c = Cfg(th=t)
        print(f"   {t:.2f}                      {metrics(rows, two_signal, c)[0]:6.1%} / {metrics(rows, two_signal, c)[1]:6.1%}"
              f"     {metrics(rows, locked, c)[0]:6.1%} / {metrics(rows, locked, c)[1]:6.1%}")
    print("   floor (threshold fixed)   locked P / R   off-topic calling understand")
    for f in (0.0, 0.10, 0.12, 0.15, 0.18):
        c = Cfg(floor=f)
        p, rc = metrics(rows, locked, c)[:2]
        print(f"   {f:.2f}                      {p:6.1%} / {rc:6.1%}   {sum(locked_calls_understand(r, c) for r in off)}/{len(off)}")

    out = ROOT / "data" / "oos_eval_results.jsonl"
    out.parent.mkdir(exist_ok=True)
    keep = ("text", "label", "lang", "sim", "classifier", "understand_ok")
    out.write_text("\n".join(json.dumps({k: r[k] for k in keep} | {
        "bypass": base.hit(r), "two_signal_oos": two_signal(r, base), "locked_oos": locked(r, base),
        "labels_on": [k for k, v in r["labels"]["labels"].items() if v["on"]]}, ensure_ascii=False)
        for r in rows) + "\n", encoding="utf-8")
    print(f"\nper-message results: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
