"""Synthetic V4 inputs: raw collector envelopes and 15m bar panels. Never touches var/."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

COLLECTOR_START = pd.Timestamp("2026-10-09T11:52:48Z")


def write_meta(root: Path, start: pd.Timestamp = COLLECTOR_START) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "meta.json").write_text(
        json.dumps({"collector_start_ts": start.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")}), encoding="utf-8"
    )


def write_env(root: Path, source: str, recv: pd.Timestamp, payload, **extra) -> None:
    recv = pd.Timestamp(recv)
    d = root / "raw" / source / f"date={recv:%Y-%m-%d}"
    d.mkdir(parents=True, exist_ok=True)
    env = {"source": source, "seq": 1, "req_ts": recv.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
           "recv_ts": recv.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "ok": payload is not None, "error": None,
           **extra, "payload": payload}
    with open(d / f"{recv:%H}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(env) + "\n")


def bars(start, n, close=100.0, volume=1.0, freq="15min", filled=False, high=None, low=None):
    """Synthetic 15m bars; `close` may be a scalar or a list."""
    idx = pd.date_range(pd.Timestamp(start), periods=n, freq=freq)
    c = pd.Series(close, index=idx, dtype="float64") if not isinstance(close, pd.Series) else close
    return pd.DataFrame({
        "open": c, "high": c if high is None else high, "low": c if low is None else low, "close": c,
        "volume": float(volume), "trades": 1, "filled": filled,
    }, index=idx)


def hourly_funding(start, n, rel=1e-5, price=100.0):
    idx = pd.date_range(pd.Timestamp(start), periods=n, freq="1h")
    rel_s = pd.Series(rel, index=idx, dtype="float64")
    return pd.DataFrame({"funding_abs": rel_s * price, "funding_rel": rel_s}, index=idx)
