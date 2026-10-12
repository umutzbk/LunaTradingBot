"""Forward market and execution adapters (spec S-DATA-4/5, S-H3-11, S-H1-X). Point-in-time on the
collector's receive times: a snapshot is usable at t iff recv_ts <= t; a fill is priced on a
snapshot received after the order. Nothing is interpolated or filled in.

- quotes: fut_tickers (bid/ask/mark/volumeQuote, 60 s), spot_ticker (60 s), fresh = age <= 180 s;
- books: fut_book (perp), spot_depth (spot, 10 levels, 5 min);
- H3 / H1 risk exits: taker on the first book snapshot after the decision, walking levels;
- H1 00:00 economic orders: maker-first (S-H1-X): limit at the reference best bid (buy) / ask (sell),
  fill when a later ticker crosses it with visible depth (each depth snapshot's level volume used
  once), taker fallback after 60 min walking the depth (remainder cancelled), perp hedge on the first
  fut_book after the spot leg completes.
"""

from __future__ import annotations

import bisect
import gzip
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from luna.instruments import asset_from_perp, asset_from_spot, spot_pair
from luna.v6.data import FundingPanel
from luna.v6.market import FUT_MAKER, FUT_TAKER, SPOT_MAKER, SPOT_TAKER, Fill
from luna.v6.spec import constants

_H1 = constants()["h1"]
_K = constants()["costs"]
FRESH = pd.Timedelta(seconds=_K["quote_freshness_s"])
MAKER_WINDOW = pd.Timedelta(minutes=_H1["forward_maker_window_minutes"])
SKIP_GAP = pd.Timedelta(hours=_H1["forward_skip_gap_hours"])
BAR = pd.Timedelta("15min")


@dataclass(frozen=True)
class Tick:
    ts: pd.Timestamp
    bid: float
    ask: float
    mark: float = math.nan
    vol_quote: float = math.nan

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Book:
    ts: pd.Timestamp
    bids: tuple  # ((px, size), ...) best first
    asks: tuple


class Stream:
    """Snapshots of one asset sorted by receive time."""

    def __init__(self, items: list) -> None:
        self.items = sorted(items, key=lambda x: x.ts)
        self._t = [x.ts.value for x in self.items]

    def latest(self, t: pd.Timestamp):
        i = bisect.bisect_right(self._t, pd.Timestamp(t).value)
        return self.items[i - 1] if i else None

    def first_after(self, t: pd.Timestamp, until: pd.Timestamp | None = None):
        i = bisect.bisect_right(self._t, pd.Timestamp(t).value)
        if i < len(self.items) and (until is None or self.items[i].ts <= until):
            return self.items[i]
        return None

    def between(self, a: pd.Timestamp, b: pd.Timestamp) -> list:
        i = bisect.bisect_right(self._t, pd.Timestamp(a).value)
        j = bisect.bisect_right(self._t, pd.Timestamp(b).value)
        return self.items[i:j]


@dataclass
class Streams:
    fut_ticks: dict = field(default_factory=dict)
    fut_book: dict = field(default_factory=dict)
    spot_ticks: dict = field(default_factory=dict)
    spot_depth: dict = field(default_factory=dict)


def walk(levels: tuple, qty: float, limit: float | None = None, side: str = "buy",
         used: dict | None = None, key=None) -> tuple[float, float]:
    """(filled qty, VWAP) walking ``levels`` (best first) up to ``qty``; ``limit`` caps the price
    (buy: px <= limit, sell: px >= limit); ``used`` tracks volume already taken per (key, px)."""
    got, cost = 0.0, 0.0
    for px, sz in levels:
        if limit is not None and ((side == "buy" and px > limit) or (side == "sell" and px < limit)):
            break
        avail = sz - (used.get((key, px), 0.0) if used is not None else 0.0)
        if avail <= 0:
            continue
        take = min(avail, qty - got)
        got += take
        cost += take * px
        if used is not None:
            used[(key, px)] = used.get((key, px), 0.0) + take
        if got >= qty - 1e-12:
            break
    return got, (cost / got if got else math.nan)


def _fill(a, leg, t_dec, ts, dq, px, mid, fee_rate, maker=False) -> Fill:
    s = 1.0 if dq > 0 else -1.0
    return Fill(a, leg, t_dec, ts, dq, px, mid, mid, math.nan, fee_rate * abs(dq) * px, abs(dq) * s * (px - mid),
                False, maker)


class FwdMarket:
    """The same interface as ``HistMarket`` on forward snapshots (S-H3-11 / S-H1-X)."""

    def __init__(self, streams: Streams, funding: FundingPanel, assets: list) -> None:
        self.s, self.funding, self._assets = streams, funding, list(assets)

    @property
    def assets(self) -> list:
        return list(self._assets)

    # --- quotes ------------------------------------------------------------------------------------
    def _tick(self, a: str, t: pd.Timestamp, spot: bool = False) -> Tick | None:
        st = (self.s.spot_ticks if spot else self.s.fut_ticks).get(a)
        x = st.latest(t) if st else None
        return x if x is not None and t - x.ts <= FRESH else None

    def mid(self, a: str, t: pd.Timestamp) -> float:
        x = self._tick(a, t)
        return x.mid if x else math.nan

    def spot_mid(self, a: str, t: pd.Timestamp) -> float:
        x = self._tick(a, t, spot=True)
        return x.mid if x else math.nan

    def decision_half(self, a: str, t: pd.Timestamp) -> tuple[float, bool]:
        x = self._tick(a, t)
        if x is None or not x.mid > 0:
            return math.inf, True
        return (x.ask - x.bid) / 2 / x.mid, False

    def volume24(self, a: str, t: pd.Timestamp, spot: bool = False) -> float:
        x = self._tick(a, t, spot=spot)
        return x.vol_quote if x else math.nan

    @staticmethod
    def spot_half(perp_half: float) -> float:
        return max(2 * perp_half, 0.0005)

    def decision_cost(self, a: str, t: pd.Timestamp, leg: str = "perp") -> float:
        if leg == "spot":
            x = self._tick(a, t, spot=True)
            half = (x.ask - x.bid) / 2 / x.mid if x else math.inf
            return SPOT_TAKER + half + 0.0002
        half, _ = self.decision_half(a, t)
        return FUT_TAKER + half + 0.0002

    # --- perp taker on the first book after t (H3 and risk exits) ----------------------------------
    def _book_taker(self, books: Stream, a: str, dq: float, t: pd.Timestamp, leg: str, fee: float,
                    ref_mid) -> Fill | None:
        need, side = abs(dq), ("buy" if dq > 0 else "sell")
        got, cost, last = 0.0, 0.0, None
        after = t
        while got < need - 1e-12:
            b = books.first_after(after) if books else None
            if b is None:
                break
            q, px = walk(b.asks if side == "buy" else b.bids, need - got, side=side)
            if q > 0:
                got += q
                cost += q * px
            last, after = b, b.ts
        if got <= 0 or last is None:
            return None
        mid = ref_mid(last.ts)
        if not math.isfinite(mid):
            mid = (last.bids[0][0] + last.asks[0][0]) / 2
        return _fill(a, leg, t, last.ts, math.copysign(got, dq), cost / got, mid, fee)

    def fill(self, a: str, dq: float, t: pd.Timestamp, leg: str = "perp") -> Fill | None:
        if leg == "spot":
            return self._book_taker(self.s.spot_depth.get(a), a, dq, t, "spot", SPOT_TAKER,
                                    lambda ts: self.spot_mid(a, ts))
        return self._book_taker(self.s.fut_book.get(a), a, dq, t, "perp", FUT_TAKER, lambda ts: self.mid(a, ts))

    def unwind_cost(self, a: str, q: float, t: pd.Timestamp, leg: str = "perp") -> tuple[float, float, float]:
        """Terminal / hypothetical unwind at t on the latest book received by t (no snapshot after t)."""
        books = (self.s.spot_depth if leg == "spot" else self.s.fut_book).get(a)
        b = books.latest(t) if books else None
        mid = self.spot_mid(a, t) if leg == "spot" else self.mid(a, t)
        if b is None or q == 0:
            return mid, 0.0, 0.0
        if not math.isfinite(mid):
            mid = (b.bids[0][0] + b.asks[0][0]) / 2
        side = "sell" if q > 0 else "buy"
        got, px = walk(b.bids if side == "sell" else b.asks, abs(q), side=side)
        if got < abs(q) - 1e-12:  # visible depth short: the rest at the worst visible level (declared D-X2)
            worst = (b.bids if side == "sell" else b.asks)[-1][0]
            px = (px * got + worst * (abs(q) - got)) / abs(q) if got else worst
        fee = (SPOT_TAKER if leg == "spot" else FUT_TAKER) * abs(q) * px
        return mid, fee, abs(q) * abs(px - mid)

    # --- H1 pair execution (S-H1-X) ----------------------------------------------------------------
    def pair_fill(self, a: str, q: float, t: pd.Timestamp, economic: bool = True):
        if not economic:
            fs = self.fill(a, q, t, leg="spot")
            if fs is None:
                return None
            fp = self.fill(a, -fs.dq, fs.ts, leg="perp")
            return ([fs], [fp] if fp else [])
        spot = maker_first_spot(self.s, a, q, t)
        if spot is None or not spot.fills:
            return None
        filled = sum(f.dq for f in spot.fills)
        fp = self.fill(a, -filled, spot.done_ts, leg="perp")
        return (spot.fills, [fp] if fp else [])


@dataclass
class SpotResult:
    fills: list
    done_ts: pd.Timestamp
    status: str


def maker_first_spot(s: Streams, a: str, q: float, t: pd.Timestamp) -> SpotResult | None:
    """S-H1-X spot leg: buy (q > 0) / sell (q < 0)."""
    ticks, depth = s.spot_ticks.get(a), s.spot_depth.get(a)
    if not ticks or not depth:
        return None
    ref = ticks.latest(t)
    if ref is None or t - ref.ts > FRESH:
        ref = ticks.first_after(t, until=t + SKIP_GAP)
        if ref is None:
            return SpotResult([], t, "skipped: no fresh ticker within 6 h")
    t_sub = max(ref.ts, t)
    buy = q > 0
    need = abs(q)
    limit = ref.bid if buy else ref.ask
    fills, used, got = [], {}, 0.0
    first = ticks.first_after(t_sub, until=t_sub + MAKER_WINDOW)
    if first is not None and ((first.ask <= limit) if buy else (first.bid >= limit)):
        d = depth.latest(first.ts)
        if d is not None:
            qq, px = walk(d.asks if buy else d.bids, need, limit=limit, side="buy" if buy else "sell",
                          used=used, key=d.ts.value)
            if qq > 0:
                fills.append(_fill(a, "spot", t, first.ts, math.copysign(qq, q), px, first.mid, SPOT_TAKER))
                got += qq
    for tk in ticks.between(first.ts if first is not None else t_sub, t_sub + MAKER_WINDOW):
        if got >= need - 1e-12:
            break
        crossed = (tk.ask <= limit) if buy else (tk.bid >= limit)
        if not crossed:
            continue
        d = depth.latest(tk.ts)
        if d is None:
            continue
        qq, _ = walk(d.asks if buy else d.bids, need - got, limit=limit, side="buy" if buy else "sell",
                     used=used, key=d.ts.value)
        if qq > 0:
            fills.append(_fill(a, "spot", t, tk.ts, math.copysign(qq, q), limit, tk.mid, SPOT_MAKER, maker=True))
            got += qq
    done = t_sub + MAKER_WINDOW
    if got < need - 1e-12:
        d = depth.latest(done)
        if d is None or d.ts <= t_sub - FRESH:
            d = depth.first_after(done)
        if d is not None:
            qq, px = walk(d.asks if buy else d.bids, need - got, side="buy" if buy else "sell", used=used,
                          key=d.ts.value)
            if qq > 0:
                tk = ticks.latest(max(d.ts, done))
                mid = tk.mid if tk is not None else (d.bids[0][0] + d.asks[0][0]) / 2
                fills.append(_fill(a, "spot", t, max(d.ts, done), math.copysign(qq, q), px, mid, SPOT_TAKER))
                got += qq
            done = max(d.ts, done)
    elif fills:
        done = max(f.ts for f in fills)
    return SpotResult(fills, done, "ok" if got >= need - 1e-12 else "partial")


# --- collector reader ------------------------------------------------------------------------------


def _hours(start: pd.Timestamp, end: pd.Timestamp):
    h = pd.Timestamp(start).floor("h")
    while h <= end:
        yield h
        h += pd.Timedelta("1h")


def _read(raw: Path, source: str, start: pd.Timestamp, end: pd.Timestamp):
    for h in _hours(start, end):
        d = raw / source / f"date={h:%Y-%m-%d}"
        for name, op in ((f"{h:%H}.jsonl.gz", gzip.open), (f"{h:%H}.jsonl", open)):
            p = d / name
            if p.exists():
                with op(p, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            yield json.loads(line)
                break


def read_streams(raw: Path, start: pd.Timestamp, end: pd.Timestamp, assets: list) -> Streams:
    """Snapshots with start <= recv_ts <= end (ok envelopes only)."""
    s = Streams()
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    want = set(assets)
    acc = {"ft": {}, "fb": {}, "st": {}, "sd": {}}
    for env in _read(raw, "fut_tickers", lo, hi):
        ts = pd.Timestamp(env["recv_ts"])
        if not env.get("ok") or not (lo <= ts <= hi):
            continue
        for x in env["payload"].get("tickers", []):
            try:
                a = asset_from_perp(x["symbol"])
            except Exception:  # noqa: BLE001 - non-PF symbols
                continue
            if a in want and x.get("bid") and x.get("ask"):
                acc["ft"].setdefault(a, []).append(Tick(ts, float(x["bid"]), float(x["ask"]), float(x.get("markPrice", math.nan)),
                                                        float(x.get("volumeQuote", math.nan))))
    for env in _read(raw, "fut_book", lo, hi):
        ts = pd.Timestamp(env["recv_ts"])
        if not env.get("ok") or not (lo <= ts <= hi):
            continue
        a = asset_from_perp((env.get("params") or {}).get("symbol", ""))
        ob = env["payload"].get("orderBook", {})
        if a in want and ob.get("bids") and ob.get("asks"):
            bids = tuple(sorted(((float(p), float(z)) for p, z in ob["bids"]), key=lambda x: -x[0]))
            asks = tuple(sorted(((float(p), float(z)) for p, z in ob["asks"]), key=lambda x: x[0]))
            acc["fb"].setdefault(a, []).append(Book(ts, bids, asks))
    for env in _read(raw, "spot_ticker", lo, hi):
        ts = pd.Timestamp(env["recv_ts"])
        if not env.get("ok") or not (lo <= ts <= hi):
            continue
        for pair, x in (env["payload"].get("result") or {}).items():
            a = asset_from_spot(pair)
            if a in want:
                vq = float(x["v"][1]) * float(x["p"][1])
                acc["st"].setdefault(a, []).append(Tick(ts, float(x["b"][0]), float(x["a"][0]), math.nan, vq))
    for env in _read(raw, "spot_depth", lo, hi):
        ts = pd.Timestamp(env["recv_ts"])
        if not env.get("ok") or not (lo <= ts <= hi):
            continue
        for pair, x in (env["payload"].get("result") or {}).items():
            a = asset_from_spot(pair)
            if a in want:
                bids = tuple((float(p), float(z)) for p, z, *_ in x["bids"])
                asks = tuple((float(p), float(z)) for p, z, *_ in x["asks"])
                acc["sd"].setdefault(a, []).append(Book(ts, bids, asks))
    s.fut_ticks = {a: Stream(v) for a, v in acc["ft"].items()}
    s.fut_book = {a: Stream(v) for a, v in acc["fb"].items()}
    s.spot_ticks = {a: Stream(v) for a, v in acc["st"].items()}
    s.spot_depth = {a: Stream(v) for a, v in acc["sd"].items()}
    return s


def coverage(streams: Streams, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """S-DATA-5: per day the share of 15-minute ticks with a fresh (<= 180 s) fut ticker."""
    any_asset = next(iter(streams.fut_ticks.values()), None)
    rows = []
    t = pd.Timestamp(start)
    while t <= end:
        x = any_asset.latest(t) if any_asset else None
        rows.append({"t": t, "fresh": bool(x is not None and t - x.ts <= FRESH)})
        t += BAR
    df = pd.DataFrame(rows)
    df["day"] = df["t"].dt.floor("D")
    out = df.groupby("day")["fresh"].mean().rename("share").to_frame()
    out["evaluable"] = out["share"] >= constants()["forward"]["evaluable_day_min_snapshot_share"]
    return out


def funding_from_collector(raw: Path, start: pd.Timestamp, end: pd.Timestamp, assets: list) -> FundingPanel:
    """Forward funding (S-DATA-1): rel/abs per period start; known_at = the receive time of the first
    collector round that carried the period (a later revision of the same period is ignored)."""
    rel, ab, known = {}, {}, {}
    for env in _read(Path(raw), "fut_funding", pd.Timestamp(start), pd.Timestamp(end)):
        if not env.get("ok"):
            continue
        ts = pd.Timestamp(env["recv_ts"])
        if ts > end:
            continue
        a = asset_from_perp((env.get("params") or {}).get("symbol", ""))
        if a not in assets:
            continue
        for r in env["payload"].get("rates", []):
            p = pd.Timestamp(r["timestamp"]).tz_convert("UTC") if pd.Timestamp(r["timestamp"]).tzinfo else \
                pd.Timestamp(r["timestamp"]).tz_localize("UTC")
            key = (a, p)
            if key in known:
                continue
            known[key] = ts
            rel[key] = float(r["relativeFundingRate"])
            ab[key] = float(r["fundingRate"])
    if not rel:
        idx = pd.date_range(pd.Timestamp(start).floor("h"), periods=1, freq="h")
        return FundingPanel(pd.DataFrame(np.nan, index=idx, columns=assets))
    idx = pd.date_range(min(p for _, p in rel), max(p for _, p in rel), freq="h")
    R = pd.DataFrame(np.nan, index=idx, columns=assets)
    A = R.copy()
    K = pd.DataFrame(pd.NaT, index=idx, columns=assets, dtype="datetime64[ns, UTC]")
    for (a, p), v in rel.items():
        R.loc[p, a], A.loc[p, a], K.loc[p, a] = v, ab[(a, p)], known[(a, p)]
    return FundingPanel(R, abs_=A, known_at=K)
