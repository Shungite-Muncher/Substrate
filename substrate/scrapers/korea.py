"""South Korea's chip exports from the Korea Customs Service flash releases (first 10 and
20 days of each month, then the full month), which are the earliest hard data on global chip
demand each month. Customs publishes them to the wires rather than a free API, so we read
the figures out of wire headlines and ledes, keeping only figures the parser can pin to a
specific month and window."""
from __future__ import annotations

import logging
import re
from datetime import date

from .. import db, http
from .news import parse_feed

log = logging.getLogger(__name__)

QUERIES = ["Korea exports first 10 days chips", "Korea exports first 20 days chips", "Korea exports 1-10 chip",
           "Korea monthly exports semiconductors record"]
URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_UP = r"up|rose|rise|rises|surged?|surges|jump(?:ed|s)?|soar(?:ed|s)?|climb(?:ed|s)?|grew|gain(?:ed|s)?|increased?|spike[ds]?"
_DOWN_W = r"down|fell|falls?|drop(?:ped|s)?|declined?|decreased?|slid|plunged?|slump(?:ed|s)?"
# "Exports up 83% ...", "exports jump 45.3% ...", "Chip shipments jump 206% ...", "270% chip surge"
_TOTAL = re.compile(rf"\bexports?\s+(?:\w+\s+){{0,2}}?({_UP}|{_DOWN_W})\s+(?:by\s+)?([\d.]+)\s*(?:%|pct|percent)", re.I)
_CHIP = re.compile(rf"(?:chip|semiconductor)s?\s+(?:exports?|shipments?)\s+(?:\w+\s+){{0,2}}?({_UP}|{_DOWN_W})\s+(?:by\s+)?([\d.]+)\s*(?:%|pct|percent)"
                   rf"|([\d.]+)\s*(?:%|pct|percent)\s+(?:chip|semiconductor)s?\s+(?:surge|jump|rise|gain|growth)", re.I)
_DOUBLE = re.compile(r"\bexports?\s+(?:more\s+than\s+)?doubled?\b", re.I)
_DAYS = re.compile(r"(?:first|1st)\s+(10|20)\s+days\s+of\s+([A-Za-z]{3})"           # first 10 days of Oct.
                   r"|\b([A-Za-z]{3})[a-z]*\.?\s+1\s*[-–]\s*(10|20)\b"                 # Oct. 1-10
                   r"|early[- ]([A-Za-z]{3})", re.I)                                    # early-June (= first 10 days)
_MONTH_ONLY = re.compile(r"\bin\s+([A-Za-z]{3})[a-z]*\b", re.I)


def _window(text: str):
    w = _DAYS.search(text)
    if w:
        if w.group(5):
            return "d10", MONTHS.get(w.group(5)[:3].lower())
        days = w.group(1) or w.group(4)
        return f"d{days}", MONTHS.get((w.group(2) or w.group(3))[:3].lower())
    mo = _MONTH_ONLY.search(text)
    return "month", (MONTHS.get(mo.group(1)[:3].lower()) if mo else None)


def extract(text: str, published: str | None) -> list[dict]:
    """All export readings in a headline: total exports and, when stated, chip exports."""
    window, mon = _window(text)
    if not mon:
        return []
    pub = date.fromisoformat((published or date.today().isoformat())[:10])
    year = pub.year if mon <= pub.month else pub.year - 1     # a January story about December
    base = {"period": f"{year}-{mon:02d}", "window": window}
    out = []
    t = _TOTAL.search(text)
    if t:
        sign = -1 if re.fullmatch(_DOWN_W, t.group(1), re.I) else 1
        out.append({**base, "series": "exports_yoy", "yoy_pct": sign * float(t.group(2))})
    elif _DOUBLE.search(text):
        out.append({**base, "series": "exports_yoy", "yoy_pct": 100.0})
    c = _CHIP.search(text)
    if c:
        if c.group(3):
            out.append({**base, "series": "chip_exports_yoy", "yoy_pct": float(c.group(3))})
        else:
            sign = -1 if re.fullmatch(_DOWN_W, c.group(1), re.I) else 1
            out.append({**base, "series": "chip_exports_yoy", "yoy_pct": sign * float(c.group(2))})
    return out


def run(con, backfill: bool = False) -> dict:
    found = {}
    for q in QUERIES:
        try:
            items = parse_feed(http.get(URL.format(q=q.replace(" ", "+"))).text)
        except Exception as e:
            log.info("korea feed %s: %s", q, e)
            continue
        for it in items:
            for x in extract(it["title"], it.get("published")):
                # several outlets report the same release; keep the first per series/period/window
                found.setdefault((x["series"], x["period"], x["window"]), (x, it["url"], it["title"]))
    for (series, period, window), (x, url, title) in found.items():
        db.upsert(con, "observations", {"source": "korea", "series": f"{series}_{window}", "period": period,
                                        "value": x["yoy_pct"], "unit": "% YoY", "meta": {"via": "wire reports", "headline": title},
                                        "url": url, "fetched_at": db.now_iso()})
    return {"source": "korea", "readings": len(found)}
