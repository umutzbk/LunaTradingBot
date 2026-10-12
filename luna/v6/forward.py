"""Forward evaluation (spec S-DATA-4/5, S-STAT-C/F, S-H1-F). Runs only at a scheduled look, only after
the code freeze (T_F) and the single holdout opening, in order, never twice. The family is fixed at
the holdout decision (all 4 configs). The frozen code replays the collector data received at or
after T_F; nothing before a scheduled look is computed or shown (S-GUARD-3)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from luna.v6 import guard, stats6
from luna.v6.criteria import episodes, evaluate
from luna.v6.forecast import gate
from luna.v6.verdict import FAMILY, LOOKS, ForwardFamily, LookError, Verdict, ladder

DAY = pd.Timedelta("1D")
STATE_REL = "docs/results/v6-forward/state.json"
_LAST_LOOK = LOOKS[-1][0]


def day1(tf: pd.Timestamp) -> pd.Timestamp:
    tf = pd.Timestamp(tf)
    return tf if tf == tf.floor("D") else tf.ceil("D")


def look_date(tf: pd.Timestamp, day: int) -> pd.Timestamp:
    return day1(tf) + day * DAY


def check_look(day: int, now: pd.Timestamp, repo: Path | None = None) -> pd.Timestamp:
    """The scheduled look may run only on or after its date, after T_F and the holdout opening."""
    tf = guard.forward_start(repo)
    if tf is None:
        raise guard.HoldoutBlocked("forward evaluation before the code freeze (no T_F)")
    if day not in dict(LOOKS):
        raise LookError(f"day {day} is not a scheduled look")
    d = look_date(tf, day)
    if pd.Timestamp(now) < d:
        raise LookError(f"look {day} is due at {d}; forward P&L stays masked until then")
    guard.require_window(day1(tf), d, "forward", repo=repo)
    return d


@dataclass
class FeasibilityMeasurement:
    maker_share: float
    round_trip: float
    p1: bool
    p3: bool
    episodes: int

    @property
    def established(self) -> bool:
        return (self.maker_share >= 0.50 and self.round_trip <= 0.0125 and self.p1 and self.p3
                and self.episodes >= 3)


def feasibility(run, unwind_cost: dict, lag_pnl: float) -> FeasibilityMeasurement:
    """S-H1-F M-a / M-b / M-c on the day 1-90 measurement replay M. ``unwind_cost``: the hypothetical
    taker unwind at day 90 of the episodes still open (fee + spread/slip, both legs, from the latest
    books received by day 90); ``lag_pnl``: the hedge-less P&L between the spot and perp fills."""
    f = run.fills[run.fills["intent"] != "terminal"]
    spot = f[f["leg"] == "spot"]
    total = float((spot["dq"].abs() * spot["mid"]).sum())
    maker = float((spot["dq"].abs() * spot["mid"])[spot.get("maker", pd.Series(False, index=spot.index)).astype(bool)].sum()) \
        if "maker" in spot else 0.0
    entry_notional = float((spot[spot["intent"] == "open"]["dq"].abs() * spot[spot["intent"] == "open"]["mid"]).sum())
    costs = float(f["fee"].sum() + f["spread_slip"].sum()) + float(sum(unwind_cost.values()))
    rt = (costs - lag_pnl) / entry_notional if entry_notional > 0 else math.inf
    d = run.daily
    return FeasibilityMeasurement(maker / total if total else 0.0, rt, float(d["net"].sum()) > 0,
                                  float(d["carry"].sum()) > 0, len(episodes(run.fills, run.settlements)))


def load_state(repo: Path | None = None) -> ForwardFamily:
    p = Path(repo or guard.code_repo()) / STATE_REL
    raw = json.loads(p.read_text(encoding="utf-8"))
    fam = ForwardFamily(tuple(raw["cand"]), tuple(raw["feas"]))
    fam.status.update(raw["status"])
    fam.looks_done = list(raw["looks_done"])
    fam.established = dict(raw.get("established", {}))
    return fam


def save_state(fam: ForwardFamily, repo: Path | None = None) -> None:
    p = Path(repo or guard.code_repo()) / STATE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cand": list(fam.cand), "feas": list(fam.feas), "status": fam.status,
                             "looks_done": fam.looks_done, "established": fam.established}, indent=1),
                 encoding="utf-8")


def init_family(cand: list, feas: list, repo: Path | None = None) -> ForwardFamily:
    """Fixed once at the holdout decision; refuses to overwrite an existing family."""
    p = Path(repo or guard.code_repo()) / STATE_REL
    if p.exists():
        raise LookError("the forward family is already fixed")
    fam = ForwardFamily(tuple(cand), tuple(feas))
    save_state(fam, repo)
    return fam


def evaluate_forward(name: str, kind: str, k, run, rows: dict, funding, window_end: pd.Timestamp,
                     coverage_share: float):
    """Evaluation, stats (B = 200000), gate and the Newey-West p-values for one forward window.
    Returns (evaluation, gate result, coverage_ok, {"net": p, "carry": p})."""
    h = 168 if kind == "H3" else 720
    g = gate(funding, run.gate_records, kind=kind, k=k if kind == "H3" else None, h=h, window_end=window_end)
    ev = evaluate(name, kind, "forward", run, rows, g.cls)
    ev.stats = stats6.p_go(run.daily["ret"].to_numpy(float), run.daily["carry_ret"].to_numpy(float), stats6.B_FORWARD)
    nwp = {"net": stats6.newey_west_p(ev.daily_net), "carry": stats6.newey_west_p(ev.daily_carry)}
    return ev, g, coverage_share >= 0.80, nwp


def nw_pass(evaluations: dict, c: str, alpha: float) -> bool:
    """Interim add-on (S-STAT-F): both Newey-West p-values <= the config's Holm threshold at this
    look, alpha / (4 - j + 1) for its rank j among the 4 slots (untested slots p = 1)."""
    _, _, nwp = evaluations[c]
    p = {cc: e[0].stats["p_go"] for cc, e in evaluations.items()}
    p.update({cc: 1.0 for cc in FAMILY if cc not in p})
    order = sorted(p, key=lambda x: (p[x], x))
    thr = alpha / (len(FAMILY) - order.index(c))
    return max(nwp["net"], nwp["carry"]) <= thr


def run_look(day: int, now: pd.Timestamp, evaluations: dict, feasibility_results: dict | None = None,
             repo: Path | None = None) -> dict:
    """One scheduled look. ``evaluations``: config -> (Evaluation, coverage_ok, nw p-values) on the
    config's frozen window (the confirmation replay C for a FEAS slot at day 365)."""
    check_look(day, now, repo)
    fam = load_state(repo)
    alpha = dict(LOOKS)[day]
    p_go = {c: e[0].stats["p_go"] for c, e in evaluations.items()}

    def lad(c: str, rej: bool) -> Verdict:
        ev, cov_ok, _ = evaluations[c]
        return ladder(ev, stat_reject=rej, coverage_ok=cov_ok, execution="S-H1-X" if ev.kind == "H1" else None)

    res = fam.look(day, p_go, lad, nw_ok={c: nw_pass(evaluations, c, alpha) for c in evaluations},
                   feasibility={c: v.established for c, v in (feasibility_results or {}).items()})
    save_state(fam, repo)
    return res


def lag_and_unwind(run, market, at: pd.Timestamp) -> tuple[float, dict]:
    """Hedge-less P&L between each spot fill and its perp fill (spot qty x spot mid change), and the
    hypothetical taker unwind cost at ``at`` of the pairs still open (both legs, latest books by ``at``)."""
    f = run.fills[run.fills["intent"] != "terminal"]
    lag = 0.0
    for (_a, _d), g in f.groupby(["asset", "decision_ts"]):
        sp, pp = g[g["leg"] == "spot"], g[g["leg"] == "perp"]
        if sp.empty or pp.empty:
            continue
        tp = pp["ts"].max()
        for _, r in sp.iterrows():
            m1 = market.spot_mid(r["asset"], tp)
            if math.isfinite(m1):
                lag += r["dq"] * (m1 - r["mid"])
    open_q = f.groupby(["asset", "leg"])["dq"].sum()
    costs = {}
    for (a, leg), q in open_q.items():
        if abs(q) > 1e-12:
            _, fee, ss = market.unwind_cost(a, q, at, leg=leg)
            costs[(a, leg)] = fee + ss
    return lag, costs
