"""Symbol mapping and instrument construction for Kraken perps and spot."""

from __future__ import annotations

from luna.config import CostConfig
from luna.types import Instrument


def _kraken_code(asset: str) -> str:
    return "XBT" if asset == "BTC" else asset


def perp_symbol(asset: str) -> str:
    return f"PF_{_kraken_code(asset)}USD"


def spot_pair(asset: str) -> str:
    return f"{_kraken_code(asset)}USD"


def asset_from_perp(symbol: str) -> str:
    code = symbol.removeprefix("PF_").removesuffix("USD")
    return "BTC" if code == "XBT" else code


def perp_instrument(meta: dict, costs: CostConfig) -> Instrument:
    """Build an Instrument from a /derivatives/api/v3/instruments entry."""
    step = 10.0 ** -int(meta["contractValueTradePrecision"])
    margin = (meta.get("marginLevels") or [{}])[0]
    return Instrument(
        asset=asset_from_perp(meta["symbol"]),
        symbol=meta["symbol"],
        venue="kraken_futures",
        kind="perp",
        qty_step=step,
        price_tick=float(meta["tickSize"]),
        min_qty=step,
        min_notional=0.0,
        maker_fee=costs.maker_fee,
        taker_fee=costs.taker_fee,
        initial_margin=float(margin.get("initialMargin", 0.0)),
        maintenance_margin=float(margin.get("maintenanceMargin", 0.0)),
    )
