"""Task 26b (SC-01 part 2): the real-scale compute paths give EXACTLY the results of the code they replace.

The reference is ``tests/v7/perf_reference.py``: verbatim copies (extracted from git at b0fbd3a) of every
function Task 26b changed. Values are compared bit for bit (floats by their IEEE representation, -0.0 and NaN
included; frames by columns, dtypes, index and every cell) on the rich synthetic universe of
``tests/v7/perf_fixtures.py`` (gaps, an instance switch, notices with the 72 h test passing / halted / a
postponement, funding interval changes and an odd record, known_at lags, non-monotone known_at, known_at
exactly t - 1 min) and on randomized inputs with fixed seeds.
"""

from __future__ import annotations

import math
from dataclasses import fields, is_dataclass, replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import beta, features, guard, labels, refs, runner, universe, viewstore, windows
from luna.v7.data.instances import Instance
from luna.v7.data.notices import U6State, known_at
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.features import XSInput
from luna.v7.pitindex import LRU, FrameIndex, HaltIndex, HaltProbe
from luna.v7.timeutil import MIN, D, H
from luna.v7.viewstore import ViewStore
from tests.v7 import fixtures_v7 as fx
from tests.v7 import perf_fixtures as pf
from tests.v7 import perf_reference as ref

Q = 15 * MIN


def canon(x: object) -> object:
    """Exact identity of a value: floats by their bits (one canonical NaN), containers / dataclasses / frames
    recursively, types kept apart (bool, int, float, str, None)."""
    if x is None or isinstance(x, str):
        return x
    if isinstance(x, bool | np.bool_):
        return ("b", bool(x))
    if isinstance(x, int | np.integer):
        return ("i", int(x))
    if isinstance(x, float | np.floating):
        f = float(x)
        return ("f", "nan" if math.isnan(f) else f.hex())
    if isinstance(x, pd.DataFrame):
        return ("df", tuple(map(str, x.columns)), tuple(map(str, x.dtypes)), type(x.index).__name__,
                tuple(canon(v) for v in x.index.tolist()),
                tuple(tuple(canon(v) for v in x[c].tolist()) for c in x.columns))
    if isinstance(x, pd.Series):
        return ("s", str(x.dtype), tuple(canon(v) for v in x.index.tolist()),
                tuple(canon(v) for v in x.tolist()))
    if isinstance(x, np.ndarray):
        return ("a", str(x.dtype), tuple(canon(v) for v in x.tolist()))
    if is_dataclass(x):
        return (type(x).__name__, tuple((f.name, canon(getattr(x, f.name))) for f in fields(x)))
    if isinstance(x, dict):
        return ("d", tuple((canon(k), canon(v)) for k, v in x.items()))
    if isinstance(x, list | tuple):
        return ("l", tuple(canon(v) for v in x))
    raise TypeError(type(x))


def same(a: object, b: object) -> None:
    assert canon(a) == canon(b)


@pytest.fixture(scope="module")
def pfu() -> runner.ResearchUniverse:
    return runner.ResearchUniverse(pf.build_inputs())


def _ref_provider(u, wf):
    return ref.DecisionProvider(u, with_features=wf, max_cached_decisions=None)


# The verbatim reference re-samples the U6 72 h halt grid (4,321 HALTED scans of the 1-minute frame) at every
# call once a scheduled time + 72 h has passed (about 1 s per call): the samples below keep a few such calls
# (the boundary minutes and two later decisions) and use every other time before.
_AFTER = (pf.S_A + 72 * H, pf.S_B + 72 * H)


def _light(times: list[int], first: int = 3) -> list[int]:
    late = [t for t in times if t >= min(_AFTER)]
    return sorted({*(t for t in times if t < min(_AFTER)), *late[:first], *late[-1:]})


# ------------------------------------------------------------------------------------- decision views


def test_provider_views_equal_reference(pfu):
    """Every view and record (gates, h-hat, beta-hat, qv24, staleness, candidate rows with the §9 features,
    max known_at) equals the reference provider's, bit for bit, at the sampled decisions and the edges (U6
    72 h boundary at t = sched + 72 h -/+ 1 min, postponement known exactly at t - 1 min, no-instance gap,
    relisting, the funding transition, known_at = t - 1 min)."""
    # GAPUSDT's 72 h test passes from S_G + 80 h on (about 1 s per reference call): every sampled time before
    # it, a few after (the U6 boundaries, the edges); the gates test covers the later times symbol by symbol
    sample = pf.decision_sample()
    late = [t for t in sample if t >= pf.S_G + 80 * H]
    times = sorted({*(t for t in sample if t < pf.S_G + 80 * H), *late[::6], pf.S_G + 80 * H,
                    pf.S_A + 72 * H, pf.S_A + 72 * H + 5 * MIN, *pf.edge_times()[:2]})
    new0, ref0 = runner.DecisionProvider(pfu, with_features=False), _ref_provider(pfu, False)
    for t in times:
        same(new0(t), ref0(t))
        same(new0.record(t), ref0._records[t])
    new1, ref1 = runner.DecisionProvider(pfu, with_features=True), _ref_provider(pfu, True)
    feat_times = [t for i, t in enumerate(times) if i % 5 == 0][:6] + [pf.S_A + 72 * H + 5 * MIN]
    for t in feat_times:
        v = new1(t)
        same(v, ref1(t))
        same(new1.record(t), ref1._records[t])
    assert any(len(new1(t).cands) for t in feat_times)
    gates = {s: g for t in times for s, g in new0(t).gates.items()}
    assert {"U6"} <= {r for g in gates.values() for r in g.reasons}  # the U6 paths were exercised


def test_provider_views_equal_reference_e2e_fixture():
    u = fx.build_universe(7)
    new, old = runner.DecisionProvider(u, with_features=True), _ref_provider(u, True)
    for t in [int(x) for x in fx.decisions_in(fx.BOOK)][:4]:
        same(new(t), old(t))
        same(new.record(t), old._records[t])


def test_gates_equal_reference_everywhere(pfu):
    """``universe.gates`` with the cache (indexed) and without it (reference path in the new code) equal the
    verbatim reference at many times for every symbol (the non-monotone NMOUSDT takes the fallback)."""
    times = list(range(pf.START + 61 * D, pf.END, 23 * H)) + pf.edge_times()
    times += [pf.S_C - 2 * D + 7 * MIN + d for d in (-1, 0, MIN)]
    boundary = [pf.S_A + 72 * H + d for d in (-MIN, 0, MIN)] + [pf.S_B + 72 * H + 5 * MIN]
    heavy = {"AAAUSDT": min(_AFTER), "BBBUSDT": min(_AFTER), "GAPUSDT": pf.S_G + 80 * H}  # passing 72 h tests
    for s in pf.SYMBOLS:
        sd = pfu.sd[s]
        bare = replace(sd, cache=None)
        if s in heavy:
            late = [t for t in times if t >= heavy[s]]
            ts = sorted({*(t for t in times if t < heavy[s]), *late[:1], *late[-1:], *boundary})
        else:
            ts = times + boundary
        for t in ts:
            want = ref.gates(bare, t)
            same(universe.gates(sd, t), want)
            if s not in heavy or t < heavy[s]:  # the uncached path is the reference code itself
                same(universe.gates(bare, t), want)
    assert not pfu.sd["NMOUSDT"].cache.b15.ok and pfu.sd["AAAUSDT"].cache.b15.ok


def test_u6_state_built_once_equals_per_call_state(pfu):
    """One U6State from every version with ``known_by = t - 1 min`` equals the state of the versions known by
    t - 1 min (the reference builds it per call)."""
    for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
        sd = pfu.sd[s]
        full = U6State(list(sd.notices), s, sd.insts)
        b1 = sd.bars1m
        for t in _light(list(range(pf.START + 78 * D, pf.START + 100 * D, 13 * H + 37 * MIN)), 2):
            cur = universe.current_instance(sd.insts, t)
            if cur is None:
                continue
            kn = [n for n in sd.notices if (k := known_at(n)) is not None and k <= t - MIN]
            pb = ref.pit_rows(b1, t, "bars1m")
            want = ref.U6State(kn, s, sd.insts).active(cur, t, lambda tau: ref.halted(pb, tau))  # noqa: B023
            assert full.active(cur, t, lambda tau: ref.halted(pb, tau), known_by=t - MIN) == want  # noqa: B023
            assert sd.cache.u6_active(cur, t) == want


def test_u6_fallback_cache_is_bounded(pfu):
    """I1: without a 1-minute index (unsorted 1m frame) every call uses a per-call state, as the reference:
    the long-lived state's 72 h cache never grows, and the results equal the reference."""
    x = pf.build_inputs()["AAAUSDT"]
    sd0 = pfu.sd["AAAUSDT"]
    sd = universe.SymbolData("AAAUSDT", x.bars15, x.bars1m.iloc[::-1], sd0.funding_annot, list(x.insts),
                             list(x.notices), x.identity)
    c = universe.SymbolCache(sd)
    for k in range(1, 4):
        t = pf.S_A + 72 * H + k * 15 * MIN
        cur = universe.current_instance(sd.insts, t)
        got = c.u6_active(cur, t)
        pb = ref.pit_rows(sd.bars1m, t, "bars1m")
        kn = [n for n in sd.notices if (q := known_at(n)) is not None and q <= t - MIN]
        assert got == ref.U6State(kn, "AAAUSDT", sd.insts).active(cur, t, lambda tau: ref.halted(pb, tau))  # noqa: B023
    assert c._halt is not None and not c._halt.ok and len(c.u6_state()._clear_cache) == 0


def test_halt_index_equals_halted_on_grid():
    """``HaltIndex.halted_many`` / ``no_halt`` = ``market.halted`` on the PIT rows, sampled on the 1-minute
    72 h grid, on random 1-minute frames with holes, zero volumes and NaN volumes."""
    g = np.random.default_rng(5)
    for trial in range(12):
        n = 6000
        ts = pf.START + np.cumsum(g.choice([MIN, MIN, MIN, 2 * MIN, 50 * MIN, 75 * MIN], n)).astype(np.int64)
        vol = g.choice([1.0, 1.0, 0.0, np.nan], n)
        b1 = pd.DataFrame({"open_ts": ts, "vol_base": vol, "known_at": ts + MIN})
        ix = HaltIndex(b1)
        assert ix.ok
        for _ in range(6):
            t = int(g.integers(ts[0], ts[-1] + 2 * H))
            pit = ref.pit_rows(b1, t, "b1")
            k = ix.pit_end(t)
            taus = g.integers(ts[0] - H, ts[-1] + 2 * H, 50)
            got = ix.halted_many(taus.astype(np.int64), k)
            assert [bool(x) for x in got] == [ref.halted(pit, int(x)) for x in taus]
            sched = int(g.integers(ts[0], ts[-1]))
            end = sched + 3 * H if trial % 2 else sched + 72 * H
            grid = [*range(sched, end, MIN), end]
            assert HaltProbe(ix, k).no_halt(sched, end, MIN) == (not any(ref.halted(pit, x) for x in grid))
    assert not HaltIndex(pd.DataFrame({"open_ts": [2, 1], "vol_base": [1.0, 1.0], "known_at": [3, 2]})).ok


def test_beta_hat_sorted_equals_beta_hat():
    g = np.random.default_rng(9)
    for _ in range(30):
        n = 900
        hours = pf.START + H * np.cumsum(g.choice([1, 1, 1, 2], n)).astype(np.int64)
        ca = 10 * np.exp(np.cumsum(g.normal(0, 0.01, n)))
        cb = 20000 * np.exp(np.cumsum(g.normal(0, 0.005, n)))
        ca[g.uniform(size=n) < 0.02] = g.choice([np.nan, 0.0, -1.0, np.inf])
        ka = hours + g.choice([0, 0, 5 * MIN, 3 * H], n)
        t = int(hours[-1] - int(g.integers(0, 200)) * H + int(g.integers(0, 3)) * MIN)
        want = ref.beta_hat(pd.Series(ca, index=hours), pd.Series(cb, index=hours), t,
                            pd.Series(ka, index=hours), pd.Series(ka, index=hours))
        got = beta.beta_hat_sorted((hours, ca, ka), (hours, cb, ka), t)
        same(got, want)


def test_cross_section_indexed_equals_reference():
    g = np.random.default_rng(3)
    t = pf.START + 90 * D
    for trial in range(40):
        rows = []
        for i in range(int(g.integers(0, 30))):
            ok = bool(g.uniform() < 0.7)
            gate = universe.GateRow(ok, True, True, True, True, True, True, True, (), None)
            vals = {k: float(g.choice([np.nan, 0.0, -0.0, 1e-4, -1e-4, float(g.normal())]))
                    for k in features.XS_KEYS}
            rows.append(XSInput(f"S{i:02d}USDT", t, gate, vals, int(t - 5 * MIN) if i % 3 else None))
        own_sym = rows[int(g.integers(len(rows)))].symbol if rows and trial % 2 else "ZZZUSDT"
        own = {k: float(g.choice([np.nan, 0.0, 1e-4, -1e-4, float(g.normal())])) for k in features.XS_KEYS}
        if rows and trial % 4 == 1:  # the candidate's own value equals its eligible entry
            own = dict(next(r.values for r in rows if r.symbol == own_sym))
        same(features._cross_section_indexed(rows, t, own_sym, own),
             ref._cross_section(rows, t, own_sym, own))


def test_frame_index_windows():
    """``FrameIndex.window`` = the PIT instance rows inside the look-back plus the last PIT row; the
    decisions-only refusal triggers only when older rows could have been loaded."""
    ts = pf.START + np.array([0, 1, 2, 40 * 96, 40 * 96 + 1], dtype=np.int64) * Q
    df = pd.DataFrame({"open_ts": ts, "c": 1.0, "known_at": ts + Q})
    ix = FrameIndex.build(df, "open_ts", ("c",))
    assert ix.ok
    t = int(ts[2] + 20 * D)  # no row in the 2-day look-back: the last PIT row only
    assert ix.window(pf.START, t, 2 * D)["open_ts"].tolist() == [ts[2]]
    assert ix.window(int(ts[3]), t, 2 * D).empty  # the instance starts later: no PIT row
    assert ix.window(pf.START, int(ts[4]) + Q + MIN, 2 * D)["open_ts"].tolist() == [ts[3], ts[4]]
    assert ix.window(pf.START, int(ts[4]) + Q + MIN - 1, 2 * D)["open_ts"].tolist() == [ts[3]]  # t - 1 min
    assert not FrameIndex.build(df.iloc[[1, 0]], "open_ts").ok
    lru = LRU(2)
    for k in "abc":
        lru.put(k, k)
    assert "a" not in lru and len(lru) == 2 and lru.stats["evictions"] == 1


# ------------------------------------------------------------------------------- refs / labels / market


def _rand_view(g: np.random.Generator, sorted_: bool = True) -> refs.InstView:
    n1, n15 = 3000, 400
    t1 = pf.START + np.cumsum(g.choice([MIN, MIN, MIN, 3 * MIN, 70 * MIN], n1)).astype(np.int64)
    o = 10 * np.exp(np.cumsum(g.normal(0, 1e-3, n1)))
    c = o * np.exp(g.normal(0, 1e-3, n1))
    o[g.uniform(size=n1) < 0.03] = g.choice([np.nan, 0.0, -1.0])
    c[g.uniform(size=n1) < 0.03] = np.nan
    vol = g.choice([1.0, 1.0, 1.0, 0.0], n1)
    b1 = pd.DataFrame({"open_ts": t1, "o": o, "c": c, "vol_base": vol, "known_at": t1 + MIN})
    t15 = pf.START + Q * np.cumsum(g.choice([1, 1, 1, 2], n15)).astype(np.int64)
    c15 = 10 * np.exp(np.cumsum(g.normal(0, 2e-3, n15)))
    b15 = pd.DataFrame({"open_ts": t15, "o": c15 * 0.999, "h": c15 * 1.002, "l": c15 * 0.998, "c": c15,
                        "vol_base": 1.0, "known_at": t15 + Q})
    b15.loc[g.uniform(size=n15) < 0.05, "o"] = np.nan
    m15 = b15[["open_ts"]].assign(c=c15 * 1.0001)
    m15.loc[g.uniform(size=n15) < 0.2, "c"] = np.nan
    fts = pf.START + 8 * H * np.arange(1, 12, dtype=np.int64)
    fts = fts[g.uniform(size=len(fts)) > 0.1]
    fund = pd.DataFrame({"funding_ts": fts, "rate": g.normal(0, 1e-4, len(fts)),
                         "interval_h_explicit": pd.array([8] * len(fts), dtype="Int64"),
                         "mark_at_settle": g.choice([np.nan, 10.0], len(fts)), "known_at": fts + MIN})
    if not sorted_:
        b1 = b1.sample(frac=1.0, random_state=1)
    inst = Instance(0, int(t1[5]), int(t1[-50]) if g.uniform() < 0.5 else None, None)
    return refs.restrict(b1, b15, m15, fund, inst, int(t1[-10]))


def test_refs_indexed_equal_frame_level():
    g = np.random.default_rng(13)
    for trial in range(8):
        x = _rand_view(g, sorted_=trial != 7)
        rx = ref.restrict(x.b1, x.b15, x.m15, x.funding, x.inst, x.end)
        assert x.ix().ok == (trial != 7)
        t1 = rx.b1["open_ts"].to_numpy() if len(rx.b1) else np.array([pf.START])
        taus = np.concatenate([g.choice(t1, 40), g.integers(t1.min() - H, t1.max() + H, 40)])
        for tau in taus.tolist():
            assert refs.x_halted(x, tau) == ref.halted(rx.b1, tau)
            same(refs.ref_price(x, tau), ref.ref_price(rx, tau))
            same(refs.x_half_spread(x, tau), ref.half_spread(rx.b15, tau))
            hi = int(tau + 2 * H)
            same(refs.x_first_executable_after(x, tau, hi),
                 ref.first_executable_after(ref.trim(rx.b1, "open_ts", None, hi), tau, x.inst))
        for kb in (pf.START, pf.START + 30 * H, pf.START + 50 * H, pf.START + 100 * H):
            a, b = refs.funding_schedule(x, kb), ref.funding_schedule(rx, kb)
            for f in ("ts", "rate", "nom", "invalid"):
                same(getattr(a, f), getattr(b, f))
            same([a.mark(i) for i in range(len(a.ts))], [b.mark(i) for i in range(len(b.ts))])
            for lo, hi in ((pf.START, pf.START + 40 * H), (pf.START + 9 * H, pf.START + 33 * H)):
                same(a.check(lo, hi), b.check(lo, hi))


def test_trim_slice_equals_mask():
    g = np.random.default_rng(1)
    for _ in range(20):
        ts = np.sort(g.integers(pf.START, pf.START + 10 * D, 200)).astype(np.int64)
        df = pd.DataFrame({"open_ts": ts, "v": np.arange(200)})
        if g.uniform() < 0.3:
            df = df.sample(frac=1.0, random_state=2)
        lo = None if g.uniform() < 0.3 else int(g.integers(pf.START, pf.START + 10 * D))
        hi = int(g.integers(pf.START, pf.START + 11 * D))
        same(refs.trim(df, "open_ts", lo, hi), ref.trim(df, "open_ts", lo, hi))


def test_labels_equal_reference(pfu):
    """``labels.leg_label`` through the universe's cached ``LegData`` (indexed views) equals the verbatim
    reference with plain ``LegData`` (every label of the label window: halted exits, gap prices, both
    sides, h = 24 and 72)."""
    w = (pf.LABEL_WIN[0] - 2 * D, pf.LABEL_WIN[1] + 4 * D)
    ts = [int(t) for t in fx.decisions_in(pf.LABEL_WIN)][::3]  # 00:05 of each day
    n = 0
    flags: set[str] = set()
    for t in ts:
        for s in pfu.candidates:
            leg, btc = pfu.leg_data(s, t), pfu.leg_data(pfu.hedge, t)
            if leg is None:
                continue
            plain, pbtc = replace(leg, cache=None), replace(btc, cache=None)
            for side in (1, -1):
                for h in (24, 72):
                    got = labels.leg_label(labels.LabelInputs(s, leg, btc, w, pf.NOW), t, side, h)
                    want = ref.leg_label(labels.LabelInputs(s, plain, pbtc, w, pf.NOW), t, side, h)
                    same(got, want)
                    n += 1
                    flags |= set(got.flags) if got is not None else set()
    assert n > 100 and "halted_exit" in flags


# ------------------------------------------------------------------------------- shared decision views


def test_view_store_replays_exactly(pfu, tmp_path, monkeypatch):
    """A provider with a store computes each view once and writes it; another provider (another book run)
    reads it back without computing; the read view equals the computed one bit for bit; a damaged file is an
    IntegrityBlocker."""
    times = pf.decision_sample()[:6]
    st = ViewStore(tmp_path)
    p1 = runner.DecisionProvider(pfu, with_features=True, store=st)
    first = {t: (p1(t), p1.record(t)) for t in times}
    assert st.stats["writes"] == len(times) and p1.n_computed == len(times)
    assert (tmp_path / viewstore.KEY_FILE).exists() and st.body["data"] == {"inputs": runner.inputs_identity(
        pfu.inputs)}
    p2 = runner.DecisionProvider(pfu, with_features=True, store=ViewStore(tmp_path))
    monkeypatch.setattr(p2, "_xs", lambda t: (_ for _ in ()).throw(AssertionError("recomputed")))
    for t in times:
        same(p2(t), first[t][0])
        same(p2.record(t), first[t][1])
    assert p2.n_computed == 0
    with pytest.raises(IntegrityBlocker, match="another key"):  # with_features is part of the key
        runner.DecisionProvider(pfu, with_features=False, store=ViewStore(tmp_path))
    pq = next(st.dir.glob("*.parquet"))
    pq.write_bytes(pq.read_bytes()[:-3] + b"xyz")
    p3 = runner.DecisionProvider(pfu, with_features=True, store=ViewStore(tmp_path))
    with pytest.raises(IntegrityBlocker):
        for t in times:
            p3(t)


def _m1_inputs() -> dict[str, runner.SymbolInputs]:
    """pf inputs without the non-monotone symbol (refused by decisions-only universes) and with a 45-day hole
    in GAPUSDT's premium bars and LAGUSDT's mark bars (the windowed slices take the anchor row, m1)."""
    out = {}
    for s, x in pf.build_inputs().items():
        if s == "NMOUSDT":
            continue
        if s in ("GAPUSDT", "LAGUSDT"):
            k = "premium" if s == "GAPUSDT" else "mark"
            f = getattr(x.r4, k)
            keep = (f["open_ts"] < pf.START + 60 * D) | (f["open_ts"] >= pf.START + 105 * D)
            x = replace(x, r4=replace(x.r4, **{k: f[keep].reset_index(drop=True)}))
        out[s] = x
    return out


def test_decision_pass_equals_full_universe(tmp_path):
    """The chunked decisions-only pass (15m / R4 look-back with the anchor rows, funding from the history
    start, 1-minute bars of the U6 windows only) stores exactly the views of the full universe, also across a
    45-day R4 hole (m1: no refusal); a decisions-only universe refuses labels and books; the store's key is
    bound to the source, the history start and the code (I2); every t is guarded before the store (m2)."""
    inputs = _m1_inputs()
    src = fx.LedgeredSource(inputs)
    times = [t for t in pf.decision_sample() if t >= pf.START + 70 * D][::2] + [pf.S_A + 72 * H + 5 * MIN]
    times = sorted(set(times))
    st = ViewStore(tmp_path / "v")
    out = runner.decision_pass(src, times, st, history_lo=pf.START, chunk_ns=9 * D)
    assert out["computed"] == len(times) and out["chunks"] > 3
    full = runner.DecisionProvider(runner.ResearchUniverse.from_source(src, pf.START, pf.END, lazy=False),
                                   with_features=True, store=ViewStore(tmp_path / "v"))
    plain = runner.DecisionProvider(runner.ResearchUniverse.from_source(src, pf.START, pf.END, lazy=False),
                                    with_features=True)
    for t in times:
        v, r = st.get(t)
        same(v, plain(t))
        same(r, plain.record(t))
        same(full(t), v)  # a full-window provider on the same key reads the stored view
    assert full.n_computed == 0
    assert runner.decision_pass(src, times, st, history_lo=pf.START)["computed"] == 0  # all stored
    # I2: another history start, another code identity: refused, never served
    with pytest.raises(IntegrityBlocker, match="another key"):
        runner.DecisionProvider(runner.ResearchUniverse.from_source(src, pf.START + D, pf.END),
                                with_features=True, store=ViewStore(tmp_path / "v"))
    with pytest.raises(IntegrityBlocker, match="another key"):
        runner.decision_pass(src, times, ViewStore(tmp_path / "v"), history_lo=pf.START + D)
    old = list(viewstore._CODE_ID)
    try:
        viewstore._CODE_ID[:] = ["0" * 64]
        with pytest.raises(IntegrityBlocker, match="another key"):
            runner.decision_pass(src, times, ViewStore(tmp_path / "v"), history_lo=pf.START)
    finally:
        viewstore._CODE_ID[:] = old
    with pytest.raises(ValueError, match="outside the universe window"):
        full(pf.END + 5 * MIN)
    # m2: a W_HOLD decision is refused (K9-logged) before any store access
    with pytest.raises(guard.HoldoutBlocked):
        runner.decision_pass(src, [windows.W_HOLD[0] + 5 * MIN], ViewStore(tmp_path / "w"),
                             history_lo=pf.START)
    assert sorted(p.name for p in (tmp_path / "w").iterdir()) == [viewstore.KEY_FILE]
    u = runner.ResearchUniverse.for_decisions(src, pf.START + 90 * D, pf.START + 91 * D, history_lo=pf.START)
    assert 0 < len(u.inputs["AAAUSDT"].bars1m) < len(inputs["AAAUSDT"].bars1m)
    assert len(u.inputs["TRNUSDT"].bars1m) == 0
    assert u.inputs["GAPUSDT"].aggtrades is None
    u2 = runner.ResearchUniverse.for_decisions(src, pf.START + 100 * D, pf.START + 101 * D,
                                               history_lo=pf.START)
    prem = u2.inputs["GAPUSDT"].r4.premium["open_ts"]  # loaded from day 65, inside the hole: the anchor only
    assert len(prem) == 1 and int(prem.iloc[0]) < pf.START + 60 * D
    with pytest.raises(ValueError, match="decisions-only"):
        u.leg_data("AAAUSDT", pf.START + 90 * D)
    with pytest.raises(ValueError, match="decisions-only"):
        u.engine_inputs(lambda t: None, pf.NOW)  # type: ignore[arg-type,return-value]
    with pytest.raises(ValueError, match="strictly increasing"):
        runner.ResearchUniverse.for_decisions(fx.LedgeredSource(pf.build_inputs()), pf.START + 90 * D,
                                              pf.START + 91 * D, history_lo=pf.START)


@pytest.mark.parametrize("seed", [21, 22])
def test_provider_views_equal_reference_random_universe(seed):
    """m3: randomized universes (other seeds of the rich fixture: other prices, volumes, funding rates)."""
    u = runner.ResearchUniverse(pf.build_inputs(seed))
    new, old = runner.DecisionProvider(u, with_features=True), _ref_provider(u, True)
    for t in [pf.START + d * D + h * H + 5 * MIN for d, h in ((63, 0), (70, 8), (77, 16), (84, 0))]:
        same(new(t), old(t))
        same(new.record(t), old._records[t])


def test_decision_pass_from_the_raw_store_equals_the_full_window(tmp_path):
    """The store-backed pass (``StoreSource.load_decision_inputs``: anchors from ``TableLoader.last_before``)
    stores the views of a ``from_source`` universe over the whole window; the key binds the store's
    manifests."""
    from luna.v7 import k9, rawstore

    with k9.using(k9.K9Log(tmp_path / "k9")):
        st = rawstore.RawStore(tmp_path / "raw")
        meta = pf.scale_store(st, 4, 75, notice_every=2)
        src = rawstore.StoreSource(st, meta, budget_bytes=8 * 2**20)
        end = pf.START + 75 * D
        ts = [int(t) for t in windows.decision_times(end - 8 * D, end - D)][::2]
        vs = ViewStore(tmp_path / "v")
        assert runner.decision_pass(src, ts, vs, history_lo=pf.START, chunk_ns=2 * D)["chunks"] >= 3
        assert vs.body["data"]["raw"] == st.manifest_sha256s()
        full = runner.DecisionProvider(runner.ResearchUniverse.from_source(src, pf.START, end, lazy=False),
                                       with_features=True)
        for t in ts:
            v, r = vs.get(t)
            same(v, full(t))
            same(r, full.record(t))
        a = src.bars["R3"].last_before("S001USDT", pf.START + 40 * D, pf.START)
        assert len(a) == 1 and int(a["open_ts"].iloc[0]) == pf.START + 40 * D - Q
        assert len(src.bars["R3"].last_before("S001USDT", pf.START, pf.START - D)) == 0
