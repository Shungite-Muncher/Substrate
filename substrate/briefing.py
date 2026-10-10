"""The automated reporter: twice-daily, news-style briefings timed to the Asian
and U.S. market opens. Gemini writes the prose when a key is configured; a
deterministic template writer produces the same structure for free otherwise.
Either way every number comes from the scoring engine, never from the model."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import config, db, llm, scoring
from .segments import SEGMENTS

log = logging.getLogger(__name__)

SYSTEM = """You are Substrate's market reporter for semiconductor procurement managers.
Write like a wire-service analyst: short, specific, no hype, no filler.
Rules:
- Use ONLY the numbers and facts in the provided context. Never invent figures, companies or events.
- Cite sources inline as [n] using the numbered source list.
- Scores run 0-100 with 50 neutral. Supply risk: higher = tighter supply. Price trend: higher = rising prices.
- Speak to a buyer: what it means for their negotiations this week.
- Output GitHub-flavored Markdown, 250-420 words."""

FORMAT = """Structure exactly:
# <headline, max 12 words>
**Bottom line:** <one sentence>

## What changed
- 3-5 bullets on new data and score moves, each with a citation.

## Segment board
- one line per segment: name, supply label (score, change), price label (score), confidence.

## Your BOM watchlist
- the 3-5 highest-risk parts with the driver behind each.

## Outlook
- 2-4 bullets from the "outlook" context: probability prices rise and supply tightens over 3/6 months, the
  80% range, whether each forecast has a tested edge or is the trend-following baseline, and the buy-timing calls.
  State probabilities as percentages and never present a baseline as a model edge.

## Negotiation angle
- 2-3 concrete, actionable points for this week's supplier conversations."""


def edition_for(now: datetime | None = None) -> str:
    h = (now or datetime.now(timezone.utc)).hour
    return "Asia open" if h >= 20 or h < 6 else "U.S. open"


def build_context(con, results: dict, outlook: dict | None = None) -> dict:
    as_of = results["as_of"]
    prev = scoring.previous(con, as_of)
    sources: list[dict] = []

    def cite(title: str, url: str | None) -> int:
        for i, s in enumerate(sources, 1):
            if s["url"] == url and url:
                return i
        sources.append({"title": title, "url": url})
        return len(sources)

    def delta(scope, key, field, cur):
        p = prev.get((scope, key))
        return None if not p else round(cur - p[field], 1)

    m = results["market"]
    market = {"supply_risk": m["supply_risk"], "price_trend": m["price_trend"], "confidence": m["confidence"],
              "labels": m["labels"], "supply_change": delta("market", "all", "supply_risk", m["supply_risk"]),
              "price_change": delta("market", "all", "price_trend", m["price_trend"]),
              "drivers": [{"signal": s["name"], "component": s["component"], "evidence": s["evidence"],
                           "pushes": "tighter/higher" if s["norm"] > 0 else "looser/lower",
                           "source": cite(s["name"] + " (" + s["source"] + ")", s["url"])} for s in m["signals"][:6]]}
    segs = []
    for k, s in results["segments"].items():
        segs.append({"segment": SEGMENTS[k]["label"], "supply_risk": s["supply_risk"],
                     "supply_label": s["labels"]["supply"], "supply_change": delta("segment", k, "supply_risk", s["supply_risk"]),
                     "price_trend": s["price_trend"], "price_label": s["labels"]["price"],
                     "confidence": s["labels"]["confidence"],
                     "top_driver": s["signals"][0]["evidence"] if s["signals"] else None})
    parts = sorted(results["parts"].values(), key=lambda p: p["supply_risk"], reverse=True)
    watch = []
    for p in parts[:5]:
        part_sigs = [s for s in p["signals"] if s["source"] in ("Octopart", "Part data", "Part history")]
        supply_sigs = [s for s in p["signals"] if s["component"] == "supply" and s["norm"] > 0]
        driver = (part_sigs or supply_sigs or p["signals"] or [{"evidence": "segment conditions"}])[0]
        watch.append({"mpn": p["mpn"], "segment": p["segment"], "supply_risk": p["supply_risk"],
                      "supply_label": p["labels"]["supply"], "price_trend": p["price_trend"],
                      "driver": driver["evidence"],
                      "lead_weeks": round(p["snapshot"]["lead_days"] / 7, 1) if p.get("snapshot") and p["snapshot"].get("lead_days") else None,
                      "price_vs_target_pct": p.get("price_vs_target_pct")})
    last = con.execute("SELECT MAX(created_at) FROM briefings").fetchone()[0] or "1970-01-01"
    news = db.rows(con, "SELECT title, url, source, published, supply_tone, price_tone FROM documents "
                        "WHERE fetched_at > ? AND (supply_tone != 0 OR price_tone != 0) "
                        "ORDER BY ABS(supply_tone) + ABS(price_tone) DESC, published DESC LIMIT 6", (last,))
    headlines = [{"title": n["title"], "outlet": n["source"], "supply_tone": n["supply_tone"],
                  "price_tone": n["price_tone"], "source": cite(f"{n['source']}: {n['title']}", n["url"])} for n in news]
    return {"as_of": as_of, "edition": edition_for(), "customer": config.CUSTOMER_NAME, "market": market,
            "segments": segs, "bom_watchlist": watch, "new_headlines": headlines,
            "outlook": outlook_context(outlook), "sources": sources}


def outlook_context(outlook: dict | None) -> dict | None:
    """Compact forecast summary for the reporter (keeps the LLM prompt small)."""
    if not outlook or not outlook.get("forecasts"):
        return None
    fc = []
    for f in outlook["forecasts"].values():
        fc.append({"what": f["label"], "p_up": f["prob_up"], "up_means": f["up"], "expected": f["point"],
                   "unit": f["unit"], "range80": [f["lo"], f["hi"]], "basis": f["basis"],
                   "tested_edge": f["metrics"].get("verdict") == "edge",
                   "base_period": f["base_period"]})
    calls = {}
    for mpn, p in (outlook.get("parts") or {}).items():
        calls.setdefault(p["action"], []).append(mpn)
    return {"forecasts": fc, "buy_timing": calls}


def template_briefing(ctx: dict) -> str:
    m = ctx["market"]
    ch = m["supply_change"]
    move = "" if ch is None else f" ({ch:+.1f} since last briefing)"
    top = max(ctx["segments"], key=lambda s: s["supply_risk"]) if ctx["segments"] else None
    head = (f"{top['segment']} leads supply risk; prices {m['labels']['price'].lower()}" if top
            else f"Supply {m['labels']['supply'].lower()}, prices {m['labels']['price'].lower()}")
    lines = [f"# {head}", "",
             f"**Bottom line:** Market supply risk is {m['labels']['supply'].lower()} at {m['supply_risk']:.0f}{move}; "
             f"price trend reads {m['labels']['price'].lower()} at {m['price_trend']:.0f} ({m['labels']['confidence'].lower()} confidence).",
             "", "## What changed"]
    for d in m["drivers"][:4]:
        lines.append(f"- {d['evidence']} [{d['source']}]")
    for h in ctx["new_headlines"][:2]:
        lines.append(f"- {h['outlet']}: {h['title']} [{h['source']}]")
    lines += ["", "## Segment board"]
    for s in sorted(ctx["segments"], key=lambda s: -s["supply_risk"]):
        c = "" if s["supply_change"] is None else f", {s['supply_change']:+.1f}"
        lines.append(f"- **{s['segment']}**: supply {s['supply_label'].lower()} ({s['supply_risk']:.0f}{c}); "
                     f"price {s['price_label'].lower()} ({s['price_trend']:.0f}); {s['confidence'].lower()} confidence")
    lines += ["", "## Your BOM watchlist"]
    for w in ctx["bom_watchlist"]:
        lt = f", {w['lead_weeks']:.0f} wk lead" if w["lead_weeks"] else ""
        lines.append(f"- **{w['mpn']}** ({w['segment'] or 'unclassified'}): supply risk {w['supply_risk']:.0f}{lt}. {w['driver']}")
    o = ctx.get("outlook")
    if o:
        lines += ["", "## Outlook"]
        for f in o["forecasts"]:
            if not f["what"].endswith("3 months"):
                continue
            rng = "" if f["range80"][0] is None else f", 80% range {f['range80'][0]:+.1f} to {f['range80'][1]:+.1f}{f['unit']}"
            basis = "tested model" if f["tested_edge"] else "trend-following baseline (no tested edge yet)"
            lines.append(f"- {f['what']}: {f['p_up']:.0%} chance {f['up_means']}; expected {f['expected']:+.1f}{f['unit']}{rng} ({basis}).")
        for action, mpns in o["buy_timing"].items():
            lines.append(f"- **{action}:** {', '.join(mpns[:6])}{' and others' if len(mpns) > 6 else ''}")
    lines += ["", "## Negotiation angle"]
    if m["supply_risk"] >= 54:
        lines.append("- Supply is tightening: prioritize allocation commitments and lead-time guarantees over unit price.")
    elif m["supply_risk"] <= 46:
        lines.append("- Supply is easing: push for price concessions and shorter lead-time commitments; you have leverage.")
    else:
        lines.append("- Conditions are balanced: lock pricing for the next quarter while terms are neutral.")
    if m["price_trend"] >= 53:
        lines.append("- Prices are firming: consider pulling forward buys on high-usage parts before increases land.")
    elif m["price_trend"] <= 47:
        lines.append("- Prices are softening: avoid long price locks; ask for quarterly re-pricing.")
    over = [w for w in ctx["bom_watchlist"] if (w.get("price_vs_target_pct") or 0) > 5]
    if over:
        lines.append(f"- {over[0]['mpn']} is trading {over[0]['price_vs_target_pct']:.0f}% above your target price; "
                     "bring the market data to the next review.")
    return "\n".join(lines)


def sources_md(ctx: dict) -> str:
    return "\n".join(f"{i}. [{s['title']}]({s['url']})" if s["url"] else f"{i}. {s['title']}"
                     for i, s in enumerate(ctx["sources"], 1))


def write(con, results: dict, use_llm: bool = True, outlook: dict | None = None) -> dict:
    ctx = build_context(con, results, outlook)
    body, model = None, "template"
    if use_llm and llm.available():
        prompt = (f"{FORMAT}\n\nEdition: {ctx['edition']} briefing for {ctx['customer']}.\n"
                  f"Context JSON:\n{json.dumps({k: v for k, v in ctx.items() if k != 'sources'}, separators=(',', ':'))}\n\n"
                  f"Numbered sources:\n" + "\n".join(f"[{i}] {s['title'][:120]}" for i, s in enumerate(ctx["sources"], 1)))
        try:
            body, model = llm.generate(prompt, system=SYSTEM, temperature=0.3)
        except Exception as e:
            log.warning("LLM briefing failed, using template: %s", e)
    if not body:
        body = template_briefing(ctx)
    headline = next((l[2:].strip() for l in body.splitlines() if l.startswith("# ")), "Substrate briefing")
    created = db.now_iso()
    row = {"id": f"{created}-{ctx['edition'].split()[0].lower().rstrip('.')}", "created_at": created,
           "edition": ctx["edition"], "headline": headline, "body_md": body + "\n\n**Sources**\n\n" + sources_md(ctx),
           "model": model, "sources": ctx["sources"]}
    db.upsert(con, "briefings", row)
    return row
