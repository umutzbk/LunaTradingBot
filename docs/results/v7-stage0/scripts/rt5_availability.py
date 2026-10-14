"""RT-5 / S0-14 (DEC-27): outcome-free W_DEV label-INPUT availability audit.

Checks only whether the inputs a label would need EXIST. It never computes label values, returns, P&L or IC, and
never reads a price value except to test presence (and volume > 0 for the S-5.4 exit bar).

Population: the Stage-0 G_D0 grid (U1-U4, d0_eval.py, primary variant) minus the 5 unknown-type symbols (DEC-30),
decisions 2021-01-01..2025-06-30, horizons 24/72/168 h; labels with t_x >= 2025-07-01 don't exist (S-1.5).
Both sides of a symbol share identical inputs, so counts are per (symbol, decision, horizon).

Per label (spec S-5.2..S-5.5, S-2.6, DEC-29, DEC-32, DEC-33):
  A  P(t_e): 1m last-price bar opening at t_e (t_e = t + 15 min = hh:20; the 15m fallback can't exist at hh:20)
  B  exit:   1m bar at t_x; else, if the instance stopped trading before t_x (last volume>0 15m bar L <= t_x - 15 min and
             no volume>0 15m bar in (L, L + 7 d]), a volume>0 1m bar inside [L, L+15m) (S-5.4); else any 1m bar in
             [t_x, t_x + 60 min] (S-5.4 data-gap rule); else invalid
  C  B(t_e), B(t_x): the same for BTCUSDT
  D  funding: no missing settlement in (t_e, t_x_eff] for the leg and for BTC: union archive (primary) + REST (fills),
     gap = spacing > max(prev, cur interval) + 60 s; the tail t_x_eff - last record must be <= last interval + 60 s
  E  mark at each settlement: REST markPrice, else the mark 15m bar ending at the settlement, else the last-price 15m
     bar ending at it (S-4.6). Mark 15m files are fetched only where the other two tiers are both missing.
Monthly 1m files are streamed; missing required minutes are looked up in the daily 1m files (monthly ∪ daily, DEC-33).
"""
import io
import json
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

V = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
MIN, H, D = 60_000, 3_600_000, 86_400_000
W_END = 1751328000000  # 2025-07-01
HORIZONS = (24, 72, 168)
UNKNOWN = {"AERGOUSDT", "BDXNUSDT", "DOTECOUSDT", "LENDUSDT", "MBLUSDT"}
BASE = "https://data.binance.vision/data/futures/um"
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
        "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
LIST1M = json.loads((V / "listings/monthly_klines_1m.json").read_text())["symbols"]


def fetch(url):
    for a in range(6):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 ** a)
        except Exception:
            time.sleep(2 ** a)
    raise RuntimeError(url)


def read_kline_zip(b):
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        raw = z.read(z.namelist()[0])
    hdr = raw[:9].decode(errors="replace").startswith("open_time")
    df = pd.read_csv(io.BytesIO(raw), header=0 if hdr else None, names=None if hdr else COLS,
                     usecols=[0, 5])
    t = df.iloc[:, 0].to_numpy(np.int64)
    if len(t) and t.max() > 1e14:
        t = t // 1000
    return t, df.iloc[:, 1].to_numpy(np.float64) > 0


def minutes_1m(sym, months):
    """(open_time, vol>0) arrays from the monthly 1m files of the given months (W_DEV only)."""
    ts, vs = [], []
    for ym in months:
        b = fetch(f"{BASE}/monthly/klines/{sym}/1m/{sym}-1m-{ym}.zip")
        if b is None:
            continue
        t, v = read_kline_zip(b)
        keep = t < W_END
        ts.append(t[keep]); vs.append(v[keep])
    if not ts:
        return np.zeros(0, np.int64), np.zeros(0, bool)
    t = np.concatenate(ts); v = np.concatenate(vs)
    o = np.argsort(t, kind="stable")
    t, v = t[o], v[o]
    u = np.r_[True, np.diff(t) != 0]
    return t[u], v[u]


def add_daily(sym, t, v, days):
    """DEC-33: fill keys missing from monthly files with the daily files of the given UTC days."""
    filled, req = 0, 0
    for day in sorted(days):
        req += 1
        b = fetch(f"{BASE}/daily/klines/{sym}/1m/{sym}-1m-{day}.zip")
        if b is None:
            continue
        dt_, dv = read_kline_zip(b)
        new = ~np.isin(dt_, t)
        if new.any():
            filled += int(new.sum())
            t = np.concatenate([t, dt_[new]]); v = np.concatenate([v, dv[new]])
    o = np.argsort(t, kind="stable")
    return t[o], v[o], filled, req


def funding(sym):
    """Union of archive (primary) and REST (fill) settlement records with intervals and markPrice presence."""
    a_p, r_p = V / "funding_archive" / f"{sym}.csv", V / "funding_rest" / f"{sym}.csv"
    a = pd.read_csv(a_p) if a_p.exists() else pd.DataFrame(columns=["calc_time", "funding_interval_hours"])
    r = pd.read_csv(r_p, dtype={"markPrice": str}) if r_p.exists() else pd.DataFrame(columns=["fundingTime", "markPrice"])
    at = a.iloc[:, 0].to_numpy(np.int64) if len(a) else np.zeros(0, np.int64)
    ai = a.iloc[:, 1].to_numpy(np.float64) if len(a) else np.zeros(0)
    o = np.argsort(at); at, ai = at[o], ai[o]
    rt = r["fundingTime"].to_numpy(np.int64) if len(r) else np.zeros(0, np.int64)
    mp = r["markPrice"].fillna("").to_numpy(str) if len(r) else np.zeros(0, str)
    rmp = np.array([(x not in ("", "0", "0.0", "0.00000000")) for x in mp], bool)
    o = np.argsort(rt); rt, rmp = rt[o], rmp[o]

    def near(x, arr):
        if len(arr) == 0:
            return np.zeros(len(x), bool), np.zeros(len(x), np.int64)
        j = np.clip(np.searchsorted(arr, x), 0, len(arr) - 1)
        j2 = np.clip(j - 1, 0, len(arr) - 1)
        pick = np.where(np.abs(arr[j] - x) <= np.abs(arr[j2] - x), j, j2)
        return np.abs(arr[pick] - x) <= MIN, pick

    rest_in_arch, _ = near(rt, at)
    extra = rt[~rest_in_arch]
    ts = np.concatenate([at, extra])
    iv = np.concatenate([ai, np.full(len(extra), np.nan)])
    src_rest = np.concatenate([np.zeros(len(at), bool), np.ones(len(extra), bool)])
    o = np.argsort(ts); ts, iv, src_rest = ts[o], iv[o], src_rest[o]
    # DEC-29: infer missing intervals mechanically from the spacing to the previous record
    unexplained = 0
    for i in np.where(np.isnan(iv))[0]:
        sp = (ts[i] - ts[i - 1]) / H if i > 0 else 8.0
        cand = min((1, 2, 4, 8), key=lambda c: abs(c - sp))
        if abs(cand - sp) * H > MIN:
            unexplained += 1
            cand = 8.0 if i == 0 else (iv[i - 1] if not np.isnan(iv[i - 1]) else cand)
        iv[i] = cand
    has_mp, _ = near(ts, rt[rmp]) if rmp.any() else (np.zeros(len(ts), bool), None)
    return ts, iv, has_mp, unexplained, int(src_rest.sum())


def funding_ok(ts, iv, lo, hi):
    """No missing settlement in (lo, hi]: DEC-29 gap rule, including the head (from the last record <= lo) and tail."""
    if hi <= lo:  # exit at or before entry (instance stopped trading before t_e): no settlement in the window
        return True, np.zeros(0, np.int64)
    if len(ts) == 0:
        return False, np.zeros(0, np.int64)
    p = np.searchsorted(ts, lo, "right") - 1
    q = np.searchsorted(ts, hi, "right")
    if p < 0:
        return False, ts[:q]
    seq_t, seq_i = ts[p:q], iv[p:q]
    sp = np.diff(seq_t)
    allowed = np.maximum(seq_i[1:], seq_i[:-1]) * H + MIN
    ok = bool(np.all(sp <= allowed)) and (hi - seq_t[-1] <= seq_i[-1] * H + MIN)
    return ok, ts[p + 1:q]


def last15(sym):
    k = pd.read_parquet(V / "k15_last" / f"{sym}.parquet", columns=["open_time", "volume"])
    return k["open_time"].to_numpy(np.int64), k["volume"].to_numpy(np.float64) > 0


def present(sorted_arr, x):
    """Membership of x in a sorted int64 array (vectorized, no re-sorting)."""
    x = np.asarray(x, np.int64)
    if len(sorted_arr) == 0:
        return np.zeros(len(x), bool)
    i = np.clip(np.searchsorted(sorted_arr, x), 0, len(sorted_arr) - 1)
    return sorted_arr[i] == x


def months_between(lo, hi):
    return [d.strftime("%Y-%m") for d in pd.date_range(pd.Timestamp(lo, unit="ms").to_period("M").to_timestamp(),
                                                         pd.Timestamp(min(hi, W_END - 1), unit="ms"), freq="MS")]


def mark15_present(sym, need_open):
    """Mark 15m bar presence for the given open times (fetched only when needed)."""
    if len(need_open) == 0:
        return np.zeros(0, bool)
    have = []
    for ym in sorted({pd.Timestamp(x, unit="ms").strftime("%Y-%m") for x in need_open}):
        b = fetch(f"{BASE}/monthly/markPriceKlines/{sym}/15m/{sym}-15m-{ym}.zip")
        if b is not None:
            have.append(read_kline_zip(b)[0])
    have = np.concatenate(have) if have else np.zeros(0, np.int64)
    return np.isin(need_open, have)


def audit_symbol(sym, dec_ms, is_btc=False):
    """dec_ms: decision times (ms) at which the symbol's labels are needed."""
    te = dec_ms + 15 * MIN
    labels = [(i, h, te[i] + h * H) for i in range(len(te)) for h in HORIZONS if te[i] + h * H < W_END]
    if not labels:
        return {"symbol": sym, "labels": 0}
    li = np.array([x[0] for x in labels]); lh = np.array([x[1] for x in labels]); lx = np.array([x[2] for x in labels])
    lte = te[li]
    months = months_between(int(lte.min()), int(lx.max() + 61 * MIN))
    months = [m for m in months if m in {f[0] for f in LIST1M.get(sym, {}).get("files", [])}]
    t1, v1 = minutes_1m(sym, months)
    # DEC-33 daily fill for required exact minutes absent from the monthly files
    req = np.unique(np.concatenate([lte, lx]))
    miss = req[~np.isin(req, t1)]
    days = {pd.Timestamp(x, unit="ms").strftime("%Y-%m-%d") for x in miss}
    filled = dreq = 0
    if days:
        t1, v1, filled, dreq = add_daily(sym, t1, v1, days)
    vcum = np.r_[0, np.cumsum(v1)]
    A = np.isin(lte, t1)
    exact_x = np.isin(lx, t1)
    any60 = (np.searchsorted(t1, lx + 60 * MIN, "right") - np.searchsorted(t1, lx, "left")) > 0
    # S-5.4 instance-stopped path (not for BTC)
    s54 = np.zeros(len(lx), bool); s54_ok = np.zeros(len(lx), bool); tx_eff = lx.copy()
    if not is_btc:
        k15, kv = last15(sym)
        kt = k15[kv]  # traded 15m bars
        for n in np.where(~exact_x)[0]:
            j = np.searchsorted(kt, lx[n] - 15 * MIN, "right") - 1
            if j < 0:
                continue
            L = kt[j]
            nxt = kt[j + 1] if j + 1 < len(kt) else None
            if nxt is None or nxt > L + 7 * D:  # stopped trading before t_x
                s54[n] = True
                a, b = np.searchsorted(t1, L, "left"), np.searchsorted(t1, L + 15 * MIN, "left")
                s54_ok[n] = (vcum[b] - vcum[a]) > 0
                tx_eff[n] = L + 15 * MIN
    B = exact_x | (s54 & s54_ok) | (~s54 & any60)
    # funding + mark
    ts, iv, has_mp, unexplained, rest_fill = funding(sym)
    k15all = last15(sym)[0] if not is_btc else last15("BTCUSDT")[0]
    Dok = np.zeros(len(lx), bool); Eok = np.zeros(len(lx), bool)
    need_mark15 = set()
    per_label_settle = []
    for n in range(len(lx)):
        ok, settles = funding_ok(ts, iv, lte[n], tx_eff[n])
        Dok[n] = ok
        per_label_settle.append(settles)
        if len(settles):
            idx = np.searchsorted(ts, settles)
            no_mp = ~has_mp[idx]
            hour_open = (settles // (15 * MIN)) * (15 * MIN) - 15 * MIN  # 15m bar ending at the settlement
            no_last = ~present(k15all, hour_open)
            for x in hour_open[no_mp & no_last]:
                need_mark15.add(int(x))
    m15 = dict(zip(sorted(need_mark15), mark15_present(sym, np.array(sorted(need_mark15), np.int64))))
    tier = {"markPrice": 0, "mark15_or_last15": 0, "missing": 0}
    for n, settles in enumerate(per_label_settle):
        if not len(settles):
            Eok[n] = True
            continue
        idx = np.searchsorted(ts, settles)
        hour_open = (settles // (15 * MIN)) * (15 * MIN) - 15 * MIN
        mp = has_mp[idx]
        fb = present(k15all, hour_open) | np.array([m15.get(int(x), False) for x in hour_open], bool)
        okk = mp | fb
        Eok[n] = bool(okk.all())
        tier["markPrice"] += int(mp.sum()); tier["mark15_or_last15"] += int((~mp & fb).sum()); tier["missing"] += int((~okk).sum())
    res = {"symbol": sym, "labels": int(len(lx)), "A_entry_missing": int((~A).sum()), "B_exit_missing": int((~B).sum()),
           "B_exit_exact": int(exact_x.sum()), "B_via_gap60": int((~exact_x & ~s54 & any60).sum()),
           "B_via_s54": int((s54 & s54_ok).sum()), "exit_at_or_before_entry": int((tx_eff <= lte).sum()),
           "D_funding_missing": int((~Dok).sum()),
           "E_mark_missing": int((~Eok).sum()), "mark_tiers": tier, "daily_fill_minutes": filled,
           "daily_files_requested": dreq, "funding_rest_fill_records": rest_fill,
           "funding_unexplained_interval_records": unexplained}
    by = pd.DataFrame({"y": pd.to_datetime(lte, unit="ms").year, "h": lh,
                       "inv": ~(A & B & Dok & Eok)})
    res["by_year_h"] = by.groupby(["y", "h"])["inv"].agg(["sum", "count"]).reset_index().values.tolist()
    res["_leg_ok"] = {"A": A, "B": B, "D": Dok, "E": Eok, "li": li, "lh": lh}
    return res


def main():
    z = np.load(V / "summary/elig_wdev.elig.npz")
    dts = z["dts"]
    syms = [k for k in z.files if k != "dts" and k not in UNKNOWN]
    if len(sys.argv) > 3:
        syms = [k for k in syms if k in sys.argv[3].split(',')]
    elig = {k: np.unpackbits(z[k])[:len(dts)].astype(bool) for k in syms}
    elig = {k: e for k, e in elig.items() if e.any()}
    any_dec = np.zeros(len(dts), bool)
    for e in elig.values():
        any_dec |= e
    print(len(elig), "eligible symbols;", int(sum(e.sum() for e in elig.values())), "symbol-decisions", flush=True)
    t0 = time.time()
    btc = audit_symbol("BTCUSDT", dts[any_dec], is_btc=True)
    btc_ok = btc.pop("_leg_ok")
    (OUT / "btc.json").write_text(json.dumps(btc))
    btc_dec = dts[any_dec]
    # BTC validity per (decision, horizon)
    bvalid = {}
    for k in range(len(btc_ok["li"])):
        bvalid[(int(btc_dec[btc_ok["li"][k]]), int(btc_ok["lh"][k]))] = bool(btc_ok["A"][k] and btc_ok["B"][k] and btc_ok["D"][k] and btc_ok["E"][k])
    print("BTC done", round(time.time() - t0), "s", {k: btc[k] for k in btc if k not in ("by_year_h", "mark_tiers")}, flush=True)

    def job(sym):
        f = OUT / f"{sym}.json"
        if f.exists():
            return json.loads(f.read_text())
        r = audit_symbol(sym, dts[elig[sym]])
        ok = r.pop("_leg_ok", None)
        if ok is not None:
            d = dts[elig[sym]]
            btc_bad = [not bvalid.get((int(d[ok["li"][k]]), int(ok["lh"][k])), False) for k in range(len(ok["li"]))]
            leg_ok = ok["A"] & ok["B"] & ok["D"] & ok["E"]
            r["btc_input_missing"] = int(np.sum(btc_bad))
            r["invalid_total"] = int(np.sum(~leg_ok | np.array(btc_bad, bool)))
        f.write_text(json.dumps(r, default=int))
        return r

    out = []
    with ThreadPoolExecutor(6) as ex:
        for i, r in enumerate(ex.map(job, sorted(elig))):
            out.append(r)
            if i % 25 == 0:
                print(i, r["symbol"], r.get("labels"), r.get("invalid_total"), time.strftime("%H:%M:%S"), flush=True)
    keys = ["labels", "A_entry_missing", "B_exit_missing", "B_exit_exact", "B_via_gap60", "B_via_s54", "exit_at_or_before_entry",
            "D_funding_missing", "E_mark_missing", "btc_input_missing", "invalid_total", "daily_fill_minutes",
            "daily_files_requested", "funding_rest_fill_records", "funding_unexplained_interval_records"]
    tot = {k: int(sum(r.get(k, 0) for r in out)) for k in keys}
    tiers = {k: int(sum(r.get("mark_tiers", {}).get(k, 0) for r in out)) for k in ("markPrice", "mark15_or_last15", "missing")}
    by = pd.DataFrame([x for r in out for x in r.get("by_year_h", [])], columns=["y", "h", "inv", "n"]).groupby(["y", "h"]).sum()
    summary = {"population": "G_D0 grid (U1-U4) minus 5 unknown-type symbols; W_DEV decisions; h in {24,72,168}; t_x < 2025-07-01",
               "symbols": len(out), "totals": tot, "mark_tiers_settlements": tiers,
               "invalid_rate": tot["invalid_total"] / max(tot["labels"], 1),
               "by_year_h": by.reset_index().values.tolist(),
               "btc": {k: btc[k] for k in btc if k != "by_year_h"},
               "worst_symbols": sorted(((r.get("invalid_total", 0) / max(r.get("labels", 1), 1), r["symbol"], r.get("labels", 0))
                                        for r in out), reverse=True)[:15]}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=int))
    print(json.dumps({k: summary[k] for k in ("symbols", "totals", "mark_tiers_settlements", "invalid_rate")}, default=int), flush=True)


if __name__ == "__main__":
    main()
