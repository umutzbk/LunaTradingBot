"""SC-06 (red-team M5): one canonical, hash-checked R_V6 / T_F7 source shared by ``windows.m6``,
``forward.l7``, the forward store, ``netguard`` and the outcome guard (spec S-1.2, S-1.3, S-1.7 M3-3 / N2,
DEC-45, §15, L10; AMB-34). Synthetic git fixtures only (``auth_fixtures``); the trusted clock is pinned by
monkeypatching ``release.trusted_now_ns`` (no API takes a date)."""

from __future__ import annotations

import inspect
from unittest import mock

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


def _at(repo):
    """The guards' single location seam (``guard._roots``) pointed at ``repo`` (no API takes a repository:
    round 3 m-c, red-team r2 MJ5)."""
    return mock.patch.object(guard, "_roots", lambda: (repo, repo.parent / "home"))


def _cow(repo, *args, **kw):
    """``check_outcome_window`` with the guard's location seam at ``repo`` (round 3 m-c: no caller repo)."""
    with _at(repo):
        guard.check_outcome_window(*args, **kw)


@pytest.fixture
def canon(tmp_path):
    return af.canonical_repo(tmp_path / "canon")


def test_without_a_record_m6_is_open_and_l7_undefined(tmp_path, monkeypatch):
    r = af.release_repo(tmp_path / "bare")
    release.cache_clear()
    with _at(r):
        assert windows.m6() == (M6_START, None) and windows.m6_cached() == (M6_START, None)
        assert release.r_v6(r) is None and release.t_f7(r) is None and forward.l7() is None
    af.clock(monkeypatch, to_ns("2040-01-01"))
    with pytest.raises(guard.EmbargoBlocked):
        _cow(r, M6_START, M6_START + D, now_ns=to_ns("2040-01-01"))
    with _at(r), pytest.raises(SealedBeforeL7):
        forward.require_l7()
    # the real repository has no release record either
    assert release.r_v6() is None and forward.l7() is None


def test_canonical_record_ends_m6_and_derives_l7(canon):
    assert release.r_v6(canon) == R_V6 and release.t_f7(canon) == TF7
    with _at(canon):
        assert windows.m6() == (M6_START, R_V6) == windows.m6_cached()
        assert forward.l7() == L7 and forward.l7(extended=True) == L7  # R_V6 dominates
    rel = release.rv6_release(canon)
    assert rel.basis == "a" and len(rel.evidence_sha256) == 64 and len(rel.record_sha256) == 64


def test_early_written_release_and_extension(tmp_path):
    r = af.canonical_repo(tmp_path / "c", r_v6="2027-01-01T00:00:00Z", basis="c",
                          status="released_in_writing")
    assert release.r_v6(r) == to_ns("2027-01-01")
    with _at(r):
        assert forward.l7() == TF7 + 181 * D and forward.l7(extended=True) == TF7 + 271 * D


def test_forward_l7_cannot_be_fed_a_guessed_r_v6(canon):
    params = inspect.signature(forward.l7).parameters
    assert "r_v6" not in params and "t_f7_ns" not in params and "repo" not in params
    with pytest.raises(TypeError):
        forward.l7(TF7, R_V6)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        forward.l7(canon, r_v6=to_ns("2026-11-01"))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        forward.l7(canon)  # type: ignore[call-arg]  (red-team r2 MJ5: no caller repository)
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
    c = ForwardStore(tmp_path / "fs")
    with _at(canon):  # the canonical source: only through the guards' location seam
        for now in (TF7 + 180 * D, R_V6, L7 - 1):
            af.clock(monkeypatch, now)
            with pytest.raises(SealedBeforeL7):
                c.read(rec.sha256)
            with pytest.raises(SealedBeforeL7):
                list(c.iter_records())
        af.clock(monkeypatch, L7)
        assert c.read(rec.sha256) == b"abc" and [r.sha256 for r in c.iter_records()] == [rec.sha256]


@pytest.mark.parametrize("variant", ["record_hash", "evidence_hash", "holdout_doc_hash", "lightweight_tag",
                                     "basis_a_too_early", "basis_b_before_first_look", "basis_c_before_m6",
                                     "unknown_basis", "unknown_status", "before_written_release",
                                     "before_holdout_report", "before_evidence"])
def test_unverified_release_record_is_absent(tmp_path, variant):
    kw = {"record_hash": dict(tamper_record=True), "evidence_hash": dict(tamper_evidence=True),
          "holdout_doc_hash": dict(tamper_holdout=True),  # review m6
          "lightweight_tag": dict(annotated=False),
          "basis_a_too_early": dict(r_v6="2027-10-13T23:00:00Z"),
          "basis_b_before_first_look": dict(r_v6="2027-01-11T00:00:00Z", basis="b"),
          "basis_c_before_m6": dict(r_v6="2026-06-30T00:00:00Z", basis="c", status="released_in_writing"),
          "unknown_basis": dict(basis="d"), "unknown_status": dict(status="pending"),
          # review m3: R_V6 also requires the V6-holdout event (S-1.2): never before its documented time
          "before_written_release": dict(r_v6="2027-01-01T00:00:00Z", basis="c", status="released_in_writing",
                                         holdout_at="2027-02-01T00:00:00Z"),
          "before_holdout_report": dict(holdout_at="2027-10-20T00:00:00Z"),
          "before_evidence": dict(evidence_at="2027-10-15T00:00:00Z")}[variant]
    r = af.canonical_repo(tmp_path / variant, **kw)
    assert release.rv6_release(r) is None and release.r_v6(r) is None
    with _at(r):
        assert windows.m6() == (M6_START, None) and forward.l7() is None
    assert release.t_f7(r) == TF7  # the review record is independent and still verifies


def test_unverified_review_record_is_absent(tmp_path, monkeypatch):
    r = af.release_repo(tmp_path / "r")
    af.add_release(r, "2027-10-14T00:00:00Z")
    af.add_review(r, "2026-10-15T00:00:00Z", tamper_record=True)
    release.cache_clear()
    assert release.r_v6(r) == R_V6 and release.t_f7(r) is None
    af.clock(monkeypatch, to_ns("2040-01-01"))
    with _at(r):
        assert forward.l7() is None
        with pytest.raises(SealedBeforeL7):
            ForwardStore(tmp_path / "fs").read("a" * 64)


def test_netguard_takes_no_caller_span(canon):
    """Red-team M5 probe: ``drop_m6_keys([...2026-08.zip], (m6_start, m6_start + 1))`` kept the M6 key."""
    keys = [OLD_KEY, M6_KEY, LATE_KEY]
    with pytest.raises(TypeError):
        netguard.drop_m6_keys(keys, (M6_START, M6_START + 1))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        netguard.partition_m6_keys(keys, m6_span=(M6_START, M6_START + 1))  # type: ignore[call-arg]
    assert netguard.drop_m6_keys(keys) == [OLD_KEY]  # canonical span: R_V6 undetermined, open-ended
    with _at(canon):
        kept, dropped, bad = netguard.partition_m6_keys(keys)
    assert kept == [OLD_KEY, LATE_KEY] and dropped == [M6_KEY] and bad == []


def test_outcome_guard_releases_m6_only_by_record_and_trusted_clock(monkeypatch, canon):
    lo, hi = M6_START + D, M6_START + 2 * D  # inside M6, before T_F7
    af.clock(monkeypatch, R_V6 - 1)
    with pytest.raises(guard.EmbargoBlocked):  # a caller date cannot lift M6 before the trusted clock
        _cow(canon, lo, hi, now_ns=to_ns("2040-01-01"))
    af.clock(monkeypatch, R_V6)
    _cow(canon, lo, hi, now_ns=R_V6)
    with pytest.raises(guard.EmbargoBlocked):  # the caller's own clock must have reached R_V6 as well
        _cow(canon, lo, hi, now_ns=R_V6 - 1)
    with pytest.raises(guard.EmbargoBlocked, match="canonical"):  # a span is still only a cross-check
        _cow(canon, lo, hi, now_ns=R_V6, m6_span=(M6_START, None))
    _cow(canon, lo, hi, now_ns=R_V6, m6_span=(M6_START, R_V6))


def test_w_fwd7_replay_runnable_only_at_l7(tmp_path, monkeypatch, canon):
    """The engine guard (``check_outcome_window``, called once by ``book.engine.run``) refuses a W_FWD7
    outcome window until L7, then the forward replay is runnable (no longer structurally refused)."""
    win = forward.w_fwd7(TF7)
    ext = (win[1], forward.w_fwd7(TF7, extended=True)[1])
    af.clock(monkeypatch, R_V6 + H)  # M6 released, L7 not reached
    with pytest.raises(SealedBeforeL7):
        _cow(canon, *win, now_ns=R_V6 + H)
    af.clock(monkeypatch, L7)
    with pytest.raises(SealedBeforeL7):  # the caller's clock too
        _cow(canon, *win, now_ns=L7 - 1)
    _cow(canon, *win, now_ns=L7)
    _cow(canon, *ext, now_ns=L7)  # extended L7 = max(T_F7 + 270 d, R_V6) + 1 d
    early = af.canonical_repo(tmp_path / "early", r_v6="2027-01-01T00:00:00Z", basis="c",
                              status="released_in_writing")
    af.clock(monkeypatch, TF7 + 181 * D)
    _cow(early, *win, now_ns=TF7 + 181 * D)
    with pytest.raises(SealedBeforeL7):  # the extension window waits for the extended look
        _cow(early, *ext, now_ns=TF7 + 181 * D)
    af.clock(monkeypatch, TF7 + 271 * D)
    _cow(early, *ext, now_ns=TF7 + 271 * D)



def test_basis_c_floor_and_event_times_accepted(tmp_path):
    """Review m6 / m3: a written release (c) at the V6 holdout start is accepted; R_V6 after the documented
    V6-holdout event and evidence is accepted."""
    r = af.canonical_repo(tmp_path / "c", r_v6="2026-07-01T00:00:00Z", basis="c",
                          status="released_in_writing")
    assert release.r_v6(r) == M6_START
    r2 = af.canonical_repo(tmp_path / "c2", r_v6="2027-03-01T00:00:00Z", basis="c",
                           status="released_in_writing", holdout_at="2027-02-01T00:00:00Z",
                           evidence_at="2027-02-15T00:00:00Z")
    assert release.r_v6(r2) == to_ns("2027-03-01")


def test_extension_records_stay_sealed_until_the_extended_l7(tmp_path, monkeypatch):
    """Review m2: records received at or after T_F7 + 180 d belong to the extension window: sealed until the
    extended L7 even after the base look."""
    r = af.canonical_repo(tmp_path / "early", r_v6="2027-01-01T00:00:00Z", basis="c",
                          status="released_in_writing")
    s = ForwardStore(tmp_path / "fs")
    monkeypatch.setattr(guard, "_roots", lambda: (r, r.parent / "home"))
    a = s.write_raw("u", b"in window", TF7 + H)
    b = s.write_raw("u", b"in extension", TF7 + 180 * D)
    again = s.write_raw("u", b"in window", TF7 + 200 * D)  # the same bytes again in the extension
    af.clock(monkeypatch, TF7 + 181 * D)  # base L7 reached, extended L7 not
    assert s.read(a.sha256) == b"in window" and again.sha256 == a.sha256
    with pytest.raises(SealedBeforeL7):
        s.read(b.sha256)
    assert [(x.sha256, x.recv_ts) for x in s.iter_records()] == [(a.sha256, a.recv_ts)]
    af.clock(monkeypatch, TF7 + 271 * D)
    assert s.read(b.sha256) == b"in extension"
    assert [x.recv_ts for x in s.iter_records()] == [a.recv_ts, b.recv_ts, again.recv_ts]


def test_outcome_guard_takes_no_caller_repo():
    """Round 3 m-c: the M6 / R_V6 / T_F7 / L7 source of the outcome guard is the canonical code repository
    (``guard._roots``), never a caller argument."""
    assert "repo" not in inspect.signature(guard.check_outcome_window).parameters


def test_forged_scratch_repo_cannot_unseal_the_forward_store_or_m6(tmp_path, monkeypatch):
    """Red-team r2 probe P2 (MJ5): a scratch git repository holding valid-looking R_V6 (basis (c), at the V6
    holdout start) and review-pass (2020) records under annotated tags made ``ForwardStore(repo=fake).read``
    return the sealed body and emptied M6 in ``partition_m6_keys(repo=fake)``. No forward / M6 function takes
    a repository any more; the canonical sources (``guard._roots``: the real code repository here) stay
    sealed whatever the trusted clock."""
    fake = af.canonical_repo(tmp_path / "fake", r_v6="2026-07-01T00:00:00Z", basis="c",
                             status="released_in_writing", review_pass="2020-01-01T00:00:00Z")
    assert release.l7(fake) is not None  # the forged records do verify as records of THAT repository
    store = ForwardStore(tmp_path / "fs")
    rec = store.write_raw("https://www.binance.com/x", b"announcement body", TF7 + H)
    for call in (lambda: ForwardStore(tmp_path / "fs", repo=fake), lambda: forward.l7(fake),
                 lambda: forward.require_l7(fake), lambda: forward.r_v6_strict_default(fake),
                 lambda: windows.m6(fake), lambda: windows.m6_cached(fake),
                 lambda: netguard.partition_m6_keys([M6_KEY], repo=fake),
                 lambda: netguard.drop_m6_keys([M6_KEY], repo=fake)):
        with pytest.raises(TypeError):
            call()  # passing a repository is impossible
    for fn in (forward.forward_evaluate, forward.forward_verdict, forward.l7, forward.require_l7,
               windows.m6, windows.m6_cached, netguard.partition_m6_keys, netguard.drop_m6_keys,
               ForwardStore.__init__):
        assert "repo" not in inspect.signature(fn).parameters
    af.clock(monkeypatch, to_ns("2040-01-01"))
    assert forward.l7() is None  # the canonical (real) repository has no release / review records
    with pytest.raises(SealedBeforeL7):
        store.read(rec.sha256)
    with pytest.raises(SealedBeforeL7):
        list(store.iter_records())
    assert netguard.partition_m6_keys([M6_KEY]) == ([], [M6_KEY], [])  # M6 open-ended: the key is dropped
