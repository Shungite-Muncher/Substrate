"""Federal Reserve / BLS series via FRED's keyless CSV download.

CAPUTLG3344S  Capacity utilization, semiconductors & other electronic components (Fed G.17)
IPG3344S      Industrial production, same industry
PCU33443344   Producer price index, semiconductor & other electronic component mfg (BLS)
"""
from __future__ import annotations

import csv
import io

from .. import db, http

SERIES = {
    "CAPUTLG3344S": ("capacity_utilization", "% of capacity"),
    "IPG3344S": ("industrial_production", "index 2017=100"),
    "PCU33443344": ("ppi_semis", "index Dec-1984=100"),
    # Census M3. Semiconductor orders aren't published on their own (Census folds them into 34S),
    # so we track electronic components (34H: passives, connectors, boards) and electronics overall (34S).
    "A34HNO": ("components_new_orders", "US$ million"),
    "A34HUO": ("components_unfilled_orders", "US$ million"),
    "A34HVS": ("components_shipments", "US$ million"),
    "A34HTI": ("components_inventories", "US$ million"),
    "A34SUO": ("electronics_unfilled_orders", "US$ million"),
    "A34STI": ("electronics_inventories", "US$ million"),
    "A34SVS": ("electronics_shipments", "US$ million"),
    # Asian currencies per US$ (daily; stored as the month's latest value)
    "DEXTAUS": ("fx_twd", "TWD per USD"),
    "DEXJPUS": ("fx_jpy", "JPY per USD"),
    "DEXKOUS": ("fx_krw", "KRW per USD"),
}
URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"


def parse(text: str) -> list[tuple[str, float]]:
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 2 or not row[0][:1].isdigit():
            continue
        try:
            out.append((row[0][:7], float(row[1])))
        except ValueError:  # "." marks a missing value
            continue
    return out


def run(con, backfill: bool = False) -> dict:
    n = 0
    for sid, (name, unit) in SERIES.items():
        url = URL.format(sid=sid)
        points = parse(http.get(url).text)
        if not backfill:
            points = points[-90:]   # ~3 years monthly, ~3 months daily
        for period, value in points:
            db.upsert(con, "observations", {"source": "fred", "series": name, "period": period, "value": value,
                                            "unit": unit, "meta": {"fred_id": sid}, "url": url,
                                            "fetched_at": db.now_iso()})
            n += 1
    return {"source": "fred", "rows": n}
