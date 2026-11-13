"""Pure decision logic (no network), so every rule is unit-tested.

match_listing(places, domain, zip, city, site_name) -> (place|None, how)
pick_low_reviews(newest, lowest, want)               -> (most recent 1-2 star reviews with text, star_only_count)
competitors(prospect, candidates, ...)               -> (list, reasons_dropped)
"""
from __future__ import annotations

import difflib
import math
import re
import statistics

from .names import clean_company_name, name_violations
from .website import NOT_A_SITE, phone_digits, root_domain

# ---------------- similarity ----------------

STOP = {"the", "and", "of", "dds", "dmd", "inc", "llc", "pc", "dr", "a"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP}


GENERIC = {"dental", "dentistry", "dentist", "dentists", "care", "family", "center", "centre", "group", "smile",
           "smiles", "office", "clinic", "health", "healthcare", "home", "welcome", "practice", "cosmetic",
           "general", "associates", "studio", "orthodontics", "implants", "oral", "surgery", "pediatric", "kids"}


def _distinct(s: str) -> set[str]:
    return {w for w in _tokens(s) if w not in GENERIC and len(w) > 1}


def name_specific(s: str) -> bool:
    """A name we can match on: at least two words, at least one of them not a generic dental word."""
    return len(_tokens(s)) >= 2 and bool(_distinct(s))


def token_set_ratio(a: str, b: str) -> int:
    """0-100. Same idea as rapidfuzz.token_set_ratio, stdlib only: a name contained in the other scores 100."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0
    inter = " ".join(sorted(ta & tb))
    ra, rb = " ".join(sorted(ta - tb)), " ".join(sorted(tb - ta))
    s1, s2 = (inter + " " + ra).strip(), (inter + " " + rb).strip()
    cands = [difflib.SequenceMatcher(None, s1, s2).ratio()]
    if inter:
        cands += [difflib.SequenceMatcher(None, inter, s1).ratio(), difflib.SequenceMatcher(None, inter, s2).ratio()]
    return round(100 * max(cands))


def miles(a_lat, a_lng, b_lat, b_lng) -> float | None:
    if None in (a_lat, a_lng, b_lat, b_lng):
        return None
    r = 3958.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(h)), 2)


def zip_of(address: str) -> str:
    m = re.findall(r"\b(\d{5})(?:-\d{4})?\b", address or "")
    return m[-1] if m else ""


# ---------------- listing match ----------------

def match_listing(places: list[dict], domain: str, zip5: str = "", city: str = "", site_name: str = "",
                  site_phones: list[str] | None = None, alt_domains: tuple = ()) -> tuple[dict | None, str]:
    """Find the prospect's Google listing. Never guesses; no rule met -> (None, "").
    1) the listing's website has the same root domain (or the domain our site redirects to). Several hits =
       multi-location: prefer the same ZIP, then the same city.
    2) a listing in the same ZIP/city whose phone number is printed on the practice's homepage
       (catches listings that point at an old or vanity domain, e.g. hhdental.center for hhdentalcenter.com)
    3) a listing in the same ZIP/city, with no website or the same one, whose name matches the homepage name
       (token-set >= 90)."""
    doms = {d for d in (domain, *alt_domains) if d}
    open_ = [p for p in places if not p.get("closed")]
    same = [p for p in open_ if root_domain(p.get("website")) in doms]
    if same:
        same.sort(key=lambda p: (zip_of(p.get("address")) != zip5,
                                 city.lower() not in (p.get("address") or "").lower(), p.get("position") or 99))
        return same[0], "website domain"

    def local(p):
        addr = (p.get("address") or "").lower()
        return bool((zip5 and zip_of(addr) == zip5) or (city and city.lower() in addr))

    phones = set(site_phones or [])
    if phones:
        hits = [p for p in open_ if local(p) and phone_digits(p.get("phone")) in phones]
        if len(hits) == 1 or (hits and len({phone_digits(p.get("phone")) for p in hits}) == 1
                              and len({p.get("address") for p in hits}) == 1):
            return hits[0], "phone on website + same ZIP/city"
    if site_name:
        clean, _ = clean_company_name(site_name)
        if not name_specific(clean or site_name):
            return None, ""  # "PDC", "Home", "Dentist": too generic to identify a business by name
        best, best_score = None, 0
        for p in open_:
            if not local(p):
                continue
            w = root_domain(p.get("website"))
            if w and w not in doms and w not in NOT_A_SITE:
                continue  # listing names another website: a different business with a similar name
            if not (_distinct(clean or site_name) <= _tokens(p.get("name") or "")):
                continue  # every distinctive word of the site's name must be in the listing name
            s = token_set_ratio(clean or site_name, p.get("name") or "")
            if s > best_score:
                best, best_score = p, s
        if best is not None and best_score >= 90:
            return best, "name on website + same ZIP/city"
    return None, ""


# ---------------- bad reviews ----------------

def pick_low_reviews(newest: list[dict], lowest: list[dict] | None = None, want: int = 5) -> tuple[list[dict], int]:
    """Most recent `want` reviews rated 1 or 2 stars that have text. Star-only low reviews are skipped
    (there is no complaint to summarise) and counted."""
    seen, low, star_only = set(), [], 0
    for r in list(newest) + list(lowest or []):
        key = r.get("id") or (r.get("author"), r.get("date"))
        if key in seen:
            continue
        seen.add(key)
        try:
            stars = float(r.get("rating") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= stars <= 2:
            continue
        if not (r.get("text") or "").strip():
            star_only += 1
            continue
        low.append(r)
    low.sort(key=lambda r: r.get("date") or "", reverse=True)
    return low[:want], star_only


def bullet(name: str, summary: str) -> str:
    return f"• {name} - {summary}"


def clean_summary(s: str) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip().strip(".\"'").strip()
    s = s[:1].lower() + s[1:] if s[:2] != s[:2].upper() else s  # keep acronyms like "ER"
    return s if 3 <= len(s.split()) <= 6 else ""


# ---------------- competitors ----------------

NON_PRACTICE = re.compile(
    r"\b(dental lab(oratory|s)?|laborator(y|ies)|school of dentistry|dental school|university|college|hospital|"
    r"community (health|clinic|dental)|health cent(er|re)|family health|fqhc|dental supply|supplies|medical cent(er|re)|"
    r"department of|veterans|va|kaiser|clinica|free clinic|appointpanda|dental insurance|dentures? lab|mobile dent\w*|"
    r"staffing|marketing|headquarters|corporate office|health clinic|primary (health )?care|health services|"
    r"medical\s*(&|and)\s*dental|insurance)\b", re.I)
NON_PRACTICE_CATS = {"dental laboratory", "dental supply store", "dental school", "university", "college", "hospital",
                     "general hospital", "community health center", "community health centre", "medical clinic",
                     "medical center", "insurance agency", "dental insurance agency", "non-profit organization",
                     "school", "medical supply store", "medical laboratory", "corporate office", "dental radiology",
                     "dental lab"}
DENTAL = re.compile(r"dent|orthodont|periodont|endodont|prosthodont|oral|implant|teeth|tooth", re.I)


def centroid(places: list[dict], zip5: str) -> tuple[float | None, float | None]:
    """Stand-in location when the prospect has no listing: median of results in its ZIP (else all results)."""
    pts = [(p["lat"], p["lng"]) for p in places if p.get("lat") is not None and zip_of(p.get("address")) == zip5]
    if not pts:
        pts = [(p["lat"], p["lng"]) for p in places if p.get("lat") is not None]
    if not pts:
        return None, None
    return statistics.median(x for x, _ in pts), statistics.median(y for _, y in pts)


def competitors(prospect: dict, candidates: list[dict], *, want: int = 3, radius_mi: float = 5.0,
                min_reviews: int = 10, require_higher: bool = True, min_rating: float = 4.0,
                min_distance_mi: float = 0.0, exclude: list[dict] | None = None, city: str = "",
                order: str = "google") -> tuple[list[dict], dict]:
    """Top local practices that pass every rule: real dental practice, open, 10+ reviews, rated ABOVE
    `min_rating` (4.0 itself is out) and higher than the prospect, within `radius_mi`, not the prospect itself,
    not a same-domain sibling, not at the same address, not a second listing of a brand already picked
    (including anything in `exclude`), and a name that cleans to the Part 3 standard.
    order="google" keeps Google's ranking; order="nearest" puts the prospect's own city first, then distance."""
    p_lat, p_lng = prospect.get("lat"), prospect.get("lng")
    my_dom = prospect.get("domain") or ""
    my_rating = prospect.get("rating")
    my_ids = {x for x in (prospect.get("place_id"), prospect.get("cid")) if x}
    my_name = prospect.get("name") or ""
    out, seen, dropped = [], set(), {}
    for e in exclude or []:
        seen.add(re.sub(r"\s+(?:\d+|ii|iii|iv)$", "", e["name"].lower()))
        if e.get("domain") and e["domain"] not in NOT_A_SITE:
            seen.add(e["domain"])
    ids_seen = set()

    def drop(why):
        dropped[why] = dropped.get(why, 0) + 1

    def key(c):
        d = miles(p_lat, p_lng, c.get("lat"), c.get("lng"))
        same_city = bool(city) and f", {city.lower()}," in f", {(c.get('address') or '').lower()}"
        return (not same_city, d if d is not None else 999)

    ordered = sorted(candidates, key=key) if order == "nearest" else list(candidates)
    for c in ordered:
        cid = c.get("place_id") or c.get("cid") or (c.get("name"), c.get("address"))
        if cid in ids_seen:
            continue  # the same listing from two searches
        ids_seen.add(cid)
        if {c.get("place_id"), c.get("cid")} & my_ids:
            drop("the prospect itself"); continue
        dom = root_domain(c.get("website"))
        if my_dom and dom == my_dom:
            drop("same website as prospect"); continue
        raw = c.get("name") or ""
        cats = [x.lower() for x in ([c.get("category")] + list(c.get("categories") or [])) if x]
        if c.get("closed"):
            drop("closed"); continue
        if NON_PRACTICE.search(raw) or (cats and cats[0] in NON_PRACTICE_CATS) or \
                (cats and not any(DENTAL.search(x) for x in cats)) or (not cats and not DENTAL.search(raw)):
            drop("not a dental practice"); continue
        if (c.get("review_count") or 0) < min_reviews:
            drop(f"fewer than {min_reviews} reviews"); continue
        rating = c.get("rating")
        if not rating:
            drop("no rating"); continue
        if rating <= min_rating:
            drop(f"rated {min_rating:.1f} or lower"); continue
        if require_higher and my_rating and rating <= my_rating:
            drop("not rated higher than prospect"); continue
        dist = miles(p_lat, p_lng, c.get("lat"), c.get("lng"))
        if dist is None:
            drop("no map location"); continue
        if dist > radius_mi:
            drop(f"more than {radius_mi:g} mi away"); continue
        if dist < min_distance_mi:
            continue  # already considered in the inner ring
        if dist < 0.02:
            drop("same address as prospect"); continue
        name, _ = clean_company_name(raw)
        if not name or name_violations(name):
            drop("name could not be cleaned"); continue
        if my_name and token_set_ratio(name, my_name) >= 90:
            drop("same brand as prospect"); continue
        base = re.sub(r"\s+(?:\d+|ii|iii|iv)$", "", name.lower())
        if base in seen or (dom and dom in seen):
            drop("second listing of a brand already picked"); continue
        seen.add(base)
        if dom and dom not in NOT_A_SITE:
            seen.add(dom)
        out.append({"name": name, "rating": rating, "review_count": c.get("review_count"), "distance_mi": dist,
                    "domain": dom, "raw_name": raw})
        if len(out) >= want:
            break
    return out, dropped


def pick_competitors(prospect: dict, candidates: list[dict], *, city: str = "", want: int = 3,
                     radius_mi: float = 5.0, city_radius_mi: float = 15.0, **rules) -> tuple[list[dict], dict, bool]:
    """Within `radius_mi` first, in Google's order. If fewer than `want` qualify, widen to `city_radius_mi`:
    same city first, then nearest. Returns (competitors, reasons dropped in the inner ring, widened?)."""
    inner, dropped = competitors(prospect, candidates, want=want, radius_mi=radius_mi, **rules)
    if len(inner) >= want or city_radius_mi <= radius_mi:
        return inner, dropped, False
    outer, _ = competitors(prospect, candidates, want=want - len(inner), radius_mi=city_radius_mi,
                           min_distance_mi=radius_mi, exclude=inner, city=city, order="nearest", **rules)
    return inner + outer, dropped, True
