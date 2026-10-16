"""Rich seeded synthetic universe for the Task 26b equivalence tests (SC-01 part 2; no market data, nothing
under var/). 140 days from 2022-01-01 (inside W_DEV), 9 symbols built directly in normalised form:

* ``AAAUSDT``: a delisting notice whose ``known_at`` is exactly a decision time - 1 min, a scheduled time
  S_A and dense volume > 0 1-minute bars over [S_A - 2 h, S_A + 74 h]: the U6 72 h test passes (U6 clears);
* ``BBBUSDT``: a delisting notice with dense 1-minute bars except a 90-minute hole inside the 72 h: HALTED
  there, U6 never clears;
* ``CCCUSDT``: a delisting then a matched postponement (U6 clears at its known_at);
* ``RELUSDT``: no bar for 10 days -> two instances (instance switch, DEC-35), a class-B terminal event;
* ``TRNUSDT``: no explicit funding interval, 8 h -> 4 h settlements (EC-9) and a 6-hour gap of 15m bars;
* ``GAPUSDT``: missing 15m / premium rows, a missing funding settlement and an odd-spaced (invalid) record;
* ``LAGUSDT``: 15m known_at lags 7 more minutes from day 50 on (still monotone);
* ``NMOUSDT``: non-monotone 15m / R4 known_at (the reference / fallback paths);
* dense 1-minute bars for every symbol over ``LABEL_WIN`` (+ one day) with a volume-0 stretch for
  ``AAAUSDT`` at a label exit (halted exit) and sparse 1-minute bars elsewhere.

``edge_times()`` are decision-like times where a 15m bar's known_at equals t - 1 min exactly.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from luna.v7.data.instances import build_instances
from luna.v7.data.notices import NoticeVersion
from luna.v7.data.terminal import TerminalEvent
from luna.v7.features import R4Bars
from luna.v7.runner import SymbolInputs
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.universe import Identity
from luna.v7.windows import decision_times

Q = 15 * MIN
START = to_ns("2022-01-01")
N_DAYS = 140
END = START + N_DAYS * D
NOW = to_ns("2026-10-15")
BTC = "BTCUSDT"
SYMBOLS = (BTC, "AAAUSDT", "BBBUSDT", "CCCUSDT", "GAPUSDT", "LAGUSDT", "NMOUSDT", "RELUSDT", "TRNUSDT")
DORMANT = (START + 70 * D, START + 80 * D)  # RELUSDT: no bar at all in [lo, hi)
TRANSITION = START + 60 * D  # TRNUSDT 8 h -> 4 h
S_A = START + 83 * D + 9 * H  # AAAUSDT scheduled delisting (72 h test passes)
R_A = START + 80 * D + 8 * H + 5 * MIN - MIN  # known_at = the 08:05 decision - 1 min
S_B = START + 87 * D + 12 * H  # BBBUSDT scheduled delisting (halted inside the 72 h)
HOLE_B = (S_B + 30 * H, S_B + 30 * H + 90 * MIN)
S_C = START + 95 * D
S_G = START + 66 * D + 4 * H  # GAPUSDT / LAGUSDT: notices known only after S_G + 72 h (fix round 1, m3)
LABEL_WIN = (START + 110 * D, START + 114 * D)  # dense 1m for every symbol (labels)
HALT_A = (START + 111 * D + 23 * H, START + 112 * D + 2 * H)  # AAAUSDT volume-0 1m bars (halted exit)


def _rng(seed: int, i: int) -> np.random.Generator:
    return np.random.default_rng([seed, 26, i])


def _bars15(sym: str, i: int, seed: int, btc_ret: np.ndarray) -> pd.DataFrame:
    g = _rng(seed, i)
    ts = np.arange(START, END, Q, dtype=np.int64)
    n = len(ts)
    ret = btc_ret if sym == BTC else 0.8 * btc_ret + g.normal(0.0, 0.0005, n)
    p0 = 20_000.0 if sym == BTC else float(g.uniform(0.5, 50.0))
    c = p0 * np.exp(np.cumsum(ret))
    o = np.concatenate([[p0], c[:-1]])
    wig = np.abs(g.normal(0.0, 1.5e-4, (2, n)))
    h, lo = np.maximum(o, c) * (1.0 + wig[0]), np.minimum(o, c) * (1.0 - wig[1])
    qv = g.uniform(0.8e6, 2.2e6, n)
    df = pd.DataFrame({"open_ts": ts, "o": o, "h": h, "l": lo, "c": c, "vol_base": qv / c, "vol_quote": qv,
                       "n_trades": g.integers(200, 900, n), "known_at": ts + Q})
    keep = np.ones(n, bool)
    if sym == "RELUSDT":
        keep &= ~((ts >= DORMANT[0]) & (ts < DORMANT[1]))
    if sym == "TRNUSDT":
        keep &= ~((ts >= START + 40 * D) & (ts < START + 40 * D + 6 * H))
    if sym == "GAPUSDT":
        keep &= g.uniform(size=n) > 0.03  # scattered missing bars
        df.loc[g.uniform(size=n) < 0.01, "c"] = np.nan  # unpriced bars
    if sym == "LAGUSDT":
        df.loc[ts >= START + 50 * D, "known_at"] = ts[ts >= START + 50 * D] + Q + 7 * MIN
    if sym == "NMOUSDT":
        ka = (ts + Q).copy()
        ka[(np.arange(n) % 2 == 0) & (ts >= START + 30 * D)] += 30 * MIN
        df["known_at"] = ka
    return df[keep].reset_index(drop=True)


def _r4(b15: pd.DataFrame, sym: str, i: int, seed: int) -> R4Bars:
    g = _rng(seed, 100 + i)
    prem = g.normal(0.0, 2e-4, len(b15)) + 1e-4
    ka = b15["known_at"].to_numpy()
    mark = b15[["open_ts"]].assign(c=b15["c"].to_numpy() * (1.0 + prem / 2.0), known_at=ka)
    index = b15[["open_ts"]].assign(c=mark["c"].to_numpy() / (1.0 + prem), known_at=ka)
    premium = b15[["open_ts"]].assign(c=prem, known_at=ka)
    if sym == "GAPUSDT":
        premium = premium[g.uniform(size=len(premium)) > 0.2].reset_index(drop=True)
    return R4Bars(premium, mark, index)


def _funding(sym: str, i: int, seed: int) -> pd.DataFrame:
    g = _rng(seed, 200 + i)
    if sym == "TRNUSDT":
        ts = np.concatenate([np.arange(START, TRANSITION, 8 * H), np.arange(TRANSITION, END, 4 * H)])
    else:
        ts = np.arange(START, END, 8 * H, dtype=np.int64)
    if sym == "GAPUSDT":
        ts = ts[ts != START + 70 * D]  # a missing settlement
        ts = np.sort(np.append(ts, START + 90 * D + 3 * H))  # an odd-spaced record (invalid without interval)
    e = np.cumsum(g.normal(0.0, 1e-4, len(ts))) * 0.1
    rate = np.round((1e-3 if i % 2 else -6e-4) + e, 8)
    expl = pd.array([pd.NA if sym in ("TRNUSDT", "GAPUSDT") else 8] * len(ts), dtype="Int64")
    ka = ts + MIN
    if sym == "CCCUSDT":  # late REST-like records: known_at not monotone in funding_ts (fix round 1, m3)
        late = (ts >= START + 75 * D) & (ts < START + 76 * D)
        ka = np.where(late, ts + 9 * H, ka)
    return pd.DataFrame({"funding_ts": ts, "rate": rate, "interval_h_explicit": expl,
                         "mark_at_settle": np.nan, "known_at": ka})


def _bars1m(b15: pd.DataFrame, windows: list[tuple[int, int]], seed: int, i: int,
            holes: list[tuple[int, int]], zero: list[tuple[int, int]]) -> pd.DataFrame:
    g = _rng(seed, 300 + i)
    ts = np.unique(np.concatenate([np.arange(lo, hi, MIN, dtype=np.int64) for lo, hi in windows]))
    for lo, hi in holes:
        ts = ts[(ts < lo) | (ts >= hi)]
    b = b15.set_index("open_ts")
    q = ts // Q * Q
    ts = ts[np.isin(q, b.index.to_numpy())]
    base = b.reindex(ts // Q * Q)["o"].to_numpy(np.float64)
    o = base * np.exp(g.normal(0.0, 1e-4, len(ts)))
    c = o * np.exp(g.normal(0.0, 1e-4, len(ts)))
    vol = np.ones(len(ts))
    for lo, hi in zero:
        vol[(ts >= lo) & (ts < hi)] = 0.0
    return pd.DataFrame({"open_ts": ts, "o": o, "h": np.maximum(o, c), "l": np.minimum(o, c), "c": c,
                         "vol_base": vol, "known_at": ts + MIN})


def _notice(sym: str, nid: str, kind: str, ka: int, text: str) -> NoticeVersion:
    return NoticeVersion(nid, 1, kind, ka, ka, text, hashlib.sha256(text.encode()).hexdigest())  # type: ignore


def _when(ns: int) -> str:
    return pd.Timestamp(ns, tz="UTC").strftime("%Y-%m-%d %H:%M")


def _notices(sym: str) -> tuple[NoticeVersion, ...]:
    ctx = "Binance Futures will delist the USDⓈ-M {s} perpetual contract at {w} (UTC)."
    if sym == "AAAUSDT":
        return (_notice(sym, "n-a", "delisting", R_A, ctx.format(s=sym, w=_when(S_A))),)
    if sym == "BBBUSDT":
        return (_notice(sym, "n-b", "delisting", S_B - 2 * D, ctx.format(s=sym, w=_when(S_B))),)
    if sym == "CCCUSDT":
        post = f"Binance Futures postpones the delisting of the USDⓈ-M {sym} perpetual contract."
        return (_notice(sym, "n-c", "delisting", S_C - 4 * D, ctx.format(s=sym, w=_when(S_C))),
                _notice(sym, "n-c2", "postponement", S_C - 2 * D + 7 * MIN, post))
    if sym == "GAPUSDT":  # known after sched + 72 h, no halt: the 72 h test passes at known_at (never active)
        return (_notice(sym, "n-g", "delisting", S_G + 80 * H, ctx.format(s=sym, w=_when(S_G))),)
    if sym == "LAGUSDT":  # known after sched + 72 h without 1-minute bars: HALTED, active from known_at
        return (_notice(sym, "n-l", "delisting", S_G + 75 * H, ctx.format(s=sym, w=_when(S_G))),)
    return ()


def build_inputs(seed: int = 11) -> dict[str, SymbolInputs]:
    btc_ret = np.random.default_rng([seed, 26, 999]).normal(0.0, 0.0006, N_DAYS * 96)
    out: dict[str, SymbolInputs] = {}
    lab = (LABEL_WIN[0] - D, LABEL_WIN[1] + 2 * D)
    for i, sym in enumerate(SYMBOLS):
        b15 = _bars15(sym, i, seed, btc_ret)
        insts = build_instances(b15, END)
        wins, holes, zero = [lab, (START + 20 * D, START + 21 * D)], [], []
        if sym == "AAAUSDT":
            wins.append((S_A - 2 * H, S_A + 74 * H))
            zero.append(HALT_A)
        if sym == "BBBUSDT":
            wins.append((S_B - 2 * H, S_B + 74 * H))
            holes.append(HOLE_B)
        if sym == "CCCUSDT":
            wins.append((S_C - 2 * H, S_C + 74 * H))
        if sym == "GAPUSDT":
            wins.append((S_G - 2 * H, S_G + 74 * H))
        b1 = _bars1m(b15, wins, seed, i, holes, zero)
        terms = {}
        if sym == "RELUSDT" and insts[0].last_trade_ts is not None:
            terms = {0: TerminalEvent(insts[0], int(insts[0].last_trade_ts), "B", None, None, None)}
        ident = Identity(sym.removesuffix("USDT"), "PERPETUAL", "USDT", "COIN", 1.0, True)
        out[sym] = SymbolInputs(sym, b15, b1, _funding(sym, i, seed), insts, _r4(b15, sym, i, seed), ident,
                                _notices(sym), terms)
    return out


def decision_sample(step_days: int = 9) -> list[int]:
    """Decision times over the span (every ``step_days``-th day, all three decisions) plus the U6 edges."""
    ts = [int(t) for t in decision_times(START + 62 * D, END - D)
          if (int(t) - START) // D % step_days == 0]
    special = [S_G + 75 * H + 5 * MIN, S_G + 80 * H + MIN, S_G + 80 * H, START + 76 * D + 5 * MIN,
               START + 78 * D + 5 * MIN, R_A + MIN, S_A + 72 * H - MIN, S_A + 72 * H, S_A + 72 * H + MIN,
               S_A + 72 * H + 5 * MIN, S_B + 72 * H + 5 * MIN, S_C - 2 * D + 8 * MIN, S_C - 2 * D + 7 * MIN,
               DORMANT[1] + 5 * MIN, DORMANT[1] + 16 * D + 5 * MIN, TRANSITION + 8 * H + 5 * MIN]
    return sorted(set(ts) | set(special))


def edge_times() -> list[int]:
    """Times where a 15m bar's known_at is exactly t - 1 min (and one where it is t - 1 min + 1 ns)."""
    base = START + 100 * D
    return [base + Q + MIN, base + 7 * H + Q + MIN, base + 7 * H + Q + MIN - 1,
            base + 13 * H + 22 * MIN + MIN]


# ------------------------------------------------------------------- representative scale (raw store)


def scale_store(store, n_symbols: int, n_days: int, seed: int = 3, start: int = START,
                notice_every: int = 10, write: bool = True, auth=None, sources=None) -> dict:
    """Write a synthetic universe in store form (``rawstore.RawStore``; archive known_at rule, monthly
    chunks): BTCUSDT + ``n_symbols - 1`` candidates of ``n_days`` 15m bars (R3), R4 mark / index / premium,
    8 h archive funding (R2) and, for every ``notice_every``-th symbol, a delisting notice whose 72 h window
    has dense 1-minute bars (R5; the only 1m rows a decision reads). Returns ``{symbol: SymbolMeta}``
    (``write=False``: the metadata only, deterministic, for a process reading a store written earlier).
    ``auth``: passed to ``put_chunk`` (a W_HOLD store under the opening); ``sources(table, symbol,
    month)``: the chunk's source records (default none)."""
    from luna.v7 import rawstore

    g0 = np.random.default_rng([seed, 1])
    n = n_days * 96
    ts = start + np.arange(n, dtype=np.int64) * Q
    btc_ret = g0.normal(0.0, 0.0006, n)
    end = start + n_days * D
    months = rawstore.months_between(start, end)
    syms = ["BTCUSDT"] + [f"S{i:03d}USDT" for i in range(1, n_symbols)]
    meta = {}

    def put(table, sym, df, col):
        if not write:
            return
        t = df[col].to_numpy(np.int64)
        for m in months:
            lo, hi = rawstore.month_period(m)
            a, b = np.searchsorted(t, lo), np.searchsorted(t, hi)
            if b > a:
                store.put_chunk(table, sym, m, df.iloc[a:b].reset_index(drop=True),
                                sources=sources(table, sym, m) if sources else [], auth=auth)

    for i, sym in enumerate(syms):
        g = np.random.default_rng([seed, 2, i])
        ret = btc_ret if i == 0 else 0.8 * btc_ret + g.normal(0.0, 0.0005, n)
        c = np.round((20_000.0 if i == 0 else float(g.uniform(0.5, 50.0))) * np.exp(np.cumsum(ret)), 6)
        o = np.r_[c[0], c[:-1]]
        h, lo_ = np.round(np.maximum(o, c) * 1.0002, 6), np.round(np.minimum(o, c) * 0.9998, 6)
        qv = np.round(g.uniform(0.8e6, 2.2e6, n), 2)
        bars = pd.DataFrame({"open_ts": ts, "o": o, "h": h, "l": lo_, "c": c, "vol_base": np.round(qv / c, 3),
                             "vol_quote": qv, "n_trades": g.integers(200, 900, n),
                             "taker_buy_quote": np.round(qv / 2, 2)})
        put("R3", sym, bars, "open_ts")
        prem = np.round(g.normal(0.0, 2e-4, n), 8)
        for table, cc in (("R4_mark", c), ("R4_index", np.round(c * (1 - prem), 6)), ("R4_premium", prem)):
            put(table, sym, pd.DataFrame({"open_ts": ts, "o": cc, "h": cc, "l": cc, "c": cc}), "open_ts")
        fts = np.arange(start, end, 8 * H, dtype=np.int64)
        rate = np.round(g.normal(1e-4, 1e-4, len(fts)), 8)
        put("R2_archive", sym, pd.DataFrame({"funding_ts": fts, "rate": rate,
                                             "interval_h_explicit": pd.array([8] * len(fts), dtype="Int64"),
                                             "mark_at_settle": np.nan}), "funding_ts")
        notices: tuple = ()
        if i and i % notice_every == 0:
            sched = start + (n_days - 20) * D + 9 * H
            text = f"Binance Futures will delist the USDⓈ-M {sym} perpetual contract at {_when(sched)} (UTC)."
            notices = (_notice(sym, f"n-{sym}", "delisting", sched - 3 * D, text),)
            t1 = np.arange(sched - 2 * H, sched + 74 * H, MIN, dtype=np.int64)
            o1 = np.round(np.interp(t1, ts, o), 6)
            put("R5", sym, pd.DataFrame({"open_ts": t1, "o": o1, "h": o1, "l": o1, "c": o1, "vol_base": 1.0,
                                         "vol_quote": o1, "n_trades": 1, "taker_buy_quote": 0.0}), "open_ts")
        from luna.v7.data.instances import build_instances
        ident = Identity(sym.removesuffix("USDT"), "PERPETUAL", "USDT", "COIN", 1.0, True)
        meta[sym] = rawstore.SymbolMeta(build_instances(bars, end), ident, notices, {}, None)
    return meta
