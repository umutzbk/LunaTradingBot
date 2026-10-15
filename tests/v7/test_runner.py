"""Research runner (Task 19b): decision provider, fold-model routing, label panel, S-10.6 / S-10.7 driver,
holdout-input assembly, DT4 record, and the user's "the runner CANNOT ..." properties (synthetic data only).

Most tests use a 6-candidate subset of the seeded fixture universe (``tests.v7.fixtures_v7``) and the A0 arm
or picklable fake fold models (``FakeFold``) so the books run in seconds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import (
    cv,
    features,
    guard,
    leakage,
    models,
    opening,
    runner,
    runrecord,
    selection,
    verdict,
    windows,
)
from luna.v7.arms import arm_scores, families
from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance
from luna.v7.data.notices import NoticeVersion
from luna.v7.data.terminal import TerminalEvent
from luna.v7.features import FEATURES
from luna.v7.runner import DecisionProvider, FoldScoreModel, LabelPanel, ResearchUniverse
from luna.v7.runner_select import (
    dev_leakage_checks,
    dev_selection,
    dev_selection_record,
    holdout_inputs,
    holdout_verdict,
    missing_funding_rate,
    verify_dev_selection,
)
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.windows import Fold
from tests.v7 import auth_fixtures as af
from tests.v7 import fixtures_v7 as fx
from tests.v7 import leak_fixtures as lf

BTC = fx.BTC
SHORTS = ("AAAUSDT", "BBBUSDT", "CCCUSDT")  # positive funding: A0 shorts them
LONGS = ("EEEUSDT", "FFFUSDT", "GGGUSDT")  # negative funding: A0 is long
SUB = (BTC, *SHORTS, *LONGS)
REL_WIN = (to_ns("2022-04-07"), to_ns("2022-04-11"))  # RELUSDT's instance 0 ends inside (dormancy 04-10)
LO = fx.BOOK[0]
T = LO + D + 5 * MIN  # a decision (2022-12-06 00:05) with dense 1m bars and a day of history in BOOK
NOW = fx.NOW
B_SMALL = 200


@pytest.fixture(scope="module")
def inputs() -> dict[str, runner.SymbolInputs]:
    return fx.build_inputs(7, dense=(fx.BOOK, REL_WIN))


def uni(inputs, syms=SUB, **repl) -> ResearchUniverse:
    """A universe of ``syms``; ``repl`` maps a symbol to replaced ``SymbolInputs`` fields."""
    return ResearchUniverse({s: (replace(inputs[s], **repl[s]) if s in repl else inputs[s]) for s in syms})


def scaled(x: runner.SymbolInputs, from_ts: int, factor: float) -> dict:
    """Prices of every bar opening at or after ``from_ts`` multiplied by ``factor`` (15m, 1m, mark, index)."""
    def sc(df, cols):
        df = df.copy()
        m = df["open_ts"].to_numpy() >= from_ts
        for c in cols:
            df.loc[m, c] = df.loc[m, c] * factor
        return df
    r4 = features.R4Bars(x.r4.premium, sc(x.r4.mark, ["c"]), sc(x.r4.index, ["c"]))
    return dict(bars15=sc(x.bars15, ["o", "h", "l", "c"]), bars1m=sc(x.bars1m, ["o", "h", "l", "c"]), r4=r4)


@dataclass
class FakeFold:
    """Picklable stand-in for a ``cv.FoldModel``: ranks by sign * carry (carry = -side * FL30); the hurdle
    passes only where sign * carry > 0, so sign +1 builds the carry book and sign -1 the opposite book."""

    fold: Fold
    horizon: int
    sign: float

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        if "fl30_r8" in df.columns:  # decision candidates
            carry = -df["side"].to_numpy(np.float64) * df["fl30_r8"].to_numpy(np.float64)
        else:  # label samples: f1_fl30 is already side-adjusted (-side * FL30)
            carry = df["f1_fl30"].to_numpy(np.float64)
        out = pd.DataFrame(index=df.index)
        for fam in ("en", "lgb_reg"):
            out[f"yhat_{fam}"] = self.sign * carry * 1e4
            out[f"yhat_cal_{fam}"] = self.sign * carry * 1e4
        for fam in ("logit_en", "lgb_clf"):
            out[f"p_{fam}"] = 0.6
            out[f"p_{fam}_cal"] = 0.6
        return out


class _FamilyFold:
    """A FakeFold with the ``cv.FoldModel`` family maps (``reg`` / ``clf`` keyed by family)."""

    def __init__(self, inner: FakeFold, reg: dict, clf: dict) -> None:
        self.inner, self.horizon, self.reg, self.clf = inner, inner.horizon, reg, clf

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.inner.predict(df)


class NoP1Cal:
    """A fold model whose output lacks A1's calibrated classifier column (fail closed)."""

    def __init__(self, f: Fold) -> None:
        self.fold, self.horizon = f, 24

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        return FakeFold(self.fold, 24, 1.0).predict(df).drop(columns="p_logit_en_cal")


def fold(i: int, lo: int, hi: int) -> Fold:
    return Fold(i, lo, hi, (windows.W_DEV[0], lo - 120 * D), (lo - 120 * D, lo - 60 * D), (lo - 60 * D, lo))


def fakes(qs, signs=None, h=24) -> list[FakeFold]:
    signs = signs or [1.0] * len(qs)
    return [FakeFold(fold(i, lo, hi), h, s) for i, ((lo, hi), s) in enumerate(zip(qs, signs, strict=True))]


def entries(run) -> pd.DataFrame:
    f = run.fills
    return f[(f["reason"] == "entry") & (f["role"] == "leg")] if len(f) else f


# ------------------------------------------------------------------------------------ feature speed-up


def _compute_row_reference(sd, btc, universe_rows, t_ns, side, *, r4):
    """Verbatim copy of ``features.compute_row`` at 03be9d3 (before ``compute_rows``)."""
    from luna.v7.beta import beta_hat, hourly_known_at_from_15m
    from luna.v7.data.instances import current_instance
    from luna.v7.features import (
        CARRY_DIRECTION,
        F14_COLS,
        NAN,
        F,
        FeatureRow,
        _capped_ratio,
        _cross_section,
        _f,
        _hourly,
        _instance_rows,
        _ret,
        _rvol,
        _symbol,
        assert_pit,
    )
    from luna.v7.universe import pit_rows

    t_ns = int(t_ns)
    raw, used = _symbol(sd, r4, t_ns, xs_only=False)
    v: dict[str, float] = {"side": float(side)}
    for k in ("fl1", "fl3", "fl7", "fl30"):
        v[f"f1_{k}"] = raw.get(k, NAN)
    for k in ("premium_mean_8h", "premium_mean_24h", "premium_last"):
        v[f"f5_{k}"] = raw.get(k, NAN)
    for c in FEATURES:
        if c in raw:
            v[c] = raw[c]
    bcur = current_instance(btc.insts, t_ns)
    bb = _instance_rows(btc.bars15, bcur, t_ns, "btc.bars15") if bcur is not None else btc.bars15.iloc[0:0]
    if bcur is None:
        pit_rows(btc.bars15, t_ns, "btc.bars15")
    used["btc_bars15"] = bb
    bhc = _hourly(bb)
    v["f11_btc_ret_30d"] = _ret(bhc, t_ns, 30 * D)
    v["f11_btc_rvol_7d"] = _rvol(bhc, t_ns, 7 * D)
    resid = NAN
    ab = used.get("bars15")
    if ab is not None and len(ab) and len(bb):
        beta = beta_hat(_hourly(ab), bhc, t_ns, hourly_known_at_from_15m(ab), hourly_known_at_from_15m(bb))
        ra, rb = raw.get("f10_ret_7d", NAN), _ret(bhc, t_ns, 7 * D)
        if beta is not None:
            resid = ra - beta * rb
    v["f10_resid_ret_7d"] = _f(resid)
    v.update(_cross_section(universe_rows, t_ns, sd.symbol, raw))
    v["f13_rvol_over_carry"] = _capped_ratio(
        raw.get("f9_rvol_7d", NAN), abs(raw.get("fl7", NAN)) * F.f13_settlements_per_year
    )
    v["f13_r8_std_over_fl30"] = _capped_ratio(raw.get("r8_std_30d", NAN), abs(raw.get("fl30", NAN)))
    c = FROZEN.costs
    cost = 2.0 * FROZEN.fees.taker + 2.0 * raw.get("_h0", NAN) + 2.0 * c.slip
    v["f14_cost_bps"] = cost * 1e4
    for h, col in zip(FROZEN.horizons_h, F14_COLS, strict=True):
        v[col] = abs(raw.get("fl7", NAN)) * 3.0 * h / 24.0 / cost
    for k in FEATURES:
        v.setdefault(k, NAN)
    out = {k: (-side * v[k] if k in CARRY_DIRECTION else v[k]) for k in FEATURES}
    out = {k: _f(x) for k, x in out.items()}
    ka = assert_pit(used, t_ns)
    xs_ka = [r.max_known_at for r in universe_rows if r.max_known_at is not None]
    ka = max([x for x in (ka, *xs_ka) if x is not None], default=None)
    return FeatureRow(out, ka)


def test_compute_rows_equals_per_side_reference(inputs):
    u = uni(inputs, syms=(BTC, "AAAUSDT", "EEEUSDT", fx.RELIST, fx.TRANS))
    p = DecisionProvider(u, with_features=False)
    for t in (T, T + 8 * H, to_ns("2022-04-20 08:05"), to_ns("2022-06-01 16:05")):
        xs = p.xs(t)
        for s in u.candidates:
            both = features.compute_rows(u.sd[s], u.sd[BTC], xs, t, (1, -1), r4=u.r4(s))
            for side in (1, -1):
                ref = _compute_row_reference(u.sd[s], u.sd[BTC], xs, t, side, r4=u.r4(s))
                got = both[side]
                assert got.max_known_at == ref.max_known_at
                assert list(got.values) == list(ref.values)
                np.testing.assert_array_equal(np.array(list(got.values.values())),
                                              np.array(list(ref.values.values())))


# -------------------------------------------------------------------------------------------- provider


def _spike_after(x: runner.SymbolInputs, cut: int, notice: NoticeVersion | None = None) -> dict:
    """Every row known after ``cut`` replaced by absurd values (prices x 3, volumes x 100, rates x 50)."""
    def sp(df, cols, k):
        df = df.copy()
        m = df["known_at"].to_numpy() > cut
        for c in cols:
            df.loc[m, c] = df.loc[m, c] * k
        return df
    r4 = features.R4Bars(sp(x.r4.premium, ["c"], 3.0), sp(x.r4.mark, ["c"], 3.0), sp(x.r4.index, ["c"], 3.0))
    out = dict(bars15=sp(x.bars15, ["o", "h", "l", "c", "vol_base", "vol_quote"], 3.0),
               bars1m=sp(x.bars1m, ["o", "h", "l", "c"], 3.0), funding=sp(x.funding, ["rate"], 50.0), r4=r4)
    if notice is not None:
        out["notices"] = (notice,)
    return out


def _notice(sym: str, known: int) -> NoticeVersion:
    return NoticeVersion("n1", 1, "delisting", known - H, known,
                         f"Binance Futures will delist the USDⓈ-M {sym} perpetual contract", "0" * 64)


def _views_equal(a, b) -> None:
    assert a.t == b.t and a.stale == b.stale
    assert dict(a.gates) == dict(b.gates)
    for k in ("h_hat", "betas", "qv24"):
        assert dict(getattr(a, k)) == dict(getattr(b, k)), k
    pd.testing.assert_frame_equal(a.cands, b.cands, check_exact=True)


def test_provider_pit(inputs):
    """A future spike in any input (bars 15m / 1m, funding, R4, a notice known after t) changes nothing at t;
    the same spike on a row known exactly at t - 1 min (or a notice known before t) does."""
    base = DecisionProvider(uni(inputs), with_features=True)(T)
    assert len(base.cands) > 0
    cut = T - MIN
    spiked = {s: _spike_after(inputs[s], cut) for s in SUB}
    spiked["AAAUSDT"] = _spike_after(inputs["AAAUSDT"], cut, _notice("AAAUSDT", T))  # known at t: too late
    _views_equal(base, DecisionProvider(uni(inputs, **spiked), with_features=True)(T))
    # discrimination: rows known by t - 1 min do enter (bars of the last 15 minutes, a notice known earlier)
    early = {s: _spike_after(inputs[s], T - 20 * MIN) for s in SUB}
    v2 = DecisionProvider(uni(inputs, **early), with_features=False)(T)
    assert v2.qv24 != base.qv24 and v2.h_hat != base.h_hat
    v3 = DecisionProvider(uni(inputs, AAAUSDT=dict(notices=(_notice("AAAUSDT", T - 2 * MIN),))),
                          with_features=False)(T)
    assert v3.gates["AAAUSDT"].u6 is False and "AAAUSDT" not in set(v3.cands["symbol"])


def test_provider_qv24_window(inputs):
    """qv24 = sum of vol_quote of the current instance's 15m bars closing in (t - 24 h - 1 min, t - 1 min]."""
    x = inputs["AAAUSDT"]
    b = x.bars15
    close = b["open_ts"].to_numpy() + 15 * MIN
    inside = (close > T - D - MIN) & (close <= T - MIN)
    p = DecisionProvider(uni(inputs, syms=(BTC, "AAAUSDT")), with_features=False)
    q, ka = p.qv24("AAAUSDT", T)
    assert q == pytest.approx(float(b.loc[inside, "vol_quote"].sum()), rel=1e-15) and inside.sum() == 96
    assert ka == T - 5 * MIN  # the last bar used closes at 00:00, known at 00:00
    # edges: the bar closing at t - 24 h - 5 min is outside, the one closing at t - 24 h + 10 min inside
    for close_ts, counted in ((T - D - 5 * MIN, False), (T - D + 10 * MIN, True), (T + 10 * MIN, False)):
        b2 = b.copy()
        b2.loc[close == close_ts, "vol_quote"] += 1e12
        q2, _ = DecisionProvider(uni(inputs, syms=(BTC, "AAAUSDT"), AAAUSDT=dict(bars15=b2)),
                                 with_features=False).qv24("AAAUSDT", T)
        assert (q2 - q > 1e11) is counted, close_ts
    # after the relisting only instance 1's bars count; no bar in the window -> missing (None), never 0
    pr = DecisionProvider(uni(inputs, syms=(BTC, fx.RELIST)), with_features=False)
    i0, i1 = inputs[fx.RELIST].insts
    t_new = i1.first_bar_ts + 12 * H + 5 * MIN
    rb = inputs[fx.RELIST].bars15
    rc = rb["open_ts"].to_numpy() + 15 * MIN
    m = (rc > t_new - D - MIN) & (rc <= t_new - MIN) & (rb["open_ts"].to_numpy() >= i1.first_bar_ts)
    assert pr.qv24(fx.RELIST, t_new)[0] == pytest.approx(float(rb.loc[m, "vol_quote"].sum()), rel=1e-15)
    t_gap = fx.DORMANT[0] + 6 * D + 5 * MIN  # bars missing for > 24 h, instance 0 still current (PIT)
    assert pr.instance_bars(fx.RELIST, t_gap) is not None and pr.qv24(fx.RELIST, t_gap) == (None, None)


def test_provider_staleness(inputs):
    """S-7.3 staleness (AMB-25 (b)): the hedge's last 15m bar or its h-hat missing -> stale, entries skipped
    at that decision through the engine; the next fresh decision enters."""
    p = DecisionProvider(uni(inputs), with_features=False)
    assert p.stale_reasons(T) == () and p(T).stale is False
    b = inputs[BTC].bars15
    last = b["open_ts"].to_numpy() == T - 5 * MIN - 15 * MIN
    u1 = uni(inputs, **{BTC: dict(bars15=b[~last].reset_index(drop=True))})
    p1 = DecisionProvider(u1, with_features=False)
    assert p1.stale_reasons(T) == ("hedge_last_bar_missing",) and p1(T).stale is True
    recent = (b["open_ts"].to_numpy() >= T - 26 * H) & (b["open_ts"].to_numpy() < T)
    p2 = DecisionProvider(uni(inputs, **{BTC: dict(bars15=b[~recent].reset_index(drop=True))}),
                          with_features=False)
    assert set(p2.stale_reasons(T)) == {"hedge_last_bar_missing", "hedge_h_hat_missing"}
    # through the engine: no entry at the stale decision, entries at the next one
    run = runner.run_book("A0", u1, p1, (T - 5 * MIN, T + D), 168, NOW)
    d0, d1 = run.decisions.iloc[0], run.decisions.iloc[1]
    assert d0["stale"] and not any(o.startswith("open:") for o in d0["orders"])
    assert not d1["stale"] and any(o.startswith("open:") for o in d1["orders"])
    assert entries(run)["ts"].min() == T + 8 * H + 15 * MIN


# ---------------------------------------------------------------------------- U1-U8 cannot be bypassed


def _gate_breaks(inputs) -> dict[str, dict]:
    x = inputs["AAAUSDT"]
    b = x.bars15.copy()
    ot = b["open_ts"].to_numpy()
    f = x.funding
    fts = f["funding_ts"].to_numpy()
    u2 = b.copy()
    u2.loc[ot >= T - 40 * D, "vol_quote"] *= 1e-3
    drop = np.flatnonzero((ot >= T - 30 * H) & (ot < T + D))[::8]
    u5 = b.copy()
    m5 = ot >= T - 3 * D
    u5.loc[m5, "h"] = u5.loc[m5, "c"] * 1.02
    u5.loc[m5, "l"] = u5.loc[m5, "c"]
    u8 = b.copy()
    u8.loc[(ot >= T - 2 * H) & (ot < T + D), "vol_base"] = 0.0
    gap = int(np.flatnonzero(fts >= T - 3 * D)[0])
    return {
        "u1": dict(insts=[Instance(0, T - 10 * D, None, None)]),
        "u2": dict(bars15=u2),
        "u3": dict(funding=f.drop(index=f.index[gap]).reset_index(drop=True)),
        "u4": dict(bars15=b.drop(index=b.index[drop]).reset_index(drop=True)),
        "u5": dict(bars15=u5),
        "u6": dict(notices=(_notice("AAAUSDT", T - D),)),
        "u7": dict(funding=f[fts < T - 2 * D].reset_index(drop=True)),
        "u8": dict(bars15=u8),
    }


@pytest.fixture(scope="module")
def baseline_a0(inputs):
    u = uni(inputs)
    return runner.run_book("A0", u, DecisionProvider(u, with_features=False), (T - 5 * MIN, T + D), 168, NOW)


@pytest.mark.parametrize("gate", ["u1", "u2", "u3", "u4", "u5", "u6", "u7", "u8"])
def test_gates_cannot_be_bypassed(inputs, baseline_a0, gate):
    """A symbol failing any of U1-U8 is never a candidate and never entered (baseline: it is entered)."""
    assert "AAAUSDT" in set(entries(baseline_a0)["symbol"])
    u = uni(inputs, AAAUSDT=_gate_breaks(inputs)[gate])
    p = DecisionProvider(u, with_features=False)
    v = p(T)
    assert getattr(v.gates["AAAUSDT"], gate) is False
    assert "AAAUSDT" not in set(v.cands["symbol"])
    run = runner.run_book("A0", u, p, (T - 5 * MIN, T + D), 168, NOW)
    assert "AAAUSDT" not in set(entries(run)["symbol"]) and len(entries(run)) > 0


# ---------------------------------------------------------------------------------------- fold routing


def test_fold_model_routing_by_quarter():
    q = [(LO, LO + 2 * D), (LO + 2 * D, LO + 4 * D)]
    m = FoldScoreModel(fakes(q, [1.0, -1.0]), "A2", 24)
    assert m.classifier == "logit_en" and m.regressor == "en" and m.ml_star == "A2"
    assert FoldScoreModel(fakes(q), "A3", 24).classifier == "lgb_clf"
    cands = pd.DataFrame(dict(symbol=["A", "A"], side=[1, -1], gate_ok=True, fl30_r8=[1e-3, 1e-3],
                              cost_side=1e-3))
    a, b = m.predict(LO + 5 * MIN, cands), m.predict(LO + 2 * D + 5 * MIN, cands)
    np.testing.assert_array_equal(a["yhat_en"].to_numpy(), -b["yhat_en"].to_numpy())
    assert m.log[LO + 5 * MIN][0] == 0 and m.log[LO + 2 * D + 5 * MIN][0] == 1
    assert m.fold_index(LO) == 0 and m.fold_index(LO + 2 * D) == 1  # a quarter's first instant is its own
    assert m.fold_index(LO + 2 * D - 1) == 0
    assert m.hashes[0] != m.hashes[1]
    for t in (LO - 5 * MIN, LO + 4 * D):  # outside every test quarter
        with pytest.raises(ValueError, match="outside every fold"):
            m.predict(t, cands)
    with pytest.raises(ValueError, match="overlap"):
        FoldScoreModel(fakes([(LO, LO + 2 * D), (LO + D, LO + 3 * D)]), "A2", 24)
    with pytest.raises(ValueError, match="h = 72"):
        FoldScoreModel(fakes(q, h=72), "A2", 24)
    with pytest.raises(ValueError):
        FoldScoreModel(fakes(q), "A1", 24)


def test_wrong_fold_model_cannot_be_used(inputs):
    """Two folds whose models build opposite books: entries decided in quarter q follow fold q's ranking, and
    the provenance log names fold q's model for every decision of quarter q."""
    q = [(LO, LO + 2 * D), (LO + 2 * D, fx.BOOK[1])]
    u = uni(inputs)
    p = DecisionProvider(u, with_features=True)
    m = FoldScoreModel(fakes(q, [1.0, -1.0]), "A2", 24)
    run = runner.run_book("A2", u, p, fx.BOOK, 24, NOW, model=m)
    assert run.valid
    e = entries(run)
    dec_t = e["position_id"].str.split("@").str[1].astype("int64")
    for t, grp in e.groupby(dec_t):
        i = 0 if t < q[1][0] else 1
        expect = FakeFold(fold(i, *q[i]), 24, [1.0, -1.0][i]).predict(p(t).cands)
        sc = arm_scores("A2", pd.concat([p(t).cands, expect], axis=1), 24.0)
        ok = {(r.symbol, r.side) for r in sc.itertuples() if r.passes_hurdle}
        assert {(s, d) for s, d in zip(grp["symbol"], grp["direction"], strict=True)} <= ok
    sides = {i: {(s, d) for s, d, t in zip(e["symbol"], e["direction"], dec_t, strict=True)
                 if (t >= q[1][0]) == bool(i)} for i in (0, 1)}
    assert ("AAAUSDT", -1) in sides[0] and ("AAAUSDT", 1) in sides[1]  # opposite books really happened
    log = runner.decision_log(run, p, m)
    for r in log.itertuples():
        i = 0 if r.t < q[1][0] else 1
        assert r.fold_id == i and r.model_hash == m.hashes[i]
        assert r.max_known_at <= r.t - MIN


# ----------------------------------------------------------------------------- risk through the runner


def _shocked_run(inputs, factors: dict[str, float], tau: int, h: int = 168):
    u = uni(inputs, **{s: scaled(inputs[s], tau, k) for s, k in factors.items()})
    return runner.run_book("A0", u, DecisionProvider(u, with_features=False), fx.BOOK, h, NOW)


def test_risk_layer_cannot_be_bypassed(inputs):
    """Kill, leg stop and the daily-loss pause act through the runner path."""
    tau = LO + 12 * H
    kill = _shocked_run(inputs, {**{s: 1.6 for s in SHORTS}, **{s: 0.4 for s in LONGS}}, tau)
    assert kill.valid
    dk = kill.decisions[kill.decisions["t"] > tau]
    assert dk["killed"].all() and (kill.fills["reason"] == "kill").sum() >= len(SUB) - 1
    assert (entries(kill)["ts"] < tau).all()  # no re-entry after the kill
    stop = _shocked_run(inputs, {"AAAUSDT": 1.2}, tau)
    f = stop.fills
    st = f[(f["reason"] == "leg_stop") & (f["symbol"] == "AAAUSDT")]
    assert len(st) == 1 and st["ts"].iloc[0] > tau
    re = entries(stop)
    assert not ((re["symbol"] == "AAAUSDT") & (re["ts"] > tau) & (re["ts"] < tau + D)).any()  # cooldown
    pause = _shocked_run(inputs, {**{s: 1.1 for s in SHORTS}, **{s: 0.9 for s in LONGS}}, tau)
    dp = pause.decisions[(pause.decisions["t"] > tau) & (pause.decisions["t"] < LO + D)]
    assert dp["paused"].all() and not dp["killed"].any()
    assert not (pause.fills["reason"] == "leg_stop").any()


def test_one_sided_book_is_flattened_at_next_executable_opportunity(inputs):
    """AMB-16 (d) (user decision 2026-10-15, CLARIFICATIONS-2026-10-15-round1.md; supersedes the interim
    AMB-28 "next decision" reading): when one side has no holdable leg, the whole book is flattened at the
    next executable opportunity, even inside the minimum hold: the remaining legs exit as forced exits with
    the S-4.2 timing of the tick that emptied the side; no replacement keeps the other side alone."""
    tau = LO + 12 * H
    run = _shocked_run(inputs, {s: 0.8 for s in LONGS}, tau, h=168)  # every long leg stops out
    assert run.valid
    f = run.fills
    stops = f[f["reason"] == "leg_stop"]
    assert set(stops["symbol"]) == set(LONGS)
    flat = f[(f["reason"] == "one_sided_flatten") & f["symbol"].isin(SHORTS)]
    assert set(flat["symbol"]) == set(SHORTS) and (flat["liquidity"] == "taker").all()
    assert (flat["ts"] < flat["entry_decision_ts"] + 168 * H).all()  # inside the minimum hold
    # the same S-4.2 fill bar as the leg stops (first 1m bar after the emptying tick + 1 min), long before the
    # next decision (16:05) would fill at 16:20
    assert set(flat["ts"]) == set(stops["ts"]) and len(set(flat["ts"])) == 1
    flat_from = int(flat["ts"].max())
    assert tau < flat_from < tau + H
    assert not (f["reason"] == "neutrality").any()
    cool_end = int(stops["ts"].max()) + D
    pos = run.positions
    legs = pos[(pos["role"] == "leg") & (pos["ts"] > flat_from) & (pos["ts"] < cool_end)]
    assert len(legs) == 0, "a one-sided book was carried"
    assert not ((entries(run)["ts"] > tau) & (entries(run)["ts"] < cool_end)).any()


# ---------------------------------------------------------------------------------------------- labels


def test_label_panel_schema_and_invalid_rate(inputs):
    ts = [int(t) for t in fx.sample_times()[60:62]]
    x = inputs["BBBUSDT"]
    f = x.funding
    t_e = ts[0] + 15 * MIN
    keep = ~((f["funding_ts"] > t_e) & (f["funding_ts"] <= t_e + 24 * H))
    u = uni(inputs, BBBUSDT=dict(funding=f[keep].reset_index(drop=True)))
    p = DecisionProvider(u, with_features=True)
    panel = runner.label_panel(u, p, ts, 24, windows.W_DEV, NOW)
    s = panel.samples
    assert set(cv.REQUIRED) | {"net"} <= set(s.columns) and (s["h"] == 24).all()
    cv.canonical(s, 24)  # valid R2 samples
    assert np.isfinite(s["y_reg"]).all() and set(s["y_pos"]) <= {0.0, 1.0}
    assert dict(panel.invalid_by_reason).get("funding_missing", 0) >= 2
    assert not ((s["symbol"] == "BBBUSDT") & (s["decision_ts"] == ts[0])).any()
    assert panel.n_labels == len(s) + panel.n_invalid
    assert panel.invalid_rate == pytest.approx(panel.n_invalid / panel.n_labels)
    assert panel.invalid_rate == panel.labels["invalid_reason"].notna().mean()
    assert (s["label_known_at"] > s["decision_ts"] + 24 * H).all()
    with pytest.raises(guard.HoldoutBlocked):  # the outcome guard runs before anything is read
        runner.label_panel(u, p, [windows.W_HOLD[0] + 5 * MIN], 24, windows.W_HOLD, NOW)
    with pytest.raises(ValueError, match="features"):
        runner.label_panel(u, DecisionProvider(u, with_features=False), ts, 24, windows.W_DEV, NOW)


# --------------------------------------------------------------------------------- temporal boundaries


def _synthetic_samples(seed: int = 3) -> pd.DataFrame:
    g = np.random.default_rng(seed)
    days = np.arange(to_ns("2022-01-03"), to_ns("2022-12-31"), 2 * D) + 5 * MIN
    rows = []
    for t in days:
        for i, sym in enumerate(("S1USDT", "S2USDT", "S3USDT", "S4USDT", "S5USDT", "S6USDT")):
            for side in (1, -1):
                x = g.normal(size=len(FEATURES))
                y = 30.0 * x[3] * side + 10.0 * g.normal() + i
                rows.append(dict(zip(FEATURES, x, strict=True)) | dict(
                    side=side, decision_ts=int(t), symbol=sym, h=24, label_known_at=int(t + 25 * H),
                    y_reg=y, y_pos=float(y > 0), net=y / 1e4))
    return pd.DataFrame(rows)


@pytest.fixture
def tiny_space(monkeypatch):
    monkeypatch.setattr(models, "search_space", lambda h: list(fx.TINY_SPACE))


def test_temporal_boundaries_cannot_be_crossed(tiny_space):
    """Purge / blocks through the runner: labels known after their block's purge boundary, and test-quarter
    labels, never reach a fit (changing them changes nothing); a changed admissible label does."""
    s = _synthetic_samples()
    [base] = runner.fit_fold_models(s, [fx.FOLD], 24)

    def refit(mask):
        s2 = s.copy()
        s2.loc[mask, "y_reg"] += 1e4
        s2.loc[mask, "y_pos"] = 1.0 - s2.loc[mask, "y_pos"]
        [m] = runner.fit_fold_models(s2, [fx.FOLD], 24)
        return m.test_pred

    ts, lka = s["decision_ts"].to_numpy(), s["label_known_at"].to_numpy()
    purged = (ts < fx.FOLD.sel[0]) & (lka > fx.FOLD.sel[0] - FROZEN.cv.purge_ns)
    purged |= (ts >= fx.FOLD.sel[0]) & (ts < fx.FOLD.cal[0]) & (lka > fx.FOLD.cal[0] - FROZEN.cv.purge_ns)
    purged |= (ts >= fx.FOLD.cal[0]) & (ts < fx.FOLD.test_lo) & (lka > fx.FOLD.test_lo - FROZEN.cv.purge_ns)
    test_q = ts >= fx.FOLD.test_lo
    assert purged.sum() > 0 and test_q.sum() > 0
    pd.testing.assert_frame_equal(refit(purged | test_q), base.test_pred, check_exact=True)
    admissible = (ts < fx.FOLD.sel[0] - 30 * D)
    assert not refit(admissible).equals(base.test_pred)


def test_fold_convergence_collected_and_written(tmp_path, monkeypatch):
    """A non-converging EN fit inside cv (frozen-size alpha on bps labels, collinear features) is recorded in
    FoldModel.convergence, collected by the runner and written to DT4; nothing is raised (-W error)."""
    s = _synthetic_samples()
    s[FEATURES[5]] = s[FEATURES[4]] + 1e-9 * np.arange(len(s))
    s["y_reg"] *= 100.0
    space = [models.Config("en", {"alpha": 1e-3, "l1_ratio": 0.2}), *fx.TINY_SPACE[1:]]
    monkeypatch.setattr(models, "search_space", lambda h: list(space))
    fms = runner.fit_fold_models(s, [fx.FOLD], 24)
    conv = runner.fold_convergence(fms)
    lin = [r for r in conv if r["family"] in ("en", "logit_en")]
    assert len(lin) == 4  # 2 EN candidate fits (selection) + the EN refit + the logistic EN refit
    assert all(isinstance(r["converged"], bool) for r in lin)
    assert all(r["converged"] is None for r in conv if r["family"] in ("lgb_reg", "lgb_clf"))
    with pytest.raises(ValueError, match="convergence record"):  # never a silent omission
        runner.fold_convergence([FakeFold(fold(0, LO, LO + D), 24, 1.0)])
    un = runner.fold_convergence([FakeFold(fold(3, LO, LO + D), 24, 1.0)], allow_unavailable=True)
    assert len(un) == 1 and un[0]["stage"] == "unavailable" and un[0]["converged"] is None


def _record(runs_, conv=(), **kw):
    base = dict(commit="a" * 40, dirty=False, config={"h": 24}, data_manifests={"bars15": "b" * 64},
                seeds=runrecord.default_seeds(), uv_lock_sha256="c" * 64,
                libraries=runrecord.library_versions(), convergence=conv, runs=runs_)
    return runrecord.run_record(**{**base, **kw})


def test_dt4_run_record(tmp_path, dev_valid):
    """DT4 matches data.schema (run_id key, the schema columns), records the working-tree state, the run
    identities with output sha256s and the convergence rows; run_id is deterministic and content-bound."""
    from luna.v7.data import schema

    dev, _ = dev_valid
    runs = [dev.runs[("A0", 24)], dev.runs[("A2", 24)]]
    conv = [dict(fold_id=0, horizon=24, stage="select", family="en", params=(("alpha", 0.001),),
                 converged=False, n_iter=1000, max_iter=1000, messages=("did not converge",))]
    rec = _record(runs, conv)
    assert set(schema.TABLES["DT4"].key) | set(schema.TABLES["DT4"].columns) <= set(rec)
    assert rec["code_dirty"] is False and rec["convergence_summary"] == {"fits": 1, "not_converged": 1}
    ids = rec["runs"]
    assert [(r["arm"], r["h"], r["execution"], r["layer"]) for r in ids] == [("A0", 24, "E_T", "L-norm"),
                                                                             ("A2", 24, "E_T", "L-norm")]
    assert ids[0]["window"] == list(dev.window)
    assert ids[0]["output_sha256"] == runrecord.run_output_sha256(runs[0])
    assert _record(runs, conv)["run_id"] == rec["run_id"]  # deterministic
    other = replace(runs[0], integrity=(*runs[0].integrity, "x"))
    assert runrecord.run_output_sha256(other) != ids[0]["output_sha256"]
    assert _record([other, runs[1]], conv)["run_id"] != rec["run_id"]
    # DK-114 / m10: run_book output is a research run, so its record is a research record whatever the
    # caller passes, and a dirty working tree is refused
    assert all(r.research for r in runs) and rec["research"] is True
    assert _record(runs, conv, research=False)["research"] is True
    for kw in (dict(dirty=True), dict(dirty=True, research=True), dict(dirty=True, research=False)):
        with pytest.raises(ValueError, match="clean working tree"):
            _record(runs, conv, **kw)
    plain = [replace(r, research=False) for r in runs]  # e.g. a direct engine.run (not a research entry)
    assert _record(plain, conv, dirty=True)["run_id"] != _record(plain, conv)["run_id"]
    for bad in (dict(runs=[]), dict(commit="xyz"), dict(data_manifests={}), dict(uv_lock_sha256="no"),
                dict(dirty="no")):
        with pytest.raises(ValueError):
            _record(runs, conv, **bad)
    commit, dirty = runrecord.code_state()
    assert len(commit) == 40 and isinstance(dirty, bool)
    p = runrecord.write_run_record(tmp_path / "run.json", rec)
    assert json.loads(p.read_text(encoding="utf-8")) == json.loads(runrecord.canonical_json(rec))
    with pytest.raises(FileExistsError):  # a record is never overwritten
        runrecord.write_run_record(tmp_path / "run.json", rec)


# ------------------------------------------------------------------------------------- S-10.6 / S-10.7


@pytest.fixture(scope="module")
def dev_valid(inputs):
    u = uni(inputs)
    p = DecisionProvider(u, with_features=True)
    q = [(LO, LO + D), (LO + D, LO + 2 * D)]
    fm = {h: fakes(q, h=h) for h in selection.HORIZONS}
    return dev_selection(u, p, fm, NOW, B=B_SMALL), q


@pytest.fixture(scope="module")
def opened(dev_valid, tmp_path_factory):
    """A real synthetic opening (SC-05) bound to a DevSelection with dev_valid's books, ML* and E_M runs and
    the leak_fixtures fold models (issued through the documented test hook, ``auth_fixtures``), and the real
    passed LeakageReport of those fold models."""
    dev, _ = dev_valid
    arm, h = dev.ml_star
    hooked = af.dev_selection(arm, h, runs=dev.runs, em_runs=dev.em_runs, result=dev.result,
                              window=dev.window)
    mp = pytest.MonkeyPatch()  # the guard's location seam points at this opening for the module
    try:
        yield af.authorize(tmp_path_factory.mktemp("opening"), mp, dev=hooked)
    finally:
        mp.undo()


def test_dev_selection_runs_all_required(dev_valid):
    dev, q = dev_valid
    assert verify_dev_selection(dev) and dev.sha256 == opening.content_sha256(dev_selection_record(dev))
    assert set(dev.runs) == set(selection.REQUIRED)
    assert all(r.window == (q[0][0], q[-1][1]) and r.execution == "E_T" for r in dev.runs.values())
    assert isinstance(dev.result, selection.Choice) and dev.ml_star is not None
    assert dev.fold_nets is not None and len(dev.fold_nets) == 2 and isinstance(dev.kill, tuple)
    assert dev.quarter_edges == (q[0][0], q[1][0], q[1][1])
    assert set(dev.em_runs) == {"ML*", "A0"} and all(r.execution == "E_M" for r in dev.em_runs.values())
    arm, h = dev.ml_star
    assert dev.em_runs["ML*"].arm == arm and dev.em_runs["ML*"].h_star == h == dev.em_runs["A0"].h_star
    # one continuous book: a position opened in quarter 0 is still held after the quarter boundary
    a0 = dev.runs[("A0", 168)]
    held = a0.positions[(a0.positions["ts"] > q[1][0]) & (a0.positions["role"] == "leg")]
    opened = held["position_id"].str.split("@").str[1].astype("int64")
    assert (opened < q[1][0]).any()
    for m in dev.models.values():
        assert {t for t in m.log if t >= q[1][0]} and all(m.log[t][0] == (t >= q[1][0]) for t in m.log)


def test_dev_selection_invalid_precondition(inputs, opened):
    """A held B / C terminal event (RELUSDT's instance 0 ends with no reconstructible settlement) in A0 at a
    horizon stops V7 before selection with the exact EC-2 / S-10.6 string; no kill, no E_M run. The exit is
    never filled from instance 1 bars (no cross-instance fill)."""
    syms = (BTC, "AAAUSDT", "BBBUSDT", fx.RELIST, *LONGS)
    i0 = inputs[fx.RELIST].insts[0]
    with pytest.raises(ValueError, match="TerminalEvent"):  # an unclassified cessation is never guessed
        u0 = uni(inputs, syms=syms)
        dev_selection(u0, DecisionProvider(u0, with_features=True),
                      {h: fakes([(REL_WIN[0], REL_WIN[1])], h=h) for h in selection.HORIZONS}, NOW, B=B_SMALL)
    ev = TerminalEvent(i0, int(i0.last_trade_ts), "C", None, None, None)  # no official termination record
    u = uni(inputs, syms=syms, **{fx.RELIST: dict(terminals={i0.idx: ev})})
    p = DecisionProvider(u, with_features=True)
    q = [(REL_WIN[0], REL_WIN[0] + 2 * D), (REL_WIN[0] + 2 * D, REL_WIN[1])]
    fm = {h: fakes(q, h=h) for h in selection.HORIZONS}
    dev = dev_selection(u, p, fm, NOW, B=B_SMALL)
    assert isinstance(dev.result, selection.Invalid)
    assert dev.result.reason == "INVALID RUN — historical terminal execution not reconstructible"
    bad = [r.split(":")[0] for r in dev.result.runs]
    assert "A0@72h" in bad and "A0@168h" in bad and "A0@24h" not in bad
    assert dev.kill is None and dev.fold_nets is None and dev.em_runs == {} and dev.ml_star is None
    r72 = dev.runs[("A0", 72)]
    assert r72.invalid == "INVALID RUN — terminal settlement not reconstructible"
    rel = r72.fills[r72.fills["symbol"] == fx.RELIST]
    assert len(rel) > 0 and (rel["instance"] == i0.idx).all() and (rel["ts"] < i0.last_trade_ts).all()
    with pytest.raises(ValueError, match="no ML"):
        holdout_inputs(auth=opened.auth, ml_T=r72, a0_T=r72, ml_M=r72,
                       a0_M=r72, quarter_edges=(), panel=None,
                       final_model=None, leakage_report=None, records=None, integrity=(), dev=dev)
    with pytest.raises(ValueError, match="no ML"):  # no ML*: no leakage report either (holdout sealed)
        dev_leakage_checks(dev, {}, None, **lf.PROV)


# -------------------------------------------------------------------------------------- holdout inputs


def _panel(h: int, n_days: int = 4, rate: float = 0.002) -> LabelPanel:
    s = _synthetic_samples()
    s = s[s["decision_ts"] < to_ns("2022-01-03") + n_days * 7 * D].copy()
    s["h"] = h
    return LabelPanel(h, s.reset_index(drop=True), pd.DataFrame(), len(s), 0, 0, rate, ())


def test_holdout_inputs_require_authorization(dev_valid, opened, tmp_path, monkeypatch):
    dev, q = dev_valid
    arm, h = dev.ml_star
    reg, clf = families(arm)
    final = lf.final(h, reg)  # the final model the pinned LeakageReport audited (I-2)
    kw = dict(ml_T=dev.runs[(arm, h)], a0_T=dev.runs[("A0", h)], ml_M=dev.em_runs["ML*"],
              a0_M=dev.em_runs["A0"],
              quarter_edges=(q[0][0], q[0][0] + 12 * H, q[1][0], q[1][0] + 12 * H, q[1][1]),
              panel=_panel(h), final_model=final,
              leakage_report=opened.report, records=lf.clean_records, integrity=(),
              dev_ml_run=dev.runs[(arm, h)], dev_kill=(), B=B_SMALL)
    auth = lambda: opened.auth  # noqa: E731  (the real opening of this module)
    for bad in (None, object(), {"approved": True}, object.__new__(guard.HoldoutAuthorization)):
        with pytest.raises(guard.HoldoutBlocked):
            holdout_inputs(auth=bad, **kw)
    x = holdout_inputs(auth=auth(), **kw)
    assert isinstance(x, verdict.HoldoutInputs) and x.dev_kill == () and x.k8.invalid_label_rate == 0.002
    assert x.k8.missing_funding_rate == missing_funding_rate([kw["ml_T"], kw["a0_T"]])
    assert len(x.ic[0]) == len(x.calib[0]) == len(x.a1_calib[0]) == len(kw["panel"].samples)
    assert verdict.holdout(x).cls in verdict.CLASSES  # the assembled inputs are accepted by the verdict
    # SC-02: the holdout-phase leakage report is built here over the four runs, linked to the pre report
    assert x.leakage is kw["leakage_report"] and x.holdout_leakage.pre_sha256 == x.leakage.sha256
    assert lf.leakage.covers(x.holdout_leakage, [kw[k] for k in ("ml_T", "a0_T", "ml_M", "a0_M")])
    assert x.holdout_leakage.findings == ()
    late = holdout_inputs(auth=auth(), **{**kw, "records": lambda t: lf.clean_records(t + MIN)})
    assert late.holdout_leakage.findings and verdict.holdout(late).cls in (verdict.LEAKAGE, verdict.INVALID)
    skipped = lf.run_checks(arm, h, samples={}, fold_models={}, final_model=None)
    for bad, msg in (((), "run_leakage_checks"), (replace(kw["leakage_report"], commit="c" * 40),
                                                   "run_leakage_checks"), (skipped, "did not pass")):
        with pytest.raises(ValueError, match=msg):
            holdout_inputs(auth=auth(), **{**kw, "leakage_report": bad})
    other = ("A3" if arm == "A2" else "A2", h)
    wrong = lf.run_checks(*other, samples={}, fold_models={}, final_model=None)
    with pytest.raises(ValueError, match="did not pass|ML"):
        holdout_inputs(auth=auth(), **{**kw, "leakage_report": wrong})
    with pytest.raises(ValueError, match="kill"):
        holdout_inputs(auth=auth(), **{**kw, "dev_kill": ("F_net <= 0",)})
    with pytest.raises(ValueError, match="is not"):
        holdout_inputs(auth=auth(), **{**kw, "ml_T": kw["a0_T"]})
    with pytest.raises(ValueError, match="undefined"):
        holdout_inputs(auth=auth(), **{**kw, "panel": _panel(h, rate=float("nan"))})
    # DK-114 / m10: only runs of the research entry point (run_book) are accepted
    for name in ("ml_T", "a0_T", "ml_M", "a0_M", "dev_ml_run"):
        with pytest.raises(ValueError, match="research"):
            holdout_inputs(auth=auth(), **{**kw, name: replace(kw[name], research=False)})
    # deferred 19b minor: the classifier mapping must belong to ML*'s family (A2 -> logit_en, A3 -> lgb_clf)
    other = "lgb_clf" if clf == "logit_en" else "logit_en"
    fm = FakeFold(fold(9, 0, 1), h, 1.0)
    for bad_clf in ({other: object()}, {}):
        bad = _FamilyFold(fm, reg={reg: object()}, clf=bad_clf)
        with pytest.raises(ValueError, match="classifier"):
            holdout_inputs(auth=auth(), **{**kw, "final_model": bad})
    good = _FamilyFold(fm, reg={reg: object()}, clf={clf: object(), other: object()})
    # I-2: the right families are not enough: the model scored must be the final model the report audited
    for other_model in (good, FakeFold(fold(9, 0, 1), h, 1.0)):
        with pytest.raises(ValueError, match="audited"):
            holdout_inputs(auth=auth(), **{**kw, "final_model": other_model})
    rest = {k: v for k, v in kw.items() if k not in ("dev_ml_run", "dev_kill")}
    if dev.kill:  # the real W_DEV result: a kill never opens the holdout (DEC-19)
        with pytest.raises(ValueError, match="kill"):
            holdout_inputs(auth=auth(), **rest, dev=dev)
    else:  # not the DevSelection pinned at the opening (its fold models are not the report's either)
        with pytest.raises(ValueError, match="DevSelection"):
            holdout_inputs(auth=auth(), **rest, dev=dev)
    # the pinned DevSelection itself is accepted on the dev= path
    assert isinstance(holdout_inputs(auth=auth(), **rest, dev=opened.dev), verdict.HoldoutInputs)
    # SC-05: the W_DEV ML* run, the LeakageReport and the fold models are the pinned ones on every path
    with pytest.raises(ValueError, match="pinned"):
        holdout_inputs(auth=auth(), **{**kw, "dev_ml_run": replace(kw["dev_ml_run"], invalid_detail="x")})
    content = leakage.report_content(opened.report)
    content["data_manifests"] = [["W_DEV", "d" * 64]]  # a verified, passed report for ML*, not the pinned one
    twin = leakage._load_persisted_report(content, opening.content_sha256(content))
    assert twin.passed and twin.ml_star == (arm, h) and twin.sha256 != opened.report.sha256
    with pytest.raises(ValueError, match="pinned"):
        holdout_inputs(auth=auth(), **{**kw, "leakage_report": twin})
    # M-4: a verified, passed report for another ML* reaches the ML* branch
    other_ml = lf.passed_report("A2", 72) if (arm, h) != ("A2", 72) else lf.passed_report("A2", 24)
    with pytest.raises(ValueError, match=r"is for ML\*"):
        holdout_inputs(auth=auth(), **{**kw, "leakage_report": other_ml})
    # the binding uses the canonical opening (the guard's location seam), never the authorization's record
    af.roots(monkeypatch, af.frozen_repo(tmp_path / "elsewhere"), tmp_path / "elsewhere_home")
    with pytest.raises(guard.HoldoutBlocked):
        holdout_inputs(auth=auth(), **kw)
    # R45 / review round 2 I-C, on a separate opening of the same DevSelection (the module's stays open)
    o2 = af.authorize(tmp_path / "o2", monkeypatch, dev=opened.dev)
    x2 = holdout_inputs(auth=o2.auth, **{**kw, "leakage_report": o2.auth.leakage_report})
    with pytest.raises(ValueError, match="holdout_verdict"):  # the public verdict refuses W_HOLD-shaped use
        verdict.holdout(replace(x2, ml_T=replace(x2.ml_T, window=windows.W_HOLD)))
    with pytest.raises(ValueError, match="issued by holdout_inputs"):  # inputs not issued for this opening
        holdout_verdict(x, auth=o2.auth)
    with pytest.raises(ValueError, match="issued by holdout_inputs"):
        holdout_verdict(replace(x2, integrity=()), auth=o2.auth)
    v = holdout_verdict(x2, auth=o2.auth)  # the W_HOLD verdict: once, with the verdict marker
    assert v.cls in verdict.CLASSES
    assert guard.verdict_produced()["opening_id"] == o2.auth.opening_id
    with pytest.raises(guard.HoldoutBlocked, match="verdict"):  # no second look
        holdout_verdict(x2, auth=o2.auth)
    with pytest.raises(guard.HoldoutBlocked, match="verdict"):  # the authorization opens nothing any more
        holdout_inputs(auth=o2.auth, **{**kw, "leakage_report": o2.auth.leakage_report})
    with pytest.raises(guard.HoldoutBlocked, match="verdict"):
        guard.check_outcome_window(windows.W_HOLD[0], windows.W_HOLD[0] + D, auth=o2.auth, now_ns=NOW)
    with af.verify_spec_off(), pytest.raises(guard.HoldoutBlocked, match="verdict"):
        guard.resume_holdout(guard.resume_phrase(), pre_opening=o2.record, manifest_sha256=af.MANIFEST)
    # the DT4 record of the holdout run lists the opening, its attempts and the verdict (read-only summary)
    summary = guard.opening_summary(o2.auth)
    assert [r["result"] for r in summary["resumes"]] == ["refused"]
    assert [r["result"] for r in summary["openings"]] == ["opened"] and summary["verdict_produced"]
    rec = _record([kw["ml_T"], kw["a0_T"]], opening=summary)
    assert rec["holdout_opening"]["opening_id"] == o2.auth.opening_id
    with pytest.raises(ValueError, match="opening"):
        _record([kw["ml_T"]], opening={"pin": {}})


def test_dev_leakage_checks_use_the_dev_fold_models():
    """dev_leakage_checks passes ML* and the fold models the S-10.6 books scored with (no canary inputs here:
    the report records the provenance and is not passed)."""
    from types import SimpleNamespace

    fms = lf.fold_models()
    dev = SimpleNamespace(ml_star=("A2", 72), models={
        (a, h): SimpleNamespace(models=fms[h]) for a in ("A2", "A3") for h in fms})
    with lf.tiny_space():
        r = dev_leakage_checks(dev, {}, None, **lf.PROV)
    assert r.ml_star == ("A2", 72) and not r.passed
    assert dict(r.fold_model_hashes) == {h: tuple(lf.leakage.model_hash(m) for m in fms[h]) for h in fms}
    assert {k.key for k in r.k7 if k.evaluated} == {(72, "fold0", "en"), (72, "fold0", "logit_en")}


def test_missing_funding_rate(dev_valid):
    dev, _ = dev_valid
    r = dev.runs[("A0", 24)]
    ff = r.funding_flows.iloc[:0].reindex(range(7))
    integ = ("funding_invalid:p@1", "mark_missing:p@2", "funding_missing:p", "funding_invalid:p",
             "order_cancelled_at_unwind:p")  # 3 count: per-settlement (@) entries and episode gaps
    r2 = replace(r, funding_flows=ff, integrity=integ)
    assert missing_funding_rate([r2]) == pytest.approx(3 / 10)
    assert missing_funding_rate([replace(r, funding_flows=ff.iloc[:0], integrity=())]) == 0.0


# ----------------------------------------------------------------------------------------- fail closed


def test_missing_required_inputs_fail_closed(inputs, dev_valid, opened):
    x = inputs["AAAUSDT"]
    cases = {
        "no hedge": lambda: ResearchUniverse({"AAAUSDT": x}),
        "bars15 known_at": lambda: uni(inputs, AAAUSDT=dict(bars15=x.bars15.drop(columns="known_at"))),
        "bars1m known_at": lambda: uni(inputs, AAAUSDT=dict(bars1m=x.bars1m.drop(columns="known_at"))),
        "funding interval": lambda: uni(inputs, AAAUSDT=dict(funding=x.funding.drop(
            columns="interval_h_explicit"))),
        "r4 missing": lambda: uni(inputs, AAAUSDT=dict(r4=None)),
        "r4 mark known_at": lambda: uni(inputs, AAAUSDT=dict(r4=features.R4Bars(
            x.r4.premium, x.r4.mark.drop(columns="known_at"), x.r4.index))),
        "no instances": lambda: uni(inputs, AAAUSDT=dict(insts=[])),
        "notices feed missing": lambda: uni(inputs, AAAUSDT=dict(notices=None)),
        "terminals feed missing": lambda: uni(inputs, AAAUSDT=dict(terminals=None)),
        "notices not notices": lambda: uni(inputs, AAAUSDT=dict(notices=("text",))),
    }
    for name, fn in cases.items():
        with pytest.raises(ValueError):
            fn()
            pytest.fail(name)
    with pytest.raises(TypeError):  # notices / terminals have no default: they must be stated
        runner.SymbolInputs(x.symbol, x.bars15, x.bars1m, x.funding, x.insts, x.r4, x.identity)
    u = uni(inputs, AAAUSDT=dict(aggtrades=None))
    p = DecisionProvider(u, with_features=False)
    with pytest.raises(ValueError, match="aggTrades"):
        runner.run_book("A0", u, p, (T - 5 * MIN, T + D), 24, NOW, execution="E_M")
    with pytest.raises(ValueError, match="lacks"):
        FoldScoreModel([NoP1Cal(fold(0, LO, fx.BOOK[1]))], "A2", 24).predict(T, p(T).cands)
    m24 = FoldScoreModel(fakes([(LO, LO + D)]), "A2", 24)
    with pytest.raises(ValueError, match="with_features"):  # an ML arm never scores feature-less rows
        runner.run_book("A2", u, p, (T - 5 * MIN, T + D), 24, NOW, model=m24)
    with pytest.raises(ValueError, match="horizon"):
        runner.run_book("A2", u, DecisionProvider(u, with_features=True), (T - 5 * MIN, T + D), 72, NOW,
                        model=m24)
    dev, _ = dev_valid
    with pytest.raises(ValueError, match="W_DEV facts"):
        holdout_inputs(auth=opened.auth, ml_T=dev.runs[("A2", 24)],
                       a0_T=dev.runs[("A0", 24)],
                       ml_M=dev.em_runs["A0"], a0_M=dev.em_runs["A0"], quarter_edges=(), panel=_panel(24),
                       final_model=None, leakage_report=None, records=None, integrity=())


def test_dev_selection_refusals(inputs):
    """S-10.6 inputs are refused before any book runs: a window outside W_DEV, non-consecutive quarters,
    quarters that differ across horizons, missing horizons, a provider without the §9 features."""
    u = uni(inputs)
    pf, p0 = DecisionProvider(u, with_features=True), DecisionProvider(u, with_features=False)
    q = [(LO, LO + D), (LO + D, LO + 2 * D)]
    hs = selection.HORIZONS
    w_hold = [(windows.W_HOLD[0], windows.W_HOLD[0] + D)]
    cases = [
        ({h: fakes(w_hold, h=h) for h in hs}, pf, "W_DEV only"),
        ({h: fakes([(LO, LO + D), (LO + 2 * D, LO + 3 * D)], h=h) for h in hs}, pf, "consecutive"),
        ({**{h: fakes(q, h=h) for h in hs}, 72: fakes([(LO, LO + D)], h=72)}, pf, "same outer test"),
        ({24: fakes(q, h=24)}, pf, "every horizon"),
        ({h: fakes(q, h=h) for h in hs}, p0, "with_features"),
    ]
    for fm, prov, msg in cases:
        with pytest.raises(ValueError, match=msg):
            dev_selection(u, prov, fm, NOW, B=B_SMALL)
    assert pf._views == {} and p0._views == {}  # nothing was computed


def test_provider_cache_bounded_and_identical(inputs):
    """Bars are kept for the current decision only (positional slices), views / records are a bounded LRU;
    every output equals the unbounded provider's and the boolean reference definition of the bars."""
    u = uni(inputs)
    small = DecisionProvider(u, with_features=False, max_cached_decisions=2)
    full = DecisionProvider(u, with_features=False, max_cached_decisions=None)
    ts = [int(t) for t in fx.decisions_in(fx.BOOK)]
    for t in ts:
        _views_equal(small(t), full(t))
        assert len(small._views) <= 2 and len(small._records) <= 2
        assert small._win_t == t and len(small._wins) <= len(SUB) + 1  # bars of the current t only
        for s in SUB:
            got = small.instance_bars(s, t)
            cur = runner.current_instance(u.sd[s].insts, t)
            ref = runner._instance_bars_reference(u.sd[s], cur, t)
            pd.testing.assert_frame_equal(got, ref, check_exact=True)
    assert len(full._views) == len(ts)
    _views_equal(small(ts[0]), full(ts[0]))  # an evicted decision is recomputed identically
    # unsorted bars: the reference (boolean) path, same values
    b = inputs["AAAUSDT"].bars15.sample(frac=1.0, random_state=1)
    us = uni(inputs, AAAUSDT=dict(bars15=b))
    ps = DecisionProvider(us, with_features=False)
    assert ps._positions("AAAUSDT")[0] is False
    got = ps.instance_bars("AAAUSDT", T)
    ref = full.instance_bars("AAAUSDT", T)
    pd.testing.assert_frame_equal(got.sort_values("open_ts"), ref, check_exact=True)
    with pytest.raises(ValueError):
        DecisionProvider(u, max_cached_decisions=0)
