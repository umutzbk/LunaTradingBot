"""Synthetic BookRuns for the statistics / selection / verdict tests: a book whose only cash flow is one hedge
funding settlement at each day's close, so the daily net path is exactly the requested one and the run
accounts cleanly (K10 and the daily identity hold, no fills). Daily nets are quantised to multiples of 2^-30
so every equity sum is exact in float64 (a run without fills has a zero K10 / daily tolerance). Optional
15-minute marks for P3 / P4."""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.v7.book.engine import BookRun
from luna.v7.timeutil import MIN, D, to_ns

LO = to_ns("2024-01-01")
HEDGE_PID = "BTCUSDT#0@hedge1"
Q = float(2**30)


def _rt_fill(ts, d, q_after, fee, pid):
    """One BTC hedge fill of a zero-price-P&L round trip (open and close at one timestamp and one ref_mid)."""
    return dict(ts=ts, symbol="BTCUSDT", instance=0, direction=d, qty=0.001, ref_mid=100.0, price=100.0,
                fee=fee,
                spread_cost=0.0, slip_cost=0.0, liquidity="taker", reason="x", position_id=pid, flags=(),
                adverse=None, beta=np.nan, btc_ref=100.0, role="hedge", side=-1, q_after=q_after,
                entry_decision_ts=ts)


def funding_run(nets, *, arm="A2", h=24, execution="E_T", lo=LO, invalid=None, marks=None, decisions=None,
                costs=None):
    """``nets``: the daily funding (book units); ``costs``: optional daily execution cost (a fee booked on a
    hedge round trip at 06:00 with zero price P&L), so the daily net is funding - cost."""
    nets = [round(float(x) * Q) / Q for x in nets]
    n = len(nets)
    costs = [0.0] * n if costs is None else [round(float(x) * Q) / Q for x in costs]
    end = lo + n * D
    rows, flows, fills, eq = [], [], [], 1.0
    for i, (fund, cost) in enumerate(zip(nets, costs, strict=True)):
        s, e = lo + i * D, (lo + (i + 1) * D if i < n - 1 else end - MIN)
        net = fund - cost
        rows.append(dict(day=s // D, start_ts=s, end_ts=e, equity_start=eq, equity_end=eq + net, net=net,
                         funding=fund, fees=cost, spread_slip=0.0, price=0.0))
        flows.append(dict(ts=e, symbol="BTCUSDT", instance=0, position_id=HEDGE_PID, role="hedge", qty=1.0,
                          rate=0.0, mark=1.0, cash=fund))
        if cost:
            pid = f"BTCUSDT#0@rt{i}"
            fills += [_rt_fill(s + 6 * 3600 * 10**9, -1, -0.001, cost, pid),
                      _rt_fill(s + 6 * 3600 * 10**9, 1, 0.0, 0.0, pid)]
        eq += net
    if marks is None:
        marks = pd.DataFrame(dict(ts=np.arange(lo, end - MIN, 15 * MIN, dtype=np.int64)))
        marks["equity"], marks["cash"], marks["gross_legs"] = 1.0, 1.0, 0.0
        marks["hedge_notional"], marks["btc_mark"] = 0.0, 100.0
    return BookRun(arm=arm, h_star=h, execution=execution, layer="L-norm", window=(lo, end),
                   fills=pd.DataFrame(fills), marks=marks, positions=pd.DataFrame(),
                   funding_flows=pd.DataFrame(flows), daily=pd.DataFrame(rows),
                   decisions=pd.DataFrame(dict(t=[lo], killed=[False])) if decisions is None else decisions,
                   integrity=(), invalid=invalid)
