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

``DecisionProvider`` is the test-side wiring of the per-decision inputs the engine expects (gates, h-hat,
beta, 24 h quote volume, gated candidate rows with A0 inputs and §9 features); no ``luna.v7`` module provides
it yet (Task 19 integration finding). ``FoldScorer`` adapts a ``cv.FoldModel`` to ``engine.ScoreModel``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v7 import features
from luna.v7.arms import cost_side
from luna.v7.beta import beta_hat, hourly_close_from_15m, hourly_known_at_from_15m
from luna.v7.book.engine import DecisionView, Instrument
from luna.v7.cv import FoldModel
from luna.v7.data import funding as fund_mod
from luna.v7.data import klines
from luna.v7.data.instances import Instance, build_instances, current_instance
from luna.v7.data.intervals import annotate
from luna.v7.features import R4Bars, XSInput
from luna.v7.labels import LegData
from luna.v7.models import Config
from luna.v7.spread import half_spread
from luna.v7.timeutil import MIN, D, H, ns_array, to_ns
from luna.v7.universe import Identity, SymbolData, eligible, pit_rows
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
BOOK = (to_ns("2022-12-05"), to_ns("2022-12-08"))  # inside the fold's test quarter

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


def _mask_1m() -> np.ndarray:
    dense = np.arange(BOOK[0] - D, BOOK[1], MIN, dtype=np.int64)
    parts = [dense]
    for t in sample_times():
        for anchor in (t + 15 * MIN, t + 15 * MIN + H_STAR * H):
            parts.append(np.arange(anchor - 65 * MIN, anchor + 66 * MIN, MIN, dtype=np.int64))
    return np.unique(np.concatenate(parts))


def _raw_funding(sym: str, i: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(archive frame: calc_time ms, funding_interval_hours, last_funding_rate; REST frame)."""
    g = _rng(seed, 20_000 + i)
    if sym == TRANS:
        ts = np.concatenate([np.arange(START, TRANSITION_TS, 8 * H), np.arange(TRANSITION_TS, END, 4 * H)])
    else:
        ts = np.arange(START, END, 8 * H, dtype=np.int64)
    mu = MU.get(sym, 1e-4)
    e = np.zeros(len(ts))
    for k in range(1, len(ts)):
        e[k] = 0.7 * e[k - 1] + g.normal(0.0, 1e-4)
    rate = np.round(mu + e, 8)
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


@dataclass
class Universe:
    """Normalised per-symbol inputs (the outputs of the ingestion / instance / interval stages)."""

    bars15: dict[str, pd.DataFrame]
    bars1m: dict[str, pd.DataFrame]
    funding: dict[str, pd.DataFrame]  # merged records (funding_ts, rate, interval_h_explicit, known_at, ...)
    funding_annot: dict[str, pd.DataFrame]
    r4: dict[str, R4Bars]
    insts: dict[str, list[Instance]]
    sd: dict[str, SymbolData] = field(default_factory=dict)
    instruments: dict[str, Instrument] = field(default_factory=dict)
    legs: dict[str, LegData] = field(default_factory=dict)


def build_universe(seed: int) -> Universe:
    """Ingestion normalisation -> instances -> interval annotation for every symbol (seeded)."""
    btc_ret = _btc_returns(seed)
    mask = _mask_1m()
    u = Universe({}, {}, {}, {}, {}, {})
    for i, sym in enumerate(SYMBOLS):
        raw = _raw_klines(sym, i, seed, btc_ret)
        raw.insert(0, "symbol", sym)
        cut = int(len(raw) * 0.6)
        monthly = klines.normalise(raw.iloc[:cut], "ms")
        daily = klines.normalise(raw.iloc[cut - 500:], "ms")  # overlapping keys agree (DEC-33)
        b15 = klines.union(monthly, daily).drop(columns="symbol").reset_index(drop=True)
        insts = build_instances(b15, END)
        arch, rest = _raw_funding(sym, i, seed)
        f = fund_mod.merge(fund_mod.normalise_archive(arch, "ms"), fund_mod.normalise_rest(rest, "ms"))
        g = _rng(seed, 30_000 + i)
        prem = g.normal(0.0, 2e-4, len(b15)) + MU.get(sym, 0.0) / 4.0
        mark = b15[["open_ts"]].assign(c=b15["c"].to_numpy() * (1.0 + prem / 2.0), known_at=b15["known_at"])
        index = b15[["open_ts"]].assign(c=mark["c"].to_numpy() / (1.0 + prem), known_at=b15["known_at"])
        premium = b15[["open_ts"]].assign(c=prem, known_at=b15["known_at"])
        u.bars15[sym], u.insts[sym], u.funding[sym] = b15, insts, f
        u.funding_annot[sym] = annotate(f, insts)
        u.bars1m[sym] = _bars1m(b15, mask, seed, i)
        u.r4[sym] = R4Bars(premium, mark, index)
    for sym in SYMBOLS:
        ident = Identity(sym.removesuffix("USDT"), "PERPETUAL", "USDT", "COIN", 1.0, True)
        u.sd[sym] = SymbolData(sym, u.bars15[sym], u.bars1m[sym], u.funding_annot[sym], u.insts[sym],
                               identity=ident)
        mark15 = u.r4[sym].mark[["open_ts", "c"]]
        u.instruments[sym] = Instrument(sym, u.bars1m[sym], u.bars15[sym], mark15, u.funding[sym],
                                        u.insts[sym])
        u.legs[sym] = LegData(u.bars1m[sym], u.bars15[sym], mark15, u.funding[sym], u.insts[sym])
    return u


# ------------------------------------------------------------------------------------------- decision inputs


def _instance_bars(sd: SymbolData, t: int) -> pd.DataFrame | None:
    cur = current_instance(sd.insts, t)
    if cur is None:
        return None
    b = pit_rows(sd.bars15, t, f"{sd.symbol}.bars15")
    return b[ns_array(b["open_ts"], "open_ts") >= cur.first_bar_ts]


def h_hat(sd: SymbolData, t: int) -> float | None:
    b = _instance_bars(sd, t)
    return None if b is None or len(b) == 0 else half_spread(b, t)


def beta_at(sd: SymbolData, btc: SymbolData, t: int) -> float | None:
    a, b = _instance_bars(sd, t), _instance_bars(btc, t)
    if a is None or b is None or len(a) == 0 or len(b) == 0:
        return None
    return beta_hat(hourly_close_from_15m(a), hourly_close_from_15m(b), t, hourly_known_at_from_15m(a),
                    hourly_known_at_from_15m(b))


def qv24(sd: SymbolData, t: int) -> float | None:
    """Trailing 24 h quote volume of the current instance: bars closing in (t - 24 h, t - 1 min]."""
    b = _instance_bars(sd, t)
    if b is None:
        return None
    close = ns_array(b["open_ts"], "open_ts") + Q
    m = (close > t - D) & (close <= t - MIN)
    return float(b.loc[m, "vol_quote"].sum()) if m.any() else None


@dataclass
class DecisionProvider:
    """Point-in-time ``DecisionView`` per decision t (cached per t within one pipeline run)."""

    u: Universe
    with_features: bool
    cache: dict[int, DecisionView] = field(default_factory=dict)

    def xs(self, t: int) -> list[XSInput]:
        return [features.xs_input(self.u.sd[s], self.u.r4[s], t) for s in ALTS]

    def candidates(self, t: int, xs: list[XSInput], hh: dict[str, float | None]) -> pd.DataFrame:
        rows = []
        for x in xs:
            if not eligible(x.gate):
                continue
            for side in (1, -1):
                r = dict(symbol=x.symbol, side=side, gate_ok=True, fl30_r8=x.values["fl30"],
                         cost_side=cost_side(hh[x.symbol]))  # eligible => U5 => h-hat finite
                if self.with_features:
                    r.update(features.compute(self.u.sd[x.symbol], self.u.sd[BTC], xs, t, side,
                                              r4=self.u.r4[x.symbol]))
                    r["side"] = side
                rows.append(r)
        cols = ["symbol", "side", "gate_ok", "fl30_r8", "cost_side"]
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=cols)

    def __call__(self, t: int) -> DecisionView:
        t = int(t)
        if t not in self.cache:
            xs = self.xs(t)
            hh = {s: h_hat(self.u.sd[s], t) for s in ALTS}
            betas = {s: beta_at(self.u.sd[s], self.u.sd[BTC], t) for s in ALTS}
            vols = {s: qv24(self.u.sd[s], t) for s in ALTS}
            gates = {x.symbol: x.gate for x in xs}
            self.cache[t] = DecisionView(t, gates, hh, betas, vols, self.candidates(t, xs, hh), False)
        return self.cache[t]


@dataclass
class FoldScorer:
    """``engine.ScoreModel`` over one fold model: adds the ``FoldModel.predict`` columns to the candidates."""

    fm: FoldModel
    ml_star: str = "A2"

    def predict(self, t: int, cands: pd.DataFrame) -> pd.DataFrame:
        if not (FOLD.test_lo <= t < FOLD.test_hi):
            raise ValueError("the decision is outside the fold model's test quarter")
        return pd.concat([cands, self.fm.predict(cands)], axis=1)


def decisions_in(window: tuple[int, int]) -> np.ndarray:
    return decision_times(*window)
