import numpy as np

from substrate import db
from substrate.predict import engine, features, outlook
from substrate.predict.models import Logistic, Ridge, brier


def test_logistic_and_ridge_recover_signal():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(400, 3))
    y = (X[:, 0] - 0.5 * X[:, 1] + rng.normal(scale=0.5, size=400) > 0).astype(float)
    m = Logistic.fit(X, y, l2=1.0)
    p = m.prob(X)
    assert brier(p, y) < brier(np.full_like(y, y.mean()), y) * 0.6
    assert m.w[1] > 0 > m.w[2]
    r = Ridge.fit(X, X[:, 0] * 2 + 1, l2=0.01)
    assert np.allclose(r.predict(X[:5]), X[:5, 0] * 2 + 1, atol=0.05)


def test_scaler_caps_outliers():
    X = np.random.default_rng(1).normal(size=(200, 1))
    m = Logistic.fit(X, (X[:, 0] > 0).astype(float))
    at_cap = m.scaler.mean + 3 * m.scaler.sd
    # anything beyond 3 sd scores exactly like 3 sd: no runaway extrapolation
    assert np.isclose(m.prob(np.array([[1e6]]))[0], m.prob(at_cap[None, :])[0])


def _synthetic(months=360):
    start = features.idx("1995-01")
    rng = np.random.default_rng(2)
    d = {"ppi": {}, "cu": {}, "ip": {}, "tsmc": {}}
    lvl, cu = 100.0, 78.0
    for k in range(months):
        i = start + k
        lvl *= np.exp(0.002 * np.sin(k / 9) + rng.normal(scale=0.004))
        cu = 78 + 5 * np.sin(k / 15) + rng.normal(scale=0.5)
        d["ppi"][i], d["cu"][i] = lvl, cu
        d["ip"][i] = 100 * np.exp(k / 300)
        d["tsmc"][i] = 1000 * np.exp(k / 200) * (1 + 0.05 * np.sin(k / 6))
    return d


def test_rows_are_point_in_time_and_walk_forward_never_peeks():
    d = _synthetic()
    rows = engine.build_rows(d)
    assert rows and all(set(features.FEATURE_NAMES) <= set(r) for r in rows)
    # targets near the end are unknown, not fabricated
    assert rows[-1]["ppi_3m"] is None
    wf = engine.walk_forward(rows, "ppi_3m")
    by_period = {r["period"]: r for r in rows}
    first = wf["oos"][0]
    # at the first OOS point, training only used rows whose outcome was already public
    assert features.idx(first["period"]) - features.idx(rows[0]["period"]) >= engine.MIN_TRAIN
    assert first["y"] == by_period[first["period"]]["ppi_3m"]
    m = wf["metrics"]
    assert m["verdict"] in ("edge", "matches trend", "no skill") and 0 <= m["interval_coverage_80"] <= 1


def test_ledger_records_once_and_resolves(tmp_path, monkeypatch):
    from substrate import config
    monkeypatch.setattr(config, "WAREHOUSE_DIR", tmp_path / "wh")
    con = db.connect(tmp_path / "t.db")
    fc = {"target": "ppi_3m", "base_period": "2026-01", "target_period": "2026-04", "h": 3,
          "prob_up": 0.7, "point": 2.0, "lo": -1.0, "hi": 5.0, "basis": "model"}
    engine.record(con, fc, 100.0)
    engine.record(con, {**fc, "prob_up": 0.1}, 100.0)          # a later run can't quietly revise it
    assert con.execute("SELECT prob_up FROM forecasts").fetchone()[0] == 0.7
    d = {"ppi": {features.idx("2026-01"): 100.0, features.idx("2026-04"): 103.0}, "cu": {}}
    assert engine.resolve(con, d) == 1
    r = db.rows(con, "SELECT * FROM forecasts")[0]
    assert r["hit"] == 1 and r["in_range"] == 1 and abs(r["realized"] - 2.956) < 0.01


def test_timing_calls():
    price = {"point": 3.0}
    assert outlook._timing(0.7, 0.5, price, {})[0] == "Buy ahead / lock pricing"
    assert outlook._timing(0.3, 0.4, price, {})[0] == "Wait / keep terms short"
    action, why = outlook._timing(0.5, 0.5, price, {"price_vs_target_pct": 12})
    assert action == "Hold course" and "above your target" in why
    assert outlook.tilt(0.6, 70, 50) > 0.6 > outlook.tilt(0.6, 30, 50)
