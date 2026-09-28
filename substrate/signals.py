"""Turn accumulated raw data into named, weighted, normalized signals.

Every signal carries:
  norm    in [-1, 1]; + is bad for the buyer (tighter supply / higher prices)
  weight  its base importance in the component score
  evidence  a plain-English sentence with the actual numbers
  url     where the number came from
so any score can be decomposed back into exactly what moved it.

The macro indicator functions are pure (lists of (period, value) in, number out)
so the backtest can replay them on history truncated to what was known then.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from . import db
from .segments import COMPANY_NAMES, SEGMENTS

# Publication lag in months: data for month M is typically public in month M+lag.
PUB_LAG = {"capacity_utilization": 1, "ppi_semis": 1, "tsmc_revenue": 1, "sia_sales": 2}
HALF_LIFE_DAYS = {"FRED": 45, "TSMC": 40, "SIA": 60, "SEC": 120, "Trade press": 10, "Earnings releases": 60,
                  "Octopart": 7, "Part history": 14, "Part data": 7}


@dataclass
class Signal:
    name: str
    source: str
    component: str          # "supply" | "price"
    norm: float
    weight: float
    evidence: str
    raw: float | None = None
    as_of: str | None = None
    url: str | None = None
    age_days: float = 0.0

    def to_dict(self):
        d = asdict(self)
        d["norm"] = round(self.norm, 3)
        d["weight"] = round(self.weight, 3)
        return d


def _t(x: float, scale: float) -> float:
    return math.tanh(x / scale) if scale else 0.0


# ---- pure indicator math (shared with backtest) --------------------------------

def utilization_level(cu: list[tuple[str, float]], window: int = 120) -> tuple[float, float, float] | None:
    """z-score of latest capacity utilization vs trailing window. -> (norm, latest, mean)"""
    if len(cu) < 24:
        return None
    hist = [v for _, v in cu[-window:]]
    mu, sd = statistics.fmean(hist), statistics.pstdev(hist) or 1.0
    latest = cu[-1][1]
    return _t((latest - mu) / sd, 1.5), latest, mu


def utilization_momentum(cu: list[tuple[str, float]]) -> tuple[float, float] | None:
    if len(cu) < 4:
        return None
    d = cu[-1][1] - cu[-4][1]
    return _t(d, 2.0), d


def yoy_growth(series: list[tuple[str, float]], months: int = 3) -> float | None:
    """Growth of the last `months` summed vs the same months a year earlier (%)."""
    m = dict(series)
    periods = [p for p, _ in series]
    if len(periods) < 12 + months:
        return None
    last = periods[-months:]
    prev = [f"{int(p[:4]) - 1}{p[4:]}" for p in last]
    if not all(p in m for p in prev):
        return None
    a, b = sum(m[p] for p in last), sum(m[p] for p in prev)
    return (a / b - 1) * 100 if b else None


def tsmc_momentum(rev: list[tuple[str, float]]) -> tuple[float, float, float] | None:
    """3-month YoY revenue growth vs its own 5-year median -> excess loading."""
    g = yoy_growth(rev)
    if g is None:
        return None
    hist = [yoy_growth(rev[:i]) for i in range(max(15, len(rev) - 60), len(rev))]
    hist = [h for h in hist if h is not None]
    base = statistics.median(hist) if len(hist) >= 12 else 10.0
    return _t(g - base, 20.0), g, base


def sales_growth(sia: list[tuple[str, float]]) -> tuple[float, float] | None:
    if len(sia) < 13:
        return None
    m = dict(sia)
    p = sia[-1][0]
    prev = f"{int(p[:4]) - 1}{p[4:]}"
    if prev not in m:
        return None
    g = (sia[-1][1] / m[prev] - 1) * 100
    return _t(g - 8.0, 25.0), g   # ~8%/yr is the industry's long-run growth rate


def ppi_momentum(ppi: list[tuple[str, float]]) -> tuple[float, float] | None:
    """Annualized 3-month change in the semiconductor PPI (%).
    (3 months beat 6 months in the 2014-2026 backtest: 68% vs 56% directional hit rate.)"""
    if len(ppi) < 4:
        return None
    a, b = ppi[-1][1], ppi[-4][1]
    ann = ((a / b) ** 4 - 1) * 100 if b else 0.0
    return _t(ann, 5.0), ann


def truncate(series: list[tuple[str, float]], as_of: date, lag_months: int) -> list[tuple[str, float]]:
    """Only periods that would have been published by `as_of`."""
    cutoff_idx = as_of.year * 12 + as_of.month - 1 - lag_months
    return [(p, v) for p, v in series if int(p[:4]) * 12 + int(p[5:7]) - 1 <= cutoff_idx]


# ---- DB-backed builders ----------------------------------------------------------

def series(con, source: str, name: str) -> list[tuple[str, float]]:
    return [(r["period"], r["value"]) for r in con.execute(
        "SELECT period, value FROM observations WHERE source=? AND series=? ORDER BY period", (source, name))]


def _url(con, source: str, name: str) -> str | None:
    r = con.execute("SELECT url FROM observations WHERE source=? AND series=? ORDER BY period DESC LIMIT 1",
                    (source, name)).fetchone()
    return r["url"] if r else None


def _period_age(period: str) -> float:
    y, m = int(period[:4]), int(period[5:7])
    mid = date(y, m, 15)
    return max((date.today() - mid).days, 0)


def _iso_age(iso: str | None) -> float:
    if not iso:
        return 999.0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 999.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - dt).total_seconds() / 86400, 0.0)


def _month_name(period: str) -> str:
    return date(int(period[:4]), int(period[5:7]), 1).strftime("%b %Y")


def macro_signals(con, calibration: dict | None = None) -> list[Signal]:
    cal = (calibration or {}).get("price_weight_factors", {})
    out: list[Signal] = []
    cu = series(con, "fred", "capacity_utilization")
    lvl = utilization_level(cu)
    if lvl:
        norm, latest, mu = lvl
        ev = f"Semiconductor capacity utilization {latest:.1f}% in {_month_name(cu[-1][0])} vs 10-yr avg {mu:.1f}%"
        common = dict(raw=latest, as_of=cu[-1][0], url="https://fred.stlouisfed.org/series/CAPUTLG3344S",
                      age_days=_period_age(cu[-1][0]), evidence=ev)
        out.append(Signal("Fab utilization level", "FRED", "supply", norm, 0.20, **common))
        out.append(Signal("Fab utilization level", "FRED", "price", norm,
                          0.10 * cal.get("utilization_level", 1.0), **common))
    mom = utilization_momentum(cu)
    if mom:
        norm, d = mom
        out.append(Signal("Fab utilization momentum", "FRED", "supply", norm, 0.10,
                          f"Utilization {'up' if d >= 0 else 'down'} {abs(d):.1f} pts over 3 months",
                          raw=d, as_of=cu[-1][0], url="https://fred.stlouisfed.org/series/CAPUTLG3344S",
                          age_days=_period_age(cu[-1][0])))
    rev = series(con, "tsmc", "monthly_revenue")
    tm = tsmc_momentum(rev)
    if tm:
        norm, g, base = tm
        out.append(Signal("TSMC foundry loading", "TSMC", "supply", norm, 0.15,
                          f"TSMC 3-month revenue {g:+.1f}% YoY through {_month_name(rev[-1][0])} "
                          f"vs a typical {base:+.1f}%", raw=g, as_of=rev[-1][0], url=_url(con, "tsmc", "monthly_revenue"),
                          age_days=_period_age(rev[-1][0])))
    sia = series(con, "sia", "global_sales_3mma")
    sg = sales_growth(sia)
    if sg:
        norm, g = sg
        common = dict(raw=g, as_of=sia[-1][0], url=_url(con, "sia", "global_sales_3mma"),
                      age_days=_period_age(sia[-1][0]),
                      evidence=f"Global chip sales ${sia[-1][1]:.1f}B in {_month_name(sia[-1][0])}, {g:+.1f}% YoY (SIA/WSTS)")
        out.append(Signal("Industry demand growth", "SIA", "supply", norm, 0.10, **common))
        out.append(Signal("Industry demand growth", "SIA", "price", norm,
                          0.15 * cal.get("sales_growth", 1.0), **common))
    ppi = series(con, "fred", "ppi_semis")
    pm = ppi_momentum(ppi)
    if pm:
        norm, ann = pm
        out.append(Signal("Producer price momentum", "FRED", "price", norm, 0.30 * cal.get("ppi_momentum", 1.0),
                          f"Semiconductor PPI {ann:+.1f}% annualized over 3 months to {_month_name(ppi[-1][0])}",
                          raw=ann, as_of=ppi[-1][0], url="https://fred.stlouisfed.org/series/PCU33443344",
                          age_days=_period_age(ppi[-1][0])))
    return out


def inventory_days(con, ticker: str) -> list[tuple[str, float]]:
    inv = dict(series(con, "sec", f"{ticker}:inventory"))
    cogs = dict(series(con, "sec", f"{ticker}:cogs"))
    return [(q, inv[q] / cogs[q] * 91) for q in sorted(inv) if q in cogs and cogs[q] > 0]


def inventory_signal(con, tickers: list[str], label: str, weight_supply: float, weight_price: float) -> list[Signal]:
    parts, notes, ages = [], [], []
    for t in tickers:
        dio = inventory_days(con, t)
        if len(dio) < 5:
            continue
        latest_q, latest = dio[-1]
        base = statistics.fmean(v for _, v in dio[-9:-1])
        chg = latest / base - 1
        parts.append(_t(-chg, 0.15))
        notes.append(f"{COMPANY_NAMES.get(t, t)} {latest:.0f} days ({chg:+.0%} vs 2-yr avg, {latest_q})")
        ages.append(_quarter_age(latest_q))
    if not parts:
        return []
    norm = statistics.fmean(parts)
    ev = f"{label} inventory: " + "; ".join(notes)
    common = dict(evidence=ev, raw=norm, age_days=statistics.fmean(ages),
                  url="https://www.sec.gov/edgar/search/")
    return [Signal("Supplier inventory days", "SEC", "supply", norm, weight_supply, **common),
            Signal("Supplier inventory days", "SEC", "price", norm, weight_price, **common)]


def _quarter_age(q: str) -> float:
    y, n = int(q[:4]), int(q[-1])
    end = date(y, n * 3, 28)
    return max((date.today() - end).days, 0)


def document_signals(con, segment: str | None, source_kind: str) -> list[Signal]:
    """Aggregate tone from trade press ('news') or earnings releases ('sec-8k')."""
    if source_kind == "sec-8k":
        docs = db.rows(con, "SELECT * FROM documents WHERE source='sec-8k'")
        name, src, half, horizon = "Earnings-release commentary", "Earnings releases", 60, 150
    else:
        docs = db.rows(con, "SELECT * FROM documents WHERE source!='sec-8k'")
        name, src, half, horizon = "Trade-press tone", "Trade press", 10, 30
    picked = []
    for d in docs:
        age = _iso_age(d["published"])
        if age > horizon:
            continue
        segs = json.loads(d["segments"] or "[]")
        if segment and segment not in segs:
            continue
        picked.append((d, age))
    out = []
    for comp, col, base_w in (("supply", "supply_tone", 0.15), ("price", "price_tone", 0.20)):
        toned = [(d, a) for d, a in picked if d[col]]
        if not toned:
            continue
        ws = [math.exp(-a / half) for _, a in toned]
        mean = sum(w * d[col] for w, (d, _) in zip(ws, toned)) / sum(ws)
        shrink = min(1.0, len(toned) / 5)  # a single story shouldn't swing a score
        top = sorted(toned, key=lambda x: (abs(x[0][col]), -x[1]), reverse=True)[:3]
        ev = (f"{len(toned)} {'stories' if source_kind != 'sec-8k' else 'releases'} in {horizon} days, net "
              f"{'tightening' if comp == 'supply' and mean > 0 else 'easing' if comp == 'supply' else 'up' if mean > 0 else 'down'}"
              f" ({mean:+.2f}). e.g. " + " | ".join(f"“{d['title'][:90]}”" for d, _ in top))
        out.append(Signal(name, src, comp, mean * shrink, base_w if source_kind != "sec-8k" else 0.10, ev,
                          raw=mean, url=top[0][0]["url"], age_days=statistics.fmean(a for _, a in toned)))
    return out


def segment_signals(con, segment: str, calibration: dict | None = None) -> list[Signal]:
    spec = SEGMENTS[segment]
    sigs = []
    for s in macro_signals(con, calibration):
        if s.name == "TSMC foundry loading":
            s.weight *= spec["foundry_exposure"]
        sigs.append(s)
    sigs += inventory_signal(con, spec["companies"], spec["label"], 0.20, 0.10)
    sigs += document_signals(con, segment, "news")
    sigs += document_signals(con, segment, "sec-8k")
    return [s for s in sigs if s.weight > 0]


def market_signals(con, calibration: dict | None = None) -> list[Signal]:
    sigs = macro_signals(con, calibration)
    sigs += document_signals(con, None, "news")
    sigs += document_signals(con, None, "sec-8k")
    return sigs


BASELINE_LEAD_DAYS = 84  # ~12 weeks: a "normal" factory lead time for standard semis


def part_signals(snap: dict | None, hist: list[dict], bom_row: dict | None) -> list[Signal]:
    if not snap:
        return []
    src = "Octopart" if snap["provider"] == "octopart" else "Part data"
    url = snap.get("url")
    age = _iso_age(snap["day"] + "T12:00:00+00:00")
    out = []
    lead = snap.get("lead_days")
    if lead is not None:
        out.append(Signal("Factory lead time", src, "supply", _t(lead - BASELINE_LEAD_DAYS, 60), 0.30,
                          f"Factory lead time {lead / 7:.0f} weeks vs a ~12-week norm ({snap['provider']})",
                          raw=lead, as_of=snap["day"], url=url, age_days=age))
    older = [h for h in hist if h.get("day") and h["day"] <= _days_before(snap["day"], 14)]
    if older and lead is not None and older[-1].get("lead_days") is not None:
        d = lead - older[-1]["lead_days"]
        out.append(Signal("Lead-time trend", "Part history", "supply", _t(d, 21), 0.15,
                          f"Lead time {'up' if d >= 0 else 'down'} {abs(d) / 7:.1f} weeks since {older[-1]['day']}",
                          raw=d, as_of=snap["day"], age_days=age))
    stock = snap.get("total_avail")
    if stock is not None:
        qty = (bom_row or {}).get("annual_qty")
        if qty:
            weeks = stock / (qty / 52)
            norm = _t(8 - weeks, 8)
            ev = f"Channel stock {stock:,} units = {weeks:.1f} weeks of your usage"
        else:
            norm = _t(4 - math.log10(stock + 1), 1.5)
            ev = f"Channel stock {stock:,} units across authorized sellers"
        out.append(Signal("Channel inventory cover", src, "supply", norm, 0.20, ev, raw=stock,
                          as_of=snap["day"], url=url, age_days=age))
    sellers = snap.get("sellers") or []
    if sellers:
        n = len({s["seller"] for s in sellers if (s.get("stock") or 0) > 0})
        out.append(Signal("Sources with stock", src, "supply", _t(3 - n, 2), 0.05,
                          f"{n} authorized seller(s) currently holding stock", raw=n, as_of=snap["day"],
                          url=url, age_days=age))
    lc = (snap.get("lifecycle") or "").lower()
    if any(k in lc for k in ("nrnd", "not recommended", "obsolete", "eol", "end of life", "last time")):
        out.append(Signal("Lifecycle status", src, "supply", 1.0, 0.15, f"Lifecycle: {snap['lifecycle']}",
                          as_of=snap["day"], url=url, age_days=age))
    price = snap.get("price_1k")
    olderp = [h for h in older if h.get("price_1k")]
    if price and olderp:
        pct = price / olderp[-1]["price_1k"] - 1
        out.append(Signal("Part price trend", "Part history", "price", _t(pct, 0.10), 0.25,
                          f"1k-unit price ${price:.3f}, {pct:+.1%} since {olderp[-1]['day']}", raw=pct,
                          as_of=snap["day"], age_days=age))
    return out


def _days_before(day: str, n: int) -> str:
    return date.fromordinal(date.fromisoformat(day).toordinal() - n).isoformat()
