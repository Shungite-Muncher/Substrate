from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass
class Offer:
    seller: str
    stock: int | None = None
    lead_days: float | None = None
    on_order: int | None = None        # PBO: units on order / backorder from the manufacturer
    on_order_eta: str | None = None
    moq: int | None = None
    price_1k: float | None = None
    currency: str | None = None


@dataclass
class PartRecord:
    mpn: str
    provider: str
    manufacturer: str | None = None
    description: str | None = None
    category: str | None = None
    total_avail: int | None = None
    lead_days: float | None = None     # factory lead time
    price_1k: float | None = None      # unit price at ~1k quantity
    price_1: float | None = None       # unit price at quantity 1
    on_order: int | None = None        # PBO: total units on order / backorder across sellers
    on_order_eta: str | None = None    # earliest expected arrival of on-order stock
    currency: str | None = "USD"
    lifecycle: str | None = None
    # trade & tariff
    country_of_origin: str | None = None
    fab_country: str | None = None        # "country of diffusion": where the wafer was made
    assembly_country: str | None = None
    us_hts: str | None = None             # US tariff classification (8541 discretes, 8542 ICs)
    eccn: str | None = None               # US export-control classification
    tariff_note: str | None = None        # distributor's tariff surcharge notice
    tariff_pct: float | None = None
    notice: str | None = None             # e.g. "not sold in your region"
    url: str | None = None
    sellers: list[Offer] = field(default_factory=list)

    def to_row(self, day: str) -> dict:
        d = asdict(self)
        d["day"] = day
        d["sellers"] = [asdict(s) for s in self.sellers]
        return d


class PartProvider:
    name = "base"

    def available(self, con) -> bool:
        raise NotImplementedError

    def lookup(self, con, mpn: str) -> PartRecord | None:
        raise NotImplementedError


def price_at(breaks: list[tuple[int, float]], qty: int = 1000) -> float | None:
    """Unit price for the largest price break <= qty (or the smallest break available)."""
    breaks = sorted((q, p) for q, p in breaks if q and p)
    if not breaks:
        return None
    eligible = [p for q, p in breaks if q <= qty]
    return eligible[-1] if eligible else breaks[0][1]


def parse_money(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"[\d.,]+", str(s))
    if not m:
        return None
    txt = m.group(0)
    # "1.234,56" (EU) vs "1,234.56" (US)
    if "," in txt and "." in txt and txt.rfind(",") > txt.rfind("."):
        txt = txt.replace(".", "").replace(",", ".")
    else:
        txt = txt.replace(",", "")
    try:
        return float(txt)
    except ValueError:
        return None


def parse_lead_days(s) -> float | None:
    """'12 Weeks' / '84 days' / 12 -> days."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(week|wk|day|month)?", str(s), re.I)
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "day").lower()
    return n * 7 if unit.startswith("w") else n * 30 if unit.startswith("m") else n


def earliest(dates) -> str | None:
    ds = sorted(str(d)[:10] for d in dates if d)
    return ds[0] if ds else None


def normalize_mpn(mpn: str) -> str:
    return re.sub(r"\s+", "", mpn or "").upper()
