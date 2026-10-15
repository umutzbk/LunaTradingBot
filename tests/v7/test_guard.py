"""Task 2 guard tests: holdout seal, M6 embargo, markers (spec S-1.2, S-1.3, S-1.7, DEC-19, L10, K9). Since
Task 24 (SC-05) the opening needs a persisted pre-opening record and a manifest to pin: the repository is the
deterministic frozen repo of ``auth_fixtures`` (its HEAD is the cached reports' code commit)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from luna.v7 import guard, windows
from luna.v7.timeutil import D, to_ns
from tests.v7 import auth_fixtures as af

M6_START = windows.m6()[0]
NOW = to_ns("2026-10-14")


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = af.frozen_repo(tmp_path / "repo")
    monkeypatch.setattr(guard, "verify_spec", lambda repo=None: None)
    return r


def _auth(repo, tmp_path):
    rec = tmp_path / "pre_opening.json"
    if not rec.exists():
        af.pre_opening(rec)
    return guard.authorize_holdout(guard.approval_phrase(), repo, home=tmp_path / "home", pre_opening=rec,
                                   manifest_sha256=af.MANIFEST)


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


def test_m6_lifted_only_when_release_known_and_reached(monkeypatch):
    span = (M6_START, M6_START + 10 * D)
    # a finite R_V6 is accepted only when the canonical (hash-checked) source provides it
    monkeypatch.setattr(windows, "m6_cached", lambda repo=None: span)
    with pytest.raises(guard.EmbargoBlocked):
        guard.check_outcome_window(M6_START, M6_START + D, now_ns=M6_START + 5 * D, m6_span=span)
    guard.check_outcome_window(M6_START, M6_START + D, now_ns=M6_START + 10 * D, m6_span=span)
    guard.check_outcome_window(M6_START, M6_START + D, now_ns=M6_START + 10 * D)
    with pytest.raises(guard.EmbargoBlocked):  # a span differing from the canonical one
        guard.check_outcome_window(M6_START, M6_START + D, now_ns=NOW * 100, m6_span=(M6_START, None))
    monkeypatch.undo()
    with pytest.raises(guard.EmbargoBlocked):  # R_V6 undetermined: never lifted
        guard.check_outcome_window(M6_START, M6_START + D, now_ns=NOW * 100, m6_span=(M6_START, None))


def test_forged_m6_span_refused():
    canon = windows.m6_cached()
    assert canon == windows.m6() == (M6_START, None)
    lo, hi = M6_START + D, M6_START + 2 * D
    for forged in ((windows.W_HOLD[1] + 30 * D, None), (M6_START, NOW - D), (M6_START, NOW),
                   (M6_START - D, None), (M6_START, M6_START + D)):
        with pytest.raises(guard.EmbargoBlocked, match="canonical"):
            guard.check_outcome_window(lo, hi, now_ns=NOW, m6_span=forged)
    with pytest.raises(guard.EmbargoBlocked, match="canonical"):  # even for a window outside M6
        guard.check_outcome_window(windows.W_DEV[0], windows.W_DEV[0] + D, now_ns=NOW,
                                   m6_span=(windows.W_HOLD[1] + D, None))
    assert canon == (windows.W_HOLD[1], None)  # M6 starts at the W_HOLD end: that span is the canonical one
    guard.check_outcome_window(windows.W_DEV[0], windows.W_DEV[0] + D, now_ns=NOW, m6_span=canon)


def test_m6_cached_reads_git_once(monkeypatch):
    windows.m6_cached()  # primed

    def boom(*a, **k):
        raise AssertionError("git read after the cache was primed")

    monkeypatch.setattr("luna.v7.spec._git", boom)
    monkeypatch.setattr("luna.v7.spec._git_show", boom)
    assert windows.m6_cached() == (M6_START, None)
    with pytest.raises(guard.EmbargoBlocked):
        guard.check_outcome_window(M6_START, M6_START + D, now_ns=NOW)


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
    """The manifest is pinned by the opening itself, exclusively and once, next to both markers (SC-05)."""
    home = tmp_path / "home"
    assert guard.pinned_manifest(repo, home=home) is None and not hasattr(guard, "pin_manifest")
    auth = _auth(repo, tmp_path)
    pin = guard.pinned_manifest(repo, home=home)
    assert pin["manifest_sha256"] == af.MANIFEST and pin == auth.record["pin"]
    assert all((m.parent / guard.PIN).exists() for m in guard.marker_paths(repo, home=home))
    with pytest.raises(guard.HoldoutBlocked, match="already opened"):
        _auth(repo, tmp_path)
    assert guard.pinned_manifest(repo, home=home) == pin


def test_check_listing_only():
    hold = "data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-08.zip"
    guard.check_listing_only(hold, access="listing")  # listing: allowed
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_listing_only(hold, access="content")
    with pytest.raises(guard.HoldoutBlocked):  # M6 period
        guard.check_listing_only("X-15m-2026-08.zip", access="content")
    with pytest.raises(guard.HoldoutBlocked):  # unparseable period: fail closed
        guard.check_listing_only("X-15m-2026-13.zip", access="content")
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_listing_only("no-period.zip", access="content")
    with pytest.raises(TypeError):  # access is required
        guard.check_listing_only(hold)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        guard.check_listing_only(hold, access="read")
    guard.check_listing_only("data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06.zip",
                             access="content")


def test_file_period():
    assert guard.file_period("X-15m-2025-06.zip") == (to_ns("2025-06-01"), to_ns("2025-07-01"))
    assert guard.file_period("a/b/X-2025-06-30.zip.CHECKSUM") == (to_ns("2025-06-30"), to_ns("2025-07-01"))
    assert guard.file_period("data/futures/um/monthly/klines/") is None


def test_historical_field_whitelist():
    assert guard.historical_field("baseAsset") == "baseAsset"
    for f in ("status", "onboardDate", "deliveryDate", "filters", "pricePrecision"):
        with pytest.raises(guard.GuardFailure):
            guard.historical_field(f)
