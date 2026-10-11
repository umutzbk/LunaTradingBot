"""Stage 0 audit items that do not need the continuous spread series (outcome-free; no P&L)."""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd
from luna.carry import frozen, parity

FROZEN, SHA, OUT = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
man = frozen.verify_frozen(FROZEN, SHA)
inp = parity.load_parity_inputs(FROZEN, SHA)
CUT = frozen.CUTOFF
lo = pd.Timestamp("2025-10-06T00:00Z")
res = {"holdout": {}, "mark_vs_trade": {}, "coverage_15m": {}}
mx = max(pd.Timestamp(e["last"]) for e in man["files"].values() if e.get("last"))
res["holdout"] = {"max_ts_in_copy": mx.isoformat(), "pass": bool(mx < CUT), "files": len(man["files"])}
all_rel = []
for a in inp.assets:
    p, m = inp.perp[a], inp.mark.get(a)
    p = p[(p.index >= lo) & (p.index < CUT)]
    grid = pd.date_range(lo, CUT, freq="15min", inclusive="left")
    real = p[~p["filled"].astype(bool)]
    mm = m[(m.index >= lo) & (m.index < CUT)] if m is not None else None
    j = real.index.intersection(mm.index) if mm is not None else []
    rel = (mm.loc[j, "close"] / real.loc[j, "close"] - 1).to_numpy() if len(j) else np.array([])
    all_rel.append(rel)
    ar = np.abs(rel)
    res["mark_vs_trade"][a] = {"n": int(len(rel)), "median_abs": float(np.median(ar)) if len(ar) else None,
                              "p99_abs": float(np.quantile(ar, .99)) if len(ar) else None,
                              "max_abs": float(ar.max()) if len(ar) else None}
    res["coverage_15m"][a] = {"trade_real_bars": round(len(real) / len(grid), 4),
                              "mark_bars": round(len(mm) / len(grid), 4) if mm is not None else 0.0}
ar = np.abs(np.concatenate(all_rel))
res["mark_vs_trade"]["ALL"] = {"n": int(len(ar)), "median_abs": float(np.median(ar)), "p95_abs": float(np.quantile(ar, .95)),
                               "p99_abs": float(np.quantile(ar, .99)), "max_abs": float(ar.max())}
OUT.write_text(json.dumps(res, indent=1))
print(json.dumps(res["holdout"]), json.dumps(res["mark_vs_trade"]["ALL"]))
print({a: v for a, v in res["coverage_15m"].items() if v["trade_real_bars"] < 0.99 or v["mark_bars"] < 0.99})
