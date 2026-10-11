"""§5.5 W-V4 -> W bridge steps 1-4 inside the V4 engine (fabricated data only)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from luna.carry import backtest as bt
from luna.carry import parity
from luna.carry.history import HistoricalData
from tests.carry.helpers import carry_config
from tests.carry.test_parity import _inputs

MON = pd.Timestamp("2025-11-10T00:00:00Z")
DAY = pd.Timedelta("1D")
M15 = pd.Timedelta("15min")
RELS = {"A": 3e-5, "B": 2e-5, "C": 1e-5, "D": -1e-5, "E": -2e-5, "F": -3e-5}


def _hist(inp: parity.ParityInputs, full=4e-4) -> HistoricalData:
    sp = {}
    for a, df in inp.perp.items():
        idx = pd.date_range(df.index[0], df.index[-1] + M15, freq="5min", inclusive="left")
        sp[a] = pd.DataFrame({"spread_full": full}, index=idx)
    return HistoricalData(mark=inp.mark, perp=inp.perp, spreads=sp, store=parity.funding_store(inp))


def _days(n=15):
    return list(pd.date_range(MON, periods=n, freq="D"))


def _cfg():
    return carry_config(universe=sorted(RELS))


def test_all_steps_off_is_exactly_w_v4():
    inp = _inputs(RELS)
    ref = parity.run_w_v4(inp, _days())
    got = parity.run_w_v4(inp, _days(), bridge=parity.BridgeFlags(), hist=_hist(inp), cfg=_cfg())
    pd.testing.assert_frame_equal(ref.daily, got.daily)
    assert ref.fills == got.fills


def test_step1_v5_snapshot_and_delayed_executor():
    inp = _inputs(RELS)
    for a in RELS:
        inp.mark[a].loc[MON, "close"] = 101.0  # the mark bar that closes at t + 15m
    r = parity.run_w_v4(inp, _days(), bridge=parity.BridgeFlags(exec_v5=True), hist=_hist(inp), cfg=_cfg())
    first = [f for f in r.fills if f["ts"] == (MON + M15).isoformat()]
    assert len(first) == 4 and all(f["px"] == 101.0 for f in first)
    assert all(f["half_spread"] == pytest.approx(2e-4) for f in first)


def test_step2_slippage_2_bps():
    inp = _inputs(RELS)
    a = parity.run_w_v4(inp, _days(), bridge=parity.BridgeFlags(), hist=_hist(inp), cfg=_cfg())
    b = parity.run_w_v4(inp, _days(), bridge=parity.BridgeFlags(slip2=True), hist=_hist(inp), cfg=_cfg())
    fa, fb = a.fills[0], b.fills[0]
    s = 1 if fb["dq"] > 0 else -1
    assert fb["spread"] == pytest.approx(abs(fb["dq"]) * fb["px"] * ((1 + s * fb["half_spread"]) * (1 + s * 2e-4) - 1) * s)
    assert fb["spread"] > fa["spread"]


def test_step3_v5_gates_drop_a_wide_spread_asset():
    inp = _inputs(RELS)
    hist = _hist(inp)
    hist.spreads["A"]["spread_full"] = 0.003  # 30 bps: fails the V5 15 bps entry gate, not V4 elig
    off = parity.run_w_v4(inp, _days(), bridge=parity.BridgeFlags(), hist=hist, cfg=_cfg())
    on = parity.run_w_v4(inp, _days(), bridge=parity.BridgeFlags(gates_v5=True), hist=hist, cfg=_cfg())
    assert "A" in off.decisions[0]["sides"] and "A" not in on.decisions[0]["sides"]
    assert on.decisions[0]["sides"] == {"B": -1, "C": -1, "E": 1, "F": 1}


def test_step3_fewer_than_2k_eligible_gives_k_one_not_flat():
    inp = _inputs(RELS)
    hist = _hist(inp)
    for a in ("A", "B", "C"):
        hist.spreads[a]["spread_full"] = 0.003
    on = parity.run_w_v4(inp, _days(), bridge=parity.BridgeFlags(gates_v5=True), hist=hist, cfg=_cfg())
    assert on.decisions[0]["sides"] == {"D": -1, "F": 1}  # k = min(2, 3 // 2) = 1


def test_step4_no_resize_of_held_legs():
    inp = _inputs(RELS)
    for a in RELS:  # prices drift so V4 resizes held legs on the second Monday
        inp.perp[a]["close"] = np.linspace(100, 120, len(inp.perp[a]))
        inp.mark[a]["close"] = inp.perp[a]["close"]
    off = parity.run_w_v4(inp, _days(), bridge=parity.BridgeFlags(), hist=_hist(inp), cfg=_cfg())
    on = parity.run_w_v4(inp, _days(), bridge=parity.BridgeFlags(no_resize=True), hist=_hist(inp), cfg=_cfg())
    mon2 = (MON + 7 * DAY).isoformat()
    assert any(f["ts"].startswith(mon2[:10]) and f["source"] == "rebalance" for f in off.fills)
    assert not any(f["ts"].startswith(mon2[:10]) and f["source"] == "rebalance" for f in on.fills)


def test_bridge_runs_cumulative_steps_in_both_orders():
    inp = _inputs(RELS)
    fwd = parity.run_bridge(inp, _hist(inp), _cfg(), _days())
    assert [x[0] for x in fwd] == ["W-V4", "+exec_v5", "+slip2", "+gates_v5", "+no_resize"]
    rev = parity.run_bridge(inp, _hist(inp), _cfg(), _days(), order=tuple(reversed(parity.BRIDGE_STEPS)))
    assert [x[0] for x in rev] == ["W-V4", "+no_resize", "+gates_v5", "+slip2", "+exec_v5"]
    pd.testing.assert_frame_equal(fwd[-1][1].daily, rev[-1][1].daily)  # same end point
    pd.testing.assert_frame_equal(fwd[0][1].daily, parity.run_w_v4(inp, _days()).daily)


def test_bridge_on_real_inputs_needs_the_outcome_authorization():
    inp = _inputs(RELS)
    hist = _hist(inp)
    hist.real = True
    with pytest.raises(bt.OutcomeBlocked):
        parity.run_bridge(inp, hist, _cfg(), _days())
