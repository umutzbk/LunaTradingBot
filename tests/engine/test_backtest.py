import math
import time

import numpy as np
import pandas as pd
import pytest

from luna.config import load_settings
from luna.engine.backtest import BacktestResult, run_backtest
from luna.ranking.ranker import AcceptAllRanker, RandomRanker
from luna.report.metrics import daily_returns, summary
from luna.strategy.base import SignalFrame
from luna.strategy.naive import NaiveMeanReversion, NaiveMomentum, NaivePairs
from luna.types import Candidate, ExitSpec, Instrument, Leg, StopSpec
from tests.helpers import synthetic_bars, synthetic_pair

SETTINGS = load_settings()
BAR = pd.Timedelta("15min")
START = pd.Timestamp("2000-01-01", tz="UTC")
END = pd.Timestamp("2100-01-01", tz="UTC")


def insts(assets, kind="perp"):
    venue = "kraken_futures" if kind == "perp" else "kraken_spot"
    return {a: Instrument(a, a, venue, kind, 1e-6, 0.01, 1e-6, 0.0, 0.0002, 0.0005, 0.02, 0.01)
            for a in assets}


def bt(panel, strategies, keys, kind="perp", funding=None, **kw) -> BacktestResult:
    return run_backtest(panel, funding or {}, strategies, keys, AcceptAllRanker(), kind, SETTINGS,
                        kw.pop("start", START), kw.pop("end", END),
                        instruments=insts(panel, kind), **kw)


class Forced:
    """Test strategy: enters at given bar positions, exits by signal at given positions."""

    name = "forced"

    def __init__(self, entries: dict[int, int], exits: set[int], stop=0.01):
        self.params = {}
        self.entries, self.exits, self.stop = entries, exits, stop

    def signals(self, panel, keys):
        out = {}
        for key in keys:
            assets = key.split("/")
            idx = panel[assets[0]].index
            for a in assets[1:]:
                idx = idx.intersection(panel[a].index)
            entry = pd.Series(np.zeros(len(idx), dtype="int8"), index=idx)
            for i, d in self.entries.items():
                entry.iloc[i] = d
            ex = pd.Series(False, index=idx)
            ex.iloc[sorted(self.exits)] = True
            out[key] = SignalFrame(
                key=key, legs_beta=None, entry=entry, exit_long=ex, exit_short=ex,
                stop_distance=pd.Series(self.stop, index=idx), stop_kind="price", stop_z=None,
                z=None, atr=None, exit_spec=ExitSpec(10_000), regime=pd.Series("NONE", index=idx),
                ref=panel[assets[0]]["close"].reindex(idx),
            )
        return out

    def candidate_at(self, sf, i, mids):
        d = int(sf.entry.iloc[i])
        if d == 0:
            return None
        assets = sf.key.split("/")
        legs = (Leg(assets[0], 1.0),) if len(assets) == 1 else (Leg(assets[0], 1.0), Leg(assets[1], -1.0))
        ref = float(sf.ref.iloc[i])
        return Candidate(sf.entry.index[i], self.name, sf.key, legs, d, ref,
                         StopSpec("price", ref * (1 - d * self.stop), self.stop), sf.exit_spec,
                         "NONE", "forced", {})

    def should_exit(self, sf, i, pos, bar_close):
        return "signal" if bool(sf.exit_long.iloc[i]) else None


class ForcedPairs(NaivePairs):
    """NaivePairs with forced entries every 100 bars (alternating) and exits 50 bars later."""

    def signals(self, panel, keys):
        out = super().signals(panel, keys)
        for sf in out.values():
            n = len(sf.entry)
            pos = np.arange(n)
            entry = np.where((pos % 100 == 0) & (pos > 0), np.where(pos % 200 == 0, 1, -1), 0)
            sf.entry = pd.Series(entry.astype("int8"), index=sf.entry.index)
            ex = pd.Series(pos % 100 == 50, index=sf.entry.index)
            sf.exit_long, sf.exit_short = ex, ex
            sf.z = pd.Series(0.0, index=sf.entry.index)
            sf.stop_distance = pd.Series(0.01, index=sf.entry.index)
        return out


def swing_bars(n, seed, drift=0.0004, leg=1_500, sigma=0.002):
    """Log price with alternating down/up trends of `leg` bars (so EMA crosses happen)."""
    rng = np.random.default_rng(seed)
    sign = np.where((np.arange(n) // leg) % 2 == 0, -1.0, 1.0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, sigma, n) + sign * drift))
    bars = synthetic_bars(n, "random_walk", seed=seed)
    bars["close"] = close
    bars["open"] = np.r_[close[0], close[:-1]]
    bars["high"] = np.maximum(bars["open"], close) * (1 + np.abs(rng.normal(0, sigma / 2, n)))
    bars["low"] = np.minimum(bars["open"], close) * (1 - np.abs(rng.normal(0, sigma / 2, n)))
    return bars


# --- baseline behaviour ---------------------------------------------------------------------------


def test_random_walk_net_negative():
    bars = synthetic_bars(20_000, "random_walk", seed=11)
    res = bt({"SOL": bars}, [NaiveMeanReversion()], {"naive_mean_reversion": ["SOL"]})
    tr = res.trades
    assert len(tr) >= 50
    gross_r = tr["pnl_gross"] / tr["risk_amount"]
    assert abs(gross_r.mean()) < 0.15
    assert tr["pnl_net"].sum() < 0
    assert res.equity.iloc[-1] < SETTINGS.accounts["perp"].capital_usd
    # cost identity per trade
    np.testing.assert_allclose(
        tr["pnl_net"], tr["pnl_gross"] - tr["fees"] - tr["spread_slip"] + tr["funding"], atol=1e-9)


def test_trend_momentum_profits_gross():
    bars = swing_bars(12_000, seed=3)
    res = bt({"BTC": bars}, [NaiveMomentum()], {"naive_momentum": ["BTC"]})
    tr = res.trades
    assert len(tr) >= 3
    assert tr["pnl_gross"].sum() > 0
    # the opposite cross fires on the bar the open position exits -> rejected "already_open"
    c = res.candidates
    assert (c.loc[c["status"] == "rejected", "reason"] == "already_open").all()


def test_constant_spread_pairs_loses_costs():
    a, b = synthetic_pair(3_000, "constant_spread", seed=2)
    res = bt({"ETH": a, "BTC": b}, [ForcedPairs()], {"naive_pairs": ["ETH/BTC"]})
    tr = res.trades
    assert len(tr) >= 10
    costs = tr["fees"] + tr["spread_slip"]
    assert (costs > 0).all()
    assert (tr["funding"] == 0).all()
    np.testing.assert_allclose(tr["pnl_net"], -costs, rtol=0.02, atol=1e-6)
    assert set(tr["exit_reason"]) == {"signal"}
    # the opposite cross fires on the bar the open position exits -> rejected "already_open"
    c = res.candidates
    assert (c.loc[c["status"] == "rejected", "reason"] == "already_open").all()


# --- risk ---------------------------------------------------------------------------------------


def _multi_panel(n, seed=0):
    names = ["BTC", "ETH", "SOL", "XRP", "ADA"]
    kinds = ["random_walk", "ou", "trend", "random_walk", "ou"]
    return {a: synthetic_bars(n, k, seed=seed + i, sigma=0.003)
            for i, (a, k) in enumerate(zip(names, kinds, strict=True))}


def _with_limits(**update):
    lim = SETTINGS.accounts["perp"].model_copy(update=update)
    return SETTINGS.model_copy(update={"accounts": {**SETTINGS.accounts, "perp": lim}})


def test_risk_invariants_hold_every_bar():
    panel = _multi_panel(5_000)
    strategies = [NaiveMeanReversion({"span": 48}), NaiveMomentum({"fast": 24, "slow": 96, "atr_n": 24})]
    keys = {"naive_mean_reversion": list(panel), "naive_momentum": list(panel)}
    # loose loss limits so the run keeps trading for all 5k bars (the naive baselines lose)
    settings = _with_limits(max_drawdown=0.9, daily_loss_limit=0.5)
    res = run_backtest(panel, {}, strategies, keys, AcceptAllRanker(), "perp", settings, START, END,
                       instruments=insts(panel))
    lim = settings.accounts["perp"]
    stats = res.bar_stats
    assert len(stats) == 5_000
    assert stats["n_positions"].max() == lim.max_positions  # the cap binds and holds
    assert (stats["n_positions"] <= lim.max_positions).all()
    entry_lev = stats["entry_gross_leverage"].dropna()
    assert len(entry_lev) > 50 and entry_lev.max() > 1.0
    assert (entry_lev <= lim.max_gross_leverage + 1e-9).all()  # R8 at order time, every entry bar
    assert (stats["gross_leverage"] <= lim.max_gross_leverage * 1.05).all()  # only price drift on top
    assert res.trades["exit_ts"].max() > panel["BTC"].index[4_500]  # traded through the whole run
    # exact accounting: equity change = closed trades' net + open positions' net at the last marks
    pf = res.portfolio
    open_pnl = sum(
        p.net_pnl() + sum(q * (pf.marks[a] - p.entry_ref_price[a]) for a, q in p.qty.items() if q)
        for p in pf.positions.values()
    )
    capital = lim.capital_usd
    assert res.equity.iloc[-1] - capital == pytest.approx(res.trades["pnl_net"].sum() + open_pnl, abs=1e-8)
    assert (stats["open_risk"] <= lim.max_open_risk * stats["equity"] + 1e-9).all()
    assert len(res.trades) > 20
    reasons = set(res.candidates["reason"].dropna())
    assert "max_positions" in reasons or any(str(r).startswith("R7") for r in reasons)
    # every position_id traded at most once, no overlapping positions of the same (strategy, key)
    assert res.trades["position_id"].is_unique
    for _, g in res.trades.groupby(["strategy", "key"]):
        g = g.sort_values("entry_ts")
        assert (g["entry_ts"].iloc[1:].to_numpy() >= g["exit_ts"].iloc[:-1].to_numpy()).all()


def test_emergency_stop_closes_intrabar():
    bars = synthetic_bars(600, "random_walk", seed=5)
    k = 320
    bars.iloc[k, bars.columns.get_loc("low")] = bars["open"].iloc[k] * 0.85  # crash inside bar k
    strat = Forced({300: 1}, {500})
    res = bt({"SOL": bars}, [strat], {"forced": ["SOL"]})
    (tr,) = res.trades.itertuples()
    assert tr.exit_reason == "emergency_stop"
    assert tr.exit_ts == bars.index[k]
    eq_threshold = SETTINGS.accounts["perp"].emergency_stop_frac * res.equity.loc[bars.index[k - 1]]
    # loss is the threshold plus the exit costs (stop slippage/spread + taker fee), nothing like the crash
    assert -tr.pnl_net == pytest.approx(eq_threshold, rel=0.15)
    assert -tr.pnl_net >= eq_threshold * 0.99
    assert (res.risk_events["kind"] == "emergency_stop").any()


def test_pairs_leg_failure_flattens_filled_leg():
    a, b = synthetic_pair(600, "cointegrated", seed=1)
    b.iloc[301, b.columns.get_loc("filled")] = True  # leg B cannot fill on bar 301
    strat = Forced({300: 1}, {500})
    res = bt({"ETH": a, "BTC": b}, [strat], {"forced": ["ETH/BTC"]})
    (tr,) = res.trades.itertuples()
    assert tr.exit_reason == "leg_failure"
    assert tr.entry_ts == a.index[301] and tr.exit_ts == a.index[302]
    assert (res.risk_events["kind"] == "leg_failure").any()
    assert res.candidates["status"].tolist() == ["partial"]


def test_drawdown_kill_flattens_and_blocks():
    bars = synthetic_bars(800, "random_walk", seed=5, sigma=0.0005)
    close = bars["close"].to_numpy().copy()
    close[400:] *= 0.2  # catastrophic gap down while the position is open -> > 15% DD
    bars["close"] = close
    bars["open"] = np.r_[close[0], close[:-1]]
    bars["high"] = np.maximum(bars["open"], bars["close"]) * 1.0001
    bars["low"] = np.minimum(bars["open"], bars["close"]) * 0.9999
    settings = _with_limits(emergency_stop_frac=10.0)  # no emergency stop
    strat = Forced({390: 1, 600: 1}, {700}, stop=0.002)
    res = run_backtest({"SOL": bars}, {}, [strat], {"forced": ["SOL"]}, AcceptAllRanker(), "perp",
                       settings, START, END, instruments=insts(["SOL"]))
    assert res.trades["exit_reason"].tolist() == ["kill_switch"]
    assert (res.risk_events["kind"] == "FLATTEN_AND_KILL").sum() == 1
    late = res.candidates[res.candidates["ts"] == bars.index[600]]
    assert len(late) == 1 and late["reason"].str.startswith("R1").all()


# --- causality / data edge cases -----------------------------------------------------------------


def test_no_lookahead_engine():
    panel = _multi_panel(3_000, seed=7)
    strategies = [NaiveMeanReversion({"span": 48})]
    keys = {"naive_mean_reversion": list(panel)}
    full = bt(panel, strategies, keys)
    k = 2_000
    cut = bt({a: df.iloc[:k] for a, df in panel.items()}, strategies, keys)
    t_k = panel["BTC"].index[k]
    a = full.trades[full.trades["exit_ts"] < t_k].reset_index(drop=True)
    b = cut.trades[cut.trades["exit_ts"] < t_k].reset_index(drop=True)
    assert len(a) > 5
    pd.testing.assert_frame_equal(a, b)
    pd.testing.assert_series_equal(full.equity.iloc[:k], cut.equity)


def test_backtest_handles_asset_listed_midway():
    n = 4_000
    btc = swing_bars(n, seed=1, leg=500)
    late = swing_bars(n, seed=2, leg=300).iloc[n // 2:]
    strat = NaiveMomentum({"fast": 24, "slow": 96, "atr_n": 24})
    res = bt({"BTC": btc, "NEW": late}, [strat], {"naive_momentum": ["BTC", "NEW"]})
    listing = late.index[0]
    warm = listing + 96 * BAR
    new_c = res.candidates[res.candidates["key"] == "NEW"]
    assert len(new_c) > 0 and (new_c["ts"] >= warm).all()
    new_t = res.trades[res.trades["key"] == "NEW"]
    assert (new_t["entry_ts"] > warm).all()
    assert res.equity.index[0] == btc.index[0] and len(res.equity) == n
    assert np.isfinite(res.equity).all()


def test_funding_unknown_flagged():
    bars = synthetic_bars(6_000, "ou", seed=4, sigma=0.003)
    hours = pd.date_range(bars.index[0], bars.index[-1], freq="1h")
    known_from = hours[len(hours) // 2]
    funding = {"SOL": pd.DataFrame({"rate": 1e-4}, index=hours[hours >= known_from])}
    strat = NaiveMeanReversion({"span": 48, "max_hold": 40})
    res = bt({"SOL": bars}, [strat], {"naive_mean_reversion": ["SOL"]}, funding=funding)
    assert res.funding_known_from == {"SOL": known_from}
    tr = res.trades
    before = tr[tr["exit_ts"] < known_from - pd.Timedelta("1h")]
    after = tr[tr["entry_ts"] > known_from]
    held = before[(before["exit_ts"] - before["entry_ts"]) >= pd.Timedelta("2h")]
    assert len(held) > 0 and len(after) > 0
    assert (held["funding_unknown_bars"] > 0).all()
    assert (before["funding"] == 0).all()
    assert (after["funding_unknown_bars"] == 0).all()
    longs = after[(after["direction"] == 1) & ((after["exit_ts"] - after["entry_ts"]) >= pd.Timedelta("2h"))]
    assert len(longs) > 0 and (longs["funding"] < 0).all()  # positive rate: longs pay

    # sensitivity: estimated funding fills the unknown period but keeps the flag
    est = bt({"SOL": bars}, [strat], {"naive_mean_reversion": ["SOL"]}, funding=funding,
             funding_mode="estimated_x2")
    eb = est.trades[est.trades["exit_ts"] < known_from - pd.Timedelta("1h")]
    eheld = eb[(eb["exit_ts"] - eb["entry_ts"]) >= pd.Timedelta("2h")]
    assert len(eheld) > 0
    assert (eheld["funding_unknown_bars"] > 0).all() and (eheld["funding"] != 0).all()


def test_spot_drops_shorts():
    bars = synthetic_bars(4_000, "ou", seed=8, sigma=0.003)
    res = bt({"SOL": bars}, [NaiveMeanReversion({"span": 48})], {"naive_mean_reversion": ["SOL"]},
             kind="spot")
    c = res.candidates
    assert (c["direction"] == -1).any()
    assert (c.loc[c["direction"] == -1, "reason"] == "short_not_allowed").all()
    assert len(res.trades) > 0 and (res.trades["direction"] == 1).all()
    assert res.funding_known_from == {}
    assert (res.trades["funding"] == 0).all()


def test_random_ranker_deterministic():
    panel = _multi_panel(2_000, seed=3)
    keys = {"naive_mean_reversion": list(panel)}
    runs = [run_backtest(panel, {}, [NaiveMeanReversion({"span": 48})], keys, RandomRanker(7), "perp",
                         SETTINGS, START, END, instruments=insts(panel)) for _ in range(2)]
    assert len(runs[0].trades) > 0
    pd.testing.assert_frame_equal(runs[0].trades, runs[1].trades)


# --- metrics ------------------------------------------------------------------------------------


def test_metrics_summary():
    panel = _multi_panel(6_000, seed=9)
    res = bt(panel, [NaiveMeanReversion({"span": 48})], {"naive_mean_reversion": list(panel)})
    s = summary(res)
    assert list(s)[:3] == ["n_trades", "expectancy_r", "expectancy_bps"]
    tr = res.trades
    assert s["n_trades"] == len(tr)
    assert s["expectancy_r"] == pytest.approx(tr["r_multiple"].mean())
    assert s["pnl_net"] == pytest.approx(tr["pnl_net"].sum())
    assert s["pnl_net"] == pytest.approx(s["pnl_gross"] - s["fees"] - s["spread_slip"] + s["funding"])
    assert 0 <= s["max_dd"] < 1 and 0 <= s["exposure"] <= 1 and 0 <= s["win_rate"] <= 1
    assert math.isfinite(s["corr_btc"]) and -1 <= s["corr_btc"] <= 1
    dr = daily_returns(res.equity)
    assert len(dr) == len(res.equity.resample("1D").last())
    assert s["worst_day"] == pytest.approx(dr.min())
    assert s["sharpe_daily"] == pytest.approx(dr.mean() / dr.std(ddof=1) * math.sqrt(365))
    no_btc = bt({"SOL": panel["SOL"]}, [NaiveMeanReversion({"span": 48})], {"naive_mean_reversion": ["SOL"]})
    assert math.isnan(summary(no_btc)["corr_btc"])


def test_performance_5_assets_20k_bars():
    names = ["BTC", "ETH", "SOL", "XRP", "ADA"]
    kinds = ["random_walk", "ou", "trend", "random_walk", "ou"]
    panel = {a: synthetic_bars(20_000, k, seed=i, sigma=0.003)
             for i, (a, k) in enumerate(zip(names, kinds, strict=True))}
    strategies = [NaiveMeanReversion({"span": 48}), NaiveMomentum({"fast": 24, "slow": 96, "atr_n": 24})]
    keys = {"naive_mean_reversion": list(panel), "naive_momentum": list(panel)}
    settings = _with_limits(max_drawdown=0.99, daily_loss_limit=0.9)
    t0 = time.perf_counter()
    res = run_backtest(panel, {}, strategies, keys, AcceptAllRanker(), "perp", settings, START, END,
                       instruments=insts(panel))
    assert time.perf_counter() - t0 < 60
    assert len(res.trades) > 500
