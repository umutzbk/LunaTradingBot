"""Collector loop with a fake clock (§3 schedule, §10.3/4/5/8/9/13) and status.json (§8)."""

import gzip
import json

import pytest

from luna.forward.collector import Collector
from luna.forward.envelope import iso_ns
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
    assert kraken.count("historicalfundingrates") == 2 * 21  # start (full history) + 12:05
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
    assert kraken.times("/public/Time")[0] == T0 + 150 * S  # :02:30 spot round: spot_time, spot_depth
    assert kraken.times("/Depth")[0] == T0 + 150 * S + kraken.latency_ns
    assert kraken.times("/orderbook")[0] > kraken.times("/Depth")[20]  # then the futures round


def test_heavy_sources_are_spaced_and_sequential(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    _run(tmp_path / "forward", clock, kraken, 300)
    book, depth = kraken.times("/orderbook"), kraken.times("/Depth")
    assert all(b - a >= 0.5 * S for a, b in zip(book, book[1:], strict=False))
    assert all(b - a >= 1.0 * S for a, b in zip(depth, depth[1:], strict=False))
    assert depth[-1] < book[0]  # spot round first, then futures; never interleaved


def test_hourly_and_daily_sources_and_start_fetch(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    _run(tmp_path / "forward", clock, kraken, 3600 + 8 * 60)
    assert kraken.count("historicalfundingrates") == 3 * 21  # start (full), 12:05, 13:05
    assert 0 <= kraken.times("historicalfundingrates")[21] - (T0 + 300 * S) < S  # :05, after the minute round
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
    (root / "meta.json").write_text('{"collector_start_ts": "2026-10-09T09:00:00Z"}', encoding="utf-8")
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
    (target / ".luna-backup-target").write_text("", encoding="utf-8")
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


def test_status_lists_days_that_failed_to_close(tmp_path, fake_kraken, monkeypatch):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    old = RawWriter(root).path_for("spot_time", T0 - 3 * 86400 * S)
    old.parent.mkdir(parents=True)
    old.write_bytes(b'{"x":1}\n')

    def broken(self, day, *a, **k):
        raise ValueError("cannot close")

    monkeypatch.setattr(RawWriter, "_close_day", broken)
    (root / "meta.json").write_text(json.dumps({"collector_start_ts": "2026-10-06T00:00:00.000000000Z"}),
                                    encoding="utf-8")
    c = _collector(root, clock, kraken)
    c.start()
    c.close()
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["unclosed_days"] == ["2026-10-06"]


def test_start_round_rows_are_flagged_at_start(tmp_path, fake_kraken):
    start = T0 + 12 * 3600 * S - 60 * S  # 23:59
    clock, kraken = fake_kraken(start)
    root = tmp_path / "forward"
    c = _collector(root, clock, kraken)
    c.start()
    c.run_forever(until_ns=start + 14 * 60 * S)
    c.close()
    rows = _rows(root, "fut_instruments")
    assert [r.get("at_start", False) for r in rows] == [True, False]
    assert "at_start" not in _rows(root, "fut_tickers")[0]


def test_funding_hourly_filtered_and_daily_full_at_00_20(tmp_path, fake_kraken):
    start = T0 + 12 * 3600 * S - 60 * S  # 23:59
    clock, kraken = fake_kraken(start)
    root = tmp_path / "forward"
    c = _collector(root, clock, kraken)
    c.start()
    c.run_forever(until_ns=start + 22 * 60 * S)  # to 00:21
    c.close()
    hourly, full = _rows(root, "fut_funding"), _rows(root, "fut_funding_full")
    assert len(hourly) == 21 and hourly[0]["filtered_since_ts"] == "2026-10-08T00:05:00.000000000Z"
    assert len(full) == 42 and all(r["filtered_since_ts"] is None for r in full)
    assert [r.get("at_start", False) for r in full] == [True] * 21 + [False] * 21  # start + 00:20
    assert full[21]["recv_ts"].startswith("2026-10-10T00:20:")
    assert all(r["attempts"] == 1 for r in hourly)


def test_hanging_backup_does_not_block_collection(tmp_path, fake_kraken, monkeypatch):
    import threading

    from luna.forward import backup as bk
    from luna.forward import collector as coll

    start = T0 + 11 * 3600 * S + 50 * 60 * S  # 23:50
    clock, kraken = fake_kraken(start)
    root, target = tmp_path / "var" / "forward", tmp_path / "drive"
    target.mkdir()
    (target / bk.MARKER).write_text("", encoding="utf-8")
    release = threading.Event()
    real_copy, calls = bk._copy, []

    def hanging_copy(src, dst):
        release.wait(30)
        real_copy(src, dst)

    def counting(*a, **k):
        calls.append(a)
        return bk.run_backup(*a, **k)

    monkeypatch.setattr(bk, "_copy", hanging_copy)
    monkeypatch.setattr(coll, "run_backup", counting)
    c = _collector(root, clock, kraken, backup_dir=target)
    c.start()
    c.run_forever(until_ns=start + (3 * 3600 + 20 * 60) * S)
    assert len(calls) == 1  # still running: hourly triggers are skipped, never a second worker
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["sources"]["fut_tickers"]["last_ok_recv_ts"].startswith("2026-10-10T03:09")
    assert st["backup_running"] is True
    release.set()
    c.close()
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["backup_ok"] is True and st["backup_running"] is False


def test_slow_failures_keep_status_and_heartbeat_fresh_and_exit_3_in_time(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0, latency_s=20.0)
    kraken.fail = {""}  # every endpoint answers 500 after 20 s
    root = tmp_path / "forward"
    c = _collector(root, clock, kraken)
    c.start()
    writes, beats = [], []
    real_write, real_beat = c.status.write, c.lock.heartbeat

    def spy_write(now, *a, **k):
        writes.append(now)
        return real_write(now, *a, **k)

    def spy_beat():
        beats.append(clock.ns)
        return real_beat()

    c.status.write, c.lock.heartbeat = spy_write, spy_beat
    c.run_forever(until_ns=T0 + 3 * 3600 * S)
    c.close()
    assert c.exit_code == 3 and clock.ns <= T0 + 31 * 60 * S
    for seq in (writes, beats):
        times = [T0, *seq]
        assert max(b - a for a, b in zip(times, times[1:], strict=False)) <= 120 * S
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["sources"]["fut_book"]["skipped_calls"] > 0  # aborted at the next slot, no fake rows
    assert len(_rows(root, "fut_book")) < 21 * 6


def test_stop_interrupts_a_heavy_round(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0 + 150 * S)  # 12:02:30, five-minute round due
    c = _collector(tmp_path / "forward", clock, kraken)
    c.start()
    real = c._fetch_write
    n = {"calls": 0}

    def counting(*a, **k):
        n["calls"] += 1
        if n["calls"] == 5:
            c.stop()
        return real(*a, **k)

    c._fetch_write = counting
    c.run_forever(until_ns=T0 + 600 * S)
    c.close()
    assert n["calls"] == 5


def test_lost_lock_stops_the_collector(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    c = _collector(root, clock, kraken)
    c.start()
    c.run_forever(until_ns=T0 + 90 * S)
    (root / "collector.lock").write_text(json.dumps({"pid": 5, "host": "intruder", "heartbeat_ns": clock.ns}),
                                         encoding="utf-8")
    c.run_forever(until_ns=T0 + 600 * S)
    assert c.exit_code == 1 and clock.ns < T0 + 200 * S
    c.close()
    assert json.loads((root / "collector.lock").read_text(encoding="utf-8"))["host"] == "intruder"


def test_backward_clock_step_reschedules(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    c = _collector(root, clock, kraken)
    c.start()
    c.run_forever(until_ns=T0 + 300 * S)
    n = kraken.count("/v3/tickers")
    clock.ns -= 3600 * S  # NTP steps the clock back an hour
    c.run_forever(until_ns=clock.ns + 300 * S)
    c.close()
    assert kraken.count("/v3/tickers") - n >= 4


def test_refuses_to_start_with_raw_data_but_no_meta(tmp_path, fake_kraken):
    from luna.forward.collector import CollectorStartError

    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    RawWriter(root).append({"source": "spot_time", "recv_ts": iso_ns(T0 - S), "ok": True})
    c = _collector(root, clock, kraken)
    with pytest.raises(CollectorStartError, match="meta.json"):
        c.start()
    assert not (root / "meta.json").exists() and not (root / "collector.lock").exists()


# ---- second review: N1 (no starvation, no dropped slots), N4, N7


def test_slow_fut_book_never_starves_spot_depth(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    kraken.latency_by = {"/orderbook": 15.0}  # a futures round now overruns its 5-minute slot
    root = tmp_path / "forward"
    c = _collector(root, clock, kraken)
    c.start()
    c.run_forever(until_ns=T0 + 3600 * S + 60 * S)
    c.close()
    hour = T0 + 3600 * S
    assert len([t for t in kraken.times("/Depth") if t < hour]) == 12 * 21  # every slot, all calls
    starts = [t for p, q, t in kraken.calls
              if "/orderbook" in p and q.get("symbol") == "PF_ADAUSD" and t < hour]
    assert len(starts) == 12  # every futures slot is started, none dropped
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["sources"]["fut_book"]["skipped_calls"] > 0
    assert st["sources"]["spot_depth"]["skipped_calls"] == 0


def test_whole_skipped_slots_are_counted(tmp_path, fake_kraken):
    clock, kraken = fake_kraken(T0)
    root = tmp_path / "forward"
    c = _collector(root, clock, kraken)
    c.start()
    c.run_forever(until_ns=T0 + 30 * S)
    real = c._run_round

    def stall_once(r):
        if "fut_book" in r.sources and not getattr(stall_once, "done", False):
            stall_once.done = True
            clock.ns += 16 * 60 * S  # e.g. the machine slept: three 5-minute slots pass unseen
        return real(r)

    c._run_round = stall_once
    c.run_forever(until_ns=T0 + 25 * 60 * S)
    c.close()
    st = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert st["sources"]["fut_book"]["skipped_slots"] >= 2


def test_backup_handoff_and_join_timeout():
    import queue

    import httpx

    from luna.forward import collector as coll

    c = Collector("unused", make_client(httpx.MockTransport(lambda r: httpx.Response(500))))
    assert isinstance(c._backup_results, queue.SimpleQueue)
    assert coll.BACKUP_JOIN_S == 10.0 and c.backup_join_s == 10.0
