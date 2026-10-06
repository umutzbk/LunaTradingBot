import math

import numpy as np
import pandas as pd
import pytest

from luna.config import load_settings
from luna.engine.backtest import default_instruments, run_backtest
from luna.execution.costs import CostModel
from luna.ranking.ranker import AcceptAllRanker
from luna.research.kernel import R_SIGNAL, R_STOP, R_TIME, REASONS, STOP_NONE, STOP_PRICE, simulate
from luna.research.tradelists import TRADE_COLUMNS, build_all_tradelists, build_tradelist
from luna.strategy.mean_reversion import MeanReversion
from luna.strategy.momentum import Momentum
from luna.strategy.naive import NaiveMeanReversion, NaiveMomentum, NaivePairs
from luna.strategy.pairs import Pairs

SETTINGS = load_settings()
START = pd.Timestamp("2000-01-01", tz="UTC")
END = pd.Timestamp("2100-01-01", tz="UTC")
BAR = pd.Timedelta("15min")


# --- kernel ----------------------------------------------------------------------------------------


def _kernel(close, entry, *, open_=None, low=None, high=None, exit_flag=None, stop_mode=STOP_NONE,
            stop_dist=0.01, max_hold=10_000, real=None):
    n = len(close)
    close = np.asarray(close, float)

    def col(x, default):
        return np.ascontiguousarray((default if x is None else np.asarray(x, float)).reshape(n, 1))

    ex = np.zeros(n, bool) if exit_flag is None else np.asarray(exit_flag, bool)
    out = simulate(
        col(open_, np.r_[close[0], close[:-1]]), col(high, close), col(low, close), col(None, close),
        np.ones((n, 1), bool) if real is None else np.asarray(real, bool).reshape(n, 1),
        np.asarray(entry, np.int8), np.ones((n, 1)), np.ones(n, bool), ex, ex, close,
        np.full(n, stop_dist), np.full(n, np.nan), np.full(n, np.nan), stop_mode, math.nan, math.nan,
        max_hold, False, math.inf, np.zeros(1), 0.0, np.zeros(1), np.zeros(n, bool),
        np.full((n, 1), np.nan),
    )
    return dict(zip(["sig", "entry", "exit", "dir", "reason", "gross", "cost", "fund", "unk"], out,
                    strict=True))


def test_kernel_entry_next_open():
    close = np.array([100.0, 101, 102, 103, 104, 105, 106, 107])
    open_ = np.array([100.0, 100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5])
    entry = np.zeros(8, np.int8)
    entry[2] = 1
    flag = np.zeros(8, bool)
    flag[4] = True  # exit decided at the close of bar 4 -> filled at the open of bar 5
    t = _kernel(close, entry, open_=open_, exit_flag=flag)
    assert list(t["sig"]) == [2] and list(t["entry"]) == [3] and list(t["exit"]) == [5]
    assert t["reason"][0] == R_SIGNAL and t["dir"][0] == 1
    # unit = 1 USD at the signal close (102): P&L between the fill opens 102.5 -> 104.5
    assert t["gross"][0] == pytest.approx((104.5 - 102.5) / 102.0, abs=1e-15)
    assert t["cost"][0] == pytest.approx(0.0, abs=1e-15)
    # an entry on a bar that is not real on the next bar never fills
    real = np.ones(8, bool)
    real[3] = False
    assert len(_kernel(close, entry, open_=open_, exit_flag=flag, real=real)["sig"]) == 0


def test_kernel_stop_at_close_not_intrabar():
    """Price stops are evaluated at the bar close as the engine does (an intrabar touch whose close
    recovers does not stop), and the exit fills at the next bar's open."""
    close = np.array([100.0, 100, 100, 100, 99.5, 98.0, 97.0, 97.0])
    low = close.copy()
    low[3] = 95.0  # touch through the stop level 99 at bar 3, close back at 100
    entry = np.zeros(8, np.int8)
    entry[1] = 1
    t = _kernel(close, entry, low=low, stop_mode=STOP_PRICE, stop_dist=0.01)  # level 99.0
    assert list(t["exit"]) == [6] and t["reason"][0] == R_STOP  # close 98 <= 99 at bar 5
    assert t["gross"][0] == pytest.approx((98.0 - 100.0) / 100.0)
    # short: stop level 101; the close of bar 2 reaches it exactly (<= triggers)
    close_s = np.array([100.0, 100, 101, 100, 100])
    entry_s = np.zeros(5, np.int8)
    entry_s[0] = -1
    t = _kernel(close_s, entry_s, stop_mode=STOP_PRICE, stop_dist=0.01)
    assert list(t["exit"]) == [3] and t["reason"][0] == R_STOP and t["dir"][0] == -1


def test_kernel_time_stop():
    close = np.full(12, 100.0)
    entry = np.zeros(12, np.int8)
    entry[0] = 1
    entry[3] = -1  # ignored: position still open at bar 3
    t = _kernel(close, entry, max_hold=4)
    # filled at bar 1 (bars_held 1), bars_held 4 at bar 4 -> exit fills at bar 5
    assert list(t["entry"]) == [1] and list(t["exit"]) == [5] and t["reason"][0] == R_TIME
    # an entry signal on the bar the previous exit fills is taken (the key is free again)
    entry[3] = 0
    entry[5] = -1
    t = _kernel(close, entry, max_hold=4)
    assert list(t["sig"]) == [0, 5] and list(t["dir"]) == [1, -1]
    assert list(t["exit"]) == [5, 10]


# --- synthetic market for the engine equivalence -----------------------------------------------


def _bars(close, rng, *, gap_sigma=3e-4, big_gap_p=0.01, filled_p=0.02, drop=None):
    """Bars whose open is NOT the previous close (small gaps, some beyond the 0.3% IOC band),
    with forward-filled bars (filled=True, flat at the previous close) and optionally a block of
    missing rows."""
    n = len(close)
    prev = np.r_[close[0], close[:-1]]
    gap = rng.normal(0.0, gap_sigma, n) + np.where(rng.random(n) < big_gap_p,
                                                    rng.choice([-0.006, 0.006], n), 0.0)
    open_ = prev * (1.0 + gap)
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.001, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.001, n)))
    df = pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close,
                       "volume": 1.0, "trades": np.int64(1), "filled": False},
                      index=pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC"))
    filled = np.flatnonzero(rng.random(n) < filled_p)
    filled = filled[filled > 0]
    for i in filled:  # flat bar at the previous close
        c = df["close"].iat[i - 1]
        df.iloc[i, :4] = c
        df.iat[i, df.columns.get_loc("filled")] = True
    df["volume"] = np.where(df["filled"], 0.0, 1.0)
    if drop is not None:
        df = df.drop(df.index[drop])
    return df


def _market(n=7_000, seed=0):
    """AAA swings between trends (momentum trades), BBB/CCC are cointegrated with AAA (pairs),
    CCC is listed midway; funding is known for AAA from a third of the way with gaps."""
    rng = np.random.default_rng(seed)
    sign = np.where((np.arange(n) // 900) % 2 == 0, 1.0, -1.0)
    jumps = np.where(rng.random(n) < 0.006, rng.choice([-0.06, 0.06], n), 0.0)  # z-stop shocks
    log_a = np.cumsum(rng.normal(0, 0.003, n) + sign * 0.0004 + jumps) + np.log(100.0)
    ou = np.zeros(n)
    eps = rng.normal(0, 0.002, n)
    for i in range(1, n):
        ou[i] = 0.97 * ou[i - 1] + eps[i]
    log_b = (log_a - np.log(100.0)) / 0.9 + ou + np.log(50.0)
    panel = {
        "AAA": _bars(np.exp(log_a), rng),
        "BBB": _bars(np.exp(log_b), rng, drop=slice(4_000, 4_050)),
        "CCC": _bars(np.exp(log_a + rng.normal(0, 0.002, n)), rng).iloc[n // 2:],
    }
    hours = pd.date_range(panel["AAA"].index[0], panel["AAA"].index[-1], freq="h")
    known = hours[hours >= hours[len(hours) // 3]]
    known = known[rng.random(len(known)) > 0.05]  # some unknown hours inside the known period
    funding = {"AAA": pd.DataFrame({"rate": rng.normal(2e-5, 1e-4, len(known))}, index=known),
               "BBB": pd.DataFrame({"rate": rng.normal(-1e-5, 1e-4, len(known))}, index=known)}
    return panel, funding


def _engine_settings(kind):
    lim = SETTINGS.accounts[kind].model_copy(update={
        "capital_usd": 1e9, "risk_per_trade": 1e-5, "max_risk_per_trade": 1.0, "max_open_risk": 1e6,
        "max_positions": 10_000, "max_gross_leverage": 1e6, "max_leg_notional_frac": 1e6,
        "daily_loss_limit": 1e9, "max_drawdown": 1e9, "max_orders_per_day": 10**9,
        "emergency_stop_frac": 1e9, "liq_distance_mult": 0.0,
    })
    return SETTINGS.model_copy(update={"accounts": {**SETTINGS.accounts, kind: lim}})


CASES = [
    (NaiveMomentum, {"fast": 48, "slow": 192, "atr_n": 48, "max_hold": 300}, "AAA", "perp"),
    (NaiveMomentum, {"fast": 48, "slow": 192, "atr_n": 48, "max_hold": 300}, "AAA", "spot"),
    (NaiveMeanReversion, {"span": 48}, "AAA", "perp"),
    (NaiveMeanReversion, {"span": 48}, "CCC", "spot"),
    (NaivePairs, {"span": 96, "beta": 0.9}, "BBB/AAA", "perp"),
    (Momentum, {"fast": 48, "slow": 192, "donchian": 96, "k_atr": 2}, "AAA", "perp"),
    (Momentum, {"fast": 48, "slow": 192, "donchian": 96, "k_atr": 3}, "AAA", "spot"),
    (MeanReversion, {"W": 96, "z_in": 2.0, "z_out": 0.5}, "BBB", "perp"),
    (MeanReversion, {"W": 96, "z_in": 2.0, "z_out": 0.0}, "AAA", "spot"),
    (Pairs, {"W": 96, "z_in": 1.5, "z_out": 0.0}, "BBB/AAA", "perp"),
]


@pytest.fixture(scope="module")
def market():
    return _market()


def _engine_vs_list(market, cls, params, key, kind):
    panel, funding = market
    strat = cls(params)
    settings = _engine_settings(kind)
    costs = CostModel(settings.costs[kind])
    res = run_backtest(panel, funding, [strat], {strat.name: [key]}, AcceptAllRanker(), kind,
                       settings, START, END,
                       instruments=default_instruments(sorted(panel), kind, settings))
    sf = strat.signals(panel, [key])[key]
    tl = build_tradelist(sf, panel, funding, costs, kind, strategy=strat,
                         band=settings.accounts[kind].max_price_band)
    return res, tl, costs


@pytest.mark.parametrize(("cls", "params", "key", "kind"), CASES,
                         ids=[f"{c[0].__name__}-{c[2]}-{c[3]}" for c in CASES])
def test_tradelist_matches_backtest_single_strategy(market, cls, params, key, kind):
    res, tl, costs = _engine_vs_list(market, cls, params, key, kind)
    eng = res.trades.sort_values("entry_ts").reset_index(drop=True)
    tl = tl.sort_values("entry_ts").reset_index(drop=True)
    assert len(eng) >= 3
    assert list(tl.columns) == TRADE_COLUMNS
    assert len(tl) == len(eng)
    assert (tl["entry_ts"].to_numpy() == eng["entry_ts"].to_numpy()).all()
    assert (tl["exit_ts"].to_numpy() == eng["exit_ts"].to_numpy()).all()
    assert (tl["direction"].to_numpy() == eng["direction"].astype(int).to_numpy()).all()
    assert list(tl["exit_reason"]) == list(eng["exit_reason"])
    assert list(tl["regime"]) == list(eng["regime"])
    # engine gross notional at the signal mids, from the constructor's sizing identity
    liq = "taker" if kind == "perp" else "maker"
    legs = key.split("/")
    rt = np.mean([costs.round_trip_frac(a, liq) for a in legs])  # equal half spreads per leg
    assert len({costs.round_trip_frac(a, liq) for a in legs}) == 1
    g = eng["risk_amount"].astype(float) / (tl["stop_distance"] + rt)
    np.testing.assert_allclose(tl["ret_net"], eng["pnl_net"].astype(float) / g, rtol=0, atol=1e-9)
    np.testing.assert_allclose(tl["funding"], eng["funding"].astype(float) / g, rtol=0, atol=1e-12)
    assert (tl["funding_unknown_bars"].to_numpy() == eng["funding_unknown_bars"].astype(int)).all()
    np.testing.assert_allclose(tl["ret_net"], tl["ret_gross"] - tl["cost"] + tl["funding"], atol=1e-15)
    np.testing.assert_allclose(tl["r_net"], tl["ret_net"] / tl["stop_distance"])
    assert (tl["day"] == tl["exit_ts"].dt.date).all()
    assert (tl["cost"] != 0).all()


def test_equivalence_covers_engine_paths(market):
    """The synthetic market exercises every path the equivalence test is meant to pin: all exit
    reasons (a stop for every exit rule), both directions, pair leg failures, funding known and
    unknown, and entries that do not fill (gaps beyond the IOC band, post-only not traded through,
    forward-filled bars)."""
    reasons, dirs, unknown, known = set(), set(), 0, 0
    statuses, stopped = set(), set()
    for cls, params, key, kind in CASES:
        res, tl, _ = _engine_vs_list(market, cls, params, key, kind)
        reasons |= set(tl["exit_reason"])
        if (tl["exit_reason"] == "stop").any():
            stopped.add(cls)
        dirs |= set(tl["direction"])
        unknown += int(tl["funding_unknown_bars"].sum())
        known += int((tl["funding"] != 0).sum())
        statuses |= set(res.candidates["status"])
    assert {"signal", "stop", "trailing", "time", "leg_failure"} <= reasons
    assert stopped == {NaiveMomentum, NaiveMeanReversion, NaivePairs, Momentum, MeanReversion, Pairs}
    assert dirs == {-1, 1}
    assert unknown > 0 and known > 0
    assert "unfilled" in statuses


def test_spot_tradelist_drops_shorts(market):
    panel, funding = market
    strat = NaiveMeanReversion({"span": 48})
    sf = strat.signals(panel, ["AAA"])["AAA"]
    costs = CostModel(SETTINGS.costs["perp"])
    perp = build_tradelist(sf, panel, funding, costs, "perp", strategy=strat)
    spot = build_tradelist(sf, panel, funding, CostModel(SETTINGS.costs["spot"]), "spot",
                           strategy=strat)
    assert (perp["direction"] == -1).any() and (perp["direction"] == 1).any()
    assert len(spot) > 0 and (spot["direction"] == 1).all()
    assert (spot["funding"] == 0).all() and (spot["funding_unknown_bars"] == 0).all()
    # pairs always have a short leg: no spot trades at all, and build_all skips pair keys
    p = NaivePairs({"span": 96})
    psf = p.signals(panel, ["BBB/AAA"])["BBB/AAA"]
    assert len(build_tradelist(psf, panel, funding, costs, "spot", strategy=p)) == 0
    out = build_all_tradelists({"naive_pairs": NaivePairs}, panel, funding, costs, "spot",
                               {"naive_pairs": ["BBB/AAA"]}, cache_dir=None)
    assert len(out) == 0 and list(out.columns) == TRADE_COLUMNS


def test_funding_unknown_counted(market):
    panel, funding = market
    strat = NaiveMeanReversion({"span": 48})
    sf = strat.signals(panel, ["AAA"])["AAA"]
    costs = CostModel(SETTINGS.costs["perp"])
    tl = build_tradelist(sf, panel, funding, costs, "perp", strategy=strat)
    first = funding["AAA"].index[0]
    rates = funding["AAA"]["rate"]
    for _, t in tl.iterrows():
        hours = pd.date_range(t["entry_ts"].ceil("h"), t["exit_ts"], freq="h")
        hours = hours[(hours > t["entry_ts"]) & (hours <= t["exit_ts"])]
        n_unknown = int((~hours.isin(rates.index)).sum())
        assert t["funding_unknown_bars"] == n_unknown
        if t["exit_ts"] <= first:
            assert t["funding"] == 0.0 and n_unknown == len(hours)
    assert (tl["funding_unknown_bars"] > 0).any()
    # no funding data at all: every held hour unknown, nothing booked
    none = build_tradelist(sf, panel, {}, costs, "perp", strategy=strat)
    assert (none["funding"] == 0).all()
    long_held = (none["exit_ts"] - none["entry_ts"]) >= pd.Timedelta("1h")
    assert long_held.any() and (none.loc[long_held, "funding_unknown_bars"] > 0).all()
    np.testing.assert_allclose(none["ret_net"], none["ret_gross"] - none["cost"])


def test_build_all_tradelists_grids_and_cache(market, tmp_path):
    panel, funding = market
    costs = CostModel(SETTINGS.costs["perp"])
    strategies = {"mean_reversion": MeanReversion, "naive_mean_reversion": NaiveMeanReversion}
    keys = {"mean_reversion": ["AAA", "BBB"], "naive_mean_reversion": ["AAA"]}
    out = build_all_tradelists(strategies, panel, funding, costs, "perp", keys, cache_dir=tmp_path)
    assert list(out.columns) == TRADE_COLUMNS
    ids = {MeanReversion(g).params_id for g in MeanReversion.GRID}
    mr = out[out["strategy"] == "mean_reversion"]
    assert set(mr["params_id"]) == ids and set(mr["key"]) == {"AAA", "BBB"}
    assert set(out.loc[out["strategy"] == "naive_mean_reversion", "key"]) == {"AAA"}
    assert set(out["exit_reason"]) <= set(REASONS)
    # one position at a time per (strategy, params, key)
    for _, g in out.groupby(["strategy", "params_id", "key"]):
        g = g.sort_values("entry_ts")
        assert (g["entry_ts"].iloc[1:].to_numpy() >= g["exit_ts"].iloc[:-1].to_numpy()).all()
    path = tmp_path / "tradelists_perp.parquet"
    key_file = path.with_suffix(".key")
    assert path.exists() and key_file.exists()
    stamp, key = path.stat().st_mtime_ns, key_file.read_text()
    again = build_all_tradelists(strategies, panel, funding, costs, "perp", keys, cache_dir=tmp_path)
    assert path.stat().st_mtime_ns == stamp  # served from the cache
    pd.testing.assert_frame_equal(again, out)
    # changed data -> recomputed and the cache overwritten
    panel2 = {**panel, "AAA": panel["AAA"].iloc[:-500]}
    third = build_all_tradelists(strategies, panel2, funding, costs, "perp", keys, cache_dir=tmp_path)
    key2 = key_file.read_text()
    assert key2 != key
    aaa = third[third["key"] == "AAA"]
    assert aaa["exit_ts"].max() <= panel2["AAA"].index[-1] < out.loc[out["key"] == "AAA", "exit_ts"].max()
    pd.testing.assert_frame_equal(pd.read_parquet(path), third)
    # changed grid inputs (costs) -> recomputed too
    cheap = CostModel(SETTINGS.costs["perp"].model_copy(update={"slippage_bps": 0.0}))
    build_all_tradelists(strategies, panel2, funding, cheap, "perp", keys, cache_dir=tmp_path)
    assert key_file.read_text() not in (key, key2)
