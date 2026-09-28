"""BOM ingestion. Accepts any CSV with a part-number column; other columns are
matched by common header names (case/spacing-insensitive)."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from . import config, db
from .parts.base import normalize_mpn
from .segments import SEGMENTS

ALIASES = {
    "mpn": ["mpn", "partnumber", "part", "manufacturerpartnumber", "mfrpartnumber", "mfrpn", "mfgpn", "pn"],
    "manufacturer": ["manufacturer", "mfr", "mfg", "vendor", "brand"],
    "description": ["description", "desc", "partdescription"],
    "segment": ["segment", "commodity", "category"],
    "annual_qty": ["annualqty", "annualquantity", "annualusage", "eau", "qty", "quantity", "volume"],
    "target_price": ["targetprice", "price", "unitprice", "currentprice", "cost", "unitcost"],
    "supplier": ["supplier", "distributor", "source"],
}


def _key(h: str) -> str:
    return "".join(ch for ch in h.lower() if ch.isalnum())


def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = {_key(h): h for h in reader.fieldnames or []}
    colmap = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in fields:
                colmap[field] = fields[n]
                break
    if "mpn" not in colmap:
        raise ValueError(f"No part-number column found. Headers: {reader.fieldnames}")
    out = []
    for row in reader:
        mpn = normalize_mpn(row.get(colmap["mpn"], ""))
        if not mpn:
            continue
        rec = {"mpn": mpn}
        for field, col in colmap.items():
            if field == "mpn":
                continue
            v = (row.get(col) or "").strip()
            if field in ("annual_qty", "target_price"):
                try:
                    v = float(v.replace(",", "").replace("$", "")) if v else None
                except ValueError:
                    v = None
            if field == "segment" and v and v.lower() not in SEGMENTS:
                v = None  # segment is inferred from the part category later
            rec[field] = v or None
        out.append(rec)
    return out


def ingest(con, path: Path, customer: str | None = None, replace: bool = True) -> int:
    customer = customer or config.CUSTOMER_NAME
    rows = parse_csv(Path(path).read_text(encoding="utf-8-sig"))
    if replace:
        con.execute("DELETE FROM bom WHERE customer=?", (customer,))
    for r in rows:
        db.upsert(con, "bom", {"customer": customer, "manufacturer": None, "description": None, "segment": None,
                               "annual_qty": None, "target_price": None, "supplier": None, **r})
    return len(rows)


def load(con, customer: str | None = None) -> list[dict]:
    return db.rows(con, "SELECT * FROM bom WHERE customer=? ORDER BY mpn", (customer or config.CUSTOMER_NAME,))
