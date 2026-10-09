from __future__ import annotations

import pandas as pd
import pytest

from luna.carry.funding import FundingStore
from luna.carry.market import parse_tickers
from luna.carry.signals import compute_signals
from tests.carry.helpers import T0, carry_config, flat_history, funding_envelope, ticker, tickers_envelope

FEE = 0.0005


def _setup(
    rates: dict[str, float],
    rows: list[dict] | None = None,
    now: pd.Timestamp = T0,
    snap_ts: pd.Timestamp = T0,
    **cfg_over,
):
    cfg = carry_config(universe=sorted(rates), **cfg_over)
    store = FundingStore()
    for a, r in rates.items():
        store.merge_envelope(funding_envelope(a, flat_history(r, now)))
    snap = parse_tickers(tickers_envelope(snap_ts, rows if rows is not None else [ticker(a) for a in rates]))
    return compute_signals(snap, store, now, cfg, leg_notional=66.25, fee_rate=FEE), cfg


def test_fl30_rank_and_percentile_among_eligible() -> None:
    sig, _ = _setup({"A": 3e-5, "B": 1e-5, "C": -2e-5, "D": 0.0})
    assert sig["A"].fl == pytest.approx(3e-5) and sig["A"].fl_n == 720
    assert [a for a in sorted(sig, key=lambda a: sig[a].rank)] == ["A", "B", "D", "C"]  # 1 = highest
    assert sig["A"].pct == pytest.approx(1.0) and sig["C"].pct == pytest.approx(0.0)
    assert all(s.eligible for s in sig.values())


def test_side_cost_is_fee_plus_live_half_spread_plus_slippage() -> None:
    rows = [ticker(a, 100.0, spread_bps=4.0) for a in "ABCD"]
    sig, cfg = _setup({a: 1e-5 for a in "ABCD"}, rows)
    assert sig["A"].side_cost == pytest.approx(FEE + 2e-4 + cfg.slippage_bps * 1e-4)


def test_stale_snapshot_makes_every_asset_ineligible() -> None:
    sig, _ = _setup({"A": 1e-5, "B": 0.0, "C": 0.0, "D": 0.0}, now=T0 + pd.Timedelta(seconds=181), snap_ts=T0)
    assert not sig["A"].eligible and "stale_quote" in sig["A"].reasons


def test_quote_gates() -> None:
    rows = [
        ticker("A", spread_bps=30.0),
        ticker("B", volume_usd=1e5),
        ticker("C", size=0.1),
        ticker("D", suspended=True),
        ticker("E"),
    ]
    sig, _ = _setup({a: 1e-5 for a in "ABCDEF"}, rows)
    assert "wide_spread" in sig["A"].reasons
    assert "low_volume" in sig["B"].reasons
    assert "thin_book" in sig["C"].reasons  # top of book 0.1 x ~100 USD < one 66.25 USD leg
    assert "bad_quote" in sig["D"].reasons
    assert sig["E"].eligible
    assert "no_quote" in sig["F"].reasons  # no ticker row at all
    assert sig["E"].rank == 1 and sig["A"].rank is None  # ranks only among eligible


def test_funding_coverage_and_staleness_gates() -> None:
    cfg = carry_config(universe=["A", "B", "C", "D"])
    store = FundingStore()
    store.merge_envelope(funding_envelope("A", flat_history(1e-5, T0)))
    store.merge_envelope(funding_envelope("B", flat_history(1e-5, T0, hours=500)))  # 69% coverage
    store.merge_envelope(funding_envelope("C", flat_history(1e-5, T0 - pd.Timedelta(hours=5))))
    snap = parse_tickers(tickers_envelope(T0, [ticker(a) for a in "ABCD"]))
    sig = compute_signals(snap, store, T0, cfg, leg_notional=66.25, fee_rate=FEE)
    assert sig["A"].eligible
    assert "funding_coverage" in sig["B"].reasons
    assert "funding_stale" in sig["C"].reasons  # newest period ended 5h ago > 3h


def test_monitoring_signals_present() -> None:
    sig, _ = _setup({"A": 2e-5, "B": -1e-5, "C": 0.0, "D": 0.0})
    a = sig["A"]
    assert a.fl_short == pytest.approx(2e-5)  # FL7
    assert a.current_rel == pytest.approx(1e-5)  # ticker relativeFundingRate (helper default)
    assert a.persistence == pytest.approx(1.0)  # every recent period has the sign of FL30
    assert a.fl_annual == pytest.approx(2e-5 * 8760)


def test_depth_within_the_order_band_beats_a_thin_top_of_book() -> None:
    from dataclasses import replace

    cfg = carry_config(universe=["A", "B", "C", "D"])
    store = FundingStore()
    for a in "ABCD":
        store.merge_envelope(funding_envelope(a, flat_history(1e-5, T0)))
    rows = [ticker(a, 100.0, 2.0, size=0.05) for a in "ABCD"]  # top of book only ~5 USD
    snap = parse_tickers(tickers_envelope(T0, rows))
    thin = compute_signals(snap, store, T0, cfg, leg_notional=66.25, fee_rate=FEE)
    assert "thin_book" in thin["A"].reasons
    deep = ([(99.99, 0.05), (99.9, 1.0), (99.0, 50.0)], [(100.01, 0.05), (100.1, 1.0), (101.0, 50.0)])
    books = {a: (T0, deep) for a in "ABCD"}
    sig = compute_signals(replace(snap, books=books), store, T0, cfg, leg_notional=66.25, fee_rate=FEE)
    # within +/- 25 bps: 99.99 x 0.05 + 99.9 x 1.0 ~ 104.9 USD per side (99.0 / 101.0 are outside)
    assert sig["A"].eligible and sig["A"].depth_usd == pytest.approx(99.99 * 0.05 + 99.9, rel=1e-6)
    stale = {a: (T0 - pd.Timedelta(minutes=11), deep) for a in "ABCD"}
    old = compute_signals(replace(snap, books=stale), store, T0, cfg, leg_notional=66.25, fee_rate=FEE)
    assert "thin_book" in old["A"].reasons  # a stale book falls back to the top of book
