"""From market forecasts to decisions: segment outlooks, part lead-time projections
and buy-timing calls.

Layering, and how much each layer is validated:
  1. Market forecasts (engine.py): walk-forward tested since 2010.
  2. Segment tilt: shifts the market probability by how far the segment's current
     signals sit from the market's. Not separately validated (no free segment-level
     price history); labelled as a tilt.
  3. Part projections: lead time 13 weeks out from the segment supply outlook plus the
     part's own trend. A heuristic until part history accrues; every projection goes into
     the ledger and is scored when its 13 weeks are up.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from .. import db
from ..segments import SEGMENTS

TILT = 1.0              # log-odds shift per unit of (segment score - market score) / 50
LEAD_ELASTICITY = 0.30  # log lead-time change at full-confidence tightening (+1)
LEAD_HORIZON_DAYS = 91


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def tilt(p: float, score: float, ref: float) -> float:
    return _sig(_logit(p) + TILT * (score - ref) / 50)


def _week(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def build(con, scores: dict, forecasts: dict) -> dict:
    f = forecasts.get("forecasts", {})
    price, supply = f.get("ppi_3m"), f.get("cu_3m")
    if not price or not supply:
        return {}
    mkt = scores["market"]
    segs = {}
    for k, s in scores["segments"].items():
        ps = tilt(supply["prob_up"], s["supply_risk"], mkt["supply_risk"])
        pp = tilt(price["prob_up"], s["price_trend"], mkt["price_trend"])
        segs[k] = {"label": SEGMENTS[k]["label"], "p_supply_tighter": round(ps, 3), "p_price_up": round(pp, 3),
                   "supply_call": _call(ps, "Tightening", "Loosening"), "price_call": _call(pp, "Rising", "Falling"),
                   "basis": f"market forecast tilted by {SEGMENTS[k]['label'].lower()} signals today"}
    parts = {}
    for mpn, p in scores["parts"].items():
        seg = segs.get(p.get("segment") or "")
        ps0 = seg["p_supply_tighter"] if seg else supply["prob_up"]
        pp0 = seg["p_price_up"] if seg else price["prob_up"]
        ref = scores["segments"].get(p.get("segment") or "", mkt)
        ps = tilt(ps0, p["supply_risk"], ref["supply_risk"])
        pp = tilt(pp0, p["price_trend"], ref["price_trend"])
        snap = p.get("snapshot") or {}
        lead = snap.get("lead_days")
        proj = None
        if lead:
            s = 2 * ps - 1
            proj = {"now_weeks": round(lead / 7, 1), "in_13w_weeks": round(lead * math.exp(LEAD_ELASTICITY * s) / 7, 1),
                    "range_weeks": [round(lead * math.exp(LEAD_ELASTICITY * s - 0.25) / 7, 1),
                                    round(lead * math.exp(LEAD_ELASTICITY * s + 0.25) / 7, 1)]}
            _record_part(con, mpn, ps, lead, proj["in_13w_weeks"] * 7)
        action, why = _timing(pp, ps, price, p)
        parts[mpn] = {"p_supply_tighter": round(ps, 3), "p_price_up": round(pp, 3),
                      "expected_price_change_3m_pct": price["point"], "lead_projection": proj,
                      "action": action, "why": why}
    return {"segments": segs, "parts": parts,
            "notes": {"segment": "Segment and part probabilities tilt the validated market forecast by how far "
                                 "their current signals sit from the market's. The tilt itself is not yet backtested.",
                      "lead": "Lead-time projections are heuristic until 13 weeks of part history exist; each one is "
                              "logged and scored when due."}}


def _call(p: float, up: str, down: str) -> str:
    return up if p >= 0.58 else down if p <= 0.42 else "Steady"


def _timing(pp: float, ps: float, price: dict, part: dict) -> tuple[str, str]:
    gap = part.get("price_vs_target_pct")
    if pp >= 0.6 or (pp >= 0.55 and ps >= 0.6):
        why = f"{pp:.0%} chance prices are higher in 3 months"
        if ps >= 0.55:
            why += f"; {ps:.0%} chance supply tightens"
        return "Buy ahead / lock pricing", why
    if pp <= 0.4 and ps <= 0.5:
        return "Wait / keep terms short", f"only {pp:.0%} chance prices rise in 3 months; supply not tightening"
    why = f"{pp:.0%} chance prices rise; no strong timing signal"
    if gap is not None and gap > 5:
        why += f". Market is {gap:.0f}% above your target, so negotiate rather than wait"
    return "Hold course", why


def _record_part(con, mpn: str, p_tight: float, lead_days: float, projected_days: float) -> None:
    key = (f"lead:{mpn}", _week(date.today()))
    if con.execute("SELECT 1 FROM forecasts WHERE target=? AND base_period=?", key).fetchone():
        return
    db.upsert(con, "forecasts", {
        "target": key[0], "base_period": key[1],
        "target_period": (date.fromisoformat(key[1]) + timedelta(days=LEAD_HORIZON_DAYS)).isoformat(),
        "horizon": 13, "made_at": db.now_iso(), "prob_up": round(p_tight, 3), "point": round(projected_days, 1),
        "lo": None, "hi": None, "basis": "heuristic", "base_value": lead_days,
        "realized": None, "hit": None, "in_range": None, "resolved_at": None})


def resolve_parts(con) -> int:
    """Score lead-time projections whose 13 weeks are up against the observed lead time."""
    n = 0
    for r in db.rows(con, "SELECT * FROM forecasts WHERE realized IS NULL AND target LIKE 'lead:%'"):
        snap = con.execute("SELECT lead_days, day FROM part_snapshots WHERE mpn=? AND day>=? AND lead_days IS NOT NULL "
                           "ORDER BY day LIMIT 1", (r["target"][5:], r["target_period"])).fetchone()
        if not snap:
            continue
        v = snap["lead_days"]
        moved = v - r["base_value"]
        r.update(realized=v, resolved_at=db.now_iso(),
                 hit=None if abs(moved) < 7 else int((moved > 0) == (r["prob_up"] > 0.5)))
        db.upsert(con, "forecasts", r)
        n += 1
    return n
