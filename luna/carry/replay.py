"""Replay / backtest adapter: drives the same ``CarryCore.run_cycle`` (and so the same
``strategy.decide``) over a stream of market snapshots with simulated fills.

Only the adapter differs from paper: snapshots come from an iterable instead of the collector,
fills from ``SimExecutor`` instead of the PaperBroker, nothing is persisted. The trigger and the
decision mode come from the config, so the V4 weekly Monday baseline
(``decision_mode: rebalance_to_target``, ``trigger: weekly``), a daily or fixed-interval rebalance
and the event-driven cost-aware book all run through this one path.

The caller supplies point-in-time inputs: the funding store may hold the whole history (signals
read only ended periods, settlement reads a period's rate after it ends), snapshots must be in time
order. Building historical snapshots and the backtest protocol are a separate design step.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd

from luna.carry.config import CarryConfig
from luna.carry.core import CarryCore, SimExecutor
from luna.carry.funding import FundingStore
from luna.carry.market import MarketSnapshot
from luna.types import Instrument


@dataclass
class ReplayResult:
    equity: pd.Series
    records: pd.DataFrame
    fills: list = field(default_factory=list)
    settlements: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def run_replay(
    cfg: CarryConfig,
    snapshots: Iterable[MarketSnapshot],
    store: FundingStore,
    instruments: dict[str, Instrument],
    fee_rate: float,
    slippage_bps: float = 0.0,
) -> ReplayResult:
    core = CarryCore(cfg, instruments, fee_rate, mode="backtest")
    ex = SimExecutor(fee_rate, slippage_bps)
    eq: dict[pd.Timestamp, float] = {}
    recs: list[dict] = []
    fills: list = []
    settlements: list = []
    refs: dict[str, float] = {}
    last: pd.Timestamp | None = None
    for snap in snapshots:
        now = snap.ts
        if last is not None and now <= last:
            raise ValueError(f"snapshots must be strictly increasing in time ({now} after {last})")
        last = now
        res = core.run_cycle(now, snap, store, ex, core.trigger.due(now, core.last_rebalance))
        eq[now] = res.equity
        for r in res.records:
            recs.append({"ts": now, **r.__dict__})
        refs.update({o.client_id: ref for o, ref in res.orders})
        fills += res.fills
        settlements += res.settlements
    records = pd.DataFrame(
        recs,
        columns=[
            "ts",
            "decision",
            "reason",
            "side",
            "asset_out",
            "asset_in",
            "expected_carry",
            "current_carry",
            "incremental_carry",
            "switch_cost",
            "safety_buffer",
            "value",
        ],
    )
    equity = pd.Series(eq, dtype=float)
    funding = sum(sum(s.amounts.values()) for s in settlements)
    fees = sum(f.fee for f, _ in fills)
    slip = sum(
        (1 if f.side == "buy" else -1) * (f.price - refs.get(f.client_id, f.price)) * f.qty for f, _ in fills
    )
    end = float(equity.iloc[-1]) if len(equity) else cfg.capital_usd
    net = end - cfg.capital_usd
    summary = {
        "equity_end": end,
        "net": net,
        "funding": funding,
        "fees": fees,
        "spread_slip": slip,
        "price": net - funding + fees + slip,  # mark-to-market price P&L at decision prices
        "turnover": sum(f.qty * f.price for f, _ in fills),
        "n_fills": len(fills),
        "unknown_funding_periods": sum(1 for s in settlements if not s.known),
    }
    return ReplayResult(equity, records, fills, settlements, summary)
