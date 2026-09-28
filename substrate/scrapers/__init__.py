"""Scrapers. Each module exposes run(con, backfill=False) -> summary dict."""
from . import fred, news, sec, sia, tsmc

ALL = {"tsmc": tsmc, "sia": sia, "fred": fred, "sec": sec, "news": news}
