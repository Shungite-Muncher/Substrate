"""element14 / Newark / Farnell Product Search API (free key). A third distributor's stock,
lead time and price breaks, so one distributor's gap or outlier doesn't define a part."""
from __future__ import annotations

from .. import config, db, http
from .base import Offer, PartProvider, PartRecord, price_at

URL = "https://api.element14.com/catalog/products"
DAILY_LIMIT = 900


def parse(payload: dict, mpn: str) -> PartRecord | None:
    ret = payload.get("manufacturerPartNumberSearchReturn") or payload.get("premierFarnellPartNumberReturn") or {}
    prods = ret.get("products") or []
    exact = [p for p in prods if (p.get("translatedManufacturerPartNumber") or "").upper() == mpn.upper()]
    p = (exact or prods or [None])[0]
    if not p:
        return None
    stock_info = p.get("stock") or {}
    stock = stock_info.get("level")
    lead_days = stock_info.get("leastLeadTime") or stock_info.get("leadTime")
    breaks = [(int(x.get("from") or 0), float(x.get("cost") or 0) or None) for x in p.get("prices") or []]
    price = price_at(breaks)
    return PartRecord(
        mpn=p.get("translatedManufacturerPartNumber") or mpn, provider="element14",
        manufacturer=p.get("brandName") or p.get("vendorName"), description=p.get("displayName"),
        total_avail=int(stock) if stock is not None else None,
        lead_days=float(lead_days) if lead_days else None, price_1k=price, price_1=price_at(breaks, 1),
        lifecycle=p.get("productStatus"), url=None,
        sellers=[Offer(seller="Newark/element14", stock=int(stock) if stock is not None else None,
                       lead_days=float(lead_days) if lead_days else None, price_1k=price, currency="USD")])


class Element14Provider(PartProvider):
    name = "element14"

    def available(self, con) -> bool:
        return bool(config.ELEMENT14_API_KEY) and db.usage(con, self.name)["calls"] < DAILY_LIMIT * 28

    def lookup(self, con, mpn: str) -> PartRecord | None:
        r = http.get(URL, params={"term": f"manuPartNum:{mpn}", "storeInfo.id": config.ELEMENT14_STORE,
                                  "resultsSettings.offset": 0, "resultsSettings.numberOfResults": 3,
                                  "resultsSettings.responseGroup": "large", "callInfo.responseDataFormat": "json",
                                  "callInfo.apiKey": config.ELEMENT14_API_KEY})
        db.record_usage(con, self.name, calls=1)
        return parse(r.json(), mpn)
