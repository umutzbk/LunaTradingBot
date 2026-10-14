"""Task 2 guard tests: holdout seal, M6 embargo, markers (spec S-1.2, S-1.3, S-1.7, DEC-19, L10, K9)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from luna.v7 import guard, windows
from luna.v7.timeutil import D, to_ns

M6_START = windows.m6()[0]
NOW = to_ns("2026-10-14")


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.txt").write_text("x")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "c")
    _git(r, "tag", "-a", guard.SPEC_TAG, "-m", "spec")
    _git(r, "tag", "-a", guard.CODE_TAG, "-m", "code")
    monkeypatch.setattr(guard, "verify_spec", lambda repo=None: None)
    return r


def _auth(repo, tmp_path):
    return guard.authorize_holdout(guard.approval_phrase(), repo, home=tmp_path / "home")


def test_hold_window_needs_authorization(repo, tmp_path):
    lo, hi = windows.W_HOLD[0] + D, windows.W_HOLD[0] + 2 * D
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_outcome_window(lo, hi, now_ns=NOW)
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_outcome_window(lo, hi, auth=object(), now_ns=NOW)  # type: ignore[arg-type]
    guard.check_outcome_window(lo, hi, auth=_auth(repo, tmp_path), now_ns=NOW)
    # dev window needs nothing
    guard.check_outcome_window(windows.W_DEV[0], windows.W_DEV[1], now_ns=NOW)


def test_authorization_not_constructible():
    with pytest.raises(guard.HoldoutBlocked):
        guard.HoldoutAuthorization(object(), {})


def test_m6_overlap_refused_even_with_auth(repo, tmp_path):
    auth = _auth(repo, tmp_path)
    for lo, hi in ((M6_START - D, M6_START + D), (M6_START, M6_START + 1), (M6_START + D, M6_START + 2 * D)):
        with pytest.raises(guard.EmbargoBlocked):
            guard.check_outcome_window(lo, hi, auth=auth, now_ns=NOW)
    with pytest.raises(guard.EmbargoBlocked):
        guard.check_outcome_window(M6_START - D, M6_START + D, now_ns=NOW)
    # ends exactly at the M6 start: no overlap (half-open)
    guard.check_outcome_window(M6_START - D, M6_START, auth=auth, now_ns=NOW)


def test_m6_lifted_only_when_release_known_and_reached():
    span = (M6_START, M6_START + 10 * D)
    with pytest.raises(guard.EmbargoBlocked):
        guard.check_outcome_window(M6_START, M6_START + D, now_ns=M6_START + 5 * D, m6_span=span)
    guard.check_outcome_window(M6_START, M6_START + D, now_ns=M6_START + 10 * D, m6_span=span)
    with pytest.raises(guard.EmbargoBlocked):  # R_V6 undetermined: never lifted
        guard.check_outcome_window(M6_START, M6_START + D, now_ns=NOW * 100, m6_span=(M6_START, None))


def test_marker_exclusivity(repo, tmp_path):
    _auth(repo, tmp_path)
    with pytest.raises(guard.HoldoutBlocked, match="already opened"):
        _auth(repo, tmp_path)
    assert guard.holdout_opened(repo, home=tmp_path / "home")
    assert len(guard.marker_paths(repo, home=tmp_path / "home")) == 2


def test_authorize_needs_phrase_tags_clean_head(repo, tmp_path):
    with pytest.raises(guard.HoldoutBlocked, match="approval"):
        guard.authorize_holdout("yes", repo, home=tmp_path / "home")
    assert not guard.holdout_opened(repo, home=tmp_path / "home")
    (repo / "dirty.txt").write_text("d")
    with pytest.raises(guard.HoldoutBlocked, match="not clean"):
        _auth(repo, tmp_path)
    _git(repo, "add", "dirty.txt")
    _git(repo, "commit", "-q", "-m", "more")
    with pytest.raises(guard.HoldoutBlocked, match="HEAD"):
        _auth(repo, tmp_path)
    _git(repo, "tag", "-d", guard.CODE_TAG)
    with pytest.raises(guard.HoldoutBlocked, match="missing"):
        _auth(repo, tmp_path)
    assert not guard.holdout_opened(repo, home=tmp_path / "home")


def test_spec_mismatch_blocks(repo, tmp_path, monkeypatch):
    def bad(repo=None):
        raise guard.SpecMismatch("x")

    monkeypatch.setattr(guard, "verify_spec", bad)
    with pytest.raises(guard.HoldoutBlocked, match="spec"):
        _auth(repo, tmp_path)


def test_run_id_and_phrase():
    assert guard.RUN_ID == "v7-" + guard.SPEC_BLOB_SHA256[:16]
    assert guard.SPEC_BLOB_SHA256[:16] in guard.approval_phrase()


def test_pin_manifest_once(repo, tmp_path):
    home = tmp_path / "home"
    auth = _auth(repo, tmp_path)
    assert guard.pinned_manifest(repo, home=home) is None
    guard.pin_manifest("ab" * 32, auth, repo, home=home)
    assert guard.pinned_manifest(repo, home=home) == "ab" * 32
    with pytest.raises(FileExistsError):
        guard.pin_manifest("cd" * 32, auth, repo, home=home)
    with pytest.raises(guard.HoldoutBlocked):
        guard.pin_manifest("ab" * 32, object(), repo, home=home)  # type: ignore[arg-type]


def test_check_listing_only():
    hold = "data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-08.zip"
    guard.check_listing_only(hold)  # listing: allowed
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_listing_only(hold, read_content=True)
    guard.check_listing_only("data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06.zip",
                             read_content=True)


def test_file_period():
    assert guard.file_period("X-15m-2025-06.zip") == (to_ns("2025-06-01"), to_ns("2025-07-01"))
    assert guard.file_period("a/b/X-2025-06-30.zip.CHECKSUM") == (to_ns("2025-06-30"), to_ns("2025-07-01"))
    assert guard.file_period("data/futures/um/monthly/klines/") is None


def test_historical_field_whitelist():
    assert guard.historical_field("baseAsset") == "baseAsset"
    for f in ("status", "onboardDate", "deliveryDate", "filters", "pricePrecision"):
        with pytest.raises(guard.GuardFailure):
            guard.historical_field(f)
