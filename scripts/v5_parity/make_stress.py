"""Synthetic stress inputs (V4 layout) for the V4 vs W-V4 cross-check of paths the real data never hit:
margin reduction, liquidation with penalty, sign flip in a resize, missing 00:00 Monday bar (fill at
00:30), skipped all-or-nothing action, below-minimum resize. Synthetic only: no market data."""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd
from luna.carry import frozen

out = Path(sys.argv[1]); var = out.with_name(out.name + "_src")
A = ["ADA", "AVAX", "BNB", "BTC", "DOGE"]
start, end = pd.Timestamp("2025-09-01", tz="UTC"), pd.Timestamp("2026-07-01", tz="UTC")
idx = pd.date_range(start, end, freq="15min", inclusive="left")
hidx = pd.date_range(start, end, freq="1h", inclusive="left")
rates = {"ADA": -3e-5, "AVAX": -1e-5, "BNB": 1e-5, "BTC": 3e-5, "DOGE": 0.0}
px = {a: pd.Series(100.0, index=idx) for a in A}
# margin: BTC (short) ramps 100 -> 300 over 2025-11-12 .. 2025-11-16
r = (idx >= "2025-11-12") & (idx < "2025-11-16 23:45")
px["BTC"][r] = np.linspace(100, 300, r.sum()); px["BTC"][idx >= "2025-11-16 23:45"] = 300.0
hi = {a: s.copy() for a, s in px.items()}
# liquidation: BNB (short) intraday high spike on 2025-11-26 10:00
hi["BNB"][idx == pd.Timestamp("2025-11-26 10:00", tz="UTC")] = float(sys.argv[2]) if len(sys.argv) > 2 else 450.0
rel = {a: pd.Series(v, index=hidx) for a, v in rates.items()}
# sign flip: ADA funding jumps from 2025-12-01 -> its FL30 rises above the others
rel["ADA"][hidx >= "2025-12-01"] = 4e-4
for d in ("bars/kraken_futures", "bars/kraken_spot", "history_v4/funding", "history_v4/mark", "history_v4/index", "history_v4/spreads"):
    (var / d).mkdir(parents=True, exist_ok=True)
days = pd.date_range(pd.Timestamp("2025-09-02", tz="UTC"), end, freq="D")
sp_idx = pd.DatetimeIndex(np.concatenate([pd.date_range(d - pd.Timedelta("1h"), d, freq="1min", inclusive="left").tz_localize(None).values for d in days])).tz_localize("UTC")
for a in A:
    filled = np.zeros(len(idx), bool)
    if "nomissing" not in sys.argv:  # missing 00:00 bar on Monday 2025-12-08 for every asset -> fills at 00:30
        filled[idx == pd.Timestamp("2025-12-08 00:00", tz="UTC")] = True
    if a == "AVAX":  # no real bar in [00:00, 01:00) on Monday 2025-12-15 -> whole rebalance skipped
        filled[(idx >= "2025-12-15 00:00") & (idx < "2025-12-15 01:00")] = True
    c = px[a].to_numpy()
    bars = pd.DataFrame({"open": c, "high": np.maximum(c, hi[a].to_numpy()), "low": c, "close": c,
                         "volume": 1e4, "trades": 5, "filled": filled}, index=idx)
    bars.to_parquet(var / "bars/kraken_futures" / f"{a}.parquet")
    bars.to_parquet(var / "bars/kraken_spot" / f"{a}.parquet")
    m = bars[["open", "high", "low", "close"]]
    m.to_parquet(var / "history_v4/mark" / f"{a}.parquet"); m.to_parquet(var / "history_v4/index" / f"{a}.parquet")
    rr = rel[a]
    pxh = px[a].reindex(hidx).to_numpy()
    pd.DataFrame({"funding_abs": rr.to_numpy() * pxh, "funding_rel": rr.to_numpy()}, index=hidx).to_parquet(var / "history_v4/funding" / f"{a}.parquet")
    pd.DataFrame({"spread_full": 0.0004 + 0.0001 * A.index(a)}, index=sp_idx).to_parquet(var / "history_v4/spreads" / f"{a}.parquet")
inst = {"assets": {a: {"perp": {"step": 0.001, "min_qty": 0.001, "min_cost": 5.0 if a == "AVAX" else 0.0, "tick": 0.01}} for a in A}}
(var / "inst.json").write_text(json.dumps(inst))
m = frozen.freeze_inputs(var, var / "inst.json", out, assets=A)
print(frozen.sha256_file(out / "manifest.json"))
