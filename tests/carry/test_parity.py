"""W-V4 replay (backtest protocol §5.2) and parity tooling (§5.3, §5.4) on synthetic data."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from luna.carry import parity
from luna.carry.history import parity_quote

DAY = pd.Timedelta("1D")
BAR = pd.Timedelta("15min")
MON = pd.Timestamp("2025-11-10T00:00:00Z")  # a Monday


def _bars(start, end, px, vol=1e5, filled=None):
    idx = pd.date_range(start, end, freq="15min", tz="UTC", inclusive="left")
    p = np.full(len(idx), float(px)) if np.isscalar(px) else np.asarray(px, dtype=float)
    f = np.zeros(len(idx), dtype=bool) if filled is None else filled
    return pd.DataFrame(
        {"open": p, "high": p, "low": p, "close": p, "volume": vol, "trades": 1, "filled": f}, index=idx
    )


def _funding(start, end, rel, px):
    idx = pd.date_range(start, end, freq="1h", tz="UTC", inclusive="left")
    return pd.DataFrame({"funding_abs": rel * px, "funding_rel": float(rel)}, index=idx)


def _spreads(days, full=0.0004):
    idx = []
    for d in days:
        idx.extend(pd.date_range(d - pd.Timedelta("1h"), d, freq="1min", tz="UTC", inclusive="left"))
    return pd.DataFrame({"spread_full": full}, index=pd.DatetimeIndex(idx))


def _inputs(rels: dict[str, float], start="2025-09-01", end="2025-11-26", px=100.0):
    start, end = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    days = list(pd.date_range(start + DAY, end, freq="D"))
    perp, mark, funding, spreads, inst = {}, {}, {}, {}, {}
    for a, rel in rels.items():
        perp[a] = _bars(start, end, px)
        mark[a] = perp[a][["open", "high", "low", "close"]].copy()
        funding[a] = _funding(start, end, rel, px)
        spreads[a] = _spreads(days)
        inst[a] = parity.LegRule(step=0.001, min_qty=0.001, min_cost=0.0)
    return parity.ParityInputs(perp=perp, mark=mark, funding=funding, spreads=spreads, instruments=inst)


# --- V4 primitives ---------------------------------------------------------------------------------


def test_round_qty_floors_to_step_with_sign():
    r = parity.LegRule(step=0.1, min_qty=0.1, min_cost=0.0)
    assert parity.round_qty_v4(-0.37, r) == pytest.approx(-0.3)
    assert parity.round_qty_v4(0.05, r) == 0.0


def test_order_ok_needs_min_qty_and_min_cost():
    r = parity.LegRule(step=0.1, min_qty=0.2, min_cost=5.0)
    assert not parity.order_ok_v4(0.1, 100.0, r)
    assert not parity.order_ok_v4(0.2, 10.0, r)
    assert parity.order_ok_v4(0.2, 30.0, r)


# --- quote (parity mode of the §3 builder) ----------------------------------------------------------


def test_parity_quote_uses_first_real_bar_in_the_hour_and_the_v4_spread_window():
    bars = _bars(MON - DAY, MON + DAY, 100.0)
    bars.loc[MON, "filled"] = True
    bars.loc[MON + BAR, "close"] = 101.0
    sp = _spreads([MON], full=0.0006)
    q = parity_quote(bars, sp, MON)
    assert q.mid == 101.0
    assert q.ts == MON + 2 * BAR  # close of the 00:15 bar
    assert q.half == pytest.approx(0.0003)
    assert not q.fallback


def test_parity_quote_falls_back_to_10bps_with_fewer_than_30_samples():
    bars = _bars(MON - DAY, MON + DAY, 100.0)
    sp = _spreads([MON]).iloc[:29]
    q = parity_quote(bars, sp, MON)
    assert q.half == pytest.approx(0.0005) and q.fallback


def test_parity_quote_is_none_without_a_real_bar_in_the_hour():
    bars = _bars(MON - DAY, MON + DAY, 100.0)
    bars.loc[(bars.index >= MON) & (bars.index < MON + pd.Timedelta("1h")), "filled"] = True
    assert parity_quote(bars, _spreads([MON]), MON) is None


# --- eligibility / ranking ---------------------------------------------------------------------------


def test_v4_eligibility_and_ranking_long_lowest_short_highest():
    inp = _inputs({"A": 1e-5, "B": 2e-5, "C": 3e-5, "D": 4e-5, "E": 5e-5})
    store = parity.funding_store(inp)
    sig = parity.v4_signals(inp, store, MON)
    assert all(s.eligible for s in sig.values())
    assert sig["A"].fl_annual == pytest.approx(1e-5 * 8760)
    assert sig["A"].n == 720
    sides = parity.v4_h2_sides(sig, k=2)
    assert sides == {"A": 1, "B": 1, "D": -1, "E": -1}


def test_v4_eligibility_needs_age_volume_and_coverage():
    inp = _inputs({"A": 1e-5, "B": 2e-5, "C": 3e-5, "D": 4e-5})
    inp.perp["B"] = inp.perp["B"][inp.perp["B"].index >= MON - pd.Timedelta(days=20)]  # too young
    inp.perp["C"]["volume"] = 10.0  # 10 x 100 x 96 = 96k USD/day < 1M
    f = inp.funding["D"]
    inp.funding["D"] = f[(f.index < MON - pd.Timedelta(days=10)) | (f.index >= MON)]  # coverage 480/720
    sig = parity.v4_signals(inp, parity.funding_store(inp), MON)
    assert sig["A"].eligible
    assert not sig["B"].eligible and not sig["C"].eligible and not sig["D"].eligible
    assert parity.v4_h2_sides(sig, k=2) is None  # fewer than 2k eligible: flat week


# --- W-V4 replay --------------------------------------------------------------------------------------


def test_w_v4_rebalances_only_on_mondays_and_matches_a_hand_computed_week():
    """Constant prices: the week's P&L is funding minus entry costs, by hand."""
    rels = {"A": -2e-5, "B": -1e-5, "C": 1e-5, "D": 2e-5}
    inp = _inputs(rels)
    days = list(pd.date_range(MON - 2 * DAY, MON + 6 * DAY, freq="D"))
    res = parity.run_w_v4(inp, days, fee_rate=0.0005)
    reb = sorted({f["ts"] for f in res.fills})
    assert reb == [(MON + BAR).isoformat()]  # Monday 00:00 decision fills at the 00:15 close
    # each leg 0.25 x 265 at 100 -> 0.6625 units (step 0.001 -> 0.662)
    q = {f["asset"]: f["dq"] for f in res.fills}
    assert q == pytest.approx({"A": 0.662, "B": 0.662, "C": -0.662, "D": -0.662})
    fee = 0.0005 * 0.662 * 100 * 4
    spread = 0.0002 * 0.662 * 100 * 4
    d = res.daily.loc[MON]
    assert d["fees"] == pytest.approx(fee, abs=1e-12)
    assert d["spread_slippage"] == pytest.approx(spread, abs=1e-12)
    # funding from 00:15: 0.75 h of the 00:00 period, then 23 full hours; longs on negative rates
    # and shorts on positive rates all receive |rel| x px x q per hour
    per_h = 0.662 * 100 * (2e-5 + 1e-5 + 1e-5 + 2e-5)
    assert d["funding_cf"] == pytest.approx(per_h * 23.75, rel=1e-12)
    assert d["perp_price_pnl"] == pytest.approx(0.0, abs=1e-9)
    nxt = res.daily.loc[MON + DAY]
    assert nxt["funding_cf"] == pytest.approx(per_h * 24, rel=1e-12)
    assert nxt["fees"] == 0.0
    assert res.counters["decisions"] == 1


def test_w_v4_resizes_held_legs_to_half_over_k_of_equity_next_monday():
    rels = {"A": -2e-5, "B": -1e-5, "C": 1e-5, "D": 2e-5}
    inp = _inputs(rels, end="2025-12-01")
    for a in rels:  # price of A doubles mid-week: next Monday its long leg is cut back
        if a == "A":
            p = inp.perp[a]
            p.loc[p.index >= MON + 3 * DAY, ["open", "high", "low", "close"]] = 200.0
            inp.mark[a] = p[["open", "high", "low", "close"]].copy()
    days = list(pd.date_range(MON, MON + 8 * DAY, freq="D"))
    res = parity.run_w_v4(inp, days, fee_rate=0.0005)
    second = [f for f in res.fills if f["ts"] == (MON + 7 * DAY + BAR).isoformat()]
    assert {f["asset"] for f in second} >= {"A"}
    a = next(f for f in second if f["asset"] == "A")
    assert a["dq"] < 0  # long A trimmed back toward 0.5 / k x E


def test_w_v4_flat_week_closes_everything():
    rels = {"A": -2e-5, "B": -1e-5, "C": 1e-5, "D": 2e-5}
    inp = _inputs(rels, end="2025-12-01")
    f = inp.funding["D"]  # D loses coverage before the second Monday -> 3 eligible < 2k
    inp.funding["D"] = f[f.index < MON + 3 * DAY]
    days = list(pd.date_range(MON, MON + 8 * DAY, freq="D"))
    res = parity.run_w_v4(inp, days, fee_rate=0.0005)
    assert res.counters["flat_weeks"] == 1
    assert res.daily.loc[MON + 7 * DAY, "n_positions"] == 0


def test_w_v4_missing_funding_hour_pays_zero_and_is_counted():
    rels = {"A": -2e-5, "B": -1e-5, "C": 1e-5, "D": 2e-5}
    inp = _inputs(rels)
    f = inp.funding["A"]
    inp.funding["A"] = f[f.index != MON + pd.Timedelta("5h")]
    days = list(pd.date_range(MON, MON + 2 * DAY, freq="D"))
    res = parity.run_w_v4(inp, days, fee_rate=0.0005)
    assert res.counters["funding_unknown_hours"] == pytest.approx(1.0)
    per_h = 0.662 * 100 * (2e-5 + 1e-5 + 1e-5 + 2e-5)
    assert res.daily.loc[MON, "funding_cf"] == pytest.approx(per_h * 23.75 - 0.662 * 100 * 2e-5, rel=1e-12)


def test_w_v4_equity_identity_holds_every_day():
    rels = {"A": -2e-5, "B": -1e-5, "C": 1e-5, "D": 2e-5}
    inp = _inputs(rels, end="2025-12-01")
    rng = np.random.default_rng(1)
    for a in rels:
        p = inp.perp[a]
        path = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, len(p))))
        for col in ("open", "high", "low", "close"):
            p[col] = path
        inp.mark[a] = p[["open", "high", "low", "close"]].copy()
    days = list(pd.date_range(MON, MON + 14 * DAY, freq="D"))
    res = parity.run_w_v4(inp, days, fee_rate=0.0005)
    d = res.daily
    prev = d["equity"].shift(1).fillna(265.0)
    lhs = d["equity"] - prev
    rhs = d["perp_price_pnl"] + d["funding_cf"] - d["fees"] - d["spread_slippage"] - d["liquidation_penalty"]
    assert np.allclose(lhs, rhs, atol=1e-9)
    assert np.allclose(d["ret"], d["equity"] / prev - 1, atol=1e-15)


# --- first-divergence report ----------------------------------------------------------------------------


def test_first_divergence_reports_the_first_day_asset_and_component():
    idx = pd.date_range(MON, periods=3, freq="D")
    ref = pd.DataFrame({"fees": [0.1, 0.2, 0.3], "funding_cf": [1.0, 1.0, 1.0]}, index=idx)
    got = ref.copy()
    got.loc[idx[1], "funding_cf"] = 1.0 + 2e-9
    got.loc[idx[2], "fees"] = 0.4
    rep = parity.compare_daily(ref, got, {"fees": 1e-9, "funding_cf": 1e-9})
    assert not rep["pass"]
    assert rep["first"]["day"] == idx[1].isoformat()
    assert rep["first"]["component"] == "funding_cf"
    assert rep["first"]["delta"] == pytest.approx(2e-9)
    assert rep["by_component"]["fees"]["first_day"] == idx[2].isoformat()


def test_compare_daily_passes_within_tolerance():
    idx = pd.date_range(MON, periods=2, freq="D")
    ref = pd.DataFrame({"fees": [0.1, 0.2]}, index=idx)
    got = ref + 1e-12
    assert parity.compare_daily(ref, got, {"fees": 1e-9})["pass"]


def test_compare_fills_reports_first_mismatch_with_state():
    a = [{"ts": "t1", "asset": "A", "dq": 1.0, "px": 10.0}, {"ts": "t2", "asset": "B", "dq": -1.0, "px": 5.0}]
    b = [dict(a[0]), {"ts": "t2", "asset": "B", "dq": -1.0, "px": 5.5}]
    rep = parity.compare_fills(a, b, rel_tol=1e-9)
    assert not rep["pass"]
    assert rep["first"]["index"] == 1 and rep["first"]["field"] == "px"
    assert rep["first"]["expected"] == 5.0 and rep["first"]["actual"] == 5.5
    assert math.isclose(rep["first"]["delta"], 0.5)


# --- B1: the reference fill log through V5 accounting -------------------------------------------------


def test_replay_fill_log_reproduces_the_w_v4_daily_from_its_own_fills_and_valuations():
    rels = {"A": -2e-5, "B": -1e-5, "C": 1e-5, "D": 2e-5}
    inp = _inputs(rels, end="2025-12-01")
    rng = np.random.default_rng(3)
    for a in rels:
        p = inp.perp[a]
        path = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, len(p))))
        for col in ("open", "high", "low", "close"):
            p[col] = path
        inp.mark[a] = p[["open", "high", "low", "close"]].copy()
    days = list(pd.date_range(MON, MON + 14 * DAY, freq="D"))
    res = parity.run_w_v4(inp, days, fee_rate=0.0005)
    log = [{**f, "fee_rate": 0.0005} for f in res.fills]
    got = parity.replay_fill_log(log, res.valuations, parity.funding_store(inp), days)
    cols = ["funding_cf", "fees", "spread_slippage", "perp_price_pnl", "liquidation_penalty", "equity"]
    rep = parity.compare_daily(res.daily[cols], got[cols], {c: 1e-9 for c in cols})
    assert rep["pass"], rep["first"]


def test_replay_fill_log_books_the_penalty_separately():
    inp = _inputs({"A": 1e-5})
    store = parity.funding_store(inp)
    t = MON + BAR
    log = [
        {"ts": t.isoformat(), "asset": "A", "dq": 1.0, "px": 100.0, "half_spread": 0.0, "fee_rate": 0.0,
         "penalty_rate": 0.0},
        {"ts": (t + pd.Timedelta("1h")).isoformat(), "asset": "A", "dq": -1.0, "px": 100.0, "half_spread": 0.0,
         "fee_rate": 0.0, "penalty_rate": 0.005},
    ]
    got = parity.replay_fill_log(log, {}, store, [MON])
    assert got.loc[MON, "liquidation_penalty"] == pytest.approx(0.5)
    assert got.loc[MON, "equity"] == pytest.approx(265.0 - 0.5 - 1e-5 * 100)


# --- review fixes (2026-10-11): NaN never passes, unsupported V4 orderings fail loudly -------------------


def test_compare_daily_flags_nan_against_a_number():
    idx = pd.date_range(MON, periods=2, freq="D")
    ref = pd.DataFrame({"fees": [0.1, 0.2]}, index=idx)
    got = ref.copy()
    got.loc[idx[1], "fees"] = np.nan
    rep = parity.compare_daily(ref, got, {"fees": 1e-9})
    assert not rep["pass"]
    assert rep["first"]["day"] == idx[1].isoformat()


def test_compare_daily_flags_a_missing_column():
    idx = pd.date_range(MON, periods=1, freq="D")
    rep = parity.compare_daily(pd.DataFrame({"fees": [0.1]}, index=idx), pd.DataFrame({"x": [0.1]}, index=idx),
                               {"fees": 1e-9})
    assert not rep["pass"]


def test_compare_fills_flags_nan():
    a = [{"ts": "t1", "asset": "A", "dq": 1.0, "px": 10.0}]
    b = [{"ts": "t1", "asset": "A", "dq": 1.0, "px": float("nan")}]
    assert not parity.compare_fills(a, b, rel_tol=1e-9)["pass"]


def test_funding_store_refuses_nan_rows_whose_v4_semantics_differ():
    inp = _inputs({"A": 1e-5})
    inp.funding["A"].iloc[5, inp.funding["A"].columns.get_loc("funding_rel")] = np.nan
    with pytest.raises(parity.ParityUnsupported, match="NaN"):
        parity.funding_store(inp)


def test_out_of_order_fill_timestamps_fail_loudly():
    """V4 appends an earlier-stamped fill after a later one and double-counts the overlap in its
    funding path; W-V4 does not imitate that silently."""
    inp = _inputs({"A": 1e-5})
    sim = parity._WV4(inp, 0.0005, 1.0, 2, 1.0)
    sim.fill("A", 1.0, 100.0, MON + 3 * BAR, 0.0, "rebalance")
    with pytest.raises(parity.ParityUnsupported, match="before"):
        sim.fill("A", -1.0, 100.0, MON + BAR, 0.0, "liquidation")


def test_paper_engine_never_loads_replay_only_modules():
    """Protocol R10 / §5.2: the paper engine cannot reach W-V4, the parity tooling or the frozen copy."""
    import subprocess
    import sys

    code = (
        "import sys, luna.carry.engine, luna.cli\n"
        "bad = [m for m in ('luna.carry.parity', 'luna.carry.history', 'luna.carry.frozen', 'luna.carry.verdict') if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
