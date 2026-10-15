"""SC-06 (red-team M5): one canonical, hash-checked R_V6 / T_F7 source shared by ``windows.m6``,
``forward.l7``, the forward store, ``netguard`` and the outcome guard (spec S-1.2, S-1.3, S-1.7 M3-3 / N2,
DEC-45, §15, L10; AMB-34). Synthetic git fixtures only (``auth_fixtures``); the trusted clock is pinned by
monkeypatching ``release.trusted_now_ns`` (no API takes a date)."""

from __future__ import annotations

import inspect

import pytest

from luna.v7 import forward, guard, netguard, release, windows
from luna.v7.forward_store import ForwardStore, SealedBeforeL7
from luna.v7.timeutil import D, H, to_ns
from tests.v7 import auth_fixtures as af

M6_START = to_ns("2026-07-01")
TF7 = to_ns("2026-10-16")  # review PASS 2026-10-15T00:00Z -> first 00:00 >= 24 h later
R_V6 = to_ns("2027-10-14")
L7 = to_ns("2027-10-15")  # max(T_F7 + 180 d, R_V6) + 1 d
M6_KEY = "data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2026-08.zip"
LATE_KEY = "data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2027-11.zip"
OLD_KEY = "data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06.zip"


@pytest.fixture
def canon(tmp_path):
    return af.canonical_repo(tmp_path / "canon")


def test_without_a_record_m6_is_open_and_l7_undefined(tmp_path, monkeypatch):
    r = af.release_repo(tmp_path / "bare")
    release.cache_clear()
    assert windows.m6(r) == (M6_START, None) and windows.m6_cached(r) == (M6_START, None)
    assert release.r_v6(r) is None and release.t_f7(r) is None and forward.l7(r) is None
    af.clock(monkeypatch, to_ns("2040-01-01"))
    with pytest.raises(guard.EmbargoBlocked):
        guard.check_outcome_window(M6_START, M6_START + D, now_ns=to_ns("2040-01-01"), repo=r)
    with pytest.raises(SealedBeforeL7):
        forward.require_l7(r)
    # the real repository has no release record either
    assert release.r_v6() is None and forward.l7() is None


def test_canonical_record_ends_m6_and_derives_l7(canon):
    assert release.r_v6(canon) == R_V6 and release.t_f7(canon) == TF7
    assert windows.m6(canon) == (M6_START, R_V6) == windows.m6_cached(canon)
    assert forward.l7(canon) == L7 and forward.l7(canon, extended=True) == L7  # R_V6 dominates
    rel = release.rv6_release(canon)
    assert rel.basis == "a" and len(rel.evidence_sha256) == 64 and len(rel.record_sha256) == 64


def test_early_written_release_and_extension(tmp_path):
    r = af.canonical_repo(tmp_path / "c", r_v6="2027-01-01T00:00:00Z", basis="c",
                          status="released_in_writing")
    assert release.r_v6(r) == to_ns("2027-01-01")
    assert forward.l7(r) == TF7 + 181 * D and forward.l7(r, extended=True) == TF7 + 271 * D


def test_forward_l7_cannot_be_fed_a_guessed_r_v6(canon):
    params = inspect.signature(forward.l7).parameters
    assert "r_v6" not in params and "t_f7_ns" not in params
    with pytest.raises(TypeError):
        forward.l7(TF7, R_V6)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        forward.l7(canon, r_v6=to_ns("2026-11-01"))  # type: ignore[call-arg]
    assert "now_ns" not in inspect.signature(forward.require_l7).parameters
    assert not {"r_v6", "now_ns", "t_f7"} & {f for f in forward.ForwardInputs.__dataclass_fields__}


def test_store_sealed_regardless_of_arguments(tmp_path, monkeypatch, canon):
    """Red-team M5 probe: ``read(sha, now_ns=X, l7_ns=X)`` returned the raw bytes before any real L7."""
    s = ForwardStore(tmp_path / "fs")
    rec = s.write_raw("https://example/a", b"abc", TF7 + H)
    x = to_ns("2026-11-01")
    with pytest.raises(TypeError):
        s.read(rec.sha256, now_ns=x, l7_ns=x)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        list(s.iter_records(now_ns=x, l7_ns=x))  # type: ignore[call-arg]
    af.clock(monkeypatch, to_ns("2040-01-01"))
    with pytest.raises(SealedBeforeL7):  # no canonical L7 in this repository: sealed whatever the clock
        s.read(rec.sha256)
    with pytest.raises(SealedBeforeL7):
        list(s.iter_records())
    c = ForwardStore(tmp_path / "fs", repo=canon)  # the canonical source injected like repo / home
    for now in (TF7 + 180 * D, R_V6, L7 - 1):
        af.clock(monkeypatch, now)
        with pytest.raises(SealedBeforeL7):
            c.read(rec.sha256)
        with pytest.raises(SealedBeforeL7):
            list(c.iter_records())
    af.clock(monkeypatch, L7)
    assert c.read(rec.sha256) == b"abc" and [r.sha256 for r in c.iter_records()] == [rec.sha256]


@pytest.mark.parametrize("variant", ["record_hash", "evidence_hash", "lightweight_tag", "basis_a_too_early",
                                     "basis_b_before_first_look", "unknown_basis", "unknown_status"])
def test_unverified_release_record_is_absent(tmp_path, variant):
    kw = {"record_hash": dict(tamper_record=True), "evidence_hash": dict(tamper_evidence=True),
          "lightweight_tag": dict(annotated=False),
          "basis_a_too_early": dict(r_v6="2027-10-13T23:00:00Z"),
          "basis_b_before_first_look": dict(r_v6="2027-01-11T00:00:00Z", basis="b"),
          "unknown_basis": dict(basis="d"), "unknown_status": dict(status="pending")}[variant]
    r = af.canonical_repo(tmp_path / variant, **kw)
    assert release.rv6_release(r) is None and release.r_v6(r) is None
    assert windows.m6(r) == (M6_START, None) and forward.l7(r) is None
    assert release.t_f7(r) == TF7  # the review record is independent and still verifies


def test_unverified_review_record_is_absent(tmp_path, monkeypatch):
    r = af.release_repo(tmp_path / "r")
    af.add_release(r, "2027-10-14T00:00:00Z")
    af.add_review(r, "2026-10-15T00:00:00Z", tamper_record=True)
    release.cache_clear()
    assert release.r_v6(r) == R_V6 and release.t_f7(r) is None and forward.l7(r) is None
    af.clock(monkeypatch, to_ns("2040-01-01"))
    with pytest.raises(SealedBeforeL7):
        ForwardStore(tmp_path / "fs", repo=r).read("a" * 64)


def test_netguard_takes_no_caller_span(canon):
    """Red-team M5 probe: ``drop_m6_keys([...2026-08.zip], (m6_start, m6_start + 1))`` kept the M6 key."""
    keys = [OLD_KEY, M6_KEY, LATE_KEY]
    with pytest.raises(TypeError):
        netguard.drop_m6_keys(keys, (M6_START, M6_START + 1))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        netguard.partition_m6_keys(keys, m6_span=(M6_START, M6_START + 1))  # type: ignore[call-arg]
    assert netguard.drop_m6_keys(keys) == [OLD_KEY]  # canonical span: R_V6 undetermined, open-ended
    kept, dropped, bad = netguard.partition_m6_keys(keys, repo=canon)
    assert kept == [OLD_KEY, LATE_KEY] and dropped == [M6_KEY] and bad == []


def test_outcome_guard_releases_m6_only_by_record_and_trusted_clock(monkeypatch, canon):
    lo, hi = M6_START + D, M6_START + 2 * D  # inside M6, before T_F7
    af.clock(monkeypatch, R_V6 - 1)
    with pytest.raises(guard.EmbargoBlocked):  # a caller date cannot lift M6 before the trusted clock
        guard.check_outcome_window(lo, hi, now_ns=to_ns("2040-01-01"), repo=canon)
    af.clock(monkeypatch, R_V6)
    guard.check_outcome_window(lo, hi, now_ns=R_V6, repo=canon)
    with pytest.raises(guard.EmbargoBlocked):  # the caller's own clock must have reached R_V6 as well
        guard.check_outcome_window(lo, hi, now_ns=R_V6 - 1, repo=canon)
    with pytest.raises(guard.EmbargoBlocked, match="canonical"):  # a span is still only a cross-check
        guard.check_outcome_window(lo, hi, now_ns=R_V6, repo=canon, m6_span=(M6_START, None))
    guard.check_outcome_window(lo, hi, now_ns=R_V6, repo=canon, m6_span=(M6_START, R_V6))


def test_w_fwd7_replay_runnable_only_at_l7(tmp_path, monkeypatch, canon):
    """The engine guard (``check_outcome_window``, called once by ``book.engine.run``) refuses a W_FWD7
    outcome window until L7, then the forward replay is runnable (no longer structurally refused)."""
    win = forward.w_fwd7(TF7)
    ext = (win[1], forward.w_fwd7(TF7, extended=True)[1])
    af.clock(monkeypatch, R_V6 + H)  # M6 released, L7 not reached
    with pytest.raises(SealedBeforeL7):
        guard.check_outcome_window(*win, now_ns=R_V6 + H, repo=canon)
    af.clock(monkeypatch, L7)
    with pytest.raises(SealedBeforeL7):  # the caller's clock too
        guard.check_outcome_window(*win, now_ns=L7 - 1, repo=canon)
    guard.check_outcome_window(*win, now_ns=L7, repo=canon)
    guard.check_outcome_window(*ext, now_ns=L7, repo=canon)  # extended L7 = max(T_F7 + 270 d, R_V6) + 1 d
    early = af.canonical_repo(tmp_path / "early", r_v6="2027-01-01T00:00:00Z", basis="c",
                              status="released_in_writing")
    af.clock(monkeypatch, TF7 + 181 * D)
    guard.check_outcome_window(*win, now_ns=TF7 + 181 * D, repo=early)
    with pytest.raises(SealedBeforeL7):  # the extension window waits for the extended look
        guard.check_outcome_window(*ext, now_ns=TF7 + 181 * D, repo=early)
    af.clock(monkeypatch, TF7 + 271 * D)
    guard.check_outcome_window(*ext, now_ns=TF7 + 271 * D, repo=early)

