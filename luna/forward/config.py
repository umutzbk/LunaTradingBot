"""Fixed V4 universe, endpoints and cadences of the forward collector (spec §1, §3)."""

from __future__ import annotations

import math
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
FUNDING_KEEP_S = 48 * 3600  # hourly fut_funding stores only rates of the last 48 h


@dataclass(frozen=True)
class Cadence:
    period_s: int  # round every period_s seconds ...
    offset_s: int  # ... at UTC epoch multiples of period_s plus offset_s
    calls: int  # requests per round (envelope rows expected per round)
    spacing_s: float = 0.0  # minimum pause between the round's requests
    round: str = ""  # sources with the same cadence *and* round name run as one round


SOURCES: dict[str, Cadence] = {
    "fut_tickers": Cadence(60, 0, 1),
    "spot_ticker": Cadence(60, 0, 1),  # right after fut_tickers, same pair_seq
    # two :02:30 rounds, spot first, each with its own deadline: slow futures never starve spot
    "spot_time": Cadence(300, 150, 1, round="spot"),
    "fut_book": Cadence(300, 150, len(UNIVERSE), 0.5, round="futures"),
    "spot_depth": Cadence(300, 150, len(UNIVERSE), 1.0, round="spot"),
    "fut_funding": Cadence(3600, 300, len(UNIVERSE), 0.5),
    "fut_analytics": Cadence(3600, 420, len(UNIVERSE) * len(ANALYTICS_TYPES), 0.5),
    # daily reference data, also fetched once at every collector start
    "fut_instruments": Cadence(86400, 600, 1),
    "spot_assetpairs": Cadence(86400, 720, 1),
    # daily full (unfiltered) funding history as an audit trail, 00:20 UTC
    "fut_funding_full": Cadence(86400, 1200, len(UNIVERSE), 0.5),
}
AT_START = ("fut_instruments", "spot_assetpairs", "fut_funding_full")


def rounds_between(cad: Cadence, start_s: float, end_s: float) -> int:
    """Number of scheduled rounds ``k * period + offset`` in ``[start_s, end_s)``."""
    if end_s <= start_s:
        return 0
    p, o = cad.period_s, cad.offset_s
    return math.ceil((end_s - o) / p) - math.ceil((start_s - o) / p)


def expected_rows(source: str, start_s: float, end_s: float) -> int:
    cad = SOURCES[source]
    return rounds_between(cad, start_s, end_s) * cad.calls
