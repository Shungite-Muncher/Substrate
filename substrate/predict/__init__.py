"""Predictive layer: forward forecasts for prices and supply, validated walk-forward,
turned into segment outlooks, part-level forecasts and buy-timing calls. Every forecast is
logged once and scored automatically; part models train and promote themselves as data accrues."""
from __future__ import annotations

from . import engine, outlook, parts_model


def run(con, scores: dict) -> dict:
    """Score what's due, retrain, forecast, log. Returns one combined 'outlook' dict."""
    out = engine.run(con)                       # market forecasts (also scores due market forecasts)
    if out.get("status") != "ok":
        return out
    out["resolved_parts_now"] = outlook.resolve_parts(con)
    pm = parts_model.run(con)                   # rebuild outcome pairs, test, promote if earned
    out.update(outlook.build(con, scores, out, pm))
    out["part_models"] = pm["status"]
    out["ledger"] = engine.ledger_summary(con)
    return out
