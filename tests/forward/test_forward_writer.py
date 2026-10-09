"""§5 storage + §10.5-7: append-only hours, hour close, day close/manifest, os.replace retry."""

import gzip
import hashlib
import json
import os
import stat

import pytest

from luna.forward import writer as w
from luna.forward.envelope import iso_ns
from luna.forward.writer import ClosedDayError, RawWriter

S = 1_000_000_000
DAY0 = 1_791_504_000 * S  # 2026-10-09T00:00:00Z
H12 = DAY0 + 12 * 3600 * S


def _env(source, recv_ns, ok=True, seq=1):
    return {"source": source, "seq": seq, "req_ts": iso_ns(recv_ns - 1000), "recv_ts": iso_ns(recv_ns),
            "latency_ms": 0.001, "ok": ok, "error": None if ok else "boom", "url": "u", "params": None,
            "filtered_to_universe": False, "payload": {"x": [1, 2.5, None]} if ok else None}


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


def test_append_goes_to_the_recv_ts_hour_file(tmp_path):
    wr = RawWriter(tmp_path)
    e1, e2 = _env("spot_time", H12 + 5 * S), _env("spot_time", H12 + 59 * 60 * S + 59 * S, seq=2)
    p = wr.append(e1)
    assert p == tmp_path / "raw" / "spot_time" / "date=2026-10-09" / "12.jsonl"
    assert wr.append(e2) == p
    lines = p.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [e1, e2]
    # a late response after midnight lands in the new day's file (chosen by recv_ts)
    late = wr.append(_env("spot_time", DAY0 + 86400 * S + 1))
    assert late == tmp_path / "raw" / "spot_time" / "date=2026-10-10" / "00.jsonl"


def test_hour_close_gzips_identical_content_and_removes_open_file(tmp_path):
    wr = RawWriter(tmp_path)
    p = wr.append(_env("fut_tickers", H12 + S))
    wr.append(_env("fut_tickers", H12 + 2 * S, ok=False))
    original = p.read_bytes()
    assert wr.close_hours(H12 + 3600 * S + 119 * S) == []  # not before 2 min past the hour
    assert wr.open_hours() == 1
    (gz,) = wr.close_hours(H12 + 3600 * S + 120 * S)
    assert gz == p.with_name("12.jsonl.gz")
    assert gzip.decompress(gz.read_bytes()) == original
    assert not p.exists() and wr.open_hours() == 0
    assert not list(gz.parent.glob("*.tmp"))


def test_leftover_open_hours_are_closed_by_a_new_writer(tmp_path):
    p = RawWriter(tmp_path).append(_env("spot_ticker", H12 + S))  # "previous run" died here
    original = p.read_bytes()
    closed = RawWriter(tmp_path).close_hours(H12 + 5 * 3600 * S)
    assert closed == [p.with_name("12.jsonl.gz")]
    assert gzip.decompress(closed[0].read_bytes()) == original and not p.exists()


def test_closing_an_hour_that_already_has_a_gz_keeps_both_parts(tmp_path):
    wr = RawWriter(tmp_path)
    p = wr.append(_env("spot_time", H12 + S))
    first = p.read_bytes()
    wr.close_hours(H12 + 2 * 3600 * S)
    wr.append(_env("spot_time", H12 + 2 * S, seq=2))
    second = p.read_bytes()
    (gz,) = wr.close_hours(H12 + 2 * 3600 * S)
    assert gzip.decompress(gz.read_bytes()) == first + second


def test_replace_retries_permission_error_then_succeeds(tmp_path, monkeypatch):
    real = os.replace
    fails = {"n": 2}

    def flaky(src, dst):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("locked by AV")
        real(src, dst)

    monkeypatch.setattr(w.os, "replace", flaky)
    sleeps = Sleeps()
    wr = RawWriter(tmp_path, sleep=sleeps)
    p = wr.append(_env("spot_time", H12 + S))
    original = p.read_bytes()
    (gz,) = wr.close_hours(H12 + 2 * 3600 * S)
    assert sleeps == [0.2, 0.4]
    assert gzip.decompress(gz.read_bytes()) == original and not p.exists()


def test_replace_gives_up_after_five_retries_without_losing_data(tmp_path, monkeypatch):
    def locked(src, dst):
        raise PermissionError("locked")

    sleeps = Sleeps()
    wr = RawWriter(tmp_path, sleep=sleeps)
    p = wr.append(_env("spot_time", H12 + S))
    original = p.read_bytes()
    monkeypatch.setattr(w.os, "replace", locked)
    with pytest.raises(PermissionError):
        wr.close_hours(H12 + 2 * 3600 * S)
    assert sleeps == [0.2, 0.4, 0.8, 1.6, 3.2]  # 6 attempts in total
    assert p.read_bytes() == original  # the open hour is untouched


def test_atomic_write_text(tmp_path):
    target = tmp_path / "a" / "x.json"
    w.atomic_write_text(target, "hello")
    w.atomic_write_text(target, "world")
    assert target.read_text(encoding="utf-8") == "world" and not list(target.parent.glob("*.tmp"))


def _fill_day(wr):
    wr.append(_env("fut_tickers", DAY0 + 10 * S))
    wr.append(_env("fut_tickers", DAY0 + 70 * S, ok=False, seq=2))
    wr.append(_env("fut_tickers", DAY0 + 23 * 3600 * S + 5 * S, seq=3))
    wr.append(_env("fut_funding", DAY0 + 23 * 3600 * S + 300 * S))


def test_day_close_writes_manifest_and_makes_files_read_only(tmp_path):
    wr = RawWriter(tmp_path)
    _fill_day(wr)
    end = DAY0 + 86400 * S
    wr.close_hours(end + 3 * 3600 * S)
    assert wr.close_days(end + 3 * 3600 * S - 1) == []  # not before 3h past midnight
    assert wr.close_days(end + 3 * 3600 * S) == ["2026-10-09"]
    m = json.loads((tmp_path / "manifest" / "date=2026-10-09.json").read_text(encoding="utf-8"))
    assert m["date"] == "2026-10-09"
    files = {f["path"]: f for f in m["files"]}
    f0 = files["raw/fut_tickers/date=2026-10-09/00.jsonl.gz"]
    data = (tmp_path / f0["path"]).read_bytes()
    assert f0["sha256"] == hashlib.sha256(data).hexdigest() and f0["bytes"] == len(data)
    assert (f0["lines"], f0["ok"], f0["errors"]) == (2, 1, 1)
    assert m["sources"]["fut_tickers"] == {"expected": 1440, "received": 2, "lines": 3, "errors": 1}
    assert m["sources"]["fut_funding"]["expected"] == 24 * 21
    assert m["sources"]["fut_book"] == {"expected": 288 * 21, "received": 0, "lines": 0, "errors": 0}
    for f in files:
        assert not (tmp_path / f).stat().st_mode & stat.S_IWRITE
    assert wr.closed_days() == ["2026-10-09"]
    assert wr.close_days(end + 9 * 3600 * S) == []  # once only


def test_expected_counts_start_at_collector_start(tmp_path):
    wr = RawWriter(tmp_path)
    _fill_day(wr)
    end = DAY0 + 86400 * S
    wr.close_hours(end + 3 * 3600 * S)
    wr.close_days(end + 3 * 3600 * S, start_ns=DAY0 + 12 * 3600 * S)
    m = json.loads((tmp_path / "manifest" / "date=2026-10-09.json").read_text(encoding="utf-8"))
    assert m["sources"]["fut_tickers"]["expected"] == 720
    assert m["sources"]["fut_analytics"]["expected"] == 12 * 126


def test_day_with_an_open_hour_is_not_closed(tmp_path):
    wr = RawWriter(tmp_path)
    _fill_day(wr)
    assert wr.close_days(DAY0 + 86400 * S + 4 * 3600 * S) == []  # 23.jsonl still open


def test_writing_into_a_closed_day_fails(tmp_path):
    wr = RawWriter(tmp_path)
    _fill_day(wr)
    end = DAY0 + 86400 * S
    wr.close_hours(end + 3 * 3600 * S)
    wr.close_days(end + 3 * 3600 * S)
    with pytest.raises(ClosedDayError):
        wr.append(_env("spot_time", DAY0 + 5 * 3600 * S))
    assert not (tmp_path / "raw" / "spot_time" / "date=2026-10-09").exists()
    assert wr.append(_env("spot_time", end + S)).parent.name == "date=2026-10-10"
