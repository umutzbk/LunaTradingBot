"""The ONE place where V6 classes are produced (spec S-STAT-L, S-STAT-O, S-STAT-C, S-STAT-F). Reports
only display what these functions return; no other module decides a class.

Severity: INVALID RUN > MODEL INVALID > NO-GO (NO-GO-PRIMARY is a NO-GO label) >
{MODEL INCONCLUSIVE, INSUFFICIENT, INCONCLUSIVE} > ADVANCE / FINAL GO. Every check is evaluated;
the final class is the most severe one triggered; all reasons are reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from luna.v6 import stats6
from luna.v6.criteria import Evaluation
from luna.v6.spec import constants

_S = constants()["statistics"]
FAMILY = tuple(_S["holdout_family"])  # ("H3-k4", "H3-k6", "H1-m1.5", "H1-m2.5")
HOLDOUT_ALPHA = _S["holdout_alpha_fw"]
LOOKS = tuple(zip(_S["forward_looks_days"], _S["forward_alpha_fw"]))  # ((90, .001), (180, .001), (365, .048))
SEVERITY = {"INVALID RUN": 5, "MODEL INVALID": 4, "NO-GO": 3, "MODEL INCONCLUSIVE": 2, "INSUFFICIENT": 2,
            "INCONCLUSIVE": 2, "ADVANCE": 1, "FINAL GO": 1}
TIE_ORDER = ["MODEL INCONCLUSIVE", "INSUFFICIENT", "INCONCLUSIVE"]  # display order within severity 2 (D-V1)


@dataclass
class Verdict:
    config: str
    cls: str
    label: str
    reasons: list = field(default_factory=list)


def ladder(ev: Evaluation, *, stat_reject: bool, coverage_ok: bool = True,
           execution: str | None = None) -> Verdict:
    """S-STAT-L. ``stat_reject``: the config was rejected by Holm in its family at this look/run;
    P2 also needs the unadjusted LB95 > 0 (net and carry, both blocks) from ``ev.stats``."""
    reasons: list[tuple[str, str]] = []
    if ev.invalid_run:
        reasons += [("INVALID RUN", r) for r in ev.invalid_run]
    if ev.kind == "H1" and ev.env == "forward" and execution != "S-H1-X":
        reasons.append(("INVALID RUN", "H1 forward needs the S-H1-X execution model"))
    if ev.env == "forward":
        for k in ("P7", "P11"):
            if not ev.P.get(k, {}).get("evaluable"):
                reasons.append(("INVALID RUN", f"forward {k} row missing"))
    if ev.gate == "INVALID":
        reasons.append(("MODEL INVALID", "H2a gate INVALID"))
    P = ev.P
    if not P["P10"]["pass"]:
        reasons.append(("NO-GO", "P10 kill/liquidation"))
    no_go_primary = False
    if not (P["P1"]["pass"] and P["P3"]["pass"]):
        mb = ev.maker_bound
        if ev.kind == "H1" and mb is not None and mb["P1"] and mb["P3"]:
            no_go_primary = True
            reasons.append(("NO-GO", "P1/P3 <= 0 under PRIMARY taker (NO-GO-PRIMARY)"))
        else:
            reasons.append(("NO-GO", "P1/P3 <= 0"))
    for k in ("P4", "P5", "P6", "P7", "P11"):
        if P[k]["evaluable"] and P[k]["pass"] is False:
            reasons.append(("NO-GO", f"{k} failed"))
    for k in ("P8a", "P8b", "P8c", "P8d", "P9"):
        if k not in P:
            continue
        if P[k]["evaluable"] and P[k]["pass"] is False:
            reasons.append(("NO-GO", f"{k} failed"))
        elif not P[k]["evaluable"] and P["P1"]["pass"]:
            reasons.append(("INCONCLUSIVE", f"{k} not evaluable"))
    if not ev.evidence.get("ok", False):
        reasons.append(("INSUFFICIENT", "evidence gate (E1/E2/E3)"))
    if ev.gate == "INCONCLUSIVE":
        reasons.append(("MODEL INCONCLUSIVE", "H2a gate INCONCLUSIVE"))
    lb_ok = bool(ev.stats and ev.stats.get("lb_ok"))
    if not (stat_reject and lb_ok):
        reasons.append(("INCONCLUSIVE", "P2 (Holm rejection and LB95 > 0)"))
    if not coverage_ok:
        reasons.append(("INCONCLUSIVE", "forward coverage < 80 %"))
    if reasons:
        top = max(SEVERITY[c] for c, _ in reasons)
        tied = [c for c, _ in reasons if SEVERITY[c] == top]
        cls = tied[0] if top != 2 else next(c for c in TIE_ORDER if c in tied)
    else:
        cls = "ADVANCE" if ev.env == "holdout" else "FINAL GO"
    label = "NO-GO-PRIMARY" if cls == "NO-GO" and no_go_primary else cls
    return Verdict(ev.config, cls, label, [f"{c}: {r}" for c, r in reasons])


def feas_member(ev: Evaluation) -> bool:
    """S-H1-F FEAS membership (decided once, at the holdout decision)."""
    if ev.kind != "H1" or ev.invalid_run or ev.gate == "INVALID" or not ev.P["P10"]["pass"]:
        return False
    mb = ev.maker_bound
    if mb is None:
        return False
    primary_fails = not (ev.P["P1"]["pass"] and ev.P["P3"]["pass"])
    return primary_fails and mb["P1"] and mb["P3"] and mb["P5"] and mb["P6"]


@dataclass
class HoldoutDecision:
    p: dict
    holm: dict
    verdicts: dict
    cand: list
    feas: list


def holdout_decision(evals: dict) -> HoldoutDecision:
    """S-STAT-O: one run, the fixed family of 4, Holm at 0.05, then the ladder for each config."""
    if tuple(sorted(evals)) != tuple(sorted(FAMILY)):
        raise ValueError(f"the holdout family must be exactly {FAMILY}")
    p = {c: evals[c].stats["p_go"] for c in FAMILY}
    h = stats6.holm(p, HOLDOUT_ALPHA)
    verdicts = {c: ladder(evals[c], stat_reject=h[c]["reject"]) for c in FAMILY}
    cand = [c for c in FAMILY if _candidate(evals[c], verdicts[c])]
    feas = [c for c in FAMILY if feas_member(evals[c])]
    return HoldoutDecision(p, h, verdicts, cand, feas)


def _candidate(ev: Evaluation, v: Verdict) -> bool:
    bad = any(r.startswith(("INVALID RUN", "NO-GO", "MODEL INVALID")) for r in v.reasons)
    return not bad and ev.P["P1"]["pass"] and ev.P["P3"]["pass"]


# --- forward sequential looks (S-STAT-C / S-STAT-F) -------------------------------------------------


class LookError(RuntimeError):
    pass


@dataclass
class ForwardFamily:
    """Fixed at the holdout decision: always the 4 configs; denominator 4 at every look."""

    cand: tuple
    feas: tuple
    status: dict = field(default_factory=dict)
    looks_done: list = field(default_factory=list)
    history: list = field(default_factory=list)
    established: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if set(self.cand) & set(self.feas):
            raise ValueError("a config cannot be both a candidate and a feasibility slot")
        for c in FAMILY:
            self.status[c] = "PENDING" if (c in self.cand or c in self.feas) else "CLOSED"

    @property
    def size(self) -> int:
        return len(FAMILY)

    def look(self, day: int, p_go: dict, ladder_fn, nw_p: dict | None = None,
             feasibility: dict | None = None) -> dict:
        """One scheduled look. ``p_go``: config -> p_GO on its window (only used where the spec says
        it is tested); ``ladder_fn(config, stat_reject) -> Verdict`` for the configs still PENDING (for
        a FEAS slot: on its confirmation replay C); ``nw_p``: config -> (Newey-West p net, p carry),
        tested at interim looks against the config's own Holm threshold in this look's p vector;
        ``feasibility``: config -> bool (ESTABLISHED) at day 90."""
        expected = LOOKS[len(self.looks_done)][0] if len(self.looks_done) < len(LOOKS) else None
        if day != expected:
            raise LookError(f"look {day} is not the next scheduled look ({expected})")
        alpha = dict(LOOKS)[day]
        if day == 90:
            for c in self.feas:
                ok = bool((feasibility or {}).get(c, False))
                self.established[c] = ok
                if not ok:
                    self.status[c] = "CLOSED"
        p = {}
        for c in FAMILY:
            if self.status[c] in ("CLOSED", "FINAL GO"):
                p[c] = 1.0
            elif c in self.feas:
                p[c] = float(p_go[c]) if (day == 365 and self.established.get(c)) else 1.0
            elif c in self.cand:
                p[c] = float(p_go[c])
            else:
                p[c] = 1.0
        h = stats6.holm(p, alpha)  # denominator = 4 always
        rejected = {c for c in FAMILY if h[c]["reject"]}
        if day != 365:
            rejected = {c for c in rejected
                        if c in (nw_p or {}) and max(nw_p[c]) <= h[c]["alpha"]}
        out = {}
        for c in FAMILY:
            if self.status[c] != "PENDING":
                continue
            if c in self.feas and day == 90:
                continue  # day 90: only the M feasibility measurement; C is laddered from day 180 on
            v = ladder_fn(c, c in rejected)  # a FEAS slot is never rejected before 365 (p = 1)
            out[c] = v
            if v.cls == "FINAL GO":
                self.status[c] = "FINAL GO"
            elif any(r.startswith("MODEL INVALID") or "P10" in r for r in v.reasons):
                self.status[c] = "CLOSED"
            elif day == 365:
                self.status[c] = v.cls
        self.looks_done.append(day)
        self.history.append({"day": day, "alpha": alpha, "p": p, "holm": h, "verdicts": out,
                             "status": dict(self.status)})
        return {"alpha": alpha, "p": p, "holm": h, "verdicts": out, "status": dict(self.status)}

    def monitor_after_go(self, config: str, risk_breach: bool) -> None:
        if self.status.get(config) == "FINAL GO" and risk_breach:
            self.status[config] = "GO REVOKED"
