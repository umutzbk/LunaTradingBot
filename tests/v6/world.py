"""A synthetic market for engine tests (no real data): 15-minute GBM mark/trade bars, constant
5-minute spread snapshots, hourly funding with a persistent cross-section (abs = rel x price)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.v6 import riskmodel as rm
from luna.v6.data import BarPanel, FundingPanel
from luna.v6.market import HistMarket

T0 = pd.Timestamp("2025-03-01T00:00Z")


@dataclass
class World:
    market: HistMarket
    prices: pd.DataFrame
    vol: pd.DataFrame
    insts: dict
    funding_levels: pd.Series
    start: pd.Timestamp


def build_world(days=80, names=None, vols=None, rho=0.3, seed=0, spread_bps=2.0, levels=None,
                shocks=None, volume=5e7) -> World:
    names = names or ["BTC"] + [f"A{i:02d}" for i in range(11)]
    n = len(names)
    rng = np.random.default_rng(seed)
    vols = vols or {a: 0.6 for a in names}
    idx = pd.date_range(T0, periods=days * 96, freq="15min")
    C = np.full((n, n), rho) + (1 - rho) * np.eye(n)
    z = rng.normal(size=(len(idx), n)) @ np.linalg.cholesky(C).T
    sd = np.array([vols[a] for a in names]) / np.sqrt(8760 * 4)
    logp = np.cumsum(z * sd, axis=0)
    px = pd.DataFrame(100 * np.exp(logp), index=idx, columns=names)
    for (a, when, factor) in (shocks or []):
        px.loc[px.index >= when, a] *= factor
    mark = BarPanel(px)
    vol_usd = BarPanel(pd.DataFrame(volume / 96, index=idx, columns=names))
    sidx = pd.date_range(T0, periods=days * 288, freq="5min")
    spreads = {a: pd.Series(spread_bps * 1e-4, index=sidx) for a in names}
    hidx = pd.date_range(T0, periods=days * 24, freq="h")
    levels = levels if levels is not None else pd.Series(np.linspace(8e-5, -8e-5, n), index=names)
    rel = pd.DataFrame(np.tile(levels.to_numpy(), (len(hidx), 1)), index=hidx, columns=names)
    hp = px.reindex(hidx, method="ffill")
    funding = FundingPanel(rel, abs_=rel * hp)
    m = HistMarket(mark=mark, trade=None, volume_usd=vol_usd, funding=funding, spreads=spreads)
    hourly = px.copy()
    hourly.index = hourly.index + pd.Timedelta("15min")
    hourly = hourly[hourly.index.minute == 0]
    insts = {a: {"step": 0.001, "min_qty": 0.001} for a in names}
    return World(m, hourly, rm.ewma_vol(hourly), insts, levels, T0 + 62 * pd.Timedelta("1D"))


class FixedModel:
    """A stand-in for H2a in engine unit tests: fixed forecasts, h = 168."""

    h = 168

    def __init__(self, levels: pd.Series, drop: dict | None = None) -> None:
        self.levels, self.drop = levels, drop or {}

    def forecast(self, t: pd.Timestamp) -> pd.Series:
        f = self.levels.copy()
        for a, since in self.drop.items():
            if t >= since:
                f[a] = np.nan
        return f
