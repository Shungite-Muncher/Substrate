"""Monthly indicators that are released to the wires rather than through a free API:

  * Taiwan export orders (Ministry of Economic Affairs): orders booked by Taiwan firms,
    a 1-3 month lead on shipments
  * China semiconductor exports (customs): China's mature-node output reaching the market
  * TrendForce memory contract-price outlook (quarterly): the prices buyers actually pay

Each is read from wire headlines, kept only when the parser can pin a figure to a period.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from .. import db, http
from .news import parse_feed

log = logging.getLogger(__name__)
MAX_AGE_DAYS = 120   # news search returns years-old stories too; only recent releases count
URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_NUM = r"(-?[\d.]+)\s*(?:%|pct|percent)"

INDICATORS = {
    "taiwan_export_orders_yoy": {
        "queries": ["Taiwan export orders", "Taiwan export orders percent"],
        "must": re.compile(r"export orders?", re.I),
        "value": re.compile(rf"(?:orders?\s+(?:growth\s+)?(?:\w+\s+){{0,4}}?(?:up|rose|grew|jump\w*|surg\w*|soar\w*|increas\w*|hits?|of|fell|fall|drop\w*|declin\w*|shr[ai]nk\w*)\s+(?:over\s+|by\s+)?{_NUM})"
                            rf"|(?:(?:up|rose|grew|jump\w*|surg\w*|soar\w*|increas\w*)\s+(?:over\s+|by\s+)?{_NUM})", re.I),
        "down": re.compile(r"\b(fell|fall|drop|declin|shrank|down)\w*", re.I),
    },
    "china_chip_exports_yoy": {
        "queries": ["China semiconductor exports percent", "China chip exports surge"],
        "must": re.compile(r"(chip|semiconductor|integrated circuit)s?\s+exports?", re.I),
        "value": re.compile(rf"exports?\s+(?:\w+\s+){{0,3}}?(?:up|rose|grew|jump\w*|surg\w*|soar\w*|increas\w*|fell|drop\w*|declin\w*)\s+(?:by\s+)?{_NUM}", re.I),
        "down": re.compile(r"\b(fell|drop|declin)\w*", re.I),
    },
}
_CONTRACT = re.compile(r"(DRAM|NAND|HBM|memory)[^.]{0,60}?contract prices?[^.]{0,40}?(?:rise|grow|increase|up|climb|fall|drop|decline)\w*"
                       r"[^.]{0,20}?(?:only\s+)?(-?[\d.]+)(?:\s*[-–~]\s*([\d.]+))?\s*%[^.]{0,20}?(?:in\s+)?(?:([1-4])Q(\d{2})|Q([1-4]))", re.I)


def _month(text: str, published: str | None) -> str | None:
    rng = re.search(rf"\b{_MON}\s*[-–]\s*{_MON}", text, re.I)     # "Jan-Aug": a year-to-date figure through Aug
    m = re.search(rf"\b{_MON}\b", text, re.I)
    if not (rng or m):
        return None
    mon = MONTHS[(rng.group(2) if rng else m.group(1)).lower()[:3]]
    pub = date.fromisoformat((published or date.today().isoformat())[:10])
    year = pub.year if mon <= pub.month else pub.year - 1
    return f"{year}-{mon:02d}"


def extract(name: str, title: str, published: str | None) -> tuple[str, float] | None:
    spec = INDICATORS[name]
    if not spec["must"].search(title):
        return None
    m = spec["value"].search(title)
    if not m:
        return None
    v = float(next(g for g in m.groups() if g))
    if spec["down"].search(title) and v > 0:
        v = -v
    period = _month(title, published)
    return (period, v) if period else None


def contract(title: str, published: str | None) -> tuple[str, str, float] | None:
    m = _CONTRACT.search(title)
    if not m:
        return None
    lo, hi = float(m.group(2)), float(m.group(3) or m.group(2))
    falling = re.search(r"fall|drop|decline", m.group(0), re.I)
    mid = (lo + hi) / 2 * (-1 if falling else 1)
    pub = date.fromisoformat((published or date.today().isoformat())[:10])
    q = m.group(4) or m.group(6)
    year = 2000 + int(m.group(5)) if m.group(5) else pub.year
    kind = "dram" if m.group(1).upper() in ("DRAM", "HBM", "MEMORY") else "nand"
    return kind, f"{year}Q{q}", mid


def _recent(it: dict) -> bool:
    try:
        return (date.today() - date.fromisoformat((it.get("published") or "")[:10])).days <= MAX_AGE_DAYS
    except ValueError:
        return False


def run(con, backfill: bool = False) -> dict:
    con.execute("DELETE FROM observations WHERE source='wires'")    # rebuilt each run from recent releases
    found = {}
    for name, spec in INDICATORS.items():
        for q in spec["queries"]:
            try:
                items = parse_feed(http.get(URL.format(q=q.replace(" ", "+"))).text)
            except Exception as e:
                log.info("wires %s: %s", q, e)
                continue
            for it in filter(_recent, items):
                x = extract(name, it["title"], it.get("published"))
                if x:
                    found.setdefault((name, x[0]), (x[1], it["url"], it["title"]))
    for q in ("TrendForce DRAM contract prices", "TrendForce NAND contract prices"):
        try:
            items = parse_feed(http.get(URL.format(q=q.replace(" ", "+"))).text)
        except Exception as e:
            log.info("wires %s: %s", q, e)
            continue
        for it in filter(_recent, items):
            c = contract(it["title"], it.get("published"))
            if c:
                found.setdefault((f"{c[0]}_contract_qoq", c[1]), (c[2], it["url"], it["title"]))
    for (series, period), (v, url, title) in found.items():
        db.upsert(con, "observations", {"source": "wires", "series": series, "period": period, "value": v,
                                        "unit": "% change", "meta": {"headline": title}, "url": url,
                                        "fetched_at": db.now_iso()})
    return {"source": "wires", "readings": len(found)}
