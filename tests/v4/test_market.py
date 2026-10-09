"""Execution / valuation prices (spec §3, §5.1, §5.3): historical bars and forward snapshots."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from luna.v4 import market as mk
from luna.v4.data import ForwardDaily, HistoricalInputs
from tests.v4.synth import bars, hourly_funding, write_env, write_meta

D = pd.Timestamp("2026-03-02T00:00:00Z")


def _hist(perp=None, spot=None, mark=None, spreads=None):
    perp = perp if perp is not None else bars(D - pd.Timedelta("1D"), 3 * 96, close=101.0)
    spot = spot if spot is not None else bars(D - pd.Timedelta("1D"), 3 * 96, close=100.0)
    return HistoricalInputs(
        perp={"A": perp},
        spot={"A": spot},
        mark={"A": mark if mark is not None else pd.DataFrame()},
        index={"A": pd.DataFrame()},
        funding={"A": hourly_funding(D, 24)},
        spreads={"A": spreads if spreads is not None else pd.DataFrame()},
    )


def test_hist_exec_at_close_of_the_bar_opening_at_t():
    closes = (
        pd.Series(
            range(3 * 96),
            index=pd.date_range(D - pd.Timedelta("1D"), periods=3 * 96, freq="15min"),
            dtype="float64",
        )
        + 100
    )
    m = mk.HistoricalMarket(_hist(perp=bars(D - pd.Timedelta("1D"), 3 * 96, close=closes)))
    q = m.quote("A", "perp", D)
    assert q.mid == closes[D] and q.ts == D + pd.Timedelta("15min")
    assert q.half_spread_obs == pytest.approx(0.0005) and q.fallback  # no analytics: 10 bps full


def test_hist_exec_falls_back_to_first_bar_within_one_hour_else_none():
    p = bars(D - pd.Timedelta("1D"), 3 * 96, close=101.0)
    p = p.drop(pd.date_range(D, periods=2, freq="15min"))
    m = mk.HistoricalMarket(_hist(perp=p))
    assert m.quote("A", "perp", D).ts == D + pd.Timedelta("45min")
    p2 = p.drop(pd.date_range(D, periods=4, freq="15min"), errors="ignore")
    assert mk.HistoricalMarket(_hist(perp=p2)).quote("A", "perp", D) is None
    filled = bars(D - pd.Timedelta("1D"), 3 * 96, close=101.0)
    filled.loc[(filled.index >= D) & (filled.index < D + pd.Timedelta("1h")), "filled"] = True
    assert mk.HistoricalMarket(_hist(perp=filled)).quote("A", "perp", D) is None


def test_hist_perp_spread_from_analytics_hour():
    sp = pd.DataFrame({"spread_full": [0.0004]}, index=[D])
    q = mk.HistoricalMarket(_hist(spreads=sp)).quote("A", "perp", D)
    assert q.half_spread_obs == pytest.approx(0.0002) and not q.fallback
    assert mk.HistoricalMarket(_hist()).quote("A", "spot", D).half_spread_obs is None


def test_hist_value_is_close_of_the_2345_bar():
    closes = pd.Series(
        range(3 * 96),
        index=pd.date_range(D - pd.Timedelta("1D"), periods=3 * 96, freq="15min"),
        dtype="float64",
    )
    m = mk.HistoricalMarket(_hist(spot=bars(D - pd.Timedelta("1D"), 3 * 96, close=closes)))
    assert m.value("A", "spot", D) == closes[D + pd.Timedelta("23h45min")]


def test_hist_extremes_use_mark_bars_after_execution():
    mark = bars(D, 96, close=101.0)
    mark.loc[D + pd.Timedelta("5h"), "high"] = 150.0
    mark.loc[D, "high"] = 999.0  # the execution bar itself is not "after" the trade
    m = mk.HistoricalMarket(_hist(mark=mark))
    e = m.extremes("A", D)
    assert e["perp_high"] == 150.0 and e["spot_low"] == 100.0
    assert mk.HistoricalMarket(_hist()).extremes("A", D)["perp_high"] == 101.0  # trade-bar fallback


def _fd():
    idx = pd.MultiIndex.from_tuples([(D, "A"), (D + pd.Timedelta("1D"), "A")], names=["day", "asset"])
    ex = pd.DataFrame(
        {
            "ts": [D + pd.Timedelta("20s"), D + pd.Timedelta("1D 30s")],
            "perp_mid": [101.0, 102.0],
            "perp_half_spread": [0.0003, 0.0003],
            "spot_mid": [100.0, 100.5],
            "spot_half_spread": [0.0004, 0.0004],
            "mark": [101.0, 102.0],
            "perp_vol_quote": [1, 1],
            "spot_vol_usd": [1, 1],
            "suspended": [False, True],
        },
        index=idx,
    )
    daily = pd.DataFrame(
        {
            "basis_median": [0.01, 0.01],
            "basis_n": [1440, 0],
            "mark_max": [130.0, 103.0],
            "mark_min": [90.0, 101.0],
            "perp_max": [130.0, 103.0],
            "perp_min": [90.0, 101.0],
            "spot_min": [95.0, 100.0],
        },
        index=idx,
    )
    return ForwardDaily(exec=ex, daily=daily, quality=pd.DataFrame())


def test_forward_quote_value_extremes_suspension_outage():
    m = mk.ForwardMarket(_fd(), {"A": hourly_funding(D, 24)})
    q = m.quote("A", "spot", D)
    assert q.mid == 100.0 and q.half_spread_obs == 0.0004 and q.ts == D + pd.Timedelta("20s")
    assert m.quote("A", "perp", D + pd.Timedelta("1h")) is None  # decisions happen at 00:00 only
    assert m.value("A", "perp", D) == 102.0
    assert m.extremes("A", D) == {"perp_high": 130.0, "perp_low": 90.0, "spot_low": 95.0, "spot_high": None}
    assert not m.suspended("A", D) and m.suspended("A", D + pd.Timedelta("1D"))
    assert not m.outage(D - pd.Timedelta("1D")) and m.outage(D)  # day D+1 has no valid snapshot


def test_book_walk_average_price_and_effective_half_spread():
    asks = [(101.0, 1.0), (102.0, 1.0), (104.0, 10.0)]
    avg, short = mk.walk(asks, 3.0)
    assert avg == pytest.approx((101 + 102 + 104) / 3) and not short
    avg, short = mk.walk(asks, 20.0)
    assert short  # not enough depth: remainder at the worst level
    assert mk.walk(asks, 0.5)[0] == 101.0


def test_forward_quote_walks_the_book_when_the_order_exceeds_the_top():
    books = {("A", "spot"): {"bids": [(99.0, 0.5), (98.0, 10.0)], "asks": [(101.0, 0.5), (103.0, 10.0)]}}
    m = mk.ForwardMarket(_fd(), {}, book_loader=lambda a, leg, ts: books.get((a, leg)))
    small = m.quote("A", "spot", D, qty=0.1)
    assert small.half_spread_obs == 0.0004
    big = m.quote("A", "spot", D, qty=1.5)  # buys 0.5 @101 and 1.0 @103 vs depth mid 100
    assert big.half_spread_obs == pytest.approx(((0.5 * 101 + 1.0 * 103) / 1.5 - 100) / 100)
    assert m.counters["book_walks"] == 1
    m2 = mk.ForwardMarket(_fd(), {}, book_loader=lambda a, leg, ts: None)
    assert m2.quote("A", "spot", D, qty=1.5).half_spread_obs == 0.0004
    assert m2.counters["book_missing"] == 1


def test_nearest_book_reads_the_last_depth_snapshot_before_ts(tmp_path: Path):
    write_meta(tmp_path, pd.Timestamp("2026-03-01T00:00Z"))
    t = D
    write_env(
        tmp_path,
        "fut_book",
        t - pd.Timedelta("7min"),
        {"orderBook": {"bids": [[1, 2]], "asks": [[3, 4]]}},
        params={"symbol": "PF_XBTUSD"},
    )
    write_env(
        tmp_path,
        "fut_book",
        t - pd.Timedelta("2min"),
        {"orderBook": {"bids": [[5, 6]], "asks": [[7, 8]]}},
        params={"symbol": "PF_XBTUSD"},
    )
    write_env(
        tmp_path,
        "spot_depth",
        t - pd.Timedelta("2min"),
        {"error": [], "result": {"XXBTZUSD": {"bids": [["9", "1", 0]], "asks": [["11", "2", 0]]}}},
        params={"pair": "XBTUSD"},
    )
    assert mk.nearest_book(tmp_path, "BTC", "perp", t) == {"bids": [(5.0, 6.0)], "asks": [(7.0, 8.0)]}
    assert mk.nearest_book(tmp_path, "BTC", "spot", t) == {"bids": [(9.0, 1.0)], "asks": [(11.0, 2.0)]}
    assert mk.nearest_book(tmp_path, "BTC", "perp", t - pd.Timedelta("1h")) is None
