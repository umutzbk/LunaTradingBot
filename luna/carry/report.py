"""V5 backtest outputs (frozen protocol rev 3, §12): ``summary.json`` (sha256 pinned in
``manifest.json``), ``manifest.json`` (frozen input / protocol / commit), ``report.md``."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from luna.carry import stats
from luna.carry.frozen import sha256_file

LABEL = "EXPLORATORY — reused development period"
MDE_A_PRIORI = {"b28": 6.6e-5, "b14": 6.3e-5, "note": "lower bound, W-side distribution only (protocol §5.4)"}
TABLES = ("daily", "fills", "records", "settlements", "schedule")

DECLARATIONS = [
    "Depth: no historical order book; the builder uses infinite book sizes, so the depth gate passes (§3.1).",
    "Instruments: frozen V4 file (qty step, tick, min qty, min cost); maintenance margin is not in that file "
    "and is 0, so the liquidation-distance pre-check (equity / leg) effectively always passes.",
    "S-NOKILL: R4 disabled by setting risk.max_drawdown = 1.0 (it can only fire at equity <= 0); leg stop and "
    "R3 unchanged.",
    "G6 when KILL-AFFECTED: read from an S-COST2X/NOKILL row (S-COST2X with the R4 kill off), because §10 moves "
    "every guardrail to the kill-off rows; it is not a trial (§7).",
    "G6 for the S-PIT10 and S-FB-CONS row verdicts: the same S-COST2X delta-carry as the base row.",
    "FALLBACK-AFFECTED: fallback shares from the base row's fills (S-NOKILL when KILL-AFFECTED).",
    "P-C8 'interval 8 h': scheduled slots at 00:00, 08:00, 16:00 UTC with the §3.2 completion / 4-attempt rule "
    "(the paper Trigger's 'since the last rebalance' is not used in replay).",
    "Valid snapshot for the 4-attempt counter: at least one asset has a quote at t.",
    "Terminal unwind (S-UNWIND): |q| x mark x row cost multiplier x (fee + decision-window half spread at the end "
    "+ 2 bps slip).",
    "Secondary comparisons: Holm over D-W, C-W, E-C separately for carry and net and for b = 28 and b = 14, on "
    "the bootstrap one-sided p-values.",
    "Switch ledger: hit = realized incremental funding over the actual holding > realized cost (fees + spread/slip "
    "of the new leg's fills); bias = expected incremental scaled to the holding duration - realized.",
    "Fallback hour-of-day: the hour of the decision that sent the order.",
    "Outcome authorization: a guard against accidental runs inside this codebase (tag, clean tree, protocol and "
    "manifest sha, frozen config, approval phrase, exclusive markers), not a security boundary.",
]


def _clean(x):
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (pd.Timestamp,)):
        return x.isoformat()
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return x


def _metrics(d: pd.DataFrame, e0: float, counters: dict, ideal_funding: float | None) -> dict:
    net = float(d["equity"].iloc[-1] - e0)
    out = {
        "net_pnl": net, "net_return": net / e0, "funding": float(d["funding"].sum()),
        "fees": float(d["fees"].sum()), "spread_slip": float(d["spread_slip"].sum()),
        "price": float(d["price"].sum()), "delay_drift": float(d["delay_drift"].sum()),
        "carry_net_mean": float(d["carry_net"].mean()), "turnover": float(d["turnover"].sum()),
        "sharpe": stats.sharpe(d["ret"]), "max_drawdown": stats.max_drawdown(d["ret"]),
        "worst_day": stats.worst_day(d["ret"]), "es975": stats.es975(d["ret"]),
        "mean_gross_frac": float(d["gross_frac"].mean()), "mean_net_frac": float(d["net_frac"].mean()),
        "flat_days": int((d["n_legs"] == 0).sum()), "counters": counters,
        "decisions": counters.get("decisions", {}), "stops": counters.get("stops", 0),
    }
    meq = float(d["equity"].mean())
    out["turnover_per_year_over_mean_equity"] = out["turnover"] / meq * 365 / len(d) if meq > 0 and len(d) else None
    turn = out["turnover"]
    out["carry_per_turnover_bps"] = {"funding": out["funding"] / turn * 1e4 if turn else None,
                                     "carry_net": (out["funding"] - out["fees"] - out["spread_slip"]) / turn * 1e4
                                     if turn else None}
    out["capture_ratio"] = out["funding"] / ideal_funding if ideal_funding else None
    return out


def _switch_summary(df: pd.DataFrame) -> dict:
    if df is None or not len(df):
        return {"n": 0}
    hit = (df["realized_incremental"] > df["realized_cost"]).mean()
    return {"n": int(len(df)), "hit_rate": float(hit),
            "bias_expected_minus_realized": float((df["expected_over_hold"] - df["realized_incremental"]).mean()),
            "by_decision": df.groupby("decision").size().to_dict()}


def holding_hours(fills: pd.DataFrame, end: pd.Timestamp) -> dict:
    if not len(fills):
        return {}
    opens = fills[fills["intent"] == "open"].groupby("position_id")["ts"].min()
    closes = fills[fills["intent"] == "close"].groupby("position_id")["ts"].max()
    h = ((closes.reindex(opens.index).fillna(end) - opens) / pd.Timedelta("1h")).astype(float)
    return {"median": float(h.median()), "mean": float(h.mean()), "p10": float(h.quantile(0.1)),
            "p90": float(h.quantile(0.9)), "n": int(len(h))}


def write_report(study: dict, bridge: list | None, out_dir: Path, meta: dict) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res, ev = study["results"], study["evaluation"]
    start, end = study["window"]
    first = next(iter(res.values()))
    e0 = float(first.daily["equity"].iloc[0] / (1 + first.daily["ret"].iloc[0]))
    ideal = study.get("ideal")
    i_fund = float(ideal["funding"].sum()) if ideal is not None else None
    arms: dict = {}
    for (arm, row), r in res.items():
        arms.setdefault(arm, {})[row] = {**_metrics(r.daily, e0, r.counters, i_fund),
                                         "kill_ts": r.kill_ts, "holding_hours": holding_hours(r.fills, end),
                                         "scheduled": {"retries": r.counters.get("scheduled_retries"),
                                                       "exhausted": r.counters.get("scheduled_exhausted"),
                                                       "late_1h": r.counters.get("scheduled_late_1h")}}
    for arm, d in study["unwind"].items():
        arms[arm]["S-UNWIND"] = _metrics(d, e0, {}, i_fund)
    summary = {
        "label": LABEL, "window": [start, end], "meta": meta,
        "verdict": {"final": ev["verdict"], "tags": ev["tags"], "rows_used": ev["rows_used"],
                    "rows": {k: {"verdict": v["verdict"], "delta_carry": v["delta_carry"],
                                 "broken_guardrails": v["broken"]} for k, v in ev["row_verdicts"].items()}},
        "kill": ev["kill"], "fallback_exposure": ev["fallback_exposure"], "fallback_affected": ev["fallback_affected"],
        "fallback_diffs_vs_W": ev["fallback_diffs_vs_W"], "cost2x_mean_delta_carry": ev["cost2x_mean_delta_carry"],
        "secondary": ev["secondary"], "holm": ev["holm"], "delta_net_ci": ev["delta_net_ci"],
        "mde": {"a_priori": MDE_A_PRIORI, "post_hoc_b28": ev["mde_post_hoc_b28"], "post_hoc_label": "POST HOC"},
        "arms": arms, "attribution": study["attribution"],
        "switch_ledger": {a: _switch_summary(df) for a, df in study["switch_ledgers"].items()},
        "ideal_funding": i_fund,
        "bridge": [{"step": name, "final_equity": float(r.daily["equity"].iloc[-1]),
                    "carry_net_mean": float(r.daily["carry_net"].mean()), "counters": r.counters}
                   for name, r in bridge] if bridge else None,
    }
    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_clean(summary), indent=1, ensure_ascii=False), encoding="utf-8")
    tables = write_tables(res, out_dir / "tables")
    manifest = {**meta, "summary_sha256": sha256_file(sp), "tables": tables}
    (out_dir / "manifest.json").write_text(json.dumps(_clean(manifest), indent=1), encoding="utf-8")
    lines = [
        "# V5 historical backtest", "", f"**{LABEL}**", "",
        f"- Window: {start.isoformat()} → {end.isoformat()}",
        f"- Verdict: **{ev['verdict']}** {' '.join(ev['tags'])}", f"- Rows used: {', '.join(ev['rows_used'])}",
        f"- summary.json sha256: `{manifest['summary_sha256']}`", "",
        "| Arm | Row | Net | Funding | Fees | Spread/slip | Price | carry_net mean | Sharpe | MaxDD |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in ("W", "D", "C", "E"):
        for row, m in arms[arm].items():
            lines.append(f"| {arm} | {row} | {m['net_pnl']:.4f} | {m['funding']:.4f} | {m['fees']:.4f} | "
                         f"{m['spread_slip']:.4f} | {m['price']:.4f} | {m['carry_net_mean']:.3e} | "
                         f"{m['sharpe']:.3f} | {m['max_drawdown']:.4f} |")
    lines += ["", "## Declared interpretations", ""] + [f"- {x}" for x in DECLARATIONS]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def _table(r, kind: str) -> pd.DataFrame:
    if kind == "daily":
        return r.daily.reset_index()
    if kind == "schedule":
        return pd.DataFrame(r.schedule)
    df = getattr(r, kind).copy()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v))
    return df


def write_tables(res: dict, root: Path) -> dict:
    """Every arm x row table (the single run cannot be repeated, R9), sha256-pinned in the manifest."""
    root.mkdir(parents=True, exist_ok=True)
    out = {}
    for (arm, row), r in res.items():
        for kind in TABLES:
            df = _table(r, kind)
            if kind == "schedule":
                for c in df.columns:
                    if df[c].dtype == object:
                        df[c] = df[c].map(str)
            name = f"{arm}_{row.replace('/', '_')}_{kind}.parquet"
            df.to_parquet(root / name)
            out[f"{arm}:{row}:{kind}"] = {"path": f"tables/{name}", "sha256": sha256_file(root / name)}
    return out


def assemble_bridge(forward: list, reverse: list, w) -> list:
    """§5.5 table: steps 1-4 (forward and reverse order), each ending with the engine change -> W, so
    the last value of either order is W itself."""
    return ([(f"forward:{n}", r) for n, r in forward] + [("forward:engine->W", w)]
            + [(f"reverse:{n}", r) for n, r in reverse] + [("reverse:engine->W", w)])
