"""Predictive layer: forward forecasts for prices and supply, validated walk-forward,
turned into segment outlooks, part lead-time projections and buy-timing calls."""
from __future__ import annotations

from . import engine, outlook


def run(con, scores: dict) -> dict:
    """Forecast, then translate into decisions. Returns one combined 'outlook' dict."""
    out = engine.run(con)
    if out.get("status") != "ok":
        return out
    out.update(outlook.build(con, scores, out))
    out["resolved_parts_now"] = outlook.resolve_parts(con)
    out["ledger"] = engine.ledger_summary(con)   # refresh after part projections were logged
    return out
