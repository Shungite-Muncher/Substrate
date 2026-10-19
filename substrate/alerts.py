"""Alerts: the things a buyer should hear about between briefings.

  * a BOM part's buy-timing call changes (e.g. Hold course -> Buy ahead)
  * a market or segment score jumps sharply between runs
  * a new U.S. export-control / tariff action touching chips
  * a strong earthquake near a fab cluster

State (last calls, alerts already sent) lives in data/state/alerts.json so each alert
fires once, even across the twice-daily cloud runs.
"""
from __future__ import annotations

import json

from . import config, db, scoring, signals

STATE_PATH = config.DATA_DIR / "state" / "alerts.json"
SCORE_JUMP = 8.0          # points on the 0-100 scale between consecutive runs
QUAKE_MIN_TONE = 0.3      # ~M6 within range of a fab cluster
LOOKBACK_DAYS = 14


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"calls": {}, "seen": []}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["seen"] = state["seen"][-500:]
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8", newline="\n")


def build(con, results: dict, outlook: dict | None) -> list[dict]:
    state = _load()
    first_run = not state["calls"]
    alerts: list[dict] = []

    # 1. buy-timing call changes
    calls = {m: p["action"] for m, p in ((outlook or {}).get("parts") or {}).items()}
    for mpn, action in calls.items():
        before = state["calls"].get(mpn)
        if before and before != action and not first_run:
            p = outlook["parts"][mpn]
            alerts.append({"kind": "call change", "severity": "high" if action.startswith("Buy") else "medium",
                           "title": f"{mpn}: {before} → {action}", "detail": p["why"], "url": None, "mpn": mpn})
    state["calls"] = calls

    # 2. score jumps vs the previous run
    prev = scoring.previous(con, results["as_of"])
    checks = [("market", "all", "Market", results["market"])] + \
             [("segment", k, v["label"], v) for k, v in results["segments"].items()]
    for scope, key, label, cur in checks:
        p = prev.get((scope, key))
        if not p:
            continue
        for field, name in (("supply_risk", "supply risk"), ("price_trend", "price trend")):
            d = cur[field] - p[field]
            if abs(d) >= SCORE_JUMP:
                top = cur["signals"][0]["evidence"] if cur.get("signals") else ""
                alerts.append({"kind": "score jump", "severity": "medium",
                               "title": f"{label} {name} {'up' if d > 0 else 'down'} {abs(d):.0f} pts to {cur[field]:.0f}",
                               "detail": top, "url": None})

    # 3/4. new policy actions and quakes near fabs (each alerted once)
    seen = set(state["seen"])
    for d in db.rows(con, "SELECT * FROM documents WHERE source IN ('fedreg','usgs') ORDER BY published DESC"):
        if d["url"] in seen or signals._iso_age(d["published"]) > LOOKBACK_DAYS:
            continue
        if d["source"] == "usgs" and (d["supply_tone"] or 0) < QUAKE_MIN_TONE:
            continue
        seen.add(d["url"])
        state["seen"].append(d["url"])
        if first_run:
            continue   # don't flood the very first delivery with history
        alerts.append({"kind": "policy" if d["source"] == "fedreg" else "earthquake",
                       "severity": "high" if (d["supply_tone"] or 0) >= 0.9 else "medium",
                       "title": d["title"][:140], "detail": (d["summary"] or "")[:280], "url": d["url"]})
    _save(state)
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(alerts, key=lambda a: order.get(a["severity"], 3))
