"""Seeded synthetic universe for the end-to-end determinism test (no market data, nothing under var/).

12 symbols (BTCUSDT + 11 candidates) over 400 days from 2022-01-01, inside W_DEV on the real calendar (no
W_HOLD / M6 overlap, so every guard runs unmodified):

* 15-minute last-price bars for every symbol over the whole span (raw archive-style frames in ms, split into
  overlapping "monthly" / "daily" parts and merged by ``data.klines.normalise`` / ``union``);
* 1-minute bars only where something reads them (sparse): densely over the book window (+ 1 day) and in
  +-65 min around 00:20 UTC of every label day (entry t_e = 00:20, exit t_x = next day 00:20 for h = 24);
* 8 h funding records (archive frame with an explicit interval, through ``data.funding.merge`` with a small
  REST fill for one symbol), R4 premium / mark / index bars;
* ``RELUSDT`` is relisted: no volume > 0 bar for 10 days (5 days of exchange zero-volume bars, then missing
  bars) -> two contract instances (DEC-35);
* ``TRNUSDT`` has no explicit funding interval and switches from 8 h to 4 h settlements on 2022-06-01 -> an
  EC-9 regime transition (``interval_transition``).

* ``1000PEPEUSDT`` carries the identity multiplier 1000 (prices are per contract, so quantities are not
  rescaled: ``qty * price`` is the leg notional);
* synthetic aggTrades (E_M) over the dense 1-minute windows.

TEST-ONLY ingest path (ruling R55 round 1): research entry points refuse a universe without a verified store
ingest ledger. ``LedgeredSource`` is an ``InMemorySource`` with an in-memory ingest ledger whose
``check_ingest`` is a real check (an empty ledger passes; a non-``stored`` or conflict record overlapping the
window refuses, IntegrityBlocker); ``ledgered(u)`` binds that check to a universe built directly from inputs.
Production code has no in-memory path (``runner.InMemorySource`` has no ``check_ingest``).

The per-decision wiring (gates, h-hat, beta, 24 h quote volume, candidate rows, fold-model routing) is
``luna.v7.runner`` (Task 19b); this module only builds the seeded ``runner.SymbolInputs``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from luna.v7.data import funding as fund_mod
from luna.v7.data import klines
from luna.v7.data.instances import build_instances
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.features import R4Bars
from luna.v7.models import Config
from luna.v7.runner import InMemorySource, ResearchUniverse, SymbolInputs
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.universe import Identity
from luna.v7.windows import Fold, decision_times

Q = 15 * MIN
START = to_ns("2022-01-01")
N_DAYS = 400
END = START + N_DAYS * D  # 2023-02-05, data end
NOW = to_ns("2026-10-15")  # injected guard clock (never the wall clock)
BTC = "BTCUSDT"
RELIST, TRANS = "RELUSDT", "TRNUSDT"
# candidate symbols and their mean 8 h funding rate (sign: > 0 favours the short side)
MU: dict[str, float] = {
    "AAAUSDT": 1.6e-3, "BBBUSDT": 1.3e-3, "CCCUSDT": 1.1e-3, RELIST: 6e-4, "DDDUSDT": 2e-4,
    TRANS: -2e-4, "EEEUSDT": -6e-4, "FFFUSDT": -1.1e-3, "GGGUSDT": -1.4e-3, "HHHUSDT": -1.7e-3,
    "1000PEPEUSDT": 9e-4,
}
ALTS: tuple[str, ...] = tuple(sorted(MU))
SYMBOLS: tuple[str, ...] = (BTC, *ALTS)
DORMANT = (to_ns("2022-04-10"), to_ns("2022-04-20"))  # RELUSDT: no volume > 0 bar in [lo, hi)
TRANSITION_TS = to_ns("2022-06-01")  # TRNUSDT: 8 h -> 4 h settlements (no explicit interval field)
REST_FILL = ("CCCUSDT", (to_ns("2022-08-01"), to_ns("2022-08-02")))  # archive lacks these: REST fills them

H_STAR = 24
FOLD = Fold(0, to_ns("2022-11-01"), to_ns("2023-01-01"), (START, to_ns("2022-07-01")),
            (to_ns("2022-07-01"), to_ns("2022-09-01")), (to_ns("2022-09-01"), to_ns("2022-11-01")))
SAMPLE_LO, SAMPLE_HI, SAMPLE_STEP_D = to_ns("2022-03-03"), to_ns("2022-12-31"), 4
BOOK = (to_ns("2022-12-05"), to_ns("2022-12-09"))  # inside the fold's test quarter (4 days)
PEPE = "1000PEPEUSDT"
MULTIPLIER = {PEPE: 1000.0}  # identity multiplier (immutable R1 field); 1 elsewhere

SMALL = {"num_leaves": 4, "min_data_in_leaf": 20}
# test-only stand-in for the frozen search space (as tests/v7/test_cv.py); the EN alphas are large enough for
# coordinate descent to converge on labels in bps (no ConvergenceWarning under -W error)
TINY_SPACE = [
    Config("en", {"alpha": 1.0, "l1_ratio": 0.2}),
    Config("en", {"alpha": 10.0, "l1_ratio": 0.8}),
    Config("lgb_reg", SMALL),
    Config("lgb_reg", {"num_leaves": 8, "min_data_in_leaf": 40}),
    Config("logit_en", {"alpha": 1e-2, "l1_ratio": 0.5}),
    Config("lgb_clf", SMALL),
]


def sample_times() -> np.ndarray:
    days = np.arange(SAMPLE_LO, SAMPLE_HI, SAMPLE_STEP_D * D, dtype=np.int64)
    return days + 5 * MIN  # the 00:05 decision


# ------------------------------------------------------------------------------------------- raw data


def _rng(seed: int, i: int) -> np.random.Generator:
    return np.random.default_rng([seed, i])


def _grid15() -> np.ndarray:
    return np.arange(START, END, Q, dtype=np.int64)


def _btc_returns(seed: int) -> np.ndarray:
    return _rng(seed, 999).normal(0.0, 0.0006, len(_grid15()))


def _raw_klines(sym: str, i: int, seed: int, btc_ret: np.ndarray) -> pd.DataFrame:
    """Archive-style raw 15m klines (ms times, string-free numeric columns)."""
    g = _rng(seed, i)
    ts = _grid15()
    n = len(ts)
    if sym == BTC:
        ret, p0 = btc_ret, 20_000.0
    else:
        ret, p0 = 0.8 * btc_ret + g.normal(0.0, 0.0004, n), float(g.uniform(0.5, 50.0))
    c = p0 * np.exp(np.cumsum(ret))
    o = np.concatenate([[p0], c[:-1]])
    wig = np.abs(g.normal(0.0, 1e-4, (2, n)))
    h = np.maximum(o, c) * (1.0 + wig[0])
    lo = np.minimum(o, c) * (1.0 - wig[1])
    qv = g.uniform(0.8e6, 2.2e6, n)
    vol = qv / c
    cnt = g.integers(200, 900, n)
    keep = np.ones(n, bool)
    if sym == RELIST:  # 5 days of exchange zero-volume flat bars, then 5 days without bars
        mid = DORMANT[0] + 5 * D
        z = (ts >= DORMANT[0]) & (ts < mid)
        last = c[np.flatnonzero(ts < DORMANT[0])[-1]]
        o[z] = h[z] = lo[z] = c[z] = last
        vol[z], qv[z], cnt[z] = 0.0, 0.0, 0
        keep &= ~((ts >= mid) & (ts < DORMANT[1]))
    df = pd.DataFrame({
        "open_time": ts // 10**6, "open": o, "high": h, "low": lo, "close": c, "volume": vol,
        "close_time": (ts + Q) // 10**6 - 1, "quote_volume": qv, "count": cnt,
        "taker_buy_volume": vol * 0.5, "taker_buy_quote_volume": qv * 0.5, "ignore": 0,
    })
    return df[keep].reset_index(drop=True)


def _bars1m(b15: pd.DataFrame, mask_ts: np.ndarray, seed: int, i: int) -> pd.DataFrame:
    """1m bars at ``mask_ts`` (opens), priced around the containing 15m bar's open (volume 1)."""
    g = _rng(seed, 10_000 + i)
    b = b15.set_index("open_ts")
    q = mask_ts // Q * Q
    ok = np.isin(q, b.index.to_numpy()) & (b.reindex(q)["vol_base"].to_numpy() > 0)
    ts = mask_ts[ok]
    base = b.reindex(ts // Q * Q)["o"].to_numpy(np.float64)
    o = base * np.exp(g.normal(0.0, 1e-4, len(ts)))
    c = o * np.exp(g.normal(0.0, 1e-4, len(ts)))
    return pd.DataFrame({"open_ts": ts, "o": o, "h": np.maximum(o, c), "l": np.minimum(o, c), "c": c,
                         "vol_base": 1.0, "known_at": ts + MIN})


def _mask_1m(dense: Sequence[tuple[int, int]]) -> np.ndarray:
    parts = [np.arange(lo - D, hi, MIN, dtype=np.int64) for lo, hi in dense]
    for t in sample_times():
        for anchor in (t + 15 * MIN, t + 15 * MIN + H_STAR * H):
            parts.append(np.arange(anchor - 65 * MIN, anchor + 66 * MIN, MIN, dtype=np.int64))
    return np.unique(np.concatenate(parts))


def _aggtrades(b1: pd.DataFrame, dense: Sequence[tuple[int, int]], seed: int, i: int) -> pd.DataFrame:
    """Two trades per 1-minute bar inside the dense windows, priced around the bar close (E_M fills)."""
    g = _rng(seed, 40_000 + i)
    ts = b1["open_ts"].to_numpy(np.int64)
    m = np.zeros(len(ts), bool)
    for lo, hi in dense:
        m |= (ts >= lo) & (ts < hi)
    c = b1["c"].to_numpy(np.float64)[m]
    t0 = ts[m]
    tt = np.concatenate([t0 + 20 * 10**9, t0 + 40 * 10**9])
    px = np.concatenate([c, c]) * np.exp(g.normal(0.0, 5e-4, 2 * len(c)))
    qty = 3000.0 / px * g.uniform(0.5, 1.5, len(px))
    o = np.argsort(tt, kind="stable")
    return pd.DataFrame({"ts": tt[o], "price": px[o], "qty": qty[o]})


def _raw_funding(sym: str, i: int, seed: int, mu: Mapping[str, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(archive frame: calc_time ms, funding_interval_hours, last_funding_rate; REST frame)."""
    g = _rng(seed, 20_000 + i)
    if sym == TRANS:
        ts = np.concatenate([np.arange(START, TRANSITION_TS, 8 * H), np.arange(TRANSITION_TS, END, 4 * H)])
    else:
        ts = np.arange(START, END, 8 * H, dtype=np.int64)
    mu_s = mu.get(sym, 1e-4)
    e = np.zeros(len(ts))
    for k in range(1, len(ts)):
        e[k] = 0.7 * e[k - 1] + g.normal(0.0, 1e-4)
    rate = np.round(mu_s + e, 8)
    if sym == TRANS:
        rate = np.where(ts >= TRANSITION_TS, np.round(rate / 2, 8), rate)
    arch = pd.DataFrame({"calc_time": ts // 10**6, "last_funding_rate": rate})
    if sym != TRANS:  # TRNUSDT has no explicit interval field (DEC-29 inference)
        arch.insert(1, "funding_interval_hours", 8)
    rest = pd.DataFrame(columns=["fundingTime", "fundingRate", "markPrice"])
    if sym == REST_FILL[0]:
        lo, hi = REST_FILL[1]
        gap = (ts >= lo) & (ts < hi)
        rest = pd.DataFrame({"fundingTime": ts[gap] // 10**6, "fundingRate": rate[gap], "markPrice": ""})
        arch = arch[~gap].reset_index(drop=True)
    return arch, rest


def build_inputs(seed: int, dense: Sequence[tuple[int, int]] = (BOOK,),
                 mu: Mapping[str, float] | None = None) -> dict[str, SymbolInputs]:
    """Ingestion normalisation -> instances (seeded) -> ``runner.SymbolInputs`` per symbol. ``dense``: the
    windows with dense 1-minute bars and aggTrades (each starts one day earlier for the 1m bars); ``mu``
    overrides the mean funding rates."""
    mu_all = {**MU, **(mu or {})}
    btc_ret = _btc_returns(seed)
    mask = _mask_1m(dense)
    out: dict[str, SymbolInputs] = {}
    for i, sym in enumerate(SYMBOLS):
        raw = _raw_klines(sym, i, seed, btc_ret)
        raw.insert(0, "symbol", sym)
        cut = int(len(raw) * 0.6)
        monthly = klines.normalise(raw.iloc[:cut], "ms")
        daily = klines.normalise(raw.iloc[cut - 500:], "ms")  # overlapping keys agree (DEC-33)
        b15 = klines.union(monthly, daily).drop(columns="symbol").reset_index(drop=True)
        insts = build_instances(b15, END)
        arch, rest = _raw_funding(sym, i, seed, mu_all)
        f = fund_mod.merge(fund_mod.normalise_archive(arch, "ms"), fund_mod.normalise_rest(rest, "ms"))
        g = _rng(seed, 30_000 + i)
        prem = g.normal(0.0, 2e-4, len(b15)) + mu_all.get(sym, 0.0) / 4.0
        mark = b15[["open_ts"]].assign(c=b15["c"].to_numpy() * (1.0 + prem / 2.0), known_at=b15["known_at"])
        index = b15[["open_ts"]].assign(c=mark["c"].to_numpy() / (1.0 + prem), known_at=b15["known_at"])
        premium = b15[["open_ts"]].assign(c=prem, known_at=b15["known_at"])
        b1 = _bars1m(b15, mask, seed, i)
        mult = MULTIPLIER.get(sym, 1.0)
        ident = Identity(sym.removesuffix("USDT"), "PERPETUAL", "USDT", "COIN", mult, True)
        out[sym] = SymbolInputs(sym, b15, b1, f, insts, R4Bars(premium, mark, index), ident, notices=(),
                                terminals={}, aggtrades=_aggtrades(b1, dense, seed, i))
    return out


class LedgeredSource(InMemorySource):
    """TEST ONLY (module docstring): ``InMemorySource`` + an in-memory ingest ledger (records in the store
    ledger format: table, symbol, month, status, reason, conflict)."""

    def __init__(self, inputs: Mapping[str, SymbolInputs], ledger: Sequence[Mapping] = ()) -> None:
        super().__init__(inputs)
        self.ledger = [dict(r) for r in ledger]

    def check_ingest(self, lo: int, hi: int) -> None:
        bad = []
        for r in self.ledger:
            if r.get("status") == "stored":
                continue
            m = pd.Timestamp(str(r.get("month")) + "-01", tz="UTC")
            p_lo, p_hi = int(m.value), int((m + pd.DateOffset(months=1)).value)
            if r.get("conflict") is True or (p_lo < int(hi) and int(lo) < p_hi):
                bad.append(r)
        if bad:
            raise IntegrityBlocker(f"in-memory ledger: unresolved records {bad}")


def ledgered(u: ResearchUniverse, ledger: Sequence[Mapping] = ()) -> ResearchUniverse:
    """TEST ONLY: bind a ``LedgeredSource`` ingest check (over all time) to a universe built from inputs."""
    u._ingest_check = (LedgeredSource(u.inputs, ledger).check_ingest, -(2**62), 2**62)
    return u


def build_universe(seed: int, dense: Sequence[tuple[int, int]] = (BOOK,),
                   mu: Mapping[str, float] | None = None) -> ResearchUniverse:
    return ledgered(ResearchUniverse(build_inputs(seed, dense, mu)))


def decisions_in(window: tuple[int, int]) -> np.ndarray:
    return decision_times(*window)
