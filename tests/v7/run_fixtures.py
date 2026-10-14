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


def funding_run(nets, *, arm="A2", h=24, execution="E_T", lo=LO, invalid=None, marks=None, decisions=None):
    nets = [round(float(x) * Q) / Q for x in nets]
    n = len(nets)
    end = lo + n * D
    rows, flows, eq = [], [], 1.0
    for i, net in enumerate(nets):
        s, e = lo + i * D, (lo + (i + 1) * D if i < n - 1 else end - MIN)
        rows.append(dict(day=s // D, start_ts=s, end_ts=e, equity_start=eq, equity_end=eq + net, net=net,
                         funding=net, fees=0.0, spread_slip=0.0, price=0.0))
        flows.append(dict(ts=e, symbol="BTCUSDT", instance=0, position_id=HEDGE_PID, role="hedge", qty=1.0,
                          rate=0.0, mark=1.0, cash=net))
        eq += net
    if marks is None:
        marks = pd.DataFrame(dict(ts=np.arange(lo, end - MIN, 15 * MIN, dtype=np.int64)))
        marks["equity"], marks["cash"], marks["gross_legs"] = 1.0, 1.0, 0.0
        marks["hedge_notional"], marks["btc_mark"] = 0.0, 100.0
    return BookRun(arm=arm, h_star=h, execution=execution, layer="L-norm", window=(lo, end),
                   fills=pd.DataFrame(), marks=marks, positions=pd.DataFrame(),
                   funding_flows=pd.DataFrame(flows), daily=pd.DataFrame(rows),
                   decisions=pd.DataFrame(dict(t=[lo], killed=[False])) if decisions is None else decisions,
                   integrity=(), invalid=invalid)
