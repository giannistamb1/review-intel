"""HTTP plumbing shared by every paid call: treg REST client, on-disk cache, spend meter, retries.

treg REST contract (verified with live calls, 13 Nov 2026):
    POST https://treg.to/call/<tool-id>      JSON body       (crawl4ai.* tools)
    GET  https://treg.to/call/<tool-id>?k=v                  (litescrape.* tools)
    header  X-Treg-Token: $TREG_API_KEY      (Authorization: Bearer is NOT accepted on /call -> 401)
    header  X-Treg-Org: <team-slug>          only for a personal sign-in token; a team API key carries its team
    response body = the provider's JSON, verbatim
    response header X-Treg-Cost-Micro = what this call cost, in millionths of a dollar
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import CACHE

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
UA = "review-intel/1.0 (+https://github.com)"


class BudgetExceeded(RuntimeError):
    pass


class AuthError(RuntimeError):
    """Bad or missing key. Stops the whole run: every other row would fail the same way."""


class UpstreamError(RuntimeError):
    pass


class Meter:
    """Per-run spend and call counters. Thread-safe. The cap covers treg + LLM together."""

    def __init__(self, max_usd: float) -> None:
        self.max_usd = max_usd
        self.lock = threading.Lock()
        self.treg_usd = 0.0
        self.llm_usd = 0.0
        self.calls = 0
        self.cache_hits = 0
        self.retries = 0
        self.failures = 0
        self.by_tool: dict[str, dict] = {}

    @property
    def total(self) -> float:
        return self.treg_usd + self.llm_usd

    def reserve(self, est_usd: float, what: str) -> None:
        with self.lock:
            if self.total + est_usd > self.max_usd:
                raise BudgetExceeded(f"spend cap ${self.max_usd:.2f} reached before {what} "
                                     f"(spent ${self.total:.4f}); raise REVIEW_INTEL_MAX_USD and rerun")

    def add(self, tool: str, usd: float, *, llm: bool = False, cached: bool = False) -> None:
        with self.lock:
            t = self.by_tool.setdefault(tool, {"calls": 0, "cache_hits": 0, "usd": 0.0})
            if cached:
                self.cache_hits += 1
                t["cache_hits"] += 1
                return
            self.calls += 1
            t["calls"] += 1
            t["usd"] += usd
            if llm:
                self.llm_usd += usd
            else:
                self.treg_usd += usd

    def snapshot(self) -> dict:
        with self.lock:
            return {"treg_usd": round(self.treg_usd, 5), "llm_usd": round(self.llm_usd, 5),
                    "total_usd": round(self.total, 5), "max_usd": self.max_usd, "paid_calls": self.calls,
                    "cache_hits": self.cache_hits, "retries": self.retries, "failures": self.failures,
                    "by_tool": {k: dict(v, usd=round(v["usd"], 5)) for k, v in self.by_tool.items()}}


# ---------------- cache ----------------

_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    """One in-flight request per cache key: rows sharing a ZIP share one paid Maps call."""
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def cache_path(namespace: str, payload) -> Path:
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    d = CACHE / namespace
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def cache_get(namespace: str, payload):
    p = cache_path(namespace, payload)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError:
            return None
    return None


def cache_put(namespace: str, payload, value) -> None:
    p = cache_path(namespace, payload)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(p)


# ---------------- raw HTTP with retries ----------------

def request(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
            timeout: float = 90, retries: int = 4, meter: Meter | None = None) -> tuple[int, dict, bytes]:
    """Returns (status, headers, body). Retries 408/425/429/5xx and network errors with exponential
    backoff + jitter, honouring Retry-After. Any other status is returned to the caller."""
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                hdrs = dict(r.headers.items())
                hdrs["X-Final-Url"] = r.geturl()  # after redirects
                return r.status, hdrs, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, data = e.code, dict(e.headers.items()) if e.headers else {}, e.read() if e.fp else b""
            if status not in RETRY_STATUS or attempt == retries:
                return status, hdrs, data
            last = f"HTTP {status}"
            wait = _retry_after(hdrs) or min(30.0, 1.5 * 2 ** attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = f"{type(e).__name__}: {str(e)[:120]}"
            if attempt == retries:
                raise UpstreamError(last) from e
            wait = min(30.0, 1.5 * 2 ** attempt)
        if meter:
            with meter.lock:
                meter.retries += 1
        time.sleep(wait + random.uniform(0, 0.75))
    raise UpstreamError(last)


def _retry_after(h: dict) -> float:
    v = next((v for k, v in h.items() if k.lower() == "retry-after"), "")
    try:
        return min(60.0, float(v))
    except ValueError:
        return 0.0


# ---------------- treg ----------------

class Treg:
    def __init__(self, key: str, meter: Meter, base: str = "https://treg.to", org: str = "") -> None:
        self.key, self.meter, self.base, self.org = key, meter, base, org

    def call(self, tool: str, *, body: dict | None = None, query: dict | None = None, est_usd: float = 0.0005):
        """Cached call to a treg catalog tool. Returns parsed JSON. Raises UpstreamError after retries.
        Failures are never cached, so a rerun retries only what failed and pays for nothing else."""
        payload = {"tool": tool, "body": body, "query": query}
        hit = cache_get("treg", payload)
        if hit is not None:
            self.meter.add(tool, 0, cached=True)
            return hit
        with _key_lock(json.dumps(payload, sort_keys=True)):
            hit = cache_get("treg", payload)  # another worker may have fetched it while we waited
            if hit is not None:
                self.meter.add(tool, 0, cached=True)
                return hit
            if not self.key:
                raise AuthError("TREG_API_KEY is not set")
            self.meter.reserve(est_usd, tool)
            url = f"{self.base}/call/{tool}"
            if query:
                url += "?" + urllib.parse.urlencode(query)
            headers = {"X-Treg-Token": self.key, "Accept": "application/json"}
            if self.org:
                headers["X-Treg-Org"] = self.org
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            status, hdrs, raw = request("POST" if body is not None else "GET", url, headers=headers, body=data,
                                        meter=self.meter)
            low = {k.lower(): v for k, v in hdrs.items()}
            cost = int(low.get("x-treg-cost-micro") or 0) / 1_000_000
            if status in (401, 403) and low.get("x-treg-error"):
                raise AuthError(f"treg rejected the key ({status}): {raw[:200].decode('utf-8', 'replace')}")
            if status == 402:
                raise AuthError("treg balance or spend cap exhausted (402); top up the team balance")
            self.meter.add(tool, cost if status < 400 else 0)
            if status >= 400:
                with self.meter.lock:
                    self.meter.failures += 1
                raise UpstreamError(f"{tool} answered {status}: {raw[:200].decode('utf-8', 'replace')}")
            try:
                parsed = json.loads(raw)
            except ValueError as e:
                raise UpstreamError(f"{tool} returned non-JSON") from e
            if low.get("x-treg-upstream-failed"):
                raise UpstreamError(f"{tool}: provider reported failure ({low['x-treg-upstream-failed']})")
            cache_put("treg", payload, parsed)
            return parsed
