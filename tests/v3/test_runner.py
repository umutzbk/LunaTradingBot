"""Research V3 runner: V2 research on V3 configs + family null baselines (spec §4)."""

import pandas as pd
import pytest

from luna.engine.backtest import default_instruments
from luna.engine.walkforward import make_windows
from luna.report.walkforward import Inputs
from luna.v2.walkforward import ConfigWindowModel
from luna.v2.xsec import XSecParams
from luna.v3.configs import V3Config
from luna.v3.runner import NULL_SEEDS, null_model, run_v3

BAR = pd.Timedelta("15min")
CFGS = [
    V3Config("spot", "xsec", "1d", "broad", signal="lowvol"),
    V3Config("spot", "xsec", "1d", "broad", signal="volmanaged"),
]


@pytest.fixture(scope="module")
def small(v2_panel, v2_settings_small):
    panel = {a: v2_panel[a] for a in ("BTC", "ETH", "AAA")}
    windows, _ = make_windows(
        panel["BTC"].index[0], panel["BTC"].index[-1] + BAR, v2_settings_small.walkforward
    )
    inp = Inputs(panel, {}, None, default_instruments(sorted(panel), "spot", v2_settings_small), [])
    return v2_settings_small, inp, windows[-3:]


def _cw(params: XSecParams) -> ConfigWindowModel:
    cfg = CFGS[0]
    w = make_windows(
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2025-01-01", tz="UTC"),
        __import__("luna.config", fromlist=["load_settings"]).load_settings().walkforward,
    )[0][0]
    return ConfigWindowModel(cfg, w, ["BTC"], params.params_id, 0.1, True, xsec=(params, ["BTC"]))


def test_null_model_shuffles_premium_and_lowvol():
    assert null_model(_cw(XSecParams(30, 1, signal="lowvol")), 3).xsec[0].signal == "shuffle3:lowvol"
    assert null_model(_cw(XSecParams(30, 1, signal="premium")), 1).xsec[0].signal == "shuffle1:premium"


def test_null_model_drops_the_vol_target_of_volmanaged():
    p = null_model(_cw(XSecParams(30, 99, vol_target=0.1)), 2).xsec[0]
    assert p.vol_target is None and p.signal == "momentum" and p.k == 99


def test_null_model_keeps_flat_windows_flat():
    assert null_model(None, 1) is None


def test_run_v3_fits_carry_the_v3_signal(small, tmp_path):
    settings, inp, windows = small
    res, _ = run_v3(settings, "spot", inp, tmp_path, windows=windows, configs=CFGS, loo=False)
    sigs = {cw.xsec[0].signal for cw in res.fitted[CFGS[0].id] if cw.xsec is not None}
    assert sigs == {"lowvol"}


def test_run_v3_runs_every_null_seed(small, tmp_path):
    settings, inp, windows = small
    res, null = run_v3(settings, "spot", inp, tmp_path, windows=windows, configs=CFGS, loo=False)
    assert set(null["null_runs"]) == set(NULL_SEEDS) == {1, 2, 3, 4, 5}
    assert set(res.config_runs) == {c.id for c in CFGS}
    e = null["null_expectancy_r"]
    assert e is None or isinstance(e, float)


def test_run_v3_reports_null_sharpe_and_traded_seeds(small, tmp_path):
    settings, inp, windows = small
    _, null = run_v3(settings, "spot", inp, tmp_path, windows=windows, configs=CFGS, loo=False)
    assert 0 <= null["null_seeds_traded"] <= len(NULL_SEEDS)
    assert set(null) >= {"null_expectancy_r", "null_sharpe_daily", "null_seed_sharpe_daily"}


def test_plans_without_a_shuffle_run_the_null_once(small, tmp_path, monkeypatch):
    import luna.v3.runner as rmod

    calls = []
    real = rmod.run_selected_walkforward
    monkeypatch.setattr(rmod, "run_selected_walkforward", lambda *a, **k: calls.append(1) or real(*a, **k))
    settings, inp, windows = small
    vm = [V3Config("spot", "xsec", "1d", "broad", signal="volmanaged")]
    run_v3(settings, "spot", inp, tmp_path, windows=windows, configs=vm, loo=False)
    assert len(calls) == 1
