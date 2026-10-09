"""Execution and valuation prices for the V4 accounts (spec §3, §5.1, §5.3, §7.2).

Historical: a decision at t executes at the close of the bar with open_ts = t; if that bar is missing
(or forward-filled) the first real bar within the next hour is used, else nothing executes. Perp half
spread = mean analytics spread of [t - 1h, t) / 2, else 10 bps full (fallback flagged). Valuation of
day d = close of the bar with open_ts = d 23:45 (the last earlier bar when it is missing).
Mark (margin ratio and liquidation only, never funding): margin at t uses the close of the mark bar with
open_ts = t - 15m (trade close if missing, counted); liquidation extremes = mark 15m highs (short perp
legs) / lows (long perp legs) of the bars after the execution bar (trade bars when no mark data).

Forward: decisions at 00:00 execute at the first skew-valid paired snapshot within 15 minutes (the
``ForwardDaily.exec`` row); the real bid / ask gives the half spread; an order larger than the top of
book is walked through the nearest earlier depth snapshot. Valuation of day d = the next day's execution
snapshot. A day with no valid snapshot at all on d + 1 is an outage (E_d = E_{d-1}).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from luna.forward.dq import _spot_key_pair
from luna.instruments import perp_symbol, spot_pair
from luna.v4.accounting import Quote
from luna.v4.constants import BAR, DAY, HOUR
from luna.v4.costs import perp_half_spread_hist
from luna.v4.data import ForwardDaily, HistoricalInputs, iter_forward


def _empty_funding() -> pd.DataFrame:
    return pd.DataFrame(
        {"funding_abs": pd.Series(dtype="float64"), "funding_rel": pd.Series(dtype="float64")},
        index=pd.DatetimeIndex([], tz="UTC"),
    )


class HistoricalMarket:
    forward = False

    def __init__(self, inp: HistoricalInputs) -> None:
        self.inp = inp
        self.counters: Counter = Counter()
        self._quotes: dict = {}
        self._values: dict = {}
        self._extremes: dict = {}

    def _bars(self, asset: str, leg: str) -> pd.DataFrame | None:
        df = (self.inp.perp if leg == "perp" else self.inp.spot).get(asset)
        return df if df is not None and len(df) else None

    def quote(self, asset: str, leg: str, t: pd.Timestamp, qty: float = 0.0) -> Quote | None:
        key = (asset, leg, t)
        if key not in self._quotes:
            self._quotes[key] = self._quote(asset, leg, t)
        return self._quotes[key]

    def _quote(self, asset: str, leg: str, t: pd.Timestamp) -> Quote | None:
        df = self._bars(asset, leg)
        if df is None:
            return None
        w = df.iloc[df.index.searchsorted(t, "left") : df.index.searchsorted(t + HOUR, "left")]
        w = w[~w["filled"].astype(bool)]
        if w.empty:
            return None
        ts, mid = w.index[0], float(w["close"].iloc[0])
        if leg == "spot":
            return Quote(mid, None, ts + BAR)
        half, fallback = perp_half_spread_hist(self.inp.spreads.get(asset), t)
        return Quote(mid, half, ts + BAR, fallback)

    def value(self, asset: str, leg: str, day: pd.Timestamp) -> float | None:
        key = (asset, leg, day)
        if key not in self._values:
            df = self._bars(asset, leg)
            i = df.index.searchsorted(day + DAY - BAR, "right") if df is not None else 0
            self._values[key] = float(df["close"].iloc[i - 1]) if i > 0 else None
        return self._values[key]

    def mark(self, asset: str, t: pd.Timestamp) -> float | None:
        """Mark at t for the margin ratio: close of the mark bar with open_ts = t - 15m, else the trade
        bar's close (counted)."""
        for src, counted in ((self.inp.mark.get(asset), False), (self._bars(asset, "perp"), True)):
            if src is not None and len(src) and (t - BAR) in src.index:
                if counted:
                    self.counters["mark_fallbacks"] += 1
                return float(src.at[t - BAR, "close"])
        return None

    def extremes(self, asset: str, day: pd.Timestamp) -> dict[str, float | None]:
        key = (asset, day)
        if key not in self._extremes:
            self._extremes[key] = self._extreme(asset, day)
        return dict(self._extremes[key])

    def _extreme(self, asset: str, day: pd.Timestamp) -> dict[str, float | None]:
        lo, hi = day + BAR, day + DAY

        def window(df):
            if df is None or len(df) == 0:
                return None
            w = df.iloc[df.index.searchsorted(lo, "left") : df.index.searchsorted(hi, "left")]
            return w if len(w) else None

        mark = window(self.inp.mark.get(asset))
        if mark is None:
            self.counters["mark_fallback_days"] += 1
            mark = window(self._bars(asset, "perp"))
        spot = window(self._bars(asset, "spot"))
        return {
            "perp_high": float(mark["high"].max()) if mark is not None else None,
            "perp_low": float(mark["low"].min()) if mark is not None else None,
            "spot_low": float(spot["low"].min()) if spot is not None else None,
            "spot_high": float(spot["high"].max()) if spot is not None else None,
        }

    def funding(self, asset: str) -> pd.DataFrame:
        f = self.inp.funding.get(asset)
        return f if f is not None and len(f) else _empty_funding()

    def suspended(self, asset: str, t: pd.Timestamp) -> bool:
        return False

    def outage(self, day: pd.Timestamp) -> bool:
        return False


def walk(levels: list[tuple[float, float]], qty: float) -> tuple[float, bool]:
    """Average fill price of ``qty`` through ``levels`` (best first); remainder beyond the visible
    depth fills at the worst visible level (flagged)."""
    left, cost = abs(qty), 0.0
    for px, size in levels:
        take = min(left, size)
        cost += take * px
        left -= take
        if left <= 1e-15:
            return cost / abs(qty), False
    cost += left * levels[-1][0]
    return cost / abs(qty), True


BookLoader = Callable[[str, str, pd.Timestamp], dict | None]


class ForwardMarket:
    forward = True

    def __init__(
        self, fd: ForwardDaily, funding: dict[str, pd.DataFrame], book_loader: BookLoader | None = None
    ):
        self.fd, self._funding, self.book_loader = fd, funding, book_loader
        self.counters: Counter = Counter()
        valid = fd.daily["basis_n"].groupby(level="day").sum() if len(fd.daily) else pd.Series(dtype=float)
        self._valid_days = set(valid[valid > 0].index)

    def _exec(self, day: pd.Timestamp, asset: str):
        key = (day, asset)
        return self.fd.exec.loc[key] if len(self.fd.exec) and key in self.fd.exec.index else None

    def quote(self, asset: str, leg: str, t: pd.Timestamp, qty: float = 0.0) -> Quote | None:
        if t != t.floor("D"):
            return None
        r = self._exec(t, asset)
        if r is None:
            return None
        mid, half = float(r[f"{leg}_mid"]), float(r[f"{leg}_half_spread"])
        ts = pd.Timestamp(r["ts"])
        if qty and self.book_loader is not None:
            book = self.book_loader(asset, leg, ts)
            side = "asks" if qty > 0 else "bids"
            if not book or not book.get("bids") or not book.get("asks"):
                self.counters["book_missing"] += 1
            elif abs(qty) > book[side][0][1]:
                depth_mid = (book["bids"][0][0] + book["asks"][0][0]) / 2
                avg, short = walk(book[side], qty)
                half = max(half, abs(avg - depth_mid) / depth_mid)
                self.counters["book_walks"] += 1
                self.counters["book_walk_beyond_depth"] += short
        return Quote(mid, half, ts)

    def value(self, asset: str, leg: str, day: pd.Timestamp) -> float | None:
        r = self._exec(day + DAY, asset)
        return float(r[f"{leg}_mid"]) if r is not None else None

    def extremes(self, asset: str, day: pd.Timestamp) -> dict[str, float | None]:
        key = (day, asset)
        if not len(self.fd.daily) or key not in self.fd.daily.index:
            return {"perp_high": None, "perp_low": None, "spot_low": None, "spot_high": None}
        r = self.fd.daily.loc[key]

        def f(x):
            return float(x) if x is not None and np.isfinite(x) else None

        return {
            "perp_high": f(r["mark_max"]),
            "perp_low": f(r["mark_min"]),
            "spot_low": f(r["spot_min"]),
            "spot_high": None,
        }

    def mark(self, asset: str, t: pd.Timestamp) -> float | None:
        """Last snapshot markPrice at or before t (the previous UTC day's last mark for a 00:00 t)."""
        key = (t.floor("D") - DAY, asset)
        if not len(self.fd.daily) or key not in self.fd.daily.index or "mark_last" not in self.fd.daily:
            return None
        v = self.fd.daily.at[key, "mark_last"]
        return float(v) if v is not None and np.isfinite(v) else None

    def funding(self, asset: str) -> pd.DataFrame:
        f = self._funding.get(asset)
        return f if f is not None and len(f) else _empty_funding()

    def suspended(self, asset: str, t: pd.Timestamp) -> bool:
        r = self._exec(t.floor("D"), asset)
        return bool(r["suspended"]) if r is not None else False

    def outage(self, day: pd.Timestamp) -> bool:
        return (day + DAY) not in self._valid_days


BOOK_LOOKBACK = pd.Timedelta(minutes=10)


def _levels(rows) -> list[tuple[float, float]]:
    return [(float(r[0]), float(r[1])) for r in rows or []]


def nearest_book(root: Path, asset: str, leg: str, ts: pd.Timestamp) -> dict | None:
    """The last depth snapshot (``fut_book`` / ``spot_depth``) of ``asset`` received in [ts - 10 min, ts)."""
    source = "fut_book" if leg == "perp" else "spot_depth"
    want = perp_symbol(asset) if leg == "perp" else spot_pair(asset)
    best = None
    for env in iter_forward(root, source, ts - BOOK_LOOKBACK, ts):
        params = env.get("params") or {}
        if not env.get("ok") or params.get("symbol" if leg == "perp" else "pair") != want:
            continue
        payload = env.get("payload") or {}
        if leg == "perp":
            ob = payload.get("orderBook") or {}
            book = {"bids": _levels(ob.get("bids")), "asks": _levels(ob.get("asks"))}
        else:
            book = None
            for key, v in (payload.get("result") or {}).items():
                if _spot_key_pair(key) == want and isinstance(v, dict):
                    book = {"bids": _levels(v.get("bids")), "asks": _levels(v.get("asks"))}
            if book is None:
                continue
        if best is None or env["_recv_ns"] >= best[0]:
            best = (env["_recv_ns"], book)
    return best[1] if best else None
