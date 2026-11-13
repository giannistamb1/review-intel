"""Domains and the practice's own homepage (free, direct HTTP, cached).

root_domain(url)      -> "brightsmiles.com"
site_name(domain)     -> the practice name its homepage gives (og:site_name, then <title>), or ""
"""
from __future__ import annotations

import html
import re
import urllib.parse

from .http import UpstreamError, cache_get, cache_put, request

# Two-label public suffixes we are likely to meet. US practices are almost all .com/.net/.org/.dental.
SECOND_LEVEL = {"co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.nz", "com.br", "com.mx", "co.za",
                "co.in", "com.sg", "co.jp", "com.cn", "com.tr", "com.ar", "co.il"}
# Builder hosts: every customer shares the root, so keep the full host or two sites would "match".
SHARED_HOSTS = {"wixsite.com", "business.site", "squarespace.com", "godaddysites.com", "wordpress.com",
                "blogspot.com", "weebly.com", "webflow.io", "netlify.app", "vercel.app", "square.site", "site123.me"}
NOT_A_SITE = {"facebook.com", "instagram.com", "yelp.com", "google.com", "goo.gl", "linktr.ee", "healthgrades.com",
              "zocdoc.com", "opencare.com", "x.com", "twitter.com", "youtube.com", "tiktok.com", "linkedin.com"}


def root_domain(url: str | None) -> str:
    if not url:
        return ""
    u = str(url).strip().lower()
    if "://" not in u:
        u = "http://" + u
    host = (urllib.parse.urlsplit(u).hostname or "").strip(".")
    if not host or "." not in host or re.fullmatch(r"[\d.]+", host):
        return ""
    labels = host.split(".")
    if host.startswith("www."):
        labels = labels[1:]
    n = 3 if ".".join(labels[-2:]) in SECOND_LEVEL else 2
    root = ".".join(labels[-n:])
    if root in SHARED_HOSTS:
        return ".".join(labels)
    return root


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_RE = re.compile(r"<meta[^>]+property=[\"']og:site_name[\"'][^>]*>", re.I)
CONTENT_RE = re.compile(r"content=[\"']([^\"']+)[\"']", re.I)


def _extract_name(page: str) -> str:
    m = OG_RE.search(page)
    if m:
        c = CONTENT_RE.search(m.group(0))
        if c and c.group(1).strip():
            return html.unescape(c.group(1)).strip()
    m = TITLE_RE.search(page)
    if m:
        return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
    return ""


PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?([2-9]\d{2})[\s.\-]?(\d{4})(?!\d)")


def phones_in(text: str) -> list[str]:
    """US 10-digit numbers in a page, digits only, first 40 distinct."""
    out = []
    for m in PHONE_RE.finditer(text or ""):
        n = "".join(m.groups())
        if n not in out:
            out.append(n)
        if len(out) >= 40:
            break
    return out


def phone_digits(p: str | None) -> str:
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else ""


def fetch_homepage(domain: str) -> dict:
    """{"ok", "url", "final_root", "title", "phones", "error"}; cached per domain, failures included
    (a dead site stays dead for the cache's life; delete .cache/site to retry)."""
    hit = cache_get("site", domain)
    if hit is not None and "phones" in hit:
        return hit
    out = {"ok": False, "url": "", "final_root": "", "title": "", "phones": [], "error": ""}
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept-Language": "en-US,en"}
    for url in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
        try:
            status, hdrs, body = request("GET", url, headers=headers, timeout=12, retries=0)
            final = next((v for k, v in hdrs.items() if k.lower() == "x-final-url"), url)
        except UpstreamError as e:
            msg = str(e)
            out["error"] = ("domain does not resolve (no DNS record)" if "nodename" in msg or "Name or service" in msg
                            else "connection failed" if "refused" in msg.lower() or "timed out" in msg.lower()
                            else msg[:80])
            continue
        if status < 400 and body:
            page = body[:2_000_000].decode("utf-8", "replace")
            out.update(ok=True, url=url, final_root=root_domain(final), title=_extract_name(page),
                       phones=phones_in(page), error="")
            break
        out["error"] = f"HTTP {status}"
    cache_put("site", domain, out)
    return out
