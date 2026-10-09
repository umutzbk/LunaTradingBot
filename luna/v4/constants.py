"""Frozen V4 dates and universe (spec §2, R1, §7.3–§7.5). Nothing here is tunable."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from luna.forward.config import UNIVERSE as COLLECTOR_UNIVERSE
from luna.v2.data import V2_DATA_END

# §2a: the research universe is the result of the general rules U1-U7, frozen in this document.
UNIVERSE_DOC = Path(__file__).resolve().parents[2] / "docs" / "results" / "v4-universe.md"
_ROW = re.compile(r"^\|\s*([A-Z0-9]+)\s*\|\s*(YES|NO)\s*\|")
_ELIGIBLE = re.compile(r"\*\*Eligible \((\d+)\):\*\*\s*([A-Z0-9, ]+)\.")


def load_universe(path: Path = UNIVERSE_DOC) -> tuple[str, ...]:
    """Eligible assets of the frozen universe document; the table and the "Eligible (n)" line must agree
    and every asset must be a collector candidate."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    table = {m.group(1): m.group(2) == "YES" for m in map(_ROW.match, text.splitlines()) if m}
    m = _ELIGIBLE.search(text)
    if not table or m is None:
        raise ValueError(f"{path}: universe table or Eligible line not found")
    listed = tuple(sorted(a.strip() for a in m.group(2).split(",") if a.strip()))
    yes = tuple(sorted(a for a, ok in table.items() if ok))
    if listed != yes or int(m.group(1)) != len(yes):
        raise ValueError(f"{path}: Eligible line {listed} disagrees with the table {yes}")
    return yes


V4_UNIVERSE: tuple[str, ...] = load_universe()
if not set(V4_UNIVERSE) <= set(COLLECTOR_UNIVERSE):  # pragma: no cover - document integrity
    raise ValueError("V4 universe contains an asset the collector does not record")

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
