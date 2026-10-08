"""Core immutable value types shared across Luna."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

Venue = Literal["kraken_futures", "kraken_spot"]
AccountKind = Literal["perp", "spot"]
Side = Literal["buy", "sell"]
OrderModel = Literal["taker", "maker"]  # taker = IOC within the band, maker = post-only


def default_order_model(kind: AccountKind) -> OrderModel:
    """V1 execution per account: perp IOC (taker), spot post-only (maker)."""
    return "taker" if kind == "perp" else "maker"


@dataclass(frozen=True)
class Instrument:
    asset: str
    symbol: str
    venue: Venue
    kind: AccountKind
    qty_step: float
    price_tick: float
    min_qty: float
    min_notional: float
    maker_fee: float
    taker_fee: float
    initial_margin: float = 0.0
    maintenance_margin: float = 0.0


@dataclass(frozen=True)
class Leg:
    asset: str
    weight: float  # signed weight per unit of direction; pairs: (A, +1), (B, -beta)


@dataclass(frozen=True)
class StopSpec:
    kind: Literal["price", "zscore"]
    level: float
    distance_frac: float  # loss fraction of gross notional if hit


@dataclass(frozen=True)
class ExitSpec:
    max_hold_bars: int
    take_profit_z: float | None = None
    trailing_atr_mult: float | None = None


@dataclass(frozen=True)
class Candidate:
    ts: pd.Timestamp
    strategy: str
    key: str
    legs: tuple[Leg, ...]
    direction: int
    entry_ref: float
    stop: StopSpec
    exit: ExitSpec
    regime: str
    reason: str
    features: dict[str, float]
    score: float = 0.0
    edge_bps: float = 0.0


@dataclass(frozen=True)
class OrderRequest:
    client_id: str
    ts: pd.Timestamp
    asset: str
    side: Side
    qty: float
    limit_price: float
    tif: Literal["IOC", "POST_ONLY"]
    intent: Literal["open", "close", "reduce"]
    position_id: str
    leg_group: str


@dataclass(frozen=True)
class Fill:
    client_id: str
    ts: pd.Timestamp
    asset: str
    side: Side
    qty: float
    price: float
    fee: float
    liquidity: Literal["maker", "taker"]
