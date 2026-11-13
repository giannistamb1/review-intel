"""python3 -m review_intel run path/to/leads.csv [--out output/x.csv]   headless
   python3 -m review_intel serve [--port 8765] [--no-open]             local web app
   python3 -m review_intel check                                      keys + treg reachability, no spend
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from .config import OUTPUT, Settings
from .pipeline import InputError, Job
from .summarize import backend


def cmd_run(args) -> int:
    path = Path(args.csv).expanduser()
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        return 2
    try:
        job = Job(path.read_text(encoding="utf-8-sig", errors="replace"), path.name)
    except InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    s = job.s
    if not s.treg_key:
        print("error: TREG_API_KEY is not set. Copy .env.example to .env and add it.", file=sys.stderr)
        return 2
    print(f"review-intel: {len(job.rows)} rows from {path.name} · {s.workers} workers · cap ${s.max_usd:.2f} · "
          f"summaries via {backend(s.anthropic_key) or 'NONE (bullets will be blank)'}")
    t = threading.Thread(target=job.run, daemon=True)
    t.start()
    last = -1
    while t.is_alive():
        t.join(1.0)
        snap = job.summary()
        if snap["done"] != last:
            last = snap["done"]
            print(f"  {snap['done']}/{snap['rows']} rows · ${snap['spend']['total_usd']:.4f} · {snap['seconds']}s",
                  flush=True)
    out = Path(args.out).expanduser() if args.out else OUTPUT / f"{path.stem}_enriched.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(job.csv_text(), encoding="utf-8")
    summ = job.summary()
    summ["rows_detail"] = [{"domain": r["input"].get("domain"), "status": (r["result"] or {}).get("status"),
                            "bad_reviews": (r["result"] or {}).get("bad_review_count"),
                            "competitors": sum(1 for k in (1, 2, 3) if (r["result"] or {}).get(f"competitor_{k}")),
                            "seconds": r["seconds"], "notes": (r["result"] or {}).get("notes")} for r in job.rows]
    sp = out.with_name(out.stem + "_summary.json")
    sp.write_text(json.dumps(summ, indent=2))
    print("\nstatus   " + "  ".join(f"{k}={v}" for k, v in sorted(summ["status_counts"].items())))
    sp_ = summ["spend"]
    print(f"spend    ${sp_['total_usd']:.4f} (treg ${sp_['treg_usd']:.4f} + LLM ${sp_['llm_usd']:.4f}) · "
          f"{sp_['paid_calls']} paid calls · {sp_['cache_hits']} cache hits · {sp_['retries']} retries")
    print(f"time     {summ['seconds']}s")
    print(f"output   {out}\nsummary  {sp}")
    if summ["fatal"]:
        print(f"STOPPED  {summ['fatal']}", file=sys.stderr)
        return 1
    return 0


def cmd_check(_args) -> int:
    s = Settings()
    print(f"TREG_API_KEY       {'set' if s.treg_key else 'MISSING'}")
    print(f"ANTHROPIC_API_KEY  {'set' if s.anthropic_key else 'not set'}  -> summaries via "
          f"{backend(s.anthropic_key) or 'NONE'}")
    print(f"spend cap          ${s.max_usd:.2f} per run   workers {s.workers}   radius {s.radius_mi:g} mi")
    if s.treg_key:
        from .http import request
        h = {"X-Treg-Token": s.treg_key}
        if s.treg_org:
            h["X-Treg-Org"] = s.treg_org
        st, _, body = request("GET", f"{s.treg_base}/auth/me", headers=h, timeout=15, retries=1)
        print(f"treg auth          {'ok' if st < 400 else f'FAILED ({st})'}")
        return 0 if st < 400 else 1
    return 1


def cmd_serve(args) -> int:
    from .server import serve
    return serve(port=args.port, open_browser=not args.no_open)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="review_intel", description="Bad Google reviews + top local competitors")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="enrich a CSV headless")
    r.add_argument("csv")
    r.add_argument("--out")
    r.set_defaults(fn=cmd_run)
    sv = sub.add_parser("serve", help="start the local web app")
    sv.add_argument("--port", type=int, default=0)
    sv.add_argument("--no-open", action="store_true")
    sv.set_defaults(fn=cmd_serve)
    c = sub.add_parser("check", help="check keys and treg access (no spend)")
    c.set_defaults(fn=cmd_check)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
