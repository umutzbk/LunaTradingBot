"""Task 25 (SC-03, red-team M3 (b)): the W_HOLD / M6 guard on the decision views, the features and the label /
book entry points of the runner. Before the opening a W_HOLD decision time or an input frame with a W_HOLD row
is refused (K9-logged); a forged authorization does not help; W_DEV is unaffected; after the opening the
hardened authorization lets W_HOLD through while M6 stays refused."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from luna.v7 import guard, k9, runner, windows
from luna.v7.book import engine
from luna.v7.data.instances import Instance
from luna.v7.data.notices import NoticeVersion
from luna.v7.data.terminal import TerminalEvent
from luna.v7.runner import DecisionProvider, ResearchUniverse
from luna.v7.timeutil import MIN, D
from tests.v7 import auth_fixtures as af
from tests.v7 import fixtures_v7 as fx

BTC = fx.BTC
SUB = (BTC, "AAAUSDT", "EEEUSDT")
T_DEV = fx.BOOK[0] + D + 5 * MIN
T_HOLD = windows.W_HOLD[0] + 5 * MIN
T_M6 = windows.W_HOLD[1] + 5 * MIN
NOW = fx.NOW


@pytest.fixture(scope="module")
def inputs() -> dict[str, runner.SymbolInputs]:
    return fx.build_inputs(7, dense=(fx.BOOK,))


@pytest.fixture
def log(tmp_path):
    lg = k9.K9Log(tmp_path / "k9")
    with k9.using(lg):
        yield lg


@pytest.fixture(scope="module")
def _opening(tmp_path_factory):
    return af.opening_once(tmp_path_factory.mktemp("opening"))


@pytest.fixture
def opened(_opening, monkeypatch):
    """The module's opening, ``guard._roots`` pointed at it for this test only (review m-7)."""
    return af.use_opening(monkeypatch, _opening)


def uni(inputs, **repl) -> ResearchUniverse:
    return fx.ledgered(ResearchUniverse({s: (replace(inputs[s], **repl[s]) if s in repl else inputs[s])
                                         for s in SUB}))


def with_row_at(df: pd.DataFrame, col: str, ts: int) -> pd.DataFrame:
    """``df`` plus one copy of its last row moved to ``ts`` (``known_at`` one minute later)."""
    row = df.iloc[[-1]].copy()
    row[col] = ts
    if "known_at" in row.columns:
        row["known_at"] = ts + MIN
    return pd.concat([df, row], ignore_index=True)


def test_w_dev_decision_is_unaffected(inputs, log):
    u = uni(inputs)
    assert u.data_span()[1] <= windows.W_HOLD[0]
    v = DecisionProvider(u, with_features=True)(T_DEV)
    assert v.t == T_DEV and len(v.cands) > 0
    assert log.records() == []


def test_w_hold_decision_is_refused_before_the_opening(inputs, log):
    p = DecisionProvider(uni(inputs), with_features=True)
    for t in (T_HOLD, windows.W_HOLD[1] - 1):
        with pytest.raises(guard.HoldoutBlocked):
            p(t)
        with pytest.raises(guard.HoldoutBlocked):
            p.record(t)
    with pytest.raises(guard.EmbargoBlocked):  # reaching M6
        p(T_M6)
    recs = log.records()
    assert recs and recs[0]["request"] == {"kind": "data_span", "what": "decision t", "lo": T_HOLD,
                                           "hi": T_HOLD + 1}
    assert p(T_DEV).t == T_DEV  # W_DEV still served by the same provider


@pytest.mark.parametrize("field,col", [("bars15", "open_ts"), ("bars1m", "open_ts"),
                                       ("funding", "funding_ts")])
def test_input_frame_with_a_w_hold_row_is_refused(inputs, log, field, col):
    x = inputs["AAAUSDT"]
    u = uni(inputs, AAAUSDT={field: with_row_at(getattr(x, field), col, windows.W_HOLD[0])})
    assert u.data_span()[1] == windows.W_HOLD[0] + 1
    with pytest.raises(guard.HoldoutBlocked):
        DecisionProvider(u, with_features=True)
    p0 = DecisionProvider(uni(inputs), with_features=True)
    with pytest.raises(guard.HoldoutBlocked):  # label / book entry points check the universe too
        runner.label_panel(u, p0, [T_DEV], 24, windows.W_DEV, NOW)
    with pytest.raises(guard.HoldoutBlocked):
        runner.run_book("A0", u, p0, (T_DEV - 5 * MIN, T_DEV + D), 168, NOW)
    assert {r["request"]["what"] for r in log.records()} == {"research universe"}


def test_r4_and_aggtrades_rows_count_too(inputs, log):
    x = inputs["EEEUSDT"]
    r4 = x.r4.__class__(x.r4.premium, with_row_at(x.r4.mark, "open_ts", T_M6), x.r4.index)
    with pytest.raises(guard.EmbargoBlocked):
        DecisionProvider(uni(inputs, EEEUSDT={"r4": r4}))
    agg = pd.DataFrame(dict(ts=[windows.W_HOLD[0]], price=[1.0], qty=[1.0]))
    with pytest.raises(guard.HoldoutBlocked):
        DecisionProvider(uni(inputs, EEEUSDT={"aggtrades": agg}))


def test_forged_authorization_does_not_help(inputs, log):
    u = uni(inputs)
    for forged in (object(), object.__new__(guard.HoldoutAuthorization), True):
        p = DecisionProvider(u, with_features=True, auth=forged)  # type: ignore[arg-type]
        with pytest.raises(guard.HoldoutBlocked):
            p(T_HOLD)
    bad = uni(inputs, AAAUSDT={"bars15": with_row_at(inputs["AAAUSDT"].bars15, "open_ts", T_HOLD)})
    with pytest.raises(guard.HoldoutBlocked):
        DecisionProvider(bad, auth=object())  # type: ignore[arg-type]


def test_after_the_opening_w_hold_passes_and_m6_stays_refused(inputs, opened, log):
    u = uni(inputs, AAAUSDT={"bars15": with_row_at(inputs["AAAUSDT"].bars15, "open_ts", T_HOLD)})
    p = DecisionProvider(u, with_features=True, auth=opened.auth)
    assert p(T_HOLD).t == T_HOLD  # the guard lets the W_HOLD decision through (no data there: no candidates)
    assert p(T_DEV).t == T_DEV
    with pytest.raises(guard.EmbargoBlocked):
        p(T_M6)
    m6 = uni(inputs, AAAUSDT={"bars15": with_row_at(inputs["AAAUSDT"].bars15, "open_ts", T_M6)})
    with pytest.raises(guard.EmbargoBlocked):
        DecisionProvider(m6, auth=opened.auth)
    assert [r["exception"] for r in log.records()] == ["EmbargoBlocked", "EmbargoBlocked"]


def test_cached_view_is_re_guarded(inputs, log, monkeypatch):
    """A view computed while the guard allowed it is not served once the guard refuses (e.g. after the
    verdict closed the opening): every call is checked, cached or not."""
    p = DecisionProvider(uni(inputs), with_features=False)
    p(T_DEV)
    calls = []
    real = guard.check_data_span

    def spy(lo, hi, **kw):
        calls.append((lo, hi, kw.get("what")))
        return real(lo, hi, **kw)

    monkeypatch.setattr(guard, "check_data_span", spy)
    p(T_DEV)
    assert (T_DEV, T_DEV + 1, "decision t") in calls


# ------------------------------------------------------------------ Task 25 review round 1 (I-2, I-3, m-2..4)


def _notice(ts: int, capture: int | None = None, recv: int | None = None) -> NoticeVersion:
    return NoticeVersion("n1", 1, "delisting", ts, capture, "AAAUSDT will be delisted", "0" * 64, recv)


@pytest.mark.parametrize("field", ["notice release", "notice capture", "notice recv", "terminal cessation",
                                   "terminal settle", "instance last trade", "instance end known"])
def test_non_frame_inputs_in_w_hold_are_refused(inputs, log, field):
    """Frames pre-sliced to W_DEV; one notice / terminal event / instance time in W_HOLD is refused."""
    x = inputs["AAAUSDT"]
    dev, hold = T_DEV - D, windows.W_HOLD[0] + D
    inst = x.insts[-1]
    repl = {
        "notice release": {"notices": (_notice(hold),)},
        "notice capture": {"notices": (_notice(dev, capture=hold),)},
        "notice recv": {"notices": (_notice(dev, recv=hold),)},
        "terminal cessation": {"terminals": {inst.idx: TerminalEvent(inst, hold, "B", None, None, None)}},
        "terminal settle": {"terminals": {inst.idx: TerminalEvent(inst, dev, "A", hold, 1.0, "x")}},
        "instance last trade": {"insts": [*x.insts[:-1], Instance(inst.idx, inst.first_bar_ts, hold, None)]},
        "instance end known": {"insts": [*x.insts[:-1], Instance(inst.idx, inst.first_bar_ts, dev, hold)]},
    }[field]
    u = uni(inputs, AAAUSDT=repl)
    assert u.data_span()[1] == hold + 1
    with pytest.raises(guard.HoldoutBlocked):
        DecisionProvider(u, with_features=False)
    DecisionProvider(uni(inputs, AAAUSDT={"notices": (_notice(dev),)}), with_features=False)  # W_DEV: fine
    assert [r["request"]["what"] for r in log.records()] == ["research universe"]


def test_public_per_t_methods_are_guarded(inputs, log):
    p = DecisionProvider(uni(inputs), with_features=False)
    for call in (lambda t: p.instance_bars("AAAUSDT", t), lambda t: p.h_hat("AAAUSDT", t),
                 lambda t: p.beta("AAAUSDT", t), lambda t: p.qv24("AAAUSDT", t), lambda t: p.stale_reasons(t),
                 lambda t: p.xs(t)):
        call(T_DEV)
        with pytest.raises(guard.HoldoutBlocked):
            call(T_HOLD)
    assert len(log.records()) == 6


def test_universe_span_is_recomputed_on_every_check(inputs, log):
    """Review m-4: an input mutated after construction is caught by the next ``check_data`` (label_panel,
    run_book, engine_inputs)."""
    x = inputs["AAAUSDT"]
    b15 = x.bars15.copy()
    u = uni(inputs, AAAUSDT={"bars15": b15})
    p = DecisionProvider(u, with_features=True)
    b15.loc[b15.index[-1], "open_ts"] = T_HOLD  # in-place mutation after construction
    with pytest.raises(guard.HoldoutBlocked):
        u.check_data(None)
    with pytest.raises(guard.HoldoutBlocked):
        u.engine_inputs(p, NOW)
    with pytest.raises(guard.HoldoutBlocked):
        runner.label_panel(u, p, [T_DEV], 24, windows.W_DEV, NOW)


def test_research_entry_points_refuse_to_run_without_a_k9_log(inputs):
    """Review I-3: fail closed without a configured K9 log, even for W_DEV."""
    u = uni(inputs)
    p = DecisionProvider(u, with_features=True)
    with k9.using(None):
        for call in (lambda: DecisionProvider(u, with_features=True),
                     lambda: runner.run_book("A0", u, p, (T_DEV - 5 * MIN, T_DEV + D), 168, NOW),
                     lambda: runner.label_panel(u, p, [T_DEV], 24, windows.W_DEV, NOW),
                     lambda: u.engine_inputs(p, NOW),
                     lambda: engine.run("A0", object(), (T_DEV, T_DEV + D), 168),  # type: ignore[arg-type]
                     lambda: p(T_DEV)):
            with pytest.raises(k9.K9NotConfigured):
                call()
