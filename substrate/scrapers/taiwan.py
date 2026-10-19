"""Monthly revenue for Taiwan's chip supply chain, from the Market Observation Post
System (MOPS). Every Taiwan-listed company files monthly revenue by the 10th, which makes
this the fastest free read on foundry, memory and packaging loading after TSMC itself."""
from __future__ import annotations

import logging
from datetime import date

from bs4 import BeautifulSoup

from .. import db, http

log = logging.getLogger(__name__)

# ticker -> (name, market, segment it informs)
COMPANIES = {
    "2303": ("UMC", "sii", "mcu"),             # mature-node foundry: MCUs, analog, power
    "5347": ("Vanguard", "otc", "analog"),     # 8-inch foundry: PMICs, discretes
    "6770": ("PSMC", "sii", "mcu"),            # mature-node foundry + memory
    "2408": ("Nanya", "sii", "memory"),        # DRAM
    "2344": ("Winbond", "sii", "memory"),      # DRAM + NOR flash
    "2337": ("Macronix", "sii", "memory"),     # NOR / NAND
    "3711": ("ASE", "sii", "logic"),           # packaging & test
    "2454": ("MediaTek", "sii", "logic"),      # fabless SoCs
    "5483": ("SAS", "otc", "power"),           # silicon wafers incl. SiC
}
URL = "https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc}_{month}_0.html"


def parse(html: bytes, wanted: set[str]) -> dict[str, float]:
    """Ticker -> revenue for the month (NT$ thousand)."""
    soup = BeautifulSoup(html.decode("big5", errors="ignore"), "html.parser")
    out = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all("td")]
        if len(cells) >= 3 and cells[0] in wanted:
            try:
                out[cells[0]] = float(cells[2].replace(",", ""))
            except ValueError:
                continue
    return out


def _months(back: int) -> list[tuple[int, int]]:
    t = date.today()
    y, m = t.year, t.month - 1          # the current month is never published yet
    out = []
    for _ in range(back):
        if m == 0:
            y, m = y - 1, 12
        out.append((y, m))
        m -= 1
    return out


def run(con, backfill: bool = False) -> dict:
    have = {r["period"] for r in db.rows(con, "SELECT DISTINCT period FROM observations WHERE source='mops'")}
    n, fetched = 0, 0
    for y, m in _months(48 if backfill else 3):
        period = f"{y}-{m:02d}"
        if period in have and not backfill and (y, m) != _months(1)[0]:
            continue
        for market in ("sii", "otc"):
            wanted = {t for t, (_, mk, _) in COMPANIES.items() if mk == market}
            url = URL.format(market=market, roc=y - 1911, month=m)
            try:
                vals = parse(http.get(url).content, wanted)
                fetched += 1
            except Exception as e:
                log.info("mops %s %s: %s", market, period, e)
                continue
            for t, v in vals.items():
                name, _, seg = COMPANIES[t]
                db.upsert(con, "observations", {"source": "mops", "series": f"{t}:revenue", "period": period,
                                                "value": v, "unit": "NT$ thousand",
                                                "meta": {"company": name, "segment": seg}, "url": url,
                                                "fetched_at": db.now_iso()})
                n += 1
    return {"source": "mops", "pages": fetched, "rows": n}
