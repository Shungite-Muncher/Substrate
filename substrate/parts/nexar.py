"""Octopart data through the Nexar GraphQL API (the Octopart API's successor).

Cost control: Nexar bills per *part object returned*. We request exactly one part
per lookup (limit: 1), cache snapshots for PART_REFRESH_DAYS, and stop at
NEXAR_MONTHLY_PART_BUDGET. With a free Evaluation app (a small lifetime allowance)
that makes the allowance last; the free Mouser/DigiKey APIs cover the rest.
"""
from __future__ import annotations

import logging
import re
import time

from .. import config, db, http
from .base import Offer, PartProvider, PartRecord, earliest, price_at

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
            onOrderQuantity
            onOrderEta
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
    offers, stock_total, all_breaks = [], 0, []
    for s in p.get("sellers") or []:
        for o in s.get("offers") or []:
            breaks = [(pr["quantity"], pr["price"]) for pr in o.get("prices") or [] if pr.get("currency") in (None, "USD")]
            all_breaks += breaks
            offers.append(Offer(seller=s["company"]["name"], stock=o.get("inventoryLevel"),
                                lead_days=o.get("factoryLeadDays"), on_order=o.get("onOrderQuantity"),
                                on_order_eta=o.get("onOrderEta"), moq=o.get("moq"),
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
        price_1=min((pr for q, pr in all_breaks if q == 1), default=None),
        on_order=sum(o.on_order or 0 for o in offers) or None,
        on_order_eta=earliest(o.on_order_eta for o in offers),
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
        query, payload = QUERY, {}
        for _ in range(5):           # Nexar's schema varies by plan; drop any field it rejects and retry
            status, payload = self._query(query, mpn)
            bad = rejected(payload)
            if not bad:
                break
            log.warning("nexar rejected %s; retrying without it", ", ".join(sorted(bad)))
            query = strip_fields(query, bad)
        if payload.get("errors"):
            log.warning("nexar %s: %s", mpn, str(payload["errors"][:1])[:300])
        rec = parse(payload, mpn) if payload.get("data") else None
        db.record_usage(con, self.name, calls=1, parts=1 if rec else 0)
        return rec

    def _query(self, query: str, mpn: str) -> tuple[int, dict]:
        # GraphQL validation errors come back as HTTP 400 with the reason in the body, so read it
        r = http.session().post(GRAPHQL_URL, json={"query": query, "variables": {"q": mpn}},
                                headers={"Authorization": f"Bearer {_get_token()}"}, timeout=60)
        try:
            return r.status_code, r.json()
        except ValueError:
            r.raise_for_status()
            return r.status_code, {}


_FIELD = re.compile(r"[`\"](\w+)[`\"] does not exist|Cannot query field [`\"](\w+)[`\"]|[Ff]ield [`\"](\w+)[`\"] .*not", re.I)
_ARG = re.compile(r"argument [`\"](\w+)[`\"]", re.I)


def rejected(payload: dict) -> set[str]:
    out = set()
    for e in payload.get("errors") or []:
        msg = str(e.get("message", ""))
        m = _FIELD.search(msg)
        if m:
            out.add(next(g for g in m.groups() if g))
        a = _ARG.search(msg)
        if a:
            out.add("(" + a.group(1))
    return out


def strip_fields(query: str, names: set[str]) -> str:
    for n in names:
        if n.startswith("("):                       # an unsupported argument: drop the whole (...) list
            query = re.sub(r"\(\s*" + re.escape(n[1:]) + r"[^)]*\)", "", query)
            continue
        # the field plus any { ... } selection under it (two levels of nesting)
        query = re.sub(r"\b" + re.escape(n) + r"\b(\s*\((?:[^()]*)\))?(\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\})?", "", query)
    return query
