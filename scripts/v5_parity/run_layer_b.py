"""Layer B (protocol §5.3): B1 accounting, B2 snapshot (parity mode), B3 signal, vs the V4 reference."""
import json, math, sys
from pathlib import Path
import numpy as np
import pandas as pd
from luna.carry import parity
from luna.carry.history import parity_quote

FROZEN, MAN_SHA, REF, OUT = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
OUT.mkdir(parents=True, exist_ok=True)
inp = parity.load_parity_inputs(FROZEN, MAN_SHA)
store = parity.funding_store(inp)
days = list(pd.date_range("2025-11-05", "2026-06-30", freq="D", tz="UTC"))
fills = json.loads((REF / "fills.json").read_text())
vals = json.loads((REF / "valuations.json").read_text())
out = {}

# B1
TOL = {"funding_cf": 1e-9, "fees": 1e-9, "spread_slippage": 1e-9, "perp_price_pnl": 1e-9,
       "liquidation_penalty": 1e-9, "ret": 1e-10, "carry_net": 1e-10, "equity": 1e-6}
for name in ("PRIMARY", "S-COST2X"):
    log = [{**f, "penalty_rate": f["penalty"]} for f in fills if f["row"] == name]
    v = {x["day"]: x["perp"] for x in vals if x["row"] == name}
    got = parity.replay_fill_log(log, v, store, days)
    ref = pd.read_parquet(REF / f"H2-L30-k2__{name}.daily.parquet")
    if ref.index.tz is None:
        ref.index = ref.index.tz_localize("UTC")
    rep = parity.compare_daily(ref, got, TOL)
    out[f"B1.{name}"] = rep
    print("B1", name, "PASS" if rep["pass"] else "FAIL", {c: f'{x["max_abs_delta"]:.2e}' for c, x in rep["by_component"].items()})
    if not rep["pass"]:
        print(json.dumps(rep["first"], default=str)[:1500])

# B2
q = json.loads((REF / "quotes_mondays.json").read_text())
bad, n, rel_mark = [], 0, []
for r in q:
    t = pd.Timestamp(r["t"])
    mine = parity_quote(inp.perp.get(r["asset"]), inp.spreads.get(r["asset"]), t)
    n += 1
    exp = None if r["mid"] is None else (r["mid"], r["half"], r["ts"], r["fallback"])
    got = None if mine is None else (mine.mid, mine.half, mine.ts.isoformat(), mine.fallback)
    if exp is None or got is None:
        if exp != got:
            bad.append({"t": r["t"], "asset": r["asset"], "expected": exp, "actual": got})
        continue
    if not (got[0] == exp[0] and abs(got[1] - exp[1]) <= 1e-12 and got[2] == exp[2] and got[3] == exp[3]):
        bad.append({"t": r["t"], "asset": r["asset"], "expected": exp, "actual": got})
    m = inp.mark.get(r["asset"])
    if m is not None and (t - parity.BAR) in m.index:
        rel_mark.append(float(m.at[t - parity.BAR, "close"]) / exp[0] - 1)
rm = np.abs(np.array(rel_mark))
out["B2"] = {"pass": not bad, "n": n, "n_mismatch": len(bad), "first": bad[0] if bad else None,
             "primary_vs_parity_mid_rel_diff": {"n": int(len(rm)), "median_abs": float(np.median(rm)),
                                                "p95_abs": float(np.quantile(rm, 0.95)), "max_abs": float(rm.max())}}
print("B2", "PASS" if not bad else "FAIL", n, "quotes", len(bad), "mismatch;", out["B2"]["primary_vs_parity_mid_rel_diff"])

# B3
tab = pd.read_parquet(REF / "signal_table_mondays.parquet")
static = {a: parity._AssetStatic(b) for a, b in inp.perp.items()}
bad3, maxrel, cache, skipped3 = [], 0.0, {}, 0
for _, r in tab.iterrows():
    t = pd.Timestamp(r["day"])
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    if t not in cache:
        cache[t] = parity.v4_signals(inp, store, t, static)
    if r["asset"] not in cache[t]:  # asset without any input file (synthetic data only); counted
        skipped3 += 1
        continue
    s = cache[t][r["asset"]]
    fl_ok = (math.isnan(s.fl_annual) and math.isnan(r["FL30"])) or abs(s.fl_annual - r["FL30"]) <= 1e-12 * max(1.0, abs(r["FL30"]))
    if not math.isnan(r["FL30"]) and r["FL30"] != 0:
        maxrel = max(maxrel, abs(s.fl_annual / r["FL30"] - 1))
    if not (fl_ok and s.n == r["FL30_n"] and s.eligible == bool(r["elig_h2_30"])):
        bad3.append({"t": str(t), "asset": r["asset"], "expected": [r["FL30"], int(r["FL30_n"]), bool(r["elig_h2_30"])],
                     "actual": [s.fl_annual, s.n, s.eligible]})
sides_bad = []
for t, sig in cache.items():
    sub = tab[pd.to_datetime(tab["day"], utc=True) == t]
    ok = sub[sub["elig_h2_30"].astype(bool) & sub["FL30"].notna()]
    order = [a for _, a in sorted((float(v), a) for a, v in zip(ok["asset"], ok["FL30"]))]
    exp = None if len(order) < 4 else {**{a: 1 for a in order[:2]}, **{a: -1 for a in order[-2:]}}
    if parity.v4_h2_sides(sig, 2) != exp:
        sides_bad.append({"t": str(t), "expected": exp, "actual": parity.v4_h2_sides(sig, 2)})
out["B3"] = {"pass": not bad3 and not sides_bad, "rows": int(len(tab)), "mondays": len(cache), "n_mismatch": len(bad3),
             "first": bad3[0] if bad3 else None, "rows_skipped_no_input": skipped3, "fl30_max_rel_delta": maxrel, "sides_mismatch": sides_bad[:3]}
print("B3", "PASS" if out["B3"]["pass"] else "FAIL", len(tab), "rows", skipped3, "skipped,", len(bad3), "mismatch; FL30 max rel", f"{maxrel:.2e}", "sides mismatch", len(sides_bad))
(OUT / "layer_b_report.json").write_text(json.dumps(out, indent=1, default=str))
