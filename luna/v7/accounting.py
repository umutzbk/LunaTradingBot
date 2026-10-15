"""Book accounting objects: the only canonical §8 definitions (spec S-8.1-S-8.4, K10, S-13.2 P6 / P11, §12).

``account(run) -> Ledger`` reads one executed ``BookRun`` (Task 14b; schema in its report) and never
re-simulates anything. An invalid run (``run.invalid`` set) is refused (``ValueError``, fail closed).

* **S-8.2:** F_leg / F_hedge / F_net from ``funding_flows`` (by ``role``); C_exec = Σ fee + spread + slippage
  over every fill (legs vs hedge); PR = Σ_j PR_j; NET = F_net − C_exec + PR; CAC = F_net − C_exec.
* **S-8.3:** the event times of a position are its fills and the 15-minute ticks while it is held (the
  ``positions`` rows); at one timestamp the tick comes first and fills keep booking order. p(e) = the fill's
  ``ref_mid`` / the tick's ``mark``; q(e⁺) = the position after e. PR_j = Σ_k q(e_k⁺)(p(e_{k+1}) − p(e_k)).
  An instrument j is one (symbol, instance) (E10); leg and hedge books are separate instruments.
* **Episodes:** maximal intervals with a non-zero, constant-sign leg position of one (symbol, instance);
  β̂_episode = the ``beta`` of the episode's opening fill (β̂ of the opening decision). **Clusters:** episodes
  of the same (instance, side), each starting ≤ ``gap_h`` (72) h after the previous one ended; N_a and the
  per-asset ρ pool a symbol's instances (asset = symbol, AMB-21 (a)). PR_c, mkt_c = Σ q(e_k⁺) p(e_k) β̂
  (B(e_{k+1}) / B(e_k) − 1) with B the fill's ``btc_ref`` / the tick's ``marks.btc_mark``; ρ_c = PR_c −
  mkt_c; MKT = Σ mkt_c + PR_BTC. AMB-21 (b) (user decision 2026-10-15): the engine turns a required missing
  B into ``INVALID RUN`` (refused here), so a valid run never reaches this with a missing B; a missing B
  still raises (defence in depth, never an invented price).
* **K10:** |Σ ρ_c + MKT − PR| ≤ 1e-9 · Σ |qty · ref_mid| and PR equal to the engine's daily price column
  (per day and in total, same tolerance; AMB-21 (c)); summation by parts holds (q_after = q + dq up to the
  engine's 1e-9 close clamp, and −Σ dq·p(f) = PR). A held position without a tick row raises.
* **S-8.4:** N_a = Σ over a's clusters of (leg funding + PR_c − leg C_exec); H = F_hedge + PR_BTC − hedge
  C_exec; NET = Σ_a N_a + H.
* **P11:** NET_trim = NET − Σ_{Top_m} max(ρ_c, 0) − max(MKT, 0), m = max(3, ⌈0.05 n_clusters⌉).
* **P6:** ``stressed(run)``: the same executed book, every fill's fee × 1.5, slippage × 1.5, spread × 3.0.

The daily table is recomputed from the events with the engine's day boundaries: a fill (and its price
event) belongs to the day with start ≤ ts < end (the unwind fill to the last day); a tick's price event and a
funding settlement to the day with start < ts ≤ end (the 00:00 tick / settlement close a day, AMB-19 (f)).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from fractions import Fraction

import numpy as np
import pandas as pd

from luna.v7.book.engine import BookRun
from luna.v7.constants import FROZEN
from luna.v7.timeutil import H, ns_array

K10_REL = FROZEN.k10_tol  # K10 tolerance: 1e-9 x sum |fill notional|
GAP_H = FROZEN.cluster_gap_h  # S-8.3 cluster gap
M_MIN, _M_FRAC = FROZEN.accept.p11_m  # P11: m = max(3, ceil(0.05 n_clusters))
M_FRAC = Fraction(_M_FRAC).limit_denominator(1000)  # the exact rational 1/20 (integer ceiling below)
if float(M_FRAC) != _M_FRAC:
    raise ValueError(f"P11 fraction {_M_FRAC!r} has no exact small rational form")
_Q_REL = 1e-9  # q_after vs q_before + dq: the engine books an exit of >= (1 - 1e-9) |q| as a close


@dataclass(frozen=True)
class Episode:
    """One leg episode (S-8.3) with its event-sum attribution, funding and execution costs."""

    symbol: str
    instance: int
    side: int
    start_ts: int
    end_ts: int | None
    beta: float
    position_ids: tuple[str, ...]
    pr: float
    mkt: float
    funding: float
    fees: float
    spread: float
    slippage: float
    n_fills: int

    @property
    def asset(self) -> str:
        return f"{self.symbol}#{self.instance}"

    @property
    def closed(self) -> bool:
        return self.end_ts is not None

    @property
    def cost(self) -> float:
        return self.fees + self.spread + self.slippage


@dataclass(frozen=True)
class Cluster:
    """A maximal chain of same-(asset, side) episodes with gaps ≤ 72 h (S-8.3)."""

    asset: str
    side: int
    episodes: tuple[Episode, ...]

    @property
    def pr_c(self) -> float:
        return math.fsum(e.pr for e in self.episodes)

    @property
    def mkt_c(self) -> float:
        return math.fsum(e.mkt for e in self.episodes)

    @property
    def symbol(self) -> str:
        return self.episodes[0].symbol

    @property
    def rho_c(self) -> float:
        return self.pr_c - self.mkt_c

    @property
    def funding(self) -> float:
        return math.fsum(e.funding for e in self.episodes)

    @property
    def cost(self) -> float:
        return math.fsum(e.cost for e in self.episodes)

    @property
    def net(self) -> float:
        """The cluster's S-8.4 contribution: leg funding + PR_c − leg C_exec."""
        return self.funding + self.pr_c - self.cost

    @property
    def n_closed(self) -> int:
        return sum(e.closed for e in self.episodes)


@dataclass(frozen=True)
class Ledger:
    """The §8 objects of one executed book (not the engine's cash ledger, ``luna.v7.book.ledger``)."""

    arm: str
    execution: str
    h_star: int
    window: tuple[int, int]
    stress: tuple[float, float, float] | None  # (fee, slip, spread) multipliers of a P6 ledger
    fills: pd.DataFrame
    episodes: tuple[Episode, ...]
    clusters: tuple[Cluster, ...]
    F_leg: float
    F_leg_credits: float
    F_leg_debits: float
    F_hedge: float
    fees_leg: float
    spread_leg: float
    slip_leg: float
    fees_hedge: float
    spread_hedge: float
    slip_hedge: float
    PR: float
    PR_legs: float
    PR_BTC: float
    PR_fills: float  # −Σ dq · ref_mid (S-8.3 summation by parts; checked by K10)
    MKT: float
    fill_notional: float
    equity_change: float  # the engine's equity change over the window
    daily: pd.DataFrame  # recomputed from the events
    daily_engine: pd.DataFrame = field(repr=False)
    q_breaks: tuple[str, ...] = ()  # fills whose q_after != q_before + dq beyond the close clamp (K10)

    @property
    def F_net(self) -> float:
        return self.F_leg + self.F_hedge

    @property
    def fees(self) -> float:
        return self.fees_leg + self.fees_hedge

    @property
    def spread(self) -> float:
        return self.spread_leg + self.spread_hedge

    @property
    def slippage(self) -> float:
        return self.slip_leg + self.slip_hedge

    @property
    def C_leg(self) -> float:
        return self.fees_leg + self.spread_leg + self.slip_leg

    @property
    def C_hedge(self) -> float:
        return self.fees_hedge + self.spread_hedge + self.slip_hedge

    @property
    def C_exec(self) -> float:
        return self.C_leg + self.C_hedge

    @property
    def NET(self) -> float:
        return self.F_net - self.C_exec + self.PR

    @property
    def CAC(self) -> float:
        return self.F_net - self.C_exec

    @property
    def H(self) -> float:
        """S-8.4 hedge unit: F_hedge + PR_BTC − hedge C_exec."""
        return self.F_hedge + self.PR_BTC - self.C_hedge

    @property
    def rho_sum(self) -> float:
        return math.fsum(c.rho_c for c in self.clusters)

    @property
    def n_clusters(self) -> int:
        """Clusters with at least one closed episode (P4, P11)."""
        return sum(c.n_closed >= 1 for c in self.clusters)


# --- input checks ------------------------------------------------------------------------------------------
def _refuse_invalid(run: BookRun) -> None:
    if run.invalid is not None:
        raise ValueError(f"accounting refuses an invalid run: {run.invalid}"
                         + (f" ({run.invalid_detail})" if run.invalid_detail else ""))


def _pos(x: object, what: str) -> float:
    v = _num(x)
    if v is None or v <= 0:
        raise ValueError(f"{what} must be finite and > 0, got {x!r}")
    return v


def _num(x: object) -> float | None:
    if x is None or x is pd.NA:
        return None
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _fin(x: object, what: str) -> float:
    v = _num(x)
    if v is None:
        raise ValueError(f"{what} must be finite, got {x!r}")
    return v


def _cost(x: object, what: str) -> float:
    v = _fin(x, what)
    if v < 0:
        raise ValueError(f"{what} must be >= 0, got {v}")
    return v


def _records(df: pd.DataFrame, cols: tuple[str, ...], what: str) -> list[dict]:
    if df is None or len(df) == 0:
        return []
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{what}: missing columns {missing}")
    recs = df.to_dict("records")
    tss = ns_array(df["ts"].to_numpy(), f"{what}.ts")
    for r, t in zip(recs, tss, strict=True):
        r["ts"] = int(t)
    return recs


def _inst(x: object, what: str) -> int:
    """An instance index: a non-negative integer (never None / NaN / bool / fractional), else ValueError."""
    if isinstance(x, bool) or x is None or x is pd.NA:
        raise ValueError(f"{what}: instance must be a non-negative integer, got {x!r}")
    v = _num(x)
    if v is None or v < 0 or v != math.floor(v):
        raise ValueError(f"{what}: instance must be a non-negative integer, got {x!r}")
    return int(v)


def _need(df: pd.DataFrame, cols: tuple[str, ...], what: str) -> None:
    """Malformed input is a ValueError (never KeyError), so a required run maps to INVALID K10 (DK-112)."""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{what}: missing columns {missing}")


# the engine's daily table (book.ledger.DAILY_COLS) as read here and by k10_ok / daily_ok / stressed
_DAILY_COLS = ("day", "start_ts", "end_ts", "equity_start", "equity_end", "net", "funding", "fees",
               "spread_slip", "price")
_MARK_COLS = ("ts", "btc_mark")
_FILL_COLS = ("ts", "symbol", "instance", "direction", "qty", "ref_mid", "fee", "spread_cost", "slip_cost",
              "position_id", "role", "q_after", "beta", "btc_ref")
_TICK_COLS = ("ts", "symbol", "instance", "position_id", "role", "qty", "mark")
_FUND_COLS = ("ts", "position_id", "role", "cash")


class _Days:
    """The engine's UTC day rows: left-closed (fills) and right-closed (ticks, settlements) assignment."""

    def __init__(self, daily: pd.DataFrame) -> None:
        if daily is None or len(daily) == 0:
            raise ValueError("the run has no daily table")
        _need(daily, _DAILY_COLS, "daily")
        self.start = ns_array(daily["start_ts"].to_numpy(), "daily.start_ts")
        self.end = ns_array(daily["end_ts"].to_numpy(), "daily.end_ts")
        if np.any(self.start[1:] != self.end[:-1]) or np.any(self.end < self.start):
            raise ValueError("daily rows are not contiguous")
        self.n = len(self.start)

    def fill_day(self, ts: int) -> int:
        i = int(np.searchsorted(self.start, ts, "right")) - 1
        if i < 0 or ts > self.end[-1] or (i < self.n - 1 and ts >= self.end[i]):
            raise ValueError(f"fill at {ts} outside the daily table")
        return i

    def close_day(self, ts: int) -> int:
        i = int(np.searchsorted(self.end, ts, "left"))
        if i >= self.n or ts < self.start[0]:
            raise ValueError(f"event at {ts} outside the daily table")
        return i


@dataclass
class _Ep:
    symbol: str
    instance: int
    side: int
    start_ts: int
    beta: float
    pids: list[str] = field(default_factory=list)
    end_ts: int | None = None
    pr: float = 0.0
    mkt: float = 0.0
    funding: float = 0.0
    fees: float = 0.0
    spread: float = 0.0
    slip: float = 0.0
    n_fills: int = 0

    def freeze(self) -> Episode:
        return Episode(self.symbol, self.instance, self.side, self.start_ts, self.end_ts, self.beta,
                       tuple(self.pids), self.pr, self.mkt, self.funding, self.fees, self.spread, self.slip,
                       self.n_fills)


@dataclass
class _Walk:
    episodes: list[_Ep]
    pid_ep: dict[str, _Ep]
    pr_legs: float
    pr_btc: float
    price_day: np.ndarray
    fills: list[dict]
    days: _Days
    q_breaks: list[str]


def _sgn(x: float) -> int:
    return (x > 0) - (x < 0)


def _walk(run: BookRun) -> _Walk:
    """The S-8.3 event walk per instrument: PR_j, episodes, mkt (no re-simulation)."""
    _refuse_invalid(run)
    days = _Days(run.daily)
    fills = _records(run.fills, _FILL_COLS, "fills")
    ticks = _records(run.positions, _TICK_COLS, "positions")
    btc_mark: dict[int, float | None] = {}
    if run.marks is not None and len(run.marks):
        _need(run.marks, _MARK_COLS, "marks")
        for t, b in zip(ns_array(run.marks["ts"].to_numpy(), "marks.ts"), run.marks["btc_mark"], strict=True):
            v = _num(b)
            btc_mark[int(t)] = v if v is not None and v > 0 else None
    events: list[tuple[int, int, int, dict]] = []
    for i, f in enumerate(fills):
        if f["role"] not in ("leg", "hedge") or f["direction"] not in (1, -1):
            raise ValueError(f"fill {i}: bad role / direction")
        f["qty"] = _pos(f["qty"], f"fill {i} qty")
        f["ref_mid"] = _pos(f["ref_mid"], f"fill {i} ref_mid")
        for c in ("fee", "spread_cost", "slip_cost"):
            f[c] = _cost(f[c], f"fill {i} {c}")
        f["q_after"] = _fin(f["q_after"], f"fill {i} q_after")
        f["instance"] = _inst(f["instance"], f"fill {i}")
        events.append((f["ts"], 1, i, f))
    for j, k in enumerate(ticks):
        k["mark"] = _pos(k["mark"], f"tick {j} mark")
        k["qty"] = _fin(k["qty"], f"tick {j} qty")
        k["instance"] = _inst(k["instance"], f"tick {j}")
        events.append((k["ts"], 0, j, k))
    events.sort(key=lambda e: e[:3])  # S-8.3: tick before fill at one ts; fills keep booking order

    streams: dict[tuple[str, str, int], list[tuple[int, int, dict]]] = {}
    for ts, kind, _, row in events:
        streams.setdefault((str(row["role"]), str(row["symbol"]), int(row["instance"])), []).append(
            (ts, kind, row))

    price_day = np.zeros(days.n)
    episodes: list[_Ep] = []
    pid_ep: dict[str, _Ep] = {}
    pr_legs: list[float] = []
    pr_btc: list[float] = []
    q_breaks: list[str] = []
    for (role, sym, inst), stream in sorted(streams.items()):
        leg = role == "leg"
        qs: dict[str, float] = {}
        p_prev: float | None = None
        b_prev: float | None = None
        ep: _Ep | None = None
        for ts, kind, row in stream:
            pid = str(row["position_id"])
            p = float(row["mark"] if kind == 0 else row["ref_mid"])
            q = math.fsum(qs.values())
            if kind == 1:
                q_new_pid = qs.get(pid, 0.0) + row["direction"] * row["qty"]
                scale = max(abs(qs.get(pid, 0.0)), row["qty"])
                if abs(row["q_after"] - q_new_pid) > _Q_REL * scale + 4 * math.ulp(scale):
                    q_breaks.append(f"{pid}@{ts}")  # a summation-by-parts break: K10 fails
                qs_after = dict(qs)
                if row["q_after"] == 0.0:
                    qs_after.pop(pid, None)
                else:
                    qs_after[pid] = row["q_after"]
            else:
                if pid not in qs or abs(qs[pid] - row["qty"]) > 1e-12 * max(1.0, abs(qs[pid])):
                    raise ValueError(f"tick {pid}@{ts}: position {row['qty']} does not match the book "
                                     f"{qs.get(pid, 0.0)} (S-8.3 tick-before-fill order)")
                qs_after = qs
            q_new = math.fsum(qs_after.values())
            b = None
            if leg and (q != 0.0 or q_new != 0.0):
                b = _num(row["btc_ref"] if kind == 1 else btc_mark.get(ts))
                if b is None or b <= 0:
                    raise ValueError(f"{pid}@{ts}: BTC reference B(e) missing (S-8.3 mkt_c; no invented "
                                     "price)")
            if q != 0.0:
                if p_prev is None:
                    raise RuntimeError("internal: a held position without a previous event")
                d = q * (p - p_prev)
                price_day[days.close_day(ts) if kind == 0 else days.fill_day(ts)] += d
                (pr_legs if leg else pr_btc).append(d)
                if leg:
                    if ep is None or b_prev is None or b is None:
                        raise RuntimeError("internal: a held leg without an episode / BTC reference")
                    ep.pr += d
                    ep.mkt += q * p_prev * ep.beta * (b / b_prev - 1.0)
            if leg and kind == 1:
                if q == 0.0:
                    beta = _num(row["beta"])
                    if beta is None:
                        raise ValueError(f"{pid}@{ts}: episode without the beta-hat of its opening decision")
                    ep = _Ep(sym, inst, _sgn(q_new), ts, beta)
                    episodes.append(ep)
                elif q_new != 0.0 and _sgn(q_new) != _sgn(q):
                    raise ValueError(f"{pid}@{ts}: a leg fill reverses the position sign")
                if ep is None:
                    raise RuntimeError("internal: a leg fill without an open episode")
                if pid not in pid_ep:
                    ep.pids.append(pid)
                    pid_ep[pid] = ep
                elif pid_ep[pid] is not ep:
                    raise ValueError(f"{pid}: one position id in two episodes")
                ep.fees += row["fee"]
                ep.spread += row["spread_cost"]
                ep.slip += row["slip_cost"]
                ep.n_fills += 1
                if q_new == 0.0:
                    ep.end_ts = ts
                    ep = None
            qs = qs_after
            p_prev, b_prev = p, b
        if qs:
            raise ValueError(f"{sym}#{inst} ({role}) is not flat at the end of the run")
    _check_ticks(events, np.array(sorted(btc_mark), dtype=np.int64))
    return _Walk(episodes, pid_ep, math.fsum(pr_legs), math.fsum(pr_btc), price_day, fills, days, q_breaks)


def _check_ticks(events: list[tuple[int, int, int, dict]], grid: np.ndarray) -> None:
    """Fail closed when a held position has no ``positions`` row at a 15-minute tick (a ``marks`` row): its
    S-8.3 event list would be incomplete. A tick at a fill's timestamp precedes the fill."""
    by_pid: dict[str, list[tuple[int, int, dict]]] = {}
    for ts, kind, _, row in events:
        by_pid.setdefault(str(row["position_id"]), []).append((ts, kind, row))
    for pid, evs in by_pid.items():
        q, prev_ts = 0.0, None
        for ts, kind, row in evs:
            if q != 0.0 and prev_ts is not None:
                n = int(np.searchsorted(grid, ts, "left") - np.searchsorted(grid, prev_ts, "right"))
                if kind == 1 and ts > prev_ts and bool(np.any(grid == ts)):
                    n += 1  # the tick at the fill's ts would be this position's event just before the fill
                if n > 0:
                    raise ValueError(f"tick missing: {pid} is held but has no positions row at a tick in "
                                     f"({prev_ts}, {ts}] (S-8.3 event list incomplete)")
            if kind == 1:
                q = float(row["q_after"])
            prev_ts = ts


def _cluster(eps: list[Episode], gap_h: float) -> tuple[Cluster, ...]:
    gap = int(round(gap_h * H))
    out: list[Cluster] = []
    cur: list[Episode] = []
    for e in sorted(eps, key=lambda e: (e.asset, e.side, e.start_ts)):
        if cur and (cur[-1].asset, cur[-1].side) == (e.asset, e.side) and cur[-1].end_ts is not None \
                and e.start_ts - cur[-1].end_ts <= gap:
            cur.append(e)
            continue
        if cur:
            out.append(Cluster(cur[0].asset, cur[0].side, tuple(cur)))
        cur = [e]
    if cur:
        out.append(Cluster(cur[0].asset, cur[0].side, tuple(cur)))
    return tuple(out)


def episodes(run: BookRun) -> tuple[Episode, ...]:
    """The run's leg episodes (S-8.3), sorted by (asset, side, start); funding attached by position id."""
    return _account_parts(run)[0]


def clusters(run: BookRun, gap_h: float = GAP_H) -> tuple[Cluster, ...]:
    """S-8.3 clusters: same (asset, side), each episode starting ≤ ``gap_h`` h after the previous ended."""
    return _cluster(list(episodes(run)), gap_h)


def _account_parts(run: BookRun) -> tuple[tuple[Episode, ...], _Walk, dict]:
    w = _walk(run)
    funding = _records(run.funding_flows, _FUND_COLS, "funding_flows")
    fday = np.zeros(w.days.n)
    f_leg: list[float] = []
    f_hedge: list[float] = []
    for i, r in enumerate(funding):
        cash = _fin(r["cash"], f"funding {i} cash")
        fday[w.days.close_day(r["ts"])] += cash
        if r["role"] == "leg":
            ep = w.pid_ep.get(str(r["position_id"]))
            if ep is None:
                raise ValueError(f"funding of {r['position_id']} without a leg episode")
            ep.funding += cash
            f_leg.append(cash)
        elif r["role"] == "hedge":
            f_hedge.append(cash)
        else:
            raise ValueError(f"funding {i}: bad role {r['role']!r}")
    eps = tuple(sorted((e.freeze() for e in w.episodes), key=lambda e: (e.asset, e.side, e.start_ts)))
    return eps, w, dict(fday=fday, f_leg=f_leg, f_hedge=f_hedge)


def account(run: BookRun) -> Ledger:
    """The §8 objects of one valid executed book (refuses ``run.invalid``)."""
    eps, w, fx = _account_parts(run)
    cl = _cluster(list(eps), GAP_H)
    n = w.days.n
    fees_d, spread_d, slip_d = np.zeros(n), np.zeros(n), np.zeros(n)
    cost = {(r, c): [] for r in ("leg", "hedge") for c in ("fee", "spread_cost", "slip_cost")}
    notional: list[float] = []
    pr_fills: list[float] = []
    for f in w.fills:
        i = w.days.fill_day(f["ts"])
        fees_d[i] += f["fee"]
        spread_d[i] += f["spread_cost"]
        slip_d[i] += f["slip_cost"]
        for c in ("fee", "spread_cost", "slip_cost"):
            cost[(f["role"], c)].append(f[c])
        notional.append(f["qty"] * f["ref_mid"])
        pr_fills.append(-f["direction"] * f["qty"] * f["ref_mid"])
    eng = run.daily.reset_index(drop=True)
    daily = pd.DataFrame(dict(day=eng["day"].to_numpy(), start_ts=w.days.start, end_ts=w.days.end,
                              equity_start=eng["equity_start"].to_numpy(dtype=float), funding=fx["fday"],
                              fees=fees_d, spread=spread_d, slippage=slip_d,
                              spread_slip=spread_d + slip_d, price=w.price_day))
    daily["net"] = daily["funding"] - daily["fees"] - daily["spread_slip"] + daily["price"]
    s = {k: math.fsum(v) for k, v in cost.items()}
    eq0 = _fin(eng["equity_start"].iloc[0], "daily.equity_start")
    eq1 = _fin(eng["equity_end"].iloc[-1], "daily.equity_end")
    return Ledger(
        arm=run.arm, execution=run.execution, h_star=run.h_star, window=run.window, stress=None,
        fills=run.fills, episodes=eps, clusters=cl,
        F_leg=math.fsum(fx["f_leg"]), F_leg_credits=math.fsum(x for x in fx["f_leg"] if x > 0),
        F_leg_debits=math.fsum(x for x in fx["f_leg"] if x < 0), F_hedge=math.fsum(fx["f_hedge"]),
        fees_leg=s[("leg", "fee")], spread_leg=s[("leg", "spread_cost")], slip_leg=s[("leg", "slip_cost")],
        fees_hedge=s[("hedge", "fee")], spread_hedge=s[("hedge", "spread_cost")],
        slip_hedge=s[("hedge", "slip_cost")],
        PR=w.pr_legs + w.pr_btc, PR_legs=w.pr_legs, PR_BTC=w.pr_btc, PR_fills=math.fsum(pr_fills),
        MKT=math.fsum(c.mkt_c for c in cl) + w.pr_btc, fill_notional=math.fsum(notional),
        equity_change=eq1 - eq0, daily=daily, daily_engine=eng, q_breaks=tuple(w.q_breaks),
    )


# --- checks and derived objects ----------------------------------------------------------------------------
def _tol(led: Ledger) -> float:
    return K10_REL * led.fill_notional


def k10_ok(led: Ledger) -> bool:
    """K10: |Σ ρ_c + MKT − PR| ≤ 1e-9 Σ|fill notional|, and PR equals the engine's daily price column (in
    total and per day, same tolerance; AMB-21 (c)). The S-8.3 summation by parts must also hold: no fill with
    q_after != q_before + dq beyond the engine's close clamp, and |−Σ dq·p(f) − PR| within the tolerance."""
    tol = _tol(led)
    eng = led.daily_engine["price"].to_numpy(dtype=float)
    if not np.all(np.isfinite(eng)) or len(eng) != len(led.daily):
        return False
    return bool(not led.q_breaks
                and abs(led.rho_sum + led.MKT - led.PR) <= tol
                and abs(led.PR_fills - led.PR) <= tol
                and abs(led.PR - math.fsum(eng)) <= tol
                and np.all(np.abs(led.daily["price"].to_numpy() - eng) <= tol))


def daily_ok(led: Ledger) -> bool:
    """S-8.1: the engine's daily components equal the recomputed ones and sum to the daily equity change;
    NET equals the window's equity change (tolerance 1e-9 Σ|fill notional|, AMB-21 (c))."""
    tol = _tol(led)
    e, r = led.daily_engine, led.daily
    if len(e) != len(r):
        return False
    ok = True
    for c in ("funding", "fees", "spread_slip", "price"):
        ok &= bool(np.all(np.abs(e[c].to_numpy(dtype=float) - r[c].to_numpy()) <= tol))
    ident = e["funding"] - e["fees"] - e["spread_slip"] + e["price"]
    ok &= bool(np.all(np.abs((e["equity_end"] - e["equity_start"]).to_numpy(dtype=float) - ident) <= tol))
    ok &= bool(np.all(np.abs(e["net"].to_numpy(dtype=float) - ident) <= tol))
    ok &= abs(led.NET - led.equity_change) <= tol
    return bool(ok)


def per_asset_net(led: Ledger) -> dict[str, float]:
    """S-8.4 N_a per asset = per SYMBOL (instances of one symbol pooled; AMB-21 (a), conservative for P5);
    NET = Σ_a N_a + ``led.H``."""
    out: dict[str, list[float]] = {}
    for c in led.clusters:
        out.setdefault(c.symbol, []).append(c.net)
    return {a: math.fsum(v) for a, v in sorted(out.items())}


def per_asset_rho(led: Ledger) -> dict[str, float]:
    """§12 per-asset ρ: Σ ρ_c per symbol (as ``per_asset_net``)."""
    out: dict[str, list[float]] = {}
    for c in led.clusters:
        out.setdefault(c.symbol, []).append(c.rho_c)
    return {a: math.fsum(v) for a, v in sorted(out.items())}


def m_rule(n_clusters: int) -> int:
    """P11: m = max(3, ⌈0.05 · n_clusters⌉) (integer arithmetic: ⌈n · 1 / 20⌉ from FROZEN.accept.p11_m)."""
    return max(M_MIN, -(-int(n_clusters) * M_FRAC.numerator // M_FRAC.denominator))


def top_rho(led: Ledger, m: int) -> tuple[Cluster, ...]:
    """The m clusters with the largest positive ρ_c (ties: asset, side, start)."""
    pos = [c for c in led.clusters if c.rho_c > 0]
    pos.sort(key=lambda c: (-c.rho_c, c.asset, c.side, c.episodes[0].start_ts))
    return tuple(pos[:m])


def net_trim(led: Ledger, m: int | None = None) -> float:
    """P11 R2′ (and the §12 report for explicit m): NET − Σ_{Top_m} max(ρ_c, 0) − max(MKT, 0)."""
    mm = m_rule(led.n_clusters) if m is None else int(m)
    if mm < 0:
        raise ValueError("m must be >= 0")
    return led.NET - math.fsum(max(c.rho_c, 0.0) for c in top_rho(led, mm)) - max(led.MKT, 0.0)


def p11_ok(led: Ledger) -> bool:
    """P11: R1 (CAC > 0) and R2′ (NET_trim > 0 with the m rule)."""
    return bool(led.CAC > 0 and net_trim(led) > 0)


def mean_daily_net(led: Ledger) -> float:
    """Mean of the daily table's net column (book units; L-norm E0 = 1; AMB-21 (d))."""
    return float(led.daily["net"].mean())


def mean_daily_return(led: Ledger) -> float:
    """Mean of r_t = net_t / E_{t-1} (the engine's day-start equity; stressed ledgers use the shifted path).
    Reported alongside ``mean_daily_net`` (AMB-21 (d))."""
    e0 = led.daily["equity_start"].to_numpy(dtype=float)
    if not np.all(np.isfinite(e0)) or np.any(e0 <= 0):
        raise ValueError("day-start equity must be finite and > 0")
    return float(np.mean(led.daily["net"].to_numpy() / e0))


def period_net(led: Ledger, edges: list[int]) -> list[float]:
    """Σ daily net over [edges[k], edges[k+1]) by each day's start (P5 quarters; edges from the caller)."""
    e = [int(x) for x in edges]
    if any(b <= a for a, b in zip(e, e[1:], strict=False)):
        raise ValueError("edges must be increasing")
    st = led.daily["start_ts"].to_numpy()
    net = led.daily["net"].to_numpy()
    return [math.fsum(net[(st >= a) & (st < b)]) for a, b in zip(e, e[1:], strict=False)]


def stressed(run: BookRun, fee: float = 1.5, slip: float = 1.5, spread: float = 3.0) -> Ledger:
    """P6 (S-13.2, DEC-38): the SAME executed book (decisions, quantities, fill times and reference prices
    unchanged) with every fill's fee × ``fee``, slippage × ``slip`` and spread charge × ``spread`` (the
    fallback and stressed-exit spreads are part of ``spread_cost``). Funding and price are unchanged.

    The equity path that drives the risk layer (daily-loss pause, drawdown kill) and the sizing is NOT
    re-simulated: only the daily table's cost columns, net and equity are shifted by the extra costs."""
    _refuse_invalid(run)
    for k, v in (("fee", fee), ("slip", slip), ("spread", spread)):
        if not (math.isfinite(v) and v >= 0):
            raise ValueError(f"stress multiplier {k} must be finite and >= 0")
    days = _Days(run.daily)
    f = run.fills.copy()
    d = run.daily.reset_index(drop=True).copy()
    if len(f):
        d_fee = (fee - 1.0) * f["fee"].astype(float)
        d_ss = (spread - 1.0) * f["spread_cost"].astype(float) + (slip - 1.0) * f["slip_cost"].astype(float)
        idx = [days.fill_day(int(t)) for t in ns_array(f["ts"].to_numpy(), "fills.ts")]
        add_fee = np.bincount(idx, weights=d_fee.to_numpy(), minlength=days.n)
        add_ss = np.bincount(idx, weights=d_ss.to_numpy(), minlength=days.n)
        f["fee"] = f["fee"].astype(float) * fee
        f["spread_cost"] = f["spread_cost"].astype(float) * spread
        f["slip_cost"] = f["slip_cost"].astype(float) * slip
        tot = add_fee + add_ss
        cum = np.cumsum(tot)
        d["fees"] = d["fees"].astype(float) + add_fee
        d["spread_slip"] = d["spread_slip"].astype(float) + add_ss
        d["net"] = d["net"].astype(float) - tot
        d["equity_end"] = d["equity_end"].astype(float) - cum
        d["equity_start"] = d["equity_start"].astype(float) - (cum - tot)
    return replace(account(replace(run, fills=f, daily=d)), stress=(float(fee), float(slip), float(spread)))


__all__ = ["Cluster", "Episode", "Ledger", "account", "clusters", "daily_ok", "episodes", "k10_ok",
           "m_rule", "mean_daily_net", "mean_daily_return", "net_trim", "p11_ok", "per_asset_net",
           "per_asset_rho", "period_net", "stressed", "top_rho"]
