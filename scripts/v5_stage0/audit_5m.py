"""Protocol rev 3 §2.1 S0.3: outcome-free audit of the 5-minute spread snapshots (no P&L, no ranking).

Usage: audit_5m.py <spreads_root> <v4_spreads_dir> <out_json>

Reads ``<spreads_root>/<ASSET>.parquet`` + ``<ASSET>.provenance.json`` (assembled by
backfill_spreads.py --assemble). Spread magnitudes are only checked for validity, never summarised.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from luna.carry.frozen import CUTOFF, sha256_file
from luna.carry.history import DECISION_MIN_VALID, FILL_MIN_VALID, MAX_VALID_FULL, Spread5m

START = pd.Timestamp("2025-10-06T00:00Z")
OUTCOME = pd.Timestamp("2025-11-10T00:00Z")
M5 = pd.Timedelta("5min")
GRID = pd.date_range(START, CUTOFF, freq="5min", inclusive="left")
POINTS = pd.date_range(START + pd.Timedelta("1h"), CUTOFF, freq="15min", inclusive="left")


def rate(x: pd.Series) -> float:
    return round(float(x.mean()), 6) if len(x) else None


def breakdown(ok: pd.Series) -> dict:
    """ok: bool Series indexed by time -> rate overall / by month / by hour of day."""
    idx = ok.index
    return {"all": rate(ok),
            "by_month": {k: rate(v) for k, v in ok.groupby(idx.strftime("%Y-%m"))},
            "by_hour": {int(k): rate(v) for k, v in ok.groupby(idx.hour)}}


def v4_overlap(new: pd.Series, v4: pd.Series) -> dict:
    """5-minute value parity (new[T] == v4[T+4m]) and cadence parity (each V4 minute equals the
    snapshot effective at that minute under one phase per day)."""
    v4 = v4[(v4.index >= START) & (v4.index < CUTOFF)]
    labels = new.index[(new.index.hour == 23)]
    labels = labels[labels.isin(v4.index - pd.Timedelta("4min"))]
    a = new.loc[labels].to_numpy()
    b = v4.reindex(labels + pd.Timedelta("4min")).to_numpy()
    both_nan = np.isnan(a) & np.isnan(b)
    eq = both_nan | (np.abs(a - b) <= 1e-12)
    bad = [t.isoformat() for t in labels[~eq][:10]]
    days_multi_phase, minutes, matched = [], 0, 0
    for d, g in v4.groupby(v4.index.floor("D")):
        best = 0
        for ph in range(5):
            lab = (g.index - pd.Timedelta(minutes=ph)).floor("5min")
            ref = new.reindex(lab).to_numpy()
            gv = g.to_numpy()
            m = int(((np.abs(ref - gv) <= 1e-12) | (np.isnan(ref) & np.isnan(gv))).sum())
            best = max(best, m)
        minutes += len(g)
        matched += best
        if best < len(g):
            days_multi_phase.append(d.date().isoformat())
    return {"labels_compared": int(len(labels)), "value_match": int(eq.sum()),
            "value_match_rate": round(float(eq.mean()), 6) if len(eq) else None,
            "both_nan": int(both_nan.sum()), "first_mismatches": bad,
            "cadence_minutes": minutes, "cadence_minutes_explained_one_phase_per_day": matched,
            "cadence_days_not_single_phase": days_multi_phase}


def audit_asset(root: Path, v4dir: Path, a: str) -> tuple[dict, pd.DataFrame]:
    p = root / f"{a}.parquet"
    prov = json.loads((root / f"{a}.provenance.json").read_text(encoding="utf-8"))
    df = pd.read_parquet(p)
    idx = df.index
    v = df["spread_full"].to_numpy(dtype=float)
    out = {"rows": int(len(df)), "first": idx.min().isoformat(), "last": idx.max().isoformat(),
           "expected_labels": int(len(GRID)), "max_ts_before_cutoff": bool(idx.max() < CUTOFF),
           "duplicates": int(idx.duplicated().sum()), "monotonic": bool(idx.is_monotonic_increasing),
           "off_grid": int((idx.as_unit("ns").asi8 % M5.value != 0).sum()),
           "outside_window": int(((idx < START) | (idx >= CUTOFF)).sum()),
           "invalid": {"nan": int(np.isnan(v).sum()), "inf": int(np.isinf(v).sum()),
                       "non_positive": int((v <= 0).sum()), "gt_500bps": int((v > MAX_VALID_FULL).sum())},
           "sha256": sha256_file(p), "sha256_matches_provenance": sha256_file(p) == prov["sha256"],
           "observed_cadence_s": prov["observed_cadence_s"], "resolution_label": prov["resolution_label"],
           "rows_dropped_at_or_after_cutoff": prov["rows_dropped_at_or_after_cutoff"]}
    same = np.r_[False, (v[1:] == v[:-1]) & (np.diff(idx.as_unit("ns").asi8) == M5.value)]
    runs = pd.Series(same).groupby((~pd.Series(same)).cumsum()).sum()
    out["identical_consecutive"] = {"count": int(same.sum()), "rate": round(float(same.mean()), 6),
                                    "longest_run_labels": int(runs.max()) + 1 if len(runs) else 0}
    s = Spread5m(df)  # refuses off-grid / non-monotonic / holdout rows
    present = pd.Series(GRID.isin(idx), index=GRID)
    valid = pd.Series(False, index=GRID)
    with np.errstate(invalid="ignore"):
        ok = np.isfinite(v) & (v > 0) & (v <= MAX_VALID_FULL)
    valid.loc[idx[ok]] = True
    out["present"] = breakdown(present)
    out["valid"] = breakdown(valid)
    daily = valid.groupby(valid.index.floor("D")).mean()
    out["days_valid_below_90pct"] = int((daily < 0.9).sum())
    out["worst_days"] = {d.date().isoformat(): round(float(x), 4) for d, x in daily.nsmallest(5).items()}
    # gaps between consecutive present labels
    gaps = pd.Series(idx[1:] - idx[:-1] - M5, index=idx[:-1])
    head = idx.min() - START
    big = gaps[gaps >= pd.Timedelta("1h")]
    out["leading_gap_from_start"] = str(head)
    out["longest_gap"] = str(gaps.max()) if len(gaps) else None
    out["gaps_ge_1h"] = {"count": int(len(big)),
                         "list": [{"after": t.isoformat(), "missing": str(g)} for t, g in big.nlargest(15).items()]}
    # decision / fill coverage at every 15m point
    vv = valid.astype(int)
    dec_n = vv.rolling(12, min_periods=1).sum().shift(1)  # labels t-60 .. t-5 for a point t on the grid
    dec_n = dec_n.reindex(POINTS).fillna(0)
    fill_n = (vv + vv.shift(-1, fill_value=0) + vv.shift(-2, fill_value=0)).reindex(POINTS).fillna(0)
    dec_fb = dec_n < DECISION_MIN_VALID
    fill_fb = fill_n < FILL_MIN_VALID
    rng = np.random.default_rng(20261010)  # spot-check the vectorised counts against Spread5m
    for t in POINTS[rng.choice(len(POINTS), 300, replace=False)]:
        assert s.decision(t).n_valid == dec_n[t] and s.fill(t).n_valid == fill_n[t], (a, t)
    pts = pd.DataFrame({"asset": a, "dec_fb": dec_fb.to_numpy(), "fill_fb": fill_fb.to_numpy()}, index=POINTS)
    return out, pts


if __name__ == "__main__":
    root, v4dir, outp = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    assets = sorted(p.stem for p in root.glob("*.parquet"))
    res = {"window": [START.isoformat(), CUTOFF.isoformat()], "assets": {}, "v4_overlap": {}}
    pts = []
    for a in assets:
        res["assets"][a], pt = audit_asset(root, v4dir, a)
        pts.append(pt)
        new = pd.read_parquet(root / f"{a}.parquet")["spread_full"]
        v4 = pd.read_parquet(v4dir / f"{a}.parquet")["spread_full"]
        res["v4_overlap"][a] = v4_overlap(new, v4)
        print(a, "done", flush=True)
    P = pd.concat(pts)
    out = P.index >= OUTCOME
    res["fallback"] = {}
    for name, sel in [("all_points", slice(None)), ("outcome_window", out)]:
        Q = P[sel]
        midnight = Q[Q.index.hour.isin([0]) & (Q.index.minute == 0)]
        res["fallback"][name] = {
            "decision": breakdown(Q["dec_fb"]), "fill": breakdown(Q["fill_fb"]),
            "decision_at_0000": rate(midnight["dec_fb"]), "fill_at_0000": rate(midnight["fill_fb"]),
            "decision_by_asset": {k: rate(g["dec_fb"]) for k, g in Q.groupby("asset")},
            "fill_by_asset": {k: rate(g["fill_fb"]) for k, g in Q.groupby("asset")},
            "decision_at_0000_by_asset": {k: rate(g["dec_fb"]) for k, g in midnight.groupby("asset")}}
    # §2.1 flag (red-team F3): per asset and per asset x hour-of-day cell, decision and fill, outcome window
    Q = P[out]
    flag = {}
    for kind in ("dec_fb", "fill_fb"):
        cell = 1 - Q.groupby(["asset", Q.index.hour])[kind].mean()
        whole = 1 - Q.groupby("asset")[kind].mean()
        flag[kind] = {"min_asset": round(float(whole.min()), 6), "min_asset_hour_cell": round(float(cell.min()), 6),
                      "assets_below_90pct": {a: round(float(c), 4) for a, c in whole.items() if c < 0.9},
                      "cells_below_90pct": {f"{a}@{h:02d}": round(float(c), 4) for (a, h), c in cell.items() if c < 0.9}}
    res["coverage_flag_lt_90pct"] = flag
    res["holdout"] = {"max_ts": max(r["last"] for r in res["assets"].values()),
                      "pass": all(r["max_ts_before_cutoff"] and r["outside_window"] == 0 for r in res["assets"].values()),
                      "rows_dropped_at_or_after_cutoff": sum(r["rows_dropped_at_or_after_cutoff"] for r in res["assets"].values())}
    ov = res["v4_overlap"].values()
    res["v4_overlap_summary"] = {"labels": sum(o["labels_compared"] for o in ov),
                                 "value_match": sum(o["value_match"] for o in ov),
                                 "cadence_minutes": sum(o["cadence_minutes"] for o in ov),
                                 "cadence_explained": sum(o["cadence_minutes_explained_one_phase_per_day"] for o in ov)}
    outp.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("holdout", "v4_overlap_summary", "coverage_flag_lt_90pct")}))
