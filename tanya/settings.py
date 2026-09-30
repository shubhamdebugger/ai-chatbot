"""Settings — config.json (the configuration table) and .env (keys, run mode).

Plain English:
- config.json holds every number with its owner (Tushar / Claude / Open).
- .env holds secrets and the run mode. It is never shared or committed.
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path):
    """Read KEY=VALUE lines from .env into the process environment (no extra library)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")


class Settings:
    def __init__(self, config_path: Path = ROOT / "config.json"):
        self.raw = json.loads(config_path.read_text(encoding="utf-8"))

    # --- numbers from the configuration table ---
    def get(self, name, default=None):
        item = self.raw.get(name)
        if isinstance(item, dict) and "value" in item:
            return item["value"]
        return default

    # --- environment (.env) ---
    @staticmethod
    def env(name, default=""):
        return os.environ.get(name, default)

    @property
    def provider(self) -> str:
        """anthropic | openai | google | mock. Falls back to mock if its key is missing."""
        p = self.env("PROVIDER", "mock").lower()
        if p != "mock" and not self.api_key(p):
            return "mock"
        return p

    @property
    def fallback_provider(self) -> str:
        p = self.env("FALLBACK_PROVIDER", "").lower()
        return p if p and self.api_key(p) else ""

    @staticmethod
    def api_key(provider: str) -> str:
        return {
            "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
            "openai": os.environ.get("OPENAI_API_KEY", ""),
            "google": os.environ.get("GOOGLE_API_KEY", ""),
            "mock": "mock",
        }.get(provider, "")

    def model(self, provider: str, tier: str) -> str:
        return self.raw["models"][provider][tier]

    def price(self, model: str):
        """US$ per million tokens (input, output)."""
        return self.raw["prices_usd_per_million_tokens"].get(model, [0.0, 0.0])

    @property
    def run_mode(self) -> str:
        """dev = file memory + inline processing; server = Redis memory + streams + workers."""
        return self.env("RUN_MODE", "dev").lower()


S = Settings()
