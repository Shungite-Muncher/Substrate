"""Semiconductor Industry Association monthly global sales (WSTS 3-month moving
average, US$ billions). Each release quotes the current month, the prior month
and the year-ago month, so even a handful of releases rebuilds useful history."""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from .. import db, http

log = logging.getLogger(__name__)
LIST_URLS = ["https://www.semiconductors.org/news-events/latest-news/",
             "https://www.semiconductors.org/news-events/latest-news/page/{n}/"]
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}

_CURRENT = re.compile(r"\$\s?([\d,.]+)\s+billion\s+(?:during|in)\s+the\s+month\s+of\s+([A-Za-z]+)\s+(\d{4})", re.I)
_OTHER = re.compile(r"(?:the\s+)?([A-Za-z]+)\s+(\d{4})\s+total\s+of\s+\$\s?([\d,.]+)\s+billion", re.I)
_REGION = re.compile(r"(Americas|Europe|Japan|China|Asia Pacific/All Other)\s*\((-?[\d.]+)%\)", re.I)


def release_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        if "semiconductor sales" in text and "month" in text and a["href"] not in seen:
            seen.add(a["href"])
            out.append(a["href"])
    return out


def parse_release(text: str) -> dict | None:
    m = _CURRENT.search(text)
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower())
    if not month:
        return None
    points = {f"{m.group(3)}-{month:02d}": float(m.group(1).replace(",", ""))}
    # Only look in the same paragraph for the comparison months.
    para = text[m.start(): m.start() + 600]
    for om in _OTHER.finditer(para):
        mo = MONTHS.get(om.group(1).lower())
        if mo:
            points.setdefault(f"{om.group(2)}-{mo:02d}", float(om.group(3).replace(",", "")))
    regions = {r.group(1): float(r.group(2)) for r in _REGION.finditer(text)}
    return {"period": f"{m.group(3)}-{month:02d}", "points": points, "regions_yoy_pct": regions}


WP_API = "https://www.semiconductors.org/wp-json/wp/v2/posts"
FEED = "https://www.semiconductors.org/feed/"


def _text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def _from_api(backfill: bool) -> list[tuple[str, str]]:
    """WordPress REST API: full release text as JSON, no per-page fetches. Primary source."""
    out = []
    for page in range(1, 26 if backfill else 2):
        r = http.get(WP_API, params={"search": "semiconductor sales", "per_page": 50 if backfill else 20, "page": page,
                                     "_fields": "date,link,title,content"})
        posts = r.json()
        out += [(p["link"], _text(p["content"]["rendered"])) for p in posts]
        if page >= int(r.headers.get("X-WP-TotalPages", 1)):
            break
    return out


def _from_feed() -> list[tuple[str, str]]:
    """RSS with full content:encoded bodies. First fallback."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(http.get(FEED).content)
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    return [((it.findtext("link") or "").strip(),
             _text(it.findtext("content:encoded", default="", namespaces=ns) or it.findtext("description") or ""))
            for it in root.iter("item")]


def _from_html(backfill: bool, known: set) -> list[tuple[str, str]]:
    """Scrape the news listing and each release page. Last resort (blocked from some cloud IPs)."""
    links: list[str] = []
    for n in (range(1, 6) if backfill else range(1, 2)):
        url = LIST_URLS[0] if n == 1 else LIST_URLS[1].format(n=n)
        try:
            links += [l for l in release_links(http.get(url).text) if l not in links]
        except Exception as e:
            log.info("sia list page %s: %s", n, e)
            break
    out = []
    for url in links:
        if url in known:
            continue
        soup = BeautifulSoup(http.get(url).text, "html.parser")
        out.append((url, (soup.find("article") or soup.find("main") or soup).get_text(" ", strip=True)))
    return out


def run(con, backfill: bool = False) -> dict:
    known = {r["url"] for r in db.rows(con, "SELECT DISTINCT url FROM observations WHERE source='sia'")}
    releases, used, errors = [], None, []
    for name, fetch in (("wp-api", lambda: _from_api(backfill)), ("rss", _from_feed),
                        ("html", lambda: _from_html(backfill, known))):
        try:
            releases = fetch()
        except Exception as e:
            errors.append(f"{name}: {e}")
            log.info("sia %s failed: %s", name, e)
            continue
        if any(parse_release(t) for _, t in releases):
            used = name
            break
    parsed = [(url, parse_release(text)) for url, text in releases]
    parsed = [(u, p) for u, p in parsed if p]
    # oldest headline month first, so newer releases' revisions win on upsert
    parsed.sort(key=lambda x: x[1]["period"])
    rows = new = 0
    for url, rel in parsed:
        new += url not in known
        for period, value in rel["points"].items():
            is_headline = period == rel["period"]
            existing = con.execute("SELECT meta FROM observations WHERE source='sia' AND series='global_sales_3mma' "
                                   "AND period=?", (period,)).fetchone()
            # comparison figures never overwrite a month's own headline release
            if not is_headline and existing and "from_release" not in (existing["meta"] or ""):
                continue
            db.upsert(con, "observations", {
                "source": "sia", "series": "global_sales_3mma", "period": period, "value": value,
                "unit": "US$ billion", "url": url, "fetched_at": db.now_iso(),
                "meta": {"regions_yoy_pct": rel["regions_yoy_pct"]} if is_headline else {"from_release": rel["period"]}})
            rows += 1
    if not used:
        log.warning("sia: every source failed (%s); keeping last stored data", "; ".join(errors))
    return {"source": "sia", "via": used, "releases": len(parsed), "new": new, "rows": rows,
            **({"errors": errors} if errors else {})}
