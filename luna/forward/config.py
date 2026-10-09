"""Fixed V4 universe, endpoints and cadences of the forward collector (spec §1, §3)."""

from __future__ import annotations

from dataclasses import dataclass

from luna.instruments import perp_symbol, spot_pair

UNIVERSE: tuple[str, ...] = (
    "ADA", "AVAX", "BNB", "BTC", "DOGE", "ENA", "ETH", "FET", "HYPE", "LINK", "MUBARAK",
    "NEAR", "PEPE", "PUMP", "SOL", "SUI", "TAO", "UNI", "XLM", "XRP", "ZEC",
)
PERP_SYMBOLS: tuple[str, ...] = tuple(perp_symbol(a) for a in UNIVERSE)
SPOT_PAIRS: tuple[str, ...] = tuple(spot_pair(a) for a in UNIVERSE)

FUTURES_BASE = "https://futures.kraken.com"
SPOT_BASE = "https://api.kraken.com/0/public"

ANALYTICS_TYPES: tuple[str, ...] = (
    "open-interest", "spreads", "liquidity", "future-basis", "funding", "long-short-info"
)
ANALYTICS_LOOKBACK_S = 3 * 3600
ANALYTICS_INTERVAL_S = 300
DEPTH_COUNT = 10


@dataclass(frozen=True)
class Cadence:
    period_s: int  # round every period_s seconds ...
    offset_s: int  # ... at UTC epoch multiples of period_s plus offset_s
    calls: int  # requests per round (envelope rows expected per round)
    spacing_s: float = 0.0  # minimum pause between the round's requests


SOURCES: dict[str, Cadence] = {
    "fut_tickers": Cadence(60, 0, 1),
    "spot_ticker": Cadence(60, 0, 1),  # right after fut_tickers, same pair_seq
    "spot_time": Cadence(300, 150, 1),
    "fut_book": Cadence(300, 150, len(UNIVERSE), 0.5),
    "spot_depth": Cadence(300, 150, len(UNIVERSE), 1.0),
    "fut_funding": Cadence(3600, 300, len(UNIVERSE), 0.5),
    "fut_analytics": Cadence(3600, 420, len(UNIVERSE) * len(ANALYTICS_TYPES), 0.5),
}
