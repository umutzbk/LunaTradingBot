"""Equity layers L-265 / L-10k and the 265 USD deployment gate (spec §14, S-14.1, DEC-7, DEC-21, DEC-34, EC-6;
AMB-23).

* **Filters (S-14.1).** ``Filters(step, min_qty, min_notional, provenance, known_at)``; provenance is one of
  ``pit_historical`` (step 1), ``current_approximation`` (step 2: today's ``exchangeInfo`` applied to a
  historical window as a labelled approximation) or ``forward_pit`` (step 4: the forward recorder's
  timestamped snapshots). Point-in-time filters (``pit_historical`` / ``forward_pit``) need ``known_at`` and
  apply at decision t only when ``known_at <= t - 60 s`` (the latest such snapshot); a current approximation
  is applied to the whole window by definition (its capture time is provenance only). One symbol never mixes
  current and point-in-time filters.
* **EC-6 separation.** Current filters never enter R1, a gate, a feature, a label or an L-norm book: the
  L-norm engine path (``book.engine.run``) accepts no filters at all, and ``assert_not_backdated`` refuses any
  ``current_approximation`` filter handed to a point-in-time metadata path.
* **L-265 / L-10k (§14).** ``run_layer`` runs the same frozen strategy at E = 265 / 10,000 USD under E_T with
  exchange step rounding (towards zero) and per-symbol minimum quantity / notional on every entry, reduce and
  hedge order, K = 5 and the S-7.4 hedge incl. BTC's filters. Infeasible legs or hedges are not traded (fail
  closed); an infeasible required hedge forbids the decision's entries (``book.engine`` USD-layer path). A
  symbol without applicable filters is excluded (never traded) and the result is flagged incomplete (DEC-34).
* **D1–D3 (§14).** D1: ≥ 90 % of the decisions at which the L-norm book is non-empty are constructible in the
  L-265 run at the same decision time (legs and any required hedge); D2: L-265 mean daily net (USD) > 0; D3:
  365 × L-265 mean daily net − 144 USD/year infrastructure > 0. Any ``current_approximation`` filter labels
  every D output "approximate (current filters)"; only forward point-in-time filters with no missing symbol
  are exact deployment evidence (S-14.1 steps 3–5). Fixed infrastructure cost never enters labels or L-norm.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from luna.v7.accounting import account, daily_ok, k10_ok, mean_daily_net
from luna.v7.book import engine
from luna.v7.book.engine import BookRun, EngineInputs, ScoreModel
from luna.v7.constants import FROZEN
from luna.v7.timeutil import MIN

DP = FROZEN.deploy
Provenance = Literal["pit_historical", "current_approximation", "forward_pit"]
PROVENANCES = ("pit_historical", "current_approximation", "forward_pit")
PIT = frozenset({"pit_historical", "forward_pit"})
LAYER_OF_E = {float(DP.E_265): "L-265", float(DP.E_10k): "L-10k"}

APPROX = "approximate (current filters)"  # S-14.1 step 3 (exact wording)
INCOMPLETE = "approximate/incomplete (filters missing)"  # DEC-34
PIT_HIST = "point-in-time historical filters"
FORWARD = "forward point-in-time filters"
_STEP_TOL = 1e-9  # a quantity within 1e-9 steps of a multiple counts as that multiple (float noise)


class CurrentFiltersNotHistorical(ValueError):
    """EC-6 / E8: current exchange filters reached a point-in-time metadata path."""


@dataclass(frozen=True)
class Filters:
    step: float
    min_qty: float
    min_notional: float
    provenance: Provenance
    known_at: int | None = None

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCES:
            raise ValueError(f"unknown filter provenance {self.provenance!r}")
        if not (math.isfinite(self.step) and self.step > 0):
            raise ValueError("step must be finite and > 0")
        for k in ("min_qty", "min_notional"):
            v = getattr(self, k)
            if not (math.isfinite(v) and v >= 0):
                raise ValueError(f"{k} must be finite and >= 0")
        if self.provenance in PIT and (self.known_at is None or isinstance(self.known_at, bool)
                                       or not isinstance(self.known_at, int)):
            raise ValueError("point-in-time filters need an int known_at (ns)")


def assert_not_backdated(obj: object) -> None:
    """EC-6: refuse any ``current_approximation`` filter (in ``obj`` or nested in mappings / sequences) on a
    path that consumes point-in-time historical metadata (R1, gates, features, labels, L-norm)."""
    if isinstance(obj, Filters):
        if obj.provenance == "current_approximation":
            raise CurrentFiltersNotHistorical(
                "current exchangeInfo filters are never point-in-time historical metadata (EC-6, E8)")
    elif isinstance(obj, Mapping):
        for v in obj.values():
            assert_not_backdated(v)
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        for v in obj:
            assert_not_backdated(v)


def round_order(qty: float, price: float, filters: Filters) -> float | None:
    """The exchange-rounded order quantity: towards zero to a multiple of ``step``; None (not traded) if the
    rounded quantity is 0, below ``min_qty``, its notional at ``price`` is below ``min_notional``, or any
    input is non-finite / non-positive (fail closed)."""
    try:
        q, p = float(qty), float(price)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(q) and q > 0 and math.isfinite(p) and p > 0):
        return None
    n = math.floor(q / filters.step + _STEP_TOL)
    if n <= 0:
        return None
    r = float(Decimal(n) * Decimal(repr(filters.step)))
    if r < filters.min_qty * (1 - _STEP_TOL) or r * p < filters.min_notional * (1 - 1e-12):
        return None
    return r


def _history(sym: str, v: Filters | Sequence[Filters]) -> tuple[Filters, ...]:
    hist = (v,) if isinstance(v, Filters) else tuple(v)
    if not hist or not all(isinstance(f, Filters) for f in hist):
        raise ValueError(f"{sym}: filters must be a Filters or a non-empty sequence of Filters")
    kinds = {f.provenance == "current_approximation" for f in hist}
    if len(kinds) > 1:
        raise ValueError(f"{sym}: current and point-in-time filters are never mixed (EC-6)")
    if True in kinds:
        if len(hist) != 1:
            raise ValueError(f"{sym}: one current-approximation snapshot per symbol")
        return hist
    return tuple(sorted(hist, key=lambda f: f.known_at))  # type: ignore[arg-type, return-value]


class _Sizer:
    """``book.engine.Lots`` over the supplied filters; records the symbols traded without filters."""

    def __init__(self, filters_by_symbol: Mapping[str, Filters | Sequence[Filters]]) -> None:
        self.hist = {str(s): _history(str(s), v) for s, v in filters_by_symbol.items()}
        self.missing: set[str] = set()

    def at(self, symbol: str, t: int) -> Filters | None:
        hist = self.hist.get(symbol)
        if hist is None:
            return None
        if hist[0].provenance == "current_approximation":
            return hist[0]
        ok = [f for f in hist if f.known_at <= int(t) - MIN]  # type: ignore[operator]
        return ok[-1] if ok else None

    def size(self, symbol: str, qty: float, price: float, t: int) -> float | None:
        f = self.at(symbol, t)
        if f is None:
            self.missing.add(symbol)  # DEC-34: excluded from exact feasibility, result incomplete
            return None
        return round_order(qty, price, f)


@dataclass(frozen=True)
class RunSpec:
    """The frozen strategy's run arguments (as ``book.engine.run``)."""

    arm: str
    inputs: EngineInputs
    window: tuple[int, int]
    h_star: int
    model: ScoreModel | None = None
    execution: str = "E_T"


def run_layer(run_inputs: RunSpec, E: float, filters_by_symbol: Mapping[str, Filters | Sequence[Filters]],  # noqa: N803
              ) -> BookRun:
    """L-265 (E = 265) or L-10k (E = 10,000) executed book with exchange filters (§14, S-14.1)."""
    layer = LAYER_OF_E.get(float(E))
    if layer is None:
        raise ValueError(f"E must be {DP.E_265} (L-265) or {DP.E_10k} (L-10k); L-norm: book.engine.run")
    if not isinstance(run_inputs, RunSpec):
        raise ValueError("run_inputs must be a RunSpec")
    sizer = _Sizer(filters_by_symbol)
    x = run_inputs
    r = engine._run(x.arm, x.inputs, x.window, x.h_star, x.model, x.execution, layer, equity0=float(E),
                    lots=sizer)
    prov = tuple(sorted({f.provenance for h in sizer.hist.values() for f in h}))
    return replace(r, filter_provenance=prov, filters_missing=tuple(sorted(sizer.missing)))


def label(run: BookRun) -> str:
    """S-14.1 label of a USD-layer result."""
    prov = set(run.filter_provenance)
    if "current_approximation" in prov:
        return APPROX
    if run.filters_missing or not prov:
        return INCOMPLETE
    return PIT_HIST if "pit_historical" in prov else FORWARD


def exact_evidence(run: BookRun) -> bool:
    """S-14.1 steps 4–5: only forward point-in-time filters, none missing."""
    return bool(set(run.filter_provenance) == {"forward_pit"} and not run.filters_missing)


def d1_share(lnorm: BookRun, l265: BookRun) -> tuple[float | None, int, int]:
    """(share, constructible, base): over the L-norm decisions with a non-empty post-decision book, the
    share whose L-265 decision at the same time is constructible (a missing L-265 decision counts as not
    constructible). share None when the base is empty."""
    ld, dd = lnorm.decisions, l265.decisions
    if ld is None or "n_post" not in ld.columns or "t" not in ld.columns:
        raise ValueError("L-norm decisions without 'n_post' (D1)")
    if dd is None or "constructible" not in dd.columns:
        raise ValueError("L-265 decisions without 'constructible' (D1)")
    cons = {int(t): bool(c) for t, c in zip(dd["t"], dd["constructible"], strict=True)}
    base = [int(t) for t, n in zip(ld["t"], ld["n_post"], strict=True) if int(n) > 0]
    ok = sum(cons.get(t, False) for t in base)
    return (ok / len(base) if base else None), ok, len(base)


def d_gates(lnorm: BookRun, l265: BookRun, infra_usd_year: float = DP.infra_usd_year) -> dict:
    """D1–D3 at 265 USD (§14) with the S-14.1 label. An invalid / non-accountable L-265 run fails every D."""
    if lnorm.layer != "L-norm" or l265.layer != "L-265":
        raise ValueError("d_gates needs an L-norm run and an L-265 run")
    if float(l265.equity0) != float(DP.E_265):
        raise ValueError("the L-265 run must start at E = 265 USD")
    same = ("arm", "h_star", "execution", "window")
    if any(getattr(lnorm, k) != getattr(l265, k) for k in same):
        raise ValueError(f"the L-norm and L-265 runs must share {same}")
    if not lnorm.valid:
        raise ValueError(f"the L-norm run is invalid: {lnorm.invalid}")
    out: dict = dict(label=label(l265), exact=exact_evidence(l265), incomplete=bool(l265.filters_missing),
                     filters_missing=l265.filters_missing, infra_usd_year=float(infra_usd_year))
    share, ok, base = d1_share(lnorm, l265)
    out.update(d1_share=share, d1_constructible=ok, d1_base=base)
    why = None
    if not l265.valid:
        why = f"L-265 run invalid: {l265.invalid}"
    else:
        try:
            led = account(l265)
            if not (k10_ok(led) and daily_ok(led)):
                why = "L-265 ledger fails K10 / the daily identity"
        except ValueError as e:
            why = f"L-265 run not accountable: {e}"
    if why is not None:
        out.update(D1=False, D2=False, D3=False, why=why, mean_daily_net_usd=None, annual_net_usd=None)
        return out
    m = mean_daily_net(led)
    annual = 365.0 * m
    out.update(D1=bool(share is not None and share >= DP.d1), D2=bool(m > 0),
               D3=bool(annual - float(infra_usd_year) > 0), why=None, mean_daily_net_usd=m,
               annual_net_usd=annual)
    return out


__all__ = ["APPROX", "FORWARD", "INCOMPLETE", "PIT_HIST", "CurrentFiltersNotHistorical", "Filters",
           "RunSpec", "assert_not_backdated", "d1_share", "d_gates", "exact_evidence", "label", "round_order",
           "run_layer"]
