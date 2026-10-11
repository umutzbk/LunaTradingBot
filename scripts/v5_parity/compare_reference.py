"""Compare the V4 reference reproduction with the committed V4 Stage 1 summary (protocol §5.1 gate)."""
import json, math, sys
from pathlib import Path

ref = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
pub = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))["configs"]["H2-L30-k2"]
checks = []
def chk(name, a, b, tol=0.0):
    d = abs(a - b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else (0.0 if a == b else math.inf)
    checks.append({"item": name, "reference": a, "published": b, "abs_delta": d, "tol": tol, "pass": d <= tol})
m = ref["metrics"]
for k in ("mean_daily", "sharpe", "max_drawdown", "worst_day", "es975", "cost2x_mean_daily"):
    chk(k, m[k], pub["metrics"][k], 1e-12)
for k in ("ub95_b28", "lb95_b28", "lb95_b14", "carry_net_mean", "mean_daily"):
    chk("stage1." + k, ref["stage1"][k], pub["stage1"][k], 1e-12)
chk("stage1.decision", ref["stage1"]["decision"], pub["stage1"]["decision"])
for row in ("PRIMARY", "S-COST2X"):
    r, p = ref["rows"][row], pub["rows"][row]
    for k in ("final_equity", "mean_daily", "sharpe", "max_drawdown"):
        chk(f"{row}.{k}", r[k], p[k], 1e-9 if k == "final_equity" else 1e-12)
    for k, v in p["pnl"].items():
        chk(f"{row}.pnl.{k}", r["pnl"][k], v, 1e-9)
    for k, v in p["counters"].items():
        chk(f"{row}.counters.{k}", r["counters"][k], float(v))
    chk(f"{row}.entries", r["entries"], p["entries"])
chk("days", ref["days"], pub["metrics"]["days"])
out = {"gate": "PASS" if all(c["pass"] for c in checks) else "FAIL", "n": len(checks),
       "failed": [c for c in checks if not c["pass"]], "max_abs_delta": max(c["abs_delta"] for c in checks), "checks": checks}
Path(sys.argv[3]).write_text(json.dumps(out, indent=1), encoding="utf-8")
print(out["gate"], out["n"], "checks; max abs delta", out["max_abs_delta"], "failed", len(out["failed"]))
