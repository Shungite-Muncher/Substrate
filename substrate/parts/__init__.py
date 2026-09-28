"""Part-level supply data: the Octopart connection port plus free fallbacks.

Every lookup lands in part_snapshots (one row per part, provider and day), so the
accumulator builds its own lead-time and price history over time. That history
is what the part scores and the lead-time backtest run on.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from .. import config, db
from .base import PartRecord, normalize_mpn
from .digikey import DigiKeyProvider
from .mouser import MouserProvider
from .nexar import NexarProvider

log = logging.getLogger(__name__)
PROVIDERS = [NexarProvider(), MouserProvider(), DigiKeyProvider()]


def configured() -> list[str]:
    return [p.name for p in PROVIDERS if _has_creds(p)]


def _has_creds(p) -> bool:
    return {"octopart": bool(config.NEXAR_CLIENT_ID and config.NEXAR_CLIENT_SECRET),
            "mouser": bool(config.MOUSER_API_KEY),
            "digikey": bool(config.DIGIKEY_CLIENT_ID and config.DIGIKEY_CLIENT_SECRET)}[p.name]


def cached(con, mpn: str, max_age_days: int | None = None) -> dict | None:
    max_age_days = config.PART_REFRESH_DAYS if max_age_days is None else max_age_days
    since = (date.today() - timedelta(days=max_age_days)).isoformat()
    r = con.execute("SELECT * FROM part_snapshots WHERE mpn=? AND day>=? ORDER BY day DESC, "
                    "CASE provider WHEN 'octopart' THEN 0 ELSE 1 END LIMIT 1", (mpn, since)).fetchone()
    return _decode(dict(r)) if r else None


def history(con, mpn: str) -> list[dict]:
    return [_decode(r) for r in db.rows(con, "SELECT * FROM part_snapshots WHERE mpn=? ORDER BY day", (mpn,))]


def _decode(r: dict) -> dict:
    if isinstance(r.get("sellers"), str):
        r["sellers"] = json.loads(r["sellers"])
    return r


def lookup(con, mpn: str, *, force: bool = False) -> dict | None:
    mpn = normalize_mpn(mpn)
    if not force:
        hit = cached(con, mpn)
        if hit:
            return hit
    for p in PROVIDERS:
        if not p.available(con):
            continue
        try:
            rec: PartRecord | None = p.lookup(con, mpn)
        except Exception as e:
            log.warning("%s lookup %s failed: %s", p.name, mpn, e)
            continue
        if rec:
            row = rec.to_row(date.today().isoformat())
            row["mpn"] = mpn  # key on the BOM's spelling
            db.upsert(con, "part_snapshots", row)
            return _decode(dict(row))
    return cached(con, mpn, max_age_days=3650)  # fall back to the last thing we ever saw


def refresh(con, mpns: list[str]) -> dict:
    fresh = stale = missing = 0
    for m in mpns:
        before = cached(con, normalize_mpn(m))
        rec = lookup(con, m)
        if rec is None:
            missing += 1
        elif before is None:
            fresh += 1
        else:
            stale += 1
    return {"parts": len(mpns), "fetched": fresh, "from_cache": stale, "no_data": missing,
            "providers": configured(), "octopart_usage": db.usage(con, "octopart")}
