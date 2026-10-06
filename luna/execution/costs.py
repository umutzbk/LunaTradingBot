"""Transaction cost model: fees, half-spread and slippage as fractions of notional."""

from __future__ import annotations

from typing import Literal

from luna.config import CostConfig

Liquidity = Literal["maker", "taker"]

_BPS = 1e-4


class CostModel:
    def __init__(self, cfg: CostConfig) -> None:
        if "default" not in cfg.half_spread_bps:
            raise ValueError("half_spread_bps needs a 'default' entry")
        self.cfg = cfg

    def fee_rate(self, liquidity: Liquidity) -> float:
        return self.cfg.maker_fee if liquidity == "maker" else self.cfg.taker_fee

    def half_spread(self, asset: str) -> float:
        """Half-spread as a fraction of price (asset override, else 'default')."""
        bps = self.cfg.half_spread_bps
        return bps.get(asset, bps["default"]) * _BPS

    @property
    def slippage(self) -> float:
        return self.cfg.slippage_bps * _BPS

    def fee(self, notional: float, liquidity: Liquidity) -> float:
        """Fee in quote currency for a fill of the given (absolute) notional."""
        return abs(notional) * self.fee_rate(liquidity)

    def round_trip_frac(self, asset: str, liquidity: Liquidity = "taker") -> float:
        """Entry + exit cost as a fraction of notional: 2*(fee + half_spread + slippage)."""
        return 2.0 * (self.fee_rate(liquidity) + self.half_spread(asset) + self.slippage)
