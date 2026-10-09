"""Fixed V4 universe, endpoints and cadences of the forward collector (spec §1, §3)."""

from __future__ import annotations

from luna.instruments import perp_symbol, spot_pair

UNIVERSE: tuple[str, ...] = (
    "ADA", "AVAX", "BNB", "BTC", "DOGE", "ENA", "ETH", "FET", "HYPE", "LINK", "MUBARAK",
    "NEAR", "PEPE", "PUMP", "SOL", "SUI", "TAO", "UNI", "XLM", "XRP", "ZEC",
)
PERP_SYMBOLS: tuple[str, ...] = tuple(perp_symbol(a) for a in UNIVERSE)
SPOT_PAIRS: tuple[str, ...] = tuple(spot_pair(a) for a in UNIVERSE)

FUTURES_BASE = "https://futures.kraken.com"
SPOT_BASE = "https://api.kraken.com/0/public"
