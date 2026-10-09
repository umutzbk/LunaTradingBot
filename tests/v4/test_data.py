"""V4 loaders (spec R1, R7, §7.2, §8): historical loaders never return a row of the sealed holdout and
raise HoldoutLeak when one reaches them; forward loaders read only collector data received after
collector_start_ts, drop funding periods that started before it, and split warmup / evidence."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from luna.data.store import BarStore
from luna.v2.data import HoldoutLeak
from luna.v4 import data as d
from luna.v4.constants import V4_DATA_END, V4_EVIDENCE_START_TS
from tests.v4.synth import COLLECTOR_START, bars, hourly_funding, write_env, write_meta

# --- historical --------------------------------------------------------------------------------


def test_hist_bars_are_cut_at_read_and_never_include_the_holdout(tmp_path: Path):
    store = BarStore(tmp_path)
    store.write("kraken_futures", "BTC", bars("2026-06-30T22:00Z", 20))  # runs into July
    df = d.load_hist_bars(tmp_path, "kraken_futures", "BTC")
    assert df.index.max() == V4_DATA_END - pd.Timedelta("15min")
    assert len(df) == 8


def test_guard_raises_on_a_bar_closing_after_the_cutoff():
    with pytest.raises(HoldoutLeak):
        d.guard_bars(bars("2026-06-30T23:45Z", 2), "x")
    d.guard_bars(bars("2026-06-30T23:45Z", 1), "x")  # closes exactly at the cut: fine


def test_guard_raises_on_funding_period_at_the_cutoff():
    with pytest.raises(HoldoutLeak):
        d.guard_series(hourly_funding("2026-06-30T23:00Z", 2), "x")
    d.guard_series(hourly_funding("2026-06-30T23:00Z", 1), "x")


def _write_hist(tmp_path: Path, kind: str, asset: str, df: pd.DataFrame) -> None:
    p = tmp_path / "history_v4" / kind
    p.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p / f"{asset}.parquet")


def test_history_v4_funding_loader_raises_on_a_leaked_row(tmp_path: Path):
    _write_hist(tmp_path, "funding", "ETH", hourly_funding("2026-06-30T22:00Z", 3))
    with pytest.raises(HoldoutLeak):
        d.load_hist_funding(tmp_path, "ETH")


def test_history_v4_loaders_return_clean_frames(tmp_path: Path):
    _write_hist(tmp_path, "funding", "ETH", hourly_funding("2026-06-30T20:00Z", 4))
    _write_hist(tmp_path, "mark", "ETH", bars("2026-06-30T23:00Z", 4)[["open", "high", "low", "close"]])
    sp = pd.DataFrame(
        {"spread_full": [0.001, 0.002]}, index=pd.date_range("2026-06-30T22:00Z", periods=2, freq="1h")
    )
    _write_hist(tmp_path, "spreads", "ETH", sp)
    assert list(d.load_hist_funding(tmp_path, "ETH").columns) == ["funding_abs", "funding_rel"]
    assert len(d.load_hist_mark(tmp_path, "ETH")) == 4
    assert d.load_hist_spreads(tmp_path, "ETH")["spread_full"].tolist() == [0.001, 0.002]
    assert d.load_hist_index(tmp_path, "ETH").empty  # missing file: empty frame, not an error


def test_load_historical_bundles_the_universe(tmp_path: Path):
    store = BarStore(tmp_path)
    store.write("kraken_futures", "BTC", bars("2026-06-29T00:00Z", 10))
    store.write("kraken_spot", "BTC", bars("2026-06-29T00:00Z", 10))
    _write_hist(tmp_path, "funding", "BTC", hourly_funding("2026-06-29T00:00Z", 5))
    h = d.load_historical(tmp_path, ["BTC", "ETH"])
    assert set(h.perp) == {"BTC", "ETH"} and len(h.perp["BTC"]) == 10 and h.perp["ETH"].empty
    assert len(h.funding["BTC"]) == 5


# --- forward -----------------------------------------------------------------------------------


def _funding_payload(stamps, abs_rate=1.0):
    return {
        "result": "success",
        "rates": [
            {"timestamp": t, "fundingRate": abs_rate, "relativeFundingRate": abs_rate / 1e5} for t in stamps
        ],
    }


def test_forward_funding_drops_periods_before_collector_start(tmp_path: Path):
    write_meta(tmp_path)
    stamps = [
        "2026-07-15T00:00:00.000Z",  # sealed holdout: must never be returned
        "2026-10-09T11:00:00.000Z",  # period started before collector_start
        "2026-10-09T12:00:00.000Z",
        "2026-10-09T13:00:00.000Z",
    ]
    write_env(
        tmp_path,
        "fut_funding_full",
        "2026-10-09T14:20:00Z",
        _funding_payload(stamps),
        params={"symbol": "PF_XBTUSD"},
    )
    write_env(
        tmp_path,
        "fut_funding",
        "2026-10-09T15:05:00Z",
        _funding_payload(["2026-10-09T13:00:00.000Z", "2026-10-09T14:00:00.000Z"]),
        params={"symbol": "PF_XBTUSD"},
    )
    out = d.load_forward_funding(tmp_path, ["BTC"], until=pd.Timestamp("2026-10-10T00:00Z"))
    idx = out["BTC"].index
    assert idx.min() == pd.Timestamp("2026-10-09T12:00Z")
    assert list(idx) == list(pd.date_range("2026-10-09T12:00Z", periods=3, freq="1h"))
    assert list(out["BTC"].columns) == ["funding_abs", "funding_rel"]


def test_forward_reader_ignores_envelopes_received_before_collector_start(tmp_path: Path):
    write_meta(tmp_path)
    write_env(
        tmp_path,
        "fut_funding",
        "2026-10-09T11:00:00Z",
        _funding_payload(["2026-10-09T13:00:00.000Z"]),
        params={"symbol": "PF_XBTUSD"},
    )
    out = d.load_forward_funding(tmp_path, ["BTC"], until=pd.Timestamp("2026-10-10T00:00Z"))
    assert out["BTC"].empty


def test_forward_reader_needs_collector_start(tmp_path: Path):
    with pytest.raises(ValueError):
        d.collector_start(tmp_path)


def test_evidence_split_at_evidence_start():
    idx = pd.DatetimeIndex([V4_EVIDENCE_START_TS - pd.Timedelta("1ns"), V4_EVIDENCE_START_TS])
    df = pd.DataFrame({"x": [1, 2]}, index=idx)
    warm, ev = d.split_evidence(df)
    assert warm["x"].tolist() == [1] and ev["x"].tolist() == [2]


# --- forward snapshots aggregated per day -------------------------------------------------------


def _fut(bid, ask, mark, vq=2e6, suspended=False, sym="PF_XBTUSD"):
    return {
        "result": "success",
        "serverTime": "x",
        "tickers": [
            {
                "symbol": sym,
                "bid": bid,
                "ask": ask,
                "markPrice": mark,
                "volumeQuote": vq,
                "suspended": suspended,
            }
        ],
    }


def _spot(bid, ask, v24=10.0, p24=100.0, key="XXBTZUSD"):
    return {
        "error": [],
        "result": {
            key: {
                "a": [str(ask), "1", "1.0"],
                "b": [str(bid), "1", "1.0"],
                "v": ["1", str(v24)],
                "p": ["1", str(p24)],
            }
        },
    }


def _pair(root, t, seq, fut, spot, skew_ms=100, server_lag_s=0.0):
    t = pd.Timestamp(t)
    fut = {**fut, "serverTime": (t - pd.Timedelta(seconds=server_lag_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    write_env(root, "fut_tickers", t, fut, pair_seq=seq)
    write_env(root, "spot_ticker", t + pd.Timedelta(milliseconds=skew_ms), spot, pair_seq=seq)


def test_forward_daily_exec_snapshot_is_first_skew_valid_after_midnight(tmp_path: Path):
    write_meta(tmp_path)
    day = pd.Timestamp("2026-10-12T00:00Z")
    _pair(tmp_path, day - pd.Timedelta("30s"), 1, _fut(99, 101, 100), _spot(98, 100))  # before t
    _pair(tmp_path, day + pd.Timedelta("1min"), 2, _fut(100, 102, 101), _spot(99, 101), skew_ms=2500)
    _pair(tmp_path, day + pd.Timedelta("2min"), 3, _fut(101, 103, 102), _spot(100, 102))  # first valid
    _pair(tmp_path, day + pd.Timedelta("3min"), 4, _fut(500, 502, 501), _spot(400, 402))
    fd = d.load_forward_daily(
        tmp_path, ["BTC"], since=day - pd.Timedelta("1D"), until=day + pd.Timedelta("1D")
    )
    row = fd.exec.loc[(day, "BTC")]
    assert row["perp_mid"] == 102 and row["spot_mid"] == 101
    assert row["perp_half_spread"] == pytest.approx(1 / 102)
    assert row["perp_vol_quote"] == 2e6 and row["spot_vol_usd"] == 1000.0
    assert fd.daily.loc[(day, "BTC"), "mark_max"] == 501
    assert fd.daily.loc[(day, "BTC"), "mark_min"] == 101
    assert fd.daily.loc[(day, "BTC"), "mark_last"] == 501
    # basis of day d: skew-valid pairs only (seq 2 excluded)
    assert fd.daily.loc[(day, "BTC"), "basis_n"] == 2
    assert fd.daily.loc[(day, "BTC"), "basis_median"] == pytest.approx((102 / 101 - 1 + 501 / 401 - 1) / 2)


def test_forward_daily_no_exec_when_no_valid_snapshot_within_15_minutes(tmp_path: Path):
    write_meta(tmp_path)
    day = pd.Timestamp("2026-10-12T00:00Z")
    _pair(tmp_path, day + pd.Timedelta("16min"), 1, _fut(100, 102, 101), _spot(99, 101))
    fd = d.load_forward_daily(tmp_path, ["BTC"], since=day, until=day + pd.Timedelta("1D"))
    assert (day, "BTC") not in fd.exec.index


def test_forward_funding_and_snapshots_stop_at_until(tmp_path: Path):
    write_meta(tmp_path)
    day = pd.Timestamp("2026-10-12T00:00Z")
    _pair(tmp_path, day + pd.Timedelta("1D 1min"), 1, _fut(100, 102, 101), _spot(99, 101))
    fd = d.load_forward_daily(tmp_path, ["BTC"], since=day, until=day + pd.Timedelta("1D"))
    assert fd.exec.empty


def test_collector_start_constant_matches_fixture():
    assert COLLECTOR_START == pd.Timestamp("2026-10-09T11:52:48Z")


def test_forward_listing_from_instruments_opening_date(tmp_path: Path):
    write_meta(tmp_path)
    write_env(
        tmp_path,
        "fut_instruments",
        "2026-10-09T12:00:00Z",
        {
            "instruments": [
                {"symbol": "PF_XBTUSD", "openingDate": "2022-03-21T00:00:00.000Z"},
                {"symbol": "PF_ETHUSD"},
            ]
        },
    )
    lst = d.load_forward_listing(tmp_path, ["BTC", "ETH", "SOL"], until=pd.Timestamp("2026-10-10T00:00Z"))
    assert lst["BTC"] == pd.Timestamp("2022-03-21T00:00Z")
    assert lst["ETH"] == pd.Timestamp("2026-10-09T12:00Z")  # no openingDate: first seen
    assert "SOL" not in lst


def test_forward_daily_applies_q2_q3_q4_and_reports_asset_quality(tmp_path: Path):
    write_meta(tmp_path)
    day = pd.Timestamp("2026-10-12T00:00Z")
    _pair(tmp_path, day + pd.Timedelta("1min"), 1, _fut(100, 102, 101), _spot(99, 101), server_lag_s=6)  # Q3
    _pair(tmp_path, day + pd.Timedelta("2min"), 2, _fut(102, 102, 102), _spot(99, 101))  # Q4: bid == ask
    _pair(tmp_path, day + pd.Timedelta("3min"), 3, _fut(100, 102, 101, suspended=True), _spot(99, 101))  # Q4
    _pair(tmp_path, day + pd.Timedelta("4min"), 4, _fut(103, 105, 104), _spot(102, 104))  # valid
    write_env(
        tmp_path,
        "spot_time",
        day + pd.Timedelta("4min 30s"),
        {"error": [], "result": {"unixtime": int((day + pd.Timedelta("4min")).timestamp())}},
    )  # 30 s off
    _pair(tmp_path, day + pd.Timedelta("5min"), 5, _fut(200, 202, 201), _spot(199, 201))  # spot clock bad
    fd = d.load_forward_daily(tmp_path, ["BTC", "ETH"], since=day, until=day + pd.Timedelta("1D"))
    assert fd.exec.loc[(day, "BTC"), "perp_mid"] == 104
    assert fd.daily.loc[(day, "BTC"), "basis_n"] == 1
    q = fd.asset_quality
    assert q.loc[(day, "BTC"), "valid_pairs"] == 1 and not q.loc[(day, "BTC"), "Q5"]
    assert q.loc[(day, "ETH"), "valid_pairs"] == 0 and q.loc[(day, "ETH"), "longest_invalid"] == 1440


def test_forward_daily_keeps_the_first_15_minutes_of_pairs_for_multi_leg_actions(tmp_path: Path):
    write_meta(tmp_path)
    day = pd.Timestamp("2026-10-12T00:00Z")
    two = {
        "result": "success",
        "tickers": [
            {"symbol": "PF_XBTUSD", "bid": 100, "ask": 102, "markPrice": 101},
            {"symbol": "PF_ETHUSD", "bid": 10, "ask": 10, "markPrice": 10},
        ],
    }  # ETH invalid in pair 1
    spot2 = {
        "error": [],
        "result": {
            "XXBTZUSD": {"a": ["101", "1", "1"], "b": ["99", "1", "1"]},
            "XETHZUSD": {"a": ["11", "1", "1"], "b": ["9", "1", "1"]},
        },
    }
    _pair(tmp_path, day + pd.Timedelta("1min"), 1, two, spot2)
    ok = {
        **two,
        "tickers": [two["tickers"][0], {"symbol": "PF_ETHUSD", "bid": 9, "ask": 11, "markPrice": 10}],
    }
    _pair(tmp_path, day + pd.Timedelta("2min"), 2, ok, spot2)
    fd = d.load_forward_daily(tmp_path, ["BTC", "ETH"], since=day, until=day + pd.Timedelta("1D"))
    pairs = fd.exec_pairs[day]
    assert [sorted(p[1]) for p in pairs] == [["BTC"], ["BTC", "ETH"]]


def test_funding_first_seen_from_hourly_fut_funding_only(tmp_path: Path):
    write_meta(tmp_path)
    t = pd.Timestamp("2026-10-12T05:00Z")
    stamp = t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    write_env(
        tmp_path,
        "fut_funding",
        t + pd.Timedelta("2h5min"),
        _funding_payload([stamp]),
        params={"symbol": "PF_XBTUSD"},
    )
    write_env(
        tmp_path,
        "fut_funding",
        t + pd.Timedelta("1h5min"),
        _funding_payload([stamp]),
        params={"symbol": "PF_XBTUSD"},
    )
    write_env(
        tmp_path,
        "fut_funding_full",
        t + pd.Timedelta("1h1min"),
        _funding_payload([stamp]),
        params={"symbol": "PF_XBTUSD"},
    )
    seen = d.funding_first_seen(tmp_path, ["BTC"], until=t + pd.Timedelta("1D"))
    assert seen["BTC"] == {t.value: (t + pd.Timedelta("1h5min")).value}
