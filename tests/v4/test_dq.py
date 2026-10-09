"""Frozen forward data-quality rules Q1–Q9 (spec §7.2) and their validity units."""

from __future__ import annotations

import pandas as pd
import pytest

from luna.v4 import dq

DAY = pd.Timestamp("2026-11-12T00:00:00Z")
NS = 1_000_000_000
MS = 1_000_000


def _env(recv_ns, req_ns=None, server_ns=None, ok=True):
    payload = {} if server_ns is None else {"serverTime": pd.Timestamp(server_ns, tz="UTC").isoformat()}
    req = recv_ns if req_ns is None else req_ns
    return {
        "ok": ok,
        "_recv_ns": recv_ns,
        "req_ts": pd.Timestamp(req, tz="UTC").isoformat(),
        "payload": payload,
    }


T = DAY.value + 60 * NS


def test_thresholds_are_the_frozen_numbers():
    assert (dq.PAIR_SKEW_MS, dq.FRESH_MS, dq.CLOCK_S) == (2000, 5000, 5)
    assert (dq.DAY_MIN_PAIRS, dq.MAX_CONSECUTIVE_INVALID) == (1368, 15)
    assert dq.FUNDING_SEEN_WITHIN == pd.Timedelta(hours=3)
    assert dq.EXEC_WINDOW == pd.Timedelta(minutes=15) and dq.DEPTH_MAX_AGE == pd.Timedelta(minutes=10)
    assert dq.SPOT_CLOCK_WINDOW == pd.Timedelta(minutes=5)


def test_q1_pair_skew_at_most_2000ms():
    fut = _env(T, server_ns=T)
    assert dq.pair_ok(fut, _env(T + 2000 * MS))
    assert not dq.pair_ok(fut, _env(T + 2001 * MS))
    assert not dq.pair_ok(fut, _env(T, ok=False))


def test_q2_request_freshness_at_most_5000ms_on_both_requests():
    assert dq.pair_ok(_env(T, req_ns=T - 5000 * MS, server_ns=T), _env(T))
    assert not dq.pair_ok(_env(T, req_ns=T - 5001 * MS, server_ns=T), _env(T))
    assert not dq.pair_ok(_env(T, server_ns=T), _env(T, req_ns=T - 6000 * MS))


def test_q3_futures_clock_per_snapshot_and_spot_clock_windows():
    assert dq.pair_ok(_env(T, server_ns=T - 5 * NS), _env(T))
    assert not dq.pair_ok(_env(T, server_ns=T - 5 * NS - 1), _env(T))
    assert not dq.pair_ok(_env(T), _env(T))  # no serverTime: cannot be verified -> invalid
    checks = [(T - 60 * NS, T - 60 * NS - 6 * NS), (T + 400 * NS, T + 400 * NS)]  # (recv, server unixtime)
    bad = dq.spot_clock_bad_windows(checks)
    assert bad == [(T - 60 * NS, T - 60 * NS + 300 * NS)]
    assert dq.in_windows(T, bad) and not dq.in_windows(T + 300 * NS, bad)
    assert not dq.pair_ok(_env(T, server_ns=T), _env(T), spot_clock_bad=bad)


def test_q4_asset_quote_validity():
    good = {"bid": 1.0, "ask": 1.1, "suspended": False}
    spot = {"bid": 1.0, "ask": 1.01}
    assert dq.asset_ok(good, spot)
    assert not dq.asset_ok({**good, "bid": 1.1}, spot)  # bid < ask strictly
    assert not dq.asset_ok({**good, "bid": 0.0}, spot)
    assert not dq.asset_ok({**good, "suspended": True}, spot)
    assert not dq.asset_ok(None, spot) and not dq.asset_ok(good, None)
    assert not dq.asset_ok(good, {"bid": None, "ask": 1.0})


def test_q5_q6_daily_coverage_and_consecutive_invalid():
    full = set(range(1440))
    assert dq.day_asset_checks(full) == {"valid_pairs": 1440, "longest_invalid": 0, "Q5": True, "Q6": True}
    gap16 = full - set(range(100, 116))
    assert dq.day_asset_checks(gap16)["Q6"] is False and dq.day_asset_checks(gap16)["Q5"] is True
    gap15 = full - set(range(100, 115))
    assert dq.day_asset_checks(gap15)["Q6"] is True
    sparse = {m for m in range(1440) if m % 20 != 0}  # 72 missing -> 1368 valid
    assert dq.day_asset_checks(sparse)["Q5"] is True
    assert dq.day_asset_checks(sparse - {1})["Q5"] is False
    assert dq.day_asset_checks(set(range(1, 1440)))["longest_invalid"] == 1
    assert dq.day_asset_checks(set(range(0, 1430)))["longest_invalid"] == 10  # trailing gap counts


def test_q7_every_period_seen_within_3_hours():
    periods = [DAY + pd.Timedelta(hours=h) for h in range(24)]
    seen = {p.value: (p + pd.Timedelta("1h5min")).value for p in periods}
    assert dq.funding_day_ok(seen, DAY)
    late = dict(seen)
    late[periods[5].value] = (periods[5] + pd.Timedelta("3h") + pd.Timedelta("1s")).value
    assert not dq.funding_day_ok(late, DAY)
    missing = dict(seen)
    del missing[periods[23].value]
    assert not dq.funding_day_ok(missing, DAY)


def test_config_day_validity_unit():
    ok = {(DAY, "A"): True, (DAY, "B"): False, (DAY, "C"): True}
    universe = ("A", "B", "C")
    assert dq.config_day_valid(DAY, eligible={"A"}, held={"C"}, asset_ok=ok, universe=universe)
    assert not dq.config_day_valid(DAY, eligible={"A"}, held={"B"}, asset_ok=ok, universe=universe)
    # empty set -> the whole universe must pass
    assert not dq.config_day_valid(DAY, eligible=set(), held=set(), asset_ok=ok, universe=universe)
    assert dq.config_day_valid(DAY, eligible=set(), held=set(), asset_ok=ok, universe=("A", "C"))


@pytest.mark.parametrize("bid,ask,ok", [(1, 2, True), (2, 2, False), (-1, 2, False), (None, 2, False)])
def test_quote_ok(bid, ask, ok):
    assert dq.quote_ok(bid, ask) is ok
