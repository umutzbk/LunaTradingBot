"""§7 forward-dq (§10.12) from synthetic envelopes: skew, coverage, gaps, completeness, sanity;
never prices/basis/spreads/returns. Funding rows before collector_start_ts are excluded."""

import json

from luna.forward import config
from luna.forward.dq import forward_dq
from luna.forward.envelope import iso_ns
from luna.forward.meta import Meta
from luna.forward.writer import RawWriter

S = 1_000_000_000
MS = 1_000_000
T0 = 1_791_547_200 * S  # 2026-10-09T12:00:00Z
PRICE_BID, PRICE_ASK = 12345.678, 12346.789  # distinctive, must never show up in the report


def _env(source, recv_ns, payload, ok=True, pair_seq=None, params=None, seq=1):
    env = {"source": source, "seq": seq}
    if pair_seq is not None:
        env["pair_seq"] = pair_seq
    env.update(req_ts=iso_ns(recv_ns - 50 * MS), recv_ts=iso_ns(recv_ns), latency_ms=50.0, ok=ok,
               error=None if ok else "boom", url="u", params=params, filtered_to_universe=False,
               payload=payload if ok else None)
    return env


def _fut_payload(server_ns, last_ns, suspended_eth=False, bad_quote=False):
    tick = [
        {"symbol": "PF_XBTUSD", "bid": PRICE_BID, "ask": PRICE_ASK, "markPrice": 12346.0,
         "lastTime": iso_ns(last_ns)[:23] + "Z", "suspended": False},
        {"symbol": "PF_ETHUSD", "bid": PRICE_ASK if bad_quote else PRICE_BID, "ask": PRICE_BID if bad_quote
         else PRICE_ASK, "lastTime": iso_ns(last_ns)[:23] + "Z", "suspended": suspended_eth},
    ]
    return {"result": "success", "serverTime": iso_ns(server_ns)[:23] + "Z", "tickers": tick}


def _spot_payload():
    q = {"a": [str(PRICE_ASK), "1", "1.000"], "b": [str(PRICE_BID), "1", "1.000"], "c": ["12346.1", "0.1"]}
    return {"error": [], "result": {"XXBTZUSD": q, "SOLUSD": q}}


def _setup(tmp_path):
    root = tmp_path / "forward"
    m = Meta.load(root)
    m.start(T0)
    m.save()
    w = RawWriter(root)
    # minutes 0..9 except 4,5,6 (gap of 4 min between ok snapshots at 3 and 7); minute 8 fails
    for i in [0, 1, 2, 3, 7, 8, 9]:
        t = T0 + i * 60 * S
        ok = i != 8
        w.append(_env("fut_tickers", t + 100 * MS, _fut_payload(t + 60 * MS, t - 900 * MS, i == 0, i == 1),
                      ok=ok, pair_seq=i + 1))
        w.append(_env("spot_ticker", t + 130 * MS, _spot_payload(), ok=ok, pair_seq=i + 1))
    w.append(_env("spot_time", T0 + 150 * S + 40 * MS, {"error": [], "result": {"unixtime": 1_791_547_350}}))
    # funding history: 3 rows before collector start (sealed holdout era), 1 after
    rates = [{"timestamp": iso_ns(T0 - h * 3600 * S)[:19] + ".000Z", "fundingRate": 0.1,
              "relativeFundingRate": 1e-5} for h in (3, 2, 1)]
    rates.append({"timestamp": "2026-10-09T13:00:00.000Z", "fundingRate": 0.2, "relativeFundingRate": 2e-5})
    w.append(_env("fut_funding", T0 + 3605 * S, {"result": "success", "rates": rates},
                  params={"symbol": "PF_XBTUSD"}))
    return root


def _dq(root):
    return forward_dq(root, since_ns=T0, until_ns=T0 + 600 * S)


def _numbers(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _numbers(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield obj


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_paired_skew(tmp_path):
    rep = _dq(_setup(tmp_path))
    ps = rep["paired_skew"]
    assert ps["pairs"] == 6  # both ok in the same pair_seq
    assert ps["fut_minus_spot_recv_ms"]["p50"] == -30.0
    assert ps["fut_recv_minus_lasttime_ms"]["max"] == 1000.0  # recv (t+100ms) - lastTime (t-900ms)


def test_clock_skew(tmp_path):
    rep = _dq(_setup(tmp_path))
    assert rep["clock_skew"]["futures_recv_minus_server_ms"]["p50"] == 40.0
    assert rep["clock_skew"]["spot_recv_minus_server_ms"]["max"] == 40.0  # unixtime has 1s resolution


def test_coverage(tmp_path):
    cov = _dq(_setup(tmp_path))["coverage"]
    assert cov["fut_tickers"]["2026-10-09"] == {"expected": 10, "ok": 6, "errors": 1, "at_start": 0,
                                             "ratio": 0.6}
    assert cov["spot_time"]["2026-10-09"]["expected"] == 2
    assert cov["fut_book"]["2026-10-09"] == {"expected": 42, "ok": 0, "errors": 0, "at_start": 0,
                                          "ratio": 0.0}
    assert set(cov) == set(config.SOURCES)


def test_gaps(tmp_path):
    gaps = _dq(_setup(tmp_path))["gaps"]
    (g,) = gaps["fut_tickers"]
    assert g["start"].startswith("2026-10-09T12:03:00") and g["end"].startswith("2026-10-09T12:07:00")
    assert g["duration_s"] == 240.0
    assert gaps["spot_time"] == []


def test_asset_completeness_and_quote_sanity(tmp_path):
    rep = _dq(_setup(tmp_path))
    fc = rep["asset_completeness"]["fut_tickers"]
    assert fc["snapshots"] == 6 and fc["present_min"] == 2 and fc["present_max"] == 2
    assert fc["missing"]["PF_ADAUSD"] == 6 and "PF_XBTUSD" not in fc["missing"]
    assert fc["suspended"] == {"PF_ETHUSD": 1}
    sc = rep["asset_completeness"]["spot_ticker"]
    assert sc["present_min"] == 2 and "XBTUSD" not in sc["missing"] and "SOLUSD" not in sc["missing"]
    assert sc["missing"]["ADAUSD"] == 6
    qs = rep["quote_sanity"]
    assert qs["fut_tickers"] == {"quotes": 12, "bid_le_ask": 11, "positive": 12}
    assert qs["spot_ticker"] == {"quotes": 12, "bid_le_ask": 12, "positive": 12}


def test_funding_rows_before_collector_start_are_excluded(tmp_path):
    rep = forward_dq(_setup(tmp_path), since_ns=T0, until_ns=T0 + 7200 * S)
    fc = rep["funding_coverage"]
    assert fc["since"] == "2026-10-09T12:00:00.000000000Z"
    assert fc["symbols"]["PF_XBTUSD"]["settlements"] == 1  # only 13:00; 09:00-11:00 excluded
    assert fc["rows_before_start_excluded"] == 3
    assert fc["expected_settlements_per_symbol"] == 2  # 12:00, 13:00 in [start, until)


def test_report_contains_no_prices_basis_spreads_or_returns(tmp_path):
    rep = _dq(_setup(tmp_path))
    json.dumps(rep)  # serialisable
    keys = {k.lower() for k in _keys(rep)}
    forbidden = {"price", "bid", "ask", "mid", "mark", "markprice", "last", "basis", "spread",
                 "return", "returns", "pnl", "fundingrate", "relativefundingrate", "rate"}
    assert not keys & forbidden
    nums = set(_numbers(rep))
    assert not nums & {PRICE_BID, PRICE_ASK, 12346.0, 12346.1, 0.1, 0.2, 1e-5, 2e-5}


# ---- review fixes: M1 (rows read once), M3 (fail closed), M6 (streaming), m6, DISK


def test_open_file_duplicating_a_closed_part_is_read_once(tmp_path):
    import gzip as _gz

    root = _setup(tmp_path)
    w = RawWriter(root)
    w.close_hours(T0 + 2 * 3600 * S)
    gz = root / "raw" / "fut_tickers" / "date=2026-10-09" / "12.jsonl.gz"
    (gz.with_suffix("")).write_bytes(_gz.decompress(gz.read_bytes()))  # crash before unlink
    late = _env("fut_tickers", T0 + 5 * 60 * S, _fut_payload(T0, T0), pair_seq=99)
    w.append(late)  # lands in 12.late.jsonl
    cov = _dq(root)["coverage"]["fut_tickers"]["2026-10-09"]
    assert (cov["ok"], cov["errors"]) == (7, 1)


def test_unknown_collector_start_fails_closed(tmp_path):
    import pytest

    root = tmp_path / "forward"
    RawWriter(root).append(_env("spot_time", T0, {"error": [], "result": {"unixtime": 1}}))
    with pytest.raises(ValueError, match="collector_start_ts"):
        forward_dq(root, since_ns=T0, until_ns=T0 + 60 * S)


def test_at_start_rows_are_excluded_from_coverage(tmp_path):
    root = _setup(tmp_path)
    w = RawWriter(root)
    e = _env("fut_instruments", T0 + S, {"instruments": []})
    e["at_start"] = True
    w.append(e)
    cov = forward_dq(root, since_ns=T0, until_ns=T0 + 86400 * S)["coverage"]["fut_instruments"]["2026-10-09"]
    assert cov["ok"] == 0 and cov["at_start"] == 1


def test_funding_coverage_uses_hourly_and_daily_full_rows(tmp_path):
    root = _setup(tmp_path)
    rates = [{"timestamp": "2026-10-09T13:00:00.000Z"}, {"timestamp": "2026-10-09T14:00:00.000Z"},
             {"timestamp": "2026-10-01T00:00:00.000Z"}]
    e = _env("fut_funding_full", T0 + 3 * 3600 * S, {"result": "success", "rates": rates},
             params={"symbol": "PF_XBTUSD"})
    e["filtered_since_ts"] = None
    RawWriter(root).append(e)
    fc = forward_dq(root, since_ns=T0, until_ns=T0 + 4 * 3600 * S)["funding_coverage"]
    assert fc["symbols"]["PF_XBTUSD"]["settlements"] == 2  # 13:00 (both sources) + 14:00
    assert fc["rows_before_start_excluded"] == 4


def test_dq_streams_rows(tmp_path, monkeypatch):
    import inspect
    import weakref

    from luna.forward import dq

    assert inspect.isgeneratorfunction(dq.iter_rows)
    root = _setup(tmp_path)

    class Row(dict):
        pass

    alive: list = []
    peak = {"n": 0}
    real = dq.iter_rows

    def spy(*a, **k):
        for r in real(*a, **k):
            row = Row(r)
            alive.append(weakref.ref(row))
            peak["n"] = max(peak["n"], sum(1 for x in alive if x() is not None))
            yield row
            del row

    monkeypatch.setattr(dq, "iter_rows", spy)
    forward_dq(root, since_ns=T0, until_ns=T0 + 7200 * S)
    assert len(alive) > 10 and peak["n"] <= 2  # never a list of all rows/payloads


def test_hour_closed_between_listing_and_reading_is_read_once(tmp_path, monkeypatch):
    from luna.forward import dq

    root = _setup(tmp_path)
    real = dq.already_closed

    def close_meanwhile(path):
        result = real(path)
        RawWriter(root).close_hours(T0 + 2 * 3600 * S)  # the collector closes the hour right now
        return result

    monkeypatch.setattr(dq, "already_closed", close_meanwhile)
    cov = _dq(root)["coverage"]["fut_tickers"]["2026-10-09"]
    assert (cov["ok"], cov["errors"]) == (6, 1)
