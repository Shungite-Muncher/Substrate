"""Negotiation-prep chatbot logic for the local server. The Cloudflare Worker
(worker/src/index.js) mirrors this prompt so hosted and local answers match."""
from __future__ import annotations

import json

from . import config, llm

SYSTEM = """You are Substrate, a negotiation-prep analyst for semiconductor procurement teams.
You have the customer's live market context (scores 0-100, 50 = neutral; supply risk higher = tighter;
price trend higher = rising) and their BOM. Rules:
- Ground every claim in the provided context. If the data doesn't cover something, say so plainly.
- Never claim knowledge of any company's confidential pricing or contract terms; position inferences come from public signals only.
- Be concrete: numbers, part numbers, what to ask the supplier for, and fallback positions.
- For negotiation briefs use sections: Situation, Leverage (ours / theirs), Asks, Walk-away & alternatives, Talking points.
- For scenario questions ("what if lead times hit 30 weeks"), reason explicitly from the scoring inputs.
- Keep answers under 350 words unless asked for more. Markdown."""


def load_context() -> dict:
    try:
        return json.loads((config.SITE_DATA_DIR / "context.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def compact_context(ctx: dict, question: str, budget: int = llm.MAX_PROMPT_CHARS - 5000) -> str:
    """Fit the context into the free-tier token budget: parts named in the question
    stay in full; the rest of the BOM and the briefing shrink first."""
    q = question.upper()
    c = dict(ctx)
    c["latest_briefing"] = (c.get("latest_briefing") or "").split("**Sources**")[0][:2000]
    for level in range(4):
        bom = c.get("bom", [])
        if level >= 1:
            bom = [p if p["mpn"] in q else {k: p[k] for k in ("mpn", "segment", "supply_risk", "price_trend")} for p in bom]
        if level >= 2:
            c["headlines"] = [h["title"] for h in ctx.get("headlines", [])[:6]]
            c.pop("peer_inventory_days", None)
        if level >= 3:
            c["latest_briefing"] = c["latest_briefing"][:600]
            c["segments"] = {k: {kk: v[kk] for kk in ("label", "supply_risk", "price_trend", "labels")} | {"drivers": v["drivers"][:1]}
                             for k, v in ctx.get("segments", {}).items()}
        out = json.dumps({**c, "bom": bom}, separators=(",", ":"))
        if len(out) <= budget:
            return out
    return out[:budget]


def _find_part(ctx: dict, text: str) -> dict | None:
    t = text.upper()
    return next((p for p in ctx.get("bom", []) if p["mpn"] in t), None)


def fallback_answer(ctx: dict, question: str) -> str:
    """Deterministic negotiation brief when no LLM key is configured."""
    p = _find_part(ctx, question)
    m = ctx.get("market", {})
    if not p:
        segs = sorted(ctx.get("segments", {}).values(), key=lambda s: -s["supply_risk"])
        lines = [f"**Market:** supply risk {m.get('supply_risk', 50):.0f} ({m.get('labels', {}).get('supply', '?')}), "
                 f"price trend {m.get('price_trend', 50):.0f} ({m.get('labels', {}).get('price', '?')}).", "",
                 "**Tightest segments:**"]
        lines += [f"- {s['label']}: supply {s['supply_risk']:.0f}, price {s['price_trend']:.0f}. {s['drivers'][0] if s['drivers'] else ''}"
                  for s in segs[:3]]
        lines += ["", "_Name a part number from your BOM for a full negotiation brief. "
                      "(Template mode: set GROQ_API_KEY for conversational answers.)_"]
        return "\n".join(lines)
    tight = p["supply_risk"] >= 54
    rising = p["price_trend"] >= 53
    lt = f"{p['lead_weeks']:.0f}-week lead time" if p.get("lead_weeks") else "lead time not yet sampled"
    lines = [f"## Negotiation brief: {p['mpn']}", "",
             f"**Situation.** Supply risk {p['supply_risk']:.0f}, price trend {p['price_trend']:.0f}; {lt}.",
             *[f"- {d}" for d in p["drivers"]], "",
             "**Leverage.** " + ("The supplier holds more leverage while supply is tight; trade volume commitments for allocation."
                                 if tight else "Supply is not tight; you have room to push on price and terms."), "",
             "**Asks.**"]
    if tight:
        lines += ["- Written allocation / capacity commitment covering your forecast", "- Lead-time cap with notice period for changes"]
    else:
        lines += ["- Price reduction backed by the market data above", "- Shorter lead-time commitment and buffer stock at supplier"]
    if rising:
        lines.append("- Lock current pricing for 2 quarters before increases flow through")
    else:
        lines.append("- Quarterly re-pricing rather than a long lock")
    if p.get("target_price") and p.get("price_1k"):
        gap = (p["price_1k"] / p["target_price"] - 1) * 100
        lines.append(f"- Market 1k price ${p['price_1k']:.3f} vs your target ${p['target_price']:.3f} ({gap:+.0f}%)")
    lines += ["", "**Walk-away.** Qualify a second source or pin-compatible alternate before the next review.",
              "", "_Template mode: set GROQ_API_KEY for conversational answers._"]
    return "\n".join(lines)


def answer(messages: list[dict], part: dict | None = None) -> dict:
    ctx = load_context()
    question = messages[-1]["content"] if messages else ""
    if not llm.available():
        return {"reply": fallback_answer(ctx, question), "model": "template"}
    convo = "\n".join(f"{m['role'].upper()}: {m['content'][:1500]}" for m in messages[-6:])
    prompt = (f"Customer market context (JSON):\n{compact_context(ctx, question)}\n\n"
              + (f"Live part lookup:\n{json.dumps(part, separators=(',', ':'))[:2500]}\n\n" if part else "")
              + f"Conversation:\n{convo}\n\nAnswer the last USER message.")
    try:
        text, model = llm.generate(prompt, system=SYSTEM, lite=True, temperature=0.4)
        return {"reply": text, "model": model}
    except Exception as e:
        return {"reply": fallback_answer(ctx, question) + f"\n\n_(LLM unavailable: {e.__class__.__name__})_",
                "model": "template"}
