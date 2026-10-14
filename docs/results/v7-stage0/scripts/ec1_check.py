"""EC-1 impact check (outcome-free): which W_DEV decisions change point-in-time instance membership when
instance_end_known_at = last close + 7*24h (EC-1, canonical) instead of 00:00 after the 7th inactive day (rt5 v3)?
Reads only 15m bar presence/volume flags and the Stage-0 eligibility grid."""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, r"C:/Users/umutzbk/Documents/LunaTradingBot-v7/docs/results/v7-stage0/scripts")
import rt5_v3_r3 as r3
D, MIN = r3.D, r3.MIN
z = np.load(r3.V / "summary/elig_wdev.elig.npz"); dts = z["dts"]
tot_aff = 0; aff_rows = []
for k in [k for k in z.files if k != "dts"]:
    e = np.unpackbits(z[k])[:len(dts)].astype(bool)
    if not e.any():
        continue
    inst, t15, act15 = r3.instances(k)
    for a, b in inst:
        if b is None:
            continue
        lo, hi = b + 7 * D, r3.end_known(b)
        assert lo <= hi, (k, b)
        m = (dts >= lo) & (dts < hi)
        # U8 at those decisions (vol>0 15m bar closing in [t-60, t-1])
        for j in np.where(m)[0]:
            t = int(dts[j])
            u8 = np.searchsorted(act15, t - 16 * MIN, "right") - np.searchsorted(act15, t - 75 * MIN, "left") > 0
            aff_rows.append((k, pd.Timestamp(t, unit="ms").isoformat(), bool(e[j]), bool(u8)))
print("decisions in [close+7d, 00:00-form) for some ended instance:", len(aff_rows))
print("  of which on the G_D0 grid (affect D0 count):", sum(r[2] for r in aff_rows))
print("  of which U8 true (could affect the label population):", sum(r[3] for r in aff_rows))
for r in aff_rows:
    if r[2] or r[3]:
        print("  ", r)
