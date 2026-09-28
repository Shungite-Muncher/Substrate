import math

import pytest

from substrate import bom, db, scoring, signals, tone
from substrate.parts import mouser, nexar
from substrate.parts.base import parse_lead_days, parse_money, price_at
from substrate.scrapers import fred, news, sia, tsmc
from substrate.segments import segment_for_category, segments_for_text


def test_tsmc_parse_handles_both_table_layouts():
    new = """<table><tr><th>Month</th><th>Consolidated</th></tr>
      <tr><td>Jan.</td><td>401,255</td><td>36.8%</td></tr><tr><td>Sept.</td><td></td><td></td></tr></table>"""
    old = """<table><tr><td>Feb.</td><td>33,584</td><td>5.8%</td><td>33,856</td><td>3.6%</td></tr></table>"""
    assert tsmc.parse(new, 2026) == [{"period": "2026-01", "value": 401255.0, "yoy": 36.8}]
    assert tsmc.parse(old, 2012) == [{"period": "2012-02", "value": 33856.0, "yoy": 3.6}]


def test_sia_release_parse():
    text = ("Global semiconductor sales were $146.8 billion during the month of July 2026, an increase of 6.4% "
            "compared to the June 2026 total of $137.9 billion and 135.1% more than the July 2025 total of $62.5 billion. "
            "Regionally, year-to-year sales in July were up in the Americas (171.3%), Japan (50.8%).")
    r = sia.parse_release(text)
    assert r["period"] == "2026-07"
    assert r["points"] == {"2026-07": 146.8, "2026-06": 137.9, "2025-07": 62.5}
    assert r["regions_yoy_pct"]["Americas"] == 171.3


def test_fred_parse_skips_missing():
    csv = "observation_date,X\n2026-07-01,74.1\n2026-08-01,.\n"
    assert fred.parse(csv) == [("2026-07", 74.1)]


def test_feed_parse_rss_and_atom():
    rss = """<?xml version="1.0"?><rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item>
      <title>DRAM prices rise as supply tightens</title><link>https://x/a</link>
      <description>&lt;p&gt;Contract prices up&lt;/p&gt;</description><pubDate>Mon, 28 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Foundry news</title>
      <link rel="alternate" href="https://x/b"/><updated>2026-09-28T10:00:00Z</updated></entry></feed>"""
    a = news.parse_feed(rss)[0]
    assert a["url"] == "https://x/a" and "Contract prices up" in a["summary"] and a["published"].startswith("2026-09-28")
    assert news.parse_feed(atom)[0]["url"] == "https://x/b"


def test_tone_directions():
    assert tone.score("Lead times extend as MCU shortage worsens; parts on allocation")["supply_tone"] == 1.0
    assert tone.score("Inventory correction continues; lead times normalize")["supply_tone"] == -1.0
    assert tone.score("Micron raises prices; DRAM contract prices rise")["price_tone"] == 1.0
    assert tone.score("Nothing to see here")["n"] == 0


def test_segment_routing():
    assert "memory" in segments_for_text("SK hynix HBM capacity sold out")
    assert "mcu" in segments_for_text("Microchip MCU lead times")
    assert segment_for_category("Integrated Circuits (ICs) > Embedded - Microcontrollers") == "mcu"
    assert segment_for_category("Transistors - FETs, MOSFETs - Single") == "power"


def test_money_lead_and_breaks():
    assert parse_money("$1,234.50") == 1234.5
    assert parse_money("1.234,50 €") == 1234.5
    assert parse_lead_days("12 Weeks") == 84
    assert parse_lead_days(30) == 30
    assert price_at([(1, 2.0), (100, 1.5), (1000, 1.2), (5000, 1.0)]) == 1.2
    assert price_at([(2500, 0.9)]) == 0.9


def test_nexar_parse():
    payload = {"data": {"supSearchMpn": {"results": [{"part": {
        "mpn": "LM358DR", "manufacturer": {"name": "Texas Instruments"}, "shortDescription": "Op amp",
        "category": {"name": "Op Amps", "path": "Amplifiers > Op Amps"}, "octopartUrl": "https://octopart.com/lm358dr",
        "totalAvail": 120000, "estimatedFactoryLeadDays": 42, "medianPrice1000": {"price": 0.085, "currency": "USD"},
        "specs": [{"attribute": {"shortname": "lifecyclestatus"}, "displayValue": "Production"}],
        "sellers": [{"company": {"name": "DigiKey"}, "offers": [{"inventoryLevel": 50000, "factoryLeadDays": 42, "moq": 1,
                     "prices": [{"quantity": 1, "price": 0.3, "currency": "USD"}, {"quantity": 1000, "price": 0.09, "currency": "USD"}]}]}],
    }}]}}}
    r = nexar.parse(payload, "LM358DR")
    assert r.provider == "octopart" and r.lead_days == 42 and r.price_1k == 0.085
    assert r.sellers[0].price_1k == 0.09 and r.lifecycle == "Production"
    assert nexar.parse({"data": {"supSearchMpn": {"results": []}}}, "X") is None


def test_mouser_parse_prefers_exact_match():
    payload = {"SearchResults": {"Parts": [
        {"ManufacturerPartNumber": "LM358DRG4", "AvailabilityInStock": "5"},
        {"ManufacturerPartNumber": "LM358DR", "Manufacturer": "TI", "AvailabilityInStock": "12,345", "LeadTime": "6 Weeks",
         "PriceBreaks": [{"Quantity": 1, "Price": "$0.30", "Currency": "USD"}, {"Quantity": 1000, "Price": "$0.08", "Currency": "USD"}]}]}}
    r = mouser.parse(payload, "LM358DR")
    assert r.mpn == "LM358DR" and r.total_avail == 12345 and r.lead_days == 42 and r.price_1k == 0.08


def test_bom_parse_flexible_headers():
    rows = bom.parse_csv("Mfr Part Number,Mfr,EAU,Unit Cost\nstm32f407vgt6 ,ST,\"120,000\",$7.10\n,,,\n")
    assert rows == [{"mpn": "STM32F407VGT6", "manufacturer": "ST", "annual_qty": 120000.0, "target_price": 7.1}]
    with pytest.raises(ValueError):
        bom.parse_csv("foo,bar\n1,2\n")


def test_indicator_math_and_truncation():
    from datetime import date
    s = [(f"{2020 + i // 12}-{i % 12 + 1:02d}", 100 + i) for i in range(40)]
    assert signals.truncate(s, date(2021, 3, 20), 1)[-1][0] == "2021-02"
    g = signals.yoy_growth(s)
    assert g is not None and g > 0
    norm, ann = signals.ppi_momentum([("2026-05", 100.0), ("2026-06", 101), ("2026-07", 102), ("2026-08", 103)])
    assert math.isclose(ann, (1.03 ** 4 - 1) * 100) and 0 < norm < 1


def test_scoring_is_traceable():
    sigs = [signals.Signal("a", "FRED", "supply", 1.0, 0.5, "x", age_days=0),
            signals.Signal("b", "TSMC", "supply", -1.0, 0.5, "y", age_days=0),
            signals.Signal("c", "FRED", "price", 0.5, 1.0, "z", age_days=0)]
    out = scoring.summarize(sigs, "market")
    assert out["supply_risk"] == 50.0                      # equal and opposite -> neutral
    assert out["components"]["supply"]["agreement"] == 0.0  # and zero agreement
    assert out["price_trend"] == 75.0
    assert {s["name"] for s in out["signals"]} == {"a", "b", "c"}


def test_part_signals_from_snapshot():
    snap = {"provider": "octopart", "day": "2026-09-28", "lead_days": 182, "total_avail": 1000, "url": "u",
            "sellers": [{"seller": "A", "stock": 1000}], "lifecycle": "NRND", "price_1k": 1.1}
    hist = [{"day": "2026-08-01", "lead_days": 120, "price_1k": 1.0}, snap]
    names = {s.name: s for s in signals.part_signals(snap, hist, {"annual_qty": 520000})}
    assert names["Factory lead time"].norm > 0.8
    assert names["Lead-time trend"].norm > 0.9
    assert names["Channel inventory cover"].norm > 0.6      # 0.1 weeks of cover
    assert names["Lifecycle status"].norm == 1.0
    assert names["Part price trend"].norm > 0.7


def test_db_roundtrip(tmp_path, monkeypatch):
    from substrate import config
    monkeypatch.setattr(config, "WAREHOUSE_DIR", tmp_path / "wh")
    con = db.connect(tmp_path / "a.db")
    db.upsert(con, "observations", {"source": "s", "series": "x", "period": "2026-01", "value": 1.0, "unit": "u",
                                    "meta": {"k": 1}, "url": None, "fetched_at": "t"})
    db.record_usage(con, "octopart", parts=2)
    db.dump_warehouse(con)
    con2 = db.connect(tmp_path / "b.db")  # fresh DB rebuilds from the warehouse
    assert con2.execute("SELECT value FROM observations").fetchone()[0] == 1.0
    assert db.usage(con2, "octopart")["parts"] == 2
