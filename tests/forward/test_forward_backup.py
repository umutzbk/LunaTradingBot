"""§6 backup (§10.8): copy closed days, verify sha256 against the manifest, then the receipt."""

import json
from pathlib import Path

import pytest

from luna.forward import backup as b
from luna.forward.backup import resolve_backup_dir, run_backup
from luna.forward.envelope import iso_ns
from luna.forward.writer import RawWriter

S = 1_000_000_000
DAY0 = 1_791_504_000 * S  # 2026-10-09T00:00:00Z
CLOSE = DAY0 + 27 * 3600 * S  # day close time (3h after midnight)


def _env(source, recv_ns):
    return {"source": source, "seq": 1, "req_ts": iso_ns(recv_ns), "recv_ts": iso_ns(recv_ns),
            "latency_ms": 0.0, "ok": True, "error": None, "url": "u", "params": None,
            "filtered_to_universe": False, "payload": {"v": 1}}


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "var" / "forward"
    wr = RawWriter(root)
    wr.append(_env("fut_tickers", DAY0 + S))
    wr.append(_env("spot_time", DAY0 + 5 * 3600 * S))
    wr.close_hours(CLOSE)
    assert wr.close_days(CLOSE) == ["2026-10-09"]
    return root


def test_resolve_backup_dir_treats_empty_as_unset():
    assert resolve_backup_dir(None) is None
    assert resolve_backup_dir("") is None and resolve_backup_dir("   ") is None
    assert resolve_backup_dir("/backup") == Path("/backup")


def test_backup_copies_verifies_and_writes_receipt(root, tmp_path):
    target = tmp_path / "drive"
    target.mkdir()
    res = run_backup(root, target, CLOSE + 60 * S)
    assert res.ok and res.reason is None and res.backed_up == ["2026-10-09"] and res.warnings == []
    manifest = json.loads((root / "manifest" / "date=2026-10-09.json").read_text(encoding="utf-8"))
    for f in manifest["files"]:
        assert (target / "forward" / f["path"]).read_bytes() == (root / f["path"]).read_bytes()
    assert (target / "forward" / "manifest" / "date=2026-10-09.json").exists()
    receipt = json.loads((root / "backup" / "date=2026-10-09.json").read_text(encoding="utf-8"))
    assert receipt["date"] == "2026-10-09" and receipt["files"] == len(manifest["files"])
    assert receipt["target"] == str(target)
    assert run_backup(root, target, CLOSE + 120 * S).backed_up == []  # receipt: done once


def test_corrupt_copy_writes_no_receipt(root, tmp_path, monkeypatch):
    target = tmp_path / "drive"
    target.mkdir()

    def bad_copy(src, dst):
        Path(dst).write_bytes(b"corrupted")

    monkeypatch.setattr(b, "_copy", bad_copy)
    res = run_backup(root, target, CLOSE + 60 * S)
    assert not res.ok and "sha256" in res.reason and res.backed_up == []
    assert not (root / "backup" / "date=2026-10-09.json").exists()
    monkeypatch.undo()
    assert run_backup(root, target, CLOSE + 3600 * S).backed_up == ["2026-10-09"]  # hourly retry


def test_unset_target_reports_not_ok(root):
    res = run_backup(root, None, CLOSE + 60 * S)
    assert not res.ok and "LUNA_FORWARD_BACKUP_DIR" in res.reason
    assert not (root / "backup").exists()


def test_unreachable_target_reports_not_ok(root, tmp_path):
    res = run_backup(root, tmp_path / "unplugged_disk", CLOSE + 60 * S)
    assert not res.ok and "unreachable" in res.reason
    assert not (tmp_path / "unplugged_disk").exists()


def test_overdue_warning_after_36h_without_receipt(root):
    assert run_backup(root, None, CLOSE + 36 * 3600 * S).warnings == []
    res = run_backup(root, None, CLOSE + 36 * 3600 * S + S)
    assert any("2026-10-09" in w and "36h" in w for w in res.warnings)
