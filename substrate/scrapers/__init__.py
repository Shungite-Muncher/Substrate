"""Scrapers. Each module exposes run(con, backfill=False) -> summary dict."""
from . import dramx, fedreg, fred, korea, news, sec, sia, taiwan, tsmc, usgs

ALL = {"tsmc": tsmc, "sia": sia, "fred": fred, "sec": sec, "news": news, "mops": taiwan, "dramx": dramx,
       "korea": korea, "fedreg": fedreg, "usgs": usgs}
