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
            points = points[-36:]
        for period, value in points:
            db.upsert(con, "observations", {"source": "fred", "series": name, "period": period, "value": value,
                                            "unit": unit, "meta": {"fred_id": sid}, "url": url,
                                            "fetched_at": db.now_iso()})
            n += 1
    return {"source": "fred", "rows": n}
