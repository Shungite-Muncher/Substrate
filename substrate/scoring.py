"""The scoring engine.

  Supply risk  0-100   50 = neutral, higher = tighter supply / more risk
  Price trend  0-100   50 = flat, higher = prices rising
  Confidence   0-100   how much independent sources agree, how many showed up,
                       and how fresh they are

Each score = 50 + 50 * (weighted mean of signal norms). Weights are named and
exported with every score, so nothing is a black box.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from . import bom as bom_mod
from . import config, db, parts, signals
from .segments import SEGMENTS, segment_for_category

EXPECTED_SOURCES = {
    "market": {"supply": {"FRED", "TSMC", "SIA", "Trade press", "Earnings releases"},
               "price": {"FRED", "SIA", "Trade press", "Earnings releases"}},
    "segment": {"supply": {"FRED", "TSMC", "SIA", "SEC", "Trade press", "Earnings releases"},
                "price": {"FRED", "SIA", "SEC", "Trade press", "Earnings releases"}},
    "part": {"supply": {"FRED", "TSMC", "SIA", "SEC", "Trade press", "Octopart", "Part history"},
             "price": {"FRED", "SIA", "SEC", "Trade press", "Part history"}},
}
PART_MACRO_DISCOUNT = 0.5   # segment-level evidence counts half as much as the part's own data


def component(sigs: list[signals.Signal], comp: str, scope: str) -> dict:
    xs = [s for s in sigs if s.component == comp and s.weight > 0]
    if not xs:
        return {"score": 50.0, "confidence": 0.0, "agreement": 0.0, "coverage": 0.0, "freshness": 0.0}
    wsum = sum(s.weight for s in xs)
    net = sum(s.weight * s.norm for s in xs)
    score = 50 + 50 * net / wsum
    mag = sum(s.weight * abs(s.norm) for s in xs)
    agreement = abs(net) / mag if mag > 1e-9 else 0.5
    present = {("Octopart" if s.source in ("Octopart", "Part data") else s.source) for s in xs}
    expected = EXPECTED_SOURCES[scope][comp]
    coverage = len(present & expected) / len(expected)
    freshness = sum(s.weight * math.exp(-s.age_days / signals.HALF_LIFE_DAYS.get(s.source, 30)) for s in xs) / wsum
    conf = 100 * (0.45 * agreement + 0.35 * coverage + 0.20 * freshness)
    return {"score": round(score, 1), "confidence": round(conf, 1), "agreement": round(agreement, 2),
            "coverage": round(coverage, 2), "freshness": round(freshness, 2)}


def supply_label(s: float) -> str:
    return ("Severe" if s >= 75 else "Elevated" if s >= 62 else "Rising" if s >= 54 else
            "Neutral" if s > 46 else "Easing" if s > 38 else "Low")


def price_label(s: float) -> str:
    return ("Rising fast" if s >= 72 else "Rising" if s >= 60 else "Firming" if s >= 53 else
            "Flat" if s > 47 else "Softening" if s > 40 else "Falling")


def confidence_label(c: float) -> str:
    return "High" if c >= 70 else "Medium" if c >= 50 else "Low"


def summarize(sigs: list[signals.Signal], scope: str) -> dict:
    sup, pri = component(sigs, "supply", scope), component(sigs, "price", scope)
    conf = round((sup["confidence"] + pri["confidence"]) / 2, 1)
    drivers = sorted(sigs, key=lambda s: abs(s.weight * s.norm), reverse=True)
    return {
        "supply_risk": sup["score"], "price_trend": pri["score"], "confidence": conf,
        "labels": {"supply": supply_label(sup["score"]), "price": price_label(pri["score"]),
                   "confidence": confidence_label(conf)},
        "components": {"supply": sup, "price": pri},
        "signals": [s.to_dict() for s in drivers],
    }


def part_segment(snap: dict | None, bom_row: dict | None) -> str | None:
    seg = (bom_row or {}).get("segment")
    if seg in SEGMENTS:
        return seg
    return segment_for_category((snap or {}).get("category")) or segment_for_category((bom_row or {}).get("description"))


def score_part(con, mpn: str, bom_row: dict | None, seg_cache: dict, calibration: dict | None) -> dict:
    snap = parts.cached(con, mpn, max_age_days=3650)
    hist = parts.history(con, mpn)
    seg = part_segment(snap, bom_row)
    if seg and seg not in seg_cache:
        seg_cache[seg] = signals.segment_signals(con, seg, calibration)
    base = seg_cache.get(seg) or signals.market_signals(con, calibration)
    macro = [signals.Signal(**{**s.to_dict(), "weight": s.weight * PART_MACRO_DISCOUNT}) for s in base]
    sigs = macro + signals.part_signals(snap, hist, bom_row)
    out = summarize(sigs, "part")
    out.update({"mpn": mpn, "segment": seg, "snapshot": snap, "bom": bom_row,
                "history": [{k: h.get(k) for k in ("day", "provider", "lead_days", "total_avail", "price_1k")} for h in hist][-60:]})
    if snap and bom_row and bom_row.get("target_price") and snap.get("price_1k"):
        out["price_vs_target_pct"] = round((snap["price_1k"] / bom_row["target_price"] - 1) * 100, 1)
    return out


def run(con, calibration: dict | None = None, as_of: str | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    results = {"as_of": as_of, "market": None, "segments": {}, "parts": {}}
    market = summarize(signals.market_signals(con, calibration), "market")
    results["market"] = market
    _store(con, as_of, "market", "all", market)
    seg_cache: dict = {}
    for seg in SEGMENTS:
        seg_cache[seg] = signals.segment_signals(con, seg, calibration)
        s = summarize(seg_cache[seg], "segment")
        s["label"] = SEGMENTS[seg]["label"]
        results["segments"][seg] = s
        _store(con, as_of, "segment", seg, s)
    for row in bom_mod.load(con):
        p = score_part(con, row["mpn"], row, seg_cache, calibration)
        results["parts"][row["mpn"]] = p
        _store(con, as_of, "part", row["mpn"], p)
    return results


def _store(con, as_of: str, scope: str, key: str, s: dict) -> None:
    db.upsert(con, "scores", {"as_of": as_of, "scope": scope, "key": key, "supply_risk": s["supply_risk"],
                              "price_trend": s["price_trend"], "confidence": s["confidence"],
                              "detail": json.dumps({"signals": s["signals"], "components": s["components"]})})


def previous(con, as_of: str) -> dict:
    """Scores from the run before `as_of`, keyed by (scope, key)."""
    r = con.execute("SELECT MAX(as_of) FROM scores WHERE as_of < ?", (as_of,)).fetchone()[0]
    if not r:
        return {}
    return {(x["scope"], x["key"]): x for x in db.rows(con, "SELECT * FROM scores WHERE as_of=?", (r,))}
