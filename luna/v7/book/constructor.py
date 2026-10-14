"""Deterministic book constructor (spec S-7.1, S-0.6, S-4.4 liquidity cap, §14 L-norm).

One constructor for every arm; only the scores change. At decision t, in this order:

1. **Forced exits** (``RiskActions.forced``: kill > leg stop > U6 > ineligibility) close their legs whatever
   the score or the minimum hold. Legs with a ``pending_exit`` get no new order.
2. **Decision exits**, only after the minimum hold h* (measured from the entry decision, AMB-16 (g)): a gate
   failure (any of U1-U7; U8 is never an exit trigger), a rank outside K + 2 on its side (or not ranked), or a
   score below the hurdle (``passes_hurdle`` False). Before h* only risk exits close a leg.
3. **Entries** (only if the risk layer allows entries at t): per side, the candidates ranked in the top K of
   that side by the arm's score (re-derived: rank_key desc, NaN last, ties by symbol) that pass the hurdle,
   pass U1-U8 (``universe.eligible``; no gate row fails closed), are not in cooldown / U8-blocked, are not
   held on either side (the other side: no same-decision flip), are not halted, and have a finite beta and 24
   h quote volume. A slot lost to any of these is NOT backfilled by a lower rank (A1 = A0's entries minus the
   vetoed, literal, AMB-16 (a)). A symbol qualifying on both sides enters neither (AMB-16 (b)). Entrants fill
   the free slots (K minus the legs still held on the side after this decision's exits) in rank order.
4. **Targets**: equal weight within a side, ``min(0.5 E / n_side, 0.5 E / K)`` (the per-leg cap binds whenever
   n_side < K), each capped by the liquidity cap ``0.005 * qv24 / usd_per_unit`` (L-norm: usd_per_unit =
   E_ref = 10,000 USD per unit of E, §14). Neutrality: if ``|L - S| > 0.05 (L + S)``, every target of the
   heavier side is scaled by S/L (or L/S) so the target book is dollar-neutral; an empty side therefore makes
   the other side's targets 0 (flat), including legs inside the minimum hold (conservative, AMB-16 (d)). Caps
   and neutrality bound the targets (AMB-16 (e)); the actual book follows them through the drift rule.
5. **Resizes**: a kept leg is resized to its target only when ``|n - T| > 25 % T`` and only downward
   (reduce-only between entries, AMB-16 (f)); a target of 0 closes the leg (reason ``neutrality``). Halted:
   no resize.

Halted instruments (``halted`` evaluated by the engine at the order time): no open and no reduce is emitted;
an exit is emitted with ``halted=True`` so the engine defers it (S-5.4). ``Targets.post`` holds the signed leg
notionals after this decision's orders (pending / halted legs at their current notional), the input of
``hedge.decide``. ML enters only through ``scores`` (order + hurdle flag): it cannot add a gate-failing
candidate, change a size or cancel a forced exit (S-0.6).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.v7.book import EPS, SIDES, Leg, Order, check_side, positive
from luna.v7.book.risk import RiskActions
from luna.v7.constants import FROZEN
from luna.v7.timeutil import H
from luna.v7.universe import GateRow, eligible

BK = FROZEN.book
LIQ = FROZEN.costs.liquidity_cap


@dataclass(frozen=True)
class Targets:
    """The constructor's output at decision t.

    ``orders``: forced exits (risk precedence), decision exits, neutrality closes, reduces, opens; each group
    sorted by (symbol, side). ``post``: symbol -> signed notional after the orders (legs that will be held).
    ``notes``: ``"<reason>:<symbol>"`` for every skipped entry / resize (deterministic order).
    """

    t: int
    orders: tuple[Order, ...]
    post: Mapping[str, float]
    notes: tuple[str, ...]


def _finite_pos(x: object) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _finite(x: object) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ranked(scores: pd.DataFrame) -> dict[int, list[tuple[str, bool]]]:
    """Per side: [(symbol, passes_hurdle)] best first (rank_key desc, NaN last, ties by symbol)."""
    for col in ("symbol", "side", "rank_key", "passes_hurdle"):
        if col not in scores:
            raise ValueError(f"scores lacks column {col!r}")
    if scores.duplicated(["symbol", "side"]).any():
        raise ValueError("duplicate (symbol, side) scores")
    out: dict[int, list[tuple[str, bool]]] = {s: [] for s in SIDES}
    if len(scores) == 0:
        return out
    key = pd.to_numeric(scores["rank_key"], errors="coerce").to_numpy(np.float64)
    df = pd.DataFrame(
        {
            "symbol": scores["symbol"].astype(str).to_numpy(),
            "side": [check_side(int(x)) for x in scores["side"]],
            "k": np.where(np.isfinite(key), key, -np.inf),
            "p": [isinstance(x, (bool, np.bool_)) and bool(x) for x in scores["passes_hurdle"]],
        }
    )
    df = df.sort_values(["k", "symbol"], ascending=[False, True], kind="mergesort")
    for sym, side, p in zip(df["symbol"], df["side"], df["p"], strict=True):
        out[int(side)].append((str(sym), bool(p)))
    return out


def _gate_failed(g: GateRow | None) -> bool:
    """Decision-exit gate failure: any of U1-U7 (U8 is never an exit trigger, S-2.3 / S-7.3)."""
    if g is None:
        return True
    return not all((g.u1, g.u2, g.u3, g.u4, g.u5, g.u6, g.u7))


def targets(
    *,
    t: int,
    equity: float,
    held: tuple[Leg, ...],
    scores: pd.DataFrame,
    gates: Mapping[str, GateRow | None],
    risk: RiskActions,
    halted: Mapping[str, bool],
    betas: Mapping[str, float | None],
    quote_vol_24h: Mapping[str, float | None],
    h_star_h: float,
    usd_per_unit: float,
) -> Targets:
    """Leg orders at decision t (S-7.1). See the module docstring for the exact rule order.

    ``scores``: ``arms.arm_scores`` output (symbol, side, rank_key, passes_hurdle) over the gate-passing
    candidates of both sides; ``gates``: GateRow per held / candidate symbol; ``risk``:
    ``RiskLayer.pre_decision`` actions for the same t; ``halted``: S-5.4 halt flag per symbol at the order
    time (missing = halted, fail closed); ``betas``: beta_hat(t) per symbol; ``quote_vol_24h``: trailing 24 h
    quote volume (USD) known at t - 1 min; ``equity``: E in book units (L-norm: the marked equity, starting
    at 1); ``usd_per_unit``: USD per book unit for the liquidity cap (L-norm: ``FROZEN.deploy.E_ref``; USD
    layers: 1).
    """
    t = int(t)
    e = positive(equity, "equity")
    upu = positive(usd_per_unit, "usd_per_unit")
    h_ns = int(round(positive(h_star_h, "h_star_h") * H))
    if risk.t != t:
        raise ValueError("risk actions belong to another decision time")
    held = tuple(sorted(held, key=lambda x: x.symbol))
    if len({x.symbol for x in held}) != len(held):
        raise ValueError("a symbol is held more than once")
    ranked = _ranked(scores)
    rank = {s: {sym: (i + 1, p) for i, (sym, p) in enumerate(ranked[s])} for s in SIDES}
    notes: list[str] = []
    is_halted = {x.symbol: bool(halted.get(x.symbol, True)) for x in held}

    def order(lg: Leg, kind: str, notional: float, target: float, reason: str, forced: bool) -> Order:
        return Order(lg.symbol, lg.side, kind, notional, target, reason, forced,  # type: ignore[arg-type]
                     kind == "close" and is_halted[lg.symbol], lg.position_id)

    # 1-2. exits ----------------------------------------------------------------------------------------
    forced_by = {f.symbol: f for f in risk.forced}
    forced_orders: list[Order] = []
    exit_orders: list[Order] = []
    kept: list[Leg] = []
    for lg in held:
        if lg.pending_exit is not None:
            continue
        f = forced_by.get(lg.symbol)
        if f is not None:
            if f.side != lg.side:
                raise ValueError(f"forced exit side mismatch for {lg.symbol}")
            forced_orders.append(order(lg, "close", lg.notional, 0.0, f.reason, True))
            continue
        if t - lg.entry_decision_ts >= h_ns:
            r = rank[lg.side].get(lg.symbol)
            why = None
            if _gate_failed(gates.get(lg.symbol)):
                why = "gate"
            elif r is None or r[0] > BK.K + BK.exit_rank_slack:
                why = "rank"
            elif not r[1]:
                why = "hurdle"
            if why is not None:
                exit_orders.append(order(lg, "close", lg.notional, 0.0, why, False))
                continue
        kept.append(lg)
    closing = {o.symbol for o in forced_orders + exit_orders}
    # legs still physically held after t + 15 min: kept, pending, and halted exits (they occupy their slot)
    occupied = {s: 0 for s in SIDES}
    for lg in held:
        if lg.symbol not in closing or is_halted[lg.symbol]:
            occupied[lg.side] += 1

    # 3. entries ----------------------------------------------------------------------------------------
    held_syms = {x.symbol for x in held}
    qualified: dict[int, list[str]] = {s: [] for s in SIDES}
    if not risk.entries_open:
        notes.append(f"entries_blocked:{risk.entry_block}")
    else:
        for s in SIDES:
            for sym, p in ranked[s][: BK.K]:
                held_side = next((x.side for x in held if x.symbol == sym), None)
                why = None
                if held_side == s:
                    continue
                if held_side is not None:
                    why = "flip"
                elif not p:
                    why = "hurdle"
                elif gates.get(sym) is None or not eligible(gates[sym]):  # type: ignore[arg-type]
                    why = "gate"
                elif not risk.allows_entry(sym):
                    why = risk.blocked.get(sym, "risk")
                elif bool(halted.get(sym, True)):
                    why = "halted"
                elif _finite(betas.get(sym)) is None:
                    why = "beta_missing"
                elif _finite_pos(quote_vol_24h.get(sym)) is None:
                    why = "liquidity_missing"
                if why is None:
                    qualified[s].append(sym)
                else:
                    notes.append(f"{why}:{sym}")
        both = set(qualified[1]) & set(qualified[-1])
        for sym in sorted(both):
            notes.append(f"both_sides:{sym}")
        entrants: dict[int, list[str]] = {}
        for s in SIDES:
            q = [x for x in qualified[s] if x not in both]
            free = max(BK.K - occupied[s], 0)
            entrants[s] = q[:free]
            notes.extend(f"no_slot:{x}" for x in q[free:])
        qualified = entrants
    assert not (held_syms & (set(qualified[1]) | set(qualified[-1])))

    # 4. targets ----------------------------------------------------------------------------------------
    tgt: dict[str, float] = {}
    side_of: dict[str, int] = {}
    for s in SIDES:
        members = [x.symbol for x in kept if x.side == s] + qualified[s]
        if not members:
            continue
        w = min(BK.side_gross * e / len(members), BK.side_gross * e / BK.K)
        for sym in members:
            qv = _finite_pos(quote_vol_24h.get(sym))
            cap = LIQ * qv / upu if qv is not None else math.inf
            if qv is None:
                notes.append(f"liquidity_missing_held:{sym}")
            tgt[sym] = min(w, cap)
            side_of[sym] = s
    gross = {s: sum(v for k, v in tgt.items() if side_of[k] == s) for s in SIDES}
    lg_, sh_ = gross[1], gross[-1]
    if abs(lg_ - sh_) > BK.neutral_tol * (lg_ + sh_) * (1 + EPS) + EPS * e:
        heavy, light = (1, -1) if lg_ > sh_ else (-1, 1)
        f = gross[light] / gross[heavy]
        for k in tgt:
            if side_of[k] == heavy:
                tgt[k] *= f
    per_leg = BK.side_gross * e / BK.K
    assert all(v <= per_leg * (1 + EPS) for v in tgt.values())
    assert sum(tgt.values()) <= FROZEN.risk.gross_max * e * (1 + EPS)

    # 5. resizes / neutrality closes / opens --------------------------------------------------------------
    neutral_close: list[Order] = []
    reduces: list[Order] = []
    post: dict[str, float] = {}
    for lg in kept:
        T = tgt[lg.symbol]
        n = lg.notional
        if T <= EPS * e:
            neutral_close.append(order(lg, "close", n, 0.0, "neutrality", False))
            if is_halted[lg.symbol]:
                post[lg.symbol] = lg.signed
            continue
        post[lg.symbol] = lg.signed
        if abs(n - T) <= BK.resize_drift * T * (1 + EPS):
            continue
        if n < T:
            notes.append(f"no_increase:{lg.symbol}")
        elif is_halted[lg.symbol]:
            notes.append(f"halted_resize:{lg.symbol}")
        else:
            reduces.append(order(lg, "reduce", n - T, T, "drift", False))
            post[lg.symbol] = lg.side * T
    opens: list[Order] = []
    for s in SIDES:
        for sym in qualified[s]:
            T = tgt[sym]
            if T <= EPS * e:
                notes.append(f"neutrality:{sym}")
                continue
            opens.append(Order(sym, s, "open", T, T, "entry", False, False, None))
            post[sym] = s * T
    for lg in held:  # pending exits and halted closes stay held
        if lg.pending_exit is not None or (lg.symbol in closing and is_halted[lg.symbol]):
            post[lg.symbol] = lg.signed

    def srt(xs: list[Order]) -> list[Order]:
        return sorted(xs, key=lambda o: (o.symbol, o.side))

    forced_sorted = sorted(forced_orders, key=lambda o: (o.symbol, o.side))
    orders = tuple(forced_sorted + srt(exit_orders) + srt(neutral_close) + srt(reduces) + srt(opens))
    return Targets(t, orders, dict(sorted(post.items())), tuple(notes))
