"""Forward execution (S-H1-X, S-H3-11): synthetic snapshot streams; no fake fill from later books,
partial depth, 60-minute timeout, taker fallback, hedge lag, no book data, exit symmetry, gaps."""

import gzip
import json

import pandas as pd
import pytest

from luna.v6 import forward_exec as fx

T = pd.Timestamp("2026-12-01T00:00Z")
S = pd.Timedelta("1s")
M = pd.Timedelta("1min")


def streams(ticks, depths, fticks=None, fbooks=None, a="ETH"):
    s = fx.Streams()
    s.spot_ticks[a] = fx.Stream([fx.Tick(T + dt, b, k) for dt, b, k in ticks])
    s.spot_depth[a] = fx.Stream([fx.Book(T + dt, tuple(bids), tuple(asks)) for dt, bids, asks in depths])
    s.fut_ticks[a] = fx.Stream([fx.Tick(T + dt, b, k) for dt, b, k in (fticks or [(0 * S, 99.9, 100.1)])])
    s.fut_book[a] = fx.Stream([fx.Book(T + dt, tuple(bids), tuple(asks)) for dt, bids, asks in
                               (fbooks or [(5 * M, [(99.9, 100.0)], [(100.1, 100.0)])])])
    return s


def test_maker_fill_needs_a_later_crossing_ticker_and_visible_depth():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (10 * M, 99.9, 100.0), (20 * M, 99.8, 99.95)],
                depths=[(-60 * S, [(100.0, 5)], [(100.2, 5)]), (19 * M, [(99.8, 5)], [(99.95, 2.0)])])
    r = fx.maker_first_spot(s, "ETH", 3.0, T)
    # limit = 100.0 (ref bid); 10 min ticker ask 100.0 <= limit but the depth known then (t-60 s) has no
    # ask <= 100.0 -> nothing; at 20 min ask 99.95 crosses with 2.0 visible -> partial maker fill 2.0
    maker = [f for f in r.fills if f.maker]
    assert maker and maker[0].ts == T + 20 * M and maker[0].dq == pytest.approx(2.0) and maker[0].px == 100.0
    assert maker[0].fee == pytest.approx(0.004 * 2.0 * 100.0)


def test_depth_snapshot_after_the_ticker_is_never_used():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (10 * M, 99.9, 99.95)],
                depths=[(-60 * S, [(100.0, 5)], [(100.2, 5)]), (11 * M, [(99.9, 5)], [(99.95, 9.0)])])
    r = fx.maker_first_spot(s, "ETH", 3.0, T)
    assert not any(f.maker for f in r.fills)  # the crossing depth arrived after the ticker


def test_each_depth_level_volume_is_used_once():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (5 * M, 100.0, 100.2), (10 * M, 99.9, 99.95), (11 * M, 99.9, 99.95)],
                depths=[(9 * M, [(99.9, 5)], [(99.95, 1.0)])])
    r = fx.maker_first_spot(s, "ETH", 3.0, T)
    assert sum(f.dq for f in r.fills if f.maker) == pytest.approx(1.0)  # two crossings, one snapshot


def test_timeout_then_taker_fallback_walks_depth_and_cancels_the_rest():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (30 * M, 100.0, 100.2)],
                depths=[(55 * M, [(100.0, 5)], [(100.2, 1.0), (100.3, 1.0)])])
    r = fx.maker_first_spot(s, "ETH", 3.0, T)
    taker = [f for f in r.fills if not f.maker]
    assert taker and taker[0].ts == T + 60 * M and taker[0].dq == pytest.approx(2.0)
    assert taker[0].px == pytest.approx((100.2 + 100.3) / 2) and r.status == "partial"
    assert taker[0].fee == pytest.approx(0.008 * 2.0 * taker[0].px)


def test_first_crossing_ticker_is_marketable_taker_not_maker():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (10 * M, 99.9, 99.95)], depths=[(9 * M, [(99.9, 5)], [(99.95, 1.0)])])
    r = fx.maker_first_spot(s, "ETH", 1.0, T)
    assert len(r.fills) == 1 and not r.fills[0].maker and r.fills[0].fee == pytest.approx(0.008 * 99.95)


def test_marketable_order_fills_as_taker_only_at_levels_within_the_limit():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (1 * M, 99.5, 99.9)],
                depths=[(30 * S, [(99.5, 5)], [(99.9, 1.0), (100.1, 5.0)])])
    r = fx.maker_first_spot(s, "ETH", 3.0, T)
    first = r.fills[0]
    assert not first.maker and first.dq == pytest.approx(1.0) and first.px == pytest.approx(99.9)


def test_stale_reference_waits_for_a_fresh_ticker_and_gap_skips():
    s = streams(ticks=[(-10 * M, 100.0, 100.2), (2 * M, 100.0, 100.2)], depths=[(1 * M, [(100.0, 5)], [(100.2, 5)])])
    r = fx.maker_first_spot(s, "ETH", 1.0, T)
    assert r is not None and all(f.ts >= T + 2 * M for f in r.fills)  # t_sub = the fresh ticker
    s2 = streams(ticks=[(-10 * M, 100.0, 100.2), (7 * pd.Timedelta("1h"), 100.0, 100.2)],
                 depths=[(1 * M, [(100.0, 5)], [(100.2, 5)])])
    r2 = fx.maker_first_spot(s2, "ETH", 1.0, T)
    assert r2.fills == [] and r2.status.startswith("skipped")


def test_no_book_data_gives_no_fill():
    s = fx.Streams()
    assert fx.maker_first_spot(s, "ETH", 1.0, T) is None


def test_exit_is_symmetric_maker_sell_at_the_ask():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (5 * M, 100.0, 100.2), (10 * M, 100.2, 100.3)],
                depths=[(9 * M, [(100.25, 5.0)], [(100.3, 5)])])
    r = fx.maker_first_spot(s, "ETH", -2.0, T)
    m = [f for f in r.fills if f.maker]
    assert m and m[0].dq == pytest.approx(-2.0) and m[0].px == 100.2


def test_perp_hedge_on_the_first_book_after_the_spot_leg_and_lag_is_explicit():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (10 * M, 99.9, 99.95)],
                depths=[(9 * M, [(99.9, 5)], [(99.95, 5.0)])],
                fticks=[(0 * S, 99.9, 100.1), (11 * M, 99.0, 99.2)],
                fbooks=[(5 * M, [(99.9, 50.0)], [(100.1, 50.0)]), (12 * M, [(99.0, 50.0)], [(99.2, 50.0)])])
    m = fx.FwdMarket(s, None, ["ETH"])
    spot, perp = m.pair_fill("ETH", 2.0, T, economic=True)
    assert spot[0].ts == T + 10 * M and perp[0].ts == T + 12 * M  # first book after the spot completed
    assert perp[0].dq == pytest.approx(-2.0) and perp[0].px == pytest.approx(99.0)
    assert perp[0].fee == pytest.approx(0.0005 * 2.0 * 99.0)


def test_risk_exits_are_taker_on_both_legs():
    s = streams(ticks=[(-30 * S, 100.0, 100.2)], depths=[(2 * M, [(100.0, 5)], [(100.2, 5)])],
                fbooks=[(3 * M, [(99.9, 50.0)], [(100.1, 50.0)])])
    m = fx.FwdMarket(s, None, ["ETH"])
    spot, perp = m.pair_fill("ETH", -2.0, T, economic=False)
    assert not spot[0].maker and spot[0].px == 100.0 and perp[0].dq == pytest.approx(2.0)


def test_h3_forward_fill_walks_the_first_book_after_the_decision():
    s = streams(ticks=[(-30 * S, 100.0, 100.2)], depths=[(1 * M, [(100.0, 5)], [(100.2, 5)])],
                fticks=[(-30 * S, 99.9, 100.1), (4 * M, 99.9, 100.1)],
                fbooks=[(-1 * M, [(99.0, 1.0)], [(99.1, 1.0)]), (5 * M, [(99.9, 1.0)], [(100.1, 1.0), (100.3, 2.0)])])
    m = fx.FwdMarket(s, None, ["ETH"])
    f = m.fill("ETH", 2.0, T)
    assert f.ts == T + 5 * M and f.px == pytest.approx((100.1 + 100.3) / 2)  # not the book before t


def test_reader_parses_collector_envelopes(tmp_path):
    raw = tmp_path / "raw"
    def put(source, recs):
        d = raw / source / "date=2026-12-01"
        d.mkdir(parents=True)
        with gzip.open(d / "00.jsonl.gz", "wt", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
    rt = "2026-12-01T00:00:05Z"
    put("fut_tickers", [{"recv_ts": rt, "ok": True, "payload": {"tickers": [
        {"symbol": "PF_ETHUSD", "bid": 99.9, "ask": 100.1, "markPrice": 100.0, "volumeQuote": 5e7}]}}])
    put("fut_book", [{"recv_ts": rt, "ok": True, "params": {"symbol": "PF_ETHUSD"},
                      "payload": {"orderBook": {"bids": [[99.9, 3]], "asks": [[100.1, 4]]}}}])
    put("spot_ticker", [{"recv_ts": rt, "ok": True, "payload": {"result": {"ETHUSD": {
        "a": ["100.2", "1", "1.0"], "b": ["100.0", "1", "1.0"], "v": ["10", "200"], "p": ["100", "101"]}}}}])
    put("spot_depth", [{"recv_ts": rt, "ok": True, "params": {"pair": "ETHUSD"}, "payload": {"result": {"ETHUSD": {
        "asks": [["100.2", "5", 1]], "bids": [["100.0", "6", 1]]}}}}])
    s = fx.read_streams(raw, T, T + pd.Timedelta("1h"), ["ETH"])
    assert s.fut_ticks["ETH"].latest(T + M).mid == pytest.approx(100.0)
    assert s.fut_book["ETH"].latest(T + M).asks == ((100.1, 4.0),)
    assert s.spot_ticks["ETH"].latest(T + M).vol_quote == pytest.approx(200 * 101)
    assert s.spot_depth["ETH"].latest(T + M).bids == ((100.0, 6.0),)
    assert s.fut_ticks["ETH"].latest(T) is None  # received at 00:00:05, not usable at 00:00:00


def test_coverage_marks_days_below_ninety_percent():
    s = fx.Streams()
    ticks = [fx.Tick(T + i * pd.Timedelta("15min"), 1, 1) for i in range(96) if i not in range(0, 15)]
    s.fut_ticks["ETH"] = fx.Stream(ticks)
    cov = fx.coverage(s, T, T + pd.Timedelta("23h45min"))
    assert cov["share"].iloc[0] == pytest.approx(81 / 96) and not cov["evaluable"].iloc[0]


def test_forward_funding_known_at_is_the_first_receive_time(tmp_path):
    raw = tmp_path / "raw"
    d = raw / "fut_funding" / "date=2026-12-01"
    d.mkdir(parents=True)
    rows = [{"timestamp": "2026-12-01T00:00:00.000Z", "fundingRate": 0.01, "relativeFundingRate": 1e-5}]
    with gzip.open(d / "01.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"recv_ts": "2026-12-01T01:05:00Z", "ok": True, "params": {"symbol": "PF_ETHUSD"},
                             "payload": {"rates": rows}}) + "\n")
        fh.write(json.dumps({"recv_ts": "2026-12-01T01:10:00Z", "ok": True, "params": {"symbol": "PF_ETHUSD"},
                             "payload": {"rates": [{**rows[0], "relativeFundingRate": 9.0}]}}) + "\n")
    p = fx.funding_from_collector(raw, T, T + pd.Timedelta("2h"), ["ETH"])
    assert p.abs_rate("ETH", T, T + pd.Timedelta("1h")) != p.abs_rate("ETH", T, T + pd.Timedelta("1h"))  # NaN: not received
    assert p.abs_rate("ETH", T, T + pd.Timedelta("65min")) == pytest.approx(0.01)
    assert p.rel.loc[T, "ETH"] == pytest.approx(1e-5)  # the later revision is ignored


def test_maker_loop_never_uses_a_depth_received_after_the_ticker():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (5 * M, 100.0, 100.2), (10 * M, 99.9, 99.95)],
                depths=[(-60 * S, [(100.0, 5)], [(100.2, 5)]), (11 * M, [(99.9, 5)], [(99.95, 9.0)])])
    r = fx.maker_first_spot(s, "ETH", 3.0, T)
    assert not any(f.maker for f in r.fills)


def test_no_maker_fill_after_the_60_minute_window():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (5 * M, 100.0, 100.2), (70 * M, 99.9, 99.95)],
                depths=[(65 * M, [(99.9, 5)], [(99.95, 9.0)])])
    r = fx.maker_first_spot(s, "ETH", 3.0, T)
    assert not any(f.maker for f in r.fills)


def test_a_stale_ticker_gives_no_mid():
    s = streams(ticks=[(-200 * S, 100.0, 100.2)], depths=[(1 * M, [(100.0, 5)], [(100.2, 5)])],
                fticks=[(-181 * S, 99.9, 100.1)])
    m = fx.FwdMarket(s, None, ["ETH"])
    assert m.mid("ETH", T) != m.mid("ETH", T)  # NaN: 181 s old
    assert m.spot_mid("ETH", T) != m.spot_mid("ETH", T)
