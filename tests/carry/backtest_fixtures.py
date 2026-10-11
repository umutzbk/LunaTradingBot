"""Tiny fabricated market for the V5 backtest tests (never real data)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.carry.funding import FundingStore
from luna.carry.history import HistoricalData
from tests.carry.helpers import carry_config, instrument

T = pd.Timestamp("2026-01-05T00:00Z")  # a Monday
ASSETS = ["A", "B", "C", "D", "E", "F"]
RELS = {"A": 3e-5, "B": 2e-5, "C": 1e-5, "D": -1e-5, "E": -2e-5, "F": -3e-5}  # shorts earn positive
LO = T - pd.Timedelta("40D")


def make_data(days: int = 4, price: float = 100.0, spread: float = 4e-4, rels: dict | None = None,
              t0: pd.Timestamp = T) -> HistoricalData:
    lo = t0 - pd.Timedelta("40D")
    hi = t0 + pd.Timedelta(days=days)
    idx = pd.date_range(lo, hi, freq="15min", inclusive="left")
    mark, perp, spreads = {}, {}, {}
    store = FundingStore()
    rels = rels or RELS
    for a in ASSETS:
        c = np.full(len(idx), price)
        mark[a] = pd.DataFrame({"open": c, "high": c, "low": c, "close": c}, index=idx)
        perp[a] = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e4, "filled": False}, index=idx)
        sidx = pd.date_range(lo, hi, freq="5min", inclusive="left")
        spreads[a] = pd.DataFrame({"spread_full": spread}, index=sidx)
        hrs = pd.date_range(lo, hi, freq="1h", inclusive="left")
        store.merge_rows(a, [{"timestamp": h.isoformat(), "fundingRate": rels[a] * price,
                              "relativeFundingRate": rels[a]} for h in hrs])
    return HistoricalData(mark=mark, perp=perp, spreads=spreads, store=store)


def set_rate(data: HistoricalData, asset: str, lo: pd.Timestamp, hi: pd.Timestamp, rel: float, price: float = 100.0):
    hrs = pd.date_range(lo, hi, freq="1h", inclusive="left")
    data.store.merge_rows(asset, [{"timestamp": h.isoformat(), "fundingRate": rel * price,
                                   "relativeFundingRate": rel} for h in hrs])


def base_cfg(**kw):
    return carry_config(universe=ASSETS, **kw)


def instruments():
    return {a: instrument(a) for a in ASSETS}
