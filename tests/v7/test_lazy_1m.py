"""Task 29a (red-team r2 MJ1, AMB-38 (h)): lazy, time-chunked 1-minute access for labels and the book engine
gives EXACTLY the results of the resident path.

Compared bit for bit (``test_perf_equivalence.canon``; books by their canonical encoding and output sha256):
* the 1-minute rules (``refs.InstIndex`` / ``refs.ref_price`` / the halted-exit search, ``ExecView``) on
  randomized views, resident frame vs ``lazy1m.Lazy1m``, with a small forward-scan step;
* label panels and books (E_T, E_M) of a lazy universe (``ResearchUniverse.from_source`` with
  ``load_outcome_inputs``) vs the resident one, on the rich Task 26b fixture, on the end-to-end fixture
  (labels, A0 / A2 under E_T and E_M) and from a synthetic raw store whose month chunks split the edge cases:
  a halted exit filled in the first minute of a month chunk and one in the last minute, a 60-minute data-gap
  search across the boundary, HALTED windows spanning it, a book whose terminal unwind is the last minute of
  a chunk and E_M decisions whose halt check spans it; with a tiny R5 cache (evictions), small label blocks
  and a small scan step.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import k9, lazy1m, models, rawstore, refs, runner, windows
from luna.v7.book.execution import (
    INVALID_NO_EXIT,
    ExecView,
    Pending,
    Trade,
    forced_at,
    halted_exit,
    taker_at,
)
from luna.v7.data.instances import Instance
from luna.v7.runrecord import run_output_sha256
from luna.v7.runstore import encode_run
from luna.v7.timeutil import MIN, D, H, to_ns
from tests.v7 import fixtures_v7 as fx
from tests.v7 import perf_fixtures as pf
from tests.v7.test_perf_equivalence import _rand_view, same

Q = 15 * MIN


@pytest.fixture
def small_steps(monkeypatch):
    """Scan step 37 min and label blocks of 2 days: many window / block boundaries."""
    monkeypatch.setattr(lazy1m, "SCAN_STEP_NS", 37 * MIN)
    monkeypatch.setattr(runner, "LABEL_BLOCK_NS", 2 * D)


def _lazy_view(x: refs.InstView) -> refs.InstView:
    """``x`` with its 1-minute frame replaced by a ``Lazy1m`` over the same rows."""
    lz = lazy1m.from_frame("SYM", x.b1, -(2**62), 2**62)
    return refs.restrict(lz, x.b15, x.m15, x.funding, x.inst, x.end)


def _same_run(a, b) -> None:
    assert encode_run(a) == encode_run(b)
    assert run_output_sha256(a) == run_output_sha256(b)


# ----------------------------------------------------------------------------------------- the 1m rules


def test_lazy_rules_equal_resident_on_random_views(small_steps):
    g = np.random.default_rng(29)
    n = 0
    for _ in range(6):
        x = _rand_view(g)
        lx = _lazy_view(x)
        assert x.ix().ok and lx.ix().ok and lx.ix().lazy is not None
        t1 = x.b1["open_ts"].to_numpy()
        taus = np.concatenate([g.choice(t1, 40), g.integers(t1.min() - H, t1.max() + H, 40)])
        for tau in taus.tolist():
            assert refs.x_halted(lx, tau) == refs.x_halted(x, tau)
            same(refs.ref_price(lx, tau), refs.ref_price(x, tau))
            same(refs.x_half_spread(lx, tau), refs.x_half_spread(x, tau))
            for hi in (int(tau + 2 * H), int(tau + 9 * H), x.end):
                same(refs.x_first_executable_after(lx, tau, hi), refs.x_first_executable_after(x, tau, hi))
                n += refs.x_first_executable_after(x, tau, hi) is not None
        a, b = refs.funding_schedule(lx, pf.START + 50 * H), refs.funding_schedule(x, pf.START + 50 * H)
        same((a.ts, a.rate, a.nom, a.invalid), (b.ts, b.rate, b.nom, b.invalid))
    assert n > 100


def test_lazy_exec_view_equals_resident(small_steps):
    g = np.random.default_rng(31)
    kinds = set()
    for _ in range(4):
        x = _rand_view(g)
        rv, lv = ExecView.build("SYM", x), ExecView.build("SYM", _lazy_view(x))
        t1 = x.b1["open_ts"].to_numpy()
        for tau in np.concatenate([g.choice(t1, 30), g.integers(t1.min(), t1.max(), 30)]).tolist():
            same(lv.b1(tau - H, tau + H), rv.b1(tau - H, tau + H).loc[:, ["open_ts", "o", "c", "vol_base"]]
                 .reset_index(drop=True))
            assert lv.halted(tau) == rv.halted(tau)
            same(lv.ref(tau), rv.ref(tau))
            same(lv.ref_at(tau), rv.ref_at(tau))
            same(lv.p_exact(tau), rv.p_exact(tau))
            for opening in (True, False):
                tr = Trade("SYM", 1, 2.0, None, opening, "test", "p1")
                a, b = taker_at(lv, tr, tau), taker_at(rv, tr, tau)
                same(a, b)
                kinds.add(type(a).__name__)
                if not opening:
                    same(forced_at(lv, tr, tau), forced_at(rv, tr, tau))
                    p = Pending(tr, int(tau))
                    same(halted_exit(lv, p), halted_exit(rv, p))
    assert {"Fill", "Pending"} <= kinds


def test_lazy_path_refuses_what_the_resident_index_would_not_take():
    t0 = pf.START
    b1 = pd.DataFrame({"open_ts": t0 + np.array([3, 1, 2], np.int64) * MIN, "o": 1.0, "c": 1.0,
                       "vol_base": 1.0})
    with pytest.raises(ValueError, match="strictly increasing"):
        lazy1m.from_frame("X", b1, t0, t0 + 10 * MIN)
    dup = b1.assign(open_ts=t0 + np.array([1, 1, 2], np.int64) * MIN)
    with pytest.raises(ValueError, match="strictly increasing"):
        lazy1m.from_frame("X", dup, t0, t0 + 10 * MIN)
    bad = lazy1m.Lazy1m("X", t0, t0 + 10 * MIN,
                        lambda a, b, c: {"open_ts": t0 + np.array([2, 1], np.int64) * MIN,
                                         **{k: np.ones(2) for k in c}}, lambda a, b: None)
    with pytest.raises(ValueError, match="strictly increasing"):
        bad.halted(t0 + 5 * MIN)
    x = _rand_view(np.random.default_rng(3))
    lz = lazy1m.from_frame("X", x.b1, -(2**62), 2**62)
    b15 = x.b15.iloc[::-1]  # unsorted 15m: the resident index falls back to frames; the lazy one refuses
    lx = refs.restrict(lz, b15, x.m15, x.funding, x.inst, x.end)
    with pytest.raises(ValueError, match="lazy 1-minute path needs 15m bars"):
        refs.x_halted(lx, int(x.end))


# ------------------------------------------------------------------------------- universes (in memory)


def _universes(inputs, lo, hi):
    src = fx.LedgeredSource(inputs)
    return (runner.ResearchUniverse.from_source(src, lo, hi, lazy_1m=False),
            runner.ResearchUniverse.from_source(src, lo, hi))


def test_lazy_universe_labels_equal_resident_rich_fixture(small_steps):
    inputs = pf.build_inputs()
    ru, lu = _universes(inputs, pf.START, pf.END)
    assert set(lu.lazy_1m) == set(inputs) and not ru.lazy_1m
    assert len(lu.inputs["AAAUSDT"].bars1m) < len(inputs["AAAUSDT"].bars1m)  # halt windows only
    assert lu.data_span() == ru.data_span()
    ts = [int(t) for t in fx.decisions_in(pf.LABEL_WIN)]
    w = (pf.LABEL_WIN[0] - 2 * D, pf.LABEL_WIN[1] + 4 * D)
    rp, lp = runner.DecisionProvider(ru, with_features=True), runner.DecisionProvider(lu, with_features=True)
    for h in (24, 72):
        a = runner.label_panel(ru, rp, ts, h, w, pf.NOW)
        b = runner.label_panel(lu, lp, ts, h, w, pf.NOW)
        same(a, b)
        assert a.n_labels > 50
    # the resident universe built directly from the inputs gives the same panel
    du = fx.ledgered(runner.ResearchUniverse(inputs))
    same(runner.label_panel(du, runner.DecisionProvider(du, with_features=True), ts, 24, w, pf.NOW),
         runner.label_panel(lu, lp, ts, 24, w, pf.NOW))
    # provider views of the lazy universe (halt-window 1m only) equal the resident ones
    for t in pf.decision_sample()[::5]:
        same(lp(t), rp(t))


def test_lazy_universe_e2e_fixture_labels_and_books_identical(small_steps, monkeypatch):
    """The end-to-end chain's outcome side (labels, the fold model fitted on them, A0 / A2 under E_T and E_M)
    is byte-identical on the lazy universe."""
    monkeypatch.setattr(models, "search_space", lambda h: list(fx.TINY_SPACE))
    inputs = fx.build_inputs(7)
    ru, lu = _universes(inputs, fx.START, fx.END)
    out = {}
    for name, u in (("resident", ru), ("lazy", lu)):
        prov = runner.DecisionProvider(u, with_features=True)
        panel = runner.label_panel(u, prov, fx.sample_times()[::7], fx.H_STAR, windows.W_DEV, fx.NOW)
        [fm] = runner.fit_fold_models(panel.samples, [fx.FOLD], fx.H_STAR)
        model = runner.FoldScoreModel([fm], "A2", fx.H_STAR)
        runs = {(arm, ex): runner.run_book(arm, u, prov, fx.BOOK, fx.H_STAR, fx.NOW,
                                           model=model if arm == "A2" else None, execution=ex)
                for arm in ("A0", "A2") for ex in ("E_T", "E_M")}
        hold = runner.label_panel(u, prov, fx.decisions_in(fx.BOOK), fx.H_STAR, fx.BOOK, fx.NOW)
        out[name] = (panel, model.hashes, runs, hold)
    (pa, ha, ra, qa), (pb, hb, rb, qb) = out["resident"], out["lazy"]
    same(pa, pb)
    same(qa, qb)
    assert ha == hb
    for k in ra:
        _same_run(ra[k], rb[k])
        assert ra[k].valid and len(ra[k].fills) > 0, k
    assert (ra[("A2", "E_M")].fills["liquidity"] == "maker").any()


# ------------------------------------------------------------------------------- the raw store (months)


B = to_ns("2022-02-01")  # a month boundary: the R5 chunks of January / February meet here
ST_START, ST_DAYS = to_ns("2021-11-20"), 84  # 15m history >= 60 days before the labelled / booked days
DENSE = (to_ns("2022-01-26"), to_ns("2022-02-08"))


def _edit(sym: str, b1: pd.DataFrame) -> pd.DataFrame:
    t = b1["open_ts"].to_numpy(np.int64)
    if sym == "S001USDT":  # volume 0 over [B - 3 h, B): a halted exit fills on the first bar of February
        b1.loc[(t >= B - 3 * H) & (t < B), "vol_base"] = 0.0
        b1.loc[t == B, "vol_base"] = 5.0
    if sym == "S002USDT":  # volume 0 over [B - 4 h, B - 1 min), the bar at B - 1 min trades: last minute
        b1.loc[(t >= B - 4 * H) & (t < B - MIN), "vol_base"] = 0.0
        if not (t == B - MIN).any():
            raise AssertionError("fixture: no bar at B - 1 min")
        b1.loc[t == B - MIN, "vol_base"] = 5.0
    if sym == "S003USDT":  # no bar in [B - 30 min, B + 10 min): the data-gap search crosses the boundary
        b1 = b1[(t < B - 30 * MIN) | (t >= B + 10 * MIN)]
        if B + 10 * MIN not in set(b1["open_ts"].tolist()):
            raise AssertionError("fixture: no bar at B + 10 min")
    return b1


@pytest.fixture(scope="module")
def store_src(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("lazy_store")
    with k9.using(k9.K9Log(tmp / "k9")):
        st = rawstore.RawStore(tmp / "raw")
        meta = pf.scale_store(st, 5, ST_DAYS, start=ST_START, notice_every=4, dense_1m=DENSE, edit_1m=_edit,
                              aggtrades=True, carry=True)
        yield st, meta, tmp


def _label_times() -> list[int]:
    grid = [int(t) for t in windows.decision_times(DENSE[0] + D, DENSE[1] - 4 * D)]
    special = []
    for h in (24, 72):
        special += [B - H - Q - h * H, B - 30 * MIN - Q - h * H, B - 20 * MIN - Q - h * H]
    return sorted(set(grid + special + [B - Q, B - Q - MIN]))


def test_store_lazy_universe_equals_resident_at_month_boundaries(store_src, small_steps):
    st, meta, tmp = store_src
    end = ST_START + ST_DAYS * D
    with k9.using(k9.K9Log(tmp / "k9b")):
        src_r = rawstore.StoreSource(st, meta)
        src_l = rawstore.StoreSource(st, meta, lazy_1m_budget_bytes=2 * 2**20)  # tiny: chunks are evicted
        ru = runner.ResearchUniverse.from_source(src_r, ST_START, end, lazy_1m=False)
        lu = runner.ResearchUniverse.from_source(src_l, ST_START, end)
        assert len(lu.inputs["S001USDT"].bars1m) == 0 and len(ru.inputs["S001USDT"].bars1m) > 10_000
        assert len(lu.inputs["S004USDT"].bars1m) > 0  # the U6 halt window rows (decision views)
        rp, lp = (runner.DecisionProvider(u, with_features=True) for u in (ru, lu))
        ts = _label_times()
        win = (ST_START, end)
        flags: dict[str, set] = {}
        for h in (24, 72):
            a = runner.label_panel(ru, rp, ts, h, win, pf.NOW)
            b = runner.label_panel(lu, lp, ts, h, win, pf.NOW)
            same(a, b)
            for s in ("S001USDT", "S002USDT", "S003USDT"):
                leg, btc = lu.leg_data(s, B - H - Q - h * H), lu.leg_data(lu.hedge, B - H - Q - h * H)
                for t in (B - H - Q - h * H, B - 30 * MIN - Q - h * H, B - 20 * MIN - Q - h * H, B - Q):
                    r = runner.leg_label(runner.LabelInputs(s, leg, btc, win, pf.NOW), t, 1, h)
                    if r is not None:
                        flags.setdefault(s, set()).update(r.flags)
                        flags.setdefault(s, set()).add(("t_x_eff", r.t_x_eff))
                        if r.invalid_reason:
                            flags[s].add(r.invalid_reason)
        # the edge cases happened: fill on the first minute of February (closes B + 1 min), on the last
        # minute of January (closes B), the gap price found after the boundary, entries halted across it
        assert "halted_exit" in flags["S001USDT"] and ("t_x_eff", B + MIN) in flags["S001USDT"]
        assert "halted_exit" in flags["S002USDT"] and ("t_x_eff", B) in flags["S002USDT"]
        assert "exit_gap_price" in flags["S003USDT"]
        assert "entry_halted" in flags["S001USDT"]
        assert src_l.r5_lazy.cache.stats["evictions"] > 0
        assert src_l.r5_lazy.cache.bytes <= 2 * 2**20
        # books: the terminal unwind on the last minute of January; E_M with 00:05 decisions whose halt
        # check spans the boundary
        kinds: set = set()
        for window in ((B - 4 * D, B), (B - 3 * D, B + 3 * D)):
            for ex in ("E_T", "E_M"):
                a = runner.run_book("A0", ru, rp, window, 168, pf.NOW, execution=ex)
                b = runner.run_book("A0", lu, lp, window, 168, pf.NOW, execution=ex)
                _same_run(a, b)
                assert len(a.fills) > 0, (window, ex, a.invalid)
                kinds |= set(a.fills["reason"]) | set(a.fills["liquidity"]) | {a.invalid}
        # S001 is HALTED at the unwind (Jan 31 23:59, the chunk's last minute): no executable bar before the
        # end -> INVALID RUN on both paths; the other window unwinds and fills maker orders
        assert {"maker", "terminal_unwind", INVALID_NO_EXIT} <= kinds


def test_lazy_store_reads_only_needed_windows(store_src):
    """No full-history 1-minute load: a lazy read decodes only the month chunks of its window, the needed
    columns only, through the dedicated byte-bounded cache."""
    st, meta, tmp = store_src
    with k9.using(k9.K9Log(tmp / "k9c")):
        src = rawstore.StoreSource(st, meta)
        lz = src.lazy_1m("S001USDT", ST_START, ST_START + ST_DAYS * D)
        assert src.r5_lazy.stats["decoded_chunks"] == 0
        assert lz.halted(B - H) and not lz.halted(B + 2 * H)
        assert src.r5_lazy.stats["decoded_chunks"] == 2  # January and February, one read each
        assert {k[-1] for k in src.r5_lazy.cache.keys()} == {"open_ts", "vol_base"}
        assert lz.first_executable_after(B - 2 * H, B + D, Instance(0, ST_START, None, None)) == (
            B + MIN, float(src.bars["R5"].frame("S001USDT", B, B + MIN)["c"].iloc[0]))
        sp = lz.span()
        assert sp is not None and DENSE[0] <= sp[0] and sp[1] <= DENSE[1]
        assert replace(lz, hi=lz.lo).span() is None


def test_lazy_store_window_is_guarded_and_reads_stay_inside_it(store_src):
    """The lazy window is guarded once, as a load of it would be (a W_HOLD window is refused before the
    opening, K9-logged); every read must lie inside it; each chunk read is still verified."""
    from luna.v7 import guard

    st, meta, tmp = store_src
    with k9.using(k9.K9Log(tmp / "k9d")):
        src = rawstore.StoreSource(st, meta)
        with pytest.raises(guard.HoldoutBlocked):
            src.lazy_1m("S001USDT", windows.W_HOLD[0], windows.W_HOLD[0] + D)
        read = src.r5_lazy.window_reader("S001USDT", B - D, B + D)
        assert len(read(B - H, B, ("c",))["open_ts"]) > 0
        for a, b in ((B - 2 * D, B), (B, B + 2 * D), (B, B)):
            with pytest.raises(ValueError, match="outside the guarded window"):
                read(a, b, ("c",))
