"""Protocol §3.1 historical snapshot builder and the point-in-time funding view (synthetic data)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from luna.carry.funding import FundingLedger, FundingStore
from luna.carry.history import (
    FALLBACK_FULL,
    FundingPIT,
    HistoricalData,
    SnapshotBuilder,
    lookback_p90,
)

T = pd.Timestamp("2026-01-05T00:00Z")
M15 = pd.Timedelta("15min")


def _bars(lo, n, close=100.0, vol=1000.0, filled=False):
    idx = pd.date_range(lo, periods=n, freq="15min", tz="UTC")
    c = np.full(n, close, dtype=float) if np.isscalar(close) else np.asarray(close, float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": vol, "filled": filled}, index=idx)


def _spreads(lo, hi, v=4e-4):
    idx = pd.date_range(lo, hi, freq="5min", inclusive="left", tz="UTC")
    return pd.DataFrame({"spread_full": v}, index=idx)


def _store(asset="AAA", lo="2025-12-01", hi="2026-01-06", rel=1e-5):
    s = FundingStore()
    rows = [{"timestamp": t.isoformat(), "fundingRate": rel * 100, "relativeFundingRate": rel}
            for t in pd.date_range(lo, hi, freq="1h", tz="UTC", inclusive="left")]
    s.merge_rows(asset, rows)
    return s


def _data(**kw):
    lo = T - pd.Timedelta("2D")
    mark = {"AAA": _bars(lo, 4 * 24 * 3, close=np.arange(288) + 100.0)}
    perp = {"AAA": _bars(lo, 4 * 24 * 3, close=np.arange(288) + 99.0, vol=10.0)}
    spreads = {"AAA": _spreads(lo, T + pd.Timedelta("1D"))}
    d = dict(mark=mark, perp=perp, spreads=spreads, store=_store())
    d.update(kw)
    return HistoricalData(**d)


# --- point-in-time funding -----------------------------------------------------------------------


def test_pit_view_hides_periods_that_have_not_ended():
    s = _store(hi="2026-01-05T03:00")
    v = FundingPIT(s, T)
    assert v.newest_end("AAA") == T  # the period starting 23:00 ended at t
    assert s.newest_end("AAA") == T + pd.Timedelta("3h")  # the raw store knows the future
    assert v.rate("AAA", T - pd.Timedelta("1h")) is not None
    assert v.rate("AAA", T) is None
    assert v.trailing("AAA", T, 24) == s.trailing("AAA", T, 24)
    with pytest.raises(ValueError):
        v.trailing("AAA", T + pd.Timedelta("1h"), 24)


def test_pit_newest_end_drives_funding_stale_in_an_api_gap():
    s = _store(hi="2026-01-04T18:00")  # no rates for the last 6 hours before t
    s.merge_rows("AAA", [{"timestamp": "2026-01-05T02:00Z", "fundingRate": 1e-3, "relativeFundingRate": 1e-5}])
    assert FundingPIT(s, T).newest_end("AAA") == pd.Timestamp("2026-01-04T18:00Z")


def test_settlement_through_the_view_reads_only_ended_periods():
    s = _store()
    led = FundingLedger()
    led.on_fill("AAA", T - pd.Timedelta("2h"), -1.0, "p")
    out = led.settle(T, FundingPIT(s, T), pd.Timedelta("3h"))
    assert [x.period_start for x in out] == [T - pd.Timedelta("2h"), T - pd.Timedelta("1h")]


# --- snapshot ------------------------------------------------------------------------------------


def test_snapshot_mid_is_the_mark_bar_closed_at_t_and_spread_the_decision_window():
    b = SnapshotBuilder(_data())
    snap, info = b.snapshot(T)
    q = snap.quotes["AAA"]
    expect_mid = _data().mark["AAA"].at[T - M15, "close"]
    assert snap.ts == T and q.mid == pytest.approx(expect_mid) and q.mark == expect_mid
    assert q.spread_full == pytest.approx(4e-4)
    assert info["AAA"].dec_fallback is False and info["AAA"].dec_n == 12 and info["AAA"].mid_source == "mark"
    assert math.isinf(q.top_book_usd) and math.isinf(snap.depth_usd("AAA", 0.0025, 600, T))


def test_snapshot_never_uses_bars_that_close_after_t():
    d = _data()
    base = SnapshotBuilder(d).snapshot(T)[0].quotes["AAA"]
    for df in (d.mark["AAA"], d.perp["AAA"]):
        df.loc[df.index >= T, ["close", "volume"]] = 1e9
    after = SnapshotBuilder(d).snapshot(T)[0].quotes["AAA"]
    assert after == base


def test_volume_is_the_last_96_closed_trade_bars_in_usd():
    d = _data()
    q = SnapshotBuilder(d).snapshot(T)[0].quotes["AAA"]
    w = d.perp["AAA"][(d.perp["AAA"].index >= T - pd.Timedelta("1D")) & (d.perp["AAA"].index < T)]
    assert len(w) == 96 and q.volume_usd == pytest.approx((w["volume"] * w["close"]).sum())


def test_missing_mark_falls_back_to_the_trade_close_and_is_flagged():
    d = _data()
    d.mark["AAA"] = d.mark["AAA"].drop(T - M15)
    snap, info = SnapshotBuilder(d).snapshot(T)
    assert snap.quotes["AAA"].mid == pytest.approx(d.perp["AAA"].at[T - M15, "close"])
    assert info["AAA"].mid_source == "trade"


def test_no_mark_and_no_real_trade_bar_means_no_quote():
    d = _data()
    d.mark["AAA"] = d.mark["AAA"].drop(T - M15)
    d.perp["AAA"].loc[T - M15, "filled"] = True
    snap, info = SnapshotBuilder(d).snapshot(T)
    assert "AAA" not in snap.quotes and info["AAA"].mid_source is None


def test_decision_fallback_uses_10_bps_and_is_flagged():
    d = _data(spreads={"AAA": _spreads(T, T + pd.Timedelta("1h"))})  # nothing before t
    snap, info = SnapshotBuilder(d).snapshot(T)
    assert snap.quotes["AAA"].spread_full == pytest.approx(FALLBACK_FULL)
    assert info["AAA"].dec_fallback is True and info["AAA"].dec_n == 0


# --- fill quote ----------------------------------------------------------------------------------


def test_fill_quote_is_the_t15_mid_and_the_non_overlapping_fill_window():
    d = _data()
    f = SnapshotBuilder(d).fill_quote("AAA", T)
    assert f.ts == T + M15
    assert f.mid == pytest.approx(d.mark["AAA"].at[T, "close"])  # = the mid of the snapshot at t + 15m
    assert f.mid == SnapshotBuilder(d).snapshot(T + M15)[0].quotes["AAA"].mid
    assert f.half == pytest.approx(2e-4) and f.fallback is False and f.n_valid == 3


def test_fill_quote_none_without_a_t15_price():
    d = _data()
    d.mark["AAA"] = d.mark["AAA"].drop(T)
    d.perp["AAA"].loc[T, "filled"] = True
    assert SnapshotBuilder(d).fill_quote("AAA", T) is None


# --- sensitivities -------------------------------------------------------------------------------


def test_s_pit10_sees_one_snapshot_less():
    d = _data()
    _, info = SnapshotBuilder(d, known_after=pd.Timedelta("10min")).snapshot(T)
    assert info["AAA"].dec_n == 11
    assert SnapshotBuilder(d, known_after=pd.Timedelta("10min")).fill_quote("AAA", T).n_valid == 2


def test_s_spread_fix_prices_gate_and_fill_at_10_bps():
    b = SnapshotBuilder(_data(), spread_mode="fixed")
    assert b.snapshot(T)[0].quotes["AAA"].spread_full == pytest.approx(FALLBACK_FULL)
    assert b.fill_quote("AAA", T).half == pytest.approx(FALLBACK_FULL / 2)


def test_s_fb_cons_replaces_only_fallback_values():
    d = _data(spreads={"AAA": _spreads(T, T + pd.Timedelta("1h"))})
    b = SnapshotBuilder(d, fallback_full={"AAA": 0.003})
    snap, info = b.snapshot(T)
    assert info["AAA"].dec_fallback and snap.quotes["AAA"].spread_full == pytest.approx(0.003)
    assert b.fill_quote("AAA", T).half == pytest.approx(2e-4)  # fill window has data: unchanged


def test_lookback_p90_reads_only_the_lookback_window_and_valid_values():
    lo, hi = pd.Timestamp("2025-10-06T00:00Z"), pd.Timestamp("2025-11-10T00:00Z")
    idx = pd.date_range(lo, hi + pd.Timedelta("2D"), freq="5min", inclusive="left")
    v = pd.Series(np.linspace(1e-4, 2e-4, len(idx)), index=idx)
    v[idx >= hi] = 0.04  # after the lookback: must not matter
    v.iloc[:5] = [np.nan, 0.0, -1.0, 0.06, np.inf]  # invalid: ignored
    p = lookback_p90({"AAA": pd.DataFrame({"spread_full": v})})
    inside = v[(idx >= lo) & (idx < hi)].iloc[5:]
    assert p["AAA"] == pytest.approx(float(np.quantile(inside, 0.9)))
