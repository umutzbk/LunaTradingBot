"""S0-2: funding completeness (REST vs archive), interval distribution, timestamp units, mark-at-settle, gaps.

Record-level metadata/availability only; the funding rates are compared for equality between sources, never
aggregated into returns.
"""
import collections
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

V = Path(sys.argv[1])
OUT = Path(sys.argv[2])
H = 3600_000


def main():
    per, iv_counter, spacing_counter = {}, collections.Counter(), collections.Counter()
    tot = collections.Counter()
    for fr in sorted((V / "funding_rest").glob("*.csv")):
        sym = fr.stem
        r = pd.read_csv(fr, dtype={"fundingRate": str, "markPrice": str})
        a_path = V / "funding_archive" / f"{sym}.csv"
        a = pd.read_csv(a_path, dtype={"last_funding_rate": str}) if a_path.exists() else pd.DataFrame()
        rt = r["fundingTime"].to_numpy(np.int64)
        info = {"rest_n": int(len(r)), "archive_n": int(len(a))}
        tot["rest"] += len(r)
        tot["archive"] += len(a)
        if len(r):
            info["rest_first"] = int(rt.min())
            info["rest_last"] = int(rt.max())
            info["ts_unit"] = "ms" if rt.max() < 1e14 else "us"
            info["dupes"] = int(pd.Series(rt).duplicated().sum())
            r = r.drop_duplicates("fundingTime")
            rt = r["fundingTime"].to_numpy(np.int64)
            off = rt % H
            off = np.minimum(off, H - off)
            info["max_offset_from_hour_ms"] = int(off.max())
            tot["offset_gt_1s"] += int((off > 1000).sum())
            mp = r["markPrice"].fillna("")
            has_mp = (mp != "") & (mp != "0") & (mp != "0.00000000")
            info["markPrice_present"] = int(has_mp.sum())
            info["markPrice_first"] = int(rt[has_mp.to_numpy()].min()) if has_mp.any() else None
            tot["markPrice_present"] += int(has_mp.sum())
            sp = np.diff(np.sort(rt))
            hrs = np.round(sp / H, 2)
            spacing_counter.update(hrs.tolist())
        if len(a):
            info["archive_dupes"] = int(a.iloc[:, 0].duplicated().sum())
            a = a.drop_duplicates(a.columns[0])
            at = a.iloc[:, 0].to_numpy(np.int64)
            ai = a.iloc[:, 1].to_numpy()
            iv_counter.update(pd.Series(ai).astype(str).tolist())
            info["intervals"] = sorted(set(pd.Series(ai).astype(str)))
            # match by time within 1 s
            if len(r):
                rs = np.sort(rt)
                j = np.clip(np.searchsorted(rs, at), 0, len(rs) - 1)
                j2 = np.clip(j - 1, 0, len(rs) - 1)
                near = np.minimum(np.abs(rs[j] - at), np.abs(rs[j2] - at))
                info["archive_not_in_rest"] = int((near > 1000).sum())
                ats = np.sort(at)
                k = np.clip(np.searchsorted(ats, rs), 0, len(ats) - 1)
                k2 = np.clip(k - 1, 0, len(ats) - 1)
                near2 = np.minimum(np.abs(ats[k] - rs), np.abs(ats[k2] - rs))
                info["rest_not_in_archive"] = int((near2 > 1000).sum())
                # rate equality on matched records
                m = pd.merge_asof(pd.DataFrame({"t": rs}).assign(rr=r.set_index("fundingTime").loc[rs, "fundingRate"].to_numpy()),
                                  pd.DataFrame({"t": ats, "ar": a.set_index(a.columns[0]).loc[ats, a.columns[2]].to_numpy()}),
                                  on="t", direction="nearest", tolerance=1000).dropna()
                info["matched"] = int(len(m))
                info["rate_mismatch"] = int((m["rr"].astype(float) != m["ar"].astype(float)).sum())
                # gaps vs nominal interval (from the archive interval of the later record)
                pick = np.where(np.abs(ats[k] - rs) <= np.abs(ats[k2] - rs), k, k2)
                iv = pd.Series(a.set_index(a.columns[0]).loc[ats, a.columns[1]].to_numpy()).to_numpy(float)
                nom = np.where(near2 <= 60_000, iv[pick], np.nan)[1:] * H
                sp = np.diff(rs)
                gap = sp > nom + 60_000
                info["gaps_gt_interval"] = int(np.nansum(gap))
                info["max_gap_h"] = float(np.round(sp.max() / H, 2)) if len(sp) else 0.0
                info["unknown_interval_records"] = int(np.isnan(nom).sum())
                for k_ in ("archive_not_in_rest", "rest_not_in_archive", "rate_mismatch", "gaps_gt_interval", "matched",
                           "unknown_interval_records"):
                    tot[k_] += info[k_]
        per[sym] = info
    res = {"totals": dict(tot), "symbols": len(per), "interval_hours_distribution": dict(iv_counter),
           "rest_spacing_hours_top": spacing_counter.most_common(15),
           "symbols_with_gaps": sorted([s for s, i in per.items() if i.get("gaps_gt_interval")]),
           "symbols_rest_empty": sorted([s for s, i in per.items() if i["rest_n"] == 0]),
           "symbols_archive_missing": sorted([s for s, i in per.items() if i["archive_n"] == 0]),
           "per_symbol": per}
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "per_symbol"}, default=str)[:3000])


if __name__ == "__main__":
    main()
