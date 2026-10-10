"""Monthly feature matrix built from the accumulator.

A row is indexed by its base period p: the last month for which every input
series has been published. All features use data up to and including p, and
targets look forward from p, so a row is exactly what was knowable when the
forecast would have been made.
"""
from __future__ import annotations

import math
import statistics

from .. import signals

SERIES = {
    "ppi": ("fred", "ppi_semis"),
    "cu": ("fred", "capacity_utilization"),
    "ip": ("fred", "industrial_production"),
    "tsmc": ("tsmc", "monthly_revenue"),
}

FEATURES = {
    "ppi_m3": "Chip PPI momentum, 3-mo annualized %",
    "ppi_m12": "Chip PPI change, 12-mo %",
    "ppi_accel": "Chip PPI acceleration (3-mo momentum vs 3 months earlier)",
    "cu_z": "Fab utilization vs its 10-yr norm (z)",
    "cu_d3": "Fab utilization change, 3-mo pts",
    "cu_d12": "Fab utilization change, 12-mo pts",
    "ip_g12": "Chip industrial production, 12-mo growth %",
    "ip_m3": "Chip industrial production, 3-mo annualized %",
    "tsmc_g": "TSMC revenue, 3-mo YoY growth %",
    "tsmc_accel": "TSMC revenue growth acceleration (vs 3 months earlier)",
}
# The models use a compact subset. Out of sample (2010-2026), six inputs beat all ten
# for every target: fewer, less-correlated inputs overfit less on ~300 monthly rows.
FEATURE_NAMES = ["ppi_m3", "ppi_m12", "cu_z", "cu_d3", "ip_g12", "tsmc_g"]


def idx(period: str) -> int:
    return int(period[:4]) * 12 + int(period[5:7]) - 1


def period(i: int) -> str:
    return f"{i // 12}-{i % 12 + 1:02d}"


def load(con) -> dict[str, dict[int, float]]:
    return {k: {idx(p): v for p, v in signals.series(con, src, name)} for k, (src, name) in SERIES.items()}


def _log(a, b, scale):
    return math.log(a / b) * scale if a and b and a > 0 and b > 0 else None


def _sum3(s, i):
    vals = [s.get(i - k) for k in range(3)]
    return sum(vals) if all(v is not None for v in vals) else None


def _tsmc_g(s, i):
    a, b = _sum3(s, i), _sum3(s, i - 12)
    return _log(a, b, 100) if a and b else None


def features_at(d: dict, i: int) -> dict | None:
    ppi, cu, ip, ts = d["ppi"], d["cu"], d["ip"], d["tsmc"]
    try:
        m3 = _log(ppi[i], ppi[i - 3], 400)
        m3_prev = _log(ppi[i - 3], ppi[i - 6], 400)
        hist = [cu[i - k] for k in range(120) if (i - k) in cu]
        if len(hist) < 60:
            return None
        sd = statistics.pstdev(hist) or 1.0
        f = {
            "ppi_m3": m3,
            "ppi_m12": _log(ppi[i], ppi[i - 12], 100),
            "ppi_accel": None if m3 is None or m3_prev is None else m3 - m3_prev,
            "cu_z": (cu[i] - statistics.fmean(hist)) / sd,
            "cu_d3": cu[i] - cu[i - 3],
            "cu_d12": cu[i] - cu[i - 12],
            "ip_g12": _log(ip[i], ip[i - 12], 100),
            "ip_m3": _log(ip[i], ip[i - 3], 400),
            "tsmc_g": _tsmc_g(ts, i),
            "tsmc_accel": None,
        }
        prev = _tsmc_g(ts, i - 3)
        f["tsmc_accel"] = None if f["tsmc_g"] is None or prev is None else f["tsmc_g"] - prev
    except KeyError:
        return None
    return f if all(v is not None for v in f.values()) else None


def latest_base(d: dict) -> int:
    """Most recent month for which every series has published."""
    return min(max(s) for s in d.values() if s)


def target_value(d: dict, name: str, i: int, h: int) -> float | None:
    s = d["ppi"] if name.startswith("ppi") else d["cu"]
    if i + h not in s or i not in s:
        return None
    return _log(s[i + h], s[i], 100) if name.startswith("ppi") else s[i + h] - s[i]
