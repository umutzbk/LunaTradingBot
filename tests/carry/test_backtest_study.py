"""Study level (frozen protocol rev 3, §8-§10): the outcome HARD GATE fallback-attribution regression
on the real replay path, S-PIT10 / S-FB-CONS on the same replay line, attribution, verdict wiring.
Fabricated data only; nothing here reads the frozen copy."""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from luna.carry import backtest as bt
from tests.carry.backtest_fixtures import ASSETS, T, base_cfg, instruments, make_data, set_rate

H = pd.Timedelta("1h")
M15 = pd.Timedelta("15min")
D1 = pd.Timedelta("1D")


def _gap(data, asset, lo, hi):
    s = data.spreads[asset]
    data.spreads[asset] = s[(s.index < lo) | (s.index >= hi)]


@pytest.fixture(scope="module")
def gated_study():
    """Day T 00:00: every arm enters; A (priced at 50, so qty and notional weights differ) has no
    snapshot in its decision window (decision fallback only). C's funding jumps on day T, so the
    ideal short set changes from {A, B} to {A, C}: D, C and E (min_hold 24h keeps B until then) move
    into C at T + 1D 00:00, where C has no snapshot in its fill window (fill fallback only). W does
    not trade until the next Monday."""
    data = make_data(3)
    for src in (data.mark["A"], data.perp["A"]):
        src[["open", "high", "low", "close"]] = 50.0
    set_rate(data, "A", T - pd.Timedelta("40D"), T + 3 * D1, 3e-5, price=50.0)
    _gap(data, "A", T - H, T)
    set_rate(data, "C", T, T + D1, 1e-3)
    _gap(data, "C", T + D1, T + D1 + M15)
    study = bt.run_study(data, base_cfg(), instruments(), start=T, end=T + 2 * D1, B=200,
                         with_variants=False, with_ideal=False)
    return data, study


def test_hard_gate_fallback_attribution_on_the_replay_path(gated_study):
    data, study = gated_study
    res, ev = study["results"], study["evaluation"]
    expo = ev["fallback_exposure"]
    for arm in "WDCE":
        f = res[(arm, "PRIMARY")].fills
        a0 = f[(f["asset"] == "A") & (f["decision_ts"] == T)]
        assert len(a0) == 1 and a0["dec_fallback"].all() and not a0["fill_fallback"].any(), arm
        rest = f[~((f["asset"] == "A") & (f["decision_ts"] == T)) & ~((f["asset"] == "C") & (f["decision_ts"] == T + D1))]
        assert not rest["dec_fallback"].any() and not rest["fill_fallback"].any(), arm
        tot = f["notional"].sum()  # notional-weighted shares, recomputed by hand from the fill log
        assert expo[arm]["decision"] == pytest.approx(f.loc[f["dec_fallback"], "notional"].sum() / tot)
        assert expo[arm]["fill"] == pytest.approx(f.loc[f["fill_fallback"], "notional"].sum() / tot)
        ad = f[f["asset"] == "A"]
        assert expo[arm]["by_asset"]["A"]["decision"] == pytest.approx(a0["notional"].sum() / ad["notional"].sum())
        assert expo[arm]["by_hour"]["0"]["notional"] == pytest.approx(f.loc[f["decision_ts"].dt.hour == 0, "notional"].sum())
    w = res[("W", "PRIMARY")].fills
    assert set(w["decision_ts"]) == {T}
    assert expo["W"]["decision"] == pytest.approx(0.25, rel=2e-3) and expo["W"]["fill"] == 0.0
    assert w.loc[w["asset"] == "A", "qty"].iloc[0] == pytest.approx(2 * w.loc[w["asset"] == "B", "qty"].iloc[0], rel=1e-3)
    for arm in "DCE":
        f = res[(arm, "PRIMARY")].fills
        c_in = f[(f["asset"] == "C") & (f["decision_ts"] == T + D1)]
        assert len(c_in) == 1 and c_in["fill_fallback"].all() and not c_in["dec_fallback"].any(), arm
        assert expo[arm]["by_asset"]["C"]["fill"] == pytest.approx(c_in["notional"].sum() / f.loc[f["asset"] == "C", "notional"].sum())
        assert ev["fallback_diffs_vs_W"][arm]["fill"] == pytest.approx(expo[arm]["fill"] - expo["W"]["fill"])
    assert ev["fallback_affected"] is True
    assert ev["fallback_diffs_vs_W"]["D"]["fill"] > bt.FALLBACK_AFFECTED_PP
    assert "FALLBACK-AFFECTED" in ev["tags"] and "S-FB-CONS" in ev["rows_used"]


def test_rows_differ_only_in_their_declared_field():
    base = dataclasses.asdict(bt.ROWS["PRIMARY"])
    for name, spec in bt.ROWS.items():
        d = {k for k, v in dataclasses.asdict(spec).items() if v != base[k]} - {"name"}
        expect = {"S-COST2X": {"cost_mult"}, "S-SPREAD-FIX": {"spread_mode"}, "S-NOKILL": {"nokill"},
                  "S-PIT10": {"known_after"}, "S-FB-CONS": {"fb_cons"}, "S-PIT10/NOKILL": {"known_after", "nokill"},
                  "S-FB-CONS/NOKILL": {"fb_cons", "nokill"}, "S-COST2X/NOKILL": {"cost_mult", "nokill"},
                  "PRIMARY": set()}[name]
        assert d == expect, name


def test_s_pit10_runs_the_same_replay_with_only_the_known_time_changed():
    """The snapshot labelled t - 5 at every :55 is 400 bps: under T + 5m (primary) it is known at
    t and blows the 15 bps entry gate at 00:00; under T + 10m it is not yet known."""
    data = make_data(2)
    for a in ASSETS:
        s = data.spreads[a]
        s.loc[s.index.minute == 55, "spread_full"] = 0.04
    cfg = bt.arm_config(base_cfg(), "W")
    prim = bt.run_arm(data, cfg, instruments(), row=bt.ROWS["PRIMARY"], start=T, end=T + D1)
    pit = bt.run_arm(data, cfg, instruments(), row=bt.ROWS["S-PIT10"], start=T, end=T + D1)
    assert not (prim.fills["decision_ts"] == T).any()
    assert set(pit.fills.loc[pit.fills["decision_ts"] == T, "asset"]) == {"A", "B", "E", "F"}


def test_s_fb_cons_prices_fallbacks_at_the_lookback_p90_in_the_replay():
    """Data starting in the lookback: A's lookback p90 is 30 bps. With A's windows empty at the first
    decision the primary row prices A at 10 bps (passes the 15 bps gate), S-FB-CONS at 30 bps."""
    t0 = bt.OUTCOME_START
    data = make_data(2, t0=t0)
    s = data.spreads["A"]
    s.loc[s.index < t0, "spread_full"] = 0.003
    _gap(data, "A", t0 - H, t0 + M15)
    cfg = bt.arm_config(base_cfg(), "W")
    prim = bt.run_arm(data, cfg, instruments(), row=bt.ROWS["PRIMARY"], start=t0, end=t0 + D1)
    cons = bt.run_arm(data, cfg, instruments(), row=bt.ROWS["S-FB-CONS"], start=t0, end=t0 + D1)
    pa = prim.fills[prim.fills["asset"] == "A"]
    assert len(pa) == 1 and pa["half"].iloc[0] == pytest.approx(0.0005)
    assert not (cons.fills["asset"] == "A").any()  # 30 bps fails the entry gate
    assert bt.lookback_p90(data.spreads)["A"] == pytest.approx(0.003)


def test_attribution_telescopes_exactly(gated_study):
    _, study = gated_study
    a = study["attribution"]
    for c in bt.COMPONENTS:
        t = a["terms"]
        assert t["D-W"][c] + t["C-D"][c] + t["E-C"][c] == pytest.approx(t["E-W"][c], abs=1e-12)


def test_one_builder_per_row_is_shared_by_all_arms(monkeypatch):
    seen = {}
    real = bt.run_arm

    def spy(data, cfg, inst, **kw):
        seen.setdefault(kw["row"].name, set()).add(id(kw["builder"]))
        return real(data, cfg, inst, **kw)

    monkeypatch.setattr(bt, "run_arm", spy)
    bt.run_study(make_data(2), base_cfg(), instruments(), start=T, end=T + D1, B=100, with_variants=False,
                 with_ideal=False, rows=tuple(bt.ROWS))
    assert all(len(v) == 1 for v in seen.values()) and len({next(iter(v)) for v in seen.values()}) == len(bt.ROWS)


def test_ideal_reference_collects_the_fixed_notional_funding():
    data = make_data(2)
    ideal = bt.ideal_reference(data, base_cfg(), bt.builder_for(data, bt.ROWS["PRIMARY"]), T, T + D1, 265.0)
    leg = 265.0 / 4
    assert ideal.loc[T, "funding"] == pytest.approx(leg * (3e-5 + 2e-5 + 2e-5 + 3e-5) * 24)


def test_kill_affected_reads_the_kill_off_rows(gated_study):
    _, study = gated_study
    res = dict(study["results"])
    w = res[("W", "PRIMARY")]
    res[("W", "PRIMARY")] = dataclasses.replace(w, kill_ts=T + H)
    ev = bt.evaluate(res, 265.0, B=100)
    assert ev["kill"]["kill_affected"] and "KILL-AFFECTED" in ev["tags"]
    assert ev["rows_used"][0] == "S-NOKILL" and ev["rows_used"][1] == "S-PIT10/NOKILL"


def test_fallback_affected_threshold_on_either_share():
    def e(dec, fill):
        return {"decision": dec, "fill": fill}

    assert bt.fallback_affected({"W": e(0.0, 0.0), "D": e(0.0, 0.0101), "C": e(0, 0), "E": e(0, 0)})[0]
    assert bt.fallback_affected({"W": e(0.0, 0.0), "D": e(0, 0), "C": e(0, 0), "E": e(0.0101, 0.0)})[0]
    assert bt.fallback_affected({"W": e(0.02, 0.0), "D": e(0.02, 0), "C": e(0.02, 0), "E": e(0.0, 0.0)})[0]
    assert not bt.fallback_affected({"W": e(0.005, 0.005), "D": e(0.0, 0.0), "C": e(0.01, 0.01), "E": e(0, 0)})[0]
    assert not bt.fallback_affected({"W": e(0.0, 0.0), "D": e(0.01, 0.01), "C": e(0, 0), "E": e(0, 0)})[0]  # 1 pp: not more
