"""Scrapers. Each module exposes run(con, backfill=False) -> summary dict."""
from . import dramx, fedreg, fred, keyed, korea, logistics, news, sec, sia, taiwan, tariffs, tsmc, usgs, wires

ALL = {"tsmc": tsmc, "sia": sia, "fred": fred, "sec": sec, "news": news, "mops": taiwan, "dramx": dramx,
       "korea": korea, "fedreg": fedreg, "usgs": usgs, "tariffs": tariffs, "logistics": logistics, "wires": wires,
       "keyed": keyed}
