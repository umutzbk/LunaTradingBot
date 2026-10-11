"""Protocol rev 3 §3.1/§3.2: point-in-time spreads from the real 5-minute Kraken snapshots.

A snapshot labelled T is known only from T + 5 min. Decision window [t - 1h, t) (<= 12 snapshots,
>= 6 valid), fill window [t, t + 15m) known at the fill t + 15m (<= 3 snapshots, >= 2 valid); else the
10 bps fallback, flagged. No forward-fill, no interpolation. Synthetic data only."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from luna.carry.history import FALLBACK_FULL, Spread5m
from luna.v2.data import HoldoutLeak

T = pd.Timestamp("2026-01-05T00:00Z")
M5 = pd.Timedelta("5min")


def _series(labels, values=None) -> pd.DataFrame:
    idx = pd.DatetimeIndex(labels)
    v = np.arange(1, len(idx) + 1) * 1e-4 if values is None else values
    return pd.DataFrame({"spread_full": v}, index=idx)


def _grid(lo, hi, values=None) -> pd.DataFrame:
    return _series(pd.date_range(lo, hi, freq="5min", inclusive="left"), values)


def test_decision_uses_exactly_the_12_snapshots_known_at_t():
    s = Spread5m(_grid(T - pd.Timedelta("2h"), T + pd.Timedelta("1h")))
    o = s.decision(T)
    labels = pd.date_range(T - pd.Timedelta("1h"), T - M5, freq="5min")  # t-60 .. t-5
    expect = s.frame.loc[labels, "spread_full"].mean()
    assert (o.n_valid, o.n_expected, o.fallback) == (12, 12, False)
    assert o.full == pytest.approx(expect) and o.half == pytest.approx(expect / 2)


def test_decision_never_sees_a_snapshot_not_yet_known():
    base = _grid(T - pd.Timedelta("2h"), T + pd.Timedelta("1h"))
    o = Spread5m(base).decision(T)
    late = base.copy()
    late.loc[late.index >= T, "spread_full"] = 0.04  # labels t, t+5, ...: known only at >= t + 5m
    late.loc[late.index < T - pd.Timedelta("1h"), "spread_full"] = 0.04  # outside the window
    assert Spread5m(late).decision(T) == o


def test_decision_threshold_is_6_of_12_valid():
    labels = pd.date_range(T - pd.Timedelta("1h"), T - M5, freq="5min")
    six = _series(labels, [1e-4] * 6 + [np.nan, 0.0, -1e-4, np.inf, 0.06, np.nan])
    o = Spread5m(six).decision(T)
    assert (o.n_valid, o.fallback) == (6, False) and o.full == pytest.approx(1e-4)
    five = _series(labels, [1e-4] * 5 + [np.nan] * 7)
    o = Spread5m(five).decision(T)
    assert (o.n_valid, o.fallback, o.full) == (5, True, FALLBACK_FULL)


def test_missing_snapshots_are_not_forward_filled():
    labels = pd.date_range(T - pd.Timedelta("1h"), T - M5, freq="5min")[::2]  # 6 rows present
    o = Spread5m(_series(labels, [1e-4, 2e-4, 3e-4, 4e-4, 5e-4, 6e-4])).decision(T)
    assert (o.n_valid, o.fallback) == (6, False) and o.full == pytest.approx(3.5e-4)


def test_fill_uses_the_3_snapshots_in_t_to_t15_known_at_the_fill():
    s = Spread5m(_grid(T - pd.Timedelta("1h"), T + pd.Timedelta("1h")))
    o = s.fill(T)
    expect = s.frame.loc[[T, T + M5, T + 2 * M5], "spread_full"].mean()
    assert (o.n_valid, o.n_expected, o.fallback) == (3, 3, False)
    assert o.full == pytest.approx(expect)


def test_fill_threshold_is_2_of_3_and_excludes_neighbours():
    vals = {T - M5: 0.04, T: 1e-4, T + M5: np.nan, T + 2 * M5: 3e-4, T + 3 * M5: 0.04}
    o = Spread5m(_series(list(vals), list(vals.values()))).fill(T)
    assert (o.n_valid, o.fallback) == (2, False) and o.full == pytest.approx(2e-4)
    vals[T + 2 * M5] = np.nan
    o = Spread5m(_series(list(vals), list(vals.values()))).fill(T)
    assert (o.n_valid, o.fallback, o.full) == (1, True, FALLBACK_FULL)


def test_decision_and_fill_windows_never_overlap():
    s = Spread5m(_grid(T - pd.Timedelta("1D"), T + pd.Timedelta("1D")))
    for t in pd.date_range(T - pd.Timedelta("6h"), T + pd.Timedelta("6h"), freq="15min"):
        d, f = set(s.decision_labels(t)), set(s.fill_labels(t))
        assert d and f and not d & f
        assert max(d) + M5 <= t and max(f) + M5 <= t + pd.Timedelta("15min")


def test_a_one_minute_or_off_grid_series_is_refused():
    with pytest.raises(ValueError):
        Spread5m(_series(pd.date_range(T, periods=10, freq="1min")))
    with pytest.raises(ValueError):
        Spread5m(_series([T, T + pd.Timedelta("5min30s")]))
    with pytest.raises(ValueError):
        Spread5m(_series([T + M5, T]))  # not increasing


def test_a_snapshot_at_or_after_the_cutoff_is_refused():
    with pytest.raises(HoldoutLeak):
        Spread5m(_series([pd.Timestamp("2026-06-30T23:55Z"), pd.Timestamp("2026-07-01T00:00Z")]))


def test_no_series_means_fallback():
    s = Spread5m(None)
    assert s.decision(T).fallback and s.fill(T).fallback
    assert s.decision(T).n_valid == 0 and s.decision(T).full == FALLBACK_FULL


def test_s_pit10_sensitivity_ignores_the_label_t_minus_5():
    """Red-team F1: under knowledge time T + 10m the label t - 5 is not yet known at t."""
    base = _grid(T - pd.Timedelta("2h"), T + pd.Timedelta("1h"))
    o = Spread5m(base, known_after=pd.Timedelta("10min")).decision(T)
    moved = base.copy()
    moved.loc[T - M5, "spread_full"] = 0.04
    assert Spread5m(moved, known_after=pd.Timedelta("10min")).decision(T) == o
    assert o.n_valid == 11 and o.n_expected == 11
    assert Spread5m(moved).decision(T) != Spread5m(base).decision(T)  # primary T+5m does see it


def test_known_after_shorter_than_5_minutes_is_refused():
    with pytest.raises(ValueError):
        Spread5m(None, known_after=pd.Timedelta("4min"))


def test_decision_time_must_be_tz_aware_and_on_the_15_minute_grid():
    """Red-team F5: a shifted t would silently change the windows."""
    s = Spread5m(_grid(T - pd.Timedelta("2h"), T + pd.Timedelta("1h")))
    for bad in (T + pd.Timedelta("1s"), T + M5):
        with pytest.raises(ValueError):
            s.decision(bad)
        with pytest.raises(ValueError):
            s.fill(bad)
    with pytest.raises(ValueError):
        s.decision(pd.Timestamp("2026-01-05T00:00"))


def test_primary_loader_requires_real_300s_provenance(tmp_path):
    """Red-team F6: a 900 s or 60 s file must not silently become the primary series."""
    import json

    from luna.carry.history import load_spread5m

    df = _grid(T - pd.Timedelta("1D"), T)
    df.to_parquet(tmp_path / "BTC.parquet")
    from luna.carry.frozen import sha256_file
    prov = {"requested_interval_s": 300, "observed_cadence_s": 300, "real_resolution_matches_interval": True,
            "sha256": sha256_file(tmp_path / "BTC.parquet")}
    (tmp_path / "BTC.provenance.json").write_text(json.dumps(prov), encoding="utf-8")
    assert load_spread5m(tmp_path, "BTC").decision(T).n_valid == 12
    for bad in ({"requested_interval_s": 900, "observed_cadence_s": 900}, {"observed_cadence_s": 60},
                {"real_resolution_matches_interval": False}, {"sha256": "0" * 64}):
        (tmp_path / "BTC.provenance.json").write_text(json.dumps(prov | bad), encoding="utf-8")
        with pytest.raises(ValueError):
            load_spread5m(tmp_path, "BTC")
