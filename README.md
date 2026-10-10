# Substrate

**Market intelligence for semiconductor procurement.** Substrate turns fragmented public signals into scored, decision-ready intelligence. It also writes news-style briefings twice a day, timed to the Asian and U.S. market opens. The signals include TSMC monthly revenue, SIA global sales, Fed and BLS industry data, SEC filings, trade press, and Octopart lead-time data.

Everything runs on free tiers.

## What's in the demo

| Piece | Where | What it does |
|---|---|---|
| **Scrapers** | `substrate/scrapers/` | TSMC monthly revenue, SIA/WSTS global sales, FRED (fab utilization, industrial production, semiconductor PPI), SEC EDGAR (supplier and peer inventory, 8-K earnings releases), 8 trade-press feeds |
| **Accumulator** | `substrate/db.py`, `data/warehouse/` | SQLite working store, persisted as sorted JSONL in git. Every run adds to the record, and the history stays auditable and diffable. |
| **Octopart port** | `substrate/parts/` | Nexar GraphQL (Octopart) with a hard part budget, plus free Mouser and DigiKey fallbacks behind one interface. Snapshots build each part's own lead-time and price history. |
| **Scoring engine** | `substrate/signals.py`, `scoring.py` | Supply risk, price trend and confidence (0–100). Every score decomposes into named, weighted inputs with the evidence sentence and source link. |
| **Predictive layer** | `substrate/predict/` | 3- and 6-month forecasts for chip prices and fab utilization: probability of a rise, expected change, and an 80% range. Walk-forward tested since 2010 against trend-following and long-run-average baselines. Turned into segment outlooks, part lead-time projections and buy-timing calls, all logged in a forecast ledger. |
| **Backtest & calibration** | `substrate/backtest.py` | Walk-forward test of the price score against the next 3 months of the semiconductor PPI, 2014 to today, with publication lags applied. The lead-time backtest switches on as part history accumulates. |
| **Automated reporter** | `substrate/briefing.py` | Twice-daily briefings. Groq (gpt-oss-120b, free tier) writes the prose, but every number comes from the engine. A deterministic template writer is the fallback. |
| **BOM ingestion** | `substrate/bom.py` | Any CSV with a part-number column. Headers are matched loosely (MPN, Mfr Part Number, EAU, Unit Cost…). |
| **Dashboard** | `site/` (GitHub Pages) | Briefings, segment scores with drill-down, BOM watchlist with scenario sliders, part lookup, negotiation chat, peer inventory, backtest |
| **Chat + live lookup API** | `worker/` (Cloudflare Workers) | Negotiation briefs and scenario Q&A. Keys stay server-side. |
| **Chrome extension** | `extension/` | Toolbar popup or right-click any part number for instant scores |

```
 cron 23:30 & 13:00 UTC (GitHub Actions)
        │
  scrapers ──► accumulator (SQLite ⇄ data/warehouse/*.jsonl, committed)
        │              │
  Octopart/Mouser ─────┤
                       ▼
            signals ─► scores ─► briefing (Groq | template)
                       │
                       ▼
             site/data/*.json ─► GitHub Pages dashboard ◄── Chrome extension
                       ▲                     │
                       └──── Cloudflare Worker (/api/chat, /api/part)
```

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # fill in whatever keys you have (all optional)
python -m substrate run --backfill   # first run pulls ~12 years of history
python -m substrate serve            # http://localhost:8787
```

Other commands:

```bash
python -m substrate part TPS62130RGTR           # look up and score one part, print the drivers
python -m substrate bom my_bom.csv --refresh    # ingest a customer BOM and fetch part data
python -m substrate backtest                    # rerun calibration and backtest
python -m substrate forecast                    # forecasts, buy-timing calls, ledger status
python -m substrate brief                       # rescore and write a briefing from stored data
python -m substrate decide MT40A1G16TB-062E:F "locked 2Q pricing" --why "price trend 86, supply rising"
```

## Keys (all free, all optional)

| Key | Unlocks | Without it |
|---|---|---|
| `SEC_CONTACT_EMAIL` | Supplier and peer inventory days, earnings-release commentary | SEC is skipped. SEC requires a contact email in the User-Agent. |
| `GROQ_API_KEY` ([Groq console](https://console.groq.com/keys)) | LLM-written briefings, conversational chat (free tier: 1K requests/day) | Template writer, deterministic negotiation briefs |
| `NEXAR_CLIENT_ID/SECRET` ([Nexar](https://portal.nexar.com)) | Octopart lead time, stock, sellers, median price | Falls back to Mouser/DigiKey |
| `MOUSER_API_KEY` / `DIGIKEY_*` | Free part data, about 1,000 calls/day | Part scores use segment signals only |

Note on Octopart cost: Nexar's free Evaluation app has a small lifetime part allowance, and each returned part counts. Substrate asks for exactly one part per lookup, caches snapshots for `PART_REFRESH_DAYS`, and stops at `NEXAR_MONTHLY_PART_BUDGET`.

## Deploy (free)

1. **Secrets:** Repo → Settings → Secrets and variables → Actions. Add the keys above. Optional variables: `SUBSTRATE_CUSTOMER`, `SUBSTRATE_PEERS`.
2. **Pages:** Settings → Pages → Source: GitHub Actions.
3. **Run:** Actions → *Briefings pipeline* → Run workflow. After that it runs at 23:30 and 13:00 UTC.
4. **Chat API:** see [`worker/README.md`](worker/README.md), then set the Worker URL in `site/config.js`.
5. **Extension:** `chrome://extensions` → Developer mode → Load unpacked → `extension/`. Set URLs in its options.

## How the scores work

Each input is normalized to a value between −1 and +1, where + is bad for the buyer (tighter supply or higher prices). Each input also carries a base weight. Score = 50 + 50 × weighted mean. Confidence is 45% source agreement, 35% source coverage and 20% freshness.

| Input | Supply wt | Price wt | Source |
|---|---|---|---|
| Fab utilization vs 10-yr norm | 0.20 | 0.10 × cal | Fed G.17 via FRED |
| Utilization 3-mo momentum | 0.10 | | FRED |
| TSMC revenue growth vs its 5-yr norm | 0.15 × foundry exposure | | TSMC IR |
| Industry sales growth vs ~8% trend | 0.10 | 0.15 | SIA/WSTS |
| Semiconductor PPI 3-mo momentum | | 0.30 × cal | BLS via FRED |
| Supplier days of inventory vs 2-yr avg | 0.20 | 0.10 | SEC XBRL |
| Trade-press tone (decayed, shrunk when sparse) | 0.15 | 0.20 | RSS |
| Earnings-release commentary | 0.10 | 0.10 | SEC 8-K |
| Part: factory lead time vs 12 wk | 0.30 | | Octopart / distributors |
| Part: lead-time trend, stock cover, sources, lifecycle | 0.15 / 0.20 / 0.05 / 0.15 | | Part history |
| Part: price trend | | 0.25 | Part history |

At part level, segment evidence counts at half weight next to the part's own data.

**Backtest honesty:** from 2014 to 2026, macro-only price scoring called 3-month PPI direction about 68% of the time. Naive "trend continues" persistence scored about 70%. Macro data alone doesn't beat persistence, so the edge has to come from part-level history and news, which the accumulator is building. The dashboard's Backtest tab shows the live numbers.

## The predictive layer

The scores above describe the market today. The predictive layer forecasts where it goes next.

**Targets.** For chip prices (BLS semiconductor PPI) and fab utilization (Fed G.17), 3 and 6 months ahead, it forecasts:

- the probability of a rise (from a regularized logistic regression),
- the expected change (from a ridge regression),
- and an 80% range (adaptive conformal intervals that widen after misses).

**Inputs.** Six indicators, each computed only from data published by the forecast date:

- PPI 3-month momentum and 12-month change
- fab utilization against its 10-year norm, and its 3-month change
- chip industrial-production growth
- TSMC revenue growth (history back to 1999)

Inputs are capped at 3 standard deviations, so moves like 2026's chip-price spike can't produce absurd certainty.

**How it's judged.** Every month since 2010, each model is retrained using only outcomes public at that point. It's then scored on probability quality (Brier score) against two free alternatives: a calibrated "recent trend continues" model and the long-run average. A forecast is labelled as having an **edge** only if it beats both. Otherwise the trend-following baseline is what flows into outlooks and buy-timing calls, and the dashboard says so.

Current results, out of sample 2010–2026:

| Forecast | Verdict | Probability skill vs trend | 80% range held |
|---|---|---|---|
| Chip prices, 3 months | **Edge** | +4.1% | 76% |
| Chip prices, 6 months | Matches trend | +0.9% | 74% |
| Fab utilization, 3 months | Matches trend | −4.8% | 80% |
| Fab utilization, 6 months | Matches trend | −0.4% | 80% |

The model configuration (6 inputs, capping, regularization strength 10) was chosen from 12 variants on these same out-of-sample years. So treat the edge as provisional until the live ledger confirms it.

**From forecasts to decisions.**

- *Segment and part probabilities* shift the tested market forecast according to how far their current signals sit from the market's. This tilt isn't separately validated.
- *Lead-time projections* come from the segment supply outlook. They're a heuristic until part history exists.
- *Buy-timing calls*: **Buy ahead / lock pricing** when the chance of a 3-month price rise is at least 60%; **Wait / keep terms short** when it's 40% or less and supply isn't tightening; otherwise **Hold course**.

**Forecast ledger.** Every forecast is written to `data/warehouse/forecasts.jsonl` the first time it's made, and can't be revised later. It's scored automatically once the outcome is published: market forecasts when Fed/BLS data lands, part lead-time projections after 13 weeks. This live track record is the validation milestone from the venture summary.

Substrate holds no inventory and takes no position on what customers buy.
