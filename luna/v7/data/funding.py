"""Funding archive + REST merge (spec S-3 R2, S-2.6 DEC-32/DEC-29, S-4.6).

Archive monthly ``fundingRate`` files are primary; REST fills only keys the archive lacks (match within the
frozen +-60 s tolerance). Overlapping keys are compared: agreeing -> archive row kept (archive provenance,
no field borrowed from REST); a material conflict -> ``IntegrityBlocker`` (never picked silently).
"Material" is an implementation clarification (AMB-03): rate differing by more than ``RATE_ATOL`` = 1e-12,
or an ambiguous / many-to-one timestamp mapping.
"""

from __future__ import annotations

import io
from typing import Literal

import numpy as np
import pandas as pd

from luna.v7.constants import FROZEN
from luna.v7.data.klines import to_ns_checked
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.timeutil import MIN

RATE_ATOL = 1e-12
OUT_COLUMNS = ("funding_ts", "rate", "interval_h_explicit", "mark_at_settle", "source", "known_at")


def parse_archive_csv(text: str, unit: Literal["ms", "us", "ns"]) -> pd.DataFrame:
    """Archive CSV (calc_time, funding_interval_hours, last_funding_rate; header optional) -> normalised."""
    df = pd.read_csv(io.StringIO(text), header=None, dtype=str)
    if len(df) and not str(df.iloc[0, 0]).strip().isdigit():
        df = df.iloc[1:]
    df = df.iloc[:, :3].copy()
    df.columns = ["calc_time", "funding_interval_hours", "last_funding_rate"]
    return normalise_archive(df.reset_index(drop=True), unit)


def normalise_archive(df: pd.DataFrame, unit: Literal["ms", "us", "ns"]) -> pd.DataFrame:
    if "funding_interval_hours" in df.columns:
        interval = pd.array(pd.to_numeric(df["funding_interval_hours"]).to_numpy(), dtype="Int64")
    else:
        interval = pd.array([None] * len(df), dtype="Int64")
    out = pd.DataFrame({
        "funding_ts": to_ns_checked(df["calc_time"], unit).to_numpy(),
        "rate": pd.to_numeric(df["last_funding_rate"]).astype("float64").to_numpy(),
        "interval_h_explicit": interval,
        "mark_at_settle": np.nan,
    })
    if "symbol" in df.columns:
        out.insert(0, "symbol", df["symbol"].to_numpy())
    return out


def normalise_rest(df: pd.DataFrame, unit: Literal["ms", "us", "ns"]) -> pd.DataFrame:
    """REST rows (fundingTime, fundingRate, markPrice) -> normalised (no explicit interval)."""
    if "markPrice" in df.columns:
        mark = pd.to_numeric(df["markPrice"].replace("", np.nan), errors="coerce").to_numpy(dtype="float64")
    else:
        mark = np.full(len(df), np.nan)
    out = pd.DataFrame({
        "funding_ts": to_ns_checked(df["fundingTime"], unit).to_numpy(),
        "rate": pd.to_numeric(df["fundingRate"]).astype("float64").to_numpy(),
        "interval_h_explicit": pd.array([None] * len(df), dtype="Int64"),
        "mark_at_settle": mark,
    })
    if "symbol" in df.columns:
        out.insert(0, "symbol", df["symbol"].to_numpy())
    return out


def _check_self(df: pd.DataFrame, name: str, tol: int) -> None:
    ts = np.sort(df["funding_ts"].to_numpy())
    if len(ts) > 1 and (np.diff(ts) <= tol).any():
        raise IntegrityBlocker(f"{name} funding has two records within {tol // 10**9} s of each other")


def _merge_one(archive: pd.DataFrame, rest: pd.DataFrame, tol: int) -> pd.DataFrame:
    _check_self(archive, "archive", tol)
    _check_self(rest, "REST", tol)
    a_ts = np.sort(archive["funding_ts"].to_numpy())
    a_rate = dict(zip(archive["funding_ts"].tolist(), archive["rate"].tolist(), strict=True))
    claimed: set[int] = set()
    fill = []
    for i, (ts, rate) in enumerate(zip(rest["funding_ts"].tolist(), rest["rate"].tolist(), strict=True)):
        lo = int(np.searchsorted(a_ts, ts - tol, side="left"))
        hi = int(np.searchsorted(a_ts, ts + tol, side="right"))
        if hi - lo > 1:
            raise IntegrityBlocker(f"REST funding_ts {ts} matches several archive records")
        if hi == lo:
            fill.append(i)
            continue
        k = int(a_ts[lo])
        if k in claimed:
            raise IntegrityBlocker(f"two REST records map to the same archive settlement {k}")
        claimed.add(k)
        if not abs(a_rate[k] - rate) <= RATE_ATOL:
            raise IntegrityBlocker(f"funding rate conflict at {k}: archive {a_rate[k]!r} vs REST {rate!r}")
    parts = [archive.assign(source="archive"), rest.iloc[fill].assign(source="rest")]
    parts = [p for p in parts if len(p)]
    out = pd.concat(parts, ignore_index=True) if parts else archive.assign(source="archive")
    out = out.sort_values("funding_ts", kind="stable")
    out["known_at"] = out["funding_ts"] + MIN
    return out.reset_index(drop=True)


def merge(archive: pd.DataFrame, rest: pd.DataFrame) -> pd.DataFrame:
    """Archive primary, REST fills missing keys (+-60 s); conflicts raise ``IntegrityBlocker`` (DEC-32).

    Columns: ``funding_ts, rate, interval_h_explicit, mark_at_settle, source, known_at`` (``symbol`` first if
    the inputs carry it; each symbol is merged independently). ``known_at = funding_ts + 1 min``.
    """
    tol = FROZEN.funding.tol_ns
    if "symbol" not in archive.columns and "symbol" not in rest.columns:
        return _merge_one(archive, rest, tol)[list(OUT_COLUMNS)]
    syms = sorted(set(archive.get("symbol", [])) | set(rest.get("symbol", [])))
    parts = []
    for s in syms:
        a = archive[archive["symbol"] == s].drop(columns="symbol")
        r = rest[rest["symbol"] == s].drop(columns="symbol")
        m = _merge_one(a, r, tol)
        m.insert(0, "symbol", s)
        parts.append(m[["symbol", *OUT_COLUMNS]])
    if not parts:
        return pd.DataFrame(columns=["symbol", *OUT_COLUMNS])
    return pd.concat(parts, ignore_index=True)
