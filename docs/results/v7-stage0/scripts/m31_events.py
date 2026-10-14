"""M3-1 step 1 (outcome-free, W_DEV): eligible contract terminations.
An event = an activity instance that ends (S-2.2: >= 7 UTC days without a volume>0 15m bar follow its last traded bar)
before 2025-07-01, for which at least one decision of the RT-5 v2 population (G_D0 ∧ instance-U1 ∧ U8) lies within
168 h (the longest horizon) before its last traded bar. Uses volume>0 flags and eligibility bits only."""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.argv = [sys.argv[0], sys.argv[1], sys.argv[2]]
sys.path.insert(0, str(Path(__file__).parent))
import rt5_v2_dec35 as r2
V = r2.V; MIN, H, D, W_END = r2.MIN, r2.H, r2.D, r2.W_END
z = np.load(V / "summary/elig_wdev.elig.npz"); dts = z["dts"]
ev = []
for sym in [k for k in z.files if k != "dts" and k not in r2.v1.UNKNOWN]:
    e = np.unpackbits(z[sym])[:len(dts)].astype(bool)
    if not e.any():
        continue
    inst, t15, act15 = r2.instances(sym)
    d = dts[e]
    lo = np.searchsorted(act15, d - 75 * MIN, "left"); hi = np.searchsorted(act15, d - 16 * MIN, "right")
    u8 = (hi - lo) > 0
    for k, (a, b) in enumerate(inst):
        if b is None or b >= W_END:
            continue
        last_open = b - 15 * MIN
        # PIT instance membership (M3-2): live until end_known_at = 00:00 UTC after the 7th inactive day
        end_known = ((last_open // D) + 8) * D
        u1 = (a <= d - 60 * D)
        inwin = (d >= b - 168 * H - 15 * MIN) & (d < end_known) & (d >= a)
        ok = inwin & u1 & u8
        if ok.any():
            ev.append({"symbol": sym, "instance": k, "instance_start": pd.Timestamp(a, unit="ms").isoformat(),
                       "last_trade_bar_close": pd.Timestamp(b, unit="ms").isoformat(),
                       "end_known_at": pd.Timestamp(end_known, unit="ms").isoformat(),
                       "resumed_as_new_instance": k + 1 < len(inst),
                       "eligible_decisions_within_168h": int(ok.sum()),
                       "last_eligible_decision": pd.Timestamp(int(d[ok].max()), unit="ms").isoformat(),
                       "hours_last_decision_to_last_trade_close": round((b - int(d[ok].max())) / H, 2)})
(Path(sys.argv[2]) / "events.json").write_text(json.dumps(ev, indent=1))
print(len(ev)); [print(x["symbol"], x["instance"], x["last_trade_bar_close"], x["last_eligible_decision"], x["hours_last_decision_to_last_trade_close"]) for x in sorted(ev, key=lambda x: x["last_trade_bar_close"])]
