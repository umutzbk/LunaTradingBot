"""Kraken PF_ perpetual funding for the carry bot.

Semantics (verified for V4 in docs/results/v4-funding-semantics.md; re-implemented here, V4 code
is not imported):

- one funding period = 1 hour; the API ``timestamp`` is the START of the period the rate applies
  to (the rate is computed from the premium of the preceding hour);
- ``fundingRate`` (abs) = USD received by a 1-unit short held for the whole period;
  ``relativeFundingRate`` (rel) = abs / reference price, a decimal fraction per hour;
- positive rate: longs pay shorts; negative rate: shorts pay longs;
- funding accrues continuously and settles at the period end or when the position changes,
  pro rata to the time held; a period without a rate pays nothing and is counted (known_only).

Signals use only ended periods: a decision at ``t`` sees a period starting at ``T`` iff
``T + 1h <= t``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from luna.instruments import asset_from_perp

PERIOD = pd.Timedelta("1h")
HOURS_PER_YEAR = 8760
_P_NS = PERIOD.value


def funding_cashflow(qty: float, funding_abs: float, held_fraction: float = 1.0) -> float:
    """USD cash flow to a position of ``qty`` units (long > 0) held ``held_fraction`` of a period."""
    if not 0.0 <= held_fraction <= 1.0:
        raise ValueError(f"held_fraction must be in [0, 1], got {held_fraction}")
    return -qty * funding_abs * held_fraction


def carry_per_hour(side: int, rel_rate: float) -> float:
    """Expected funding income per hour per USD of notional for a leg on ``side`` (+1 long,
    -1 short) when the relative rate is ``rel_rate``: shorts earn positive funding."""
    if side not in (1, -1):
        raise ValueError(f"side must be +1 or -1, got {side}")
    return -side * rel_rate


def _ns(ts: pd.Timestamp) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        raise ValueError("funding timestamps must be tz-aware (UTC)")
    return int(t.value)


def _ts(ns: int) -> pd.Timestamp:
    return pd.Timestamp(ns, tz="UTC")


def _iso_ns(text: str) -> int:
    return (
        int(pd.Timestamp(text).tz_convert("UTC").value)
        if pd.Timestamp(text).tzinfo
        else int(pd.Timestamp(text).tz_localize("UTC").value)
    )


class FundingStore:
    """Realized hourly funding per asset: period start (ns) -> (abs, rel)."""

    def __init__(self) -> None:
        self._rates: dict[str, dict[int, tuple[float, float]]] = {}
        self._cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def assets(self) -> list[str]:
        return sorted(a for a, r in self._rates.items() if r)

    def merge_rows(self, asset: str, rows: list[dict[str, Any]]) -> int:
        """Merge Kraken ``rates`` rows; a period seen again is overwritten. Returns rows merged."""
        book = self._rates.setdefault(asset, {})
        n = 0
        for r in rows:
            try:
                t = _iso_ns(r["timestamp"])
                a, rel = float(r["fundingRate"]), float(r["relativeFundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(a) and math.isfinite(rel)):
                continue
            book[t] = (a, rel)
            n += 1
        self._cache.pop(asset, None)
        return n

    def merge_envelope(self, env: dict[str, Any]) -> int:
        """Merge one collector ``fut_funding`` / ``fut_funding_full`` envelope (ok rows only)."""
        if not env.get("ok") or not isinstance(env.get("payload"), dict):
            return 0
        sym = (env.get("params") or {}).get("symbol")
        if not sym:
            return 0
        return self.merge_rows(asset_from_perp(sym), env["payload"].get("rates") or [])

    def rate(self, asset: str, period_start: pd.Timestamp) -> tuple[float, float] | None:
        return self._rates.get(asset, {}).get(_ns(period_start))

    def _arrays(self, asset: str) -> tuple[np.ndarray, np.ndarray]:
        if asset not in self._cache:
            book = self._rates.get(asset, {})
            keys = np.array(sorted(book), dtype=np.int64)
            rel = np.array([book[k][1] for k in keys], dtype=float)
            self._cache[asset] = (keys, rel)
        return self._cache[asset]

    def window(self, asset: str, now: pd.Timestamp, hours: int) -> np.ndarray:
        """Relative rates of the ended periods starting in ``[now - hours h, now - 1h]``."""
        keys, rel = self._arrays(asset)
        t = _ns(now)
        lo = np.searchsorted(keys, t - hours * _P_NS, "left")
        hi = np.searchsorted(keys, t - _P_NS, "right")  # start + 1h <= now
        return rel[lo:hi]

    def trailing(self, asset: str, now: pd.Timestamp, hours: int) -> tuple[float, int]:
        """(mean relative rate per hour, number of known periods) over the window."""
        w = self.window(asset, now, hours)
        return (float(w.mean()) if len(w) else math.nan), int(len(w))

    def newest_end(self, asset: str) -> pd.Timestamp | None:
        keys, _ = self._arrays(asset)
        return _ts(int(keys[-1]) + _P_NS) if len(keys) else None

    def prune(self, before: pd.Timestamp) -> None:
        """Drop periods starting before ``before`` (bounded memory)."""
        cut = _ns(before)
        for asset, book in self._rates.items():
            for k in [k for k in book if k < cut]:
                del book[k]
            self._cache.pop(asset, None)


@dataclass(frozen=True)
class Settlement:
    asset: str
    period_start: pd.Timestamp
    known: bool
    funding_abs: float | None
    funding_rel: float | None
    amounts: dict[str, float]  # position id -> USD (positive = received)
    held_hours: float  # position-hours inside the period (any position id)
    qty_hours: float = 0.0  # signed qty x hours (diagnostic)


@dataclass
class FundingLedger:
    """Piecewise-constant net qty per asset (from fills) and the next unsettled period."""

    path: dict[str, list[tuple[int, float, str]]] = field(default_factory=dict)  # (ns, qty, pid)
    next_period: dict[str, int] = field(default_factory=dict)

    def assets(self) -> list[str]:
        return sorted(self.path)

    def on_fill(self, asset: str, ts: pd.Timestamp, qty: float, position_id: str) -> None:
        """The asset's net qty is ``qty`` (held by ``position_id``) from ``ts`` on."""
        t = _ns(ts)
        segs = self.path.setdefault(asset, [])
        if segs and t < segs[-1][0]:
            raise ValueError(f"{asset}: fill at {ts} before the last recorded change")
        if not segs:
            self.next_period.setdefault(asset, t - t % _P_NS)
        segs.append((t, float(qty), position_id))

    def _period(self, asset: str, start: int) -> tuple[dict[str, float], float, float]:
        """{pid: qty x fraction} (fractions of the period), held hours and signed qty hours."""
        segs = self.path[asset]
        end = start + _P_NS
        weights: dict[str, float] = {}
        held = qty_h = 0.0
        for i, (t, q, pid) in enumerate(segs):
            t_next = segs[i + 1][0] if i + 1 < len(segs) else math.inf
            lo, hi = max(t, start), min(t_next, end)
            if hi <= lo or q == 0.0:
                continue
            frac = (hi - lo) / _P_NS
            weights[pid] = weights.get(pid, 0.0) + q * frac
            held += frac
            qty_h += q * frac
        return weights, held, qty_h

    def settle(self, now: pd.Timestamp, store: FundingStore, grace: pd.Timedelta) -> list[Settlement]:
        """Settle every ended period, in order, whose rate is known (or whose grace has passed:
        settled at 0 and marked unknown). Stops at the first period still waiting for its rate."""
        t_now = _ns(now)
        g = int(pd.Timedelta(grace).value)
        out: list[Settlement] = []
        for asset in list(self.path):
            start = self.next_period[asset]
            while start + _P_NS <= t_now:
                rate = store.rate(asset, _ts(start))
                if rate is None and t_now < start + _P_NS + g:
                    break
                weights, held, qty_h = self._period(asset, start)
                if held > 0:
                    if rate is None:
                        amounts = {pid: 0.0 for pid in weights}
                    else:
                        amounts = {pid: -w * rate[0] for pid, w in weights.items()}
                    out.append(
                        Settlement(
                            asset,
                            _ts(start),
                            rate is not None,
                            None if rate is None else rate[0],
                            None if rate is None else rate[1],
                            amounts,
                            held,
                            qty_h,
                        )
                    )
                start += _P_NS
                self.next_period[asset] = start
                self._trim(asset, start)
            if asset in self.path and self._flat_done(asset):
                del self.path[asset]
                del self.next_period[asset]
        return out

    def _trim(self, asset: str, settled_until: int) -> None:
        """Drop segments that ended before ``settled_until`` (keep the one still in force)."""
        segs = self.path[asset]
        keep = 0
        for i, (t, _, _) in enumerate(segs):
            if t <= settled_until:
                keep = i
        del segs[:keep]

    def _flat_done(self, asset: str) -> bool:
        segs = self.path[asset]
        return bool(segs) and segs[-1][1] == 0.0 and segs[-1][0] <= self.next_period[asset]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": {a: [[t, q, p] for t, q, p in s] for a, s in self.path.items()},
            "next_period": dict(self.next_period),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FundingLedger:
        return cls(
            path={a: [(int(t), float(q), str(p)) for t, q, p in s] for a, s in d.get("path", {}).items()},
            next_period={a: int(v) for a, v in d.get("next_period", {}).items()},
        )
