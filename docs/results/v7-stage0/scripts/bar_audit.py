"""S0-9 / S0-3: summarize per-symbol kline audits (checksums, header/no-header, time units, duplicates, misalignment,
gaps, zero-volume bars) and reconstruct symbol history (first bar, last bar with volume > 0, delisted in W_DEV)."""
import collections
import json
import sys
from pathlib import Path

import pandas as pd

K, OUT, EXINFO = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
W_DEV_END = 1751328000000
ms = lambda x: pd.Timestamp(x, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M") if x else None


def main():
    cur = {s["symbol"]: s for s in json.loads(EXINFO.read_text(encoding="utf-8"))["symbols"]}
    rows, tot, units, hdr = [], collections.Counter(), collections.Counter(), collections.Counter()
    for f in sorted(K.glob("*.audit.json")):
        a = json.loads(f.read_text())
        for x in a["files"]:
            tot["files"] += 1
            tot["checksum_fail"] += not x["checksum_ok"]
            tot["dupes"] += x["dupes"]
            tot["misaligned"] += x["misaligned"]
            tot["non_monotonic"] += x["non_monotonic"]
            units[x["ts_unit"]] += 1
            hdr[x["header"]] += 1
        tot["bars"] += a["bars"]
        tot["missing_bars"] += a["missing_bars"]
        tot["zero_volume_bars"] += a["zero_volume_bars"]
        c = cur.get(a["symbol"])
        last = a["last_bar_volume_gt0"] or a["last_bar"]
        rows.append({"symbol": a["symbol"], "first_bar": ms(a["first_bar"]), "last_bar_vol_gt0": ms(last),
                     "ends_before_wdev_end": a["last_bar"] < W_DEV_END - 7 * 86400_000,
                     "in_current_exchangeInfo": c is not None, "current_status": c["status"] if c else None,
                     "current_underlyingType": c.get("underlyingType") if c else None,
                     "current_contractType": c.get("contractType") if c else None,
                     "missing_bars": a["missing_bars"], "max_gap_bars": a["max_gap_bars"], "n_gaps": a["n_gaps"],
                     "coverage": round(a["bars"] / a["expected_bars_first_to_last"], 5)})
    df = pd.DataFrame(rows)
    delisted = df[df["ends_before_wdev_end"]]
    res = {"totals": dict(tot), "ts_units": dict(units), "header_present": {str(k): v for k, v in hdr.items()},
           "symbols": int(len(df)),
           "delisted_in_wdev": int(len(delisted)),
           "delisted_absent_from_current_exchangeInfo": int((~delisted["in_current_exchangeInfo"]).sum()),
           "delisted_underlyingType_unknown": int(delisted["current_underlyingType"].isna().sum()),
           "coverage_quantiles": df["coverage"].quantile([0, .01, .05, .5]).round(5).to_dict(),
           "worst_gaps": df.sort_values("max_gap_bars", ascending=False).head(10)[["symbol", "max_gap_bars", "n_gaps", "missing_bars"]].values.tolist(),
           "current_type_mix": df.groupby(["current_contractType", "current_underlyingType"], dropna=False).size().reset_index().values.tolist()}
    OUT.write_text(json.dumps(res, indent=1, default=str))
    df.to_csv(OUT.with_suffix(".symbols.csv"), index=False)
    print(json.dumps(res, default=str)[:2500])


if __name__ == "__main__":
    main()
