from __future__ import annotations

import math

import pandas as pd
import pytest

from luna.carry.market import MarketSnapshot, parse_tickers
from tests.carry.helpers import T0, ticker, tickers_envelope


def test_parse_real_ticker_fields_by_asset() -> None:
    env = tickers_envelope(T0, [ticker("BTC", 60000.0, 1.0, rel=1e-5), ticker("TAO", 274.6, 8.0)])
    snap = parse_tickers(env)
    assert isinstance(snap, MarketSnapshot)
    assert snap.ts == T0
    assert set(snap.quotes) == {"BTC", "TAO"}
    q = snap.quotes["BTC"]
    assert q.mid == pytest.approx(60000.0)
    assert q.spread_full == pytest.approx(1e-4)
    assert q.half_spread == pytest.approx(0.5e-4)
    assert q.funding_rel == pytest.approx(1e-5)
    assert q.volume_usd == pytest.approx(50e6)
    assert q.top_book_usd == pytest.approx(min(q.bid * 1000.0, q.ask * 1000.0))
    assert q.valid


def test_invalid_quotes_are_flagged_not_dropped() -> None:
    rows = [ticker("A", 10.0), ticker("B", 10.0), ticker("C", 10.0, suspended=True)]
    rows[0]["bid"] = 0.0
    rows[1]["bid"], rows[1]["ask"] = 10.1, 10.0  # crossed
    snap = parse_tickers(tickers_envelope(T0, rows))
    assert not snap.quotes["A"].valid and not snap.quotes["B"].valid
    assert snap.quotes["C"].suspended and not snap.quotes["C"].valid
    assert math.isnan(snap.quotes["A"].mid)


def test_failed_envelope_gives_no_snapshot() -> None:
    assert parse_tickers(tickers_envelope(T0, [], ok=False)) is None


def test_non_universe_or_non_perpetual_rows_are_ignored() -> None:
    fut = ticker("ETH")
    fut["tag"] = "month"
    snap = parse_tickers(tickers_envelope(T0, [fut, {"symbol": "garbage"}]))
    assert snap is not None and snap.quotes == {}


def test_snapshot_age() -> None:
    snap = parse_tickers(tickers_envelope(T0, [ticker("ETH")]))
    assert snap.age_s(T0 + pd.Timedelta(seconds=42)) == pytest.approx(42.0)
