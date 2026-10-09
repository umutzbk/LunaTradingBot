import json
import os

import pandas as pd
import pytest

from luna import fsutil
from luna.data import kraken_spot as ks
from luna.data.store import BarStore


class FlakyReplace:
    """os.replace stand-in raising PermissionError (WinError 5) for the first `fails` calls."""

    def __init__(self, fails: int) -> None:
        self.fails = fails
        self.calls = 0

    def __call__(self, src, dst):
        self.calls += 1
        if self.calls <= self.fails:
            raise PermissionError(13, "Access is denied")
        os.replace(src, dst)


def test_replace_with_retry_backoff_then_success(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a"
    src.write_text("new")
    dst.write_text("old")
    flaky = FlakyReplace(3)
    monkeypatch.setattr(fsutil, "_os_replace", flaky)
    sleeps: list[float] = []
    fsutil.replace_with_retry(src, dst, sleep=sleeps.append)
    assert dst.read_text() == "new"
    assert not src.exists()
    assert sleeps == [0.1, 0.2, 0.4]


def test_replace_with_retry_exhausted_raises(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a"
    src.write_text("new")
    dst.write_text("old")
    monkeypatch.setattr(fsutil, "_os_replace", FlakyReplace(100))
    sleeps: list[float] = []
    with pytest.raises(PermissionError):
        fsutil.replace_with_retry(src, dst, sleep=sleeps.append)
    assert sleeps == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2]
    assert dst.read_text() == "old"


def test_replace_with_retry_other_oserror_not_retried(tmp_path, monkeypatch):
    def boom(src, dst):
        raise FileNotFoundError(src)

    monkeypatch.setattr(fsutil, "_os_replace", boom)
    sleeps: list[float] = []
    with pytest.raises(FileNotFoundError):
        fsutil.replace_with_retry(tmp_path / "x", tmp_path / "y", sleep=sleeps.append)
    assert sleeps == []


def test_write_text_atomic_retries_locked_tmp(tmp_path, monkeypatch):
    path = tmp_path / "cp.json"
    real = fsutil._write_text
    calls = {"n": 0}

    def flaky_write(p, text):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "Access is denied")
        real(p, text)

    monkeypatch.setattr(fsutil, "_write_text", flaky_write)
    sleeps: list[float] = []
    fsutil.write_text_atomic(path, "hello", sleep=sleeps.append)
    assert path.read_text() == "hello"
    assert sleeps == [0.1, 0.2]
    assert list(tmp_path.iterdir()) == [path]


def test_checkpoint_replace_fails_then_succeeds(tmp_path, monkeypatch):
    cp = tmp_path / "cp" / "XBTUSD.json"
    monkeypatch.setattr(fsutil, "_os_replace", FlakyReplace(4))
    sleeps: list[float] = []
    ks._write_checkpoint(cp, {"pair": "XBTUSD", "since": 5}, sleep=sleeps.append)
    assert json.loads(cp.read_text()) == {"pair": "XBTUSD", "since": 5}
    assert list(cp.parent.iterdir()) == [cp]  # no leftover tmp
    assert len(sleeps) == 4


def test_checkpoint_replace_beyond_budget_keeps_previous(tmp_path, monkeypatch):
    cp = tmp_path / "XBTUSD.json"
    ks._write_checkpoint(cp, {"pair": "XBTUSD", "since": 1}, sleep=lambda s: None)
    monkeypatch.setattr(fsutil, "_os_replace", FlakyReplace(100))
    with pytest.raises(PermissionError):
        ks._write_checkpoint(cp, {"pair": "XBTUSD", "since": 2}, sleep=lambda s: None)
    assert json.loads(cp.read_text()) == {"pair": "XBTUSD", "since": 1}


def test_barstore_write_retries_replace(tmp_path, monkeypatch):
    store = BarStore(tmp_path)
    idx = pd.date_range("2026-01-01", periods=2, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0, "trades": 4}, index=idx
    )
    flaky = FlakyReplace(2)
    monkeypatch.setattr(fsutil, "_os_replace", flaky)
    sleeps: list[float] = []
    monkeypatch.setattr(fsutil.time, "sleep", sleeps.append)
    store.write("kraken_spot", "XBT", df)
    assert len(store.read("kraken_spot", "XBT")) == 2
    assert flaky.calls == 3
    assert sleeps == [0.1, 0.2]
