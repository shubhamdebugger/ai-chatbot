"""AI-C17 Model & Prompt Registry — the approved content pack.

Plain English: everything Tanya is allowed to say word-for-word, and every
business fact she may use, lives in the content/ folder — prices, plans,
fixed lines, SEBI texts, golden examples, value cards, the guard lists and
the knowledge files. The code only reads them. Replacing a placeholder
file with Tushar's approved file needs no code change.

Each file gets a short fingerprint (version) so every reply trace records
exactly which content produced it — needed for audit and one-step rollback.
"""
import csv
import hashlib
import json
import re
from pathlib import Path

from .settings import ROOT

CONTENT = ROOT / "content"


def _fp(path: Path) -> str:
    """Short fingerprint of a file — changes whenever the file changes."""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:8]


def _no_dupes(name: str, pairs: list) -> dict:
    """A repeated key in a content file would silently replace the first one (two FX lines on one id) — refuse it."""
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"content/{name}: duplicate key {k!r}")
        seen[k] = v
    return seen


class ContentPack:
    def __init__(self, folder: Path = CONTENT):
        self.folder = folder
        self.versions = {}
        self.fixed_lines = self._json("fixed_lines.json")
        self.sebi = self._json("sebi_texts.json")
        self.golden = self._json("golden_examples.json")["examples"]
        self.cards = self._json("value_cards.json")["cards"]
        self.guard = self._json("guard_lists.json")
        self.test_users = {u["user_id"]: u for u in self._json("test_users.json")["users"]}
        self.pricing = self._csv("pricing.csv")
        self.profile_to_plan = sorted(self._csv("profile_to_plan.csv"), key=lambda r: int(r["rule_order"]))

    # ---------- loading ----------
    def _json(self, name):
        p = self.folder / name
        self.versions[name] = _fp(p)
        return json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=lambda kv: _no_dupes(name, kv))

    def _csv(self, name):
        p = self.folder / name
        self.versions[name] = _fp(p)
        with p.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    # ---------- fixed lines (never written by the AI) ----------
    def fixed(self, line_id: str, lang: str, variant: str = "", **values) -> str:
        """Return fixed line FX-xx in the user's language, placeholders filled by code.

        variant picks an A/B/C wording for the lines that rotate; others ignore it."""
        entry = self.fixed_lines[line_id]
        if variant and isinstance(entry.get("variants"), dict):
            entry = entry["variants"].get(variant) or next(iter(entry["variants"].values()))
        text = entry.get(lang) or entry.get("hinglish") or entry["english"]
        values.setdefault("sebi_reg_no", self.sebi.get("sebi_reg_no", ""))
        if not values.get("name"):
            text = text.replace("{name} ji", "")                    # "… {name} ji!" → "…!"
            text = text.replace(" {name}", "").replace("{name}", "")   # cold start: no name yet
            text = re.sub(r"  +", " ", text)
            text = re.sub(r"\s+([!,.:;?])", r"\1", text)
        for k, v in values.items():
            text = text.replace("{" + k + "}", str(v))
        return text

    # ---------- pricing source (the only source of prices) ----------
    def plan(self, plan_id: str):
        for row in self.pricing:
            if row["plan_id"] == plan_id:
                return row
        return None

    def allowed_amounts(self) -> set:
        """Every rupee figure that may appear in a reply because it is in the pricing file."""
        out = set()
        for row in self.pricing:
            try:
                out.add(int(float(row["price_inr"])))
            except (ValueError, KeyError):
                pass
        return out

    def plan_for_profile(self, facts: dict):
        """Profile-to-plan table: first matching row wins. Returns (plan_row, why_line) or (None, '')."""
        def val(field):
            f = facts.get(field)
            return (f or {}).get("value", "").lower() if f else ""

        for rule in self.profile_to_plan:
            ok = True
            for field in ("experience", "segment", "goal", "time_available"):
                want = rule.get(field, "any").strip().lower()
                if want and want != "any" and want not in val(field):
                    ok = False
                    break
            if ok:
                return self.plan(rule["plan_id"]), rule.get("why_line", "")
        return None, ""

    # ---------- golden examples ----------
    def golden_for(self, tags, lang: str, n: int):
        """Pick up to n approved/proposed examples whose tags match; approved first."""
        tags = set(tags)
        if lang in ("english", "hindi"):
            tags.add(lang)
        scored = []
        for ex in self.golden:
            overlap = len(tags & set(ex["tags"]))
            approved = 1 if ex["status"].startswith("APPROVED") else 0
            scored.append((overlap, approved, ex))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        chosen = [ex for ov, ap, ex in scored if ov > 0][:n]
        if not any(ex["id"] == "E1" for ex in chosen) and len(chosen) < n:
            chosen.append(next(ex for ex in self.golden if ex["id"] == "E1"))
        return chosen[:n]

    # ---------- value cards ----------
    def card_for(self, pain_text: str, shown: list):
        """The value card whose pain words appear in his pain (his own words); not shown before."""
        t = (pain_text or "").lower()
        for c in self.cards:
            if c["id"] in shown:
                continue
            if any(w in t for w in c["pain_words"]):
                return c
        return None

    # ---------- knowledge files ----------
    def kb_files(self):
        """(path, text) for every knowledge file, sorted by name."""
        kb = self.folder / "kb"
        out = []
        for p in sorted(kb.glob("*.md")):
            self.versions["kb/" + p.name] = _fp(p)
            out.append((p, p.read_text(encoding="utf-8")))
        return out

    def version_string(self) -> str:
        """One fingerprint for the whole pack (recorded in every trace)."""
        h = hashlib.sha1("".join(f"{k}={v}" for k, v in sorted(self.versions.items())).encode()).hexdigest()
        return h[:10]


def parse_front_matter(text: str):
    """Split '---' header lines (key: value) from the body of a knowledge file."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, flags=re.S)
    if not m:
        return {}, text
    head = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            head[k.strip()] = v.strip()
    return head, m.group(2)


PACK = ContentPack()
