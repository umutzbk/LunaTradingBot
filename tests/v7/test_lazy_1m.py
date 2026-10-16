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
from tests.v7.test_perf_equivalence import _m1_inputs, _rand_view, same

Q = 15 * MIN


@pytest.fixture
def small_steps(monkeypatch):
    """Scan step 37 min, label blocks of 2 days, decision chunks of 3 days: many window / block / chunk
    boundaries."""
    monkeypatch.setattr(lazy1m, "SCAN_STEP_NS", 37 * MIN)
    monkeypatch.setattr(runner, "LABEL_BLOCK_NS", 2 * D)
    monkeypatch.setattr(runner, "DECISION_CHUNK_NS", 3 * D)


def _lazy_view(x: refs.InstView, full: bool = False) -> refs.InstView:
    """``x`` with its 1-minute frame replaced by a ``Lazy1m`` over the same rows (``full``: the 15m and mark
    frames by ``LazyBars`` too)."""
    lz = lazy1m.from_frame("SYM", x.b1, -(2**62), 2**62)
    if not full:
        return refs.restrict(lz, x.b15, x.m15, x.funding, x.inst, x.end)
    b15 = lazy1m.table_from_frame("SYM", "bars15", x.b15.reset_index(drop=True), -(2**62), 2**62,
                                  lazy1m.B15_COLS)
    m15 = lazy1m.table_from_frame("SYM", "mark15", x.m15.reset_index(drop=True), -(2**62), 2**62,
                                  lazy1m.M15_COLS)
    return refs.restrict(lz, b15, m15, x.funding, x.inst, x.end)


def _prov(u) -> runner.DecisionProvider:
    """A provider that may compute missing views (the explicit opt-in of synthetic / in-memory runs)."""
    return runner.DecisionProvider(u, with_features=True, compute_missing=True)


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
    return (runner.ResearchUniverse.from_source(src, lo, hi, lazy=False),
            runner.ResearchUniverse.from_source(src, lo, hi))


def test_lazy_universe_labels_equal_resident_rich_fixture(small_steps):
    inputs = _m1_inputs()  # no NMOUSDT: the chunked decision universes refuse non-monotone known_at
    ru, lu = _universes(inputs, pf.START, pf.END)
    assert set(lu.lazy_1m) == set(inputs) and not ru.lazy_1m
    assert len(lu.inputs["AAAUSDT"].bars1m) < len(inputs["AAAUSDT"].bars1m)  # halt windows only
    assert lu.data_span() == ru.data_span()
    ts = [int(t) for t in fx.decisions_in(pf.LABEL_WIN)]
    w = (pf.LABEL_WIN[0] - 2 * D, pf.LABEL_WIN[1] + 4 * D)
    rp, lp = _prov(ru), _prov(lu)
    for h in (24, 72):
        a = runner.label_panel(ru, rp, ts, h, w, pf.NOW)
        b = runner.label_panel(lu, lp, ts, h, w, pf.NOW)
        same(a, b)
        assert a.n_labels > 50
    # the resident universe built directly from the inputs gives the same panel
    du = fx.ledgered(runner.ResearchUniverse(inputs))
    same(runner.label_panel(du, _prov(du), ts, 24, w, pf.NOW),
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
        prov = _prov(u)
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
        ru = runner.ResearchUniverse.from_source(src_r, ST_START, end, lazy=False)
        lu = runner.ResearchUniverse.from_source(src_l, ST_START, end)
        assert len(lu.inputs["S001USDT"].bars1m) == 0 and len(ru.inputs["S001USDT"].bars1m) > 10_000
        assert len(lu.inputs["S004USDT"].bars1m) > 0  # the U6 halt window rows (decision views)
        rp, lp = (_prov(u) for u in (ru, lu))
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


# --------------------------------------------------------------------------------- round 2: 15m / R4, panel


def test_lazy_15m_rules_equal_resident_on_random_views(small_steps):
    """P(tau) (15m fallback), h_fill windows, the beta-hat hourly inputs, the funding mark chain and every
    ExecView read (marks at ticks, h_fill, P) on lazy 15m / mark windows equal the resident frames."""
    from luna.v7.beta import beta_hat_sorted

    g = np.random.default_rng(41)
    n = 0
    for _ in range(5):
        x = _rand_view(g)
        lx = _lazy_view(x, full=True)
        assert lx.ix().ok and lx.ix().lazy15 is not None
        t15 = x.b15["open_ts"].to_numpy()
        taus = np.concatenate([g.choice(t15, 30), g.integers(t15.min(), t15.max() + 2 * H, 30)])
        for tau in taus.tolist():
            same(refs.ref_price(lx, tau), refs.ref_price(x, tau))
            same(refs.x_half_spread(lx, tau), refs.x_half_spread(x, tau))
            ra, la = x.ix().beta_arrays(tau), lx.ix().beta_arrays(tau)
            same(beta_hat_sorted(la, la, tau), beta_hat_sorted(ra, ra, tau))
            n += refs.x_half_spread(x, tau) is not None
        for kb in (pf.START + 30 * H, pf.START + 60 * H, pf.START + 100 * H):
            a, b = refs.funding_schedule(lx, kb), refs.funding_schedule(x, kb)
            same((a.ts, a.rate, a.nom, a.invalid), (b.ts, b.rate, b.nom, b.invalid))
            same([a.mark(i) for i in range(len(a.ts))], [b.mark(i) for i in range(len(b.ts))])
            for lo, hi in ((pf.START, pf.START + 40 * H), (pf.START + 9 * H, pf.START + 33 * H)):
                same(a.check(lo, hi), b.check(lo, hi))
        rv, lv = ExecView.build("SYM", x), ExecView.build("SYM", lx)
        for ts in (t15[::7] + Q).tolist():
            same(lv.mark(ts), rv.mark(ts))
            same(lv.h_fill(ts), rv.h_fill(ts))
            same(lv.p_exact(ts), rv.p_exact(ts))
            same(lv.ref_at(ts), rv.ref_at(ts))
            tr = Trade("SYM", -1, 1.5, None, False, "test", "p1")
            same(taker_at(lv, tr, ts), taker_at(rv, tr, ts))
    assert n > 50


def test_delegated_decision_views_equal_resident_across_chunks(small_steps):
    """A lazy universe's provider serves the views of 3-day decisions-only chunks (F-feature look-backs, h-hat
    and beta-hat windows across chunk boundaries); views, records and the public per-t inputs equal the
    resident universe's."""
    src = fx.LedgeredSource(_m1_inputs())
    ru = runner.ResearchUniverse.from_source(src, pf.START, pf.END, lazy=False)
    lu = runner.ResearchUniverse.from_source(src, pf.START, pf.END)
    assert lu.decision_source is src and len(lu.inputs["AAAUSDT"].bars15) == 0
    rp, lp = _prov(ru), _prov(lu)
    times = [t for t in pf.decision_sample() if t >= pf.START + 70 * D][::3] + [pf.S_A + 72 * H + 5 * MIN]
    for t in sorted(set(times)):
        same(lp(t), rp(t))
        same(lp.record(t), rp.record(t))
        for s_ in ("AAAUSDT", "GAPUSDT", pf.BTC):
            same(lp.h_hat(s_, t), rp.h_hat(s_, t))
            same(lp.beta(s_, t), rp.beta(s_, t))
            same(lp.qv24(s_, t), rp.qv24(s_, t))
        same(lp.stale_reasons(t), rp.stale_reasons(t))
    assert lp.n_computed == len(set(times))
    with pytest.raises(ValueError, match="outside the universe window"):
        lp(pf.END + 5 * MIN)


def _rows(g: np.random.Generator, n: int, kind: int) -> list[dict]:
    out = []
    for i in range(n):
        f = float(g.normal())
        out.append(dict(
            f1=None if kind == 1 else (f if g.uniform() > 0.1 else None),  # an all-None block (kind 1)
            f2=-0.0 if i % 7 == 0 else (float("nan") if i % 5 == 0 else f),
            side=int(g.choice([1, -1])), symbol=f"S{i % 3}",
            y_pos=None if (kind == 2 and i % 2) else int(i % 2),
            flag=bool(i % 2), reason=None if kind != 3 else ("a" if i % 2 else None), net=f * 1e-4,
            ts=1_640_995_200_000_000_000 + i))
    return out


def test_columnar_panel_equals_dataframe_of_dicts():
    """``runner._Columnar`` (blocks) builds exactly ``pd.DataFrame(rows)``: all-None blocks of a float column,
    ints with None, str with None, bools, -0.0 / NaN, any block split; an empty panel is ``DataFrame([])``."""
    g = np.random.default_rng(5)
    for _ in range(12):
        n_blocks = int(g.integers(1, 6))
        blocks = [_rows(g, int(g.integers(0, 40)), int(g.integers(0, 4))) for _ in range(n_blocks)]
        col = runner._Columnar()
        for b in blocks:
            col.add(b)
        want = pd.DataFrame([r for b in blocks for r in b])
        got = col.frame()
        same(got, want)
        assert list(got.dtypes) == list(want.dtypes)
    same(runner._Columnar().frame(), pd.DataFrame([]))
    bad = runner._Columnar()
    bad.add([{"x": 1.0}])
    bad.add([{"x": "a"}])
    with pytest.raises(RuntimeError, match="not reproducible"):
        bad.frame()


def test_label_block_raises_the_panel_order_first_exception(monkeypatch):
    """Two labels of one block fail differently: the panel-order first one is raised (as in one pass), not
    the per-symbol sweep's first."""
    src = fx.LedgeredSource(_m1_inputs())
    lu = runner.ResearchUniverse.from_source(src, pf.START, pf.END)
    lp = _prov(lu)
    ts = [int(t) for t in fx.decisions_in(pf.LABEL_WIN)][:4]
    first = sorted(lp(ts[0]).cands["symbol"].astype(str))[-1]  # panel-order first failure (late symbol)
    later = sorted(lp(ts[-1]).cands["symbol"].astype(str))[0]  # the sweep reaches it first (early symbol)
    assert later < first
    real = runner.leg_label

    def flaky(inputs, t, side, h):
        if (inputs.symbol, t) in ((first, ts[0]), (later, ts[-1])):
            raise ValueError(f"boom {inputs.symbol} {t}")
        return real(inputs, t, side, h)

    monkeypatch.setattr(runner, "leg_label", flaky)
    w = (pf.LABEL_WIN[0] - 2 * D, pf.LABEL_WIN[1] + 4 * D)
    with pytest.raises(ValueError, match=f"boom {first} {ts[0]}"):
        runner.label_panel(lu, lp, ts, 24, w, pf.NOW)


def test_panel_sha256_identical_resident_and_lazy(small_steps):
    """The pinned panel hash (``leakage.panel_sha256``: R54 / MJ3 / L9) of the lazy universe's panel equals
    the resident one's, for every horizon."""
    from luna.v7 import leakage

    src = fx.LedgeredSource(_m1_inputs())
    ru = runner.ResearchUniverse.from_source(src, pf.START, pf.END, lazy=False)
    lu = runner.ResearchUniverse.from_source(src, pf.START, pf.END)
    rp, lp = _prov(ru), _prov(lu)
    ts = [int(t) for t in fx.decisions_in(pf.LABEL_WIN)]
    w = (pf.START, pf.END)
    for h in (24, 72, 168):
        a, b = runner.label_panel(ru, rp, ts, h, w, pf.NOW), runner.label_panel(lu, lp, ts, h, w, pf.NOW)
        same(a, b)
        if len(a.samples):
            assert leakage.panel_sha256(a.samples, h) == leakage.panel_sha256(b.samples, h)


def test_store_aggtrades_day_loader_equals_the_frame(store_src, small_steps):
    """E_M from a per-UTC-day aggTrades loader (R10 daily files; a tiny cache: evictions) equals the
    resident frame; each loaded day is guarded (a W_HOLD day is refused before the opening)."""
    from luna.v7 import guard

    st, meta, tmp = store_src
    end = ST_START + ST_DAYS * D
    calls: list[int] = []

    def loader(sym: str, d: int) -> pd.DataFrame:
        calls.append(d)
        a = meta[sym].aggtrades
        t = a["ts"].to_numpy(np.int64)
        return a[(t >= d) & (t < d + D)].reset_index(drop=True)

    meta2 = {s: replace(m, aggtrades=loader) for s, m in meta.items()}
    with k9.using(k9.K9Log(tmp / "k9e")):
        ru = runner.ResearchUniverse.from_source(rawstore.StoreSource(st, meta), ST_START, end, lazy=False)
        src2 = rawstore.StoreSource(st, meta2, lazy_agg_budget_bytes=8 * 2**10)
        lu = runner.ResearchUniverse.from_source(src2, ST_START, end)
        rp, lp = (_prov(u) for u in (ru, lu))
        a = runner.run_book("A0", ru, rp, (B - 3 * D, B + 3 * D), 168, pf.NOW, execution="E_M")
        b = runner.run_book("A0", lu, lp, (B - 3 * D, B + 3 * D), 168, pf.NOW, execution="E_M")
        _same_run(a, b)
        assert (a.fills["liquidity"] == "maker").any() and calls
        assert src2.agg_cache.stats["evictions"] > 0
        with pytest.raises(guard.HoldoutBlocked):
            src2.lazy_trades("S001USDT", windows.W_HOLD[0], windows.W_HOLD[0] + 2 * D).frame(
                windows.W_HOLD[0], windows.W_HOLD[0] + H)


def test_guard_location_is_resolved_once_per_process():
    """The guards' location seam resolves the repository once (``spec.code_repo``); a monkeypatched
    ``guard._roots`` still replaces it (the checks are unchanged)."""
    from luna.v7 import guard, spec

    a = spec.code_repo()
    before = spec._repo_of.cache_info()
    for _ in range(20):
        guard.check_outcome_window(pf.START, pf.START + D, now_ns=pf.NOW)
    after = spec._repo_of.cache_info()
    assert spec.code_repo() is a and after.misses == before.misses and after.hits > before.hits


# ----------------------------------------------------------------------------------------------- round 3


def test_lazy_universe_refuses_a_decision_view_missing_from_the_store(tmp_path):
    """A lazy outcome universe never builds a decisions-only chunk silently: a view missing from the store
    (or no store) is an IntegrityBlocker (K9-logged), label_panel without a store is refused; the explicit
    opt-in computes it; a view the store holds is served."""
    from luna.v7.data.schema import IntegrityBlocker
    from luna.v7.viewstore import ViewStore

    src = fx.LedgeredSource(_m1_inputs())
    lu = runner.ResearchUniverse.from_source(src, pf.START, pf.END)
    t = [x for x in pf.decision_sample() if x >= pf.START + 70 * D][0]
    with pytest.raises(IntegrityBlocker, match="run decision_pass first"):
        runner.DecisionProvider(lu, with_features=True)(t)
    with pytest.raises(IntegrityBlocker, match="run decision_pass first"):
        runner.DecisionProvider(lu, with_features=True).h_hat("AAAUSDT", t)
    with pytest.raises(IntegrityBlocker, match="needs DecisionProvider"):
        runner.label_panel(lu, runner.DecisionProvider(lu, with_features=True), [t], 24, (pf.START, pf.END),
                           pf.NOW)
    vs = ViewStore(tmp_path / "v")
    runner.decision_pass(src, [t], vs, history_lo=pf.START)
    p = runner.DecisionProvider(lu, with_features=True, store=ViewStore(tmp_path / "v"))
    p(t)
    assert p.n_computed == 0
    with pytest.raises(IntegrityBlocker, match="run decision_pass first"):
        p(t + 8 * H)  # not in the store
    q = runner.DecisionProvider(lu, with_features=True, compute_missing=True)
    same(q(t), p(t))
    assert q.n_computed == 1


def test_columnar_codec_is_pinned_to_the_verified_pandas(monkeypatch):
    assert runner._Columnar.PANDAS_VERIFIED == pd.__version__
    monkeypatch.setattr(runner._Columnar, "PANDAS_VERIFIED", "0.0")
    with pytest.raises(RuntimeError, match="re-verify runner._Columnar"):
        runner._Columnar().add([{"a": 1.0}])


def test_from_source_releases_the_shared_chunk_cache(store_src):
    st, meta, tmp = store_src
    with k9.using(k9.K9Log(tmp / "k9f")):
        src = rawstore.StoreSource(st, meta)
        runner.ResearchUniverse.from_source(src, ST_START, ST_START + ST_DAYS * D)
        assert src.cache.bytes == 0 and not src.cache.keys()
