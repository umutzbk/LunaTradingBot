import numpy as np
import pandas as pd
import pytest

from luna.engine.backtest import default_instruments
from luna.risk.state import RiskState
from luna.v2.xsec import XSecParams, rebalance_stamps
from luna.v2.xsec_sim import run_xsec

DAY = 96
START = pd.Timestamp("2024-01-01", tz="UTC")
BIG = XSecParams(lookback_days=14, k=1, gross=1.0, vol_target=10.0)


def _panel(drifts: dict[str, list[float]], days: int, seed: int = 0, sigma: float = 0.0005,
           shocks: dict[str, tuple[int, float]] | None = None):
    """15m bars; asset a has daily log drift drifts[a][d // 7 % len] (weekly regimes)."""
    rng = np.random.default_rng(seed)
    n = days * DAY
    idx = pd.date_range(START, periods=n, freq="15min", tz="UTC")
    out = {}
    for a, regimes in drifts.items():
        week = (np.arange(n) // DAY // 7) % len(regimes)
        step = np.asarray(regimes)[week] / DAY + rng.normal(0, sigma, n)
        if shocks and a in shocks:
            bar, size = shocks[a]
            step[bar] += size
        close = 100.0 * np.exp(np.cumsum(step))
        open_ = np.r_[close[0], close[:-1]]
        out[a] = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.0002,
                               "low": np.minimum(open_, close) * 0.9998, "close": close,
                               "volume": 1.0, "trades": np.ones(n, dtype="int64"),
                               "filled": np.zeros(n, dtype=bool)}, index=idx)
    return out


def _run(panel, settings, params=BIG, kind="perp", start_day=40, end_day=None, **kw):
    s = settings
    if "max_drawdown" in kw:
        acc = s.accounts[kind].model_copy(update={"max_drawdown": kw.pop("max_drawdown")})
        s = s.model_copy(update={"accounts": {**s.accounts, kind: acc}})
    idx = next(iter(panel.values())).index
    end = idx[-1] + pd.Timedelta("15min") if end_day is None else START + pd.Timedelta(days=end_day)
    return run_xsec(panel, {}, params, sorted(panel), kind, s, START + pd.Timedelta(days=start_day), end,
                    instruments=default_instruments(sorted(panel), kind, s), **kw)


def _open_unrealized(res):
    pf = res.portfolio
    return sum(p.net_pnl() + sum(q * (pf.marks[a] - p.entry_ref_price[a]) for a, q in p.qty.items() if q)
               for p in pf.positions.values())


@pytest.fixture(scope="module")
def trend():
    return _panel({"A": [0.02], "B": [-0.02], "C": [0.0]}, days=120)


@pytest.mark.parametrize("kind", ["perp", "spot"])
def test_xsec_equity_identity(trend, settings, kind):
    res = _run(trend, settings, kind=kind)
    assert len(res.trades) > 5
    assert res.equity.iloc[-1] - res.initial_equity == pytest.approx(
        res.trades["pnl_net"].sum() + _open_unrealized(res), abs=1e-6)


def test_rebalance_fills_on_next_bar_open(trend, settings):
    res = _run(trend, settings)
    stamps = rebalance_stamps(next(iter(trend.values())).index, "weekly")
    assert set(res.trades["entry_ts"]) <= set(stamps + pd.Timedelta("15min"))
    assert set(res.trades["period"]) <= set(stamps)


def test_one_trade_per_period_leg_and_direction(trend, settings):
    t = _run(trend, settings).trades
    assert not t.duplicated(["period", "key"]).any()
    assert set(t.loc[t.key == "A", "direction"]) == {1} and set(t.loc[t.key == "B", "direction"]) == {-1}
    assert set(t["exit_reason"]) == {"rebalance"}
    np.testing.assert_allclose(t["r_multiple"], t["pnl_net"] / t["risk_amount"])
    assert (t["risk_precap"] >= t["risk_amount"] - 1e-9).all()  # projection only scales down


def test_carried_leg_rereferenced_without_cost(trend, settings, ):
    t = _run(trend, settings).trades.sort_values("period")
    a = t[t.key == "A"]
    first, later = a.iloc[0], a.iloc[1:]
    full_fee = settings.costs["perp"].taker_fee * first["gross_notional"]
    assert first["fees"] == pytest.approx(full_fee, rel=1e-6)
    # carried weeks only pay the resize delta, never a fresh round trip
    assert (later["fees"] < 0.25 * full_fee).all()


def test_catastrophic_stop_closes_leg_and_blocks_until_next_rebalance(settings):
    shock_bar = 60 * DAY + 10  # a Thursday after the first rebalance
    panel = _panel({"A": [0.02], "B": [-0.02], "C": [0.0]}, days=80, shocks={"A": (shock_bar, -0.15)})
    res = _run(panel, settings)
    t = res.trades
    stop = t[t.exit_reason == "catastrophic_stop"]
    assert list(stop["key"]) == ["A"]
    stop_ts = stop["exit_ts"].iloc[0]
    nxt = rebalance_stamps(panel["A"].index[panel["A"].index > stop_ts], "weekly")[0]
    reentry = t[(t.key == "A") & (t.entry_ts > stop_ts)]["entry_ts"]
    assert reentry.empty or reentry.min() >= nxt
    assert (res.risk_events["kind"] == "catastrophic_stop").sum() == 1


def test_daily_loss_blocks_rebalance_opens(trend, settings):
    first = rebalance_stamps(next(iter(trend.values())).index[40 * DAY:], "weekly")[0]
    state = RiskState(day=first.date(), day_start_equity=265.0, peak_equity=265.0,
                      entries_blocked_day=first.date())
    res = _run(trend, settings, initial_state=state)
    assert res.risk_events["detail"].str.startswith("R3_daily_loss").any()
    assert res.trades["entry_ts"].min().date() > first.date()


def test_drawdown_kill_flattens_in_realistic_not_in_edge(settings):
    panel = _panel({"A": [0.02, -0.03], "B": [-0.02, 0.03], "C": [0.0]}, days=100)
    real = _run(panel, settings, max_drawdown=0.02)
    assert (real.risk_events["kind"] == "FLATTEN_AND_KILL").any()
    assert "kill_switch" in set(real.trades["exit_reason"])
    assert not real.portfolio.positions
    edge = _run(panel, settings, max_drawdown=1.0)
    assert not (edge.risk_events["kind"] == "FLATTEN_AND_KILL").any()


def test_xsec_skips_assets_without_real_bar_or_history(settings):
    panel = _panel({"A": [0.02], "B": [-0.02], "C": [0.01], "D": [0.05]}, days=90)
    panel["D"] = panel["D"].iloc[60 * DAY:]  # listed late: < 31 daily closes for weeks
    stamp = rebalance_stamps(panel["A"].index, "weekly")[8]
    panel["C"].loc[stamp, "filled"] = True
    res = _run(panel, settings, params=XSecParams(14, 2, 1.0, 10.0), start_day=40)
    t = res.trades
    assert t[(t.key == "D") & (t.period < START + pd.Timedelta(days=91))].empty
    assert t[(t.key == "C") & (t.period == stamp)].empty


def test_k_clipped_and_empty_universe_runs_flat(trend, settings):
    s = settings
    res = run_xsec(trend, {}, BIG, [], "perp", s, START + pd.Timedelta(days=40),
                   START + pd.Timedelta(days=80))
    assert res.trades.empty and res.equity.iloc[-1] == res.initial_equity
    two = _run(trend, settings, params=XSecParams(14, 2, 1.0, 10.0))  # 3 assets: k clipped to 1
    assert two.trades.groupby("period").size().max() <= 2


def test_xsec_is_causal(trend, settings):
    cut = START + pd.Timedelta(days=90)
    full = _run(trend, settings, end_day=90)
    part = _run({a: df[df.index < cut] for a, df in trend.items()}, settings, end_day=90)
    pd.testing.assert_series_equal(full.equity, part.equity)
    pd.testing.assert_frame_equal(full.trades, part.trades)


def test_flatten_at_end_books_model_switch(trend, settings):
    res = _run(trend, settings, end_day=75, flatten_at_end=True)
    assert not res.portfolio.positions and not res.portfolio.holdings
    last = res.trades[res.trades.exit_ts == res.trades.exit_ts.max()]
    assert set(last["exit_reason"]) == {"model_switch"}
    assert res.equity.iloc[-1] - res.initial_equity == pytest.approx(res.trades["pnl_net"].sum(), abs=1e-6)


def test_spot_rebalance_buys_wait_for_sells(settings):
    # leaders rotate every week: A/B up then C/D up -> a full rotation of a k=2 long book
    panel = _panel({"A": [0.003, -0.003], "B": [0.003, -0.003], "C": [-0.003, 0.003],
                    "D": [-0.003, 0.003]}, days=100)
    res = _run(panel, settings, params=XSecParams(3, 2), kind="spot", max_drawdown=1.0)
    assert not (res.risk_events["kind"] == "catastrophic_stop").any()
    # buys that need the sells' proceeds are vetoed (R8 gross 1x / R17 cash) and retried later
    assert res.risk_events["detail"].str.match(r"R8_gross_leverage|R17_minimums_balance").any()
    assert {"A", "C"} <= set(res.trades["key"])
    assert (res.portfolio.cash >= -1e-9)
