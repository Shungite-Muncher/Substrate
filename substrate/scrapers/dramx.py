"""Memory spot prices from DRAMeXchange's public homepage table (TrendForce).

Spot prices move weeks before contract prices, which is what buyers actually pay, so
they're the best free early read on memory pricing. One snapshot per day accumulates
into a history the memory price signal uses. Only derived % changes are published on the
dashboard; the raw TrendForce table stays in the accumulator."""
from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from .. import db, http

URL = "https://www.dramexchange.com/"
TRACKED = re.compile(r"^(DDR5 16Gb|DDR4 16Gb|DDR4 8Gb|DDR3 4Gb|SLC|MLC|TLC)", re.I)


def parse(html: str) -> dict[str, float]:
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"], recursive=False)]
        if len(cells) >= 6 and TRACKED.match(cells[0]) and "GB" not in cells[0]:   # chips, not modules
            try:
                out[cells[0]] = float(cells[5].replace(",", ""))        # session average, US$
            except ValueError:
                continue
    return out


def run(con, backfill: bool = False) -> dict:
    prices = parse(http.get(URL).text)
    day = date.today().isoformat()
    for item, v in prices.items():
        kind = "nand" if re.match(r"(SLC|MLC|TLC)", item) else "dram"
        db.upsert(con, "observations", {"source": "dramx", "series": item, "period": day, "value": v,
                                        "unit": "US$ spot session avg", "meta": {"kind": kind}, "url": URL,
                                        "fetched_at": db.now_iso()})
    return {"source": "dramx", "items": len(prices)}
