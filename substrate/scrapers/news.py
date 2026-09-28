"""Trade press via public RSS/Atom feeds. We store headline, link and the feed's
own short summary only (no full-article copying), plus rule-based tone."""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .. import db, http, tone
from ..segments import segments_for_text

log = logging.getLogger(__name__)

FEEDS = {
    "EE Times": "https://www.eetimes.com/feed/",
    "Semiconductor Engineering": "https://semiengineering.com/feed/",
    "eeNews Europe": "https://www.eenewseurope.com/en/feed/",
    "DigiTimes": "https://www.digitimes.com/rss/daily.xml",
    "Electronics Weekly": "https://www.electronicsweekly.com/feed/",
    "Semiconductor Digest": "https://www.semiconductor-digest.com/feed/",
    "TrendForce": "https://www.trendforce.com/feed/Semiconductors.html",
    "Google News (supply)": "https://news.google.com/rss/search?q=semiconductor+(%22lead+times%22+OR+allocation+OR+shortage+OR+%22price+increase%22)+when:7d&hl=en-US&gl=US&ceid=US:en",
}
RELEVANT = re.compile(r"semiconductor|chip|wafer|foundry|fab\b|memory|dram|nand|mcu|microcontroller|"
                      r"analog|mosfet|sic\b|gan\b|component|distributor|lead[- ]time", re.I)
_TAG = re.compile(r"<[^>]+>")


def _text(el, *names) -> str:
    for n in names:
        found = el.find(n)
        if found is not None:
            return (found.text or found.get("href") or "").strip()
    return ""


def _date(s: str) -> str | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_feed(xml_text: str) -> list[dict]:
    # strip default namespaces so Atom and RSS can share lookups
    xml_text = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", xml_text, count=0)
    xml_text = re.sub(r"<(/?)(\w+):", r"<\1\2_", xml_text)
    xml_text = re.sub(r"\s(\w+):(\w+)=", r" \1_\2=", xml_text)
    root = ET.fromstring(xml_text.encode("utf-8"))
    items = root.findall(".//item") or root.findall(".//entry")
    out = []
    for it in items:
        title = html.unescape(_TAG.sub("", _text(it, "title")))
        link = _text(it, "link")
        if not link:
            le = it.find("link")
            link = le.get("href", "") if le is not None else ""
        summary = html.unescape(_TAG.sub(" ", _text(it, "description", "summary", "content_encoded")))
        summary = re.sub(r"\s+", " ", summary).strip()[:600]
        published = _date(_text(it, "pubDate", "published", "updated", "dc_date"))
        if title and link:
            out.append({"title": title, "url": link, "summary": summary, "published": published})
    return out


def run(con, backfill: bool = False) -> dict:
    kept = 0
    failed = []
    for name, url in FEEDS.items():
        try:
            items = parse_feed(http.get(url).text)
        except Exception as e:
            log.warning("feed %s: %s", name, e)
            failed.append(name)
            continue
        for it in items:
            blob = f"{it['title']}. {it['summary']}"
            if not RELEVANT.search(blob):
                continue
            if con.execute("SELECT 1 FROM documents WHERE url=?", (it["url"],)).fetchone():
                continue  # keep first-seen fetched_at so "new since last briefing" stays meaningful
            t = tone.score(blob)
            segs = segments_for_text(blob)
            db.upsert(con, "documents", {
                "url": it["url"], "source": name, "published": it["published"] or db.now_iso(),
                "title": it["title"], "summary": it["summary"], "segments": segs,
                "tags": {"matches": t["matches"]}, "supply_tone": t["supply_tone"],
                "price_tone": t["price_tone"], "fetched_at": db.now_iso()})
            kept += 1
    return {"source": "news", "kept": kept, "failed_feeds": failed}
