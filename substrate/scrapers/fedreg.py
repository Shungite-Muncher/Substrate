"""U.S. policy actions that move chip supply: Commerce/BIS export controls and entity-list
changes, plus tariff and Section 232 actions, from the Federal Register's free API."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .. import db, http

API = "https://www.federalregister.gov/api/v1/documents.json"
SEARCHES = [
    {"conditions[agencies][]": "industry-and-security-bureau", "conditions[term]": "semiconductor"},
    {"conditions[agencies][]": "industry-and-security-bureau", "conditions[term]": "entity list integrated circuits"},
    {"conditions[term]": "semiconductors section 232"},
    {"conditions[agencies][]": "trade-representative-office-of-united-states", "conditions[term]": "semiconductors"},
]
FIELDS = ["title", "publication_date", "type", "html_url", "abstract", "agencies"]
# full-text search is loose (a vehicle rule can mention "semiconductors" once); keep only chip-relevant actions
RELEVANT = re.compile(r"semiconductor|integrated circuit|\bchips?\b|advanced computing|entity list|export administration|"
                      r"wafer|polysilicon|lithograph|foundr|gallium|germanium|rare earth|section 232", re.I)


def purge_irrelevant(con) -> int:
    bad = [r["url"] for r in db.rows(con, "SELECT url, title, summary FROM documents WHERE source='fedreg'")
           if not RELEVANT.search(f"{r['title']} {r['summary'] or ''}")]
    for u in bad:
        con.execute("DELETE FROM documents WHERE url=?", (u,))
    return len(bad)


def run(con, backfill: bool = False) -> dict:
    since = (date.today() - timedelta(days=365 if backfill else 45)).isoformat()
    n = 0
    for q in SEARCHES:
        params = {**q, "order": "newest", "per_page": 50, "conditions[publication_date][gte]": since, "fields[]": FIELDS}
        for d in http.get(API, params=params).json().get("results", []):
            if con.execute("SELECT 1 FROM documents WHERE url=?", (d["html_url"],)).fetchone():
                continue
            if not RELEVANT.search(f"{d['title']} {d.get('abstract') or ''}"):
                continue
            agencies = ", ".join(a.get("name", "") for a in d.get("agencies") or [] if a.get("name"))
            db.upsert(con, "documents", {
                "url": d["html_url"], "source": "fedreg", "published": d["publication_date"],
                "title": d["title"], "summary": (d.get("abstract") or "")[:600],
                "segments": [], "tags": {"type": d.get("type"), "agencies": agencies},
                # a new restriction or tariff is a supply/price risk for buyers by construction
                "supply_tone": 1.0 if d.get("type") in ("Rule", "Proposed Rule", "Presidential Document") else 0.5,
                "price_tone": 0.5, "fetched_at": db.now_iso()})
            n += 1
    return {"source": "fedreg", "new": n, "purged": purge_irrelevant(con)}
