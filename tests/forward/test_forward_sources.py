"""§3: request lists of the per-symbol and hourly sources."""

from luna.forward import config, sources

T0_S = 1_791_547_200 + 7 * 60  # 2026-10-09T12:07:00Z


def test_fut_book_one_call_per_perp():
    reqs = sources.fut_book()
    assert [r.params for r in reqs] == [{"symbol": s} for s in config.PERP_SYMBOLS]
    assert {r.url for r in reqs} == {"https://futures.kraken.com/derivatives/api/v3/orderbook"}
    assert {r.source for r in reqs} == {"fut_book"} and all(r.transform is None for r in reqs)


def test_spot_depth_one_call_per_pair_count_10():
    reqs = sources.spot_depth()
    assert [r.params for r in reqs] == [{"pair": p, "count": 10} for p in config.SPOT_PAIRS]
    assert {r.url for r in reqs} == {"https://api.kraken.com/0/public/Depth"}


def test_fut_funding_one_call_per_perp():
    reqs = sources.fut_funding()
    assert [r.params for r in reqs] == [{"symbol": s} for s in config.PERP_SYMBOLS]
    assert {r.url for r in reqs} == {"https://futures.kraken.com/derivatives/api/v4/historicalfundingrates"}


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
        "fut_tickers", "spot_ticker", "fut_book", "spot_depth", "fut_funding", "fut_analytics", "spot_time"
    }
