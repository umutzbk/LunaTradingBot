"""Investigation pins: the +300R naive-momentum XRP trade (2024-11-05 -> 2024-12-05).

See docs/results/xrp-outlier-investigation.md. Verdict: genuine. A ~1% stop (3*ATR(96)/close on
15-minute bars) plus a 30-day hold through a 4.53x move gives ~300R by construction:
R ~= (P_exit/P_entry - 1) / (stop_distance + round_trip).

Synthetic tests run everywhere. The real-data test needs the local bar store (var/bars) and the
instrument cache (var/instruments/perp.json); it is skipped if they are missing. No network.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from luna.config import load_settings
from luna.data.store import BarStore, fill_missing
from luna.engine.backtest import default_instruments, run_backtest
from luna.execution.costs import CostModel
from luna.features.core import atr, ema
from luna.ranking.ranker import AcceptAllRanker
from luna.strategy.naive import NaiveMomentum

SETTINGS = load_settings()
BAR = pd.Timedelta("15min")
START = pd.Timestamp("2000-01-01", tz="UTC")
END = pd.Timestamp("2100-01-01", tz="UTC")
REPO = Path(__file__).resolve().parents[2]


def _settings(**perp_update):
    lim = SETTINGS.accounts["perp"].model_copy(update=perp_update)
    return SETTINGS.model_copy(update={"accounts": {**SETTINGS.accounts, "perp": lim}})


def _frictions(asset: str) -> tuple[float, float, float]:
    """(taker fee, half_spread + slippage, round trip) for perp, from the default settings."""
    c = CostModel(SETTINGS.costs["perp"])
    return c.fee_rate("taker"), c.half_spread(asset) + c.slippage, c.round_trip_frac(asset, "taker")


def _hand_trade(bars: pd.DataFrame, entry_ts, exit_ts, gross: float, qty_step: float, asset: str):
    """Recompute a single-leg long perp trade from bars with the engine's documented rules:
    decided at the close of bar t (mid = close[t]), IOC filled at open[t+1] +/- (half spread +
    slippage), taker fee on each fill notional, qty rounded down to the step."""
    fee, fr, _ = _frictions(asset)
    dec_in, dec_out = entry_ts - BAR, exit_ts - BAR
    mid_in, mid_out = float(bars.close[dec_in]), float(bars.close[dec_out])
    qty = math.floor(gross / mid_in / qty_step + 1e-9) * qty_step
    fill_in = float(bars.open[entry_ts]) * (1 + fr)
    fill_out = float(bars.open[exit_ts]) * (1 - fr)
    fees = fee * qty * (fill_in + fill_out)
    return {
        "qty": qty, "fill_in": fill_in, "fill_out": fill_out,
        "gross_notional": qty * fill_in,
        "pnl_gross": qty * (mid_out - mid_in),
        "spread_slip": qty * (fill_in - mid_in) + qty * (mid_out - fill_out),
        "fees": fees,
        "pnl_net": qty * (fill_out - fill_in) - fees,
    }


# --- synthetic reproduction -------------------------------------------------------------------------

def _rally_bars(n_down=1_200, n_up=900, step=0.003, wiggle=0.0028, hl=0.0002) -> pd.DataFrame:
    """Gentle downtrend with alternating +/- wiggles (sets ATR ~ wiggle), then a steady rally of
    `step` log-return per bar. Deterministic, no gaps, OHLC-consistent."""
    down = -0.0002 + wiggle * np.where(np.arange(n_down) % 2 == 0, 1.0, -1.0)
    up = np.full(n_up, step)
    close = 0.5 * np.exp(np.cumsum(np.concatenate([down, up])))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + hl)
    low = np.minimum(open_, close) * (1 - hl)
    idx = pd.date_range("2024-01-01", periods=len(close), freq="15min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": 1.0, "trades": 1, "filled": False}, index=idx)


MAX_HOLD = 500
PARAMS = {"fast": 24, "slow": 96, "atr_n": 24, "max_hold": MAX_HOLD}


@pytest.fixture(scope="module")
def synthetic():
    bars = _rally_bars()
    res = run_backtest({"XRP": bars}, {}, [NaiveMomentum(PARAMS)], {"naive_momentum": ["XRP"]},
                       AcceptAllRanker(), "perp", SETTINGS, START, END,
                       instruments=default_instruments(["XRP"], "perp", SETTINGS))
    return bars, res


def test_synthetic_big_move_small_stop_gives_hundreds_of_r(synthetic):
    bars, res = synthetic
    t = res.trades
    assert len(t) == 1
    tr = t.iloc[0]
    assert tr.direction == 1 and tr.exit_reason == "time"
    assert pd.Timestamp(tr.exit_ts) - pd.Timestamp(tr.entry_ts) == MAX_HOLD * BAR
    cand = res.candidates.iloc[0]
    dist = float(cand.stop_distance)
    assert 0.008 < dist < 0.014  # ~1% stop, like the XRP trade (0.975%)
    move = bars.close[pd.Timestamp(tr.exit_ts) - BAR] / bars.close[pd.Timestamp(tr.entry_ts) - BAR]
    assert 4.0 < move < 5.0
    _, _, rt = _frictions("XRP")
    r_closed_form = (move - 1) / (dist + rt)  # price R before the (tiny) costs
    assert 250 < tr.r_multiple < 330
    assert tr.r_multiple == pytest.approx(r_closed_form, rel=0.01)


def test_synthetic_reconciles_exactly_with_hand_pnl(synthetic):
    bars, res = synthetic
    tr = res.trades.iloc[0]
    entry_ts, exit_ts = pd.Timestamp(tr.entry_ts), pd.Timestamp(tr.exit_ts)
    dist = float(res.candidates.iloc[0].stop_distance)
    _, _, rt = _frictions("XRP")
    eq0 = SETTINGS.accounts["perp"].capital_usd
    risk = SETTINGS.accounts["perp"].risk_per_trade * eq0
    gross_planned = risk / (dist + rt)
    assert gross_planned < SETTINGS.accounts["perp"].max_gross_leverage * eq0  # not clipped
    assert tr.risk_amount == pytest.approx(risk, rel=1e-12)  # R denominator = planned risk
    h = _hand_trade(bars, entry_ts, exit_ts, gross_planned, 1e-8, "XRP")
    for k in ("gross_notional", "pnl_gross", "fees", "spread_slip", "pnl_net"):
        assert tr[k] == pytest.approx(h[k], rel=1e-9, abs=1e-9), k
    assert tr.pnl_net == pytest.approx(tr.pnl_gross - tr.fees - tr.spread_slip + tr.funding, abs=1e-9)
    assert tr.r_multiple == pytest.approx(tr.pnl_net / tr.risk_amount, rel=1e-12)
    # account equity moved by exactly the trade's net P&L (no funding data -> unknown, no cash)
    assert res.equity.iloc[-1] - res.initial_equity == pytest.approx(tr.pnl_net, abs=1e-9)


def test_risk_amount_shrinks_when_gross_is_clipped():
    """R denominator caveat: when the gross-leverage or per-leg cap clips gross, risk_amount =
    gross*(dist+rt) is smaller than risk_per_trade*equity, which inflates |R|. Not the case for the
    XRP trade (gross 170 vs caps 534 / 267)."""
    bars = _rally_bars()
    tiny = NaiveMomentum({**PARAMS, "stop_atr": 0.2})  # ~0.07% stop -> gross would exceed the caps
    res = run_backtest({"XRP": bars}, {}, [tiny], {"naive_momentum": ["XRP"]}, AcceptAllRanker(),
                       "perp", SETTINGS, START, END,
                       instruments=default_instruments(["XRP"], "perp", SETTINGS))
    tr = res.trades.iloc[0]
    lim = SETTINGS.accounts["perp"]
    dist = float(res.candidates.iloc[0].stop_distance)
    _, _, rt = _frictions("XRP")
    assert tr.risk_amount < lim.risk_per_trade * lim.capital_usd
    cap = min(lim.max_gross_leverage, lim.max_leg_notional_frac) * lim.capital_usd
    assert lim.risk_per_trade * lim.capital_usd / (dist + rt) > cap
    assert tr.risk_amount == pytest.approx(cap * (dist + rt), rel=1e-9)
    assert tr.r_multiple == pytest.approx(tr.pnl_net / tr.risk_amount, rel=1e-12)


# --- the real trade on the stored Kraken perp data ----------------------------------------------------

VAR = SETTINGS.var_dir if Path(SETTINGS.var_dir).is_absolute() else REPO / SETTINGS.var_dir
UNIVERSE = ["BTC", "ETH", "SOL", "XRP"]
DATA_PRESENT = all((VAR / "bars" / "kraken_futures" / f"{a}.parquet").exists() for a in UNIVERSE) and (
    VAR / "instruments" / "perp.json").exists()
ENTRY = pd.Timestamp("2024-11-05 21:30", tz="UTC")
EXIT = pd.Timestamp("2024-12-05 21:30", tz="UTC")


@pytest.fixture(scope="module")
def real():
    from luna.instruments import load_instruments

    s = _settings(max_drawdown=1.0).model_copy(update={"var_dir": VAR})
    st = BarStore(VAR)
    start, end = pd.Timestamp("2024-09-01", tz="UTC"), pd.Timestamp("2026-07-01", tz="UTC")
    panel = {a: fill_missing(st.read("kraken_futures", a, start - pd.Timedelta(days=10), end))
             for a in UNIVERSE}
    funding = {a: st.read_funding(a) for a in UNIVERSE}
    inst = load_instruments(s, "perp")
    res = run_backtest(panel, funding, [NaiveMomentum()], {"naive_momentum": UNIVERSE},
                       AcceptAllRanker(), "perp", s, start, end, funding_mode="known_only",
                       instruments={a: inst[a] for a in UNIVERSE})
    t = res.trades
    tr = t[(t.key == "XRP") & (pd.to_datetime(t.entry_ts) == ENTRY)]
    assert len(tr) == 1
    return panel["XRP"], inst["XRP"], res, tr.iloc[0], s


@pytest.mark.skipif(not DATA_PRESENT, reason="local Kraken perp bars / instrument cache not present")
def test_real_xrp_trade_reconciles_by_hand(real):
    bars, inst, res, tr, s = real
    lim = s.accounts["perp"]
    dec = ENTRY - BAR
    # sizing: stop distance = 3*ATR(96)/close at the decision close; risk = 0.75% of equity then
    dist = 3.0 * float(atr(bars, 96)[dec]) / float(bars.close[dec])
    _, _, rt = _frictions("XRP")
    eq_dec = float(res.equity[dec])
    risk = lim.risk_per_trade * eq_dec
    gross = risk / (dist + rt)
    assert gross < min(lim.max_gross_leverage * eq_dec, lim.max_leg_notional_frac * eq_dec)
    assert tr.risk_amount == pytest.approx(risk, rel=1e-9)
    h = _hand_trade(bars, ENTRY, EXIT, gross, inst.qty_step, "XRP")
    assert h["qty"] == 332
    for k in ("gross_notional", "pnl_gross", "fees", "spread_slip", "pnl_net"):
        assert tr[k] == pytest.approx(h[k], abs=1e-6), k
    assert tr.funding == 0.0 and tr.funding_unknown_bars == 720  # funding history starts 2025-10
    assert tr.r_multiple == pytest.approx(tr.pnl_net / tr.risk_amount, rel=1e-12)
    assert tr.r_multiple == pytest.approx(299.736, abs=1e-3)
    # +300R arithmetic: 4.53x move over a 0.975% + 0.2% risk denominator
    move = float(bars.close[EXIT - BAR]) / float(bars.close[dec])
    assert move == pytest.approx(4.5298, abs=1e-4)
    assert (move - 1) / (dist + rt) == pytest.approx(tr.r_multiple, rel=0.01)


@pytest.mark.skipif(not DATA_PRESENT, reason="local Kraken perp bars / instrument cache not present")
def test_real_xrp_window_is_clean_and_exit_is_time(real):
    bars, _, res, tr, _ = real
    w = bars.loc[ENTRY:EXIT]
    assert len(w) == 2881 and not w.filled.any()  # every bar real, no gaps
    assert (w.index.to_series().diff().dropna() == BAR).all()
    body_hi, body_lo = w[["open", "close"]].max(axis=1), w[["open", "close"]].min(axis=1)
    assert (w.high >= body_hi).all() and (w.low <= body_lo).all() and (w.low > 0).all()
    assert np.log(w.close).diff().abs().max() < 0.08  # largest 15m move 7.4% (2024-12-02)
    # stop never hit: it is checked on closes; level 0.50807, lowest close 0.51103
    dec = ENTRY - BAR
    stop_level = float(bars.close[dec]) * (1 - 3.0 * float(atr(bars, 96)[dec]) / float(bars.close[dec]))
    assert w.close.min() > stop_level
    # no signal exit: EMA(96) stayed above EMA(480) for the whole hold (first cross down 2024-12-09)
    c = bars.close
    assert ((ema(c, 96) - ema(c, 480)).loc[ENTRY:EXIT] > 0).all()
    # time exit after max_hold = 2880 bars (30 days), decided 21:15 and filled at the next open
    assert tr.exit_reason == "time" and EXIT - ENTRY == 2880 * BAR
    # no emergency stop on this position
    ev = res.risk_events
    assert not ((ev.kind == "emergency_stop") & (ev.position_id == tr.position_id)).any()
