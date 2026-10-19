"""Part-level models that train themselves once the accumulator has enough history.

Every pipeline run:
  1. Builds (snapshot today, snapshot ~13 weeks later) outcome pairs from part_snapshots,
     one per part per week, with features computed only from what was known on day one.
  2. For each target (lead time, price, PBO backlog) runs a time-ordered out-of-sample test:
     train on pairs whose outcome was known before the test period starts, then score on
     the later pairs.
  3. Promotes the trained model only if it beats all three alternatives on Brier score:
     the rule-of-thumb heuristic Substrate uses today, persistence ("recent trend continues")
     and climatology (historical base rate). Otherwise the heuristic stays in charge.

Status is written to data/part_models.json so the dashboard can show exactly where each
model stands ("accumulating 12/60 pairs", "tested, heuristic still better", "promoted").
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

import numpy as np

from .. import config, db
from .models import Logistic, Ridge, brier

STATUS_PATH = config.DATA_DIR / "part_models.json"
HORIZON_DAYS = 91
MATCH_WINDOW_DAYS = 21           # outcome snapshot may land up to 3 weeks after the horizon
MIN_PAIRS = 60                   # before any test is attempted
MIN_TEST = 20
MIN_PARTS = 5
TEST_SHARE = 0.35                # latest 35% of base dates form the out-of-sample period
PROMOTE_MARGIN = 0.02            # Brier improvement required over the best alternative
L2 = 10.0

FEATURES = ["lead_gap", "lead_trend", "price_trend_28d", "pbo_trend_28d", "cover", "sellers", "on_order_rel",
            "has_market_data", "supply_score", "price_score", "market_p_supply"]
CUSTOMER = "customer"

TARGETS = {
    "lead": {"label": "Part lead time, 13 weeks out", "field": "lead_days", "threshold": 3.5 / 84,
             "trend": "lead_trend", "up": "lead time lengthens"},
    "price": {"label": "Part price, 13 weeks out", "field": "price_1k", "threshold": 0.01,
              "trend": "price_trend_28d", "up": "price rises"},
    "pbo": {"label": "PBO backlog, 13 weeks out", "field": "on_order", "threshold": math.log(1.10),
            "trend": "pbo_trend_28d", "up": "backlog grows"},
}


def _sig(x):
    return 1 / (1 + math.exp(-x))


def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def heuristic_prob(target: str, f: dict) -> float:
    """What Substrate's rule of thumb said that week: the exact logged value when the ledger has it,
    otherwise a reconstruction from the same point-in-time inputs."""
    logged = f.get("_heuristic", {}).get(target)
    if logged is not None:
        return logged
    base = f["market_p_supply"]
    if target == "price":
        return _sig(_logit(f.get("market_p_price", 0.5)) + (f["price_score"] * 50) / 50)
    return _sig(_logit(base) + (f["supply_score"] * 50) / 50)


# ---- dataset ------------------------------------------------------------------------

def _weekly_snapshots(con) -> dict[tuple[str, str], list[dict]]:
    """First snapshot of each ISO week per part and source series. Customer PO/quote history is its
    own series, and market providers form another (preferring Octopart on days with both), so an
    outcome pair never compares a customer's negotiated price against a distributor's list price."""
    rows = db.rows(con, "SELECT * FROM part_snapshots ORDER BY mpn, day, "
                        "CASE provider WHEN 'octopart' THEN 0 ELSE 1 END")
    out: dict[tuple[str, str], list[dict]] = {}
    seen = set()
    for r in rows:
        series_key = (r["mpn"], CUSTOMER if r["provider"] == CUSTOMER else "market")
        wk = (series_key, date.fromisoformat(r["day"]).isocalendar()[:2])
        if wk in seen:
            continue
        seen.add(wk)
        out.setdefault(series_key, []).append(r)
    return out


def _value(r: dict, field: str):
    v = r.get(field)
    if field == "on_order" and r.get("provider") == CUSTOMER:
        return None                       # PO history says nothing about distributor backlog
    return 0.0 if field == "on_order" and v is None else v


def _at_or_before(seq: list[dict], day: date, max_gap: int) -> dict | None:
    best = None
    for r in seq:
        d = date.fromisoformat(r["day"])
        if d <= day and (day - d).days <= max_gap:
            best = r
    return best


def _prev_month(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.year - (d.month == 1)}-{(d.month - 2) % 12 + 1:02d}"


def _context(con, mpn: str, day: str, market_hist: dict | None = None) -> dict:
    """Scores and market forecasts as they stood on `day` (no later information). Before the live
    ledger existed (e.g. imported customer history), market odds come from the walk-forward
    forecasts, which were themselves made only from data public at the time."""
    s = con.execute("SELECT supply_risk, price_trend FROM scores WHERE scope='part' AND key=? AND as_of<=? "
                    "ORDER BY as_of DESC LIMIT 1", (mpn, day + "T23:59Z")).fetchone()
    def fc(target):
        r = con.execute("SELECT prob_up FROM forecasts WHERE target=? AND made_at<=? ORDER BY made_at DESC LIMIT 1",
                        (target, day + "T23:59:59+00:00")).fetchone()
        if r:
            return r["prob_up"]
        return ((market_hist or {}).get(target) or {}).get(_prev_month(day), 0.5)
    return {"supply_score": ((s["supply_risk"] if s else 50) - 50) / 50,
            "price_score": ((s["price_trend"] if s else 50) - 50) / 50,
            "market_p_supply": fc("cu_3m"), "market_p_price": fc("ppi_3m")}


def features_for(snap: dict, prev: dict | None, ctx: dict, annual_qty: float | None) -> dict | None:
    lead, stock, price = snap.get("lead_days"), snap.get("total_avail"), snap.get("price_1k")
    if lead is None and price is None:
        return None
    sellers = snap.get("sellers")
    if isinstance(sellers, str):
        sellers = json.loads(sellers or "[]")
    on_order = snap.get("on_order") or 0
    f = {
        "lead_gap": (lead - 84) / 84 if lead else 0.0,
        "lead_trend": math.log(lead / prev["lead_days"]) if lead and prev and prev.get("lead_days") else 0.0,
        "cover": (0.0 if stock is None else
                  math.log1p(stock / (annual_qty / 52)) if annual_qty else math.log10(stock + 1) / 2),
        "has_market_data": 0.0 if stock is None else 1.0,
        "sellers": float(len({s["seller"] for s in sellers or [] if (s.get("stock") or 0) > 0})),
        "on_order_rel": math.log1p(on_order / max(stock or 0, 1)),
        "price_trend_28d": (math.log(snap["price_1k"] / prev["price_1k"])
                            if prev and prev.get("price_1k") and snap.get("price_1k") else 0.0),
        "pbo_trend_28d": math.log((on_order + 1) / ((prev.get("on_order") or 0) + 1)) if prev else 0.0,
        **ctx,
    }
    return f


def _logged_heuristics(con) -> dict:
    out = {}
    for r in db.rows(con, "SELECT target, base_period, meta FROM forecasts WHERE target LIKE '%:%' AND meta IS NOT NULL"):
        kind, mpn = r["target"].split(":", 1)
        h = json.loads(r["meta"] or "{}").get("heuristic_prob")
        if h is not None:
            out[(kind, mpn, r["base_period"])] = h
    return out


def _week(day: str) -> str:
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def build_pairs(con, market_hist: dict | None = None) -> list[dict]:
    annual = {r["mpn"]: r["annual_qty"] for r in db.rows(con, "SELECT mpn, annual_qty FROM bom")}
    logged = _logged_heuristics(con)
    pairs = []
    for (mpn, kind), seq in _weekly_snapshots(con).items():
        for snap in seq:
            d0 = date.fromisoformat(snap["day"])
            prev = _at_or_before(seq, d0 - timedelta(days=28), 14)
            f = features_for(snap, prev, _context(con, mpn, snap["day"], market_hist), annual.get(mpn))
            if not f:
                continue
            f["_heuristic"] = {t: logged.get((t, mpn, _week(snap["day"]))) for t in TARGETS}
            later = [r for r in seq if HORIZON_DAYS <= (date.fromisoformat(r["day"]) - d0).days
                     <= HORIZON_DAYS + MATCH_WINDOW_DAYS]
            row = {"mpn": mpn, "series": kind, "day": snap["day"], "f": f,
                   "now": {k: _value(snap, s["field"]) for k, s in TARGETS.items()}}
            if later:
                row["outcome_day"] = later[0]["day"]
                row["later"] = {k: _value(later[0], s["field"]) for k, s in TARGETS.items()}
            pairs.append(row)
    return pairs


def _y(row: dict, target: str) -> float | None:
    if "later" not in row:
        return None
    a, b = row["now"][target], row["later"][target]
    if a is None or b is None:
        return None
    if target == "pbo":
        return math.log((b + 1) / (a + 1))
    if not a or not b:
        return None
    return math.log(b / a)


# ---- evaluation & promotion ----------------------------------------------------------

def _X(rows, cols=FEATURES):
    return np.array([[r["f"][c] for c in cols] for r in rows], dtype=float)


def evaluate(pairs: list[dict], target: str) -> dict:
    spec = TARGETS[target]
    labelled = [r for r in pairs if _y(r, target) is not None]
    n, parts = len(labelled), len({r["mpn"] for r in labelled})
    base = {"target": target, "label": spec["label"], "pairs": n, "parts": parts, "needed_pairs": MIN_PAIRS}
    if n < MIN_PAIRS or parts < MIN_PARTS:
        return {**base, "status": "accumulating",
                "note": f"{n}/{MIN_PAIRS} outcome pairs across {parts}/{MIN_PARTS}+ parts"}
    days = sorted({r["day"] for r in labelled})
    cutoff = days[int(len(days) * (1 - TEST_SHARE))]
    test = [r for r in labelled if r["day"] >= cutoff]
    train = [r for r in labelled if r["outcome_day"] < cutoff]       # outcome known before the test period
    if len(test) < MIN_TEST or len(train) < MIN_PAIRS // 2:
        return {**base, "status": "accumulating", "note": f"waiting for a long enough test period ({len(test)} test pairs)"}
    y_tr = np.array([_y(r, target) > spec["threshold"] for r in train], dtype=float)
    y_te = np.array([_y(r, target) > spec["threshold"] for r in test], dtype=float)
    if y_tr.min() == y_tr.max():
        return {**base, "status": "accumulating", "note": "no variation in outcomes yet"}
    model = Logistic.fit(_X(train), y_tr, l2=L2)
    pers = Logistic.fit(_X(train, [spec["trend"]]), y_tr)
    p_model = model.prob(_X(test))
    p_pers = pers.prob(_X(test, [spec["trend"]]))
    p_heur = np.array([heuristic_prob(target, r["f"]) for r in test])
    p_clim = np.full(len(test), y_tr.mean())
    scores = {"model": brier(p_model, y_te), "heuristic": brier(p_heur, y_te),
              "persistence": brier(p_pers, y_te), "climatology": brier(p_clim, y_te)}
    best_alt = min(v for k, v in scores.items() if k != "model")
    promoted = scores["model"] < best_alt * (1 - PROMOTE_MARGIN)
    sources = sorted({r["series"] for r in labelled})
    base["sources"] = sources
    return {**base, "status": "promoted" if promoted else "heuristic",
            "note": ("trained model beats heuristic, persistence and base rate out of sample" if promoted else
                     "tested; trained model not better than " +
                     min((k for k in scores if k != "model"), key=scores.get) + " yet"),
            "test_window": [cutoff, days[-1]], "train_pairs": len(train), "test_pairs": len(test),
            "brier": {k: round(v, 4) for k, v in scores.items()},
            "hit_rate": {"model": round(float(np.mean((p_model > .5) == y_te)), 3),
                         "heuristic": round(float(np.mean((p_heur > .5) == y_te)), 3)}}


class Promoted:
    """Final models refit on every labelled pair, used for today's part forecasts."""

    def __init__(self, pairs: list[dict], target: str):
        spec = TARGETS[target]
        rows = [r for r in pairs if _y(r, target) is not None]
        self.target = target
        self.clf = Logistic.fit(_X(rows), np.array([_y(r, target) > spec["threshold"] for r in rows], dtype=float), l2=L2)
        self.reg = Ridge.fit(_X(rows), np.array([_y(r, target) for r in rows]))

    def predict(self, f: dict) -> tuple[float, float]:
        x = np.array([[f[c] for c in FEATURES]], dtype=float)
        return float(self.clf.prob(x)[0]), float(self.reg.predict(x)[0])


def run(con, market_hist: dict | None = None) -> dict:
    pairs = build_pairs(con, market_hist)
    status, models = {}, {}
    for t in TARGETS:
        status[t] = evaluate(pairs, t)
        if status[t]["status"] == "promoted":
            models[t] = Promoted(pairs, t)
    status["_generated"] = db.now_iso()
    STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8", newline="\n")
    latest = {}
    for r in pairs:                       # most recent *market* feature row per part, for live predictions
        if r["series"] == CUSTOMER:
            continue
        if r["mpn"] not in latest or r["day"] > latest[r["mpn"]]["day"]:
            latest[r["mpn"]] = r
    return {"status": status, "models": models, "latest": latest}
