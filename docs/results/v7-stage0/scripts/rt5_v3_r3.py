"""RT-5 / S0-14 re-run after the ROUND-3 PATCH: DEC-44 (instance end known only 7 days later; PIT membership),
DEC-47 (DEC-29 interval inference restored, instance-aware) and DEC-43 (terminal events: A settle / B,C invalid;
the M3-1 audit found no A event, so every terminal event reached by a label is B/C -> invalid).
Otherwise identical to v2 (DEC-35, DEC-40).

Outcome-free: tests only whether label INPUTS exist (bar presence, volume > 0 flags, funding-record presence and
timing, mark-tier presence). No price value, return, label, P&L or IC is computed. W_DEV only (< 2025-07-01).

Population (new): the Stage-0 G_D0 grid (U1-U4, elig_wdev) minus the 5 unknown-type symbols (DEC-30), AND
  - U1 on the activity-based instance (S-2.2, DEC-35): the current instance started <= t - 60 d
    (U3's 30-day instance history is implied by this, given the grid's U3 already passed);
  - U8: a volume > 0 15m bar whose close lies in [t - 60 min, t - 1 min].
U5-U7 and U6 are not applied, so the population is still a superset of the true candidate set.
Labels of the OLD population (v1) are also classified, to show what became of the v1 'exit <= entry' cases.

Per label (S-4.4, S-4.5, S-5.4, S-5.5, S-2.6 / DEC-40):
  entry  halted at t_e (no vol>0 1m bar opening in [t_e-60m, t_e)) -> invalid 'entry_halted';
         else 1m bar at t_e, or the first 1m bar within 60 min (data-gap rule) -> else invalid 'entry_missing';
         entry proxy (>= 1 consecutive present 15m pair known by t_e - 1 min within 24 h) -> else invalid.
  exit   not halted at t_x: 1m bar at t_x or within 60 min -> else invalid 'exit_gap';
         halted at t_x: the first vol>0 1m bar of the same instance opening >= t_x, fill at its close; none before the
         instance ends or before 2025-07-01 -> invalid 'no_executable_exit'.
  funding  no missing settlement in (t_e, t_x_eff] (DEC-29 gap rule, head and tail) and no unreconciled record (DEC-40).
  mark     REST markPrice, else mark15 / last15 at the settlement (S-4.6).
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import rt5_availability as v1  # noqa: E402  (reuses its fetch / kline / funding helpers; argv is shared)

V, OUT = v1.V, v1.OUT
MIN, H, D, W_END = v1.MIN, v1.H, v1.D, v1.W_END
HORIZONS = v1.HORIZONS


def instances(sym):
    """Activity-based instance boundaries from 15m bars: list of (start_ts, end_ts_exclusive or None)."""
    k = pd.read_parquet(V / "k15_last" / f"{sym}.parquet", columns=["open_time", "volume"])
    t = k["open_time"].to_numpy(np.int64)
    act = t[k["volume"].to_numpy(np.float64) > 0]
    if len(t) == 0 or len(act) == 0:
        return [], t, act
    days_act = np.unique(act // D)
    first_day, last_day = t.min() // D, (W_END - 1) // D
    starts, ends = [int(t.min())], []
    # dormancy = >= 7 consecutive UTC days without a vol>0 bar, between active days (or to the data end)
    gaps = np.diff(days_act) - 1
    for g, d0 in zip(gaps, days_act[:-1]):
        if g >= 7:
            last_bar = act[np.searchsorted(act, (d0 + 1) * D) - 1]
            ends.append(int(last_bar + 15 * MIN))
            nxt = act[np.searchsorted(act, (d0 + 1) * D)]
            starts.append(int(nxt))
    tail_gap = last_day - days_act[-1]
    ends.append(int(act[-1] + 15 * MIN) if tail_gap >= 7 else None)
    # a leading dormancy before the first trade: the first instance still starts at the first archived bar (S-2.2)
    return list(zip(starts, ends)), t, act


def end_known(b):
    """DEC-44: instance_end_known_at = 00:00 UTC after the 7th inactive day (>= last close + 7 d)."""
    return None if b is None else (((b - 15 * MIN) // D) + 8) * D


def inst_pit(inst, x):
    """DEC-44 point-in-time instance at x: latest instance with start <= x, unless its end is already known at x."""
    k = -1
    for i, (a, b) in enumerate(inst):
        if a <= x:
            k = i
    if k >= 0 and inst[k][1] is not None and x >= end_known(inst[k][1]):
        return -1
    return k


def inst_of(inst, x):
    """Index of the instance containing time x (start <= x < end), or -1."""
    for i, (a, b) in enumerate(inst):
        if a <= x and (b is None or x < b):
            return i
    return -1


def reconciled(ts, iv_explicit, iv_inferred, inst):
    """DEC-47 / DEC-40 (spec S-2.6): instance-aware nominal intervals and reconciliation flags.
    nominal = explicit, else the standard interval (1/2/4/8 h) within 60 s of the spacing to the previous record of the
    same instance, else the previous nominal (flagged); first record of an instance without explicit -> undetermined.
    reconciled = spacing matches the previous (determined) nominal or the record's own explicit interval;
    first record of an instance: explicit present. Returns (ok, instance ids, nominal)."""
    ok = np.zeros(len(ts), bool); nom = np.full(len(ts), np.nan)
    ii = np.array([inst_of(inst, int(x)) for x in ts]) if len(inst) else np.full(len(ts), -1)
    for i in range(len(ts)):
        ex = iv_explicit[i]
        first = i == 0 or ii[i] != ii[i - 1]
        if first:
            nom[i] = ex
            ok[i] = not np.isnan(ex)
            continue
        sp = ts[i] - ts[i - 1]
        std = min((1, 2, 4, 8), key=lambda c: abs(c * H - sp))
        nom[i] = ex if not np.isnan(ex) else (std if abs(std * H - sp) <= MIN else nom[i - 1])
        cands = ([nom[i - 1]] if not np.isnan(nom[i - 1]) else []) + ([ex] if not np.isnan(ex) else [])
        ok[i] = any(abs(sp - c * H) <= MIN for c in cands)
    return ok, ii, nom


def funding_v2(sym):
    ts, iv, has_mp, unexplained, rest_fill = v1.funding(sym)
    # explicit intervals: archive records only (REST has none)
    a_p = V / "funding_archive" / f"{sym}.csv"
    expl = np.full(len(ts), np.nan)
    if a_p.exists():
        a = pd.read_csv(a_p)
        m = dict(zip(a.iloc[:, 0].astype(np.int64), a.iloc[:, 1].astype(float)))
        expl = np.array([m.get(int(x), np.nan) for x in ts])
    return ts, iv, expl, has_mp, rest_fill


def audit(sym, dec_old, dec_new_mask, btc_mode=False):
    te = dec_old + 15 * MIN
    lab = [(i, h, te[i] + h * H) for i in range(len(te)) for h in HORIZONS if te[i] + h * H < W_END]
    res = {"symbol": sym}
    if not lab:
        return res
    li = np.array([x[0] for x in lab]); lh = np.array([x[1] for x in lab]); lx = np.array([x[2] for x in lab])
    lte = te[li]; new = dec_new_mask[li]
    inst, t15, act15 = instances(sym)
    months = v1.months_between(int(lte.min()), int(min(lx.max() + 9 * D, W_END - 1)))
    months = [m for m in months if m in {f[0] for f in v1.LIST1M.get(sym, {}).get("files", [])}]
    t1, vol1 = v1.minutes_1m(sym, months)
    req = np.unique(np.concatenate([lte, lx]))
    miss = req[~np.isin(req, t1)]
    days = {pd.Timestamp(x, unit="ms").strftime("%Y-%m-%d") for x in miss}
    if days:
        t1, vol1, _, _ = v1.add_daily(sym, t1, vol1, days)
    act1 = t1[vol1]

    def halted(x):
        return (np.searchsorted(act1, x, "left") - np.searchsorted(act1, x - 60 * MIN, "left")) == 0

    def bar_or_gap(x):
        return (np.searchsorted(t1, x + 60 * MIN, "right") - np.searchsorted(t1, x, "left")) > 0

    # proxy presence: a consecutive present 15m pair (k, k+1) with k open >= x - 24h15m and close(k+1) <= x - 1m
    pres15 = np.sort(t15)
    pair_k = pres15[:-1][np.diff(pres15) == 15 * MIN]

    def proxy_ok(x):
        lo, hi = x - 24 * H - 15 * MIN, x - MIN - 30 * MIN  # k+1 closes at k + 30 min
        return (np.searchsorted(pair_k, hi, "right") - np.searchsorted(pair_k, lo, "left")) > 0

    ts, iv, expl, has_mp, rest_fill = funding_v2(sym)
    rec_ok, rec_inst, nom = reconciled(ts, expl, iv, inst)
    iv = np.where(np.isnan(nom), iv, nom)  # DEC-47 nominal intervals drive the U3/S-5.5 gap test
    k15all = t15
    cause = {c: np.zeros(len(lx), bool) for c in ("entry_halted", "entry_missing", "entry_proxy_missing", "exit_gap",
                                                 "no_executable_exit", "funding_missing", "funding_unreconciled",
                                                 "mark_missing")}
    waited = np.zeros(len(lx), bool); exit_proxy_fb = np.zeros(len(lx), bool)
    old_exit_le_entry = np.zeros(len(lx), bool); txe = lx.copy()
    kt = act15
    need_m15 = set(); settle_lists = []
    exact_x = v1.present(t1, lx)  # vectorized (np.isin per label against ~2M minutes was the bottleneck)
    for n in range(len(lx)):
        e, x = int(lte[n]), int(lx[n])
        # v1 classification (old S-5.4): instance-stopped => t_x := last traded 15m bar + 15 min
        if not exact_x[n]:
            j = np.searchsorted(kt, x - 15 * MIN, "right") - 1
            if j >= 0:
                L = kt[j]; nx = kt[j + 1] if j + 1 < len(kt) else None
                if (nx is None or nx > L + 7 * D) and L + 15 * MIN <= e:
                    old_exit_le_entry[n] = True
        if halted(e):
            cause["entry_halted"][n] = True
        elif not bar_or_gap(e):
            cause["entry_missing"][n] = True
        if not proxy_ok(e):
            cause["entry_proxy_missing"][n] = True
        if halted(x):
            waited[n] = True
            ie = inst_pit(inst, e)
            end = inst[ie][1] if ie >= 0 else e
            j = np.searchsorted(act1, x, "left")
            u = int(act1[j]) if j < len(act1) else None
            if u is None or (end is not None and u >= end) or u + MIN >= W_END:
                cause["no_executable_exit"][n] = True
            else:
                txe[n] = u + MIN
        elif not bar_or_gap(x):
            cause["exit_gap"][n] = True
        if not proxy_ok(int(txe[n])):
            exit_proxy_fb[n] = True
        hi = int(txe[n]) if not cause["no_executable_exit"][n] else x
        ok, settles = v1.funding_ok(ts, iv, e, hi)
        cause["funding_missing"][n] = not ok
        if len(settles):
            idx = np.searchsorted(ts, settles)
            cause["funding_unreconciled"][n] = bool((~rec_ok[idx]).any())
            ho = (settles // (15 * MIN)) * (15 * MIN) - 15 * MIN
            for xx in ho[~has_mp[idx] & ~v1.present(k15all, ho)]:
                need_m15.add(int(xx))
        settle_lists.append(settles)
    m15 = dict(zip(sorted(need_m15), v1.mark15_present(sym, np.array(sorted(need_m15), np.int64))))
    for n, settles in enumerate(settle_lists):
        if len(settles):
            idx = np.searchsorted(ts, settles)
            ho = (settles // (15 * MIN)) * (15 * MIN) - 15 * MIN
            fb = v1.present(k15all, ho) | np.array([m15.get(int(xx), False) for xx in ho], bool)
            cause["mark_missing"][n] = not bool((has_mp[idx] | fb).all())
    inv = np.zeros(len(lx), bool)
    for c in cause.values():
        inv |= c

    def tally(mask):
        d = {"labels": int(mask.sum()), "invalid": int((inv & mask).sum())}
        d.update({c: int((v & mask).sum()) for c, v in cause.items()})
        d["exit_waited_halted"] = int((waited & mask).sum())
        d["exit_waited_filled"] = int((waited & ~cause["no_executable_exit"] & mask).sum())
        d["exit_proxy_fallback"] = int((exit_proxy_fb & mask).sum())
        d["exit_at_or_before_entry"] = int(((txe <= lte) & ~cause["no_executable_exit"] & mask).sum())
        return d

    res["new"] = tally(new)
    res["old"] = tally(np.ones(len(lx), bool))
    o = old_exit_le_entry
    res["old111"] = {"n": int(o.sum()), "excluded_by_new_population": int((o & ~new).sum()),
                     "in_new_population_invalid": int((o & new & inv).sum()),
                     "in_new_population_valid": int((o & new & ~inv).sum())}
    res["unreconciled_records"] = int((~rec_ok).sum())
    res["by_year_new"] = pd.DataFrame({"y": pd.to_datetime(lte, unit="ms").year, "inv": inv, "new": new}) \
        .query("new").groupby("y")["inv"].agg(["sum", "count"]).reset_index().values.tolist()
    if btc_mode:
        res["_valid_by_dec_h"] = {(int(dec_old[li[k]]), int(lh[k])): bool(~inv[k]) for k in range(len(lx))}
    return res


def main():
    z = np.load(V / "summary/elig_wdev.elig.npz")
    dts = z["dts"]
    syms = [k for k in z.files if k != "dts" and k not in v1.UNKNOWN]
    if len(sys.argv) > 3:
        syms = [k for k in syms if k in sys.argv[3].split(",")]
    elig = {k: np.unpackbits(z[k])[:len(dts)].astype(bool) for k in syms}
    elig = {k: e for k, e in elig.items() if e.any()}

    newmask, inst_report, d0 = {}, {}, {}
    for sym, e in elig.items():
        inst, t15, act15 = instances(sym)
        d = dts[e]
        u1 = np.array([(lambda i: i >= 0 and inst[i][0] <= x - 60 * D)(inst_pit(inst, int(x))) for x in d], bool)
        lo = np.searchsorted(act15, d - 75 * MIN, "left"); hi = np.searchsorted(act15, d - 16 * MIN, "right")
        u8 = (hi - lo) > 0
        newmask[sym] = u1 & u8
        inst_report[sym] = {"instances": [[pd.Timestamp(a, unit="ms").isoformat(),
                                            None if b is None else pd.Timestamp(b, unit="ms").isoformat()] for a, b in inst],
                            "decisions_old": int(len(d)), "fail_u1_instance": int((~u1).sum()), "fail_u8": int((~u8).sum())}
        full = np.zeros(len(dts), bool); full[np.where(e)[0][u1]] = True
        d0[sym] = full
    # D0 re-check (G_D0 = U1-U4 with activity instances; U8 is not in G_D0). Unknown-type symbols included as in Stage 0.
    allsyms = [k for k in z.files if k != "dts"]
    cnt = np.zeros(len(dts), int)
    for k in allsyms:
        e = np.unpackbits(z[k])[:len(dts)].astype(bool)
        if k in d0:
            e = d0[k]
        elif e.any():
            inst, _, _ = instances(k)
            idx = np.where(e)[0]
            ok = np.array([(lambda i: i >= 0 and inst[i][0] <= int(dts[j]) - 60 * D)(inst_pit(inst, int(dts[j]))) for j in idx], bool)
            e = np.zeros(len(dts), bool); e[idx[ok]] = True
        cnt += e
    mon = pd.Series(cnt, index=pd.to_datetime(dts, unit="ms")).groupby(lambda x: x.strftime("%Y-%m")).min()
    d0_month = next((m for m, v in mon.items() if v >= 30), None)
    split_syms = {k: v["instances"] for k, v in inst_report.items() if len(v["instances"]) > 1}
    print("D0 recheck: first month with min >= 30:", d0_month, "2021-01 min =", int(mon.get("2021-01", -1)), flush=True)
    print(len(split_syms), "symbols with > 1 activity instance", flush=True)

    t0 = time.time()
    btc_dec = np.unique(np.concatenate([dts[e] for e in elig.values()]))
    btc = audit("BTCUSDT", btc_dec, np.ones(len(btc_dec), bool), btc_mode=True)
    bvalid = btc.pop("_valid_by_dec_h")
    (OUT / "btc.json").write_text(json.dumps(btc, default=int))
    print("BTC", round(time.time() - t0), "s", btc["new"], flush=True)

    def job(sym):
        f = OUT / f"{sym}.json"
        if f.exists():
            return json.loads(f.read_text())
        e = elig[sym]
        r = audit(sym, dts[e], newmask[sym])
        if "new" in r:
            # BTC inputs per (decision, horizon) for the new population
            d = dts[e]
            te = d + 15 * MIN
            bad = sum(1 for i in range(len(d)) if newmask[sym][i] for h in HORIZONS
                      if te[i] + h * H < W_END and not bvalid.get((int(d[i]), h), False))
            r["new"]["btc_input_missing"] = int(bad)
        f.write_text(json.dumps(r, default=int))
        return r

    out = []
    with ThreadPoolExecutor(6) as ex:
        for i, r in enumerate(ex.map(job, sorted(elig))):
            out.append(r)
            if i % 25 == 0:
                print(i, r["symbol"], r.get("new"), time.strftime("%H:%M:%S"), flush=True)

    def tot(key):
        ks = sorted({k for r in out for k in r.get(key, {})})
        return {k: int(sum(r.get(key, {}).get(k, 0) for r in out)) for k in ks}

    new_t, old_t, o111 = tot("new"), tot("old"), tot("old111")
    by = pd.DataFrame([x for r in out for x in r.get("by_year_new", [])], columns=["y", "inv", "n"]).groupby("y").sum()
    summary = {
        "population_new": "G_D0 grid (U1-U4) minus 5 unknown-type symbols, with U1 on PIT activity instances (DEC-35, DEC-44) and U8; terminal events B/C -> invalid (DEC-43); W_DEV; h in {24,72,168}; t_x < 2025-07-01",
        "symbols": len(out), "new": new_t, "old_population_under_new_rules": old_t, "old111": o111,
        "invalid_rate_new": new_t.get("invalid", 0) / max(new_t.get("labels", 1), 1),
        "by_year_new": by.reset_index().values.tolist(), "btc": btc,
        "d0_recheck": {"first_month_min_ge_30": d0_month, "min_2021_01": int(mon.get("2021-01", -1)),
                       "min_by_month_first12": {k: int(v) for k, v in list(mon.items())[:12]}},
        "unreconciled_funding_records": int(sum(r.get("unreconciled_records", 0) for r in out)),
        "symbols_with_multiple_activity_instances": split_syms,
        "worst_symbols_new": sorted(((r["new"]["invalid"] / max(r["new"]["labels"], 1), r["symbol"], r["new"]["labels"])
                                     for r in out if "new" in r and r["new"]["labels"]), reverse=True)[:15],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=int))
    (OUT / "instances.json").write_text(json.dumps(inst_report, indent=1, default=int))
    print(json.dumps({k: summary[k] for k in ("symbols", "new", "old111", "invalid_rate_new", "d0_recheck")}, default=int), flush=True)


if __name__ == "__main__":
    main()
