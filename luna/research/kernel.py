"""Numba trade-list kernel: the backtest engine's per-position life cycle with portfolio limits off.

One position at a time on one (strategy, params, key) signal stream, on the engine's union bar index
(rows = bars, columns = the key's legs). Per bar ``i`` the order is the engine's
(``luna.engine.backtest``):

1. fills of the orders decided at bar ``i - 1``: the entry (every leg that fills opens; a pair with
   only some legs filled is a ``leg_failure`` exited from this bar on) or the exit orders of the
   legs still held (the trade closes on the bar its last leg fills);
2. (no emergency stops: research trade lists model the strategy, not the portfolio guard);
3. perp funding at the hour boundary that closes bar ``i`` on every leg still held, at the bar's
   last close; an unknown rate counts one ``funding_unknown_bar`` per held leg and adds nothing;
4. exits decided at the close (``bars_held`` counts every union bar since the entry fill, the fill
   bar included): stop first, then the strategy's exit flag ("signal"), then the time stop. Once
   exiting, every leg still held gets a fresh exit order each bar until it fills;
5. a new entry at the close of bar ``i`` when no position is open (``entry[i]`` already holds only
   valid candidates: see ``luna.research.tradelists``).

Fills (``luna.execution.sim.SimBroker``): perp IOC at the next bar's open -/+ (half spread +
slippage), cancelled if that is beyond the decision mid +/- ``band``; spot post-only at
``mid -/+ half_spread`` only if the next bar trades strictly through it. Forward-filled or missing
bars (``real`` False) never fill. Mids are the decision bar's last close.

Units: per-leg quantities are those of 1 USD of gross notional at the signal bar's close (the mids
the engine sizes with): ``qty_l = direction * w_l / (sum|w| * close_l)``. Per trade the kernel
returns, in those units, ``gross`` = frictionless P&L between the fill bars' opens, ``cost`` =
``gross`` minus the P&L at fill prices plus fees (spread, slippage and fees of both legs and both
sides), ``funding`` (known rates only). Net = gross - cost + funding, identical to the engine's
``pnl_net`` divided by the position's gross notional at the signal mids.
"""

from __future__ import annotations

import numpy as np
from numba import njit

STOP_NONE = 0
STOP_PRICE = 1  # close at/through a fixed price level set at entry (naive momentum)
STOP_TRAIL = 2  # ATR trailing level ratcheted at each close (momentum); "stop" until it moves
STOP_Z_ABS = 3  # |z| > stop_z (naive mean reversion / pairs)
STOP_Z_DIR = 4  # z at/through z_entry - direction * stop_z (mean reversion / pairs)

R_SIGNAL, R_STOP, R_TRAILING, R_TIME, R_LEG_FAILURE = 0, 1, 2, 3, 4
REASONS = ("signal", "stop", "trailing", "time", "leg_failure")


@njit(cache=True)
def _fill(i, leg, s, mid, open_, high, low, real, post_only, band, fric, half_spread):
    """(filled, price) of a one-bar order on leg ``leg`` at bar ``i``; ``s`` = +1 buy / -1 sell."""
    if not real[i, leg]:
        return False, 0.0
    if post_only:
        limit = mid * (1.0 - s * half_spread[leg])
        ok = low[i, leg] < limit if s > 0 else high[i, leg] > limit
        return ok, limit
    if s > 0:
        price = open_[i, leg] * (1.0 + fric[leg])
    else:
        price = open_[i, leg] * (1.0 - fric[leg])
    limit = mid * (1.0 + s * band)
    if (s > 0 and price > limit) or (s < 0 and price < limit):
        return False, 0.0
    return True, price


@njit(cache=True)
def simulate(
    open_, high, low, last, real, entry, weight, row_ok, exit_long, exit_short, ref, stop_dist, z,
    atr, stop_mode, stop_z, trail_mult, max_hold, post_only, band, fric, fee, half_spread,
    boundary, rate,
):
    """Trades of one signal stream (module docstring).

    Shapes: ``open_, high, low, last, weight, rate`` (n, L) float64, ``real`` (n, L) bool;
    ``entry`` (n,) int8 (valid entries only), ``row_ok, exit_long, exit_short, boundary`` (n,) bool,
    ``ref, stop_dist, z, atr`` (n,) float64; ``fric, half_spread`` (L,) float64. ``stop_z`` is the
    |z| level (STOP_Z_ABS) or the z distance (STOP_Z_DIR); ``trail_mult`` NaN = no trailing.

    Returns (signal_idx, entry_idx, exit_idx, direction, reason_code, gross, cost, funding,
    funding_unknown_bars), one element per closed trade (positions still open at the end are
    dropped).
    """
    n, nl = open_.shape
    cap = n // 2 + 1
    o_sig = np.empty(cap, np.int64)
    o_ent = np.empty(cap, np.int64)
    o_ext = np.empty(cap, np.int64)
    o_dir = np.empty(cap, np.int8)
    o_rsn = np.empty(cap, np.int8)
    o_gross = np.empty(cap, np.float64)
    o_cost = np.empty(cap, np.float64)
    o_fund = np.empty(cap, np.float64)
    o_unk = np.empty(cap, np.int64)
    m = 0

    qty = np.zeros(nl)
    held = np.zeros(nl, np.bool_)
    pend_x = np.zeros(nl, np.bool_)
    x_mid = np.zeros(nl)
    pe = np.zeros(nl)  # entry fill price
    oe = np.zeros(nl)  # entry bar open
    pos_open = False
    pend_entry = False
    exiting = False
    sig = -1
    d = 0
    e = -1
    bars = 0
    reason = -1
    stop_level = np.nan
    trail = np.nan
    gross = 0.0
    realized = 0.0
    fees = 0.0
    fund = 0.0
    unknown = 0

    for i in range(n):
        # (1) fills of the orders decided at i - 1
        if pend_entry:
            pend_entry = False
            n_leg = 0
            n_fill = 0
            for leg in range(nl):
                if qty[leg] == 0.0:
                    continue
                n_leg += 1
                s = 1.0 if qty[leg] > 0 else -1.0
                ok, px = _fill(i, leg, s, last[sig, leg], open_, high, low, real, post_only, band,
                               fric, half_spread)
                if ok:
                    held[leg] = True
                    pe[leg] = px
                    oe[leg] = open_[i, leg]
                    fees += fee * abs(qty[leg]) * px
                    n_fill += 1
                else:
                    qty[leg] = 0.0
            if n_fill > 0:
                pos_open = True
                e = i
                bars = 0
                trail = stop_level
                exiting = n_fill < n_leg
                reason = R_LEG_FAILURE if exiting else -1
        elif pos_open:
            touched = False
            for leg in range(nl):
                if not pend_x[leg]:
                    continue
                pend_x[leg] = False
                s = -1.0 if qty[leg] > 0 else 1.0
                ok, px = _fill(i, leg, s, x_mid[leg], open_, high, low, real, post_only, band,
                               fric, half_spread)
                if ok:
                    realized += qty[leg] * (px - pe[leg])
                    gross += qty[leg] * (open_[i, leg] - oe[leg])
                    fees += fee * abs(qty[leg]) * px
                    held[leg] = False
                    touched = True
            if touched and not held.any():
                o_sig[m] = sig
                o_ent[m] = e
                o_ext[m] = i
                o_dir[m] = d
                o_rsn[m] = reason
                o_gross[m] = gross
                o_cost[m] = gross - realized + fees
                o_fund[m] = fund
                o_unk[m] = unknown
                m += 1
                pos_open = False
                exiting = False

        # (3) funding at the hour boundary closing bar i
        if pos_open and boundary[i]:
            for leg in range(nl):
                if held[leg]:
                    r = rate[i, leg]
                    if np.isfinite(r):
                        fund += -r * qty[leg] * last[i, leg]
                    else:
                        unknown += 1

        # (4) exits at the close of bar i
        if pos_open:
            bars += 1
            if not exiting and row_ok[i]:
                r_code = -1
                px = last[i, 0]
                if stop_mode == STOP_PRICE:
                    if d * (px - stop_level) <= 0:
                        r_code = R_STOP
                elif stop_mode == STOP_TRAIL:
                    level = trail
                    a = atr[i]
                    if not np.isnan(trail_mult) and np.isfinite(a):
                        cand = px - d * trail_mult * a
                        if d > 0:
                            if cand > level:
                                level = cand
                        elif cand < level:
                            level = cand
                    trail = level
                    if d * (px - level) <= 0:
                        r_code = R_STOP if level == stop_level else R_TRAILING
                elif stop_mode == STOP_Z_ABS:
                    zz = z[i]
                    if np.isfinite(zz) and abs(zz) > stop_z:
                        r_code = R_STOP
                elif stop_mode == STOP_Z_DIR:
                    zz = z[i]
                    if np.isfinite(zz) and d * (zz - stop_level) <= 0:
                        r_code = R_STOP
                if r_code < 0 and (exit_long[i] if d > 0 else exit_short[i]):
                    r_code = R_SIGNAL
                if r_code < 0 and bars >= max_hold:
                    r_code = R_TIME
                if r_code >= 0:
                    exiting = True
                    reason = r_code
            if exiting:
                for leg in range(nl):
                    if held[leg]:
                        pend_x[leg] = True
                        x_mid[leg] = last[i, leg]

        # (5) entry at the close of bar i
        if entry[i] != 0 and not pos_open:
            d = entry[i]
            sig = i
            tot = 0.0
            for leg in range(nl):
                tot += abs(weight[i, leg])
            for leg in range(nl):
                qty[leg] = d * weight[i, leg] / (tot * last[i, leg])
                held[leg] = False
                pend_x[leg] = False
            if stop_mode == STOP_PRICE or stop_mode == STOP_TRAIL:
                stop_level = ref[i] * (1.0 - d * stop_dist[i])
            elif stop_mode == STOP_Z_DIR:
                stop_level = z[i] - d * stop_z
            else:
                stop_level = np.nan
            gross = 0.0
            realized = 0.0
            fees = 0.0
            fund = 0.0
            unknown = 0
            pend_entry = True

    return (o_sig[:m], o_ent[:m], o_ext[:m], o_dir[:m], o_rsn[:m], o_gross[:m], o_cost[:m],
            o_fund[:m], o_unk[:m])
