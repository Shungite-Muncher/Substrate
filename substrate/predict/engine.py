"""Forecast engine: walk-forward evaluation, live forecasts, and the forecast ledger.

Each target is forecast two ways: "will it rise?" (probability) and "by how much?"
(point estimate with an 80% range). Every model is scored out-of-sample against two
baselines a buyer could use for free:
  * trend-following: a calibrated model that only sees the target's own recent momentum
  * climatology: the historical frequency of increases
A model is labelled as having an edge only if it beats both. Otherwise the
trend-following baseline is what flows into outlooks and buy-timing calls.
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np

from .. import db
from .features import FEATURE_NAMES, FEATURES, features_at, idx, latest_base, load, period, target_value
from .models import Logistic, Ridge, brier

log = logging.getLogger(__name__)

TARGETS = {
    "ppi_3m": {"label": "Chip prices, next 3 months", "short": "Prices 3-mo", "series": "ppi", "h": 3,
               "unit": "%", "own": "ppi_m3", "kind": "price",
               "up": "prices rise", "down": "prices fall"},
    "ppi_6m": {"label": "Chip prices, next 6 months", "short": "Prices 6-mo", "series": "ppi", "h": 6,
               "unit": "%", "own": "ppi_m3", "kind": "price",
               "up": "prices rise", "down": "prices fall"},
    "cu_3m": {"label": "Fab utilization, next 3 months", "short": "Utilization 3-mo", "series": "cu", "h": 3,
              "unit": "pts", "own": "cu_d3", "kind": "supply",
              "up": "supply tightens", "down": "supply loosens"},
    "cu_6m": {"label": "Fab utilization, next 6 months", "short": "Utilization 6-mo", "series": "cu", "h": 6,
              "unit": "pts", "own": "cu_d3", "kind": "supply",
              "up": "supply tightens", "down": "supply loosens"},
}
EVAL_START = "2010-01"
MIN_TRAIN = 96          # months of history before the first out-of-sample forecast
EDGE_MARGIN = 0.02      # Brier skill vs trend-following needed to claim an edge
L2 = 10.0               # logistic regularization strength on standardized features
INTERVAL_MISS = 0.20    # 80% ranges
ACI_GAMMA = 0.05        # adaptive conformal step: widen after misses, narrow after hits


def _naive_change(row: dict, spec: dict) -> float:
    """Magnitude baseline: the recent pace simply continues."""
    if spec["series"] == "ppi":
        return row["ppi_m3"] / 4 * spec["h"] / 3          # annualized % -> % over h months
    return row["cu_d3"] * spec["h"] / 3


def build_rows(d: dict) -> list[dict]:
    rows = []
    for i in range(min(min(s) for s in d.values()), latest_base(d) + 1):
        f = features_at(d, i)
        if f:
            rows.append({"i": i, "period": period(i), **f,
                         **{t: target_value(d, t, i, s["h"]) for t, s in TARGETS.items()}})
    return rows


def _X(rows, cols):
    return np.array([[r[c] for c in cols] for r in rows], dtype=float)


def walk_forward(rows: list[dict], name: str) -> dict:
    spec = TARGETS[name]
    h, own = spec["h"], [spec["own"]]
    known = [r for r in rows if r[name] is not None]
    out = []
    resid: list[float] = []         # errors of forecasts whose outcome is already public
    pending: list[tuple] = []       # (index outcome becomes known, error, inside-interval?)
    alpha = INTERVAL_MISS           # adaptive conformal miss rate, nudged after every resolved forecast
    for r in known:
        if r["period"] < EVAL_START:
            continue
        for item in [p for p in pending if p[0] <= r["i"]]:   # release outcomes h months later
            pending.remove(item)
            resid.append(item[1])
            alpha += ACI_GAMMA * (INTERVAL_MISS - (0 if item[2] else 1))
        train = [t for t in known if t["i"] + h <= r["i"]]        # outcome public by forecast time
        if len(train) < MIN_TRAIN:
            continue
        y = np.array([t[name] for t in train])
        up = (y > 0).astype(float)
        full = Logistic.fit(_X(train, FEATURE_NAMES), up, l2=L2)
        pers = Logistic.fit(_X(train, own), up)
        ridge = Ridge.fit(_X(train, FEATURE_NAMES), y)
        x = _X([r], FEATURE_NAMES)
        point = float(ridge.predict(x)[0])
        pool = resid if len(resid) >= 24 else list(y - ridge.predict(_X(train, FEATURE_NAMES)))
        q = _half_width(pool, alpha)
        out.append({"period": r["period"], "y": r[name], "p_model": float(full.prob(x)[0]),
                    "p_trend": float(pers.prob(_X([r], own))[0]), "p_clim": float(up.mean()),
                    "point": point, "naive": _naive_change(r, spec), "lo": point - q, "hi": point + q})
        err = r[name] - point
        pending.append((r["i"] + h, err, abs(err) <= q))
    return {"target": name, "oos": out, "metrics": metrics(out), "resid": resid, "alpha": alpha}


def _half_width(pool, alpha: float) -> float:
    return float(np.quantile(np.abs(pool), min(0.995, max(0.5, 1 - alpha))))


def metrics(oos: list[dict]) -> dict:
    if len(oos) < 24:
        return {"n": len(oos), "verdict": "insufficient history"}
    y = np.array([o["y"] > 0 for o in oos], dtype=float)
    pm, pt, pc = (np.array([o[k] for o in oos]) for k in ("p_model", "p_trend", "p_clim"))
    bm, bt, bc = brier(pm, y), brier(pt, y), brier(pc, y)
    yv = np.array([o["y"] for o in oos])
    mae = float(np.mean(np.abs(yv - np.array([o["point"] for o in oos]))))
    mae_naive = float(min(np.mean(np.abs(yv - np.array([o["naive"] for o in oos]))), np.mean(np.abs(yv))))
    cover = float(np.mean([(o["lo"] <= o["y"] <= o["hi"]) for o in oos]))
    bss_t, bss_c = 1 - bm / bt, 1 - bm / bc
    verdict = ("edge" if bss_t > EDGE_MARGIN and bss_c > 0 else
               "matches trend" if bss_c > 0 else "no skill")
    return {"n": len(oos), "window": [oos[0]["period"], oos[-1]["period"]],
            "brier_model": round(bm, 4), "brier_trend": round(bt, 4), "brier_climatology": round(bc, 4),
            "skill_vs_trend": round(bss_t, 3), "skill_vs_climatology": round(bss_c, 3),
            "hit_rate_model": round(float(np.mean((pm > 0.5) == y)), 3),
            "hit_rate_trend": round(float(np.mean((pt > 0.5) == y)), 3),
            "mae_model": round(mae, 3), "mae_naive": round(mae_naive, 3),
            "interval_coverage_80": round(cover, 3), "verdict": verdict}


def forecast_now(rows: list[dict], name: str, wf: dict) -> dict | None:
    spec = TARGETS[name]
    known = [r for r in rows if r[name] is not None]
    if len(known) < MIN_TRAIN or not rows:
        return None
    base = rows[-1]                                           # latest month with every input published
    y = np.array([r[name] for r in known])
    up = (y > 0).astype(float)
    full = Logistic.fit(_X(known, FEATURE_NAMES), up, l2=L2)
    pers = Logistic.fit(_X(known, [spec["own"]]), up)
    ridge = Ridge.fit(_X(known, FEATURE_NAMES), y)
    x = _X([base], FEATURE_NAMES)
    point = float(ridge.predict(x)[0])
    q = _half_width(wf["resid"], wf["alpha"]) if len(wf["resid"]) >= 24 else np.nan
    m = wf["metrics"]
    edge = m.get("verdict") == "edge"
    p_model, p_trend = float(full.prob(x)[0]), float(pers.prob(_X([base], [spec["own"]]))[0])
    p_used = p_model if edge else p_trend
    naive = _naive_change(base, spec)
    magnitude_edge = m.get("mae_model", 1e9) < m.get("mae_naive", 0)
    point_used = point if magnitude_edge else naive
    contrib = sorted(full.contributions(x[0], FEATURE_NAMES), key=lambda c: -abs(c["logodds"]))
    return {
        "target": name, **{k: spec[k] for k in ("label", "short", "unit", "h", "kind", "up", "down")},
        "base_period": base["period"], "target_period": period(base["i"] + spec["h"]),
        "prob_up": round(p_used, 3), "prob_model": round(p_model, 3), "prob_trend": round(p_trend, 3),
        "basis": "model" if edge else "trend-following baseline",
        "point": round(point_used, 2), "point_model": round(point, 2), "point_naive": round(naive, 2),
        "point_basis": "model" if magnitude_edge else "recent pace continues",
        "lo": None if np.isnan(q) else round(point_used - q, 2),
        "hi": None if np.isnan(q) else round(point_used + q, 2),
        "drivers": [{**c, "label": FEATURES[c["feature"]], "value": round(c["value"], 2),
                     "logodds": round(c["logodds"], 3)} for c in contrib[:5]],
        "metrics": m,
    }


# ---- ledger: every live forecast is recorded once and scored when the outcome lands ----

def record(con, fc: dict, base_value: float | None) -> None:
    exists = con.execute("SELECT 1 FROM forecasts WHERE target=? AND base_period=?",
                         (fc["target"], fc["base_period"])).fetchone()
    if exists:
        return  # first call for a base period is the one that counts; no quiet revisions
    db.upsert(con, "forecasts", {
        "target": fc["target"], "base_period": fc["base_period"], "target_period": fc["target_period"],
        "horizon": fc["h"], "made_at": db.now_iso(), "prob_up": fc["prob_up"], "point": fc["point"],
        "lo": fc["lo"], "hi": fc["hi"], "basis": fc["basis"], "base_value": base_value,
        "realized": None, "hit": None, "in_range": None, "resolved_at": None})


def resolve(con, d: dict) -> int:
    n = 0
    for r in db.rows(con, "SELECT * FROM forecasts WHERE realized IS NULL"):
        if r["target"] not in TARGETS:
            continue
        v = target_value(d, r["target"], idx(r["base_period"]), r["horizon"])
        if v is None:
            continue
        r.update(realized=round(v, 3), hit=int((v > 0) == (r["prob_up"] > 0.5)),
                 in_range=None if r["lo"] is None else int(r["lo"] <= v <= r["hi"]), resolved_at=db.now_iso())
        db.upsert(con, "forecasts", r)
        n += 1
    return n


def ledger_summary(con) -> dict:
    rows = db.rows(con, "SELECT * FROM forecasts ORDER BY made_at DESC")
    done = [r for r in rows if r["hit"] is not None]
    by_target = {}
    for r in done:
        t = by_target.setdefault(r["target"], {"resolved": 0, "hits": 0})
        t["resolved"] += 1
        t["hits"] += r["hit"]
    return {"made": len(rows), "resolved": len(done),
            "hit_rate": round(sum(r["hit"] for r in done) / len(done), 3) if done else None,
            "by_target": by_target, "recent": rows[:40]}


def run(con) -> dict:
    d = load(con)
    if not all(d.values()):
        return {"status": "missing data", "forecasts": {}}
    resolved = resolve(con, d)
    rows = build_rows(d)
    results = {}
    for name, spec in TARGETS.items():
        wf = walk_forward(rows, name)
        fc = forecast_now(rows, name, wf)
        if not fc:
            continue
        base_series = d[spec["series"]]
        record(con, fc, base_series.get(idx(fc["base_period"])))
        fc["history"] = [{k: (round(o[k], 3) if isinstance(o[k], float) else o[k])
                          for k in ("period", "y", "p_model", "p_trend", "point", "lo", "hi")} for o in wf["oos"]]
        results[name] = fc
    log.info("forecasts: %s", {k: (v["prob_up"], v["basis"]) for k, v in results.items()})
    return {"status": "ok", "generated": db.now_iso(), "as_of": date.today().isoformat(),
            "rows": len(rows), "resolved_now": resolved, "forecasts": results, "ledger": ledger_summary(con)}
