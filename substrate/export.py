"""Write the static JSON that the dashboard, Chrome extension and chat worker read.
Everything is served from GitHub Pages, so there is no always-on server to pay for."""
from __future__ import annotations

import json

from . import config, db, parts, signals
from .segments import COMPANY_NAMES, SEGMENTS


def _w(name: str, obj) -> None:
    config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.SITE_DATA_DIR / name).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                                             encoding="utf-8")


def _part_view(p: dict) -> dict:
    snap = p.get("snapshot") or {}
    return {
        "mpn": p["mpn"], "segment": p.get("segment"),
        "segment_label": SEGMENTS.get(p.get("segment") or "", {}).get("label"),
        "supply_risk": p["supply_risk"], "price_trend": p["price_trend"], "confidence": p["confidence"],
        "labels": p["labels"], "components": p["components"], "signals": p["signals"],
        "manufacturer": snap.get("manufacturer") or (p.get("bom") or {}).get("manufacturer"),
        "description": snap.get("description") or (p.get("bom") or {}).get("description"),
        "lead_days": snap.get("lead_days"), "total_avail": snap.get("total_avail"), "price_1k": snap.get("price_1k"),
        "lifecycle": snap.get("lifecycle"), "provider": snap.get("provider"), "data_day": snap.get("day"),
        "url": snap.get("url"), "sellers": (snap.get("sellers") or [])[:8],
        "annual_qty": (p.get("bom") or {}).get("annual_qty"), "target_price": (p.get("bom") or {}).get("target_price"),
        "price_vs_target_pct": p.get("price_vs_target_pct"), "history": p.get("history", []),
    }


def run(con, results: dict, backtest_report: dict | None = None) -> dict:
    briefs = db.rows(con, "SELECT id, created_at, edition, headline, body_md, model FROM briefings "
                          "ORDER BY created_at DESC LIMIT 14")
    news = db.rows(con, "SELECT title, url, source, published, segments, supply_tone, price_tone FROM documents "
                        "ORDER BY published DESC LIMIT 60")
    for n in news:
        n["segments"] = json.loads(n["segments"] or "[]")
    history = db.rows(con, "SELECT as_of, scope, key, supply_risk, price_trend, confidence FROM scores "
                           "WHERE scope IN ('market','segment') ORDER BY as_of")
    tickers = list(dict.fromkeys(sum((s["companies"] for s in SEGMENTS.values()), []) + config.CUSTOMER_PEERS))
    inventory = {t: {"name": COMPANY_NAMES.get(t, t), "days": [(q, round(v, 1)) for q, v in signals.inventory_days(con, t)][-12:]}
                 for t in tickers}
    part_views = {k: _part_view(v) for k, v in results["parts"].items()}
    latest = {
        "as_of": results["as_of"], "generated": db.now_iso(), "customer": config.CUSTOMER_NAME,
        "peers": config.CUSTOMER_PEERS,
        "market": results["market"],
        "segments": {k: {**v, "key": k} for k, v in results["segments"].items()},
        "parts": list(part_views.values()),
        "briefings": briefs, "news": news,
        "score_history": history[-2000:],
        "series": {
            "tsmc_revenue": signals.series(con, "tsmc", "monthly_revenue")[-48:],
            "sia_sales": signals.series(con, "sia", "global_sales_3mma")[-48:],
            "capacity_utilization": signals.series(con, "fred", "capacity_utilization")[-120:],
            "ppi_semis": signals.series(con, "fred", "ppi_semis")[-120:],
        },
        "inventory_days": inventory,
        "providers": parts.configured(),
        "octopart_usage": db.usage(con, "octopart"),
        "model": {"baseline_lead_days": signals.BASELINE_LEAD_DAYS},
        "backtest": {k: v for k, v in (backtest_report or {}).items() if k != "series"} or None,
    }
    _w("latest.json", latest)
    _w("parts.json", {k: {kk: vv for kk, vv in v.items() if kk not in ("history", "components")}
                      for k, v in part_views.items()})
    if backtest_report:
        _w("backtest.json", backtest_report)
    # Compact context for the chat worker (keeps prompts small on the free tier).
    ctx = {
        "as_of": results["as_of"], "customer": config.CUSTOMER_NAME,
        "market": {k: results["market"][k] for k in ("supply_risk", "price_trend", "confidence", "labels")}
        | {"drivers": [s["evidence"] for s in results["market"]["signals"][:6]]},
        "segments": {k: {"label": v["label"], "supply_risk": v["supply_risk"], "price_trend": v["price_trend"],
                         "labels": v["labels"], "drivers": [s["evidence"] for s in v["signals"][:4]]}
                     for k, v in results["segments"].items()},
        "bom": [{"mpn": p["mpn"], "segment": p["segment"], "supply_risk": p["supply_risk"],
                 "price_trend": p["price_trend"], "lead_weeks": round(p["lead_days"] / 7, 1) if p["lead_days"] else None,
                 "stock": p["total_avail"], "price_1k": p["price_1k"], "target_price": p["target_price"],
                 "annual_qty": p["annual_qty"], "drivers": [s["evidence"] for s in p["signals"][:3]]}
                for p in part_views.values()],
        "peer_inventory_days": {t: v for t, v in inventory.items() if t in config.CUSTOMER_PEERS},
        "latest_briefing": briefs[0]["body_md"] if briefs else None,
        "headlines": [{"title": n["title"], "source": n["source"], "url": n["url"]} for n in news[:12]],
    }
    _w("context.json", ctx)
    return {"parts": len(part_views), "briefings": len(briefs), "news": len(news)}
