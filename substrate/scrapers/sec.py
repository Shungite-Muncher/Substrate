"""SEC EDGAR (free, keyless, but needs a contact email in the User-Agent).

1. XBRL financials -> quarterly inventory and cost of revenue for every company in
   the segment map plus the customer's peers, which gives days-of-inventory: the
   cleanest public read on whether a supplier (or a competitor) is long or short.
2. 8-K Item 2.02 earnings releases (Exhibit 99.1) -> the free, redistributable
   stand-in for earnings-call commentary on lead times, utilization and pricing.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from .. import config, db, http, tone
from ..segments import COMPANY_NAMES, SEGMENTS, all_tickers, segments_for_text

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{name}"

CONCEPTS = {
    "inventory": ["InventoryNet", "InventoryGross"],
    "cogs": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
}
_FRAME = re.compile(r"^CY(\d{4})Q([1-4])I?$")


def _headers() -> dict:
    return {"User-Agent": f"Substrate market-intelligence demo {config.SEC_CONTACT_EMAIL}"}


def cik_map() -> dict[str, int]:
    j = http.get(TICKERS_URL, headers=_headers()).json()
    return {v["ticker"]: int(v["cik_str"]) for v in j.values()}


def quarterly_points(concept_json: dict) -> dict[str, float]:
    """Pull calendar-quarter values out of an XBRL companyconcept payload.
    SEC 'frame' tags already align each fact to one calendar quarter, deduplicated."""
    out = {}
    for fact in concept_json.get("units", {}).get("USD", []):
        m = _FRAME.match(fact.get("frame", "") or "")
        if m:
            out[f"{m.group(1)}Q{m.group(2)}"] = float(fact["val"])
    return out


def _financials(con, ticker: str, cik: int) -> int:
    n = 0
    for series, tags in CONCEPTS.items():
        for tag in tags:
            try:
                j = http.get(CONCEPT_URL.format(cik=cik, tag=tag), headers=_headers()).json()
            except Exception:
                continue  # company doesn't report this tag; try the next synonym
            points = quarterly_points(j)
            if not points:
                continue
            for period, value in points.items():
                db.upsert(con, "observations", {
                    "source": "sec", "series": f"{ticker}:{series}", "period": period, "value": value,
                    "unit": "USD", "meta": {"tag": tag, "cik": cik},
                    "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
                    "fetched_at": db.now_iso()})
                n += 1
            break
    return n


def _earnings_releases(con, ticker: str, cik: int, since: date) -> int:
    sub = http.get(SUBMISSIONS_URL.format(cik=cik), headers=_headers()).json()
    rec = sub["filings"]["recent"]
    n = 0
    for form, filed, items, acc in zip(rec["form"], rec["filingDate"], rec["items"], rec["accessionNumber"]):
        if form != "8-K" or "2.02" not in (items or "") or date.fromisoformat(filed) < since:
            continue
        acc_nd = acc.replace("-", "")
        try:
            idx = http.get(INDEX_URL.format(cik=cik, acc=acc_nd), headers=_headers()).json()
        except Exception as e:
            log.info("sec index %s %s: %s", ticker, acc, e)
            continue
        names = [i["name"] for i in idx.get("directory", {}).get("item", [])]
        ex = next((x for x in names if re.search(r"ex[-_]?99(?:[-_.]?0?1)?", x, re.I) and x.lower().endswith((".htm", ".html"))), None)
        if not ex:
            continue
        url = DOC_URL.format(cik=cik, acc=acc_nd, name=ex)
        if con.execute("SELECT 1 FROM documents WHERE url=?", (url,)).fetchone():
            continue
        try:
            text = BeautifulSoup(http.get(url, headers=_headers()).text, "html.parser").get_text(" ", strip=True)
        except Exception as e:
            log.info("sec doc %s: %s", url, e)
            continue
        sentences = tone.key_sentences(text, limit=8)
        t = tone.score(" ".join(sentences))
        segs = [s for s, spec in SEGMENTS.items() if ticker in spec["companies"]] or segments_for_text(text[:20000])
        db.upsert(con, "documents", {
            "url": url, "source": "sec-8k", "published": filed,
            "title": f"{COMPANY_NAMES.get(ticker, ticker)} earnings release ({filed})",
            "summary": " ".join(sentences)[:1500], "segments": segs,
            "tags": {"ticker": ticker, "matches": t["matches"]},
            "supply_tone": t["supply_tone"], "price_tone": t["price_tone"], "fetched_at": db.now_iso()})
        n += 1
    return n


def run(con, backfill: bool = False) -> dict:
    if not config.SEC_CONTACT_EMAIL:
        log.warning("SEC_CONTACT_EMAIL not set; skipping EDGAR (SEC requires a contact email in the User-Agent)")
        return {"source": "sec", "skipped": "SEC_CONTACT_EMAIL not set"}
    ciks = cik_map()
    tickers = list(dict.fromkeys(all_tickers() + config.CUSTOMER_PEERS))
    since = date.today() - timedelta(days=400 if backfill else 120)
    fin = docs = 0
    for t in tickers:
        cik = ciks.get(t)
        if not cik:
            log.warning("no CIK for %s", t)
            continue
        try:
            fin += _financials(con, t, cik)
            docs += _earnings_releases(con, t, cik, since)
        except Exception as e:
            log.warning("sec %s: %s", t, e)
    return {"source": "sec", "financial_rows": fin, "earnings_releases": docs}
