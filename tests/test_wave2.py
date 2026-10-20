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


def test_part_movement_alerts_and_relief(con):
    for day, lead, stock, oo, eta, tariff in [("2026-10-01", 182, 0, 5000, "2026-11-01", None),
                                              ("2026-10-08", 140, 1200, 1000, "2026-11-20", 38.0)]:
        db.upsert(con, "part_snapshots", {"mpn": "A", "provider": "mouser", "day": day, "lead_days": lead, "total_avail": stock,
                                          "on_order": oo, "on_order_eta": eta, "tariff_pct": tariff,
                                          "tariff_note": "A tariff of 38% may be applied" if tariff else None})
    kinds = {c["kind"]: c for c in alerts.part_changes(con, ["A"])}
    assert set(kinds) == {"lead time easing", "back in stock", "backorder easing", "PBO delayed", "tariff added"}
    assert kinds["lead time easing"]["title"] == "A: lead time 26 → 20 weeks"
    db.upsert(con, "part_snapshots", {"mpn": "B", "provider": "mouser", "day": date.today().isoformat(), "total_avail": 0,
                                      "on_order": 900, "on_order_eta": (date.today() + timedelta(days=10)).isoformat()})
    r = alerts.relief_due(con, ["B"], {"B": 52000})
    assert len(r) == 1 and r[0]["kind"] == "backorder relief due"


def test_mouser_trade_fields():
    from substrate.parts import mouser
    r = mouser.parse({"SearchResults": {"Parts": [{"ManufacturerPartNumber": "C3M0065090D", "AvailabilityInStock": "0",
        "SurchargeMessages": [{"code": "TARIFF_US", "message": "A tariff of 38% may be applied if shipping to the United States."}],
        "TradeCompliance": [{"ComplianceName": "Country of Origin", "ComplianceValue": "China"},
                            {"ComplianceName": "Assembly Country of Origin", "ComplianceValue": "Not available"}],
        "ProductCompliance": [{"ComplianceName": "USHTS", "ComplianceValue": "8541290065"}, {"ComplianceName": "ECCN", "ComplianceValue": "EAR99"}],
        "InfoMessages": ["Mouser does not presently sell this product in your region."]}]}}, "C3M0065090D")
    assert (r.country_of_origin, r.assembly_country, r.us_hts, r.eccn, r.tariff_pct) == ("China", None, "8541290065", "EAR99", 38.0)
    assert "region" in r.notice


def test_freshness_and_relevance_weighting():
    from substrate import scoring, signals
    assert signals.freshness("FRED", 40) == 1.0                         # within normal publication lag
    assert abs(signals.freshness("FRED", 45 + 45) - 0.5) < 1e-9          # one half-life past it
    assert signals.freshness("Trade press", 365) == signals.FRESHNESS_FLOOR
    part = signals.Signal("Factory lead time", "Part data", "supply", 1.0, 0.3, "x", level="part")
    mkt = signals.Signal("PPI", "FRED", "supply", -1.0, 0.3, "y", level="market")
    s = scoring.summarize([part, mkt], "part")
    assert s["supply_risk"] > 60      # the part's own evidence outweighs the market-wide one
    assert {x["name"]: x["relevance"] for x in s["signals"]} == {"Factory lead time": 1.0, "PPI": 0.4}


def test_tariff_estimate_from_peers(con):
    from substrate import signals
    db.upsert(con, "part_snapshots", {"mpn": "IRF540NPBF", "provider": "mouser", "day": "2026-10-19",
                                      "country_of_origin": "China", "us_hts": "8541290065", "tariff_pct": 38.0})
    db.upsert(con, "observations", {"source": "usitc", "series": "8541.21.00", "period": "current", "value": 0.0})
    te = signals.tariff_estimate(con, {"mpn": "BSS138", "country_of_origin": "China", "us_hts": "8541210095"})
    assert te["pct"] == 38.0 and te["estimated"] and "IRF540NPBF" in te["basis"] and te["base_duty"] == 0.0
    assert signals.tariff_estimate(con, {"mpn": "X", "country_of_origin": "Malaysia", "us_hts": "8541210095"})["pct"] == 0.0


def test_prediction_decay():
    from substrate.predict.engine import decay_factor
    assert decay_factor("2026-09", date(2026, 10, 15))[0] == 1.0
    assert 0.5 < decay_factor("2026-09", date(2027, 2, 1))[0] < 0.6


def test_wire_and_freight_parsers():
    from substrate.scrapers import logistics, wires
    assert wires.extract("taiwan_export_orders_yoy", "Taiwan September export orders fall 3.2% on weak demand", "2026-10-20") == ("2026-09", -3.2)
    assert wires.extract("china_chip_exports_yoy", "China's semiconductor exports surge 103.9% in Jan-Aug", "2026-09-15") == ("2026-08", 103.9)
    assert wires.contract("DRAM contract prices forecast to grow only 13-18% in Q3", "2026-06-20") == ("dram", "2026Q3", 15.5)
    assert logistics.wci("<p>WCI上涨3%至4,485美元/FEU，亚欧航线</p>") == 4485.0
