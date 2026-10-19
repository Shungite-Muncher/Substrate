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
                  "Octopart": 7, "Part history": 14, "Part data": 7, "Census": 60, "FX": 30, "Korea customs": 20,
                  "Federal Register": 45, "MOPS": 40, "DRAMeXchange": 7, "USGS": 7}
SEC_FILINGS = ("sec-8k", "sec-10q")
NON_PRESS = SEC_FILINGS + ("fedreg", "usgs")


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
    if source_kind == "sec-8k":   # earnings releases + 10-Q/10-K backlog and pricing commentary
        docs = db.rows(con, f"SELECT * FROM documents WHERE source IN ({','.join('?' * len(SEC_FILINGS))})", SEC_FILINGS)
        name, src, half, horizon = "Earnings & filing commentary", "Earnings releases", 60, 150
    else:
        docs = db.rows(con, f"SELECT * FROM documents WHERE source NOT IN ({','.join('?' * len(NON_PRESS))})", NON_PRESS)
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
    for s in macro_signals(con, calibration) + extra_macro_signals(con):
        if s.name == "TSMC foundry loading":
            s.weight *= spec["foundry_exposure"]
        if s.name == "Korea export momentum" and segment == "memory":
            s.weight *= 2           # Korea's chip exports are mostly memory
        sigs.append(s)
    sigs += inventory_signal(con, spec["companies"], spec["label"], 0.20, 0.10)
    sigs += taiwan_signal(con, segment)
    if segment == "memory":
        sigs += memory_spot_signal(con)
    sigs += quake_signal(con, segment)
    sigs += document_signals(con, segment, "news")
    sigs += document_signals(con, segment, "sec-8k")
    return [s for s in sigs if s.weight > 0]


def market_signals(con, calibration: dict | None = None) -> list[Signal]:
    sigs = macro_signals(con, calibration) + extra_macro_signals(con)
    sigs += quake_signal(con, None)
    sigs += document_signals(con, None, "news")
    sigs += document_signals(con, None, "sec-8k")
    return sigs


# ---- second-wave sources: Census, FX, Korea, Federal Register, MOPS, DRAMeXchange, USGS ----

def _avg_last(s: list[tuple[str, float]], n: int = 3) -> float | None:
    return statistics.fmean(v for _, v in s[-n:]) if len(s) >= n else None


def extra_macro_signals(con) -> list[Signal]:
    out: list[Signal] = []
    no, sh = dict(series(con, "fred", "components_new_orders")), dict(series(con, "fred", "components_shipments"))
    months = sorted(set(no) & set(sh))
    if len(months) >= 3:
        btb = statistics.fmean(no[m] / sh[m] for m in months[-3:])
        out.append(Signal("Component book-to-bill", "Census", "supply", _t(btb - 1, 0.05), 0.10,
                          f"Electronic-component new orders / shipments {btb:.2f} (3-mo avg to {_month_name(months[-1])}); "
                          f"above 1.00 means orders outrunning output", raw=btb, as_of=months[-1],
                          url="https://fred.stlouisfed.org/series/A34HNO", age_days=_period_age(months[-1])))
    uo = series(con, "fred", "components_unfilled_orders")
    if len(uo) >= 7 and uo[-7][1]:
        g = ((uo[-1][1] / uo[-7][1]) ** 2 - 1) * 100
        out.append(Signal("Component order backlog", "Census", "supply", _t(g, 15), 0.08,
                          f"Electronic-component unfilled orders {g:+.1f}% annualized over 6 months to {_month_name(uo[-1][0])}",
                          raw=g, as_of=uo[-1][0], url="https://fred.stlouisfed.org/series/A34HUO", age_days=_period_age(uo[-1][0])))
    inv, shp = dict(series(con, "fred", "electronics_inventories")), dict(series(con, "fred", "electronics_shipments"))
    m2 = sorted(set(inv) & set(shp))
    if len(m2) >= 60:
        ratios = [inv[m] / shp[m] for m in m2]
        base = statistics.fmean(ratios[-60:])
        dev = ratios[-1] / base - 1
        common = dict(raw=ratios[-1], as_of=m2[-1], url="https://fred.stlouisfed.org/series/A34STI",
                      age_days=_period_age(m2[-1]),
                      evidence=f"Electronics inventory-to-shipments {ratios[-1]:.2f} vs 5-yr avg {base:.2f} ({dev:+.0%}); "
                               f"lean inventories mean less buffer")
        out.append(Signal("Electronics inventory cover", "Census", "supply", _t(-dev, 0.05), 0.08, **common))
        out.append(Signal("Electronics inventory cover", "Census", "price", _t(-dev, 0.05), 0.05, **common))
    fx = []
    for code in ("twd", "jpy", "krw"):
        s_ = series(con, "fred", f"fx_{code}")
        if len(s_) >= 4 and s_[-4][1]:
            fx.append((code.upper(), (s_[-1][1] / s_[-4][1] - 1) * 100, s_[-1][0]))
    if fx:
        chg = statistics.fmean(c for _, c, _ in fx)
        # weaker Asian currencies (more per US$) make Asian-made parts cheaper in dollars
        out.append(Signal("Asian currency moves", "FX", "price", _t(-chg, 3), 0.08,
                          "3-mo change per US$: " + ", ".join(f"{c} {v:+.1f}%" for c, v, _ in fx)
                          + (" (Asian currencies weaker: dollar prices ease)" if chg > 0 else " (Asian currencies stronger: dollar prices firm)"),
                          raw=chg, as_of=fx[0][2], url="https://fred.stlouisfed.org/series/DEXTAUS", age_days=_period_age(fx[0][2])))
    k = _korea_latest(con)
    if k:
        val, label, period, url, age = k
        common = dict(raw=val, as_of=period, url=url, age_days=age,
                      evidence=f"Korea {label} {val:+.0f}% YoY (customs flash via wire reports); earliest hard read on chip demand")
        out.append(Signal("Korea export momentum", "Korea customs", "supply", _t(val - 10, 40), 0.08, **common))
        out.append(Signal("Korea export momentum", "Korea customs", "price", _t(val - 10, 40), 0.08, **common))
    pol = _policy(con)
    if pol:
        out.append(pol)
    return out


def _korea_latest(con):
    rows = db.rows(con, "SELECT * FROM observations WHERE source='korea' ORDER BY period DESC, series")
    if not rows:
        return None
    rank = {"d10": 1, "d20": 2, "month": 3}
    latest = rows[0]["period"]
    cand = [r for r in rows if r["period"] == latest]
    # prefer chip-specific, then the longest window
    r = max(cand, key=lambda r: (r["series"].startswith("chip"), rank.get(r["series"].rsplit("_", 1)[-1], 0)))
    window = r["series"].rsplit("_", 1)[-1]
    days = {"d10": 10, "d20": 20}.get(window, 28)
    what = ("chip exports" if r["series"].startswith("chip") else "total exports (chip-led)")
    label = f"{what}, {_month_name(latest)} {'days 1-' + str(days) if window != 'month' else 'full month'}"
    end = date(int(latest[:4]), int(latest[5:7]), min(days, 28))
    return r["value"], label, latest, r["url"], max((date.today() - end).days, 0)


def _policy(con) -> Signal | None:
    docs = [(d, _iso_age(d["published"])) for d in db.rows(con, "SELECT * FROM documents WHERE source='fedreg'")]
    docs = [(d, a) for d, a in docs if a <= 120]
    if not docs:
        return None
    load = sum(d["supply_tone"] * math.exp(-a / 45) for d, a in docs)
    newest = min(docs, key=lambda x: x[1])[0]
    return Signal("U.S. trade-policy actions", "Federal Register", "supply", _t(load, 2), 0.08,
                  f"{len(docs)} export-control / tariff actions in 120 days; latest: “{newest['title'][:100]}” ({newest['published']})",
                  raw=load, url=newest["url"], age_days=min(a for _, a in docs))


def taiwan_signal(con, segment: str) -> list[Signal]:
    from .scrapers.taiwan import COMPANIES
    parts, notes, ages, urls = [], [], [], []
    for t, (name, _, seg) in COMPANIES.items():
        if seg != segment:
            continue
        rev = series(con, "mops", f"{t}:revenue")
        g = yoy_growth(rev)
        if g is None:
            continue
        hist = [x for x in (yoy_growth(rev[:i]) for i in range(max(15, len(rev) - 36), len(rev))) if x is not None]
        base = statistics.median(hist) if len(hist) >= 6 else 5.0
        parts.append(_t(g - base, 20))
        notes.append(f"{name} {g:+.0f}% YoY (typical {base:+.0f}%)")
        ages.append(_period_age(rev[-1][0]))
        urls.append(_url(con, "mops", f"{t}:revenue"))
    if not parts:
        return []
    return [Signal("Taiwan supply-chain loading", "MOPS", "supply", statistics.fmean(parts), 0.12,
                   "3-mo revenue: " + "; ".join(notes), raw=statistics.fmean(parts), url=urls[0],
                   age_days=statistics.fmean(ages))]


def memory_spot_signal(con) -> list[Signal]:
    items = [r["series"] for r in db.rows(con, "SELECT DISTINCT series FROM observations WHERE source='dramx'")]
    chg, notes, last_day = [], [], None
    for it in items:
        s_ = series(con, "dramx", it)
        if len(s_) < 2:
            continue
        end_day = date.fromisoformat(s_[-1][0])
        ref = [v for d, v in s_ if (end_day - date.fromisoformat(d)).days >= 21]
        if not ref:
            continue
        c = (s_[-1][1] / ref[-1] - 1) * 100
        chg.append(c)
        notes.append(f"{it.split(' (')[0]} {c:+.1f}%")
        last_day = s_[-1][0]
    if not chg:
        return []           # needs ~3 weeks of daily snapshots before it speaks
    m = statistics.fmean(chg)
    common = dict(raw=m, as_of=last_day, url="https://www.dramexchange.com/", age_days=_iso_age(last_day + "T12:00:00+00:00"),
                  evidence=f"Memory spot prices over ~4 weeks: {', '.join(notes[:5])} (DRAMeXchange session averages)")
    return [Signal("Memory spot prices", "DRAMeXchange", "price", _t(m, 10), 0.30, **common),
            Signal("Memory spot prices", "DRAMeXchange", "supply", _t(m, 10), 0.10, **common)]


def quake_signal(con, segment: str | None) -> list[Signal]:
    hits = []
    for d in db.rows(con, "SELECT * FROM documents WHERE source='usgs'"):
        age = _iso_age(d["published"])
        if age > 21 or (segment and segment not in json.loads(d["segments"] or "[]")):
            continue
        hits.append((d, age, d["supply_tone"] * math.exp(-age / 7)))
    if not hits:
        return []
    d, age, v = max(hits, key=lambda x: x[2])
    if v < 0.15:            # distant or moderate quakes stay in the news feed, not the score
        return []
    return [Signal("Earthquake near fabs", "USGS", "supply", min(1.0, v), 0.15,
                   f"{d['title']} ({d['published'][:10]})", raw=v, url=d["url"], age_days=age)]


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
