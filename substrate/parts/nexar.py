"""Octopart data through the Nexar GraphQL API (the Octopart API's successor).

Cost control: Nexar bills per *part object returned*. We request exactly one part
per lookup (limit: 1), cache snapshots for PART_REFRESH_DAYS, and stop at
NEXAR_MONTHLY_PART_BUDGET. With a free Evaluation app (a small lifetime allowance)
that makes the allowance last; the free Mouser/DigiKey APIs cover the rest.
"""
from __future__ import annotations

import logging
import time

from .. import config, db, http
from .base import Offer, PartProvider, PartRecord, price_at

log = logging.getLogger(__name__)

TOKEN_URL = "https://identity.nexar.com/connect/token"
GRAPHQL_URL = "https://api.nexar.com/graphql/"

QUERY = """
query SubstratePart($q: String!) {
  supSearchMpn(q: $q, limit: 1) {
    hits
    results {
      part {
        mpn
        manufacturer { name }
        shortDescription
        category { name path }
        octopartUrl
        totalAvail
        estimatedFactoryLeadDays
        medianPrice1000 { price currency }
        specs { attribute { shortname } displayValue }
        sellers(authorizedOnly: true) {
          company { name }
          offers {
            inventoryLevel
            factoryLeadDays
            moq
            prices { quantity price currency }
          }
        }
      }
    }
  }
}
"""

_token: dict = {}


def _get_token() -> str:
    if _token.get("value") and _token.get("exp", 0) > time.time() + 60:
        return _token["value"]
    r = http.post(TOKEN_URL, data={"grant_type": "client_credentials",
                                   "client_id": config.NEXAR_CLIENT_ID,
                                   "client_secret": config.NEXAR_CLIENT_SECRET})
    j = r.json()
    _token.update(value=j["access_token"], exp=time.time() + int(j.get("expires_in", 3600)))
    return _token["value"]


def parse(payload: dict, mpn: str) -> PartRecord | None:
    results = (payload.get("data", {}).get("supSearchMpn") or {}).get("results") or []
    if not results:
        return None
    p = results[0]["part"]
    offers, stock_total = [], 0
    for s in p.get("sellers") or []:
        for o in s.get("offers") or []:
            breaks = [(pr["quantity"], pr["price"]) for pr in o.get("prices") or [] if pr.get("currency") in (None, "USD")]
            offers.append(Offer(seller=s["company"]["name"], stock=o.get("inventoryLevel"),
                                lead_days=o.get("factoryLeadDays"), moq=o.get("moq"),
                                price_1k=price_at(breaks), currency="USD"))
            stock_total += max(o.get("inventoryLevel") or 0, 0)
    lifecycle = next((sp["displayValue"] for sp in p.get("specs") or []
                      if sp["attribute"]["shortname"] in ("lifecyclestatus", "lifecycle_status")), None)
    median = p.get("medianPrice1000") or {}
    return PartRecord(
        mpn=p.get("mpn") or mpn, provider="octopart",
        manufacturer=(p.get("manufacturer") or {}).get("name"),
        description=p.get("shortDescription"),
        category=(p.get("category") or {}).get("path") or (p.get("category") or {}).get("name"),
        total_avail=p.get("totalAvail") if p.get("totalAvail") is not None else stock_total,
        lead_days=p.get("estimatedFactoryLeadDays"),
        price_1k=median.get("price"), currency=median.get("currency") or "USD",
        lifecycle=lifecycle, url=p.get("octopartUrl"), sellers=offers[:15])


class NexarProvider(PartProvider):
    name = "octopart"

    def available(self, con) -> bool:
        if not (config.NEXAR_CLIENT_ID and config.NEXAR_CLIENT_SECRET):
            return False
        used = db.usage(con, self.name)["parts"]
        if used >= config.NEXAR_MONTHLY_PART_BUDGET:
            log.info("Nexar monthly part budget reached (%s)", used)
            return False
        return True

    def lookup(self, con, mpn: str) -> PartRecord | None:
        r = http.post(GRAPHQL_URL, json={"query": QUERY, "variables": {"q": mpn}},
                      headers={"Authorization": f"Bearer {_get_token()}"})
        payload = r.json()
        if payload.get("errors"):
            log.warning("nexar %s: %s", mpn, payload["errors"][:1])
        rec = parse(payload, mpn)
        db.record_usage(con, self.name, calls=1, parts=1 if rec else 0)
        return rec
