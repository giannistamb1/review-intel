"""Paths, settings and the .env loader. Stdlib only.

Every setting is an environment variable. A `.env` file in the repo root is read on start-up
but never overrides a variable that is already set in the shell.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
OUTPUT = ROOT / "output"
CACHE = ROOT / ".cache"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Settings:
    """Read at run time (not import time) so a .env loaded later still applies."""

    def __init__(self) -> None:
        load_dotenv()
        self.treg_key = os.environ.get("TREG_API_KEY", "").strip()
        self.treg_org = os.environ.get("TREG_ORG", "").strip()  # only needed for a personal sign-in token
        self.treg_base = os.environ.get("TREG_BASE_URL", "https://treg.to").rstrip("/")
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.model = os.environ.get("REVIEW_INTEL_MODEL", "claude-haiku-5-5")
        self.max_usd = _f("REVIEW_INTEL_MAX_USD", 2.0)
        self.workers = max(1, int(_f("REVIEW_INTEL_WORKERS", 8)))
        self.radius_mi = _f("REVIEW_INTEL_RADIUS_MI", 5.0)
        self.min_comp_reviews = int(_f("REVIEW_INTEL_MIN_COMPETITOR_REVIEWS", 10))
        # Anthropic list price per million tokens, used for the spend meter. Override if it changes.
        self.llm_in_per_mtok = _f("REVIEW_INTEL_LLM_IN_PER_MTOK", 0.10)
        self.llm_out_per_mtok = _f("REVIEW_INTEL_LLM_OUT_PER_MTOK", 0.50)
        self.port = int(_f("REVIEW_INTEL_PORT", 8765))
