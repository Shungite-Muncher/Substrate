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


def run(con, backfill: bool = False) -> dict:
    known = {r["url"] for r in db.rows(con, "SELECT DISTINCT url FROM observations WHERE source='sia'")}
    links: list[str] = []
    pages = range(1, 6) if backfill else range(1, 2)
    for n in pages:
        url = LIST_URLS[0] if n == 1 else LIST_URLS[1].format(n=n)
        try:
            links += [l for l in release_links(http.get(url).text) if l not in links]
        except Exception as e:
            log.info("sia list page %s: %s", n, e)
            break
    new = [l for l in links if l not in known]
    rows = 0
    # oldest first so newer releases' revisions win on upsert
    for url in reversed(new):
        try:
            soup = BeautifulSoup(http.get(url).text, "html.parser")
        except Exception as e:
            log.warning("sia release %s: %s", url, e)
            continue
        body = soup.find("article") or soup.find("main") or soup
        parsed = parse_release(body.get_text(" ", strip=True))
        if not parsed:
            continue
        for period, value in parsed["points"].items():
            is_headline = period == parsed["period"]
            existing = con.execute("SELECT url FROM observations WHERE source='sia' AND series='global_sales_3mma' "
                                   "AND period=?", (period,)).fetchone()
            # comparison figures never overwrite a month's own headline release
            if not is_headline and existing:
                continue
            db.upsert(con, "observations", {
                "source": "sia", "series": "global_sales_3mma", "period": period, "value": value,
                "unit": "US$ billion", "url": url, "fetched_at": db.now_iso(),
                "meta": {"regions_yoy_pct": parsed["regions_yoy_pct"]} if is_headline else {"from_release": parsed["period"]}})
            rows += 1
    return {"source": "sia", "releases": len(new), "rows": rows}
