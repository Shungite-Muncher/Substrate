"""TSMC monthly consolidated net revenue (NT$ millions) from investor.tsmc.com.
TSMC publishes around the 10th of each month; it is the best free, high-frequency
read on leading-edge foundry loading."""
from __future__ import annotations

import logging
from datetime import date

from bs4 import BeautifulSoup

from .. import db, http

log = logging.getLogger(__name__)
URL = "https://investor.tsmc.com/english/monthly-revenue/{year}"
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def parse(html: str, year: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    out = []
    if not table:
        return out
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 3:
            continue
        m = MONTHS.get(cells[0].lower()[:3])
        rev, yoy = cells[-2], cells[-1]          # consolidated columns are always last
        if not m or not rev:
            continue
        try:
            value = float(rev.replace(",", ""))
        except ValueError:
            continue
        out.append({"period": f"{year}-{m:02d}", "value": value, "yoy": _pct(yoy)})
    return out


def _pct(s: str) -> float | None:
    try:
        return float(s.replace(",", "").rstrip("%"))
    except ValueError:
        return None


def run(con, backfill: bool = False) -> dict:
    today = date.today()
    if backfill:
        years = range(1999, today.year + 1)  # full history feeds the forecast models
    else:  # early in the year the December figure still lands on last year's page
        years = [today.year - 1, today.year] if today.month <= 2 else [today.year]
    n = 0
    for y in years:
        url = URL.format(year=y)
        try:
            rows = parse(http.get(url).text, y)
        except Exception as e:  # one bad year shouldn't sink the run
            log.warning("tsmc %s: %s", y, e)
            continue
        for r in rows:
            db.upsert(con, "observations", {
                "source": "tsmc", "series": "monthly_revenue", "period": r["period"], "value": r["value"],
                "unit": "NT$ million", "meta": {"yoy_pct": r["yoy"]}, "url": url, "fetched_at": db.now_iso()})
            n += 1
    return {"source": "tsmc", "rows": n}
