"""Forward evaluation (spec S-DATA-4/5, S-STAT-C/F, S-H1-F). Runs only at a scheduled look, only after
the code freeze (T_F) and the single holdout opening, in order, never twice. The family is fixed at
the holdout decision (all 4 configs). The frozen code replays the collector data received at or
after T_F; nothing before a scheduled look is computed or shown (S-GUARD-3).

Windows: a candidate config runs flat from forward day 1 to the look date. A FEAS config (H1,
NO-GO-PRIMARY at the holdout) runs the measurement replay M on days 1-90 (day-90 look only) and the
confirmation replay C from day 91, flat with 265 USD, nothing carried from M (S-H1-F)."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from luna.v6 import guard, stats6
from luna.v6.criteria import episodes, evaluate
from luna.v6.forecast import H2a, gate
from luna.v6.verdict import FAMILY, LOOKS, ForwardFamily, LookError, Verdict, ladder

DAY = pd.Timedelta("1D")
STATE_REL = "docs/results/v6-forward/state.json"
PIT_LAG = pd.Timedelta("5min")
CONFIGS = {"H3-k4": ("H3", 4), "H3-k6": ("H3", 6), "H1-m1.5": ("H1", 1.5), "H1-m2.5": ("H1", 2.5)}


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
    """S-H1-F M-a / M-b / M-c on the day 1-90 measurement replay M."""
    f = run.fills[run.fills["intent"] != "terminal"]
    spot = f[f["leg"] == "spot"]
    notion = spot["dq"].abs() * spot["mid"]
    total = float(notion.sum())
    maker = float(notion[spot["maker"].astype(bool)].sum())
    opens = spot[spot["intent"] == "open"]
    entry_notional = float((opens["dq"].abs() * opens["mid"]).sum())
    costs = float(f["fee"].sum() + f["spread_slip"].sum()) + float(sum(unwind_cost.values()))
    rt = (costs - lag_pnl) / entry_notional if entry_notional > 0 else math.inf
    d = run.daily
    return FeasibilityMeasurement(maker / total if total else 0.0, rt, float(d["net"].sum()) > 0,
                                  float(d["carry"].sum()) > 0, len(episodes(run.fills, run.settlements)))


def lag_and_unwind(run, market, at: pd.Timestamp) -> tuple[float, dict]:
    """Hedge-less P&L between each spot fill and its perp fill (spot qty x spot mid change), and the
    hypothetical taker unwind cost at ``at`` of the pairs still open (both legs, latest books by ``at``)."""
    f = run.fills[run.fills["intent"] != "terminal"]
    lag = 0.0
    for _, g in f.groupby(["asset", "decision_ts"]):
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


def check_inputs_reach(inputs) -> None:
    """A forward look needs the model history complete up to the holdout end (S-DATA-4): the last hourly
    funding period and the last hourly price must reach HOLDOUT_END (floored to the hour) minus 1 h.
    Otherwise the look is refused (INVALID LOOK), never run on a silent warm-up."""
    need = guard.HOLDOUT_END.floor("h") - pd.Timedelta("1h")
    short = []
    for name, frame in (("funding", inputs.funding.rel), ("prices", inputs.prices)):
        for a in frame.columns:
            col = frame[a].dropna()
            last = col.index.max() if len(col) else None
            if last is None or last < need:
                short.append(f"{name} {a} ends {last}")
    if short:
        raise LookError(f"INVALID LOOK: model history does not reach the holdout end ({need}) for every asset: "
                        + "; ".join(short[:10]))


def coverage_share(cov: pd.DataFrame, lo: pd.Timestamp) -> float:
    """S-DATA-5: the share of EVALUABLE days (each day >= 90 % fresh quarters) from ``lo`` on (a FEAS
    slot's C window starts at day 91); the look is INCONCLUSIVE below 80 %."""
    c = cov[cov.index >= pd.Timestamp(lo)]
    return float(c["evaluable"].astype(bool).mean()) if len(c) else 0.0


def cost2x_view(run):
    """Forward S-COST2X (S-H1-6): net - 1 x (all realised fees + spread/slip), per day."""
    d = run.daily.copy()
    d["net"] = d["net"] - d["fees"] - d["spread_slip"]
    return SimpleNamespace(daily=d)


# --- family state ----------------------------------------------------------------------------------


def _state_path(repo: Path | None) -> Path:
    return Path(repo or guard.code_repo()) / STATE_REL


def load_state(repo: Path | None = None) -> ForwardFamily:
    raw = json.loads(_state_path(repo).read_text(encoding="utf-8"))
    fam = ForwardFamily(tuple(raw["cand"]), tuple(raw["feas"]))
    fam.status.update(raw["status"])
    fam.looks_done = list(raw["looks_done"])
    fam.established = dict(raw.get("established", {}))
    fam.history = list(raw.get("history", []))
    return fam


def save_state(fam: ForwardFamily, repo: Path | None = None) -> None:
    p = _state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cand": list(fam.cand), "feas": list(fam.feas), "status": fam.status,
                               "looks_done": fam.looks_done, "established": fam.established,
                               "history": fam.history}, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def init_family(cand: list, feas: list, repo: Path | None = None) -> ForwardFamily:
    """Fixed once at the holdout decision. The complete state is written to a temporary file and then
    linked into place (create-if-absent, atomic): a crash leaves either no family or the full one.
    Re-running with the same family is idempotent (restart-safe); a different family is refused."""
    p = _state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    fam = ForwardFamily(tuple(cand), tuple(feas))
    tmp = p.with_name(f".{p.name}.{os.getpid()}.init")
    tmp.write_text(json.dumps({"cand": list(fam.cand), "feas": list(fam.feas), "status": fam.status,
                               "looks_done": [], "established": {}, "history": []}, indent=1), encoding="utf-8")
    try:
        os.link(tmp, p)
    except FileExistsError:
        old = load_state(repo)
        if tuple(old.cand) == fam.cand and tuple(old.feas) == fam.feas:
            return old
        raise LookError("the forward family is already fixed") from None
    finally:
        tmp.unlink(missing_ok=True)
    return fam


# --- evaluation of one window ----------------------------------------------------------------------


def evaluate_forward(name: str, kind: str, k, run, rows: dict, funding, window_end: pd.Timestamp,
                     coverage_share: float):
    """Evaluation, stats (B = 200000), gate and the Newey-West p-values for one forward window.
    Returns (evaluation, gate result, coverage_ok, (NW p net, NW p carry))."""
    h = 168 if kind == "H3" else 720
    g = gate(funding, run.gate_records, kind=kind, k=k if kind == "H3" else None, h=h, window_end=window_end)
    ev = evaluate(name, kind, "forward", run, rows, g.cls)
    ev.stats = stats6.p_go(run.daily["ret"].to_numpy(float), run.daily["carry_ret"].to_numpy(float), stats6.B_FORWARD)
    nwp = (stats6.newey_west_p(ev.daily_net), stats6.newey_west_p(ev.daily_carry))
    return ev, g, coverage_share >= 0.80, nwp


def run_window(name: str, inputs, start: pd.Timestamp, end: pd.Timestamp, market_fn, models: dict):
    """PRIMARY and the forward S-PIT10 row of one config on [start, end) (flat, 265 USD). ``models``:
    {(row, h): H2a} — the S-PIT10 models see funding 5 minutes later (FundingPanel.delayed)."""
    from luna.v6.engine import H3Config, H3Engine
    from luna.v6.h1 import H1Config, H1Engine
    from luna.v6.riskmodel import ewma_vol

    kind, param = CONFIGS[name]
    runs = {}
    for row, lag in (("PRIMARY", pd.Timedelta(0)), ("S-PIT10", PIT_LAG)):
        m = market_fn(lag)
        if kind == "H3":
            eng = H3Engine(m, models[(row, 168)], inputs.prices, ewma_vol(inputs.prices), inputs.insts,
                           H3Config(name, param, row))
        else:
            eng = H1Engine(m, models[(row, 720)], inputs.insts, H1Config(name, param, row))
        runs[row] = eng.run(start, end)
    return runs


def evaluate_window(name: str, inputs, start: pd.Timestamp, end: pd.Timestamp, market_fn, models: dict,
                    share: float):
    kind, param = CONFIGS[name]
    runs = run_window(name, inputs, start, end, market_fn, models)
    prim = runs["PRIMARY"]
    rows = {"S-COST2X": cost2x_view(prim), "S-PIT10": runs["S-PIT10"]}
    ev, g, cov_ok, nwp = evaluate_forward(name, kind, param, prim, rows, models[("PRIMARY", 168)].panel, end,
                                          share)
    return {"run": prim, "eval": ev, "gate": g, "coverage_ok": cov_ok, "nw": nwp}


def run_look(day: int, now: pd.Timestamp, evaluations: dict, feasibility_results: dict | None = None,
             repo: Path | None = None) -> dict:
    """One scheduled look. ``evaluations``: config -> {"eval", "coverage_ok", "nw"} on the config's
    frozen window (the confirmation replay C for a FEAS slot)."""
    check_look(day, now, repo)
    fam = load_state(repo)
    p_go = {c: e["eval"].stats["p_go"] for c, e in evaluations.items()}

    def lad(c: str, rej: bool) -> Verdict:
        e = evaluations[c]
        ev = e["eval"]
        return ladder(ev, stat_reject=rej, coverage_ok=e["coverage_ok"],
                      execution="S-H1-X" if ev.kind == "H1" else None)

    res = fam.look(day, p_go, lad, nw_p={c: e["nw"] for c, e in evaluations.items()},
                   feasibility={c: v.established for c, v in (feasibility_results or {}).items()})
    for c, e in evaluations.items():  # GO REVOKED monitoring (S-STAT-F)
        if fam.status.get(c) == "FINAL GO" and c not in res["verdicts"]:
            P = e["eval"].P
            breach = not (P["P5"]["pass"] and P["P6"]["pass"] and P["P10"]["pass"])
            fam.monitor_after_go(c, breach)
    save_state(fam, repo)
    return {**res, "status": dict(fam.status)}


def forward_look(day: int, now: pd.Timestamp, raw: Path, frozen_root: Path, holdout_root: Path,
                 api_funding: pd.DataFrame | None = None, repo: Path | None = None) -> dict:
    """The scheduled look end to end: the verified holdout history (read only, pinned at the opening),
    guarded collector loads, the family's windows, the look, the state."""
    from luna.v6.devdata import Inputs
    from luna.v6.forward_exec import (FwdMarket, coverage, funding_from_collector, hourly_marks, merge_funding,
                                      read_streams, settlement_panel)
    from luna.v6.holdout_data import load_holdout_inputs

    d = check_look(day, now, repo)
    tf = guard.forward_start(repo)
    d1 = day1(tf)
    fam = load_state(repo)
    hist = load_holdout_inputs(frozen_root, holdout_root, repo=repo)
    check_inputs_reach(hist)
    assets = list(hist.mark.close.columns)
    hend = guard.HOLDOUT_END
    s_inputs = read_streams(raw, hend, d, assets, repo=repo, purpose="inputs")
    s_evidence = read_streams(raw, tf, d, assets, repo=repo, purpose="evidence")
    fund_c = funding_from_collector(raw, hend - pd.Timedelta("48h"), d, assets, repo=repo)
    funding = merge_funding(hist.funding, fund_c, hend)
    settle = settlement_panel(funding, api_funding, assets)
    prices = hourly_marks(hist.prices, s_inputs, hend, d)
    inputs = Inputs("forward", hist.mark, hist.trade, hist.volume_usd, hist.spot, hist.spot_volume_usd, hist.index,
                    funding, hist.spreads, hist.insts, prices)
    lagged = funding.delayed(PIT_LAG)
    models = {("PRIMARY", 168): H2a(funding, 168), ("PRIMARY", 720): H2a(funding, 720),
              ("S-PIT10", 168): H2a(lagged, 168), ("S-PIT10", 720): H2a(lagged, 720)}

    def market_fn(lag):
        return FwdMarket(s_evidence, lagged if lag else funding, assets, settlement=settle, pit_lag=lag)

    cov = coverage(s_evidence, d1, d - pd.Timedelta("15min"))
    evaluations, feas_results = {}, {}
    for c in FAMILY:
        st = fam.status[c]
        if st in ("CLOSED", "GO REVOKED"):
            continue
        if c in fam.feas:
            if day == 90:
                m = market_fn(pd.Timedelta(0))
                run = run_window(c, inputs, d1, d1 + 90 * DAY, market_fn, models)["PRIMARY"]
                lag, unw = lag_and_unwind(run, m, d1 + 90 * DAY)
                feas_results[c] = feasibility(run, unw, lag)
                continue
            if not fam.established.get(c):
                continue
            c0 = d1 + 90 * DAY  # the confirmation replay C: flat from day 91, its own coverage window
            evaluations[c] = evaluate_window(c, inputs, c0, d, market_fn, models, coverage_share(cov, c0))
        else:
            evaluations[c] = evaluate_window(c, inputs, d1, d, market_fn, models, coverage_share(cov, d1))
    res = run_look(day, now, evaluations, feas_results, repo)
    return {"look": res, "coverage_share": coverage_share(cov, d1),
            "feasibility": {c: vars(v) for c, v in feas_results.items()}}
