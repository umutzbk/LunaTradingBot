"""Protocol rev 3 §2.1 S0.2: backfill the real 5-minute (interval=300) Kraken analytics spread snapshots.

Usage: backfill_spreads.py <out_root> [ASSET ...]
Resume-safe and idempotent (per-day checkpoints under <out_root>/parts); requests never reach the
holdout (``to <= cutoff - 300 s``). Assembling and provenance are a separate step (``--assemble``).
"""
import sys
from pathlib import Path

import httpx
import pandas as pd

from luna.carry import spread_backfill as sb
from luna.carry.frozen import CUTOFF

ASSETS = ["ADA", "AVAX", "BNB", "BTC", "DOGE", "ENA", "ETH", "FET", "HYPE", "LINK", "NEAR", "PEPE",
          "PUMP", "SOL", "SUI", "TAO", "UNI", "XLM", "XRP", "ZEC"]
START = pd.Timestamp("2025-10-06T00:00Z")
INTERVAL_S = 300

if __name__ == "__main__":
    args = sys.argv[1:]
    assemble = "--assemble" in args
    args = [a for a in args if a != "--assemble"]
    root, assets = Path(args[0]), args[1:] or ASSETS
    days = list(pd.date_range(START, CUTOFF, freq="D", inclusive="left"))
    if assemble:
        for a in assets:
            print(a, sb.assemble(root, a, INTERVAL_S), flush=True)
    else:
        with httpx.Client() as c:
            for a in assets:
                n = sb.backfill(c, root, [a], days, INTERVAL_S)
                print(a, "fetched days", n[a], flush=True)
