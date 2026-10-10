"""Command line: `python -m substrate <command>`."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import backtest, briefing, config, db, export, parts, predict, scoring, scrapers
from . import bom as bom_mod

log = logging.getLogger("substrate")


def _scrape(con, names, backfill) -> list[dict]:
    out = []
    for name in names:
        try:
            r = scrapers.ALL[name].run(con, backfill=backfill)
        except Exception as e:
            log.exception("scraper %s failed", name)
            r = {"source": name, "error": str(e)}
        con.commit()
        log.info("scrape %s", r)
        out.append(r)
    return out


def _ensure_bom(con) -> None:
    if not bom_mod.load(con):
        n = bom_mod.ingest(con, config.BOM_DIR / "sample_bom.csv")
        log.info("loaded sample BOM (%s parts) for %s", n, config.CUSTOMER_NAME)


def cmd_run(a) -> None:
    with db.open_db() as con:
        _ensure_bom(con)
        names = [n for n in scrapers.ALL if n not in (a.skip or [])]
        scraped = _scrape(con, names, a.backfill)
        pr = parts.refresh(con, [r["mpn"] for r in bom_mod.load(con)])
        log.info("parts %s", pr)
        report = backtest.run(con)
        results = scoring.run(con, backtest.load_calibration())
        outlook = predict.run(con, results)
        b = briefing.write(con, results, use_llm=not a.no_llm, outlook=outlook)
        ex = export.run(con, results, report, outlook)
        db.dump_warehouse(con)
    print(json.dumps({"scraped": scraped, "parts": pr, "briefing": b["headline"], "model": b["model"],
                      "export": ex, "market": {k: results["market"][k] for k in ("supply_risk", "price_trend", "confidence")}},
                     indent=2, default=str))


def cmd_scrape(a) -> None:
    unknown = set(a.names) - set(scrapers.ALL)
    if unknown:
        sys.exit(f"unknown scraper(s): {', '.join(unknown)}")
    with db.open_db() as con:
        print(json.dumps(_scrape(con, a.names or list(scrapers.ALL), a.backfill), indent=2))
        db.dump_warehouse(con)


def cmd_bom(a) -> None:
    with db.open_db() as con:
        n = bom_mod.ingest(con, a.path, a.customer)
        print(f"ingested {n} parts for {a.customer or config.CUSTOMER_NAME}")
        if a.refresh:
            print(json.dumps(parts.refresh(con, [r["mpn"] for r in bom_mod.load(con, a.customer)]), indent=2))
        db.dump_warehouse(con)


def cmd_part(a) -> None:
    with db.open_db() as con:
        snap = parts.lookup(con, a.mpn, force=a.force)
        bom_row = next((r for r in bom_mod.load(con) if r["mpn"] == parts.normalize_mpn(a.mpn)), None)
        res = scoring.score_part(con, parts.normalize_mpn(a.mpn), bom_row, {}, backtest.load_calibration())
        db.dump_warehouse(con)
    if not snap:
        print("No part data (configure NEXAR_*, MOUSER_API_KEY or DIGIKEY_* in .env); showing segment-level view.")
    print(f"{res['mpn']}  segment={res['segment']}  supply risk {res['supply_risk']:.0f} ({res['labels']['supply']})  "
          f"price {res['price_trend']:.0f} ({res['labels']['price']})  confidence {res['confidence']:.0f}")
    for s in res["signals"][:8]:
        print(f"  {s['component']:6} {s['norm']:+.2f} x{s['weight']:.2f}  {s['name']}: {s['evidence']}")


def cmd_forecast(a) -> None:
    with db.open_db() as con:
        results = scoring.run(con, backtest.load_calibration())
        o = predict.run(con, results)
        db.dump_warehouse(con)
    for f in o.get("forecasts", {}).values():
        m = f["metrics"]
        rng = "" if f["lo"] is None else f" [80%: {f['lo']:+.1f} to {f['hi']:+.1f}]"
        print(f"{f['label']:34} P({f['up']}) {f['prob_up']:.0%}  expected {f['point']:+.2f}{f['unit']}{rng}  "
              f"<{f['basis']}; verdict {m.get('verdict')}, skill vs trend {m.get('skill_vs_trend')}>")
    for mpn, p in o.get("parts", {}).items():
        lp = p["lead_projection"]
        lead = f"  lead {lp['now_weeks']}->{lp['in_13w_weeks']} wk" if lp else ""
        print(f"  {mpn:24} {p['action']:26} P(price up) {p['p_price_up']:.0%}{lead}")
    print("ledger:", {k: v for k, v in o.get("ledger", {}).items() if k != "recent"})


def cmd_backtest(a) -> None:
    with db.open_db() as con:
        r = backtest.run(con)
    print(json.dumps({k: v for k, v in r.items() if k != "series"}, indent=2))


def cmd_brief(a) -> None:
    with db.open_db() as con:
        results = scoring.run(con, backtest.load_calibration())
        outlook = predict.run(con, results)
        b = briefing.write(con, results, use_llm=not a.no_llm, outlook=outlook)
        export.run(con, results, outlook=outlook)
        db.dump_warehouse(con)
    print(b["body_md"])


def cmd_decide(a) -> None:
    """Decision log: the customer's own record of what they did and why, with the scores at the time."""
    with db.open_db() as con:
        res = scoring.score_part(con, parts.normalize_mpn(a.mpn), None, {}, backtest.load_calibration())
        db.upsert(con, "decisions", {"created_at": db.now_iso(), "mpn": res["mpn"], "action": a.action,
                                     "rationale": a.why, "snapshot": {k: res[k] for k in ("supply_risk", "price_trend", "confidence")}
                                     | {"top_signals": res["signals"][:5]}})
        db.dump_warehouse(con)
    print("logged")


def cmd_serve(a) -> None:
    from .server import serve
    serve(a.port)


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="substrate", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="full pipeline: scrape -> parts -> backtest -> score -> brief -> export")
    r.add_argument("--backfill", action="store_true", help="pull multi-year history (first run)")
    r.add_argument("--no-llm", action="store_true", help="use the template writer")
    r.add_argument("--skip", nargs="*", choices=list(scrapers.ALL))
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("scrape")
    s.add_argument("names", nargs="*", help=f"any of {', '.join(scrapers.ALL)} (default: all)")
    s.add_argument("--backfill", action="store_true")
    s.set_defaults(fn=cmd_scrape)

    b = sub.add_parser("bom", help="ingest a BOM CSV")
    b.add_argument("path")
    b.add_argument("--customer")
    b.add_argument("--refresh", action="store_true", help="look up every part now")
    b.set_defaults(fn=cmd_bom)

    pt = sub.add_parser("part", help="look up and score one part")
    pt.add_argument("mpn")
    pt.add_argument("--force", action="store_true")
    pt.set_defaults(fn=cmd_part)

    sub.add_parser("backtest").set_defaults(fn=cmd_backtest)
    sub.add_parser("forecast", help="run the predictive layer and print forecasts + buy timing").set_defaults(fn=cmd_forecast)

    br = sub.add_parser("brief", help="score and write a briefing from existing data")
    br.add_argument("--no-llm", action="store_true")
    br.set_defaults(fn=cmd_brief)

    d = sub.add_parser("decide", help="log a sourcing decision")
    d.add_argument("mpn")
    d.add_argument("action")
    d.add_argument("--why", required=True)
    d.set_defaults(fn=cmd_decide)

    sv = sub.add_parser("serve", help="local dashboard + chat/part API")
    sv.add_argument("--port", type=int, default=8787)
    sv.set_defaults(fn=cmd_serve)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main(sys.argv[1:])
