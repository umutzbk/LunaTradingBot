"""Collector loop with a fake clock (§3 schedule, §10.3/4/5/8/9/13) and status.json (§8)."""

import gzip
import json

import pytest

from luna.forward.collector import Collector
from luna.forward.lock import CollectorLocked
from luna.forward.writer import RawWriter
from luna.http import make_client

S = 1_000_000_000
T0 = 1_791_547_200 * S  # 2026-10-09T12:00:00Z
DAY = "date=2026-10-09"


def _collector(root, clock, kraken, backup_dir=None, **kw):
    import httpx

    return Collector(root, make_client(httpx.MockTransport(kraken)), clock_ns=clock, sleep=clock.sleep,
                     backup_dir=backup_dir, retries=0, backoff=0, lock_pid=1, lock_host="test", **kw)


def _rows(root, source):
    out = []
    for p in sorted((root / "raw" / source).glob("date=*/*")):
        data = gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()
        out += [json.loads(x) for x in data.splitlines()]
    return out


def _run(root, clock, kraken, seconds, **kw):
    c = _collector(root, clock, kraken, **kw)
    c.start()
    c.run_forever(until_ns=T0 + seconds * S)
    c.close()
    return c


def test_ten_minutes_schedule_and_pair_seq(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    _run(root, clock, kraken, 600)
    assert kraken.count("/derivatives/api/v3/tickers") == 10
    assert kraken.count("/0/public/Ticker") == 10
    assert kraken.count("/derivatives/api/v3/orderbook") == 2 * 21
    assert kraken.count("/0/public/Depth") == 2 * 21
    assert kraken.count("/0/public/Time") == 2
    assert kraken.count("historicalfundingrates") == 21  # 12:05
    assert kraken.count("/api/charts/v1/analytics/") == 21 * 6  # 12:07
    fut, spot = _rows(root, "fut_tickers"), _rows(root, "spot_ticker")
    assert [r["pair_seq"] for r in fut] == [r["pair_seq"] for r in spot] == list(range(1, 11))
    assert [r["seq"] for r in fut] == list(range(1, 11))
    # spot_ticker is the very next request after fut_tickers
    paths = [p for p, _, _ in kraken.calls]
    for i, p in enumerate(paths):
        if p.endswith("/v3/tickers"):
            assert paths[i + 1].endswith("/0/public/Ticker")
    # minute rounds start at :00 seconds (at most one in-flight request late)
    assert all((t - T0) % (60 * S) <= kraken.latency_ns for t in kraken.times("/v3/tickers"))
    assert kraken.times("/public/Time")[0] == T0 + 150 * S  # :02:30 round: spot_time, fut_book, spot_depth
    assert kraken.times("/orderbook")[0] == T0 + 150 * S + kraken.latency_ns


def test_heavy_sources_are_spaced_and_sequential(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    _run(tmp_path / "forward", clock, kraken, 300)
    book, depth = kraken.times("/orderbook"), kraken.times("/Depth")
    assert all(b - a >= 0.5 * S for a, b in zip(book, book[1:], strict=False))
    assert all(b - a >= 1.0 * S for a, b in zip(depth, depth[1:], strict=False))
    assert book[-1] < depth[0]  # one after the other, never interleaved


def test_hourly_and_daily_sources_and_start_fetch(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    _run(tmp_path / "forward", clock, kraken, 3600 + 8 * 60)
    assert kraken.count("historicalfundingrates") == 2 * 21  # 12:05, 13:05
    assert 0 <= kraken.times("historicalfundingrates")[0] - (T0 + 300 * S) < S  # :05, after the minute round
    assert kraken.count("/api/charts/v1/analytics/") == 2 * 21 * 6  # 12:07, 13:07
    assert kraken.count("/v3/instruments") == 1  # at start only (daily at 00:10)
    assert kraken.count("/AssetPairs") == 1


def test_daily_reference_sources_run_at_00_10_and_00_12(tmp_path, fake_kraken):
    start = T0 + 12 * 3600 * S - 60 * S  # 23:59
    clock, kraken = fake_kraken(start)
    c = _collector(tmp_path / "forward", clock, kraken)
    c.start()
    c.run_forever(until_ns=start + 14 * 60 * S)
    c.close()
    assert kraken.count("/v3/instruments") == 2 and kraken.count("/AssetPairs") == 2
    t = kraken.times("/v3/instruments")[1] - (T0 + 12 * 3600 * S + 600 * S)  # 00:10, after the minute round
    assert 0 <= t < S
    assert kraken.times("/v3/instruments")[0] < start + S  # the start fetch


def test_failed_tick_writes_error_rows_and_continues(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    kraken.fail = {"/v3/tickers", "/public/Ticker"}
    c = _collector(root, clock, kraken)
    c.start()
    c.run_forever(until_ns=T0 + 30 * S)
    kraken.fail = set()
    c.run_forever(until_ns=T0 + 90 * S)
    c.close()
    fut = _rows(root, "fut_tickers")
    assert [r["ok"] for r in fut] == [False, True]
    assert fut[0]["payload"] is None and "500" in fut[0]["error"]
    assert c.exit_code == 0


def test_dead_feed_exits_with_code_3_after_30_minutes(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    kraken.fail = {"/v3/tickers", "/public/Ticker"}
    c = _collector(tmp_path / "forward", clock, kraken)
    c.start()
    c.run_forever(until_ns=T0 + 3 * 3600 * S)
    c.close()
    assert c.exit_code == 3
    assert T0 + 30 * 60 * S <= clock.ns < T0 + 32 * 60 * S


def test_one_ticker_source_alive_is_not_dead(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    kraken.fail = {"/v3/tickers"}
    c = _collector(tmp_path / "forward", clock, kraken)
    c.start()
    c.run_forever(until_ns=T0 + 45 * 60 * S)
    c.close()
    assert c.exit_code == 0


def test_start_closes_leftover_open_hours(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    old = RawWriter(root).path_for("spot_time", T0 - 2 * 3600 * S)
    old.parent.mkdir(parents=True)
    old.write_bytes(b'{"x":1}\n')
    c = _collector(root, clock, kraken)
    c.start()
    c.close()
    assert not old.exists() and gzip.decompress(old.with_name("10.jsonl.gz").read_bytes()) == b'{"x":1}\n'


def test_collector_start_ts_kept_and_seq_resumed_across_restarts(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    _run(root, clock, kraken, 120)
    meta1 = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    clock.ns = T0 + 3600 * S
    c = _collector(root, clock, kraken)
    c.start()
    c.run_forever(until_ns=T0 + 3600 * S + 30 * S)
    c.close()
    meta2 = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    assert meta1["collector_start_ts"] == meta2["collector_start_ts"] == "2026-10-09T12:00:00.000000000Z"
    assert [r["seq"] for r in _rows(root, "fut_tickers")] == [1, 2, 3]
    assert [r["pair_seq"] for r in _rows(root, "spot_ticker")] == [1, 2, 3]


def test_second_collector_is_refused(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    first = _collector(root, clock, kraken)
    first.start()
    import httpx

    other = Collector(root, make_client(httpx.MockTransport(kraken)), clock_ns=clock, sleep=clock.sleep,
                      lock_pid=2, lock_host="other")
    with pytest.raises(CollectorLocked):
        other.start()
    first.close()


def test_status_json_contents(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    kraken.fail = {"/0/public/Time"}
    _run(root, clock, kraken, 400)
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["collector_start_ts"] == "2026-10-09T12:00:00.000000000Z"
    ft = st["sources"]["fut_tickers"]
    assert ft["today"] == {"date": "2026-10-09", "ok": 7, "error": 0}
    assert ft["last_ok_recv_ts"].startswith("2026-10-09T12:06:00")
    tm = st["sources"]["spot_time"]
    assert tm["last_ok_recv_ts"] is None and tm["today"]["error"] == 1 and "500" in tm["last_error"]
    assert st["open_hours"] == len(list((root / "raw").glob("*/date=*/*.jsonl")))
    assert st["backup_ok"] is False and "LUNA_FORWARD_BACKUP_DIR" in st["backup_reason"]
    assert set(st["sources"]) >= {"fut_tickers", "spot_ticker", "fut_funding", "fut_instruments"}


def test_today_counts_survive_a_restart_on_the_same_day(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    _run(root, clock, kraken, 120)
    _run(root, clock, kraken, 240)
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["sources"]["fut_tickers"]["today"]["ok"] == 4


def test_day_close_and_backup_while_collecting(tmp_path, fake_kraken):
    start = T0 + 11 * 3600 * S + 50 * 60 * S  # 2026-10-09T23:50Z
    clock, kraken = fake_kraken(start)
    root, target = tmp_path / "var" / "forward", tmp_path / "drive"
    target.mkdir()
    c = _collector(root, clock, kraken, backup_dir=target)
    c.start()
    c.run_forever(until_ns=start + (3 * 3600 + 20 * 60) * S)  # past 03:00 next day
    c.close()
    assert (root / "manifest" / f"{DAY}.json").exists()
    assert (root / "backup" / f"{DAY}.json").exists()
    assert (target / "forward" / "manifest" / f"{DAY}.json").exists()
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["backup_ok"] is True
    # §9/§10.11: nothing is written outside var/forward and the backup target
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    outside = [p for p in files if not (p.is_relative_to(root) or p.is_relative_to(target))]
    assert files and outside == []


def test_unreachable_backup_does_not_stop_collection(tmp_path, fake_kraken):
    start = T0 + 11 * 3600 * S + 50 * 60 * S
    clock, kraken = fake_kraken(start)
    root = tmp_path / "var" / "forward"
    c = _collector(root, clock, kraken, backup_dir=tmp_path / "missing")
    c.start()
    c.run_forever(until_ns=start + (3 * 3600 + 20 * 60) * S)
    c.close()
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["backup_ok"] is False and "unreachable" in st["backup_reason"]
    assert c.exit_code == 0
    assert st["sources"]["fut_tickers"]["last_ok_recv_ts"].startswith("2026-10-10T03:09")


def test_failed_start_releases_the_lock(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    root.mkdir()
    (root / "meta.json").write_text("{corrupt", encoding="utf-8")  # never silently reset start ts
    c = _collector(root, clock, kraken)
    with pytest.raises(ValueError):
        c.start()
    assert not (root / "collector.lock").exists()
