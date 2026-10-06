import dataclasses
import json
import math
import types

import httpx
import pytest
from typer.testing import CliRunner

import luna.baseline as baseline
from luna.cli import app
from luna.config import load_settings
from luna.data.kraken_spot import fetch_spot_instruments
from luna.data.store import BarStore
from luna.engine.backtest import default_instruments
from luna.http import make_client
from luna.instruments import load_instruments
from tests.helpers import synthetic_bars

N = 4000
KEYS = {"naive_momentum", "naive_mean_reversion", "naive_pairs"}


@pytest.fixture
def var(tmp_path, monkeypatch):
    store = BarStore(tmp_path)
    for seed, (asset, kind) in enumerate([("BTC", "trend"), ("ETH", "ou"), ("SOL", "random_walk")]):
        store.write("kraken_futures", asset, synthetic_bars(N, kind, seed=seed))
    for seed, asset in enumerate(["BTC", "ETH"]):
        store.write("kraken_spot", asset, synthetic_bars(N, "ou", seed=10 + seed))
    settings = load_settings()
    for kind in ("perp", "spot"):
        d = tmp_path / "instruments"
        d.mkdir(exist_ok=True)
        inst = default_instruments(["BTC", "ETH", "SOL"], kind, settings)
        (d / f"{kind}.json").write_text(json.dumps({a: dataclasses.asdict(i) for a, i in inst.items()}))
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("LUNA_UNIVERSE__MIN_ADV_USD", "0")
    monkeypatch.setenv("LUNA_UNIVERSE__MIN_HISTORY_DAYS", "5")
    monkeypatch.setenv("LUNA_UNIVERSE__HISTORY_START", "2024-01-01")
    return tmp_path


def run(account, *extra):
    res = CliRunner().invoke(app, ["baseline", "--account", account, *extra])
    assert res.exit_code == 0, res.output
    return res


def out_dir(var):
    (d,) = (var / "reports").glob("baseline_*")
    return d


def test_baseline_cli_on_fixture_store(var):
    run("perp")  # default start = history_start + min_history_days = 2024-01-06
    d = out_dir(var)
    assert (d / "report.html").stat().st_size > 0
    s = json.loads((d / "summary.json").read_text())
    assert set(s) == {"combined", *KEYS, "skipped_assets"} and s["skipped_assets"] == []
    assert "Run setup" in (d / "report.html").read_text(encoding="utf-8")
    assert {"n_trades", "expectancy_r", "ci_lo", "ci_hi", "pnl_net"} <= set(s["combined"])
    page = (d / "report.html").read_text(encoding="utf-8")
    assert "Secondary" in page and "expectancy_r CI" in page
    assert 'class="plotly-graph-div"' in page and "Equity" in page and "P&amp;L breakdown" in page
    assert "<script src=" not in page and len(page) > 1_000_000  # plotly.js inlined
    assert s["combined"]["ci_lo"] <= s["combined"]["expectancy_r"] <= s["combined"]["ci_hi"]


def test_empty_universe_names_earliest_valid_start(var):
    res = CliRunner().invoke(app, ["baseline", "--account", "perp", "--start", "2024-01-02"])
    assert res.exit_code != 0
    assert "earliest valid start is 2024-01-06" in str(res.exception)


def test_end_option_limits_the_window(var):
    run("perp", "--end", "2024-01-20")
    s = json.loads((out_dir(var) / "summary.json").read_text())
    n_short = s["combined"]["n_trades"]
    for d in (var / "reports").glob("baseline_*"):
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
    run("perp")
    n_full = json.loads((out_dir(var) / "summary.json").read_text())["combined"]["n_trades"]
    assert n_short < n_full


def test_perp_without_eth_btc_skips_pairs(var):
    BarStore(var).delete("kraken_futures", "ETH")
    run("perp")
    s = json.loads((out_dir(var) / "summary.json").read_text())
    assert s["naive_pairs"] == {"skipped": "ETH/BTC not in universe"}


def test_spot_baseline_has_no_shorts_and_skips_pairs(var, monkeypatch):
    seen = []
    real = baseline.run_backtest

    def spy(*a, **kw):
        r = real(*a, **kw)
        seen.append(r)
        return r

    monkeypatch.setattr(baseline, "run_backtest", spy)
    run("spot", "--start", "2024-01-06")
    s = json.loads((out_dir(var) / "summary.json").read_text())
    assert s["naive_pairs"] == {"skipped": "pairs require shorting"}
    assert set(s) == {"combined", *KEYS, "skipped_assets"}
    assert len(seen) == 3 and sum(len(r.trades) for r in seen) > 0
    assert all("direction" in r.trades.columns for r in seen)
    assert all((r.trades["direction"] > 0).all() for r in seen)


def test_spot_instruments_from_assetpairs_and_cache(tmp_path):
    payload = {"error": [], "result": {"XXBTZUSD": {"altname": "XBTUSD", "ordermin": "0.0001",
               "costmin": "0.5", "tick_size": "0.1", "lot_decimals": 8},
               "XETHZUSD": {"altname": "ETHUSD", "ordermin": "0.004", "costmin": "0.5",
                            "tick_size": "0.01", "lot_decimals": 8}}}
    calls = []

    def handler(req):
        calls.append(req.url.params["pair"])
        return httpx.Response(200, json=payload)

    client = make_client(httpx.MockTransport(handler))
    settings = load_settings()
    inst = fetch_spot_instruments(client, ["XBTUSD", "ETHUSD"], settings.costs["spot"])
    assert calls == ["XBTUSD,ETHUSD"]
    assert inst["BTC"].min_qty == 0.0001 and inst["BTC"].price_tick == 0.1
    assert inst["ETH"].qty_step == 1e-8 and inst["ETH"].min_notional == 0.5

    s2 = settings.model_copy(update={"var_dir": tmp_path})
    got = load_instruments(s2, "spot", client, assets=["BTC", "ETH"])
    assert set(got) == {"BTC", "ETH"} and (tmp_path / "instruments" / "spot.json").exists()
    calls.clear()
    assert load_instruments(s2, "spot", client) == got and calls == []


def test_spot_pair_aliases_roundtrip():
    from luna.instruments import asset_from_spot, spot_pair

    assert spot_pair("DOGE") == "XDGUSD" and spot_pair("BTC") == "XBTUSD" and spot_pair("SOL") == "SOLUSD"
    for a in ("BTC", "DOGE", "SOL", "ETH"):
        assert asset_from_spot(spot_pair(a)) == a


def test_fetch_spot_instruments_unresolved():
    payload = {"error": [], "result": {"XXDGZUSD": {"altname": "XDGUSD", "ordermin": "50",
               "costmin": "5", "tick_size": "0.00001", "lot_decimals": 8}}}
    client = make_client(httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))
    costs = load_settings().costs["spot"]
    assert set(fetch_spot_instruments(client, ["XDGUSD"], costs)) == {"DOGE"}
    with pytest.raises(ValueError, match="FOOUSD"):
        fetch_spot_instruments(client, ["XDGUSD", "FOOUSD"], costs)
    got = fetch_spot_instruments(client, ["XDGUSD", "FOOUSD"], costs, allow_missing=True)
    assert set(got) == {"DOGE"}


def test_expectancy_ci_groups_by_exit_day(monkeypatch):
    import numpy as np
    import pandas as pd

    import luna.report.html as h

    def res(exits):
        tr = pd.DataFrame({"exit_ts": pd.to_datetime(exits, utc=True),
                           "r_multiple": [1.0, -1.0, 2.0, np.nan][: len(exits)]})
        return types.SimpleNamespace(trades=tr)

    seen = {}

    def spy(values, days, **kw):
        seen["v"], seen["d"] = values, days
        return (0.0, 0.0, 0.0)

    monkeypatch.setattr(h, "block_bootstrap_ci", spy)
    h.expectancy_ci(res(["2024-01-01 01:00", "2024-01-01 23:00", "2024-01-02 05:00", "2024-01-03 00:00"]))
    assert len(seen["v"]) == 3  # NaN R dropped
    assert seen["d"][0] == seen["d"][1] != seen["d"][2]  # same exit day shares a block
    h.expectancy_ci(res(["2024-01-01 01:00", "2024-01-02 01:00", "2024-01-03 01:00"]))
    assert len(set(seen["d"].tolist())) == 3


def test_json_safe():
    import numpy as np

    out = baseline._json_safe({"a": float("inf"), "b": float("nan"), "c": np.int64(3), "d": -math.inf})
    assert out == {"a": "inf", "b": None, "c": 3, "d": "-inf"}
    json.dumps(out)
