"""Redis Streams — ordered, reliable job flow (v4 design, Architecture §5.4 and §21.1.14).

Plain English:
- Every incoming event goes into one of 8 'lanes' (tanya:in:0..7). The lane is chosen by
  the customer's ID, so one customer's messages are always handled in order.
- Workers read with a consumer group, and confirm (ACK) only after the work is saved.
- A job not confirmed within 60 s is picked up again (reclaimer). A job that failed 5 times
  goes to the dead-letter stream tanya:dead and raises an alert — nothing is silently lost.
- The same webhook event twice (CRM retries) is dropped by tanya:seen:{event_id}.

PLUMBING OWNER: JUNIOR B. This file is complete; it needs a real Redis (REDIS_URL) and a load test.
"""
import json
import socket
import sys
import time
import zlib

from .settings import S

GROUP = "tanya-workers"


def partition(user_id: str) -> int:
    return zlib.crc32(str(user_id).encode()) % S.get("stream_partitions", 8)


def stream_name(p: int) -> str:
    return f"tanya:in:{p}"


class Intake:
    """Producer side — used by the gateway (webhook receiver)."""

    def __init__(self, r):
        self.r = r

    def enqueue(self, event: dict) -> bool:
        """event needs event_id, user_id. Returns False for a duplicate."""
        seen = f"tanya:seen:{event['event_id']}" if event.get("event_id") else None
        if seen and not self.r.set(seen, "1", nx=True, ex=86400):
            return False
        try:
            self.r.xadd(stream_name(partition(event["user_id"])), {"event": json.dumps(event, ensure_ascii=False)},
                        maxlen=200_000, approximate=True)
        except Exception:
            if seen:                                # not queued: a retry of this event must not be taken as a duplicate (PT2)
                try:
                    self.r.delete(seen)
                except Exception:
                    pass
            raise
        return True


class StreamWorker:
    """Consumer side. partitions = the lanes this process owns (one owner per lane keeps order)."""

    def __init__(self, r, partitions, handler, consumer=None, group=GROUP, streams=None):
        self.r = r
        self.handler = handler
        self.group = group
        self.consumer = consumer or f"{socket.gethostname()}-{partitions[0] if partitions else 0}"
        self.streams = streams or [stream_name(p) for p in partitions]
        self.max_deliveries = S.get("max_deliveries_before_dead_letter", 5)
        self.idle_ms = S.get("reclaim_idle_seconds", 60) * 1000
        self._ensure_groups()

    def _ensure_groups(self):
        for s in self.streams:
            try:
                self.r.xgroup_create(s, self.group, id="0", mkstream=True)
            except Exception as e:  # BUSYGROUP = already exists
                if "BUSYGROUP" not in str(e):
                    raise

    @staticmethod
    def _sid(msg_id):
        a, _, b = str(msg_id).partition("-")
        return int(a), int(b or 0)

    def _handle(self, stream, msg_id, fields):
        """Order per customer (PT9): a customer's earlier message that failed is retried first; if it still
        fails, this one waits too (left pending, retried in order). Other customers are not held up."""
        try:
            ev = json.loads(fields["event"])
        except Exception:
            ev = {}
        bkey = f"tanya:blocked:{ev.get('user_id')}" if ev.get("user_id") else None
        me = f"{stream}|{msg_id}"
        try:
            if bkey:
                for m in self.r.zrange(bkey, 0, -1):
                    s2, _, mid2 = m.partition("|")
                    if m == me or s2 != stream or self._sid(mid2) >= self._sid(msg_id):
                        continue
                    claimed = self.r.xclaim(s2, self.group, self.consumer, 0, [mid2])
                    if not claimed or not claimed[0][1]:  # already done / dead-lettered elsewhere
                        self.r.zrem(bkey, m)
                        continue
                    if not self._handle(s2, claimed[0][0], claimed[0][1]):
                        raise RuntimeError(f"ORDER_HOLD: earlier message {mid2} of this customer is not done yet")
            self.handler(ev)
            self.r.xack(stream, self.group, msg_id)
            if bkey:
                self.r.zrem(bkey, me)
            return True
        except Exception as e:
            try:
                if bkey:
                    self.r.zadd(bkey, {me: time.time()})
                    self.r.expire(bkey, 86400)
                self.r.xadd("tanya:errors", {"stream": stream, "id": msg_id, "error": f"{type(e).__name__}: {e}"[:500]},
                            maxlen=10_000, approximate=True)
            except Exception:
                pass                                    # Redis itself is down: the job simply stays pending
            return False

    def reclaim(self):
        """Pick up jobs left unconfirmed; send repeat failures to the dead letter."""
        for s in self.streams:
            pend = self.r.xpending_range(s, self.group, min="-", max="+", count=50)
            for p in pend:
                if p["time_since_delivered"] < self.idle_ms:
                    continue
                if p["times_delivered"] >= self.max_deliveries:
                    msgs = self.r.xrange(s, p["message_id"], p["message_id"])
                    body = msgs[0][1] if msgs else {}
                    self.r.xadd("tanya:dead", {"stream": s, "id": p["message_id"], **body}, maxlen=100_000)
                    self.r.xack(s, self.group, p["message_id"])
                    try:                                # release the customer's later messages (PT9)
                        uid = json.loads(body.get("event", "{}")).get("user_id")
                        if uid:
                            self.r.zrem(f"tanya:blocked:{uid}", f"{s}|{p['message_id']}")
                    except Exception:
                        pass
                    print(f"[stream] DEAD LETTER {s} {p['message_id']} after {p['times_delivered']} attempts",
                          file=sys.stderr, flush=True)
                    continue
                claimed = self.r.xclaim(s, self.group, self.consumer, self.idle_ms, [p["message_id"]])
                for msg_id, fields in claimed:
                    self._handle(s, msg_id, fields)

    def run_once(self, block_ms=2000, count=10) -> int:
        resp = self.r.xreadgroup(self.group, self.consumer, {s: ">" for s in self.streams}, count=count, block=block_ms)
        n = 0
        for stream, msgs in resp or []:
            for msg_id, fields in msgs:
                self._handle(stream, msg_id, fields)
                n += 1
        return n

    def run_forever(self):
        """Never dies on a Redis/network error: logs, backs off, reconnects (redis-py reconnects on the next
        command) and recreates the consumer group if Redis lost it. Pending jobs of a crashed worker are
        reclaimed every 5 s once idle for reclaim_idle_seconds."""
        last_reclaim, delay = 0, 1
        while True:
            try:
                self.run_once()
                if time.time() - last_reclaim > 5:
                    self.reclaim()
                    last_reclaim = time.time()
                delay = 1
            except Exception as e:
                print(f"[stream] {self.consumer} error {type(e).__name__}: {str(e)[:200]} — retry in {delay}s",
                      file=sys.stderr, flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                if "NOGROUP" in str(e):               # Redis restarted without its data: recreate the groups
                    self._ensure_groups()
