import math

import pandas as pd
import pytest

from luna.engine.backtest import default_instruments, run_backtest
from luna.engine.walkforward import _simulate, make_windows
from luna.ranking.ranker import AcceptAllRanker
from luna.report.walkforward import Inputs
from luna.v2.configs import V2Config, primary_configs, restrict_universe, sensitivity_configs
from luna.v2.walkforward import (
    ConfigWindowModel,
    build_v2_tradelists,
    count_trials,
    first_active,
    fit_config,
    fit_signal_config,
    fit_xsec_config,
    run_config_walkforward,
    run_selected_walkforward,
    select_candidate,
    v2_variants,
)
from luna.v2.xsec import XSecParams

BAR = pd.Timedelta("15min")


@pytest.fixture(scope="module")
def setup(v2_panel, v2_settings_small):
    settings = v2_settings_small
    panel = {a: v2_panel[a] for a in ("BTC", "ETH", "AAA", "BBB")}
    end = panel["BTC"].index[-1] + BAR
    windows, _ = make_windows(panel["BTC"].index[0], end, settings.walkforward)
    inp = Inputs(panel, {}, None, default_instruments(sorted(panel), "perp", settings), [])
    return settings, inp, windows


@pytest.fixture(scope="module")
def tl(setup):
    settings, inp, windows = setup
    return build_v2_tradelists(inp, settings, "perp", windows, cache_dir=None)


def test_primary_config_counts():
    assert len(primary_configs("perp")) == 18 and len(primary_configs("spot")) == 14
    assert len(sensitivity_configs("perp")) == 2 and all(
        c.rebalance == "daily" for c in sensitivity_configs("spot")
    )
    ids = [c.id for c in primary_configs("perp")]
    assert len(set(ids)) == 18 and "perp|xsec|1d|broad" in ids and "perp|tsmom|15m|btc_eth" in ids


def test_btc_eth_universe_mode():
    assert restrict_universe(["SOL", "ETH", "BTC"], "btc_eth") == ["ETH", "BTC"]
    assert restrict_universe(["SOL", "ETH"], "broad") == ["SOL", "ETH"]


def test_count_trials():
    # perp: (tsmom 22 + breakout 22) x 2 universes + xsec 24 x 2 = 136; spot: (16 + 16) x 2 + 6 x 2 = 76
    assert count_trials(primary_configs("perp"))["grid_points"] == 136
    assert count_trials(primary_configs("spot"))["grid_points"] == 76


def test_v2_variants():
    assert set(v2_variants("perp", candidate=False)) == {"realistic", "edge"}
    c = v2_variants("perp", candidate=True)
    assert c["order_alt_edge"] == {"order_model": "maker", "max_drawdown_override": 1.0}
    assert {"funding_zero", "funding_estimated", "funding_estimated_x2"} <= set(c)
    s = v2_variants("spot", candidate=True)
    assert s["order_alt_edge"]["order_model"] == "taker" and not any(k.startswith("funding") for k in s)


def test_fit_signal_config_uses_train_only(setup, tl):
    settings, inp, windows = setup
    cfg = V2Config("perp", "tsmom", "1h", "broad")
    w = windows[1]
    base = fit_signal_config(cfg, w, tl, inp, settings)
    assert base.params_id is not None and math.isfinite(base.objective)
    # destroy everything from the train end on: the fit must not change
    cut = {a: df.copy() for a, df in inp.panel.items()}
    for df in cut.values():
        late = df.index >= w.train_end
        df.loc[late, ["open", "high", "low", "close"]] *= 3.0
    inp2 = Inputs(cut, {}, None, inp.instruments, [])
    tl2 = build_v2_tradelists(inp2, settings, "perp", windows, cache_dir=None)
    again = fit_signal_config(cfg, w, tl2, inp2, settings)
    assert (again.params_id, again.objective, again.accepted, again.universe) == (
        base.params_id,
        base.objective,
        base.accepted,
        base.universe,
    )


def test_fit_xsec_config_selects_on_train_only(setup):
    settings, inp, windows = setup
    cfg = V2Config("spot", "xsec", "1d", "broad")
    w = windows[2]
    sinp = Inputs(inp.panel, {}, None, default_instruments(sorted(inp.panel), "spot", settings), [])
    base = fit_xsec_config(cfg, w, sinp, settings)
    assert base.params_id is not None and base.xsec[1] == base.universe
    cut = {a: df.copy() for a, df in inp.panel.items()}
    for df in cut.values():
        df.loc[df.index >= w.train_end, ["open", "high", "low", "close"]] *= 0.3
    again = fit_xsec_config(cfg, w, Inputs(cut, {}, None, sinp.instruments, []), settings)
    assert (again.params_id, again.objective, again.accepted) == (
        base.params_id,
        base.objective,
        base.accepted,
    )


def _stub(cid, obj, accepted, w=None):
    fam = cid.split("|")[1]
    return ConfigWindowModel(
        V2Config("perp", fam, "1h" if fam != "xsec" else "1d", "broad"), w, ["BTC"], "p", obj, accepted
    )


def test_select_candidate_argmax_ties_and_none():
    a = [_stub("perp|tsmom", 0.2, True), _stub("perp|tsmom", 0.5, False), _stub("perp|tsmom", 0.1, True)]
    b = [
        _stub("perp|breakout", 0.2, True),
        _stub("perp|breakout", 0.1, True),
        _stub("perp|breakout", -1, False),
    ]
    sel = select_candidate({"a": a, "b": b})
    assert sel[0].config.family == "breakout"  # tie 0.2: "perp|breakout|..." < "perp|tsmom|..."
    assert sel[1].config.family == "breakout"  # 0.5 is not accepted
    assert sel[2].config.family == "tsmom"
    assert select_candidate({"a": [_stub("perp|tsmom", 1.0, False)]}) == [None]


def test_first_active_skips_leading_empty_windows():
    e = ConfigWindowModel(V2Config("perp", "tsmom", "1h", "broad"), None, [], None, math.nan, False)
    f = _stub("perp|tsmom", 0.1, False)
    assert first_active({"a": [e, e, f], "b": [e, f, f]}) == 1


def test_run_config_walkforward_signal_matches_v1_simulate(setup, tl):
    settings, inp, windows = setup
    cfg = V2Config("perp", "tsmom", "1h", "broad")
    fitted = fit_config(cfg, windows, tl, inp, settings)
    for cw in fitted:  # force trades: accept every candidate
        cw.signal.ranker = AcceptAllRanker()
    res = run_config_walkforward(fitted, 0, inp, settings, max_drawdown_override=1.0)
    ref = _simulate(
        [cw.signal for cw in fitted],
        inp.panel,
        inp.funding,
        settings.model_copy(
            update={
                "accounts": {
                    **settings.accounts,
                    "perp": settings.accounts["perp"].model_copy(update={"max_drawdown": 1.0}),
                }
            }
        ),
        "perp",
        1.0,
        "known_only",
        inp.instruments,
    )
    trades = pd.concat([r.trades for r in ref], ignore_index=True)
    assert len(res.oos.trades) == len(trades) > 0
    assert list(res.oos.trades["pnl_net"]) == pytest.approx(list(trades["pnl_net"]))
    assert list(res.per_window["status"]) == ["ok"] * len(windows)


def _open_unrealized(pf):
    return sum(
        p.net_pnl() + sum(q * (pf.marks[a] - p.entry_ref_price[a]) for a, q in p.qty.items() if q)
        for p in pf.positions.values()
    )


def test_selected_walkforward_engine_switch_flattens(setup, tl):
    settings, inp, windows = setup
    sig = fit_config(V2Config("perp", "tsmom", "1h", "broad"), windows, tl, inp, settings)
    for cw in sig:
        cw.signal.ranker = AcceptAllRanker()
        cw.accepted = True
    xs = [
        ConfigWindowModel(
            V2Config("perp", "xsec", "1d", "broad"),
            w,
            ["BTC", "ETH", "AAA", "BBB"],
            "x",
            0.0,
            True,
            xsec=(XSecParams(14, 1, 1.0, 10.0), ["BTC", "ETH", "AAA", "BBB"]),
        )
        for w in windows
    ]
    plan = [sig[0], xs[1], xs[2], sig[3], None]
    res = run_selected_walkforward(plan, windows, 0, inp, settings, "perp", max_drawdown_override=1.0)
    oos = res.oos
    assert "model_switch" in set(oos.trades["exit_reason"])
    assert oos.equity.iloc[-1] - oos.initial_equity == pytest.approx(
        oos.trades["pnl_net"].sum() + _open_unrealized(oos.portfolio), abs=1e-6
    )
    # a window after a switch starts flat: nothing opened before its start is still open after it
    switch = windows[1].test_start
    assert not ((oos.trades["entry_ts"] < switch) & (oos.trades["exit_ts"] > switch)).any()
    assert pd.isna(res.per_window["config"].iloc[-1])
    assert res.per_window["status"].iloc[-1] == "flat: no accepted configuration"


def test_flatten_at_end_closes_everything(setup):
    settings, inp, windows = setup
    from luna.v2.strategies import make_tsmom

    T = make_tsmom("1h")
    s = T(T.GRID[0])
    w = windows[0]
    res = run_backtest(
        {"BTC": inp.panel["BTC"]},
        {},
        [s],
        {s.name: ["BTC"]},
        AcceptAllRanker(),
        "perp",
        settings,
        w.test_start,
        w.test_end,
        instruments=inp.instruments,
        flatten_at_end=True,
    )
    assert not res.portfolio.positions and not res.pending_exits.orders
    last = res.bar_stats.iloc[-1]
    assert last["gross_leverage"] == 0 and last["n_positions"] == 0 and last["open_risk"] == 0
    assert last["net_leverage"] == 0 and last["max_asset_share"] == 0
    assert res.equity.iloc[-1] - res.initial_equity == pytest.approx(res.trades["pnl_net"].sum(), abs=1e-6)
    plain = run_backtest(
        {"BTC": inp.panel["BTC"]},
        {},
        [s],
        {s.name: ["BTC"]},
        AcceptAllRanker(),
        "perp",
        settings,
        w.test_start,
        w.test_end,
        instruments=inp.instruments,
    )
    assert "model_switch" not in set(plain.trades["exit_reason"])


def test_v1_simulate_unchanged_by_extensions(setup, tl):
    """Explicit V2 defaults (initial=None, flatten_last=False, order_model=None, owners=None) give V1's
    result trade for trade."""
    settings, inp, windows = setup
    fitted = fit_config(V2Config("perp", "tsmom", "1h", "broad"), windows, tl, inp, settings)
    for cw in fitted:
        cw.signal.ranker = AcceptAllRanker()
    models = [cw.signal for cw in fitted]
    args = (models, inp.panel, inp.funding, settings, "perp", 1.0, "known_only", inp.instruments)
    a = _simulate(*args)
    b = _simulate(*args, initial=None, flatten_last=False, order_model=None, owners=None)
    for x, y in zip(a, b, strict=True):
        pd.testing.assert_frame_equal(x.trades, y.trades)
        pd.testing.assert_series_equal(x.equity, y.equity)


def test_flatten_at_end_default_false_keeps_v1(setup):
    settings, inp, windows = setup
    from luna.v2.strategies import make_tsmom

    T = make_tsmom("1h")
    s = T(T.GRID[0])
    w = windows[0]
    kw = dict(instruments=inp.instruments)
    a = run_backtest(
        {"BTC": inp.panel["BTC"]},
        {},
        [s],
        {s.name: ["BTC"]},
        AcceptAllRanker(),
        "perp",
        settings,
        w.test_start,
        w.test_end,
        **kw,
    )
    b = run_backtest(
        {"BTC": inp.panel["BTC"]},
        {},
        [s],
        {s.name: ["BTC"]},
        AcceptAllRanker(),
        "perp",
        settings,
        w.test_start,
        w.test_end,
        flatten_at_end=False,
        order_model=None,
        **kw,
    )
    pd.testing.assert_frame_equal(a.trades, b.trades)
    pd.testing.assert_frame_equal(a.bar_stats, b.bar_stats)
