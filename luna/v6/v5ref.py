"""Verdict-free V5 W / E reference on the holdout (spec D9, S-STAT-O). The V5 code and its frozen
config run unchanged; only the V5 modules' holdout cut-off constant is moved to the holdout end
inside a context manager, and only with the V6 ``HoldoutAuthorization`` (declared D-R5). The result
enters no V5 or V6 decision.
"""

from __future__ import annotations

from contextlib import contextmanager

import pandas as pd

from luna.v6 import guard
from luna.v6.devdata import Inputs

V5_ARMS = ("W", "E")


@contextmanager
def _v5_cutoff(new: pd.Timestamp):
    from luna.carry import backtest as bt
    from luna.carry import frozen as fz
    from luna.carry import history as hi

    old = (fz.CUTOFF, hi.CUTOFF, bt.CUTOFF)
    fz.CUTOFF = hi.CUTOFF = bt.CUTOFF = pd.Timestamp(new)
    try:
        yield
    finally:
        fz.CUTOFF, hi.CUTOFF, bt.CUTOFF = old


def _v5_data(inp: Inputs):
    from luna.carry.funding import FundingStore
    from luna.carry.history import HistoricalData

    mark = {a: inp.mark.close[[a]].rename(columns={a: "close"}).dropna() for a in inp.mark.close.columns}
    perp = {}
    for a in inp.trade.close.columns:
        c = inp.trade.close[a]
        v = inp.volume_usd.close[a] / c.where(c > 0)
        perp[a] = pd.DataFrame({"close": c.ffill(), "volume": v.fillna(0.0), "filled": c.isna()}).dropna(subset=["close"])
    spreads = {a: s.to_frame("spread_full") for a, s in inp.spreads.items()}
    store = FundingStore()
    for a in inp.funding.assets:
        rel, ab = inp.funding.rel[a], inp.funding.abs[a]
        rows = [{"timestamp": t.isoformat(), "fundingRate": float(ab[t]), "relativeFundingRate": float(rel[t])}
                for t in rel.index if pd.notna(rel[t]) and pd.notna(ab[t])]
        store.merge_rows(a, rows)
    return HistoricalData(mark=mark, perp=perp, spreads=spreads, store=store)


def _v5_instruments(inp: Inputs) -> dict:
    from luna.carry.backtest import FEE_RATE
    from luna.instruments import perp_symbol
    from luna.types import Instrument

    out = {}
    for a, i in inp.insts.items():
        p = i["perp"]
        out[a] = Instrument(a, perp_symbol(a), "kraken_futures", "perp", float(p.get("step", 0.0)), float(p.get("tick", 0.0)),
                            float(p.get("min_qty", 0.0)), float(p.get("min_cost", 0.0)), 0.0002, FEE_RATE, 0.0, 0.0)
    return out


def v5_reference(inp: Inputs, auth: guard.HoldoutAuthorization, end: pd.Timestamp | None = None) -> dict:
    """V5 W and E (PRIMARY, frozen config) over [holdout start, last midnight before the holdout end)."""
    if not isinstance(auth, guard.HoldoutAuthorization):
        raise guard.HoldoutBlocked("the V5 holdout reference runs only inside the authorised holdout run")
    from luna.carry import backtest as bt
    from luna.carry import outcome_guard as og

    start = guard.HOLDOUT_START
    end = pd.Timestamp(end) if end is not None else guard.HOLDOUT_END.floor("D")
    base, cfg_sha = og.frozen_carry_config()
    data = _v5_data(inp)
    insts = _v5_instruments(inp)
    out = {"config_sha256": cfg_sha, "window": [str(start), str(end)], "verdict_free": True}
    with _v5_cutoff(guard.HOLDOUT_END):
        for arm in V5_ARMS:
            r = bt.run_arm(data, bt.arm_config(base, arm), insts, name=arm, start=start, end=end)
            out[arm] = r.daily
    return out
