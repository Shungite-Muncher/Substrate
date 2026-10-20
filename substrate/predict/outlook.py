"""From market forecasts to decisions: segment outlooks, part-level forecasts and
buy-timing calls, with every part forecast logged and auto-scored.

Layering, and how much each layer is validated:
  1. Market forecasts (engine.py): walk-forward tested since 2010.
  2. Segment tilt: shifts the market probability by how far the segment's current
     signals sit from the market's. Not separately validated (no free segment-level
     price history); labelled as a tilt.
  3. Part forecasts, logged weekly per part and scored 13 weeks later:
       price:  will the part's 1k price rise?
       lead:   will its factory lead time lengthen?
       pbo:    will its on-order / backorder quantity grow?
       eta:    will the incoming on-order stock slip past its stated arrival date?
     Each starts as a rule of thumb. parts_model.py trains a model on the accumulated
     outcomes and swaps it in only once it beats the rule of thumb out of sample.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

from .. import db
from ..segments import SEGMENTS

TILT = 1.0              # log-odds shift per unit of (segment score - market score) / 50
PART_GRACE_DAYS = 3     # part snapshots refresh every 3 days
PART_DECAY_HALF_LIFE = 14   # after that, every 2 weeks of staleness halves a part forecast's conviction
LEAD_ELASTICITY = 0.30  # log lead-time change at full-confidence tightening (+1)
HORIZON_DAYS = 91
ETA_GRACE_DAYS = 14     # stock counts as on time if it shows up within 2 weeks of the stated date
ETA_SLIP_BASE = 0.35    # prior chance a distributor's stated on-order date slips
FLAT = {"price": 0.01, "lead": 7.0, "pbo": 0.10}   # moves smaller than this don't count as hit or miss
KINDS = {
    "price": "Part price direction (13 weeks)",
    "lead": "Part lead-time direction (13 weeks)",
    "pbo": "PBO backlog direction (13 weeks)",
    "eta": "PBO on-time arrival",
}


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def tilt(p: float, score: float, ref: float) -> float:
    return _sig(_logit(p) + TILT * (score - ref) / 50)


def _week(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def build(con, scores: dict, forecasts: dict, part_models: dict | None = None) -> dict:
    f = forecasts.get("forecasts", {})
    price, supply = f.get("ppi_3m"), f.get("cu_3m")
    if not price or not supply:
        return {}
    pm = part_models or {}
    models, latest = pm.get("models", {}), pm.get("latest", {})
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
        ref = scores["segments"].get(p.get("segment") or "", mkt)
        ps_h = tilt(seg["p_supply_tighter"] if seg else supply["prob_up"], p["supply_risk"], ref["supply_risk"])
        pp_h = tilt(seg["p_price_up"] if seg else price["prob_up"], p["price_trend"], ref["price_trend"])
        snap = p.get("snapshot") or {}
        feats = (latest.get(mpn) or {}).get("f") if (latest.get(mpn) or {}).get("day") == snap.get("day") else None
        out = {"basis": {}}
        age = (date.today() - date.fromisoformat(snap["day"])).days if snap.get("day") else None
        k = 1.0 if age is None else 0.5 ** (max(0, age - PART_GRACE_DAYS) / PART_DECAY_HALF_LIFE)
        shrink = lambda q: 0.5 + (q - 0.5) * k
        out["data_age_days"], out["decay"] = age, round(k, 3)

        # price
        pp, basis = _choose(models.get("price"), feats, pp_h)
        pp = shrink(pp)
        out["p_price_up"], out["basis"]["price"] = round(pp, 3), basis
        out["expected_price_change_3m_pct"] = price["point"]
        if snap.get("price_1k"):
            _record(con, "price", mpn, pp, pp_h, basis, snap["price_1k"], snap, point=price["point"])

        # lead time
        ps, basis, lead_y = _choose(models.get("lead"), feats, ps_h, with_point=True)
        ps = shrink(ps)
        lead_y = None if lead_y is None else lead_y * k
        out["p_supply_tighter"], out["basis"]["lead"] = round(ps, 3), basis
        lead = snap.get("lead_days")
        out["lead_projection"] = None
        if lead:
            y = lead_y if lead_y is not None else LEAD_ELASTICITY * (2 * ps - 1)
            out["lead_projection"] = {"now_weeks": round(lead / 7, 1), "in_13w_weeks": round(lead * math.exp(y) / 7, 1),
                                      "range_weeks": [round(lead * math.exp(y - 0.25) / 7, 1),
                                                      round(lead * math.exp(y + 0.25) / 7, 1)],
                                      "basis": basis}
            _record(con, "lead", mpn, ps, ps_h, basis, lead, snap, point=round(lead * math.exp(y), 1))

        # PBO: backlog direction + on-time arrival
        oo, eta = snap.get("on_order"), snap.get("on_order_eta")
        out["pbo"] = None
        if oo:
            pb, basis = _choose(models.get("pbo"), feats, ps_h)
            pb = shrink(pb)
            out["pbo"] = {"on_order": oo, "eta": eta, "p_backlog_grows": round(pb, 3), "basis": basis}
            _record(con, "pbo", mpn, pb, ps_h, basis, oo, snap)
            if eta:
                p_slip = _sig(_logit(ETA_SLIP_BASE) + 1.5 * (2 * ps - 1))
                out["pbo"]["p_eta_slips"] = round(p_slip, 3)
                _record(con, "eta", mpn, p_slip, p_slip, "heuristic", oo, snap,
                        target_period=(date.fromisoformat(eta[:10]) + timedelta(days=ETA_GRACE_DAYS)).isoformat(),
                        extra={"eta": eta[:10], "stock": snap.get("total_avail")})

        out["action"], out["why"] = _timing(pp, ps, price, p)
        parts[mpn] = out
    return {"segments": segs, "parts": parts,
            "notes": {"segment": "Segment and part probabilities tilt the validated market forecast by how far "
                                 "their current signals sit from the market's. The tilt itself is not yet backtested.",
                      "parts": "Part price, lead-time and PBO forecasts start as rules of thumb. Each is logged weekly "
                               "and scored 13 weeks later; a trained model replaces a rule only after beating it out of sample."}}


def _choose(model, feats, heuristic, with_point=False):
    if model is not None and feats is not None:
        p, y = model.predict(feats)
        return (p, "trained part model", y) if with_point else (p, "trained part model")
    return (heuristic, "rule of thumb", None) if with_point else (heuristic, "rule of thumb")


def _call(p: float, up: str, down: str) -> str:
    return up if p >= 0.58 else down if p <= 0.42 else "Steady"


NORMAL_LEAD_DAYS = 84      # ~12 weeks
THIN_COVER_WEEKS = 4


def _part_supply(part: dict) -> tuple[str, str]:
    """The part's own supply position, which outranks market-wide odds for the stocking decision."""
    snap = part.get("snapshot") or {}
    lead, stock = snap.get("lead_days"), snap.get("total_avail")
    qty = (part.get("bom") or {}).get("annual_qty")
    cover = stock / (qty / 52) if stock is not None and qty else None
    oo, eta = snap.get("on_order"), snap.get("on_order_eta")
    if lead is None and stock is None:
        return "unknown", "no part data"
    facts = []
    if lead is not None:
        facts.append(f"{lead / 7:.0f}-week lead time")
    if cover is not None:
        facts.append(f"{cover:.1f} weeks of channel stock")
    if oo:
        facts.append(f"{oo:,} on order" + (f" (next {eta[:10]})" if eta else ""))
    # Lead time decides; one distributor's shelf stock vs a full year of usage would make nearly every part
    # look short, so stock only counts when it's zero with no restock due within a month.
    soon = (date.today() + timedelta(days=30)).isoformat()
    restock_soon = bool(oo) and bool(eta) and eta[:10] <= soon
    out_of_stock = stock == 0 and not restock_soon
    tight = (lead is not None and lead > 1.4 * NORMAL_LEAD_DAYS) or out_of_stock
    easy = lead is not None and lead <= NORMAL_LEAD_DAYS and (stock or 0) > 0
    return ("tight" if tight else "easy" if easy else "normal"), ", ".join(facts)


def _timing(pp: float, ps: float, price: dict, part: dict) -> tuple[str, str]:
    gap = part.get("price_vs_target_pct")
    state, facts = _part_supply(part)
    rising = pp >= 0.6 or (pp >= 0.55 and ps >= 0.6)
    if rising and state in ("easy", "normal"):
        return "Lock pricing (no need to buy ahead)", (f"{pp:.0%} chance prices are higher in 3 months, but supply is "
                                                       f"{'comfortable' if state == 'easy' else 'adequate'} ({facts}): "
                                                       "fix the price, keep normal order timing")
    if rising:
        why = f"{pp:.0%} chance prices are higher in 3 months"
        if state == "tight":
            why += f"; supply is tight ({facts})"
        elif ps >= 0.55:
            why += f"; {ps:.0%} chance supply tightens"
        return "Buy ahead / lock pricing", why
    if state == "tight":
        return "Secure supply", f"price outlook neutral, but supply is tight ({facts})"
    if pp <= 0.4 and ps <= 0.5:
        return "Wait / keep terms short", f"only {pp:.0%} chance prices rise in 3 months; supply not tightening"
    why = f"{pp:.0%} chance prices rise; no strong timing signal"
    if gap is not None and gap > 5:
        why += f". Market is {gap:.0f}% above your target, so negotiate rather than wait"
    return "Hold course", why


def _record(con, kind: str, mpn: str, prob: float, heuristic: float, basis: str, base_value: float, snap: dict,
            point: float | None = None, target_period: str | None = None, extra: dict | None = None) -> None:
    """Log once per part, kind and week. Later runs that week can't revise it."""
    key = (f"{kind}:{mpn}", _week(date.today()))
    if con.execute("SELECT 1 FROM forecasts WHERE target=? AND base_period=?", key).fetchone():
        return
    db.upsert(con, "forecasts", {
        "target": key[0], "base_period": key[1],
        "target_period": target_period or (date.fromisoformat(key[1]) + timedelta(days=HORIZON_DAYS)).isoformat(),
        "horizon": 13, "made_at": db.now_iso(), "prob_up": round(prob, 3), "point": point,
        "lo": None, "hi": None, "basis": basis, "base_value": base_value,
        "realized": None, "hit": None, "in_range": None, "resolved_at": None,
        "meta": json.dumps({"heuristic_prob": round(heuristic, 3), "provider": snap.get("provider"),
                            "snapshot_day": snap.get("day"), **(extra or {})})})


def _snap_after(con, mpn: str, day: str, provider: str | None, field: str) -> dict | None:
    q = (f"SELECT * FROM part_snapshots WHERE mpn=? AND day>=? AND day<=? "
         f"ORDER BY CASE provider WHEN ? THEN 0 ELSE 1 END, day LIMIT 1")
    limit = (date.fromisoformat(day) + timedelta(days=45)).isoformat()
    r = con.execute(q, (mpn, day, limit, provider or "")).fetchone()
    return dict(r) if r and (field == "on_order" or r[field] is not None) else None


def resolve_parts(con) -> int:
    """Score every part forecast whose horizon has passed against the observed snapshot."""
    n = 0
    today = date.today().isoformat()
    for r in db.rows(con, "SELECT * FROM forecasts WHERE realized IS NULL AND target LIKE '%:%' AND target_period<=?",
                     (today,)):
        kind, mpn = r["target"].split(":", 1)
        meta = json.loads(r.get("meta") or "{}")
        prov = meta.get("provider")
        expired = (date.today() - date.fromisoformat(r["target_period"])).days > 45
        if kind == "eta":
            s = _snap_after(con, mpn, r["target_period"], prov, "on_order")
            if not s:
                if expired:
                    r.update(realized=-1, resolved_at=db.now_iso())   # no data: closed, not counted
                    db.upsert(con, "forecasts", r)
                    n += 1
                continue
            base_oo, base_stock = r["base_value"] or 0, meta.get("stock") or 0
            arrived = (s.get("on_order") or 0) <= 0.5 * base_oo or (s.get("total_avail") or 0) - base_stock >= 0.5 * base_oo
            new_eta = (s.get("on_order_eta") or "")[:10]
            slipped = not arrived and bool(new_eta) and new_eta > meta.get("eta", "")
            hit = None if not arrived and not slipped else int(slipped == (r["prob_up"] > 0.5))
            r.update(realized=1 if slipped else 0, hit=hit, resolved_at=db.now_iso())
        else:
            field = {"price": "price_1k", "lead": "lead_days", "pbo": "on_order"}[kind]
            s = _snap_after(con, mpn, r["target_period"], prov, field)
            if not s:
                if expired:
                    r.update(realized=-1, resolved_at=db.now_iso())
                    db.upsert(con, "forecasts", r)
                    n += 1
                continue
            v, b = (s.get(field) or 0) if kind == "pbo" else s[field], r["base_value"] or 0
            if kind == "lead":
                moved, flat = v - b, abs(v - b) < FLAT["lead"]
            elif kind == "price":
                moved = (v / b - 1) if b else 0.0
                flat = abs(moved) < FLAT["price"]
            else:
                moved = math.log((v + 1) / (b + 1))
                flat = abs(moved) < math.log(1 + FLAT["pbo"])
            r.update(realized=v, resolved_at=db.now_iso(), hit=None if flat else int((moved > 0) == (r["prob_up"] > 0.5)))
        db.upsert(con, "forecasts", r)
        n += 1
    return n
