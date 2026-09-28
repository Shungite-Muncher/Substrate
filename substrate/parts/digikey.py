"""DigiKey Product Information API v4 (free with a developer account)."""
from __future__ import annotations

import time
from urllib.parse import quote

from .. import config, db, http
from .base import Offer, PartProvider, PartRecord, price_at

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DETAILS_URL = "https://api.digikey.com/products/v4/search/{pn}/productdetails"
_token: dict = {}


def _get_token() -> str:
    if _token.get("value") and _token.get("exp", 0) > time.time() + 60:
        return _token["value"]
    j = http.post(TOKEN_URL, data={"client_id": config.DIGIKEY_CLIENT_ID, "client_secret": config.DIGIKEY_CLIENT_SECRET,
                                   "grant_type": "client_credentials"}).json()
    _token.update(value=j["access_token"], exp=time.time() + int(j.get("expires_in", 600)))
    return _token["value"]


def parse(payload: dict, mpn: str) -> PartRecord | None:
    p = payload.get("Product")
    if not p:
        return None
    breaks = []
    for v in p.get("ProductVariations") or []:
        breaks += [(b.get("BreakQuantity"), b.get("UnitPrice")) for b in v.get("StandardPricing") or []]
    lead_weeks = p.get("ManufacturerLeadWeeks")
    try:
        lead = float(str(lead_weeks).split()[0]) * 7 if lead_weeks not in (None, "") else None
    except ValueError:
        lead = None
    price = price_at(breaks)
    stock = p.get("QuantityAvailable")
    return PartRecord(
        mpn=p.get("ManufacturerProductNumber") or mpn, provider="digikey",
        manufacturer=(p.get("Manufacturer") or {}).get("Name"),
        description=(p.get("Description") or {}).get("ProductDescription"),
        category=(p.get("Category") or {}).get("Name"), total_avail=stock, lead_days=lead, price_1k=price,
        currency="USD", lifecycle=(p.get("ProductStatus") or {}).get("Status"), url=p.get("ProductUrl"),
        sellers=[Offer(seller="DigiKey", stock=stock, lead_days=lead, price_1k=price, currency="USD")])


class DigiKeyProvider(PartProvider):
    name = "digikey"

    def available(self, con) -> bool:
        return bool(config.DIGIKEY_CLIENT_ID and config.DIGIKEY_CLIENT_SECRET)

    def lookup(self, con, mpn: str) -> PartRecord | None:
        r = http.session().get(DETAILS_URL.format(pn=quote(mpn, safe="")), timeout=30, headers={
            "Authorization": f"Bearer {_get_token()}", "X-DIGIKEY-Client-Id": config.DIGIKEY_CLIENT_ID,
            "X-DIGIKEY-Locale-Currency": "USD"})
        db.record_usage(con, self.name, calls=1)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return parse(r.json(), mpn)
