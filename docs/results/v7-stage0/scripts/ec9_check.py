"""EC-9 split (outcome-free): of the RT-5 v3 unreconciled funding records, which are DEC-29 regime transitions
(no explicit field; spacing within 60 s of a standard 1/2/4/8 h interval; interval_transition = true, valid) and which
are genuinely unreconciled (invalid)? Uses only funding timestamps / interval fields and 15m volume flags."""
import glob, json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, r"C:/Users/umutzbk/Documents/LunaTradingBot-v7/docs/results/v7-stage0/scripts")
import rt5_v3_r3 as r3
H, MIN = r3.H, r3.MIN
R = r3.V / "rt5v3"
out = []
for f in sorted(glob.glob(str(R / "*.json"))):
    s = json.loads(Path(f).read_text())
    if not isinstance(s, dict) or "unreconciled_records" not in s or not s["unreconciled_records"]:
        continue
    sym = s["symbol"]
    ts, iv, expl, has_mp, rest_fill = r3.funding_v2(sym)
    inst, _, _ = r3.instances(sym)
    ok, ii, nom = r3.reconciled(ts, expl, iv, inst)
    for i in np.where(~ok)[0]:
        first = i == 0 or ii[i] != ii[i - 1]
        if first:
            kind = "first record of instance, no explicit interval (undetermined)"
        else:
            sp = ts[i] - ts[i - 1]
            std = min((1, 2, 4, 8), key=lambda c: abs(c * H - sp))
            if np.isnan(expl[i]) and abs(std * H - sp) <= MIN:
                kind = "transition"
            else:
                kind = "genuinely unreconciled"
        out.append({"symbol": sym, "funding_ts": pd.Timestamp(int(ts[i]), unit="ms").isoformat(), "instance": int(ii[i]),
                    "spacing_h": None if first else round((ts[i] - ts[i - 1]) / H, 4),
                    "prev_nominal_h": None if first else (None if np.isnan(nom[i - 1]) else float(nom[i - 1])),
                    "explicit_h": None if np.isnan(expl[i]) else float(expl[i]), "kind": kind})
from collections import Counter
print(len(out), Counter(o["kind"] for o in out))
for o in out:
    print(o)
Path(sys.argv[2], "ec9_records.json").write_text(json.dumps(out, indent=1))
