"""Layer A (protocol §5.2, §5.4): W-V4 replay on the frozen copy vs the V4 reference reproduction."""
import json, math, sys
from pathlib import Path
import pandas as pd
from luna.carry import parity

FROZEN, MAN_SHA, REF, OUT = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
OUT.mkdir(parents=True, exist_ok=True)
inp = parity.load_parity_inputs(FROZEN, MAN_SHA)
days = list(pd.date_range("2025-11-05", "2026-06-30", freq="D", tz="UTC"))
rows = {"PRIMARY": (0.0005, 1.0), "S-COST2X": (0.001, 2.0)}
ref_fills = json.loads((REF / "fills.json").read_text())
ref_sum = json.loads((REF / "reference_summary.json").read_text())
report = {}
TOL = {"funding_cf": 1e-9, "fees": 1e-9, "spread_slippage": 1e-9, "perp_price_pnl": 1e-9,
       "liquidation_penalty": 1e-9, "ret": 1e-10, "carry_net": 1e-10, "equity": 1e-6}
for name, (fee, mult) in rows.items():
    res = parity.run_w_v4(inp, days, fee_rate=fee, spread_mult=mult)
    res.daily.to_parquet(OUT / f"wv4__{name}.daily.parquet")
    (OUT / f"wv4__{name}.fills.json").write_text(json.dumps(res.fills))
    (OUT / f"wv4__{name}.decisions.json").write_text(json.dumps(res.decisions))
    ref = pd.read_parquet(REF / f"H2-L30-k2__{name}.daily.parquet")
    ref.index = pd.DatetimeIndex(ref.index)
    if ref.index.tz is None:
        ref.index = ref.index.tz_localize("UTC")
    d = parity.compare_daily(ref, res.daily, TOL)
    held = (ref["held"].fillna("").astype(str) != res.daily["held"].fillna("").astype(str))
    rf = [f for f in ref_fills if f["row"] == name]
    fl = parity.compare_fills(rf, res.fills, 1e-9, numeric=("dq", "px", "half_spread"))
    notional = parity.compare_fills(
        [{**f, "notional": abs(f["dq"]) * f["px"]} for f in rf],
        [{**f, "notional": abs(f["dq"]) * f["px"]} for f in res.fills], 1e-9, numeric=("notional",))
    rs = ref_sum["rows"][name]
    cum = {
        "final_equity": (float(res.daily["equity"].iloc[-1]), rs["final_equity"], 1e-6),
        "mean_daily": (float(res.daily["ret"].mean()), rs["mean_daily"], 1e-9),
        "sharpe": (parity.sharpe(res.daily["ret"]), rs["sharpe"], 1e-9),
        "max_drawdown": (parity.max_drawdown(res.daily["ret"]), rs["max_drawdown"], 1e-9),
    }
    cum = {k: {"actual": a, "expected": e, "delta": a - e, "tol": t, "pass": abs(a - e) <= t} for k, (a, e, t) in cum.items()}
    ctr_keys = ["decisions", "flat_weeks", "skipped_actions", "min_order_skips", "perp_spread_fallbacks",
                "margin_reductions", "liquidations", "valuation_fallbacks", "funding_unknown_hours",
                "funding_held_hours", "forced_closes", "margin_check_skipped"]
    ctr = {k: {"actual": res.counters.get(k), "expected": rs["counters"].get(k),
               "pass": abs(res.counters.get(k, 0) - rs["counters"].get(k, 0)) <= (1e-9 if "hours" in k else 0)} for k in ctr_keys}
    report[name] = {"daily": d, "held_days_mismatch": int(held.sum()),
                    "first_held_mismatch": str(held[held].index[0]) if held.any() else None,
                    "fills": fl, "notionals": notional, "cumulative": cum, "counters": ctr,
                    "pass": d["pass"] and not held.any() and fl["pass"] and notional["pass"]
                    and all(v["pass"] for v in cum.values()) and all(v["pass"] for v in ctr.values())}
    print(name, "PASS" if report[name]["pass"] else "FAIL",
          {c: f'{v["max_abs_delta"]:.2e}' for c, v in d["by_component"].items()},
          "fills", fl["pass"], len(rf), len(res.fills), f'{fl["max_rel_delta"]:.1e}',
          "held_mismatch", int(held.sum()), "cum", {k: v["pass"] for k, v in cum.items()},
          "ctr_fail", [k for k, v in ctr.items() if not v["pass"]])
    if not report[name]["pass"]:
        print(json.dumps({"daily_first": d["first"], "fills_first": fl["first"]}, default=str, indent=1)[:3000])
(OUT / "layer_a_report.json").write_text(json.dumps(report, indent=1, default=str))
