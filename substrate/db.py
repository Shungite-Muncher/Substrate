"""The accumulator.

SQLite is the working store; the durable copy is a set of sorted JSONL files in
data/warehouse/ that are committed to git. Text files diff and compress well, so
running the pipeline twice a day for years doesn't bloat the repo the way a
committed binary database would, and every change to the record is auditable.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = {
    "observations": """
        source TEXT, series TEXT, period TEXT, value REAL, unit TEXT, meta TEXT, url TEXT, fetched_at TEXT,
        PRIMARY KEY (source, series, period)""",
    "documents": """
        url TEXT PRIMARY KEY, source TEXT, published TEXT, title TEXT, summary TEXT,
        segments TEXT, tags TEXT, supply_tone REAL, price_tone REAL, fetched_at TEXT""",
    "part_snapshots": """
        mpn TEXT, provider TEXT, day TEXT, manufacturer TEXT, description TEXT, category TEXT,
        total_avail INTEGER, lead_days REAL, price_1k REAL, currency TEXT, lifecycle TEXT,
        sellers TEXT, url TEXT, PRIMARY KEY (mpn, provider, day)""",
    "bom": """
        customer TEXT, mpn TEXT, manufacturer TEXT, description TEXT, segment TEXT,
        annual_qty REAL, target_price REAL, supplier TEXT, PRIMARY KEY (customer, mpn)""",
    "scores": """
        as_of TEXT, scope TEXT, key TEXT, supply_risk REAL, price_trend REAL, confidence REAL, detail TEXT,
        PRIMARY KEY (as_of, scope, key)""",
    "briefings": """
        id TEXT PRIMARY KEY, created_at TEXT, edition TEXT, headline TEXT, body_md TEXT, model TEXT, sources TEXT""",
    "decisions": """
        created_at TEXT, mpn TEXT, action TEXT, rationale TEXT, snapshot TEXT, PRIMARY KEY (created_at, mpn)""",
    "api_usage": """
        provider TEXT, month TEXT, calls INTEGER, parts INTEGER, PRIMARY KEY (provider, month)""",
}

PKS = {
    "observations": ("source", "series", "period"),
    "documents": ("url",),
    "part_snapshots": ("mpn", "provider", "day"),
    "bom": ("customer", "mpn"),
    "scores": ("as_of", "scope", "key"),
    "briefings": ("id",),
    "decisions": ("created_at", "mpn"),
    "api_usage": ("provider", "month"),
}

# Documents older than this are dropped from the warehouse (they no longer move scores).
DOCUMENT_RETENTION_DAYS = 120


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    for name, cols in SCHEMA.items():
        con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({cols})")
    if fresh:
        load_warehouse(con)
    return con


@contextmanager
def open_db(path=None):
    con = connect(path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def upsert(con: sqlite3.Connection, table: str, row: dict) -> None:
    cols = list(row)
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in PKS[table])
    conflict = f"ON CONFLICT({','.join(PKS[table])}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING")
    con.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) {conflict}",
                [_enc(row[c]) for c in cols])


def _enc(v):
    return json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v


def rows(con, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


# ---- warehouse (JSONL) --------------------------------------------------------

def load_warehouse(con: sqlite3.Connection) -> int:
    n = 0
    for table in SCHEMA:
        f = config.WAREHOUSE_DIR / f"{table}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                upsert(con, table, json.loads(line))
                n += 1
    con.commit()
    return n


def dump_warehouse(con: sqlite3.Connection) -> None:
    config.WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    latest_as_of = con.execute("SELECT MAX(as_of) FROM scores").fetchone()[0]
    for table in SCHEMA:
        order = ",".join(PKS[table])
        out = []
        for r in rows(con, f"SELECT * FROM {table} ORDER BY {order}"):
            if table == "scores" and r["as_of"] != latest_as_of:
                r["detail"] = None  # keep full evidence only for the latest run; history keeps the numbers
            if table == "documents" and r.get("published") and _age_days(r["published"]) > DOCUMENT_RETENTION_DAYS:
                continue
            out.append(json.dumps(r, sort_keys=True, ensure_ascii=False))
        (config.WAREHOUSE_DIR / f"{table}.jsonl").write_text("\n".join(out) + ("\n" if out else ""),
                                                              encoding="utf-8", newline="\n")


def _age_days(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except ValueError:
        return 0.0


# ---- API budget bookkeeping -----------------------------------------------------

def usage(con, provider: str) -> dict:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    r = con.execute("SELECT calls, parts FROM api_usage WHERE provider=? AND month=?", (provider, month)).fetchone()
    return {"month": month, "calls": r["calls"] if r else 0, "parts": r["parts"] if r else 0}


def record_usage(con, provider: str, calls: int = 1, parts: int = 0) -> None:
    u = usage(con, provider)
    upsert(con, "api_usage", {"provider": provider, "month": u["month"],
                              "calls": u["calls"] + calls, "parts": u["parts"] + parts})
