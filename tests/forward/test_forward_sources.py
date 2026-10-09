"""§3: request lists of the per-symbol and hourly sources."""

from luna.forward import config, sources

T0_S = 1_791_547_200 + 7 * 60  # 2026-10-09T12:07:00Z


def test_fut_book_one_call_per_perp():
    reqs = sources.fut_book()
    assert [r.params for r in reqs] == [{"symbol": s} for s in config.PERP_SYMBOLS]
    assert {r.url for r in reqs} == {"https://futures.kraken.com/derivatives/api/v3/orderbook"}
    assert {r.source for r in reqs} == {"fut_book"} and all(r.transform is None for r in reqs)
    assert all(r.extra is None and not r.universe_filtered for r in reqs)


def test_spot_depth_one_call_per_pair_count_10():
    reqs = sources.spot_depth()
    assert [r.params for r in reqs] == [{"pair": p, "count": 10} for p in config.SPOT_PAIRS]
    assert {r.url for r in reqs} == {"https://api.kraken.com/0/public/Depth"}


FUNDING_URL = "https://futures.kraken.com/derivatives/api/v4/historicalfundingrates"


def test_fut_funding_hourly_keeps_only_the_last_48h():
    slot = 1_791_547_200 + 300  # 12:05
    reqs = sources.fut_funding(slot)
    assert [r.params for r in reqs] == [{"symbol": s} for s in config.PERP_SYMBOLS]
    assert {r.url for r in reqs} == {FUNDING_URL} and {r.source for r in reqs} == {"fut_funding"}
    since = "2026-10-07T12:05:00.000000000Z"
    assert reqs[0].extra == {"filtered_since_ts": since} and not reqs[0].universe_filtered
    rates = [{"timestamp": "2026-10-07T12:00:00.000Z", "fundingRate": 1, "relativeFundingRate": 2},
             {"timestamp": "2026-10-07T13:00:00.000Z", "fundingRate": 3, "relativeFundingRate": 4.5},
             {"timestamp": "2026-10-09T12:00:00.000Z", "fundingRate": -1e-9, "relativeFundingRate": 0}]
    out = reqs[0].transform({"result": "success", "rates": rates, "serverTime": "t"})
    assert out == {"result": "success", "rates": rates[1:], "serverTime": "t"}  # rows unmodified


def test_fut_funding_full_daily_audit_call():
    reqs = sources.fut_funding_full()
    assert [r.params for r in reqs] == [{"symbol": s} for s in config.PERP_SYMBOLS]
    assert {r.url for r in reqs} == {FUNDING_URL} and {r.source for r in reqs} == {"fut_funding_full"}
    assert reqs[0].transform is None and reqs[0].extra == {"filtered_since_ts": None}
    cad = config.SOURCES["fut_funding_full"]
    assert (cad.period_s, cad.offset_s, cad.calls) == (86400, 1200, 21)  # 00:20 UTC


def test_fut_analytics_last_3h_at_300s_for_every_type():
    reqs = sources.fut_analytics(T0_S)
    assert len(reqs) == 21 * 6
    assert config.ANALYTICS_TYPES == (
        "open-interest", "spreads", "liquidity", "future-basis", "funding", "long-short-info"
    )
    first = reqs[0]
    assert first.url == "https://futures.kraken.com/api/charts/v1/analytics/PF_ADAUSD/open-interest"
    assert first.params == {"since": T0_S - 3 * 3600, "to": T0_S, "interval": 300}
    assert {r.source for r in reqs} == {"fut_analytics"}


def test_spot_time():
    (req,) = sources.spot_time()
    assert req.url == "https://api.kraken.com/0/public/Time" and req.params is None


def test_every_source_id_has_a_function():
    assert set(config.SOURCES) == {
        "fut_tickers", "spot_ticker", "fut_book", "spot_depth", "fut_funding", "fut_analytics", "spot_time",
        "fut_instruments", "spot_assetpairs", "fut_funding_full",
    }
    for name in config.SOURCES:
        assert callable(getattr(sources, name))


def test_daily_reference_sources_cadence():
    fi, sa = config.SOURCES["fut_instruments"], config.SOURCES["spot_assetpairs"]
    assert (fi.period_s, fi.offset_s) == (86400, 600)  # 00:10 UTC
    assert (sa.period_s, sa.offset_s) == (86400, 720)  # 00:12 UTC
    assert config.AT_START == ("fut_instruments", "spot_assetpairs")


def test_fut_instruments_filtered_to_universe():
    (req,) = sources.fut_instruments()
    assert req.url == "https://futures.kraken.com/derivatives/api/v3/instruments" and req.params is None
    raw = {"result": "success", "serverTime": "t", "instruments": [
        {"symbol": "PF_XBTUSD", "tickSize": 1}, {"symbol": "PF_FOOUSD"}, {"symbol": "FI_XBTUSD_261225"},
        {"symbol": "PF_ZECUSD", "contractValueTradePrecision": 2}]}
    out = req.transform(raw)
    assert out["instruments"] == [raw["instruments"][0], raw["instruments"][3]]
    assert out["serverTime"] == "t"


def test_spot_assetpairs_all_21_pairs_raw():
    (req,) = sources.spot_assetpairs()
    assert req.url == "https://api.kraken.com/0/public/AssetPairs"
    assert req.params == {"pair": ",".join(config.SPOT_PAIRS)} and req.transform is None
