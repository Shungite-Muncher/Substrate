"""Base US duty rates per tariff code, from the USITC Harmonized Tariff Schedule API.

Most chips (HTS 8541 discretes, 8542 ICs) are duty-free at the base rate; what moves
landed cost is the additional duties stacked on top for some origins (e.g. Section 301 on
China). The HTS API doesn't map those per code, so the per-part estimate in signals.py
combines: the base rate from here, the distributor's own posted surcharge when there is one,
and otherwise the surcharge posted on parts with the same 4-digit heading and origin.
"""
from __future__ import annotations

import logging
import re

from .. import db, http

log = logging.getLogger(__name__)
API = "https://hts.usitc.gov/reststop/search"


def heading8(hts: str) -> str | None:
    d = re.sub(r"\D", "", hts or "")
    return f"{d[:4]}.{d[4:6]}.{d[6:8]}" if len(d) >= 8 else None


def base_rate(rows: list[dict], h8: str) -> str | None:
    for x in rows:
        if x.get("htsno") == h8 and x.get("general"):
            return x["general"]
    return None


def pct(rate: str | None) -> float | None:
    if rate is None:
        return None
    if rate.strip().lower() == "free":
        return 0.0
    m = re.search(r"([\d.]+)\s*%", rate)
    return float(m.group(1)) if m else None


def run(con, backfill: bool = False) -> dict:
    codes = {heading8(r["us_hts"]) for r in db.rows(con, "SELECT DISTINCT us_hts FROM part_snapshots WHERE us_hts IS NOT NULL")}
    codes.discard(None)
    have = {r["series"] for r in db.rows(con, "SELECT series FROM observations WHERE source='usitc'")}
    n = 0
    for h8 in sorted(codes):
        if h8 in have and not backfill:
            continue
        try:
            rate = base_rate(http.get(API, params={"keyword": h8}).json(), h8)
        except Exception as e:
            log.info("usitc %s: %s", h8, e)
            continue
        db.upsert(con, "observations", {"source": "usitc", "series": h8, "period": "current", "value": pct(rate),
                                        "unit": "% ad valorem (base, column 1 general)", "meta": {"rate_text": rate},
                                        "url": f"https://hts.usitc.gov/search?query={h8}", "fetched_at": db.now_iso()})
        n += 1
    return {"source": "usitc", "codes": len(codes), "fetched": n}
