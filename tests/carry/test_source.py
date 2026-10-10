"""CollectorSource reads the collector's raw files read-only
(var/forward/raw/<source>/date=D/HH.jsonl[.gz])."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pandas as pd

from luna.carry.funding import FundingStore
from luna.carry.source import CollectorSource
from tests.carry.helpers import T0, flat_history, funding_envelope, ticker, tickers_envelope


def _path(root: Path, source: str, ts: pd.Timestamp, gz: bool = False) -> Path:
    p = root / "raw" / source / f"date={ts:%Y-%m-%d}" / f"{ts:%H}.jsonl"
    return p.with_name(p.name + ".gz") if gz else p


def _write(path: Path, envs: list[dict], gz: bool = False, tail: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(json.dumps(e).encode() + b"\n" for e in envs) + tail
    path.write_bytes(gzip.compress(data) if gz else data)


def test_latest_tickers_takes_the_last_complete_ok_line(tmp_path: Path) -> None:
    now = T0 + pd.Timedelta(minutes=30)
    a = tickers_envelope(T0 + pd.Timedelta(minutes=28), [ticker("BTC", 100.0)])
    b = tickers_envelope(T0 + pd.Timedelta(minutes=29), [ticker("BTC", 101.0)])
    bad = tickers_envelope(T0 + pd.Timedelta(minutes=30), [], ok=False)
    _write(_path(tmp_path, "fut_tickers", now), [a, b, bad], tail=b'{"source": "fut_tick')  # torn line
    snap = CollectorSource(tmp_path).latest_tickers(now)
    assert snap is not None and snap.ts == T0 + pd.Timedelta(minutes=29)
    assert snap.quotes["BTC"].mid == 101.0


def test_falls_back_to_the_previous_hour_gz(tmp_path: Path) -> None:
    prev = T0 + pd.Timedelta(minutes=59)
    _write(
        _path(tmp_path, "fut_tickers", prev, gz=True),
        [tickers_envelope(prev, [ticker("ETH", 50.0)])],
        gz=True,
    )
    now = T0 + pd.Timedelta(hours=1, seconds=30)  # new hour, its file not written yet
    snap = CollectorSource(tmp_path).latest_tickers(now)
    assert snap is not None and snap.ts == prev


def test_never_opens_an_older_open_hour_file(tmp_path: Path) -> None:
    """An open .jsonl of an hour that ended > 60 s ago may be gzip-replaced by the collector right
    now (Windows: an open handle blocks the replace); only its .gz is read."""
    prev = T0 + pd.Timedelta(minutes=59)
    _write(_path(tmp_path, "fut_tickers", prev), [tickers_envelope(prev, [ticker("ETH", 50.0)])])
    now = T0 + pd.Timedelta(hours=1, minutes=1, seconds=30)
    assert CollectorSource(tmp_path).latest_tickers(now) is None


def test_missing_root_gives_nothing(tmp_path: Path) -> None:
    assert CollectorSource(tmp_path / "nope").latest_tickers(T0) is None


def test_funding_refresh_merges_hourly_files(tmp_path: Path) -> None:
    now = T0 + pd.Timedelta(minutes=10)
    rows = flat_history(2e-5, T0, hours=48)
    _write(
        _path(tmp_path, "fut_funding", now), [funding_envelope("BTC", rows), funding_envelope("ETH", rows)]
    )
    store = FundingStore()
    n = CollectorSource(tmp_path).refresh_funding(store, now)
    assert n == 96 and store.assets() == ["BTC", "ETH"]


def test_bootstrap_reads_the_latest_full_history(tmp_path: Path) -> None:
    day = T0 - pd.Timedelta(days=1) + pd.Timedelta(minutes=20)
    full = flat_history(1e-5, T0 - pd.Timedelta(hours=2), hours=24 * 40)
    _write(
        _path(tmp_path, "fut_funding_full", day, gz=True),
        [funding_envelope("SOL", full, source="fut_funding_full")],
        gz=True,
    )
    store = FundingStore()
    CollectorSource(tmp_path).bootstrap_funding(store, T0, keep_hours=24 * 35)
    assert store.trailing("SOL", T0, 720)[1] == 718  # last period starts T0 - 3h
    assert store.trailing("SOL", T0, 24 * 40)[1] == 24 * 35 - 2  # pruned to keep_hours


def test_newest_tickers_ts_is_cheap_and_monotone(tmp_path: Path) -> None:
    now = T0 + pd.Timedelta(minutes=5)
    src = CollectorSource(tmp_path)
    assert src.has_new_tickers(now, None)
    _write(
        _path(tmp_path, "fut_tickers", now), [tickers_envelope(T0 + pd.Timedelta(minutes=4), [ticker("BTC")])]
    )
    snap = src.latest_tickers(now)
    assert not src.has_new_tickers(now, snap.ts)
    _write(
        _path(tmp_path, "fut_tickers", now), [tickers_envelope(T0 + pd.Timedelta(minutes=5), [ticker("BTC")])]
    )
    assert src.has_new_tickers(now, snap.ts)


def test_latest_books_keeps_the_newest_book_per_asset(tmp_path: Path) -> None:
    now = T0 + pd.Timedelta(minutes=12)

    def book_env(sym: str, ts: pd.Timestamp, bid: float) -> dict:
        iso = ts.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"
        return {
            "source": "fut_book",
            "recv_ts": iso,
            "ok": True,
            "params": {"symbol": sym},
            "payload": {"orderBook": {"bids": [[bid, 1.0]], "asks": [[bid + 1, 1.0]]}},
        }

    envs = [
        book_env("PF_XBTUSD", T0 + pd.Timedelta(minutes=5), 10.0),
        book_env("PF_ETHUSD", T0 + pd.Timedelta(minutes=5), 20.0),
        book_env("PF_XBTUSD", T0 + pd.Timedelta(minutes=10), 11.0),
    ]
    _write(_path(tmp_path, "fut_book", now), envs, tail=b'{"source": "fut_bo')
    books = CollectorSource(tmp_path).latest_books(now)
    assert set(books) == {"BTC", "ETH"}
    assert books["BTC"][0] == T0 + pd.Timedelta(minutes=10) and books["BTC"][1][0] == [(11.0, 1.0)]


def test_funding_changed_detects_a_growing_hour_file(tmp_path: Path) -> None:
    """2026-10-10: the bot read the open fut_funding file while the collector was still writing it."""
    now = T0 + pd.Timedelta(minutes=5)
    path = _path(tmp_path, "fut_funding", now)
    rows = flat_history(2e-5, T0, hours=48)
    _write(path, [funding_envelope("BTC", rows)])
    src, store = CollectorSource(tmp_path), FundingStore()
    assert src.refresh_funding(store, now) == 48
    assert not src.funding_changed(now)  # nothing new since the read
    _write(path, [funding_envelope("BTC", rows), funding_envelope("ETH", rows)])
    assert src.funding_changed(now + pd.Timedelta(seconds=10))
    assert src.refresh_funding(store, now + pd.Timedelta(seconds=10)) == 96  # rows read (BTC again + ETH)
    assert store.assets() == ["BTC", "ETH"]
    assert not src.funding_changed(now + pd.Timedelta(seconds=20))


def test_funding_changed_when_the_hour_file_appears(tmp_path: Path) -> None:
    now = T0 + pd.Timedelta(minutes=3)  # the :05 round has not landed yet
    src, store = CollectorSource(tmp_path), FundingStore()
    assert src.refresh_funding(store, now) == 0
    assert not src.funding_changed(now)
    _write(_path(tmp_path, "fut_funding", now), [funding_envelope("BTC", flat_history(2e-5, T0, hours=48))])
    assert src.funding_changed(now + pd.Timedelta(minutes=2))
