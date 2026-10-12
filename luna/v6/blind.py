"""Blind development run (spec S-GUARD-2): the frozen configs run on the development period and ONLY
invariants leave this module. No code path returns P&L, returns, equity, carry, Sharpe, calibration
performance, risk events that reveal performance (kills, stops, R3) or a config ranking; there is no
option that removes the mask. The report is built from the whitelist below and checked for forbidden keys.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from luna.v6 import guard
from luna.v6.criteria import asset_contributions
from luna.v6.devdata import load_dev
from luna.v6.forecast import H2a
from luna.v6.runner import CONFIGS, run_config

DEV_START = pd.Timestamp("2025-11-10T00:00Z")
DEV_END = guard.HOLDOUT_START
FORBIDDEN = ("pnl", "net", "ret", "equity", "carry", "sharpe", "profit", "loss", "drawdown", "kill", "stop",
             "r3", "calib", "rank", "funding_amount", "fee", "spread_slip", "price", "gate", "p_go", "holm")


def _q(x) -> dict:
    x = np.asarray([v for v in x if v is not None and math.isfinite(v)], float)
    if not len(x):
        return {"n": 0}
    return {"n": int(len(x)), "min": float(x.min()), "median": float(np.median(x)), "max": float(x.max())}


def availability(inp, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    out = {}
    sl = slice(start, end - pd.Timedelta("15min"))
    for a in inp.mark.close.columns:
        sp = inp.spreads[a]
        sp = sp[(sp.index >= start) & (sp.index < end)]
        fr = inp.funding.rel[a]
        fr = fr[(fr.index >= start) & (fr.index < end)]
        out[a] = {
            "mark_bars": float(inp.mark.close.loc[sl, a].notna().mean()),
            "perp_trade_bars": float(inp.trade.close.loc[sl, a].notna().mean()),
            "spot_bars": float(inp.spot.close.loc[sl, a].notna().mean()),
            "spread_snapshots_valid": float(((sp > 0) & (sp <= 0.05)).sum() / max(1, (end - start) / pd.Timedelta("5min"))),
            "funding_periods_known": float(fr.notna().sum() / max(1, (end - start) / pd.Timedelta("1h"))),
        }
    return out


def _h3_invariants(r) -> dict:
    ok = [d for d in r.decisions if d.get("status") == "OK"]
    inf = [d for d in r.decisions if d.get("status") == "INFEASIBLE"]
    warm = [d for d in r.decisions if d.get("status") == "MODEL WARMUP"]
    n = max(1, len(r.decisions))
    reasons = {}
    for d in inf:
        k = str(d.get("reason", "")).split(":")[0]
        reasons[k] = reasons.get(k, 0) + 1
    post = [d.get("post") or {} for d in ok]
    build = [d.get("build") or {} for d in ok]
    legs = [len(d["long"]) + len(d["short"]) for d in ok]
    return {
        "decision_share": {"ok": len(ok) / n, "infeasible": len(inf) / n, "model_warmup": len(warm) / n},
        "infeasible_reasons": reasons,
        "covariance_unavailable_decisions": reasons.get("covariance", 0),
        "legs_per_ok_decision": _q(legs),
        "longs_per_ok_decision": _q([len(d["long"]) for d in ok]),
        "shorts_per_ok_decision": _q([len(d["short"]) for d in ok]),
        "min_qty_dropped_legs": int(sum(len(b.get("dropped", [])) for b in build)),
        "risk_checks_satisfied_share": float(np.mean([bool(p.get("ok")) for p in post])) if post else None,
        "beta_exposure_abs_frac": _q([abs(p.get("beta_net", math.nan)) for p in post]),
        "standalone_side_vol_ratio_q": _q([p.get("q") for p in post]),
        "dollar_cap_bound_share": float(np.mean([bool(b.get("cap_bound")) for b in build])) if build else None,
        "ratio_rule_bound_share": float(np.mean([bool(b.get("ratio_bound")) for b in build])) if build else None,
        "beta_scaled_share": float(np.mean([bool(b.get("beta_scaled")) for b in build])) if build else None,
        "euler_long_risk_share": _q([(d.get("risk") or {}).get("RC_L") for d in ok]),
    }


def _h1_invariants(r) -> dict:
    o = r.fills[r.fills["intent"] == "open"]
    mism = 0
    for _, g in o.groupby(["asset", "ts"]):
        s, p = g[g["leg"] == "spot"]["dq"].sum(), g[g["leg"] == "perp"]["dq"].sum()
        mism += int(abs(s + p) > 1e-9)
    warm = sum(1 for d in r.decisions if d.get("status") == "MODEL WARMUP")
    n = max(1, len(r.decisions))
    return {
        "decision_share": {"model_warmup": warm / n},
        "entries": int(r.counters.get("entries", 0)),
        "skipped_below_min_qty": int(r.counters.get("skipped_size", 0)),
        "unmatched_hedge_qty_entries": mism,
        "pairs_held_per_tick": _q(r.ticks["n_pairs"].to_numpy(float)) if "n_pairs" in r.ticks else {"n": 0},
    }


def _identities(r) -> dict:
    d = r.daily
    eq_end = float(r.ticks["equity"].iloc[-1])
    contrib = float(asset_contributions(r.fills, r.settlements).sum()) if len(r.fills) else 0.0
    nonterm = r.fills[r.fills["intent"] != "terminal"]
    delay_bad = int(((nonterm["ts"] - nonterm["decision_ts"]) != pd.Timedelta("15min")).sum())
    flat = r.fills.groupby(["asset", "leg"])["dq"].sum().abs().max() if len(r.fills) else 0.0
    return {
        "residual_daily_sum_vs_equity_change": abs(float(d["net"].sum()) - (eq_end - r.equity0)),
        "residual_asset_attribution_vs_daily_sum": abs(contrib - float(d["net"].sum())),
        "residual_component_identity_max": float((d["net"] - (d["funding"] - d["fees"] - d["spread_slip"] + d["price"])).abs().max()),
        "fills_not_at_decision_plus_15m": delay_bad,
        "max_open_qty_after_terminal_unwind": float(flat),
    }


def _pit_checks(inp, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    out = {}
    for h in (168, 720):
        m = H2a(inp.funding, h)
        bad, n = 0, 0
        for t in pd.date_range(start, end - pd.Timedelta("1D"), freq="14D"):
            f = m.fit(t)
            if f is None:
                continue
            n += 1
            bad += int(f.max_label_end > t or f.max_sample_date + pd.Timedelta(hours=h) > t)
        out[f"h2a_{h}_fits_checked"] = n
        out[f"h2a_{h}_label_after_decision"] = bad
    out["inputs_end_before_holdout"] = bool(inp.mark.close.index.max() < guard.HOLDOUT_START)
    return out


def _assert_masked(obj, path="") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            low = str(k).lower()
            if any(tok in low for tok in FORBIDDEN) and not low.startswith(("residual", "fills_not", "max_open")):
                raise RuntimeError(f"blind report would expose a masked field: {path}/{k}")
            _assert_masked(v, f"{path}/{k}")


def blind_dev_run(frozen_root: Path, start: pd.Timestamp = DEV_START, end: pd.Timestamp = DEV_END) -> dict:
    inp = load_dev(Path(frozen_root))
    guard.require_window(start, end - pd.Timedelta("15min"), "dev")
    report = {"window": [str(start), str(end)], "data_availability": availability(inp, start, end),
              "pit_checks": _pit_checks(inp, start, end), "configs": {}}
    models = {168: H2a(inp.funding, 168), 720: H2a(inp.funding, 720)}
    for name, (kind, _) in CONFIGS.items():
        r = run_config(inp, name, start, end, rows=("PRIMARY",), models=models, invariant_mode=True)["PRIMARY"]
        inv = _h3_invariants(r) if kind == "H3" else _h1_invariants(r)
        inv["accounting_identities"] = _identities(r)
        report["configs"][name] = inv
        del r
    _assert_masked(report)
    return report


def write(report: dict, out: Path) -> None:
    _assert_masked(report)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
