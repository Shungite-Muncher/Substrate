"""Earthquakes near the world's fab clusters, from the USGS real-time feed. A strong quake
near Hsinchu or Kumamoto is a supply event long before it shows up in any revenue figure."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from .. import db, http

API = "https://earthquake.usgs.gov/fdsnws/event/1/query"
MIN_MAG = 5.5
RADIUS_KM = 250
# fab cluster -> (lat, lon, segments most exposed)
FABS = {
    "Hsinchu (TSMC, UMC, Vanguard)": (24.78, 120.99, ["logic", "mcu", "analog"]),
    "Taichung (TSMC, Micron)": (24.15, 120.68, ["logic", "memory"]),
    "Tainan (TSMC N3/N5)": (23.10, 120.28, ["logic"]),
    "Kumamoto (TSMC/JASM, Sony)": (32.80, 130.70, ["logic", "mcu"]),
    "Kitakami (Kioxia)": (39.29, 141.11, ["memory"]),
    "Yokkaichi (Kioxia)": (34.96, 136.62, ["memory"]),
    "Naka / Takasaki (Renesas)": (36.45, 140.50, ["mcu", "power"]),
    "Pyeongtaek / Hwaseong (Samsung)": (37.10, 127.05, ["memory", "logic"]),
    "Icheon / Cheongju (SK hynix)": (37.27, 127.44, ["memory"]),
}


def _km(a, b, c, d):
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def run(con, backfill: bool = False) -> dict:
    start = (date.today() - timedelta(days=120 if backfill else 21)).isoformat()
    feats = http.get(API, params={"format": "geojson", "starttime": start, "minmagnitude": MIN_MAG,
                                  "minlatitude": 20, "maxlatitude": 42, "minlongitude": 118, "maxlongitude": 145}).json()["features"]
    n = 0
    for f in feats:
        lon, lat = f["geometry"]["coordinates"][:2]
        near = [(name, round(_km(lat, lon, fl, fo))) for name, (fl, fo, _) in FABS.items() if _km(lat, lon, fl, fo) <= RADIUS_KM]
        if not near:
            continue
        p = f["properties"]
        url = p.get("url") or f"https://earthquake.usgs.gov/earthquakes/eventpage/{f['id']}"
        segs = sorted({s for name, _ in near for s in FABS[name][2]})
        closest = min(near, key=lambda x: x[1])
        mag = p.get("mag") or 0
        db.upsert(con, "documents", {
            "url": url, "source": "usgs",
            "published": datetime.fromtimestamp(p["time"] / 1000, tz=timezone.utc).replace(microsecond=0).isoformat(),
            "title": f"M{mag:.1f} earthquake {closest[1]} km from {closest[0]}",
            "summary": f"{p.get('place')}. Fab clusters within {RADIUS_KM} km: " + "; ".join(f"{a} ({b} km)" for a, b in near),
            "segments": segs, "tags": {"magnitude": mag, "near": near},
            # M5.5 near a fab is a watch item; M7 is a real disruption risk
            "supply_tone": round(min(1.0, max(0.2, (mag - 5.5) / 1.5)), 2), "price_tone": 0.0,
            "fetched_at": db.now_iso()})
        n += 1
    return {"source": "usgs", "near_fabs": n}
