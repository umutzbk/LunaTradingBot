"""Orchestration of the frozen configs and rows (spec §8, S-STAT-O). The holdout run needs the
``HoldoutAuthorization``; dev runs go through ``blind`` (masked). Classes come only from ``verdict``."""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.v6 import guard, stats6
from luna.v6.criteria import evaluate
from luna.v6.devdata import Inputs, p90_fallback
from luna.v6.engine import H3Config, H3Engine
from luna.v6.forecast import H2a, gate
from luna.v6.h1 import H1Config, H1Engine, reprice_maker_bound
from luna.v6.market import HistMarket
from luna.v6.riskmodel import ewma_vol
from luna.v6.verdict import holdout_decision

H3_ROWS = ("PRIMARY", "S-COST2X", "S-PIT10", "S-FB-CONS")
H1_ROWS = ("PRIMARY", "S-MAKER-BOUND", "S-COST2X", "S-PIT10", "S-FB-CONS", "S-MIXED", "S-SPOTSPREAD2X")
CONFIGS = {"H3-k4": ("H3", 4), "H3-k6": ("H3", 6), "H1-m1.5": ("H1", 1.5), "H1-m2.5": ("H1", 2.5)}


def market(inp: Inputs, row: str, start: pd.Timestamp) -> HistMarket:
    kw = {}
    if row == "S-COST2X":
        kw["cost_mult"] = 2.0
    elif row == "S-PIT10":
        kw["known_after"] = pd.Timedelta("10min")
    elif row == "S-FB-CONS":
        kw["fb_full"] = p90_fallback(inp.spreads, start)
    elif row == "S-MIXED":
        kw["spot_maker"] = True
    elif row == "S-SPOTSPREAD2X":
        kw["spot_spread_mult"] = 2.0
    return HistMarket(mark=inp.mark, trade=inp.trade, volume_usd=inp.volume_usd, funding=inp.funding,
                      spreads=inp.spreads, spot=inp.spot, spot_volume_usd=inp.spot_volume_usd, **kw)


def run_config(inp: Inputs, name: str, start: pd.Timestamp, end: pd.Timestamp, rows=None, models=None) -> dict:
    kind, param = CONFIGS[name]
    models = models or {}
    out = {}
    if kind == "H3":
        model = models.get(168) or H2a(inp.funding, 168)
        vol = ewma_vol(inp.prices)
        for row in rows or H3_ROWS:
            eng = H3Engine(market(inp, row, start), model, inp.prices, vol, inp.insts, H3Config(name, param, row))
            out[row] = eng.run(start, end)
    else:
        model = models.get(720) or H2a(inp.funding, 720)
        for row in rows or H1_ROWS:
            if row == "S-MAKER-BOUND":
                continue
            eng = H1Engine(market(inp, row, start), model, inp.insts, H1Config(name, param, row))
            out[row] = eng.run(start, end)
        if (rows is None or "S-MAKER-BOUND" in rows) and "PRIMARY" in out:
            out["S-MAKER-BOUND"] = reprice_maker_bound(out["PRIMARY"])
    return out


def evaluate_config(inp: Inputs, name: str, runs: dict, env: str, window_end: pd.Timestamp, B: int):
    kind, param = CONFIGS[name]
    h = 168 if kind == "H3" else 720
    prim = runs["PRIMARY"]
    g = gate(inp.funding, prim.gate_records, kind=kind, k=param if kind == "H3" else None, h=h, window_end=window_end)
    ev = evaluate(name, kind, env, prim, {k: v for k, v in runs.items() if k != "PRIMARY"}, g.cls)
    ev.stats = stats6.p_go(prim.daily["ret"].to_numpy(float), prim.daily["carry_ret"].to_numpy(float), B)
    return ev, g


def run_holdout(inp: Inputs, auth: guard.HoldoutAuthorization) -> dict:
    """The single holdout run (S-STAT-O). Not executed before the user's explicit approval."""
    if not isinstance(auth, guard.HoldoutAuthorization):
        raise guard.HoldoutBlocked("the holdout run needs the HoldoutAuthorization")
    start, end = guard.HOLDOUT_START, guard.HOLDOUT_END.floor("15min")
    guard.require_window(inp.mark.close.index.min(), inp.mark.close.index.max(), "holdout", auth)
    models = {168: H2a(inp.funding, 168), 720: H2a(inp.funding, 720)}
    runs, evals, gates = {}, {}, {}
    for name in CONFIGS:
        runs[name] = run_config(inp, name, start, end, models=models)
        evals[name], gates[name] = evaluate_config(inp, name, runs[name], "holdout", end, stats6.B_HOLDOUT)
    return {"runs": runs, "evals": evals, "gates": gates, "decision": holdout_decision(evals)}
