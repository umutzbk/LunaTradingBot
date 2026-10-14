"""Forward protocol (spec §15, DEC-10, DEC-42, EC-11, K11) on synthetic inputs."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import forward, stats, verdict
from luna.v7.accounting import account
from luna.v7.book.execution import INVALID_TERMINAL
from luna.v7.forward import (
    ForwardInputs,
    TerminalHit,
    a0_availability,
    decision_time,
    evaluable_day,
    evaluable_quarter,
    extension_allowed,
    forward_evaluate,
    forward_verdict,
    l7,
    t_f7,
)
from luna.v7.forward_store import SealedBeforeL7
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.windows import m6_cached

from .run_fixtures import funding_run

TF7 = to_ns("2026-10-16")
R_V6 = to_ns("2027-10-14")
NOW = to_ns("2027-10-20")
B = 1000
GOOD = [1e-3, 2e-3] * 90  # 180 days, clearly positive
P4_OK = dict(active_days=30, closed_legs=50, clusters=10)
ALL = {k: True for k in forward.PRIMARY_INPUTS}


def run(nets=GOOD, arm="A2", **kw):
    return funding_run(nets, arm=arm, lo=TF7, **kw)


def inputs(**kw):
    base = dict(holdout_class=verdict.ADVANCE, t_f7=TF7, r_v6=R_V6, now_ns=NOW, ml=run(), evaluable_days=170,
                integrity=(), outcome_before_l7=(), unrecovered_funding=(), terminal_events=(),
                a0=run([5e-4] * 180, arm="A0"),
                B=B)
    base.update(kw)
    return ForwardInputs(**base)


@pytest.fixture
def p4_ok(monkeypatch):
    """The synthetic funding books hold no legs: the P4 counts are injected (``p4_counts``: tested below)."""
    monkeypatch.setattr(forward, "p4_counts", lambda run, led: dict(P4_OK))


def test_t_f7_rule():
    assert t_f7(to_ns("2026-10-20 13:00")) == to_ns("2026-10-22")
    assert t_f7(to_ns("2026-10-20 00:00")) == to_ns("2026-10-21")  # exactly 24 h later is >= 24 h
    assert t_f7(to_ns("2026-10-20 00:00") + 1) == to_ns("2026-10-22")
    assert t_f7(to_ns("2026-10-20 23:59:59")) == to_ns("2026-10-22")
    assert forward.w_fwd7(TF7) == (TF7, TF7 + 180 * D)
    with pytest.raises(ValueError):
        forward.w_fwd7(TF7 + H)


def test_l7_strict_default():
    m6_lo, m6_hi = m6_cached()
    assert m6_hi is None and m6_lo <= TF7  # T_F7 inside M6, R_V6 undetermined
    strict = forward.r_v6_strict_default()
    assert strict == to_ns("2027-10-14")  # V6 day-365 look (S-1.1 table), S-1.2 (a)
    assert l7(TF7, strict) == to_ns("2027-10-15") and l7(TF7, strict) >= to_ns("2027-10-14")
    assert l7(TF7, None) is None  # R_V6 unknown: no look
    early = to_ns("2027-01-01")  # e.g. a written release (c) before the forward end
    assert l7(TF7, early) == TF7 + 181 * D
    assert l7(TF7, early, extended=True) == TF7 + 271 * D
    assert forward.FORWARD_BLOCKS["T_cal"] == (to_ns("2026-01-01"), to_ns("2026-07-01"))


def test_no_outcome_before_l7(p4_ok):
    with pytest.raises(SealedBeforeL7):
        forward_evaluate(inputs(now_ns=to_ns("2027-10-14")))  # L7 = 2027-10-15
    with pytest.raises(SealedBeforeL7):
        forward_evaluate(inputs(r_v6=None, now_ns=to_ns("2030-01-01")))
    with pytest.raises(SealedBeforeL7):
        forward.require_l7(NOW, None)
    assert forward_verdict(inputs(now_ns=to_ns("2027-10-15"))) == forward.GO_NOT_DEPLOYABLE
    v = forward_evaluate(inputs(outcome_before_l7=("P&L displayed 2027-03-01",)))
    assert v.cls == forward.INVALID and "outcome computed before L7" in v.reasons[0]
    with pytest.raises(forward.NoForwardTest):
        forward_evaluate(inputs(holdout_class=verdict.NOGO))


def test_evaluable_ignores_diagnostic_feeds():
    assert evaluable_quarter({**ALL, "book_top": False, "depth": False, "ticker": False})
    assert evaluable_quarter(ALL)  # diagnostic feeds absent
    for k in forward.PRIMARY_INPUTS:
        assert not evaluable_quarter({**ALL, k: False})
        assert not evaluable_quarter({x: v for x, v in ALL.items() if x != k})  # missing = not present
    assert not evaluable_quarter({**ALL, "funding": None})  # type: ignore[dict-item]
    # the exchange-filter snapshot only matters for D1-D3
    assert evaluable_quarter({**ALL, "exchange_filters": False})
    assert not evaluable_quarter({**ALL, "exchange_filters": False}, deployment=True)
    assert evaluable_quarter({**ALL, "exchange_filters": True}, deployment=True)
    with pytest.raises(ValueError):
        evaluable_quarter({**ALL, "orderbook_l2": True})
    assert evaluable_day([True] * 87 + [False] * 9) and not evaluable_day([True] * 86 + [False] * 10)
    with pytest.raises(ValueError):
        evaluable_day([True] * 95)


def test_gaps_and_day_count():
    win = forward.w_fwd7(TF7)
    assert forward.gap_days([(TF7 + 2 * D, TF7 + 3 * D)], win) == set()  # exactly 24 h: not > 24 h
    assert forward.gap_days([(TF7 + 2 * D + H, TF7 + 3 * D + 2 * H)], win) == {2, 3}
    days = [[True] * 96] * 180
    days[10] = [True] * 80 + [False] * 16
    assert forward.count_evaluable_days(days, win) == 179
    assert forward.count_evaluable_days(days, win, [(TF7 + 2 * D + H, TF7 + 3 * D + 2 * H)]) == 177


def test_decision_time_shift_and_skip():
    t = TF7 + 8 * H + 5 * MIN
    bad = {TF7 + 8 * H, TF7 + 8 * H + 15 * MIN}
    assert decision_time(t, lambda q: True) == t
    assert decision_time(t, lambda q: q not in bad) == t + 30 * MIN
    last_ok = TF7 + 10 * H  # t's quarter + 2 h
    assert decision_time(t, lambda q: q >= last_ok) == t + 2 * H
    assert decision_time(t, lambda q: q > last_ok) is None  # skipped: no entries
    assert forward.skipped_exit_time(t, lambda q: q > last_ok, TF7 + D) == t + 2 * H + 15 * MIN
    assert forward.skipped_exit_time(t, lambda q: False, TF7 + D) is None


def test_inconclusive_below_144_days(p4_ok):
    v = forward_evaluate(inputs(evaluable_days=144))
    assert v.cls == forward.GO_NOT_DEPLOYABLE and all(v.criteria.values())
    v = forward_evaluate(inputs(evaluable_days=143))
    assert v.cls == forward.INCONCLUSIVE and v.evidence_missing == ("coverage",) and v.extension_allowed
    assert forward.evidence(200, dict(P4_OK, closed_legs=49)) == ("p4",)
    assert forward.evidence(143, dict(P4_OK, clusters=9)) == ("coverage", "p4")


def test_p4_counts_on_book():
    r = run()
    assert forward.p4_counts(r, account(r)) == dict(active_days=0, closed_legs=0, clusters=0)
    v = forward_evaluate(inputs())  # no monkeypatch: P4 fails -> INCONCLUSIVE, not by coverage alone
    assert v.cls == forward.INCONCLUSIVE and v.evidence_missing == ("p4",) and not v.extension_allowed


def test_nogo_outranks_inconclusive(p4_ok):
    flat = list(np.random.default_rng(7).normal(-2e-4, 1e-3, 180))  # no edge: H_F fails
    v = forward_evaluate(inputs(ml=run(flat), evaluable_days=100))
    assert v.cls == forward.NOGO and not v.criteria["H_F"] and not v.extension_allowed
    dd = forward_evaluate(inputs(ml=run(decisions=pd.DataFrame(dict(t=[TF7], killed=[True])))))
    assert dd.cls == forward.NOGO and not dd.criteria["P3"]  # the replay hit the drawdown kill


def test_go_needs_forward_pit_deployment(p4_ok):
    ok = dict(exact=True, D1=True, D2=True, D3=True)
    assert forward_verdict(inputs(deploy=ok, dec14_verified=True)) == forward.GO
    assert forward_verdict(inputs(deploy=ok)) == forward.GO_NOT_DEPLOYABLE  # DEC-14 not verified
    for bad in (dict(ok, exact=False), dict(ok, D1=False), dict(ok, D3=False)):
        assert forward_verdict(inputs(deploy=bad, dec14_verified=True)) == forward.GO_NOT_DEPLOYABLE


def test_ec11_a0_event_does_not_invalidate(p4_ok):
    ev = [TerminalHit(TF7 + 50 * D, "A0", "B", True), TerminalHit(TF7 + 10 * D, "A2", "C", False)]
    assert a0_availability(ev) == (TF7 + 50 * D, "unavailable/incomplete")
    assert a0_availability([TerminalHit(TF7, "A0", "C", False), TerminalHit(TF7, "A0", "A", True)]) == (
        None, "complete")
    v = forward_evaluate(inputs(terminal_events=ev))
    assert v.cls == forward.GO_NOT_DEPLOYABLE and v.a0_unavailable_from == TF7 + 50 * D
    assert v.comparison == "unavailable/incomplete" and "ml_minus_a0_mean" not in v.details
    # an A0 replay stopped by its own B / C event: same
    a0_bad = replace(run([5e-4] * 180, arm="A0"), invalid=INVALID_TERMINAL, invalid_ts=TF7 + 20 * D,
                     invalid_detail="unparsed text")
    v2 = forward_evaluate(inputs(a0=a0_bad))
    assert v2.cls == forward.GO_NOT_DEPLOYABLE and v2.a0_unavailable_from == TF7 + 20 * D
    v2b = forward_evaluate(inputs(a0=a0_bad, terminal_events=[TerminalHit(TF7 + 9 * D, "A0", "C", True)]))
    assert v2b.a0_unavailable_from == TF7 + 9 * D  # the earlier of the two
    # no event: the ML* - A0 comparison is reported (never a gate)
    v3 = forward_evaluate(inputs())
    _, xd = stats.paired(account(run()), account(run([5e-4] * 180, arm="A0")))
    assert v3.comparison == "complete" and v3.details["ml_minus_a0_mean"] == float(np.mean(xd)) > 0


def test_ml_event_invalidates(p4_ok):
    v = forward_evaluate(inputs(terminal_events=[TerminalHit(TF7 + 5 * D, "A2", "B", True)]))
    assert v.cls == forward.INVALID and INVALID_TERMINAL in v.reasons[0]
    # the non-selected ML family (A3 while ML* = A2) and A1 are not ML*: no INVALID RUN
    other = [TerminalHit(TF7 + 5 * D, "A3", "B", True), TerminalHit(TF7 + 6 * D, "A1", "C", True)]
    assert forward_verdict(inputs(terminal_events=other)) == forward.GO_NOT_DEPLOYABLE
    assert forward_verdict(inputs(ml=run(arm="A3"), terminal_events=other)) == forward.INVALID
    with pytest.raises(ValueError):
        TerminalHit(TF7, "ML*", "B", True)  # unknown arm id refused
    with pytest.raises(TypeError):  # terminal_events is a required input
        ForwardInputs(verdict.ADVANCE, TF7, R_V6, NOW, run(), 170, (), (), ())  # type: ignore[call-arg]
    bad = replace(run(), invalid=INVALID_TERMINAL, invalid_detail="X#0@1")
    assert forward_verdict(inputs(ml=bad)) == forward.INVALID
    assert forward_verdict(inputs(unrecovered_funding=(TF7 + 8 * H,))) == forward.INVALID
    assert forward_verdict(inputs(integrity=("hash mismatch",))) == forward.INVALID
    assert forward.unrecovered_funding([1, 2, 3], {1: "a" * 64, 2: "nothash"}) == [2, 3]


def test_single_extension(p4_ok):
    assert extension_allowed("coverage")
    assert not extension_allowed("coverage", already_extended=True)
    assert not extension_allowed(("coverage", "p4")) and not extension_allowed("p4")
    # the extended look: same rules over [T_F7, T_F7 + 270 d), single look at the extended end
    ext = run(GOOD + [1e-3] * 90)
    with pytest.raises(SealedBeforeL7):
        forward_evaluate(inputs(ml=ext, extended=True, r_v6=to_ns("2027-01-01"), now_ns=TF7 + 270 * D))
    v = forward_evaluate(inputs(ml=ext, a0=None, extended=True, r_v6=to_ns("2027-01-01"),
                                now_ns=TF7 + 271 * D, evaluable_days=100))
    assert v.cls == forward.INCONCLUSIVE and not v.extension_allowed  # no second extension
    with pytest.raises(ValueError):  # the replay window must be W_FWD7
        forward_evaluate(inputs(ml=ext))
