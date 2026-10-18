"""End-to-end checks for auto-scoring and self-promoting part models, on simulated history."""
import json
import math
from datetime import date, timedelta

import numpy as np
import pytest

from substrate import config, db
from substrate.predict import engine, outlook, parts_model


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WAREHOUSE_DIR", tmp_path / "wh")
    monkeypatch.setattr(parts_model, "STATUS_PATH", tmp_path / "part_models.json")
    return db.connect(tmp_path / "t.db")


def _simulate(con, signal: bool, parts=30, weeks=80, seed=0):
    """Weekly snapshots. With signal=True, thin channel stock today means longer lead times 13 weeks later."""
    rng = np.random.default_rng(seed)
    start = date(2025, 1, 6)
    for k in range(parts):
        mpn = f"SIM{k:03d}"
        annual = 52_000.0
        db.upsert(con, "bom", {"customer": config.CUSTOMER_NAME, "mpn": mpn, "annual_qty": annual})
        stock = rng.uniform(500, 20_000, size=weeks)
        lead = np.full(weeks, 84.0)
        for w in range(13, weeks):
            cover = stock[w - 13] / (annual / 52)
            drift = (8 - cover) * 2.5 if signal else 0.0
            lead[w] = max(14.0, lead[w - 13] + drift + rng.normal(scale=6))
        for w in range(weeks):
            db.upsert(con, "part_snapshots", {
                "mpn": mpn, "provider": "mouser", "day": (start + timedelta(weeks=w)).isoformat(),
                "lead_days": float(lead[w]), "total_avail": int(stock[w]), "price_1k": 1.0 + 0.001 * w,
                "on_order": int(rng.integers(0, 5000)), "sellers": json.dumps([{"seller": "Mouser", "stock": int(stock[w])}])})


def test_model_promotes_itself_when_history_has_signal(con):
    _simulate(con, signal=True)
    res = parts_model.run(con)
    lead = res["status"]["lead"]
    assert lead["status"] == "promoted", lead
    assert lead["brier"]["model"] < lead["brier"]["heuristic"]
    assert "lead" in res["models"]
    p, y = res["models"]["lead"].predict(res["latest"]["SIM000"]["f"])
    assert 0 <= p <= 1 and math.isfinite(y)
    assert json.loads(parts_model.STATUS_PATH.read_text())["lead"]["status"] == "promoted"


def test_model_stays_benched_on_noise(con):
    _simulate(con, signal=False, seed=3)
    res = parts_model.run(con)
    assert res["status"]["lead"]["status"] in ("heuristic", "accumulating")
    assert "lead" not in res["models"]


def test_accumulating_until_enough_pairs(con):
    _simulate(con, signal=True, parts=3, weeks=20)
    st = parts_model.run(con)["status"]["lead"]
    assert st["status"] == "accumulating" and "outcome pairs" in st["note"]


def _forecast(con, target, base_value, prob, target_period, meta):
    db.upsert(con, "forecasts", {"target": target, "base_period": "2026-01-05", "target_period": target_period,
                                 "horizon": 13, "made_at": "2026-01-05T00:00:00+00:00", "prob_up": prob,
                                 "basis": "rule of thumb", "base_value": base_value, "meta": json.dumps(meta)})


def _snap(con, mpn, day, **kw):
    db.upsert(con, "part_snapshots", {"mpn": mpn, "provider": "mouser", "day": day, **kw})


def test_part_forecasts_auto_score(con):
    _forecast(con, "price:A", 1.00, 0.7, "2026-04-06", {"provider": "mouser"})
    _forecast(con, "lead:A", 84, 0.3, "2026-04-06", {"provider": "mouser"})
    _forecast(con, "pbo:A", 1000, 0.6, "2026-04-06", {"provider": "mouser"})
    _snap(con, "A", "2026-04-08", price_1k=1.08, lead_days=112, on_order=1000)
    assert outlook.resolve_parts(con) == 3
    r = {x["target"]: x for x in db.rows(con, "SELECT * FROM forecasts")}
    assert r["price:A"]["hit"] == 1             # predicted up, price rose 8%
    assert r["lead:A"]["hit"] == 0              # predicted no lengthening, lead rose 4 weeks
    assert r["pbo:A"]["hit"] is None and r["pbo:A"]["realized"] == 1000   # flat: scored as not callable
    card = {g["kind"]: g for g in engine.ledger_summary(con)["scorecard"]}
    assert card["price"]["hit_rate"] == 1.0 and card["lead"]["hit_rate"] == 0.0 and card["pbo"]["not_scorable"] == 1


def test_eta_slip_and_arrival(con):
    _forecast(con, "eta:S", 4000, 0.8, "2026-03-15", {"provider": "mouser", "eta": "2026-03-01", "stock": 0})
    _forecast(con, "eta:T", 4000, 0.8, "2026-03-15", {"provider": "mouser", "eta": "2026-03-01", "stock": 0})
    _snap(con, "S", "2026-03-16", on_order=4000, on_order_eta="2026-05-01", total_avail=0)      # slipped
    _snap(con, "T", "2026-03-16", on_order=0, total_avail=3900)                                 # arrived
    outlook.resolve_parts(con)
    r = {x["target"]: x for x in db.rows(con, "SELECT * FROM forecasts")}
    assert (r["eta:S"]["realized"], r["eta:S"]["hit"]) == (1, 1)
    assert (r["eta:T"]["realized"], r["eta:T"]["hit"]) == (0, 0)


def test_forecasts_logged_once_per_week(con):
    snap = {"provider": "mouser", "day": date.today().isoformat()}
    outlook._record(con, "price", "A", 0.7, 0.7, "rule of thumb", 1.0, snap)
    outlook._record(con, "price", "A", 0.1, 0.1, "rule of thumb", 1.0, snap)
    rows = db.rows(con, "SELECT prob_up, meta FROM forecasts")
    assert len(rows) == 1 and rows[0]["prob_up"] == 0.7
    assert json.loads(rows[0]["meta"])["heuristic_prob"] == 0.7
