"""Mouser Search API (free key, ~1,000 calls/day). Fallback for lead time / stock / price."""
from __future__ import annotations

from .. import config, db, http
from .base import Offer, PartProvider, PartRecord, parse_lead_days, parse_money, price_at

URL = "https://api.mouser.com/api/v1/search/partnumber?apiKey={key}"
DAILY_LIMIT = 900


def parse(payload: dict, mpn: str) -> PartRecord | None:
    parts = (payload.get("SearchResults") or {}).get("Parts") or []
    exact = [p for p in parts if (p.get("ManufacturerPartNumber") or "").upper() == mpn.upper()]
    if not (exact or parts):
        return None
    p = (exact or parts)[0]
    breaks = [(int(b.get("Quantity") or 0), parse_money(b.get("Price"))) for b in p.get("PriceBreaks") or []]
    stock = p.get("AvailabilityInStock")
    try:
        stock = int(str(stock).replace(",", "")) if stock not in (None, "") else None
    except ValueError:
        stock = None
    lead = parse_lead_days(p.get("LeadTime"))
    price = price_at(breaks)
    currency = (p.get("PriceBreaks") or [{}])[0].get("Currency", "USD")
    return PartRecord(
        mpn=p.get("ManufacturerPartNumber") or mpn, provider="mouser",
        manufacturer=p.get("Manufacturer"), description=p.get("Description"), category=p.get("Category"),
        total_avail=stock, lead_days=lead, price_1k=price, currency=currency,
        lifecycle=p.get("LifecycleStatus"), url=p.get("ProductDetailUrl"),
        sellers=[Offer(seller="Mouser", stock=stock, lead_days=lead, moq=int(p.get("Min") or 0) or None,
                       price_1k=price, currency=currency)])


class MouserProvider(PartProvider):
    name = "mouser"

    def available(self, con) -> bool:
        return bool(config.MOUSER_API_KEY) and db.usage(con, self.name)["calls"] < DAILY_LIMIT * 28

    def lookup(self, con, mpn: str) -> PartRecord | None:
        body = {"SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": "Exact"}}
        r = http.post(URL.format(key=config.MOUSER_API_KEY), json=body)
        db.record_usage(con, self.name, calls=1)
        return parse(r.json(), mpn)
