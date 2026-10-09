"""Kraken PF_ perpetual funding semantics (V4 spec §2, verified in docs/results/v4-funding-semantics.md).

- One funding period = 1 hour. The API `timestamp` is the START of the period the rate applies to
  (the rate itself is computed from the premium of the preceding hour).
- Positive rate: longs pay shorts. Negative rate: shorts pay longs.
- `funding_abs` (API `fundingRate`): USD received by a 1-unit short held for the whole period.
- `funding_rel` (API `relativeFundingRate`): funding_abs / reference price, decimal per hour.
- Funding accrues continuously; it settles at the end of the period or when the net position
  changes, pro rata to the time held. Missing periods pay nothing (known_only) and are counted.
"""

from __future__ import annotations

import pandas as pd

PERIOD = pd.Timedelta("1h")
HOURS_PER_YEAR = 8760


def parse_funding(payload: dict) -> pd.DataFrame:
    """API `historicalfundingrates` payload -> frame indexed by UTC period start (sorted, unique)."""
    rates = payload.get("rates") or []
    idx = pd.DatetimeIndex(pd.to_datetime([r["timestamp"] for r in rates], utc=True))
    df = pd.DataFrame(
        {
            "funding_abs": [float(r["fundingRate"]) for r in rates],
            "funding_rel": [float(r["relativeFundingRate"]) for r in rates],
        },
        index=idx,
    )
    return df[~df.index.duplicated(keep="last")].sort_index()


def funding_cashflow(qty: float, funding_abs: float, held_fraction: float = 1.0) -> float:
    """USD cash flow to a position of `qty` units (long > 0) for `held_fraction` of one period."""
    if not 0.0 <= held_fraction <= 1.0:
        raise ValueError(f"held_fraction must be in [0, 1], got {held_fraction}")
    return -qty * funding_abs * held_fraction


def known_rates(rates: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    """Rates usable by a decision at time t: only periods that have ended (start + 1h <= t)."""
    return rates[rates.index + PERIOD <= t]


def annualize(rel_rate_per_hour: float) -> float:
    return rel_rate_per_hour * HOURS_PER_YEAR


def accrue_funding(
    path: list[tuple[pd.Timestamp, float]],
    rates: pd.DataFrame,
    until: pd.Timestamp,
) -> pd.DataFrame:
    """Settle funding for a piecewise-constant position path up to `until`.

    `path` is [(ts, qty_from_ts), ...] in time order. Each row of the result is one settlement:
    at a period end or at a position change, whichever comes first. attrs carry `held_hours`
    (hours with a non-zero position) and `unknown_hours` (of those, hours with no rate).
    """
    # integer-nanosecond arithmetic (same semantics as Timestamp floor / min, much faster)
    p_ns = PERIOD.value
    starts, settles, qtys, fracs, cfs = [], [], [], [], []
    held = unknown = 0.0
    abs_by_start = {}
    if len(rates):
        keys = rates.index.as_unit("ns").asi8.tolist()
        abs_by_start = dict(zip(keys, rates["funding_abs"].astype(float).tolist(), strict=True))
    bounds = [pd.Timestamp(ts).value for ts, _ in path] + [pd.Timestamp(until).value]
    for (_, qty), start, end in zip(path, bounds[:-1], bounds[1:], strict=True):
        if qty == 0 or end <= start:
            continue
        t = start
        while t < end:
            period_start = t - t % p_ns
            seg_end = min(end, period_start + p_ns)
            frac = (seg_end - t) / p_ns
            held += frac
            rate = abs_by_start.get(period_start)
            if rate is not None:
                starts.append(period_start)
                settles.append(seg_end)
                qtys.append(qty)
                fracs.append(frac)
                cfs.append(funding_cashflow(qty, rate, frac))
            else:
                unknown += frac
            t = seg_end
    out = pd.DataFrame({
        "period_start": pd.to_datetime(starts, unit="ns", utc=True),
        "settle_ts": pd.to_datetime(settles, unit="ns", utc=True),
        "qty": pd.Series(qtys, dtype="float64"),
        "held_fraction": pd.Series(fracs, dtype="float64"),
        "cashflow": pd.Series(cfs, dtype="float64"),
    })
    out.attrs["held_hours"] = held
    out.attrs["unknown_hours"] = unknown
    return out
