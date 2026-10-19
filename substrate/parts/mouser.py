"""Mouser Search API (free key, ~1,000 calls/day). Fallback for lead time / stock / price."""
from __future__ import annotations

from .. import config, db, http
from .base import Offer, PartProvider, PartRecord, earliest, parse_lead_days, parse_money, price_at

URL = "https://api.mouser.com/api/v1/search/partnumber?apiKey={key}"
DAILY_LIMIT = 900


def _int(v) -> int | None:
    try:
        return int(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


def _listing(p: dict) -> dict:
    breaks = [(int(b.get("Quantity") or 0), parse_money(b.get("Price"))) for b in p.get("PriceBreaks") or []]
    lead = parse_lead_days(p.get("LeadTime"))
    oo = [x for x in p.get("AvailabilityOnOrder") or [] if isinstance(x, dict)]
    return {"p": p, "breaks": breaks, "stock": _int(p.get("AvailabilityInStock")),
            "lead": lead if lead else None,            # Mouser says "0 Days" for parts it no longer carries
            "on_order": sum(_int(x.get("Quantity")) or 0 for x in oo), "etas": [x.get("Date") for x in oo]}


def parse(payload: dict, mpn: str) -> PartRecord | None:
    """Mouser often lists one manufacturer part under several Mouser numbers (reels, cut tape,
    legacy listings), some of them empty placeholders. Combine every exact match: stock and
    on-order add up, price is the best qty-1 / 1k offer, lead time comes from a stocked listing."""
    parts = (payload.get("SearchResults") or {}).get("Parts") or []
    exact = [p for p in parts if (p.get("ManufacturerPartNumber") or "").upper() == mpn.upper()]
    pool = [_listing(p) for p in (exact or parts[:1])]
    if not pool:
        return None
    live = [x for x in pool if x["breaks"] or x["stock"]]
    best = max(live or pool, key=lambda x: (x["stock"] or 0, bool(x["breaks"])))
    p = best["p"]
    stocks = [x["stock"] for x in pool if x["stock"] is not None]
    stock = sum(stocks) if stocks else None
    on_order = sum(x["on_order"] for x in pool) or None
    on_order_eta = earliest(d for x in pool for d in x["etas"])
    p1 = [price_at(x["breaks"], 1) for x in pool if x["breaks"]]
    pk = [price_at(x["breaks"]) for x in pool if x["breaks"]]
    lead = best["lead"] or next((x["lead"] for x in live if x["lead"]), None)
    price = min(pk) if pk else None
    currency = (p.get("PriceBreaks") or [{}])[0].get("Currency", "USD") if p.get("PriceBreaks") else "USD"
    lifecycle = p.get("LifecycleStatus") or next((x["p"].get("LifecycleStatus") for x in pool if x["p"].get("LifecycleStatus")), None)
    return PartRecord(
        mpn=p.get("ManufacturerPartNumber") or mpn, provider="mouser",
        manufacturer=p.get("Manufacturer"), description=p.get("Description"), category=p.get("Category"),
        total_avail=stock, lead_days=lead, price_1k=price, price_1=min(p1) if p1 else None,
        on_order=on_order, on_order_eta=on_order_eta, currency=currency,
        lifecycle=lifecycle, url=p.get("ProductDetailUrl"),
        sellers=[Offer(seller=f"Mouser ({x['p'].get('MouserPartNumber') or 'listing'})", stock=x["stock"], lead_days=x["lead"],
                       on_order=x["on_order"] or None, on_order_eta=earliest(x["etas"]),
                       moq=_int(x["p"].get("Min")), price_1k=price_at(x["breaks"]) if x["breaks"] else None,
                       currency=currency) for x in (live or pool)])


class MouserProvider(PartProvider):
    name = "mouser"

    def available(self, con) -> bool:
        return bool(config.MOUSER_API_KEY) and db.usage(con, self.name)["calls"] < DAILY_LIMIT * 28

    def lookup(self, con, mpn: str) -> PartRecord | None:
        body = {"SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": "Exact"}}
        r = http.post(URL.format(key=config.MOUSER_API_KEY), json=body)
        db.record_usage(con, self.name, calls=1)
        return parse(r.json(), mpn)
