"""Row processing and the job runner shared by the headless CLI and the local web app.

Per row:
  1. Maps search "dentist" around "<city>, <ST> <ZIP>" (one call per ZIP, shared by rows in that ZIP).
     The prospect is usually in it; that same list is the competitor pool, in Google's ranking order.
  2. Not in the pool -> read the homepage name (free) and search Maps for that name; then for the bare domain.
     A listing is accepted only when its website has the same root domain, or (no website / same site)
     its name matches the homepage name in the same ZIP or city.
  3. Reviews newest-first (up to 100). Fewer than 5 one/two-star reviews with text and the place has more
     reviews than we read -> one lowest-first page as well. Keep the 5 most recent.
  4. One LLM call per practice for the 4-5 word complaint summaries.
  5. Competitors from the pool; if fewer than 3 pass the rules, top up from Google Local Finder.
"""
from __future__ import annotations

import csv
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import providers
from .config import Settings
from .http import AuthError, BudgetExceeded, Meter, Treg, UpstreamError
from .logic import bullet, centroid, competitors, match_listing, name_specific, pick_low_reviews
from .names import clean_company_name, clean_or_blank, reviewer_name
from .summarize import NONE, backend, summarise
from .website import fetch_homepage, root_domain

REQUIRED = ["domain", "city", "state", "zip"]
ALIASES = {"domain": {"domain", "website", "url", "site", "root_domain", "company_domain"},
           "city": {"city", "town"},
           "state": {"state", "st", "state_code", "region"},
           "zip": {"zip", "zipcode", "zip_code", "postal_code", "postcode", "postal"}}
OUT_COLS = ["bad_reviews", "competitor_1", "competitor_2", "competitor_3", "status", "notes", "bad_review_count",
            "google_listing_name", "google_rating", "google_review_count", "google_maps_url", "listing_match"]
MAX_ROWS = 5000
WANT = 5          # bullets per practice (brief: 3-5)
CANDIDATES = 8    # recent 1-2 star reviews sent to the LLM, so a non-complaint can be replaced
STAGES = ["queued", "listing", "reviews", "summaries", "competitors", "done"]


class InputError(ValueError):
    pass


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def parse_csv(text: str) -> tuple[list[str], list[dict], dict[str, str]]:
    """-> (original header, rows, {required field: original column name}). Raises InputError."""
    text = text.lstrip("﻿")
    if not text.strip():
        raise InputError("The file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = [h for h in (reader.fieldnames or []) if h is not None]
    colmap = {}
    for need in REQUIRED:
        for h in header:
            if _norm_header(h) in ALIASES[need]:
                colmap[need] = h
                break
    missing = [c for c in REQUIRED if c not in colmap]
    if missing:
        raise InputError(f"Missing column(s): {', '.join(missing)}. Required: domain, city, state, zip "
                         f"(found: {', '.join(header) or 'none'}).")
    clash = [c for c in OUT_COLS if c in header]
    if clash:
        raise InputError(f"Input already has output column(s) {', '.join(clash)}. Upload the original list.")
    rows = [{k: (v or "").strip() for k, v in r.items() if k is not None} for r in reader]
    rows = [r for r in rows if any(r.values())]
    if not rows:
        raise InputError("No data rows under the header.")
    if len(rows) > MAX_ROWS:
        raise InputError(f"{len(rows)} rows; the limit per run is {MAX_ROWS}. Split the file.")
    return header, rows, colmap


def normalise_input(r: dict, colmap: dict) -> tuple[dict, str]:
    """-> ({domain, city, state, zip}, problem). ZIPs that lost a leading zero in Excel are padded."""
    dom = root_domain(r.get(colmap["domain"], ""))
    city = r.get(colmap["city"], "").strip()
    state = r.get(colmap["state"], "").strip().upper()
    z = re.sub(r"\D", "", r.get(colmap["zip"], ""))[:5]
    if 3 <= len(z) < 5:
        z = z.zfill(5)
    probs = []
    if not dom:
        probs.append("domain missing or not a domain")
    if len(z) != 5 and not city:
        probs.append("need a 5-digit ZIP or a city")
    return {"domain": dom, "city": city, "state": state, "zip": z if len(z) == 5 else ""}, "; ".join(probs)


def _loc(inp: dict) -> str:
    left = ", ".join(x for x in (inp["city"], inp["state"]) if x)
    return f"{left} {inp['zip']}".strip()


def process_row(inp: dict, t: Treg, s: Settings, meter: Meter, progress=lambda stage: None) -> dict:
    out = {c: "" for c in OUT_COLS}
    notes: list[str] = []
    loc = _loc(inp)

    # 1-2. listing
    progress("listing")
    pool, _ = providers.maps_search(t, "dentist", loc, limit=60)
    place, how = match_listing(pool, inp["domain"], inp["zip"], inp["city"])
    if place is None:
        site = fetch_homepage(inp["domain"])  # free: its name, phone numbers and redirect target
        name, phones = site.get("title") or "", site.get("phones") or []
        alt = (site["final_root"],) if site.get("final_root") and site["final_root"] != inp["domain"] else ()
        # strong evidence first (website domain, homepage phone), across the pool and two targeted searches;
        # the weaker name rule runs only after every strong check has failed
        place, how = match_listing(pool, inp["domain"], inp["zip"], inp["city"], "", phones, alt)
        seen = list(pool)
        for q in [inp["domain"]] + ([name] if name_specific(clean_company_name(name)[0] or name) else []):
            if place:
                break
            res, _ = providers.maps_search(t, q, loc, limit=10)
            seen += res
            place, how = match_listing(res, inp["domain"], inp["zip"], inp["city"], "", phones, alt)
        if place is None and name:
            place, how = match_listing(seen, inp["domain"], inp["zip"], inp["city"], name, phones, alt)
        if place is None and not site.get("ok"):
            notes.append(f"website did not load: {site.get('error') or 'no response'}")
        elif place is None:
            notes.append(f"checked: website domain, {len(phones)} phone number(s) on the homepage, "
                         f"homepage name '{name[:60]}'")

    if place is None:
        out["status"] = "no_listing"
        notes.insert(0, "Google listing not found: no Maps result near this ZIP has this website, a phone "
                        "number from its homepage, or its homepage name, so no reviews")
        prospect = {"domain": inp["domain"], "rating": None, "name": ""}
        prospect["lat"], prospect["lng"] = centroid(pool, inp["zip"])
    else:
        out.update(google_listing_name=place["name"], google_rating=place.get("rating") or "",
                   google_review_count=place.get("review_count") or 0, listing_match=how,
                   google_maps_url=place.get("maps_url") or (f"https://www.google.com/maps/place/?q=place_id:"
                                                              f"{place['place_id']}" if place.get("place_id") else ""))
        prospect = {**place, "domain": inp["domain"]}

        # 3. reviews
        progress("reviews")
        low, star_only = [], 0
        if place.get("place_id") and (place.get("review_count") or 0) > 0:
            newest, _ = providers.reviews_newest(t, place["place_id"])
            low, star_only = pick_low_reviews(newest, want=CANDIDATES)
            if len(low) < CANDIDATES and (place.get("review_count") or 0) > len(newest):
                try:
                    low, star_only = pick_low_reviews(newest, providers.reviews_lowest(t, place["place_id"]),
                                                      want=CANDIDATES)
                except UpstreamError as e:
                    notes.append(f"lowest-first review page failed ({str(e)[:80]}); used newest reviews only")
        # 4. summaries: summarise up to 8 recent candidates in one call, keep the 5 newest that hold a complaint
        bullets, no_complaint, missing = [], 0, 0
        if low:
            progress("summaries")
            sums, err = summarise({str(i): r["text"] for i, r in enumerate(low)}, api_key=s.anthropic_key,
                                  model=s.model, meter=meter, in_price=s.llm_in_per_mtok,
                                  out_price=s.llm_out_per_mtok)
            for i, r in enumerate(low):
                if len(bullets) >= WANT:
                    break
                sm = sums.get(str(i))
                if sm == NONE:
                    no_complaint += 1
                elif sm:
                    bullets.append(bullet(reviewer_name(r["author"]), sm))
                else:
                    missing += 1
            if missing and err:
                notes.append(f"{missing} review summary(ies) missing ({err}); rerun to fill")
        out["bad_reviews"] = "\n".join(bullets)
        out["bad_review_count"] = len(bullets)
        if len(bullets) < 3 and not missing:
            extra = []
            if star_only:
                extra.append(f"{star_only} more are star-only with no text")
            if no_complaint:
                extra.append(f"{no_complaint} had no actual complaint in the text")
            count = f"only {len(bullets)}" if bullets else "no"
            notes.append(f"{count} one/two-star review(s) with a written complaint on Google"
                         + (f" ({'; '.join(extra)})" if extra else ""))

    # 5. competitors
    progress("competitors")
    require_higher = bool(prospect.get("rating"))
    comps, dropped = competitors(prospect, pool, radius_mi=s.radius_mi, min_reviews=s.min_comp_reviews,
                                 require_higher=require_higher)
    if len(comps) < 3 and prospect.get("lat") is not None:
        try:
            extra = providers.local_finder(t, "dentist", loc)
            comps, dropped = competitors(prospect, pool + extra, radius_mi=s.radius_mi,
                                         min_reviews=s.min_comp_reviews, require_higher=require_higher)
        except UpstreamError as e:
            notes.append(f"Local Finder top-up failed ({str(e)[:80]})")
    for i, c in enumerate(comps[:3], 1):
        out[f"competitor_{i}"] = c["name"]
    if prospect.get("lat") is None:
        notes.append("no competitors: Maps returned nothing to measure distance from")
    elif len(comps) < 3:
        rule = "rated higher than the prospect, " if require_higher else ""
        top = ", ".join(f"{k} {v}" for k, v in sorted(dropped.items(), key=lambda x: -x[1])[:3])
        notes.append(f"only {len(comps)} real competitor(s) {rule}with {s.min_comp_reviews}+ reviews within "
                     f"{s.radius_mi:g} mi (excluded: {top or 'none found'})")
    if place is None and comps:
        notes.append("competitors measured from the ZIP centre, without the higher-rating rule")

    if not out["status"]:
        out["status"] = "ok" if (out["bad_review_count"] >= 3 and len(comps) >= 3) else "partial"
    out["notes"] = "; ".join(notes)
    out["_competitors_detail"] = comps
    return out


class Job:
    """One run over one CSV. Used by both the CLI and the web app; thread-safe progress snapshot."""

    def __init__(self, text: str, filename: str = "leads.csv", settings: Settings | None = None) -> None:
        self.s = settings or Settings()
        self.filename = filename
        self.header, self.raw_rows, self.colmap = parse_csv(text)
        self.meter = Meter(self.s.max_usd)
        self.treg = Treg(self.s.treg_key, self.meter, self.s.treg_base, self.s.treg_org)
        self.lock = threading.Lock()
        self.rows = [{"i": i, "input": {}, "stage": "queued", "result": None, "seconds": None}
                     for i in range(len(self.raw_rows))]
        self.started = self.finished = None
        self.state = "pending"
        self.fatal = ""

    def _one(self, i: int) -> None:
        row = self.rows[i]
        inp, prob = normalise_input(self.raw_rows[i], self.colmap)
        row["input"] = inp
        t0 = time.time()

        def progress(stage):
            row["stage"] = stage

        if prob:
            res = {c: "" for c in OUT_COLS}
            res.update(status="invalid_input", notes=prob)
        elif self.fatal:
            res = {c: "" for c in OUT_COLS}
            res.update(status="skipped", notes=self.fatal)
        else:
            try:
                res = process_row(inp, self.treg, self.s, self.meter, progress)
            except BudgetExceeded as e:
                res = {c: "" for c in OUT_COLS}
                res.update(status="skipped_budget", notes=str(e))
            except AuthError as e:
                self.fatal = f"stopped: {e}"
                res = {c: "" for c in OUT_COLS}
                res.update(status="error", notes=str(e))
            except Exception as e:  # noqa: BLE001 - one bad row must not kill the run
                res = {c: "" for c in OUT_COLS}
                res.update(status="error", notes=f"{type(e).__name__}: {str(e)[:200]}; rerun to retry (cached "
                                                  f"steps cost nothing)")
        row["result"] = res
        row["seconds"] = round(time.time() - t0, 1)
        row["stage"] = "done"

    def run(self) -> None:
        self.started = time.time()
        self.state = "running"
        if not self.s.treg_key:
            self.fatal = "TREG_API_KEY is not set (see .env.example)"
        with ThreadPoolExecutor(self.s.workers) as ex:
            list(ex.map(self._one, range(len(self.rows))))
        self.finished = time.time()
        self.state = "done"

    # ----- output -----

    def out_header(self) -> list[str]:
        return self.header + OUT_COLS

    def out_rows(self) -> list[dict]:
        rows = []
        for raw, r in zip(self.raw_rows, self.rows):
            res = r["result"] or {c: "" for c in OUT_COLS}
            rows.append({**raw, **{c: res.get(c, "") for c in OUT_COLS}})
        return rows

    def csv_text(self) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=self.out_header(), extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(self.out_rows())
        return buf.getvalue()

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for r in self.rows:
            if r["result"]:
                counts[r["result"]["status"]] = counts.get(r["result"]["status"], 0) + 1
        done = sum(1 for r in self.rows if r["stage"] == "done")
        el = (self.finished or time.time()) - self.started if self.started else 0
        return {"file": self.filename, "rows": len(self.rows), "done": done, "state": self.state,
                "status_counts": counts, "seconds": round(el, 1), "fatal": self.fatal,
                "llm_backend": backend(self.s.anthropic_key) or "none", "model": self.s.model,
                "spend": self.meter.snapshot(), "workers": self.s.workers}

    def snapshot(self) -> dict:
        rows = []
        for r in self.rows:
            res = r["result"] or {}
            rows.append({"i": r["i"], "domain": r["input"].get("domain") or self.raw_rows[r["i"]].get(
                self.colmap["domain"], ""), "stage": r["stage"], "seconds": r["seconds"],
                "status": res.get("status", ""), "notes": res.get("notes", ""),
                "listing": res.get("google_listing_name", ""), "rating": res.get("google_rating", ""),
                "bad_reviews": res.get("bad_reviews", ""),
                "competitors": [res.get(f"competitor_{k}", "") for k in (1, 2, 3) if res.get(f"competitor_{k}")]})
        return {"summary": self.summary(), "rows": rows}


def template_csv() -> str:
    return "domain,city,state,zip\nbrightsmilesdental.com,Austin,TX,78701\n"


__all__ = ["Job", "InputError", "parse_csv", "normalise_input", "process_row", "template_csv", "OUT_COLS",
           "clean_or_blank"]
