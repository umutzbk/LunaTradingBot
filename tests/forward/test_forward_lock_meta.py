"""§9 lock (§10.10) and §2/§4 meta.json: collector_start_ts once, seq resumes (§10.9)."""

import json

import pytest

from luna.forward.lock import CollectorLock, CollectorLocked
from luna.forward.meta import Meta

S = 1_000_000_000
T0 = 1_791_547_200 * S


class Clock:
    def __init__(self, ns=T0):
        self.ns = ns

    def __call__(self):
        return self.ns


def test_lock_written_with_pid_host_and_heartbeat(tmp_path):
    clock = Clock()
    lock = CollectorLock(tmp_path / "collector.lock", clock, pid=11, host="a")
    lock.acquire()
    data = json.loads((tmp_path / "collector.lock").read_text(encoding="utf-8"))
    assert data["pid"] == 11 and data["host"] == "a" and data["heartbeat_ns"] == T0
    clock.ns += 60 * S
    lock.heartbeat()
    data = json.loads((tmp_path / "collector.lock").read_text(encoding="utf-8"))
    assert data["heartbeat_ns"] == T0 + 60 * S


def test_second_instance_is_rejected(tmp_path):
    clock = Clock()
    CollectorLock(tmp_path / "collector.lock", clock, pid=11, host="a").acquire()
    clock.ns += 299 * S
    with pytest.raises(CollectorLocked):
        CollectorLock(tmp_path / "collector.lock", clock, pid=12, host="a",
                      pid_alive=lambda p: True).acquire()
    with pytest.raises(CollectorLocked):  # another container: same pid 1, different hostname
        CollectorLock(tmp_path / "collector.lock", clock, pid=11, host="b").acquire()


def test_stale_lock_is_taken_over(tmp_path):
    clock = Clock()
    CollectorLock(tmp_path / "collector.lock", clock, pid=11, host="a").acquire()
    clock.ns += 301 * S
    CollectorLock(tmp_path / "collector.lock", clock, pid=12, host="b").acquire()
    assert json.loads((tmp_path / "collector.lock").read_text(encoding="utf-8"))["pid"] == 12


def test_same_host_and_pid_is_a_restart_of_the_same_container(tmp_path):
    clock = Clock()
    CollectorLock(tmp_path / "collector.lock", clock, pid=1, host="c1").acquire()
    clock.ns += 10 * S
    CollectorLock(tmp_path / "collector.lock", clock, pid=1, host="c1").acquire()  # no raise


def test_release_only_removes_own_lock(tmp_path):
    clock = Clock()
    mine = CollectorLock(tmp_path / "collector.lock", clock, pid=11, host="a")
    mine.acquire()
    clock.ns += 400 * S
    theirs = CollectorLock(tmp_path / "collector.lock", clock, pid=12, host="b")
    theirs.acquire()
    mine.release()
    assert (tmp_path / "collector.lock").exists()
    theirs.release()
    assert not (tmp_path / "collector.lock").exists()


def test_collector_start_ts_written_once(tmp_path):
    m = Meta.load(tmp_path)
    m.start(T0)
    m.save()
    m2 = Meta.load(tmp_path)
    m2.start(T0 + 3600 * S)
    m2.save()
    data = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert data["collector_start_ts"] == "2026-10-09T12:00:00.000000000Z"
    assert data["version"] == 1


def test_seq_counters_resume_after_restart(tmp_path):
    m = Meta.load(tmp_path)
    m.start(T0)
    assert [m.next_seq("fut_tickers") for _ in range(3)] == [1, 2, 3]
    assert m.next_seq("spot_ticker") == 1
    assert m.next_pair_seq() == 1 and m.next_pair_seq() == 2
    m.save()
    m2 = Meta.load(tmp_path)
    assert m2.next_seq("fut_tickers") == 4 and m2.next_seq("spot_time") == 1
    assert m2.next_pair_seq() == 3


def test_reserve_allocates_a_block_of_seqs(tmp_path):
    m = Meta.load(tmp_path)
    assert m.reserve("fut_book", 21) == 1
    assert m.reserve("fut_book", 21) == 22
    assert m.next_seq("fut_book") == 43


# ---- M5 (same host, dead pid) and M2 (heartbeat refuses a lost lock)


def test_same_host_dead_pid_is_taken_over(tmp_path):
    clock = Clock()
    CollectorLock(tmp_path / "collector.lock", clock, pid=11, host="luna-collector").acquire()
    clock.ns += 10 * S
    CollectorLock(tmp_path / "collector.lock", clock, pid=12, host="luna-collector",
                  pid_alive=lambda pid: False).acquire()
    assert json.loads((tmp_path / "collector.lock").read_text(encoding="utf-8"))["pid"] == 12


def test_same_host_live_pid_is_refused(tmp_path):
    clock = Clock()
    CollectorLock(tmp_path / "collector.lock", clock, pid=11, host="luna-collector").acquire()
    with pytest.raises(CollectorLocked):
        CollectorLock(tmp_path / "collector.lock", clock, pid=12, host="luna-collector",
                      pid_alive=lambda pid: True).acquire()


def test_other_host_fresh_is_refused_even_if_pid_looks_dead(tmp_path):
    clock = Clock()
    CollectorLock(tmp_path / "collector.lock", clock, pid=11, host="a").acquire()
    with pytest.raises(CollectorLocked):
        CollectorLock(tmp_path / "collector.lock", clock, pid=12, host="b",
                      pid_alive=lambda pid: False).acquire()


def test_pid_alive_real_processes():
    import os
    import subprocess
    import sys

    from luna.forward.lock import pid_alive

    assert pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not pid_alive(p.pid)


def test_heartbeat_refuses_a_lost_lock(tmp_path):
    clock = Clock()
    mine = CollectorLock(tmp_path / "collector.lock", clock, pid=11, host="a")
    mine.acquire()
    clock.ns += 400 * S
    CollectorLock(tmp_path / "collector.lock", clock, pid=12, host="b").acquire()
    with pytest.raises(CollectorLocked):
        mine.heartbeat()
    assert json.loads((tmp_path / "collector.lock").read_text(encoding="utf-8"))["pid"] == 12
