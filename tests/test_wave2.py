"""Second-wave data sources, alerts, delivery and customer-history import."""
import json
from datetime import date, timedelta

import numpy as np
import pytest

from substrate import alerts, config, db, deliver, history, parts
from substrate.predict import parts_model
from substrate.scrapers import dramx, fedreg, korea, taiwan, usgs


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WAREHOUSE_DIR", tmp_path / "wh")
    monkeypatch.setattr(parts_model, "STATUS_PATH", tmp_path / "pm.json")
    monkeypatch.setattr(alerts, "STATE_PATH", tmp_path / "state" / "alerts.json")
    return db.connect(tmp_path / "t.db")


def test_korea_headlines():
    x = korea.extract("Korea’s Sept. 1-10 exports hit record on 270% chip surge", "2026-09-11")
    assert x == [{"period": "2026-09", "window": "d10", "series": "chip_exports_yoy", "yoy_pct": 270.0}]
    y = korea.extract("Exports down 4.1% in first 20 days of January", "2027-01-21")[0]
    assert (y["period"], y["window"], y["yoy_pct"]) == ("2027-01", "d20", -4.1)
    assert korea.extract("Exports up 12% in first 10 days of Dec.", "2027-01-11")[0]["period"] == "2026-12"
    assert korea.extract("IMF raises Korea growth forecast to 3.3 percent", "2026-10-01") == []


def test_mops_parse():
    html = ("<table><tr><td>2303</td><td>UMC</td><td>25,044,641</td></tr>"
            "<tr><td>9999</td><td>X</td><td>1</td></tr></table>").encode("big5")
    assert taiwan.parse(html, {"2303"}) == {"2303": 25044641.0}


def test_dram_spot_parse_skips_modules():
    html = """<table><tr><td>Item</td><td>Daily High</td><td>Daily Low</td><td>SH</td><td>SL</td><td>Session Average</td></tr>
      <tr><td>DDR4 8Gb (1Gx8) 3200</td><td>85.50</td><td>25.00</td><td>85.5</td><td>25</td><td>45.971</td></tr>
      <tr><td>DDR4 UDIMM 16GB 3200</td><td>185</td><td>158</td><td>185</td><td>158</td><td>167.2</td></tr></table>"""
    assert dramx.parse(html) == {"DDR4 8Gb (1Gx8) 3200": 45.971}


def test_policy_relevance_filter():
    assert fedreg.RELEVANT.search("Revision to License Review Policy for Advanced Computing Commodities")
    assert fedreg.RELEVANT.search("Additions to the Entity List")
    assert not fedreg.RELEVANT.search("Safer Affordable Fuel-Efficient Vehicles Rule")
    assert not fedreg.RELEVANT.search("Archipelago fisheries management")


def test_quake_distance():
    assert 95 < usgs._km(24.78, 120.99, 23.97, 121.60) < 115     # Hsinchu -> Hualien


def _scores(mpn_action=None):
    seg = {"label": "Memory", "supply_risk": 60, "price_trend": 70, "signals": [{"evidence": "x"}]}
    return {"as_of": "2026-10-18T13:00Z", "market": {**seg, "label": "Market"}, "segments": {"memory": seg}, "parts": {}}


def test_alerts_fire_once_and_skip_first_run(con):
    db.upsert(con, "documents", {"url": "https://fr/1", "source": "fedreg", "published": date.today().isoformat(),
                                 "title": "Additions to the Entity List", "summary": "", "segments": "[]",
                                 "supply_tone": 1.0, "price_tone": 0.5})
    o1 = {"parts": {"A": {"action": "Hold course", "why": "w", "p_price_up": .5}}}
    assert alerts.build(con, _scores(), o1) == []                          # first run: baseline only
    o2 = {"parts": {"A": {"action": "Buy ahead / lock pricing", "why": "63% chance", "p_price_up": .63}}}
    db.upsert(con, "documents", {"url": "https://fr/2", "source": "fedreg", "published": date.today().isoformat(),
                                 "title": "Section 232 tariffs on semiconductors", "summary": "", "segments": "[]",
                                 "supply_tone": 1.0, "price_tone": 0.5})
    a = alerts.build(con, _scores(), o2)
    kinds = sorted(x["kind"] for x in a)
    assert kinds == ["call change", "policy"] and a[0]["severity"] == "high"
    assert alerts.build(con, _scores(), o2) == []                          # nothing fires twice


def test_delivery_payloads(monkeypatch):
    brief = {"edition": "U.S. open", "body_md": "# Memory tightens\n\n**Bottom line:** Prices up.\n\n## Outlook\n- **A**: [src](https://x)"}
    al = [{"kind": "policy", "title": "Entity List additions", "url": "https://fr/1"}]
    o = {"parts": {"A": {"action": "Buy ahead / lock pricing", "p_price_up": .63}}}
    s = deliver.slack_payload(brief, al, o)
    assert s["blocks"][0]["text"]["text"] == "Memory tightens" and "Entity List" in json.dumps(s)
    t = deliver.teams_payload(brief, al, o)
    assert t["attachments"][0]["content"]["type"] == "AdaptiveCard"
    html = deliver.md_to_html(brief["body_md"])
    assert "<h1" in html and "<li><b>A</b>: <a href=\"https://x\">src</a></li>" in html
    monkeypatch.setattr(config, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(config, "TEAMS_WEBHOOK_URL", None)
    monkeypatch.setattr(config, "RESEND_API_KEY", None)
    assert deliver.channels() == [] and deliver.send(brief, al, o) == {}


def test_history_import_flexible_columns(tmp_path, con):
    f = tmp_path / "po.csv"
    f.write_text("Item Number,PO Date,Net Price,Order Qty,Lead Time Weeks,Vendor\n"
                 "stm32f407vgt6,03/14/2025,$7.42,5000,16,Arrow\n"
                 "STM32F407VGT6,03/14/2025,$7.20,15000,18,Avnet\n"
                 "BAD,,1,1,1,x\n", encoding="utf-8")
    r = history.ingest(con, f, "Acme")
    assert (r["rows"], r["snapshots"], r["skipped"]) == (2, 1, 1)
    row = db.rows(con, "SELECT * FROM part_snapshots WHERE provider='customer'")[0]
    assert row["lead_days"] == 126 and abs(row["price_1k"] - 7.255) < 1e-6     # qty-weighted, max lead
    assert parts.cached(con, "STM32F407VGT6", 3650) is None                    # never shown as live market data
    with pytest.raises(ValueError):
        history.parse_csv("foo,bar\n1,2\n")


def test_customer_history_backtests_immediately(con):
    rng = np.random.default_rng(1)
    start = date(2023, 1, 2)
    for k in range(25):
        lead, price = 84.0, 2.0
        for w in range(90):
            lead = max(14, lead + rng.normal(0, 4))
            price *= np.exp(rng.normal(0, 0.01))
            db.upsert(con, "part_snapshots", {"mpn": f"P{k}", "provider": "customer",
                                              "day": (start + timedelta(weeks=w)).isoformat(),
                                              "lead_days": round(lead, 1), "price_1k": round(price, 4)})
    st = parts_model.run(con, {"cu_3m": {}, "ppi_3m": {}})["status"]
    assert st["lead"]["status"] in ("promoted", "heuristic") and st["lead"]["sources"] == ["customer"]
    assert st["pbo"]["status"] == "accumulating"                 # PO history carries no backlog data
    assert parts_model.run(con)["latest"] == {}                   # live predictions never use customer rows
