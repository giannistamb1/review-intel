"""4-5 word complaint summaries. One batched LLM call per practice, cached by review text.

Backend order:
  1. Anthropic Messages API with ANTHROPIC_API_KEY (default model claude-haiku-5-5)
  2. the `claude` CLI (`claude -p`), which any Claude Code user already has signed in
  3. neither -> no summaries; the row says so and the bullets stay blank (never invented)

treg has no general LLM endpoint (catalog searched 13 Nov 2026; the only Claude tool is DataForSEO's
LLM-responses API at $0.0245+/call), so summaries go straight to Anthropic.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from .http import Meter, cache_get, cache_put, request
from .logic import clean_summary

SYSTEM = "You turn patient reviews into short complaint summaries and reply with JSON only."
PROMPT = (
    "You summarise negative Google reviews of a dental practice for a sales email.\n"
    "For each review below, write a summary of the patient's main complaint in EXACTLY 4 or 5 words: lowercase, "
    "no ending punctuation, no names, no quotes, only what the review says (never invent or exaggerate).\n"
    "Examples: \"waited two hours past appointment\", \"billed for work never done\", \"rude front desk staff\".\n"
    "If a review contains no complaint (for example a positive text under a low star rating), answer \"NONE\" "
    "for that id.\n"
    "Reply with ONLY a JSON object mapping each id to its summary.\n\n")
RETRY = "Second try: your last summaries for these reviews were too long or too short. 4 or 5 words each.\n\n"


NONE = "__none__"  # the review has no complaint in it (positive text under a low star rating)


def _key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()[:24]


def backend(api_key: str) -> str:
    if api_key:
        return "anthropic"
    if shutil.which("claude"):
        return "claude-cli"
    return ""


def _parse(txt: str) -> dict:
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _anthropic(texts: dict[str, str], api_key: str, model: str, meter: Meter, in_price: float, out_price: float,
               prefix: str = "") -> dict:
    est = (len(json.dumps(texts)) / 3.5 * in_price + 400 * out_price) / 1e6
    meter.reserve(est, "review summaries")
    body = json.dumps({"model": model, "max_tokens": 600, "temperature": 0, "system": SYSTEM,
                       "messages": [{"role": "user", "content": prefix + PROMPT + json.dumps(texts, ensure_ascii=False)}]}).encode()
    status, _, raw = request("POST", "https://api.anthropic.com/v1/messages", body=body, timeout=60, meter=meter,
                             headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                      "content-type": "application/json"})
    if status >= 400:
        raise RuntimeError(f"Anthropic API {status}: {raw[:200].decode('utf-8', 'replace')}")
    d = json.loads(raw)
    u = d.get("usage") or {}
    meter.add(f"anthropic:{model}", (u.get("input_tokens", 0) * in_price + u.get("output_tokens", 0) * out_price) / 1e6,
              llm=True)
    return _parse("".join(b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text"))


def _claude_cli(texts: dict[str, str], model: str, meter: Meter, prefix: str = "") -> dict:
    meter.reserve(0.01, "review summaries")
    # Isolated: an empty temp cwd, project-only settings (so the user's own hooks, CLAUDE.md and plugins don't
    # hijack the reply), no tools, no saved session. Also keeps the call cheap (~$0.0002 instead of ~$0.01).
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["claude", "-p", "--model", model, "--output-format", "json", "--setting-sources", "project",
                            "--system-prompt", SYSTEM, "--tools", "", "--no-session-persistence",
                            "--disable-slash-commands", "--effort", "low", prefix + PROMPT + json.dumps(texts, ensure_ascii=False)],
                           capture_output=True, text=True, timeout=180, cwd=d,
                           env={**os.environ, "MAX_THINKING_TOKENS": "0", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "4096"})
    env = json.loads(r.stdout)  # the envelope itself is JSON; never fence-scan it
    meter.add(f"claude-cli:{model}", float(env.get("total_cost_usd") or 0), llm=True)
    if env.get("is_error"):
        raise RuntimeError(f"claude CLI: {str(env.get('result'))[:200]}")
    return _parse(env.get("result", ""))


def summarise(texts: dict[str, str], *, api_key: str, model: str, meter: Meter,
              in_price: float = 1.0, out_price: float = 5.0) -> tuple[dict[str, str], str]:
    """{id: text} -> ({id: summary | NONE}, error). Missing ids failed every attempt; the caller leaves them out."""
    out = {}
    for k, v in texts.items():
        hit = cache_get("summaries", _key(model, v))
        if hit:
            out[k] = hit
            meter.add(f"llm-cache:{model}", 0, cached=True)
    todo = {k: v[:1500] for k, v in texts.items() if k not in out}
    err = ""
    how = backend(api_key)
    if todo and not how:
        return out, "no ANTHROPIC_API_KEY and no claude CLI: summaries skipped"
    for attempt in range(3):  # re-ask up to twice for anything missing or the wrong length
        if not todo:
            break
        prefix = RETRY if attempt else ""
        try:
            res = (_anthropic(todo, api_key, model, meter, in_price, out_price, prefix) if how == "anthropic"
                   else _claude_cli(todo, model, meter, prefix))
        except Exception as e:  # noqa: BLE001 - BudgetExceeded must propagate
            if type(e).__name__ == "BudgetExceeded":
                raise
            err = str(e)[:200]
            continue
        for k, v in res.items():
            if k in todo and str(v).strip().upper().strip(".\"'") == "NONE":
                out[k] = NONE
                cache_put("summaries", _key(model, texts[k]), NONE)
                continue
            s = clean_summary(v)
            if k in todo and s and 4 <= len(s.split()) <= 5:
                out[k] = s
                cache_put("summaries", _key(model, texts[k]), s)
        todo = {k: v for k, v in todo.items() if k not in out}
    if todo and not err:
        err = f"{len(todo)} summary(ies) were not 4-5 words after 3 tries"
    return out, err
