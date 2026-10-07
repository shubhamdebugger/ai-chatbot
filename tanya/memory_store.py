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
  tanya:release:{conv_id}    time staff released the chat to Tanya (chat closed / #bot)
  tanya:staff_wait           zset conversation -> time a customer started waiting for staff (staff-silent alert)
  tanya:loadq                list of user IDs whose picture the loader must fill (cold start)
  tanya:in:{0..7}            intake streams, partitioned per customer (streams.py)
  tanya:seen:{event_id}      duplicate guard for webhook events (streams.py)
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

    # HUMAN -> BOT: staff closed the chat or typed #bot (v4 §7). The record's HUMAN mode older than this ends.
    def release_conversation(self, conversation_id, now):
        with self.lock:
            self.db.setdefault("human", {}).pop(conversation_id, None)
            self.db.setdefault("released", {})[conversation_id] = now.isoformat()
            self.db.setdefault("staff_wait", {}).pop(conversation_id, None)
            h = self.db.setdefault("handoffs", {}).get(conversation_id)
            if h and h.get("status") == "open":
                h["status"] = "released"
            self.db.setdefault("agent_idle", {}).pop(conversation_id, None)
            self._flush()

    def release_time(self, conversation_id):
        from datetime import datetime
        t = self.db.get("released", {}).get(conversation_id)
        return datetime.fromisoformat(t) if t else None

    # Customer waiting for staff (HUMAN mode, no staff reply yet) -> staff-silent alert after N minutes (v4 §7.6)
    def staff_wait_start(self, conversation_id, user_id, now):
        with self.lock:
            self.db.setdefault("staff_wait", {}).setdefault(conversation_id, [now.timestamp(), user_id])
            self._flush()

    def staff_wait_end(self, conversation_id):
        with self.lock:
            self.db.setdefault("staff_wait", {}).pop(conversation_id, None)
            self._flush()

    def staff_waiting_since(self, before_epoch):
        return [(c, u, t) for c, (t, u) in self.db.get("staff_wait", {}).items() if t <= before_epoch]

    # ---- Tanya's own handoff (06-Oct-2026): HUMAN mode with a recovery deadline (handoff_recovery.py)
    def handoff_start(self, conversation_id, data, due_epoch):
        with self.lock:
            self.db.setdefault("handoffs", {})[conversation_id] = dict(data, due=due_epoch)
            self._flush()

    def handoff_get(self, conversation_id):
        return self.db.get("handoffs", {}).get(conversation_id)

    def handoff_update(self, conversation_id, **fields):
        with self.lock:
            h = self.db.setdefault("handoffs", {}).get(conversation_id)
            if h is not None:
                h.update(fields)
                self._flush()

    def handoff_due(self, before_epoch):
        return [c for c, h in self.db.get("handoffs", {}).items()
                if h.get("status") == "open" and h.get("due", 0) <= before_epoch]

    def handoff_end(self, conversation_id, status):
        self.handoff_update(conversation_id, status=status)

    # ---- agent idle (07-Oct-2026): an agent wrote in the chat -> Tanya back after N quiet minutes
    def agent_idle_set(self, conversation_id, data, due_epoch):
        with self.lock:
            self.db.setdefault("agent_idle", {})[conversation_id] = dict(data, due=due_epoch)
            self._flush()

    def agent_idle_get(self, conversation_id):
        return self.db.get("agent_idle", {}).get(conversation_id)

    def agent_idle_due(self, before_epoch):
        return [c for c, e in self.db.get("agent_idle", {}).items() if e.get("due", 0) <= before_epoch]

    def agent_idle_clear(self, conversation_id):
        with self.lock:
            self.db.setdefault("agent_idle", {}).pop(conversation_id, None)
            self._flush()

    # ---- typing / queued state the PWA reads (never left on: expires by itself)
    def typing_set(self, conversation_id, state, ttl):
        import time as _t
        with self.lock:
            self.db.setdefault("typing", {})[conversation_id] = [state, _t.time() + ttl]
            self._flush()

    def typing_clear(self, conversation_id):
        with self.lock:
            self.db.setdefault("typing", {}).pop(conversation_id, None)
            self._flush()

    def typing_get(self, conversation_id):
        import time as _t
        v = self.db.get("typing", {}).get(conversation_id)
        return v[0] if v and v[1] > _t.time() else None

    def last_reply_set(self, conversation_id, crm_message_id):
        with self.lock:
            d = self.db.setdefault("last_reply", {})
            d[conversation_id] = max(int(d.get(conversation_id, 0)), int(crm_message_id))
            self._flush()

    def last_reply_get(self, conversation_id):
        return int(self.db.get("last_reply", {}).get(conversation_id, 0))

    # ---- conversation summary bookkeeping (summaries.py)
    def summary_touch(self, conversation_id, user_id, now_epoch, force=False):
        with self.lock:
            d = self.db.setdefault("summary", {}).setdefault(conversation_id, {"count": 0, "first": now_epoch,
                                                                                 "user_id": user_id, "force": 0})
            d["count"] += 1
            d["user_id"] = user_id or d.get("user_id")
            d["first"] = d.get("first") or now_epoch
            if force:
                d["force"] = 1
            self._flush()

    def summary_dirty(self):
        return {c: dict(d) for c, d in self.db.get("summary", {}).items() if d.get("count", 0) or d.get("force")}

    def summary_clear(self, conversation_id):
        with self.lock:
            self.db.setdefault("summary", {}).pop(conversation_id, None)
            self._flush()

    def summary_backoff(self, conversation_id, until_epoch):
        with self.lock:
            d = self.db.setdefault("summary", {}).get(conversation_id)
            if d is not None:
                d["retry_after"] = until_epoch
                self._flush()

    def once(self, key, days=30) -> bool:
        """True the first time a key is seen (webhook retries are dropped). Dev store: no expiry."""
        with self.lock:
            seen = self.db.setdefault("once", {})
            if key in seen:
                return False
            seen[key] = 1
            self._flush()
            return True


class RedisStore:
    """Production memory. Needs REDIS_URL in .env (e.g. redis://localhost:6379/0), AOF on."""

    def __init__(self, url=None):
        import redis
        # socket_timeout must stay above the longest blocking wait (loader BLPOP 5 s), else an empty
        # queue raises TimeoutError; health checks keep idle connections to a cloud Redis alive.
        # REDIS_CONNECT_TIMEOUT_SECONDS: the gateway sets 2 s so a Redis outage answers the CRM fast (503) instead
        # of holding the customer's "Sending..." (the reconciler recovers the message later).
        self.r = redis.Redis.from_url(url or S.env("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True,
                                      socket_timeout=30, health_check_interval=30,
                                      socket_connect_timeout=float(S.env("REDIS_CONNECT_TIMEOUT_SECONDS", "10")))

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

    # HUMAN -> BOT: staff closed the chat or typed #bot (v4 §7). The record's HUMAN mode older than this ends.
    def release_conversation(self, conversation_id, now):
        p = self.r.pipeline()
        p.delete(f"tanya:human:{conversation_id}")
        p.set(f"tanya:release:{conversation_id}", now.isoformat(), ex=30 * 86400)
        p.zrem("tanya:staff_wait", conversation_id)
        p.zrem("tanya:handoff_due", conversation_id)        # a Tanya handoff ends with the release
        p.zrem("tanya:agent_idle_due", conversation_id)     # so does the agent-idle timer
        p.execute()

    def release_time(self, conversation_id):
        from datetime import datetime
        t = self.r.get(f"tanya:release:{conversation_id}")
        return datetime.fromisoformat(t) if t else None

    # Customer waiting for staff (HUMAN mode, no staff reply yet) -> staff-silent alert after N minutes (v4 §7.6)
    def staff_wait_start(self, conversation_id, user_id, now):
        p = self.r.pipeline()
        p.zadd("tanya:staff_wait", {conversation_id: now.timestamp()}, nx=True)
        p.hset("tanya:staff_wait_user", conversation_id, user_id)
        p.execute()

    def staff_wait_end(self, conversation_id):
        self.r.zrem("tanya:staff_wait", conversation_id)

    def staff_waiting_since(self, before_epoch):
        out = []
        for c, t in self.r.zrangebyscore("tanya:staff_wait", 0, before_epoch, withscores=True):
            out.append((c, self.r.hget("tanya:staff_wait_user", c) or "", t))
        return out

    # ---- Tanya's own handoff (06-Oct-2026): HUMAN mode with a recovery deadline (handoff_recovery.py)
    #   tanya:handoff:{conv}  hash (handoff_id, user_id, action, reason, period, started_at, due_at, status, ...)
    #   tanya:handoff_due     zset conv -> due epoch (only open handoffs)
    def handoff_start(self, conversation_id, data, due_epoch):
        import json as _j
        p = self.r.pipeline()
        p.delete(f"tanya:handoff:{conversation_id}")
        p.hset(f"tanya:handoff:{conversation_id}", mapping={k: _j.dumps(v) if isinstance(v, (dict, list)) else str(v)
                                                            for k, v in dict(data, due=due_epoch).items()})
        p.expire(f"tanya:handoff:{conversation_id}", 14 * 86400)
        p.zadd("tanya:handoff_due", {conversation_id: due_epoch})
        p.execute()

    def handoff_get(self, conversation_id):
        h = self.r.hgetall(f"tanya:handoff:{conversation_id}")
        if not h:
            return None
        h["due"] = float(h.get("due", 0))
        return h

    def handoff_update(self, conversation_id, **fields):
        if self.r.exists(f"tanya:handoff:{conversation_id}"):
            self.r.hset(f"tanya:handoff:{conversation_id}", mapping={k: str(v) for k, v in fields.items()})

    def handoff_due(self, before_epoch):
        return list(self.r.zrangebyscore("tanya:handoff_due", 0, before_epoch))

    def handoff_end(self, conversation_id, status):
        p = self.r.pipeline()
        p.zrem("tanya:handoff_due", conversation_id)
        p.execute()
        self.handoff_update(conversation_id, status=status)

    # ---- agent idle (07-Oct-2026): an agent wrote in the chat -> Tanya back after N quiet minutes
    #   tanya:agent_idle:{conv}  hash (user_id, last_staff_id, at)     tanya:agent_idle_due  zset conv -> due epoch
    def agent_idle_set(self, conversation_id, data, due_epoch):
        p = self.r.pipeline()
        p.hset(f"tanya:agent_idle:{conversation_id}", mapping={k: str(v) for k, v in dict(data, due=due_epoch).items()})
        p.expire(f"tanya:agent_idle:{conversation_id}", 2 * 86400)
        p.zadd("tanya:agent_idle_due", {conversation_id: due_epoch})
        p.execute()

    def agent_idle_get(self, conversation_id):
        e = self.r.hgetall(f"tanya:agent_idle:{conversation_id}")
        return dict(e, due=float(e.get("due", 0))) if e else None

    def agent_idle_due(self, before_epoch):
        return list(self.r.zrangebyscore("tanya:agent_idle_due", 0, before_epoch))

    def agent_idle_clear(self, conversation_id):
        p = self.r.pipeline()
        p.zrem("tanya:agent_idle_due", conversation_id)
        p.delete(f"tanya:agent_idle:{conversation_id}")
        p.execute()

    # ---- typing / queued state the PWA reads (never left on: expires by itself)
    def typing_set(self, conversation_id, state, ttl):
        self.r.set(f"tanya:typing:{conversation_id}", state, ex=int(ttl))

    def typing_clear(self, conversation_id):
        self.r.delete(f"tanya:typing:{conversation_id}")

    def typing_get(self, conversation_id):
        return self.r.get(f"tanya:typing:{conversation_id}")

    # CRM id of the newest message Tanya posted in this chat: the PWA keeps the typing bubble until that message is
    # on screen (06-Oct-2026). Only ever moves forward (Lua max), kept 1 day.
    _LAST_REPLY_LUA = ("local c = tonumber(redis.call('GET', KEYS[1]) or '0') "
                       "if tonumber(ARGV[1]) > c then redis.call('SET', KEYS[1], ARGV[1]) end "
                       "redis.call('EXPIRE', KEYS[1], 86400) return 1")

    def last_reply_set(self, conversation_id, crm_message_id):
        self.r.eval(self._LAST_REPLY_LUA, 1, f"tanya:lastreply:{conversation_id}", int(crm_message_id))

    def last_reply_get(self, conversation_id):
        return int(self.r.get(f"tanya:lastreply:{conversation_id}") or 0)

    # ---- conversation summary bookkeeping (summaries.py)
    #   tanya:summary_dirty  hash conv -> json {count, first, user_id, force, retry_after}
    def summary_touch(self, conversation_id, user_id, now_epoch, force=False):
        import json as _j
        for _ in range(5):
            with self.r.pipeline() as p:
                try:
                    p.watch("tanya:summary_dirty")
                    raw = p.hget("tanya:summary_dirty", conversation_id)
                    d = _j.loads(raw) if raw else {"count": 0, "first": now_epoch, "force": 0}
                    d["count"] = d.get("count", 0) + 1
                    d["user_id"] = user_id or d.get("user_id")
                    d["first"] = d.get("first") or now_epoch
                    if force:
                        d["force"] = 1
                    p.multi()
                    p.hset("tanya:summary_dirty", conversation_id, _j.dumps(d))
                    p.execute()
                    return
                except Exception as e:
                    if "Watch" not in type(e).__name__:
                        raise

    def summary_dirty(self):
        import json as _j
        return {c: _j.loads(v) for c, v in self.r.hgetall("tanya:summary_dirty").items()}

    def summary_clear(self, conversation_id):
        self.r.hdel("tanya:summary_dirty", conversation_id)

    def summary_backoff(self, conversation_id, until_epoch):
        import json as _j
        raw = self.r.hget("tanya:summary_dirty", conversation_id)
        if raw:
            d = _j.loads(raw)
            d["retry_after"] = until_epoch
            self.r.hset("tanya:summary_dirty", conversation_id, _j.dumps(d))

    def once(self, key, days=30) -> bool:
        """True the first time a key is seen (webhook retries are dropped)."""
        return bool(self.r.set(f"tanya:once:{key}", "1", nx=True, ex=int(days * 86400)))


def make_store():
    """dev → FileStore, server → RedisStore (RUN_MODE in .env)."""
    if S.run_mode == "server":
        try:
            store = RedisStore()
            store.r.ping()
            return store
        except Exception:
            import sys
            print("[WARNING] Redis server unavailable. Falling back to FileStore.", file=sys.stderr)
            return FileStore()
    return FileStore()

