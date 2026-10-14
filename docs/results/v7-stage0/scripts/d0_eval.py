"""S0-7: evaluate the frozen S-1.8 D0 rule (G_D0 = U1 ∧ U2 ∧ U3 ∧ U4 >= 30 at every decision of a month).

Inputs: availability/metadata only (bar presence, daily quote volume, funding record timestamps/intervals).
No label, return, IC or P&L is computed. Months evaluated: 2021-01 .. 2022-12 (D0 is the first passing month).
Interpretation variants (reported, primary marked):
  U2 missing-day: 'zero' (primary: a UTC day with no bars has 0 quote volume) | 'skip' (median over days with bars)
  U3 gap tolerance: spacing <= nominal interval + 60 s (primary) | <= 2 x nominal interval
  universe: unknown-underlying archive-only symbols included (primary: excluded only if known non-COIN) | excluded
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

V = Path(sys.argv[1])
OUT = Path(sys.argv[2])
H = 3600_000
D = 24 * H
STABLE = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE"}


def classify():
    """underlyingType per symbol from current exchangeInfo and W_DEV-dated archived exchangeInfo snapshots."""
    import gzip
    ut, base = {}, {}
    srcs = [V / "meta/exchangeInfo.json"] + sorted((V / "meta/wayback").glob("exchangeInfo_*.json"))
    for p in srcs:
        b = p.read_bytes()
        if b[:2] == b"\x1f\x8b":
            b = gzip.decompress(b)
        for s in json.loads(b)["symbols"]:
            if s.get("underlyingType"):
                ut.setdefault(s["symbol"], set()).add((s.get("contractType"), s["underlyingType"]))
            base.setdefault(s["symbol"], s.get("baseAsset"))
    return ut, base


def decisions(m0, m1):
    days = pd.date_range(m0, m1, freq="D", inclusive="left", tz="UTC")
    return [int((d + pd.Timedelta(hours=h, minutes=5)).value // 1_000_000) for d in days for h in (0, 8, 16)]


def main():
    ut, base = classify()
    syms = sorted(p.stem for p in (V / "k15_last").glob("*.parquet"))
    months = pd.date_range(sys.argv[3] if len(sys.argv) > 3 else "2021-01-01", sys.argv[4] if len(sys.argv) > 4 else "2023-01-01", freq="MS", tz="UTC")
    dts = np.array(decisions(months[0], months[-1]), dtype=np.int64)
    var_names = ["primary", "u2_skip", "u3_2x", "excl_unknown"]
    counts = {v: np.zeros(len(dts), dtype=np.int32) for v in var_names}
    sym_info = {}
    elig = {}
    for sym in syms:
        b = base.get(sym) or sym[:-4]
        types = ut.get(sym)
        known_noncoin = bool(types) and not any(c == "PERPETUAL" and u == "COIN" for c, u in types)
        unknown = not types
        if sym == "BTCUSDT" or b in STABLE or sym[:-4] in STABLE or known_noncoin:
            sym_info[sym] = "excluded_S-2.1"
            continue
        k = pd.read_parquet(V / "k15_last" / f"{sym}.parquet", columns=["open_time", "quote_volume"])
        t = k["open_time"].to_numpy(np.int64)
        if len(t) == 0 or t[0] > dts[-1]:
            sym_info[sym] = "no_bars_in_eval"
            continue
        first_bar = t[0]
        # U4: number of present bars with open in [T0 - 24h, T0), T0 = the hour start of the decision (hh:00)
        T0 = dts - 5 * 60_000
        n_present = np.searchsorted(t, T0, "left") - np.searchsorted(t, T0 - D, "left")
        u4 = n_present >= 92  # >= 95 % of 96
        # U2: daily quote volume, 30 UTC days fully ended before t
        day = t // D
        dq = pd.Series(k["quote_volume"].to_numpy(np.float64)).groupby(day).sum()
        dec_day = dts // D
        lo, hi = int(dec_day.min()) - 31, int(dec_day.max())
        full = dq.reindex(range(lo, hi + 1))
        med_zero = full.fillna(0.0).rolling(30, min_periods=30).median().shift(1)  # days d-30..d-1
        med_skip = full.rolling(30, min_periods=1).median().shift(1)
        u2z = med_zero.reindex(dec_day).to_numpy() >= 20e6
        u2s = np.nan_to_num(med_skip.reindex(dec_day).to_numpy()) >= 20e6
        u1 = first_bar <= dts - 60 * D
        # U3: funding records (REST, W_DEV), intervals from the archive files
        fr = V / "funding_rest" / f"{sym}.csv"
        fa = V / "funding_archive" / f"{sym}.csv"
        if not fr.exists():
            sym_info[sym] = "no_funding_rest_file"
            continue
        f = pd.read_csv(fr)["fundingTime"].to_numpy(np.int64)
        f.sort()
        iv_h = np.full(len(f), 8.0)
        if fa.exists() and len(f):
            a = pd.read_csv(fa)
            if len(a):
                at = a.iloc[:, 0].to_numpy(np.int64)
                ai = a.iloc[:, 1].to_numpy(np.float64)
                o = np.argsort(at)
                at, ai = at[o], ai[o]
                j = np.clip(np.searchsorted(at, f), 0, len(at) - 1)
                j2 = np.clip(j - 1, 0, len(at) - 1)
                pick = np.where(np.abs(at[j] - f) <= np.abs(at[j2] - f), j, j2)
                ok = np.abs(at[pick] - f) <= 60_000
                iv_h = np.where(ok, ai[pick], 8.0)
        spacing = np.diff(f)
        u3p = np.zeros(len(dts), bool)
        u3x = np.zeros(len(dts), bool)
        for i, td in enumerate(dts):
            kn = td - 2 * 60_000  # known_at = funding_ts + 1 min <= t - 1 min
            n = np.searchsorted(f, kn, "right")
            if n == 0 or f[0] > td - 30 * D:
                continue
            s = np.searchsorted(f, td - 7 * D, "left")
            lo_i = max(s, 1)
            sp = spacing[lo_i - 1:n - 1]
            nom = iv_h[lo_i:n] * H
            u3p[i] = bool(np.all(sp <= nom + 60_000))
            u3x[i] = bool(np.all(sp <= 2 * nom))
        g = u1 & u4
        counts["primary"] += g & u2z & u3p
        elig[sym] = np.packbits(g & u2z & u3p)
        counts["u2_skip"] += g & u2s & u3p
        counts["u3_2x"] += g & u2z & u3x
        if not unknown:
            counts["excl_unknown"] += g & u2z & u3p
        sym_info[sym] = "unknown_underlying" if unknown else "coin"
    res = {"variants": {}, "symbols": {k: sum(1 for v in sym_info.values() if v == k) for k in set(sym_info.values())}}
    month_of = pd.to_datetime(dts, unit="ms", utc=True).strftime("%Y-%m")
    for v in var_names:
        df = pd.DataFrame({"m": month_of, "c": counts[v]})
        mm = df.groupby("m")["c"].agg(["min", "median", "max"])
        first = next((m for m, r in mm.iterrows() if r["min"] >= 30), None)
        res["variants"][v] = {"D0": first, "monthly": mm.astype(int).reset_index().values.tolist()}
    res["unknown_symbols"] = sorted(s for s, v in sym_info.items() if v == "unknown_underlying")
    OUT.write_text(json.dumps(res, indent=1))
    np.savez_compressed(OUT.with_suffix(".elig.npz"), dts=dts, **elig)
    for v in var_names:
        print(v, "D0 =", res["variants"][v]["D0"], res["variants"][v]["monthly"][:6])
    print(res["symbols"])


if __name__ == "__main__":
    main()
