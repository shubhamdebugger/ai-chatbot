"""AI-C05 Knowledge / RAG — approved knowledge only.

Plain English — how RAG works here:
1. Approved files (content/kb/*.md: FAQs, lessons) are split into short pieces ('chunks').
2. For each question, the closest pieces are found by
   (a) keyword search (BM25 — always on, needs no key), and
   (b) meaning search with vectors (embeddings), when EMBEDDING_PROVIDER is set in .env.
   Both scores are combined ('hybrid').
3. The top pieces go into Tanya's prompt; she answers only from them.

Where the vectors live:
- Pilot: in this program's memory (the 'vector index' is the list below), vectors cached
  in data/kb_vectors.json so they are computed once per approved version.
- KNOWLEDGE_BACKEND=qdrant in .env: the vectors are also written to Qdrant (QDRANT_URL,
  QDRANT_COLLECTION) at start-up and the meaning search asks Qdrant. If Qdrant cannot be
  reached, search falls back to the in-memory vectors — Tanya never stops answering.
- Production: the same chunks are written to MySQL orch_kb_chunks (chunks_for_db).
Customer memory is NOT here — it is looked up by user ID (memory_store.py).
"""
import hashlib
import json
import math
import re
import uuid
from collections import Counter

import httpx

from .content_pack import PACK, parse_front_matter
from .llm import embed
from .settings import ROOT, S

# Hinglish words mapped to the English words used in the approved files
SYNONYMS = {
    "nuksaan": "loss", "nuksan": "loss", "ghata": "loss", "sl": "stop-loss stop loss", "stoploss": "stop-loss stop loss",
    "kitna": "how much", "paisa": "capital money", "paise": "capital money", "shuru": "start",
    "samajh": "understand", "seekhna": "learn", "chart": "chart", "alert": "alert research",
    "call": "call alert", "tips": "tip call", "trade": "trade", "risk": "risk", "exit": "exit",
    "refund": "refund cancellation", "install": "install app", "notification": "notification alert",
    "locked": "locked access", "access": "access locked", "guarantee": "guarantee sure-shot",
    "pakka": "sure-shot guarantee", "accuracy": "accuracy sure-shot", "overtrading": "overtrading",
    "ce": "call option ce", "pe": "put option pe", "premium": "premium option", "expiry": "expiry option",
    "registration": "sebi registration", "sebi": "sebi registration", "fraud": "sebi registration verify",
}

_WORD = re.compile(r"[\wऀ-ॿ-]+", re.UNICODE)


_IGNORE = {"tanya", "ms", "tg", "level", "the", "a", "an", "is", "to", "of", "and", "or", "hai", "ka", "ki", "ke", "kya"}


def tokens(text: str):
    out = []
    for w in _WORD.findall((text or "").lower()):
        if w in _IGNORE:
            continue
        out.append(w)
        if w in SYNONYMS:
            out.extend(SYNONYMS[w].split())
    return out


def _category(head: dict, doc_id: str) -> str:
    """The file's 'category:' if it has one; else from the id letter (H = lessons, the rest = FAQ).
    Chat filters by category (support → FAQ, refusals → Lesson), so every chunk needs one."""
    return head.get("category") or ("Lesson" if doc_id.upper().startswith("H") else "FAQ")


class KnowledgeIndex:
    def __init__(self):
        self.chunks = []
        self.cache_path = ROOT / "data" / "kb_vectors.json"
        self._build()

    def _build(self):
        for path, text in PACK.kb_files():
            head, body = parse_front_matter(text)
            qs = re.search(r"Question forms:\s*\n(.*?)\n\s*\n", body, flags=re.S)
            ans = re.search(r"Answer:\s*\n(.*?)(?:\n\s*\nNever say:|\Z)", body, flags=re.S)
            never = re.search(r"Never say:\s*\n(.*)$", body, flags=re.S)
            answer = (ans.group(1) if ans else body).strip()
            doc_id = head.get("doc_id", path.stem)
            doc = {"doc_id": doc_id, "title": head.get("title", path.stem),
                   "category": _category(head, doc_id), "status": head.get("status", ""),
                   "version": head.get("version", ""), "questions": (qs.group(1).strip() if qs else ""),
                   "never_say": (never.group(1).strip() if never else "")}
            for i, part in enumerate(self._split(answer)):
                c = dict(doc, chunk_id=f"{doc['doc_id']}#{i:02d}", text=part)
                c["search_text"] = f"{doc['title']}\n{doc['questions']}\n{part}"
                c["tokens"] = tokens(c["search_text"])
                self.chunks.append(c)
        self._bm25_prepare()
        self._vectors()

    @staticmethod
    def _split(answer, max_chars=900):
        """Split a long answer into pieces at blank lines; small answers stay whole."""
        if len(answer) <= max_chars:
            return [answer]
        parts, cur = [], ""
        for para in re.split(r"\n\s*\n", answer):
            if len(cur) + len(para) > max_chars and cur:
                parts.append(cur.strip())
                cur = ""
            cur += para + "\n\n"
        if cur.strip():
            parts.append(cur.strip())
        return parts

    # ---------------- keyword search (BM25)
    def _bm25_prepare(self):
        self.N = len(self.chunks)
        self.avgdl = sum(len(c["tokens"]) for c in self.chunks) / max(1, self.N)
        df = Counter()
        for c in self.chunks:
            df.update(set(c["tokens"]))
        self.idf = {w: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for w, n in df.items()}
        for c in self.chunks:
            c["tf"] = Counter(c["tokens"])

    def _bm25(self, q_tokens, c, k1=1.5, b=0.75):
        s = 0.0
        dl = len(c["tokens"])
        for w in set(q_tokens):
            if w not in c["tf"]:
                continue
            tf = c["tf"][w]
            s += self.idf.get(w, 0) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / self.avgdl))
        return s

    # ---------------- meaning search (vectors)
    def _vectors(self):
        self.vec_model = S.env("EMBEDDING_PROVIDER", "")
        self.has_vectors = False
        if not self.vec_model:
            return
        cache = json.loads(self.cache_path.read_text()) if self.cache_path.exists() else {}
        need = [c for c in self.chunks if self._key(c) not in cache]
        if need:
            vecs = embed([c["search_text"] for c in need])
            if vecs is None:
                return
            for c, v in zip(need, vecs):
                cache[self._key(c)] = v
            self.cache_path.parent.mkdir(exist_ok=True)
            self.cache_path.write_text(json.dumps(cache))
        for c in self.chunks:
            c["vec"] = cache.get(self._key(c))
        self.has_vectors = all(c.get("vec") for c in self.chunks)
        self._qdrant_sync()

    # ---------------- Qdrant (KNOWLEDGE_BACKEND=qdrant)
    def _qdrant_sync(self):
        """Write every chunk's vector to the Qdrant collection and remove chunks that no longer exist."""
        self.qdrant = False
        self.qdrant_error = ""
        if S.env("KNOWLEDGE_BACKEND", "").lower() != "qdrant" or not self.has_vectors:
            return
        self.q_url = S.env("QDRANT_URL", "http://localhost:6333").rstrip("/")
        self.q_col = S.env("QDRANT_COLLECTION", "tanya_kb")
        col = f"{self.q_url}/collections/{self.q_col}"
        size = len(self.chunks[0]["vec"])
        try:
            r = httpx.get(col, timeout=5)
            if r.status_code == 404:
                httpx.put(col, json={"vectors": {"size": size, "distance": "Cosine"}},
                          timeout=10).raise_for_status()
            else:
                r.raise_for_status()
                have = r.json()["result"]["config"]["params"]["vectors"]["size"]
                if have != size:
                    raise ValueError(f"collection has {have}-dim vectors, embeddings are {size}-dim "
                                     f"(use another QDRANT_COLLECTION per embedding model)")
            ids = [self._point_id(c) for c in self.chunks]
            points = [{"id": pid, "vector": c["vec"],
                       "payload": {k: c[k] for k in ("chunk_id", "doc_id", "title", "category", "status", "version")}
                       | {"key": self._key(c)}}
                      for pid, c in zip(ids, self.chunks)]
            httpx.put(f"{col}/points?wait=true", json={"points": points}, timeout=30).raise_for_status()
            httpx.post(f"{col}/points/delete?wait=true", json={"filter": {"must_not": [{"has_id": ids}]}},
                       timeout=30).raise_for_status()
            self.qdrant = True
        except Exception as e:  # Qdrant down or misconfigured: in-memory vectors still work
            self.qdrant_error = f"{type(e).__name__}: {str(e)[:200]}"

    @staticmethod
    def _point_id(c):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "tanya-kb:" + c["chunk_id"]))

    def _qdrant_scores(self, qv, categories):
        """chunk_id → cosine similarity from Qdrant, or None when Qdrant fails."""
        body = {"query": qv, "limit": len(self.chunks), "with_payload": ["chunk_id"]}
        if categories:
            body["filter"] = {"must": [{"key": "category", "match": {"any": list(categories)}}]}
        try:
            r = httpx.post(f"{self.q_url}/collections/{self.q_col}/points/query", json=body, timeout=5)
            r.raise_for_status()
            return {p["payload"]["chunk_id"]: p["score"] for p in r.json()["result"]["points"]}
        except Exception as e:
            self.qdrant_error = f"{type(e).__name__}: {str(e)[:200]}"
            return None

    def _key(self, c):
        return self.vec_model + ":" + hashlib.sha1(c["search_text"].encode()).hexdigest()

    @staticmethod
    def _cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    # ---------------- the search
    def search(self, query: str, top_k=None, categories=None):
        """Hybrid search. Returns the best chunks with scores (0..1)."""
        top_k = top_k or S.get("knowledge_top_k", 3)
        q = tokens(query)
        cands = [c for c in self.chunks if not categories or c["category"] in categories]
        if not cands:
            return []
        kw = [self._bm25(q, c) for c in cands]
        mx = max(kw) or 1.0
        scores = [k / mx for k in kw]
        if self.has_vectors:
            qv = embed([query])
            if qv:
                qs = self._qdrant_scores(qv[0], categories) if self.qdrant else None
                cos = [qs.get(c["chunk_id"], 0.0) for c in cands] if qs is not None \
                    else [self._cos(qv[0], c["vec"]) for c in cands]
                scores = [0.5 * s + 0.5 * max(0.0, v) for s, v in zip(scores, cos)]
        ranked = sorted(zip(scores, kw, cands), key=lambda t: -t[0])
        out = []
        for s, raw, c in ranked[:top_k]:
            if raw <= 0 and not self.has_vectors:
                continue
            out.append({k: c[k] for k in ("chunk_id", "doc_id", "title", "category", "status", "version",
                                          "text", "never_say")} | {"score": round(s, 3)})
        return out

    def chunks_for_db(self):
        """Rows for MySQL orch_kb_chunks (production sync job)."""
        return [{"chunk_id": c["chunk_id"], "doc_id": c["doc_id"], "title": c["title"], "category": c["category"],
                 "status": c["status"], "version": c["version"], "text": c["text"],
                 "content_hash": hashlib.sha1(c["search_text"].encode()).hexdigest()} for c in self.chunks]


def is_placeholder(hit) -> bool:
    return "PLACEHOLDER" in (hit.get("status") or "").upper() or hit.get("text", "").startswith("[PLACEHOLDER]")

def for_voice(hits):
    """Search hits shaped for the voice agent's search_knowledge tool (ElevenLabs webhook).
    Placeholders are dropped, as in the chat path: only approved text is ever spoken."""
    ok = [h for h in hits if not is_placeholder(h)]
    return {"found": bool(ok),
            "results": [{"title": h["title"], "answer": h["text"], "never_say": h["never_say"],
                         "score": h["score"]} for h in ok],
            "instruction": "Answer only from these results, in short spoken sentences. Never use a "
                           "'never_say' phrase. If found is false, say you do not have that information "
                           "and offer to connect the user with the team."}
