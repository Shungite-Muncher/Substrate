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

from datetime import date, timedelta

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


LEAD_EASE_DAYS = 7        # a week shorter counts as easing
LEAD_WORSE_DAYS = 14
ETA_SLIP_DAYS = 7
RELIEF_WINDOW_DAYS = 30


def _snapshots(con, mpn: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in db.rows(con, "SELECT * FROM part_snapshots WHERE mpn=? AND provider!='customer' ORDER BY day", (mpn,)):
        out.setdefault(r["provider"], []).append(r)
    return out


def part_changes(con, mpns: list[str]) -> list[dict]:
    """What moved between each part's two most recent snapshots from the same distributor."""
    out = []
    for mpn in mpns:
        for prov, seq in _snapshots(con, mpn).items():
            if len(seq) < 2:
                continue
            new, old = seq[-1], seq[-2]
            base = {"mpn": mpn, "provider": prov, "day": new["day"], "since": old["day"]}
            ln, lo = new["lead_days"], old["lead_days"]
            if ln and lo and lo - ln >= LEAD_EASE_DAYS:
                out.append({**base, "kind": "lead time easing", "direction": "easing",
                            "title": f"{mpn}: lead time {lo / 7:.0f} → {ln / 7:.0f} weeks", "detail": f"{prov}, since {old['day']}"})
            elif ln and lo and ln - lo >= LEAD_WORSE_DAYS:
                out.append({**base, "kind": "lead time lengthening", "direction": "worsening",
                            "title": f"{mpn}: lead time {lo / 7:.0f} → {ln / 7:.0f} weeks", "detail": f"{prov}, since {old['day']}"})
            sn, so = new["total_avail"], old["total_avail"]
            if so == 0 and (sn or 0) > 0:
                out.append({**base, "kind": "back in stock", "direction": "easing",
                            "title": f"{mpn}: back in stock ({sn:,} units)", "detail": f"{prov}; was 0 on {old['day']}"})
            on, oo = new["on_order"] or 0, old["on_order"] or 0
            if oo and on <= 0.5 * oo and (sn or 0) > (so or 0):
                out.append({**base, "kind": "backorder easing", "direction": "easing",
                            "title": f"{mpn}: PBO arriving ({oo:,} → {on:,} on order, stock {so or 0:,} → {sn or 0:,})",
                            "detail": f"{prov}, since {old['day']}"})
            en, eo = (new["on_order_eta"] or "")[:10], (old["on_order_eta"] or "")[:10]
            if en and eo and (date.fromisoformat(en) - date.fromisoformat(eo)).days >= ETA_SLIP_DAYS:
                out.append({**base, "kind": "PBO delayed", "direction": "worsening",
                            "title": f"{mpn}: next PBO arrival slipped {eo} → {en}", "detail": f"{prov}, {on:,} on order"})
            tn, to = new.get("tariff_pct"), old.get("tariff_pct")
            if (tn or 0) != (to or 0) and (tn is not None or to is not None):
                worse = (tn or 0) > (to or 0)
                out.append({**base, "kind": "tariff " + ("added" if worse else "reduced"), "direction": "worsening" if worse else "easing",
                            "title": f"{mpn}: tariff surcharge {to or 0:.0f}% → {tn or 0:.0f}%",
                            "detail": new.get("tariff_note") or "surcharge notice removed"})
    return out


def relief_due(con, mpns: list[str], annual: dict) -> list[dict]:
    """Forward-looking easing: out-of-stock or thin parts with on-order stock landing soon."""
    out, horizon = [], (date.today() + timedelta(days=RELIEF_WINDOW_DAYS)).isoformat()
    for mpn in mpns:
        snaps = _snapshots(con, mpn).get("mouser") or []
        if not snaps:
            continue
        s = snaps[-1]
        eta, stock, oo = (s["on_order_eta"] or "")[:10], s["total_avail"], s["on_order"]
        if not (eta and oo and eta <= horizon):
            continue
        cover = (stock / (annual[mpn] / 52)) if stock is not None and annual.get(mpn) else None
        if stock == 0 or (cover is not None and cover < 2):
            out.append({"mpn": mpn, "kind": "backorder relief due", "direction": "easing", "day": s["day"],
                        "title": f"{mpn}: {oo:,} units due {eta} (stock now {stock or 0:,})",
                        "detail": "Mouser on-order schedule"})
    return out


def watchlist(con) -> dict:
    """Everything that moved, for the dashboard's watch panel."""
    rows = db.rows(con, "SELECT mpn, annual_qty FROM bom")
    mpns, annual = [r["mpn"] for r in rows], {r["mpn"]: r["annual_qty"] for r in rows}
    changes = part_changes(con, mpns) + relief_due(con, mpns, annual)
    tariffs = []
    for mpn in mpns:
        r = con.execute("SELECT tariff_pct, tariff_note, country_of_origin FROM part_snapshots WHERE mpn=? AND provider!='customer' "
                        "AND country_of_origin IS NOT NULL ORDER BY day DESC LIMIT 1", (mpn,)).fetchone()
        if r and r["tariff_pct"]:
            tariffs.append({"mpn": mpn, "kind": "tariff exposure", "direction": "worsening", "title":
                            f"{mpn}: {r['tariff_pct']:.0f}% US tariff surcharge (origin {r['country_of_origin']})",
                            "detail": r["tariff_note"]})
    return {"easing": [c for c in changes if c["direction"] == "easing"],
            "worsening": [c for c in changes if c["direction"] == "worsening"] + tariffs}


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

    # part movements: lead time, stock, PBO arrivals and slips, tariff surcharges (each alerted once)
    bom = [r["mpn"] for r in db.rows(con, "SELECT mpn FROM bom")]
    seen_parts = set(state.setdefault("seen_parts", []))
    for c in part_changes(con, bom):
        key = f"{c['mpn']}|{c['kind']}|{c['day']}"
        if key in seen_parts:
            continue
        state["seen_parts"].append(key)
        if not first_run:
            alerts.append({"kind": c["kind"], "severity": "medium" if c["direction"] == "easing" else "high",
                           "title": c["title"], "detail": c["detail"], "url": None, "mpn": c["mpn"]})
    state["seen_parts"] = state["seen_parts"][-1000:]

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
