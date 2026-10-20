"""Sources that need a free key. Each switches itself on when its key is set and is
skipped quietly otherwise.

  * Census international trade (CENSUS_API_KEY): monthly US imports of chips (HTS 8541
    discretes, 8542 ICs) by country of origin, showing tariffs moving supply in real time.
  * trade.gov Consolidated Screening List (TRADE_GOV_API_KEY): screens each BOM manufacturer
    against US restricted-party lists (Entity List, SDN, and others).
"""
from __future__ import annotations

import logging
from datetime import date

from .. import config, db, http

log = logging.getLogger(__name__)
CENSUS = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
CSL = "https://data.trade.gov/consolidated_screening_list/v1/search"


def _months_back(n: int) -> list[str]:
    t = date.today()
    y, m = t.year, t.month - 2          # Census publishes ~5-6 weeks after month end
    out = []
    for _ in range(n):
        if m <= 0:
            y, m = y - 1, m + 12
        out.append(f"{y}-{m:02d}")
        m -= 1
    return out


def census(con, backfill: bool) -> dict:
    if not config.CENSUS_API_KEY:
        return {"skipped": "CENSUS_API_KEY not set"}
    n = 0
    for month in _months_back(24 if backfill else 4):
        for hs in ("8541", "8542"):
            try:
                rows = http.get(CENSUS, params={"get": "CTY_CODE,CTY_NAME,GEN_VAL_MO", "COMM_LVL": "HS4",
                                                "I_COMMODITY": hs, "time": month, "key": config.CENSUS_API_KEY}).json()
            except Exception as e:      # months not yet published return an error/empty body
                log.info("census %s %s: %s", hs, month, e)
                continue
            head, body = rows[0], rows[1:]
            ix = {k: head.index(k) for k in ("CTY_CODE", "CTY_NAME", "GEN_VAL_MO")}
            for r in body:
                code = r[ix["CTY_CODE"]]
                if not code.isdigit() or code.startswith("0") and code != "0000":
                    continue                # skip regional aggregates
                db.upsert(con, "observations", {"source": "census_trade", "series": f"{hs}:{r[ix['CTY_NAME']].title()}",
                                                "period": month, "value": float(r[ix["GEN_VAL_MO"]] or 0), "unit": "US$ imports",
                                                "meta": {"cty_code": code}, "url": CENSUS, "fetched_at": db.now_iso()})
                n += 1
    return {"rows": n}


def screening(con) -> dict:
    if not config.TRADE_GOV_API_KEY:
        return {"skipped": "TRADE_GOV_API_KEY not set"}
    names = sorted({r["manufacturer"] for r in db.rows(con, "SELECT DISTINCT manufacturer FROM bom WHERE manufacturer IS NOT NULL")})
    hits = 0
    for name in names:
        try:
            j = http.get(CSL, params={"name": name, "fuzzy_name": "false"},
                         headers={"subscription-key": config.TRADE_GOV_API_KEY}).json()
        except Exception as e:
            log.info("csl %s: %s", name, e)
            continue
        results = j.get("results") or []
        hits += bool(results)
        db.upsert(con, "observations", {"source": "screening", "series": name, "period": date.today().isoformat(),
                                        "value": float(len(results)), "unit": "restricted-party matches",
                                        "meta": {"lists": sorted({x.get("source", "") for x in results})[:5]},
                                        "url": "https://www.trade.gov/consolidated-screening-list", "fetched_at": db.now_iso()})
    return {"manufacturers": len(names), "with_matches": hits}


def run(con, backfill: bool = False) -> dict:
    return {"source": "keyed", "census": census(con, backfill), "screening": screening(con)}
