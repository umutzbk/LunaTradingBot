"""Guarded, read-only use of LOCAL data (spec S-1.3, S-1.7 "read-only loaders", "no file content ... no W_HOLD
features or labels" before the opening, §16 K9, §17; SC-03, AMB-35).

The base Task 26's loaders build on. Every use of local content passes ``guard.check_data_span`` on the
content's period first: M6 is refused before the canonical R_V6 (even after the opening), W_FWD7 before L7,
W_HOLD without the verified hardened ``HoldoutAuthorization``. A refusal is raised and K9-logged.

* ``check_file(path)``: the period comes from the data.binance.vision file name (``guard.file_period``); a
  name without a parseable period is refused (fail closed, as ``guard.check_listing_only``).
* ``open_readonly`` / ``read_bytes``: the only file opens offered: binary read-only (``"rb"``), after
  ``check_file``. Loaders never write to raw stores.
* ``frame_span`` / ``check_frame``: the event-time span of a frame (min, max + 1 over the named columns;
  missing values skipped, any other unparseable value refused through ``timeutil.ns_array``) and its guard.
  Event times (bar ``open_ts``, ``funding_ts``, trade ``ts``), not ``known_at``: a W_DEV bar closing at the
  W_HOLD start is W_DEV content (AMB-35 (f)).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

import pandas as pd

from luna.v7 import guard
from luna.v7.guard import HoldoutAuthorization, HoldoutBlocked
from luna.v7.timeutil import ns_array

__all__ = ["check_file", "check_frame", "frame_span", "open_readonly", "read_bytes"]


def check_file(path: str | Path, *, auth: HoldoutAuthorization | None = None) -> tuple[int, int]:
    """The guarded period [lo, hi) of a local data file (from its name); refused (K9-logged) when the name
    has no parseable period or the period is sealed."""
    per = guard.file_period(Path(path).name)
    if per is None:
        e = HoldoutBlocked(f"local file {path}: period not parseable from the name, content refused")
        guard._k9(e, {"kind": "file", "what": "content", "path": str(path)})
        raise e
    guard.check_data_span(per[0], per[1], auth=auth, what=f"file {Path(path).name}")
    return per


def open_readonly(path: str | Path, *, auth: HoldoutAuthorization | None = None) -> BinaryIO:
    """``check_file`` then a binary read-only handle (never a writable mode)."""
    check_file(path, auth=auth)
    return open(path, "rb")  # the caller owns (and closes) the handle


def read_bytes(path: str | Path, *, auth: HoldoutAuthorization | None = None) -> bytes:
    with open_readonly(path, auth=auth) as f:
        return f.read()


def frame_span(df: pd.DataFrame, cols: Sequence[str], name: str) -> tuple[int, int] | None:
    """(min, max + 1) of the event timestamps in ``cols`` (missing values skipped); None for no value."""
    lo: int | None = None
    hi: int | None = None
    for c in cols:
        if c not in df.columns:
            raise ValueError(f"{name}: timestamp column {c!r} missing")
        s = df[c]
        s = s[s.notna()]
        if not len(s):
            continue
        a = ns_array(s.to_numpy(), f"{name}.{c}")
        lo = int(a.min()) if lo is None else min(lo, int(a.min()))
        hi = int(a.max()) + 1 if hi is None else max(hi, int(a.max()) + 1)
    return None if lo is None or hi is None else (lo, hi)


def check_frame(df: pd.DataFrame, cols: Sequence[str], *, auth: HoldoutAuthorization | None = None,
                what: str = "frame") -> tuple[int, int] | None:
    """``guard.check_data_span`` over ``frame_span``; an empty frame has nothing to guard."""
    span = frame_span(df, cols, what)
    if span is not None:
        guard.check_data_span(span[0], span[1], auth=auth, what=what)
    return span
