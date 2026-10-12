"""Profitability criteria (spec §7): P1, P3-P12, the evidence gate (S-CRIT-E), concentration
(S-CRIT-C) and temporal stability (S-CRIT-T). P2 needs the cross-config Holm step and is added by
``verdict``. Everything is computed on PRIMARY with the terminal unwind included."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.carry import stats as v5
from luna.v6.spec import constants

_CR = constants()["criteria"]
SHARPE_MIN = _CR["sharpe_min"]
MDD_MAX = _CR["max_drawdown_max"]
WORST_MIN = _CR["worst_day_min"]
ES_MIN = _CR["es975_min"]
EVID = _CR["evidence"]
EPISODE_GAP = pd.Timedelta(days=_CR["episode_gap_days"])
CLUSTER_GAP = pd.Timedelta(days=_CR["cluster_gap_days"])
P8A_SHARE, P8A_MIN = _CR["p8a_max_share"], _CR["p8a_min_assets"]
NEFF_MIN = _CR["p8c_neff_min"]
P8D_MIN, P8D_TOP, P8D_SHARE = _CR["p8d_min_closed_episodes"], _CR["p8d_top_share"], _CR["p8d_max_net_share"]
P9_FLOOR = _CR["p9_floor"]
HOUR = pd.Timedelta("1h")


# --- attribution, episodes, clusters ----------------------------------------------------------------


def asset_contributions(fills: pd.DataFrame, settlements: pd.DataFrame) -> pd.Series:
    """C_a = funding_a - fees_a - spread_slip_a - sum(dq * mid) over the asset's fills (flat at the end
    after the terminal unwind, so the last term is its whole price P&L). Sums to E_end - E0."""
    f = fills.copy()
    f["price"] = -f["dq"] * f["mid"]
    g = f.groupby("asset")[["price", "fee", "spread_slip"]].sum()
    fund = settlements.groupby("asset")["amount"].sum() if len(settlements) else pd.Series(dtype=float)
    out = g["price"] - g["fee"] - g["spread_slip"]
    return out.add(fund, fill_value=0.0)


def episodes(fills: pd.DataFrame, settlements: pd.DataFrame) -> pd.DataFrame:
    """Holding periods per asset (both legs of H1 together); a re-entry < 7 days after the previous
    close continues the same episode. Net = fills in the episode + funding settled in it."""
    rows = []
    for a, g in fills.sort_values(["ts"]).groupby("asset"):
        q = {}
        start = None
        spans = []
        for _, r in g.iterrows():
            was = any(abs(v) > 1e-12 for v in q.values())
            q[r["leg"]] = q.get(r["leg"], 0.0) + r["dq"]
            now = any(abs(v) > 1e-12 for v in q.values())
            if not was and now:
                start = r["ts"]
            elif was and not now:
                spans.append([start, r["ts"]])
        merged = []
        for s, e in spans:
            if merged and s - merged[-1][1] < EPISODE_GAP:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        for s, e in merged:
            fm = g[(g["ts"] >= s) & (g["ts"] <= e)]
            sm = settlements[(settlements["asset"] == a) & (settlements["ts"] > s) & (settlements["ts"] <= e + HOUR)] \
                if len(settlements) else settlements
            net = float((-fm["dq"] * fm["mid"]).sum() - fm["fee"].sum() - fm["spread_slip"].sum()
                        + (sm["amount"].sum() if len(sm) else 0.0))
            rows.append({"asset": a, "start": s, "end": e, "net": net, "closed": True})
    return pd.DataFrame(rows, columns=["asset", "start", "end", "net", "closed"])


def entries(fills: pd.DataFrame) -> list:
    """Every opening of a position from flat (per asset, both legs of H1 together), not merged."""
    out = []
    for _a, g in fills.sort_values(["ts"]).groupby("asset"):
        q = {}
        for _, r in g.iterrows():
            was = any(abs(v) > 1e-12 for v in q.values())
            q[r["leg"]] = q.get(r["leg"], 0.0) + r["dq"]
            if not was and any(abs(v) > 1e-12 for v in q.values()):
                out.append(r["ts"])
    return out


def clusters(entry_times: list) -> int:
    n, first = 0, None
    for t in sorted(entry_times):
        if first is None or t - first >= CLUSTER_GAP:
            n, first = n + 1, t
    return n


# --- the evaluation of one config ------------------------------------------------------------------


@dataclass
class Evaluation:
    config: str
    kind: str  # "H1" | "H3"
    env: str  # "holdout" | "forward"
    net: float
    carry: float
    P: dict = field(default_factory=dict)  # name -> {"pass": bool|None, "evaluable": bool, ...}
    evidence: dict = field(default_factory=dict)
    gate: str = "INCONCLUSIVE"
    maker_bound: dict | None = None  # H1 S-MAKER-BOUND point values
    invalid_run: list = field(default_factory=list)
    stats: dict | None = None
    daily_net: np.ndarray | None = None
    daily_carry: np.ndarray | None = None


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def evidence(run, kind: str) -> dict:
    th = EVID[kind]
    eps = episodes(run.fills, run.settlements)
    active = int(run.daily["active"].sum())
    n_eps = len(eps)
    n_cl = clusters(entries(run.fills)) if n_eps else 0
    und = int(eps["asset"].nunique()) if n_eps else 0
    ok = active >= th["active_days"] and n_eps >= th["episodes"] and n_cl >= th["clusters"] and und >= th["underlyings"]
    return {"active_days": active, "episodes": n_eps, "clusters": n_cl, "underlyings": und, "ok": ok,
            "thresholds": th}


def concentration(run, kind: str, net: float) -> dict:
    C = asset_contributions(run.fills, run.settlements)
    eps = episodes(run.fills, run.settlements)
    out = {}
    nz = C[C.abs() > 1e-12]
    if len(nz) < P8A_MIN:
        out["P8a"] = {"evaluable": False, "pass": None}
    else:
        out["P8a"] = {"evaluable": True, "pass": bool(nz.max() <= P8A_SHARE * net), "max": float(nz.max())}
    out["P8b"] = {"evaluable": True, "pass": bool(all(net - c > 0 for c in C)), "min_loao": float((net - C).min()) if len(C) else math.nan}
    cp = C.clip(lower=0.0)
    neff = float(cp.sum() ** 2 / (cp ** 2).sum()) if (cp ** 2).sum() > 0 else 0.0
    out["P8c"] = {"evaluable": True, "pass": neff >= NEFF_MIN[kind], "neff": neff}
    closed = eps[eps["closed"]]
    if len(closed) < P8D_MIN:
        out["P8d"] = {"evaluable": False, "pass": None, "n": len(closed)}
    else:
        tot = float(closed["net"].sum())
        top = closed["net"].sort_values(ascending=False).iloc[: math.ceil(P8D_TOP * len(closed))].sum()
        out["P8d"] = {"evaluable": True, "pass": bool(tot > 0 and top <= P8D_SHARE * tot), "n": len(closed)}
    return out


def temporal(run) -> dict:
    d = run.daily
    n = len(d)
    k = n // 3
    parts = [d.iloc[:k], d.iloc[k:2 * k], d.iloc[2 * k:]]
    e0 = run.equity0
    res = []
    for p in parts:
        pnl = float(p["net"].sum()) / e0
        evaluable = int(p["active"].sum()) >= max(15, math.ceil(0.25 * len(p)))
        res.append({"days": len(p), "pnl": pnl, "evaluable": evaluable, "floor_ok": pnl >= P9_FLOOR})
    if not all(r["floor_ok"] for r in res):
        return {"evaluable": True, "pass": False, "parts": res, "why": "floor"}
    ev = [r for r in res if r["evaluable"]]
    if len(ev) < 2:
        return {"evaluable": False, "pass": None, "parts": res}
    pos = sum(r["pnl"] > 0 for r in ev)
    ok = pos == 2 if len(ev) == 2 else pos >= 2
    return {"evaluable": True, "pass": bool(ok), "parts": res}


def evaluate(config: str, kind: str, env: str, primary, rows: dict, gate_cls: str) -> Evaluation:
    """``rows``: {"S-COST2X": run, "S-PIT10": run, "S-FB-CONS": run (historical only),
    "S-MAKER-BOUND": run (H1 historical only)}; runs expose daily, fills, settlements, counters, equity0."""
    d = primary.daily
    net = float(d["net"].sum())
    carry = float(d["carry"].sum())
    r = d["ret"].to_numpy(float)
    ev = Evaluation(config, kind, env, net, carry, gate=gate_cls)
    if primary.counters.get("funding_unknown", 0) > 0:
        ev.invalid_run.append(f"funding unknown for {primary.counters['funding_unknown']} held periods (S-DATA-1)")
    P = ev.P
    P["P1"] = {"evaluable": True, "pass": net > 0, "value": net}
    P["P3"] = {"evaluable": True, "pass": carry > 0, "value": carry}
    sh = v5.sharpe(pd.Series(r))
    P["P4"] = {"evaluable": True, "pass": bool(sh >= SHARPE_MIN) if math.isfinite(sh) else False, "value": sh}
    mdd = v5.max_drawdown(pd.Series(r))
    P["P5"] = {"evaluable": True, "pass": mdd <= MDD_MAX, "value": mdd}
    wd, es = v5.worst_day(pd.Series(r)), v5.es975(pd.Series(r))
    P["P6"] = {"evaluable": True, "pass": bool(wd >= WORST_MIN and es >= ES_MIN), "worst": wd, "es": es}
    c2 = rows.get("S-COST2X")
    P["P7"] = {"evaluable": c2 is not None, "pass": (float(c2.daily["net"].sum()) >= 0) if c2 is not None else None}
    P["P10"] = {"evaluable": True, "pass": primary.counters.get("kill", 0) == 0 and primary.counters.get("liquidation", 0) == 0}
    sens = [k for k in ("S-PIT10", "S-FB-CONS") if k in rows]
    flips = []
    for k in sens:
        rn, rc = float(rows[k].daily["net"].sum()), float(rows[k].daily["carry"].sum())
        if _sign(rn) != _sign(net) or _sign(rc) != _sign(carry):
            flips.append(k)
    P["P11"] = {"evaluable": bool(sens), "pass": (not flips) if sens else None, "flips": flips}
    P["P12"] = {"evaluable": True, "pass": gate_cls == "VALID", "gate": gate_cls}
    ev.evidence = evidence(primary, kind)
    if net > 0:
        P.update(concentration(primary, kind, net))
    else:
        for k in ("P8a", "P8b", "P8c", "P8d"):
            P[k] = {"evaluable": False, "pass": None, "why": "P1 failed"}
    P["P9"] = temporal(primary)
    mb = rows.get("S-MAKER-BOUND")
    if mb is not None:
        mr = mb.daily["ret"].to_numpy(float)
        ev.maker_bound = {"P1": float(mb.daily["net"].sum()) > 0, "P3": float(mb.daily["carry"].sum()) > 0,
                          "P5": v5.max_drawdown(pd.Series(mr)) <= MDD_MAX,
                          "P6": bool(v5.worst_day(pd.Series(mr)) >= WORST_MIN and v5.es975(pd.Series(mr)) >= ES_MIN)}
    ev.daily_net = d["ret"].to_numpy(float)
    ev.daily_carry = d["carry_ret"].to_numpy(float)
    return ev
