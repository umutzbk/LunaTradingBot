"""S0-1 / S0-11: dataset types, date ranges, and listing completeness (names/sizes only; W_HOLD = metadata only).

For each dataset and USDT-perp symbol: first/last file date (< 2026-07-01), files and bytes in W_DEV and W_HOLD,
internal holes (missing months/days between a symbol's first and last file, per window), zero-size files.
"""
import json
import sys
from pathlib import Path

import pandas as pd

LIST, OUT = Path(sys.argv[1]), Path(sys.argv[2])


def periods(a, b, daily):
    if daily:
        return [d.strftime("%Y-%m-%d") for d in pd.date_range(a, b, freq="D")]
    return [d.strftime("%Y-%m") for d in pd.date_range(a + "-01" if len(a) == 7 else a, (b + "-01") if len(b) == 7 else b, freq="MS")]


def main():
    out = {}
    for p in sorted(LIST.glob("*.json")):
        d = json.loads(p.read_text())
        daily = d["dataset"].startswith("daily")
        rows = []
        for sym, v in d["symbols"].items():
            if not (sym.endswith("USDT") and "_" not in sym):
                continue
            files = v["files"]
            if not files:
                rows.append({"symbol": sym, "only_post_m6": True})
                continue
            dates = [f[0] for f in files]
            r = {"symbol": sym, "first": dates[0], "last": dates[-1], "post_m6": v["post_m6"],
                 "zero_size": sum(1 for f in files if f[1] == 0)}
            have = set(dates)
            for w, lo, hi in (("dev", "0000", "2025-07"), ("hold", "2025-07", "2026-07")):
                wd = [x for x in dates if lo <= x < hi]
                r[f"{w}_files"] = len(wd)
                r[f"{w}_bytes"] = sum(f[1] for f in files if lo <= f[0] < hi)
                if wd:
                    exp = periods(wd[0], wd[-1], daily)
                    r[f"{w}_holes"] = [x for x in exp if x not in have]
                    # a symbol alive across the boundary: the window should continue to its edge
                    if w == "hold" and v["post_m6"]:
                        edge = "2026-06-30" if daily else "2026-06"
                        r["hold_holes"] += [x for x in periods(wd[-1], edge, daily)[1:] if x not in have]
            rows.append(r)
        df = pd.DataFrame(rows)
        s = {"symbols": int(len(df)), "first": df["first"].dropna().min() if "first" in df else None,
             "last_pre_m6": df["last"].dropna().max() if "last" in df else None}
        for w in ("dev", "hold"):
            if f"{w}_files" in df:
                s[f"{w}_symbols"] = int((df[f"{w}_files"] > 0).sum())
                s[f"{w}_files"] = int(df[f"{w}_files"].sum())
                s[f"{w}_GB"] = round(float(df[f"{w}_bytes"].sum()) / 1e9, 3)
                holes = df[f"{w}_holes"].dropna() if f"{w}_holes" in df else []
                s[f"{w}_symbols_with_holes"] = int(sum(1 for h in holes if h))
                s[f"{w}_hole_count"] = int(sum(len(h) for h in holes))
                s[f"{w}_hole_examples"] = [(r["symbol"], r[f"{w}_holes"][:3]) for _, r in df.iterrows()
                                           if isinstance(r.get(f"{w}_holes"), list) and r[f"{w}_holes"]][:5]
        s["zero_size_files"] = int(df["zero_size"].fillna(0).sum()) if "zero_size" in df else 0
        out[d["dataset"]] = s
        df.to_json(OUT.parent / f"listing_detail_{d['dataset']}.json", orient="records")
    OUT.write_text(json.dumps(out, indent=1))
    for k, s in out.items():
        print(k, {x: s.get(x) for x in ("symbols", "first", "last_pre_m6", "dev_files", "dev_GB", "dev_hole_count",
                                        "hold_symbols", "hold_files", "hold_GB", "hold_hole_count", "zero_size_files")})


if __name__ == "__main__":
    main()
