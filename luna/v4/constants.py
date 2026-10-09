"""Frozen V4 dates and universe (spec §2, R1, §7.3–§7.5). Nothing here is tunable."""

from __future__ import annotations

import pandas as pd

from luna.forward.config import UNIVERSE as COLLECTOR_UNIVERSE
from luna.v2.data import V2_DATA_END

# §2: MUBARAK failed the outcome-free funding consistency gate (2026-10-09) and is excluded.
EXCLUDED = ("MUBARAK",)
V4_UNIVERSE: tuple[str, ...] = tuple(sorted(a for a in COLLECTOR_UNIVERSE if a not in EXCLUDED))

# R1: sealed holdout [V4_DATA_END, COLLECTOR_START_TS); historical loaders stop at V4_DATA_END.
V4_DATA_END = V2_DATA_END
COLLECTOR_START_TS = pd.Timestamp("2026-10-09T11:52:48Z")

# §7.5: evidence start (user decision) = first 00:00 UTC after collector_start + 31 days.
V4_EVIDENCE_START_TS = pd.Timestamp("2026-11-10T00:00:00Z")
D0 = V4_EVIDENCE_START_TS
LOOK_DAYS = (182, 273, 365)
LOOKS: tuple[pd.Timestamp, ...] = tuple(D0 + pd.Timedelta(days=d) for d in LOOK_DAYS)

# §7.3: Stage 1 (real funding only); the first 30 days are lookback.
STAGE1_START = pd.Timestamp("2025-10-06T00:00:00Z")
STAGE1_EVAL_START = STAGE1_START + pd.Timedelta(days=30)
STAGE1_END = V4_DATA_END
# §7.3 proxy period (diagnostics only, no P&L): config history_start -> start of real funding
PROXY_START = pd.Timestamp("2023-01-01T00:00:00Z")

BAR = pd.Timedelta(minutes=15)
HOUR = pd.Timedelta(hours=1)
DAY = pd.Timedelta(days=1)
