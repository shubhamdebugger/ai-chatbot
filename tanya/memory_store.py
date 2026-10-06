"""AI-C25 Customer Memory — where the records live.

Plain English:
- FileStore  : one JSON file on this computer. For the dev console and tests.
- RedisStore : Ms Tanya's working memory in production. The chat path reads and
               writes ONLY here — never MySQL (Tushar's rule, Architecture §7.7).
Both have the same functions, so the rest of the code does not care which is used.

Every change is also emitted as an 'event' — the persister (workers.py) copies
events into the MySQL orch_ tables behind her, one way, within seconds.

Redis keys (production):
  tanya:rec:{user_id}        JSON record (picture + facts + journey + recent messages); 'version' for safe writes
  tanya:ledger:{YYYY-MM-DD}  company AI spend today in ₹ (INCRBYFLOAT)
  tanya:caseseq:{YYYY-MM-DD} case number counter for the day
  tanya:persist              stream of events for the MySQL persister
  tanya:killswitch           off | limited | stopped   (AI-C20)
  tanya:human:{conv_id}      HUMAN mode flag, set at once by the webhook receiver (expires by itself)
  tanya:loadq                list of user IDs whose picture the loader must fill (cold start)
  tanya:in:{0..7}            intake streams, partitioned per customer (streams.py)
  tanya:seen:{event_id}      duplicate guard for webhook events (streams.py)
  tanya:tokens:{user_id}     this lead's AI token counter (INCRBY; reset by lead_token_budget_reset)
  tanya:tokens:day:{user_id} the day the token counter was last reset (only when reset = daily)
  tanya:windup:{user_id}     SET NX — the 70% wind-up alert has fired for this lead
  tanya:windup_plan:{user_id} JSON wind-up plan (recap / open_doubt / wrapup_queries / used)
  tanya:alerts               stream of alerts (windup_70), read by the alerts worker (workers.py)
"""
import json
import threading
from pathlib import Path

from .settings import ROOT, S
from .timeutil import day_key


class VersionConflict(Exception):
    """Another worker saved a newer version first — reload and redo (Architecture §7.4)."""


class FileStore:
    def __init__(self, path: Path = ROOT / "data" / "memory.json"):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path = self.path.parent / "events.jsonl"
        self.lock = threading.RLock()
        self.db = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else \
            {"records": {}, "ledger": {}, "caseseq": {}, "killswitch": "off"}

    def _flush(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.db, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, user_id):
        with self.lock:
            rec = self.db["records"].get(user_id)
            return json.loads(json.dumps(rec)) if rec else None

    def save(self, rec):
        with self.lock:
            cur = self.db["records"].get(rec["user_id"])
            if cur and cur["version"] != rec["version"]:
                raise VersionConflict(rec["user_id"])
            rec["version"] += 1
            self.db["records"][rec["user_id"]] = json.loads(json.dumps(rec))
            self._flush()

    def reset(self, user_id):
        with self.lock:
            self.db["records"].pop(user_id, None)
            self._flush()

    def ids(self):
        return list(self.db["records"].keys())

    def emit(self, events):
        if not events:
            return
        with self.lock, self.events_path.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def ledger_add(self, inr, now) -> float:
        with self.lock:
            d = day_key(now)
            self.db["ledger"][d] = round(self.db["ledger"].get(d, 0.0) + inr, 4)
            self._flush()
            return self.db["ledger"][d]

    def ledger_get(self, now) -> float:
        return self.db["ledger"].get(day_key(now), 0.0)

    # ---- per-lead token budget + wind-up (AI-C19 for one lead) ----
    def _tokens_reset(self, user_id, now):
        """lead_token_budget_reset = 'daily' → counter, wind-up flag and plan reset at 00:00 IST."""
        if S.get("lead_token_budget_reset", "never") != "daily":
            return
        day = day_key(now)
        days = self.db.setdefault("tokens_day", {})
        if days.get(user_id) == day:
            return
        days[user_id] = day
        self.db.setdefault("tokens", {}).pop(user_id, None)
        self.db.setdefault("windup", {}).pop(user_id, None)
        self.db.setdefault("windup_plan", {}).pop(user_id, None)

    def tokens_add(self, user_id, n, now) -> int:
        with self.lock:
            self._tokens_reset(user_id, now)
            t = self.db.setdefault("tokens", {})
            t[user_id] = t.get(user_id, 0) + int(n)
            self._flush()
            return t[user_id]

    def tokens_get(self, user_id, now) -> int:
        with self.lock:
            self._tokens_reset(user_id, now)
            return self.db.get("tokens", {}).get(user_id, 0)

    def windup_mark(self, user_id) -> bool:
        """SET NX — True only the first time, so the 70% alert fires exactly once per lead."""
        with self.lock:
            w = self.db.setdefault("windup", {})
            if user_id in w:
                return False
            w[user_id] = "1"
            self._flush()
            return True

    def alerts_push(self, fields: dict):
        with self.lock:
            self.db.setdefault("alerts", []).append(fields)
            self._flush()

    def windup_plan_save(self, user_id, plan: dict):
        with self.lock:
            self.db.setdefault("windup_plan", {})[user_id] = plan
            self._flush()

    def windup_plan_get(self, user_id):
        plan = self.db.get("windup_plan", {}).get(user_id)
        return json.loads(json.dumps(plan)) if plan else None

    def next_case_no(self, now) -> str:
        with self.lock:
            d = day_key(now)
            self.db["caseseq"][d] = self.db["caseseq"].get(d, 0) + 1
            self._flush()
            return f"TG-{d.replace('-', '')}-{self.db['caseseq'][d]:03d}"

    def killswitch(self) -> str:
        return self.db.get("killswitch", "off")

    def set_killswitch(self, state):
        with self.lock:
            self.db["killswitch"] = state
            self._flush()

    # HUMAN flag — set by the webhook receiver the moment a staff reply arrives (v4 rule)
    def set_human_flag(self, conversation_id, hours, now):
        from datetime import timedelta
        with self.lock:
            self.db.setdefault("human", {})[conversation_id] = (now + timedelta(hours=hours)).isoformat()
            self._flush()

    def clear_human_flag(self, conversation_id):
        with self.lock:
            self.db.setdefault("human", {}).pop(conversation_id, None)
            self._flush()

    def human_flag(self, conversation_id, now) -> bool:
        from datetime import datetime
        until = self.db.get("human", {}).get(conversation_id)
        return bool(until) and datetime.fromisoformat(until) > now


class RedisStore:
    """Production memory. Needs REDIS_URL in .env (e.g. redis://localhost:6379/0), AOF on."""

    def __init__(self, url=None):
        import redis
        # socket_timeout must stay above the longest blocking wait (loader BLPOP 5 s), else an empty
        # queue raises TimeoutError; health checks keep idle connections to a cloud Redis alive.
        self.r = redis.Redis.from_url(url or S.env("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True,
                                      socket_timeout=30, socket_connect_timeout=10, health_check_interval=30)

    def get(self, user_id):
        raw = self.r.get(f"tanya:rec:{user_id}")
        return json.loads(raw) if raw else None

    def save(self, rec):
        """Safe write: only if nobody saved a newer version meanwhile (WATCH / MULTI)."""
        import redis
        key = f"tanya:rec:{rec['user_id']}"
        with self.r.pipeline() as p:
            try:
                p.watch(key)
                raw = p.get(key)
                if raw and json.loads(raw)["version"] != rec["version"]:
                    p.unwatch()
                    raise VersionConflict(rec["user_id"])
                rec["version"] += 1
                p.multi()
                p.set(key, json.dumps(rec, ensure_ascii=False))
                p.execute()
            except redis.WatchError:
                rec["version"] -= 1
                raise VersionConflict(rec["user_id"])

    def reset(self, user_id):
        self.r.delete(f"tanya:rec:{user_id}")

    def ids(self):
        return [k.split(":", 2)[2] for k in self.r.scan_iter("tanya:rec:*")]

    def emit(self, events):
        if not events:
            return
        p = self.r.pipeline()
        for e in events:
            p.xadd("tanya:persist", {"event": json.dumps(e, ensure_ascii=False)}, maxlen=1_000_000, approximate=True)
        p.execute()

    def ledger_add(self, inr, now) -> float:
        k = f"tanya:ledger:{day_key(now)}"
        v = self.r.incrbyfloat(k, inr)
        self.r.expire(k, 3 * 86400)
        return float(v)

    def ledger_get(self, now) -> float:
        return float(self.r.get(f"tanya:ledger:{day_key(now)}") or 0.0)

    # ---- per-lead token budget + wind-up (AI-C19 for one lead) ----
    def _tokens_reset(self, user_id, now):
        """lead_token_budget_reset = 'daily' → counter, wind-up flag and plan reset at 00:00 IST."""
        if S.get("lead_token_budget_reset", "never") != "daily":
            return
        day = day_key(now)
        if self.r.get(f"tanya:tokens:day:{user_id}") == day:
            return
        self.r.set(f"tanya:tokens:day:{user_id}", day)
        self.r.delete(f"tanya:tokens:{user_id}", f"tanya:windup:{user_id}", f"tanya:windup_plan:{user_id}")

    def tokens_add(self, user_id, n, now) -> int:
        self._tokens_reset(user_id, now)
        return int(self.r.incrby(f"tanya:tokens:{user_id}", int(n)))

    def tokens_get(self, user_id, now) -> int:
        self._tokens_reset(user_id, now)
        return int(self.r.get(f"tanya:tokens:{user_id}") or 0)

    def windup_mark(self, user_id) -> bool:
        """SET NX — True only the first time, so the 70% alert fires exactly once per lead."""
        return bool(self.r.set(f"tanya:windup:{user_id}", "1", nx=True))

    def alerts_push(self, fields: dict):
        self.r.xadd("tanya:alerts", {k: str(v) for k, v in fields.items()}, maxlen=10_000, approximate=True)

    def windup_plan_save(self, user_id, plan: dict):
        self.r.set(f"tanya:windup_plan:{user_id}", json.dumps(plan, ensure_ascii=False))

    def windup_plan_get(self, user_id):
        raw = self.r.get(f"tanya:windup_plan:{user_id}")
        return json.loads(raw) if raw else None

    def next_case_no(self, now) -> str:
        d = day_key(now)
        k = f"tanya:caseseq:{d}"
        n = self.r.incr(k)
        self.r.expire(k, 3 * 86400)
        return f"TG-{d.replace('-', '')}-{n:03d}"

    def killswitch(self) -> str:
        return self.r.get("tanya:killswitch") or "off"

    def set_killswitch(self, state):
        self.r.set("tanya:killswitch", state)

    def set_human_flag(self, conversation_id, hours, now):
        self.r.set(f"tanya:human:{conversation_id}", "1", ex=int(hours * 3600))

    def clear_human_flag(self, conversation_id):
        self.r.delete(f"tanya:human:{conversation_id}")

    def human_flag(self, conversation_id, now) -> bool:
        return bool(self.r.exists(f"tanya:human:{conversation_id}"))


def make_store():
    """dev → FileStore, server → RedisStore (RUN_MODE in .env)."""
    return RedisStore() if S.run_mode == "server" else FileStore()
