"""Logistics and fab-infrastructure signals:

  * Drewry World Container Index (weekly, US$ per 40ft container): shipping cost and
    congestion between Asian factories and buyers.
  * Taipower's published operating reserve margin: Taiwan's fabs run on that grid, and
    an orange/red reserve day is a real disruption risk.
  * Public-holiday calendars for the countries parts are made in (Nager.Date): Lunar New
    Year and Golden Week shutdowns add predictable weeks to lead times.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from .. import db, http

log = logging.getLogger(__name__)

WCI_URL = "https://www.drewry.co.uk/supply-chain-advisors/supply-chain-expertise/world-container-index-assessed-by-drewry"
TAIPOWER_URL = "https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/loadpara.json"
HOLIDAY_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{cc}"
# origin countries in typical BOMs -> ISO code (Taiwan isn't in Nager; its Lunar New Year matches China's)
HOLIDAY_COUNTRIES = {"CN": "China", "JP": "Japan", "KR": "Korea", "MY": "Malaysia", "PH": "Philippines",
                     "TH": "Thailand", "VN": "Vietnam", "MX": "Mexico", "DE": "Germany", "US": "United States"}
# multi-day factory shutdowns worth warning about (single public days are noise)
SHUTDOWNS = re.compile(r"spring festival|lunar new year|chinese new year|national day|golden week|"
                       r"showa|constitution|children's day|seollal|chuseok|tet|songkran", re.I)


def wci(html: str) -> float | None:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    for pat in (r"composite[^.$]{0,120}?\$\s?([\d,]{4,6})\s*(?:per 40ft|/40ft|per FEU)",
                r"([\d,]{4,6})\s*美元/FEU", r"\$\s?([\d,]{4,6})\s*(?:per 40ft|/40ft|per FEU)"):
        m = re.search(pat, text, re.I)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def run(con, backfill: bool = False) -> dict:
    out = {"source": "logistics"}
    today = date.today().isoformat()
    try:
        v = wci(http.get(WCI_URL).text)
        if v:
            db.upsert(con, "observations", {"source": "drewry", "series": "wci_composite", "period": today, "value": v,
                                            "unit": "US$ per 40ft", "meta": {}, "url": WCI_URL, "fetched_at": db.now_iso()})
        out["wci"] = v
    except Exception as e:
        log.info("drewry: %s", e)
    try:
        rec = http.get(TAIPOWER_URL).json()["records"]
        fc = next((r for r in rec if "fore_peak_resv_rate" in r), {})
        if fc:
            db.upsert(con, "observations", {"source": "taipower", "series": "reserve_margin", "period": today,
                                            "value": float(fc["fore_peak_resv_rate"]), "unit": "% operating reserve",
                                            "meta": {"indicator": fc.get("fore_peak_resv_indicator")}, "url": TAIPOWER_URL,
                                            "fetched_at": db.now_iso()})
            out["taipower_reserve"] = float(fc["fore_peak_resv_rate"])
    except Exception as e:
        log.info("taipower: %s", e)
    n = 0
    for year in (date.today().year, date.today().year + 1):
        for cc in HOLIDAY_COUNTRIES:
            try:
                r = http.get(HOLIDAY_URL.format(year=year, cc=cc))
                hols = r.json() if r.status_code == 200 and r.text else []
            except Exception as e:
                log.info("holidays %s %s: %s", cc, year, e)
                continue
            for h in hols:
                if not SHUTDOWNS.search(f"{h.get('name', '')} {h.get('localName', '')}"):
                    continue
                db.upsert(con, "observations", {"source": "holidays", "series": f"{cc}:{h['name']}", "period": h["date"],
                                                "value": 1, "unit": "holiday", "meta": {"country": HOLIDAY_COUNTRIES[cc]},
                                                "url": "https://date.nager.at", "fetched_at": db.now_iso()})
                n += 1
    out["holidays"] = n
    return out
