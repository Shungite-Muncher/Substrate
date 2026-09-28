"""Polite HTTP: one shared session, retries with backoff, per-host throttling."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

log = logging.getLogger(__name__)

_session: requests.Session | None = None
_last_hit: dict[str, float] = {}
# Minimum seconds between requests per host (SEC allows 10/s; be far gentler).
_MIN_INTERVAL = {"data.sec.gov": 0.25, "www.sec.gov": 0.25}
_DEFAULT_INTERVAL = 0.5


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "POST"))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.headers["User-Agent"] = config.USER_AGENT
        _session = s
    return _session


def _throttle(url: str) -> None:
    host = urlparse(url).netloc
    wait = _MIN_INTERVAL.get(host, _DEFAULT_INTERVAL) - (time.monotonic() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def get(url: str, *, headers: dict | None = None, timeout: int = 30, **kw) -> requests.Response:
    _throttle(url)
    r = session().get(url, headers=headers, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def post(url: str, *, headers: dict | None = None, timeout: int = 60, **kw) -> requests.Response:
    _throttle(url)
    r = session().post(url, headers=headers, timeout=timeout, **kw)
    r.raise_for_status()
    return r
