"""Customer purchase-order / quote history import.

A customer's own history (part, date, unit price, quoted lead time) is the fastest way to
validate part-level predictions: instead of waiting 13+ weeks for Substrate's own snapshots,
the part models can be backtested the day the file lands. It's also the account-specific
record competitors can't replicate.

Rows are stored in part_snapshots under provider 'customer', a separate series from live
market data: live part cards, scores and lookups never read them, while parts_model builds
outcome pairs from each provider's series on its own.
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from . import db
from .parts.base import normalize_mpn, parse_lead_days, parse_money

PROVIDER = "customer"
ALIASES = {
    "mpn": ["mpn", "partnumber", "part", "manufacturerpartnumber", "mfrpartnumber", "mfrpn", "pn", "item", "itemnumber"],
    "date": ["date", "orderdate", "podate", "quotedate", "createddate", "transactiondate", "receiptdate"],
    "price": ["unitprice", "price", "unitcost", "cost", "netprice", "quotedprice", "poprice"],
    "qty": ["qty", "quantity", "orderqty", "orderedqty", "poqty"],
    "lead": ["leadtime", "quotedleadtime", "lead", "leaddays", "leadtimedays", "leadweeks", "leadtimeweeks", "quotedlead"],
    "supplier": ["supplier", "vendor", "distributor", "manufacturer", "mfr"],
    "type": ["type", "doctype", "documenttype", "source", "kind"],
}
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%d-%b-%y", "%Y/%m/%d", "%b %d %Y", "%d.%m.%Y")


def _key(h: str) -> str:
    return "".join(ch for ch in h.lower() if ch.isalnum())


def _date(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return None


def parse_csv(text: str) -> tuple[list[dict], dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = {_key(h): h for h in reader.fieldnames or []}
    col = {}
    for field, names in ALIASES.items():
        col[field] = next((fields[n] for n in names if n in fields), None)
    missing = [f for f in ("mpn", "date") if not col[f]]
    if missing or not (col["price"] or col["lead"]):
        raise ValueError(f"Need part number, date, and a price or lead-time column. Headers: {reader.fieldnames}")
    lead_in_weeks = bool(col["lead"]) and "week" in col["lead"].lower()
    rows, skipped = [], 0
    for r in reader:
        mpn, day = normalize_mpn(r.get(col["mpn"], "")), _date(r.get(col["date"], ""))
        price = parse_money(r.get(col["price"])) if col["price"] else None
        lead = None
        if col["lead"] and (r.get(col["lead"]) or "").strip():
            raw = r[col["lead"]].strip()
            lead = float(raw) * 7 if lead_in_weeks and re.fullmatch(r"[\d.]+", raw) else parse_lead_days(raw)
        if not mpn or not day or (price is None and lead is None):
            skipped += 1
            continue
        qty = parse_money(r.get(col["qty"])) if col["qty"] else None
        rows.append({"mpn": mpn, "day": day, "price": price, "lead": lead, "qty": qty or 1.0,
                     "supplier": (r.get(col["supplier"]) or "").strip() if col["supplier"] else "",
                     "type": (r.get(col["type"]) or "").strip() if col["type"] else ""})
    return rows, {"columns": {k: v for k, v in col.items() if v}, "skipped": skipped}


def ingest(con, path: Path, customer: str | None = None) -> dict:
    rows, info = parse_csv(Path(path).read_text(encoding="utf-8-sig"))
    by_day: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[(r["mpn"], r["day"])].append(r)
    for (mpn, day), rs in by_day.items():
        priced = [x for x in rs if x["price"] is not None]
        leads = [x["lead"] for x in rs if x["lead"] is not None]
        price = (sum(x["price"] * x["qty"] for x in priced) / sum(x["qty"] for x in priced)) if priced else None
        db.upsert(con, "part_snapshots", {
            "mpn": mpn, "provider": PROVIDER, "day": day, "manufacturer": None, "description": customer,
            "category": None, "total_avail": None, "lead_days": max(leads) if leads else None,
            "price_1k": round(price, 6) if price is not None else None, "price_1": None, "currency": "USD",
            "lifecycle": None, "on_order": None, "on_order_eta": None, "url": None,
            "sellers": [{"seller": x["supplier"] or "customer", "type": x["type"]} for x in rs][:10]})
    days = sorted(d for _, d in by_day)
    return {"rows": len(rows), "snapshots": len(by_day), "parts": len({m for m, _ in by_day}),
            "span": [days[0], days[-1]] if days else None, **info}


def summary(con) -> dict | None:
    r = con.execute("SELECT COUNT(*) n, COUNT(DISTINCT mpn) parts, MIN(day) a, MAX(day) b FROM part_snapshots "
                    "WHERE provider=?", (PROVIDER,)).fetchone()
    return None if not r["n"] else {"snapshots": r["n"], "parts": r["parts"], "span": [r["a"], r["b"]]}
