"""The three treg jobs, each with a primary and a fallback provider. All return normalised rows.

Job                    Primary (treg id)                 Fallback                           Price/call
Maps search            crawl4ai.google.serp.maps         litescrape.google.serp.maps        $0.00023 / $0.00015
Reviews, newest first  crawl4ai.google.maps.reviews      litescrape.google.maps.reviews     $0.00015 / $0.00015
Reviews, lowest first  litescrape.google.maps.reviews    (crawl4ai has no lowest-first sort) $0.00015
Local Finder           litescrape.google.serp.local      (only used to top up competitors)  $0.00015

Why these: on treg's measured stats (13 Nov 2026) crawl4ai works 99% of the time for both Maps search
and reviews at the lowest price; litescrape is the same price but works 87-90% and was the source of
every 502/503 in the lead-list run, so it is the fallback, not the default.
"""
from __future__ import annotations

from .http import Treg, UpstreamError

PLACE = dict  # normalised place: name, place_id, cid, address, lat, lng, rating, review_count, website, category, categories, maps_url


def _place_c4(r: dict) -> PLACE:
    return {"name": r.get("name") or "", "place_id": r.get("place_id") or "", "cid": str(r.get("cid") or ""),
            "address": r.get("address") or "", "lat": r.get("latitude"), "lng": r.get("longitude"),
            "rating": r.get("rating"), "review_count": r.get("review_count") or 0, "website": r.get("website") or "",
            "category": r.get("category") or "", "categories": r.get("categories") or [],
            "maps_url": r.get("url") or "", "closed": bool(r.get("permanently_closed") or r.get("closed")),
            "phone": r.get("phone") or "",
            "position": r.get("position")}


def _place_ls(r: dict) -> PLACE:
    g = r.get("gps_coordinates") or {}
    links = r.get("links") or {}
    types = r.get("types") or ([r.get("type")] if r.get("type") else [])
    pid = r.get("place_id") or ""
    return {"name": r.get("title") or "", "place_id": pid if str(pid).startswith("ChIJ") else "",
            "cid": str(r.get("data_cid") or (pid if str(pid).isdigit() else "")),
            "address": r.get("address") or "", "lat": g.get("latitude"), "lng": g.get("longitude"),
            "rating": r.get("rating"), "review_count": r.get("reviews") or 0,
            "website": r.get("website") or links.get("website") or "", "category": r.get("type") or "",
            "categories": types, "maps_url": "", "phone": r.get("phone") or links.get("phone") or "", "closed": "closed" in str(r.get("hours") or "").lower()
            and "permanently" in str(r.get("hours") or "").lower(), "position": r.get("position")}


def _unwrap(d):
    return d.get("result", d) if isinstance(d, dict) else {}


def maps_search(t: Treg, query: str, location: str, limit: int = 60) -> tuple[list[PLACE], str]:
    """Google Maps results in Google's order. Returns (places, provider)."""
    try:
        d = t.call("crawl4ai.google.serp.maps", body={"query": query, "location": location, "limit": limit},
                   est_usd=0.00023)
        rows = _unwrap(d).get("rows")
        if rows is not None:
            return [_place_c4(r) for r in rows], "crawl4ai"
    except UpstreamError:
        pass
    d = t.call("litescrape.google.serp.maps", query={"q": f"{query} near {location}", "type": "search", "gl": "us"},
               est_usd=0.00015)
    return [_place_ls(r) for r in (_unwrap(d).get("local_results") or [])], "litescrape"


def local_finder(t: Treg, query: str, location: str) -> list[PLACE]:
    d = t.call("litescrape.google.serp.local", query={"q": query, "location": location, "gl": "us"}, est_usd=0.00015)
    return [_place_ls(r) for r in (_unwrap(d).get("local_results") or [])]


def _rev_c4(r: dict) -> dict:
    return {"id": r.get("review_id") or r.get("url") or "", "author": (r.get("author") or {}).get("name") or "",
            "rating": r.get("rating"), "date": r.get("date") or "", "text": (r.get("text") or "").strip()}


def _rev_ls(r: dict) -> dict:
    return {"id": r.get("review_id") or r.get("link") or "", "author": (r.get("user") or {}).get("name") or "",
            "rating": r.get("rating"), "date": r.get("iso_date") or "", "text": (r.get("snippet") or "").strip()}


def reviews_newest(t: Treg, place_id: str, limit: int = 100) -> tuple[list[dict], str]:
    try:
        d = t.call("crawl4ai.google.maps.reviews", body={"place": place_id, "sort": "newest", "limit": limit},
                   est_usd=0.00015)
        rows = _unwrap(d).get("rows")
        if rows is not None:
            return [_rev_c4(r) for r in rows], "crawl4ai"
    except UpstreamError:
        pass
    d = t.call("litescrape.google.maps.reviews", query={"place_id": place_id, "sort_by": "newestFirst", "num": limit},
               est_usd=0.00015)
    return [_rev_ls(r) for r in (_unwrap(d).get("reviews") or [])], "litescrape"


def reviews_lowest(t: Treg, place_id: str, limit: int = 100) -> list[dict]:
    d = t.call("litescrape.google.maps.reviews", query={"place_id": place_id, "sort_by": "ratingLow", "num": limit},
               est_usd=0.00015)
    return [_rev_ls(r) for r in (_unwrap(d).get("reviews") or [])]
