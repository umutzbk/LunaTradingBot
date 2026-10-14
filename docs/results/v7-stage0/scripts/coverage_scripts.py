"""Reproduces the Stage-0 coverage numbers that were first computed inline (red-team reproducibility finding).

usage: python -I coverage_scripts.py <var/v7/stage0>
F7 (bookDepth) / F15 (metrics) / R6 (bookTicker) file-level coverage over G_D0-eligible W_DEV symbol-decisions
(elig grid from d0_eval.py run 2021-01..2025-07 -> summary/elig_wdev.elig.npz), and delisting-notice coverage.
Availability only: no label, return or spread statistic is computed.
"""
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

V = Path(sys.argv[1])
D = 86400000
z = np.load(V / "summary/elig_wdev.elig.npz")
dts = z["dts"]
syms = [k for k in z.files if k != "dts"]
elig = {k: np.unpackbits(z[k])[:len(dts)].astype(bool) for k in syms}
day = lambda a: pd.to_datetime(a, unit="ms").strftime("%Y-%m-%d").to_numpy()


def file_days(ds):
    d = json.loads((V / f"listings/{ds}.json").read_text())["symbols"]
    return {s: {x[0] for x in v["files"]} for s, v in d.items()}


def coverage(sets_a, a, sets_b, b):
    cand = cov = 0
    dec_all = np.ones(len(dts), bool)
    dec_any = np.zeros(len(dts), bool)
    for k, e in elig.items():
        sa, sb = sets_a.get(k, set()), sets_b.get(k, set())
        c = np.array([(x in sa) and (y in sb) for x, y in zip(a, b)])
        cand += e.sum()
        cov += (e & c).sum()
        dec_all &= ~e | c
        dec_any |= e & c
    return {"candidate_level": round(cov / cand, 4), "decision_level_all": round(dec_all.mean(), 4),
            "decision_level_any": round(dec_any.mean(), 4), "eligible_symbol_decisions": int(cand)}


def main():
    res = {}
    dep, met = file_days("daily_bookDepth"), file_days("daily_metrics")
    res["F7_bookDepth"] = coverage(dep, day(dts - D), dep, day(dts - 60000))
    res["F15_metrics"] = coverage(met, day(dts - 7 * D), met, day(dts - 60000))
    m = json.loads((V / "listings/monthly_bookTicker.json").read_text())["symbols"]
    dl = file_days("daily_bookTicker")
    def month_days(ym):
        return set(pd.date_range(ym + "-01", periods=pd.Period(ym).days_in_month, freq="D").strftime("%Y-%m-%d"))
    either = {s: set().union(*[month_days(f[0]) for f in m.get(s, {}).get("files", [])]) | dl.get(s, set()) for s in elig}
    dd = day(dts - 60000)
    res["R6_bookTicker"] = coverage(either, dd, either, dd)  # a monthly or daily file covers the decision day
    # delisting notices (W_DEV-dated) vs symbols whose last traded bar is in W_DEV
    h = pd.read_csv(V / "summary/symbol_trading_history.csv")
    ann = [a for a in json.loads((V / "delist/futures_delist_announcements.json").read_text(encoding="utf-8")) if "sha256" in a]
    texts = [(a["releaseDate"], a["title"] + " " + (V / f"delist/bodies/{a['code']}.json").read_text(encoding="utf-8")) for a in ann]
    rows = []
    for _, r in h[h.delisted_in_wdev].iterrows():
        s, b = r.s, r.s[:-4]
        hit = [t for t, x in texts if t <= r.last_trade and (re.search(rf"\b{s}\b", x) or re.search(
            rf"\b{re.escape(b)}\b(?=[^A-Za-z0-9]{{0,40}}(USDT|USDⓈ|Perpetual|and|,))", x))]
        rows.append({"symbol": s, "last_trade": r.last_trade, "first_notice": min(hit) if hit else None})
    df = pd.DataFrame(rows)
    res["delisting"] = {"delisted_in_wdev": len(df), "matched_any_notice": int(df.first_notice.notna().sum()),
                        "note": "naive token match; includes mismatches/postponements (red team M-2)"}
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
