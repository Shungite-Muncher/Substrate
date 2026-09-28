"""Calibration + backtest.

Price backtest (runs today on free public history):
  For every month since 2014, rebuild the macro price signals using only data that
  had been published by then (publication lags applied), score, and check whether
  the score called the direction of the semiconductor PPI over the next 3 months.
  Compared against a naive "last 3 months continue" baseline.

Lead-time backtest (activates as the accumulator builds part history):
  Did a part's supply-risk score predict the direction of its lead time 4+ weeks later?

Calibration: each price indicator's historical correlation with forward PPI moves
sets a weight multiplier (0.25x-1.5x) that live scoring uses.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date

from . import config, db, signals

CALIBRATION_PATH = config.DATA_DIR / "calibration.json"
BASE_PRICE_WEIGHTS = {"utilization_level": 0.10, "sales_growth": 0.15, "ppi_momentum": 0.30, "tsmc": 0.0}
HORIZON = 3


def _pearson(xs, ys) -> float | None:
    if len(xs) < 8:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return None if sx == 0 or sy == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield date(y, m, 20)
        m += 1
        if m == 13:
            y, m = y + 1, 1


def indicator_history(con, start: date = date(2014, 1, 1)) -> list[dict]:
    cu = signals.series(con, "fred", "capacity_utilization")
    ppi = signals.series(con, "fred", "ppi_semis")
    rev = signals.series(con, "tsmc", "monthly_revenue")
    sia = signals.series(con, "sia", "global_sales_3mma")
    ppi_map = dict(ppi)
    rows = []
    for t in _months(start, date.today()):
        known_ppi = signals.truncate(ppi, t, signals.PUB_LAG["ppi_semis"])
        if len(known_ppi) < 7:
            continue
        last_p, last_v = known_ppi[-1]
        y, m = int(last_p[:4]), int(last_p[5:7]) + HORIZON
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        fwd = ppi_map.get(f"{y}-{m:02d}")
        row = {"as_of": t.isoformat(), "ppi_period": last_p,
               "target": None if fwd is None else (fwd / last_v - 1) * 100}
        lvl = signals.utilization_level(signals.truncate(cu, t, signals.PUB_LAG["capacity_utilization"]))
        row["utilization_level"] = lvl[0] if lvl else None
        pm = signals.ppi_momentum(known_ppi)
        row["ppi_momentum"] = pm[0] if pm else None
        sg = signals.sales_growth(signals.truncate(sia, t, signals.PUB_LAG["sia_sales"]))
        row["sales_growth"] = sg[0] if sg else None
        tm = signals.tsmc_momentum(signals.truncate(rev, t, signals.PUB_LAG["tsmc_revenue"]))
        row["tsmc"] = tm[0] if tm else None
        # persistence baseline: last 3 published months of PPI continue
        row["baseline"] = (known_ppi[-1][1] / known_ppi[-4][1] - 1) * 100 if len(known_ppi) >= 4 else None
        rows.append(row)
    return rows


def _score(row: dict, weights: dict) -> float | None:
    num = den = 0.0
    for k, w in weights.items():
        if row.get(k) is not None and w > 0:
            num += w * row[k]
            den += w
    return None if den == 0 else 50 + 50 * num / den


def _evaluate(rows, weights, dead_band=0.25) -> dict:
    hits = n = bhits = 0
    xs, ys = [], []
    for r in rows:
        if r["target"] is None:
            continue
        s = _score(r, weights)
        if s is None:
            continue
        xs.append(s)
        ys.append(r["target"])
        if abs(r["target"]) < dead_band:
            continue  # flat months don't count toward direction accuracy
        n += 1
        hits += (s > 50) == (r["target"] > 0)
        if r["baseline"] is not None:
            bhits += (r["baseline"] > 0) == (r["target"] > 0)
    return {"months_scored": len(xs), "directional_months": n,
            "hit_rate": round(hits / n, 3) if n else None,
            "baseline_hit_rate": round(bhits / n, 3) if n else None,
            "correlation": None if (c := _pearson(xs, ys)) is None else round(c, 3)}


def calibrate(rows: list[dict]) -> dict:
    factors, corrs = {}, {}
    for k in ("utilization_level", "sales_growth", "ppi_momentum"):
        pairs = [(r[k], r["target"]) for r in rows if r.get(k) is not None and r["target"] is not None]
        c = _pearson([p[0] for p in pairs], [p[1] for p in pairs]) if pairs else None
        corrs[k] = None if c is None else round(c, 3)
        factors[k] = 1.0 if c is None else round(min(1.5, max(0.25, 0.5 + 2 * c)), 2)
    return {"price_weight_factors": factors, "correlations": corrs, "horizon_months": HORIZON}


def lead_time_backtest(con, min_gap_days: int = 28) -> dict:
    """Pair each stored part score with the part's lead time >= min_gap_days later."""
    scores = db.rows(con, "SELECT as_of, key, supply_risk FROM scores WHERE scope='part' ORDER BY as_of")
    hits = n = 0
    for s in scores:
        d0 = s["as_of"][:10]
        snaps = db.rows(con, "SELECT day, lead_days FROM part_snapshots WHERE mpn=? AND lead_days IS NOT NULL "
                             "ORDER BY day", (s["key"],))
        before = [x for x in snaps if x["day"] <= d0]
        after = [x for x in snaps if (date.fromisoformat(x["day"]) - date.fromisoformat(d0)).days >= min_gap_days]
        if not before or not after:
            continue
        delta = after[0]["lead_days"] - before[-1]["lead_days"]
        if abs(delta) < 7 or abs(s["supply_risk"] - 50) < 4:
            continue
        n += 1
        hits += (s["supply_risk"] > 50) == (delta > 0)
    if n < 10:
        return {"status": "accumulating", "pairs": n,
                "note": "Needs part snapshots at least 4 weeks apart; the twice-daily pipeline builds this automatically."}
    return {"status": "ok", "pairs": n, "hit_rate": round(hits / n, 3)}


def run(con) -> dict:
    rows = indicator_history(con)
    cal = calibrate(rows)
    base_w = {k: v for k, v in BASE_PRICE_WEIGHTS.items() if v}
    cal_w = {k: v * cal["price_weight_factors"].get(k, 1.0) for k, v in base_w.items()}
    split = len(rows) // 2
    # out-of-sample check: calibrate on the first half, test on the second
    first = calibrate(rows[:split])
    oos_w = {k: v * first["price_weight_factors"].get(k, 1.0) for k, v in base_w.items()}
    report = {
        "generated": db.now_iso(),
        "price": {
            "target": f"Direction of semiconductor PPI over the next {HORIZON} published months",
            "uncalibrated": _evaluate(rows, base_w),
            "calibrated_in_sample": _evaluate(rows, cal_w),
            "calibrated_out_of_sample": {**_evaluate(rows[split:], oos_w),
                                         "trained_through": rows[split - 1]["as_of"] if split else None},
            "window": [rows[0]["as_of"], rows[-1]["as_of"]] if rows else None,
        },
        "lead_time": lead_time_backtest(con),
        "calibration": cal,
        "series": [{"as_of": r["as_of"], "score": None if (s := _score(r, cal_w)) is None else round(s, 1),
                    "target": None if r["target"] is None else round(r["target"], 2)} for r in rows],
    }
    CALIBRATION_PATH.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    return report


def load_calibration() -> dict | None:
    try:
        return json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
