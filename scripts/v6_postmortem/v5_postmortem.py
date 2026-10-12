"""V5 profitability postmortem: diagnostics on the committed single-run tables and the frozen
development-period inputs only. No replay, no new signal tested, nothing at or after 2026-07-01."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TAB = Path(r"C:\Users\umutzbk\Documents\LunaTradingBot-v5\docs\results\v5-backtest\tables")
FZ = Path(r"C:\Users\umutzbk\Documents\LunaTradingBot\var\history_v5\frozen")
OUT = Path(sys.argv[1])
CUTOFF = pd.Timestamp("2026-07-01", tz="UTC")
START = pd.Timestamp("2025-11-10", tz="UTC")
EQ0 = 265.0
U = ["ADA", "AVAX", "BNB", "BTC", "DOGE", "ENA", "ETH", "FET", "HYPE", "LINK", "NEAR", "PEPE", "PUMP", "SOL",
     "SUI", "TAO", "UNI", "XLM", "XRP", "ZEC"]
R = {}


def r6(x):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else round(float(x), 6)


# ---------------- inputs (dev period only)
px = {}
fr = {}
for a in U:
    m = pd.read_parquet(FZ / "history_v4" / "mark" / f"{a}.parquet")["close"]
    m.index = m.index + pd.Timedelta("15min")  # builder: mid at t = close of the bar opened at t-15m
    m = m[(m.index >= pd.Timestamp("2025-09-01", tz="UTC")) & (m.index <= CUTOFF)]
    px[a] = m
    f = pd.read_parquet(FZ / "history_v4" / "funding" / f"{a}.parquet")["funding_rel"]
    assert f.index.max() < CUTOFF
    fr[a] = f
P = pd.DataFrame(px)
P = P[P.index <= CUTOFF]
assert P.index.max() <= CUTOFF
Ph = P[P.index.minute == 0]  # hourly prices
F = pd.DataFrame(fr).sort_index()  # hourly funding by period start
hours = pd.date_range(F.index.min(), CUTOFF - pd.Timedelta("1h"), freq="h")
F = F.reindex(hours)


def fl30(t):
    """PIT FL30 at t: periods starting in [t-720h, t-1h] (ended by t); coverage >= 648."""
    w = F.loc[t - pd.Timedelta("720h"): t - pd.Timedelta("1h")]
    n = w.notna().sum()
    return w.mean().where(n >= 648)


def realized(t, h):
    """Mean funding of periods starting in [t, t+h) (diagnostic: forward-looking on purpose)."""
    return F.loc[t: t + pd.Timedelta(hours=h) - pd.Timedelta("1h")].mean()


# ---------------- legs from the single-run tables
def legs_of(arm, row):
    f = pd.read_parquet(TAB / f"{arm}_{row.replace('/', '_')}_fills.parquet")
    s = pd.read_parquet(TAB / f"{arm}_{row.replace('/', '_')}_settlements.parquet")
    out = []
    for pid, g in f.groupby("position_id"):
        g = g.sort_values("ts")
        side = 1 if ":L:" in pid else -1
        a = g["asset"].iloc[0]
        q_end = g["dq"].sum()
        o, c = g["ts"].iloc[0], g["ts"].iloc[-1]
        price = float((-g["dq"] * g["mid_fill"]).sum())
        if abs(q_end) > 1e-12:
            price += q_end * float(P[a].asof(CUTOFF))
            c = CUTOFF
        out.append(dict(pid=pid, asset=a, side=side, open=o, close=c, dec=g["decision_ts"].iloc[0],
                        hours=(c - o).total_seconds() / 3600, notional=float(g["notional"].iloc[0]),
                        price=price, drift=float(g["delay_drift"].sum()), fees=float(g["fee"].sum()),
                        ss=float(g["spread_slip"].sum()), n_fills=len(g),
                        exit_reason=None))
    L = pd.DataFrame(out)
    s = s.copy()
    s["pend"] = s["period_start"] + pd.Timedelta("1h")
    L["funding"] = 0.0
    for i, l in L.iterrows():
        m = (s["asset"] == l["asset"]) & (s["period_start"] < l["close"]) & (s["pend"] > l["open"])
        L.at[i, "funding"] = float(s.loc[m, "amount"].sum())
    R.setdefault("checks", {})[f"{arm}/{row}"] = {
        "settlement_total": r6(s["amount"].sum()), "legs_funding_total": r6(L["funding"].sum()),
        "legs_price_total": r6(L["price"].sum())}
    return L, f, s


def exit_reasons(arm, row, L):
    rec = pd.read_parquet(TAB / f"{arm}_{row.replace('/', '_')}_records.parquet")
    ex = rec[rec["decision"].isin(["EXIT", "SWITCH"])]
    for i, l in L.iterrows():
        m = ex[(ex["ts"] <= l["close"]) & (ex["ts"] >= l["close"] - pd.Timedelta("30min")) &
               ((ex["asset_out"] == l["asset"]) | ex["asset_out"].isna())]
        L.at[i, "exit_reason"] = ";".join(sorted(set(m["reason"].astype(str)))) if len(m) else (
            "open_at_end" if l["close"] == CUTOFF else "?")
    return L, rec


ARMROWS = [("E", "PRIMARY"), ("E", "S-NOKILL"), ("W", "S-NOKILL"), ("D", "S-NOKILL"), ("C", "S-NOKILL"),
           ("W", "PRIMARY"), ("C", "PRIMARY"), ("D", "PRIMARY")]
LEGS = {}
for arm, row in ARMROWS:
    L, f, s = legs_of(arm, row)
    L, rec = exit_reasons(arm, row, L)
    # PIT FL30 at the decision and forward realized funding
    exp, r24, r72, r168, rhold = [], [], [], [], []
    for _, l in L.iterrows():
        t = l["dec"].floor("h")
        fl = fl30(t)[l["asset"]]
        exp.append(-l["side"] * fl)
        r24.append(-l["side"] * realized(t, 24)[l["asset"]])
        r72.append(-l["side"] * realized(t, 72)[l["asset"]])
        r168.append(-l["side"] * realized(t, 168)[l["asset"]])
        hh = max(1, int(round(l["hours"])))
        rhold.append(-l["side"] * F.loc[l["open"].floor("h"): l["close"].floor("h"), l["asset"]].mean())
    L["exp_c"], L["r24"], L["r72"], L["r168"], L["r_hold"] = exp, r24, r72, r168, rhold
    LEGS[(arm, row)] = (L, f, s, rec)

# ---------------- Q1: E entries / switches
Lp, fp, sp, recp = LEGS[("E", "PRIMARY")]
hold = recp[recp["decision"] == "HOLD"]
eb = hold[hold["reason"] == "entry_carry_below_cost"]
ce = hold[hold["reason"] == "cost_exceeds_advantage"]
R["Q1"] = {
    "decision_counts": {f"{k[0]}:{k[1]}": int(v) for k, v in recp.groupby(["decision", "reason"]).size()
                        .sort_values(ascending=False).head(15).items()},
    "entry_below_cost": {"n": len(eb), "median_expected_usd": r6(eb["expected_carry"].median()),
                         "median_cost_usd": r6((eb["switch_cost"] + eb["safety_buffer"]).median()),
                         "median_ratio": r6((eb["expected_carry"] / (eb["switch_cost"] + eb["safety_buffer"])).median()),
                         "share_ratio_gt_0.75": r6(((eb["expected_carry"] / (eb["switch_cost"] + eb["safety_buffer"])) > 0.75).mean())},
    "switch_cost_exceeds": {"n": len(ce), "median_incremental_usd": r6(ce["incremental_carry"].median()),
                            "median_cost_usd": r6(ce["switch_cost"].median()),
                            "median_ratio": r6((ce["incremental_carry"] / ce["switch_cost"]).median()),
                            "share_ratio_gt_1": r6((ce["incremental_carry"] > ce["switch_cost"]).mean())},
    "legs": Lp[["asset", "side", "open", "close", "hours", "notional", "funding", "price", "fees", "ss",
                "exit_reason", "exp_c", "r_hold"]].assign(
        open=lambda d: d["open"].astype(str), close=lambda d: d["close"].astype(str)).round(6).to_dict("records"),
}
# hurdle: c_s + c_l needed for an ENTER pair over H = 168 at typical cost
ent = recp[recp["decision"] == "ENTER"]
R["Q1"]["enter_records"] = ent[["ts", "asset_in", "expected_carry", "switch_cost", "safety_buffer", "value"]].assign(
    ts=lambda d: d["ts"].astype(str)).round(6).to_dict("records")

# ---------------- positions -> hourly price P&L, exposures
def positions(f):
    q = f.pivot_table(index="ts", columns="asset", values="dq", aggfunc="sum").fillna(0.0).cumsum()
    q = q.reindex(Ph.index[(Ph.index >= START) & (Ph.index <= CUTOFF)], method="ffill").fillna(0.0)
    return q  # quantity held during the hour after each timestamp (fills at :15 snapped)


rets = Ph.pct_change()
logp = np.log(Ph)


def pit_beta_vol(t):
    w = rets.loc[t - pd.Timedelta("720h"): t - pd.Timedelta("1h")]
    b = w.cov()["BTC"] / w["BTC"].var()
    v = w.std() * np.sqrt(24 * 365)
    return b, v


days = pd.date_range(START, CUTOFF - pd.Timedelta("1D"), freq="D")
BV = {d: pit_beta_vol(d) for d in days}


def exposure_report(arm, row):
    L, f, s, rec = LEGS[(arm, row)]
    q = positions(f)
    q = q.reindex(columns=U, fill_value=0.0)
    dP = Ph.diff().shift(-1).reindex(q.index)[U]
    pnl_h = (q * dP).sum(axis=1)
    d = pd.read_parquet(TAB / f"{arm}_{row.replace('/', '_')}_daily.parquet").set_index("day")
    eq = d["equity"]
    notional = q * Ph.reindex(q.index)[U]
    rows = []
    for t in q.index[q.index.hour == 0]:
        b, v = BV[t.normalize()] if t.normalize() in BV else BV[days[-1]]
        n = notional.loc[t]
        e = float(eq.asof(t)) if len(eq[eq.index <= t]) else EQ0
        if n.abs().sum() == 0:
            rows.append(dict(t=t, gross=0.0))
            continue
        rows.append(dict(t=t, gross=n.abs().sum() / e, net=n.sum() / e, beta=(n * b[U]).sum() / e,
                         vol_signed=(n * v[U]).sum() / e,
                         long_vol=float((v[U][n > 0]).mean()) if (n > 0).any() else np.nan,
                         short_vol=float((v[U][n < 0]).mean()) if (n < 0).any() else np.nan,
                         long_beta=float((b[U][n > 0]).mean()) if (n > 0).any() else np.nan,
                         short_beta=float((b[U][n < 0]).mean()) if (n < 0).any() else np.nan))
    X = pd.DataFrame(rows).set_index("t")
    inv = X[X["gross"] > 0]
    # daily price P&L vs BTC
    dp = pnl_h.resample("D").sum()
    btc = Ph["BTC"].resample("D").last().pct_change().shift(-1).reindex(dp.index)  # same-day close-to-close
    btc = (Ph["BTC"].shift(-24) / Ph["BTC"] - 1).reindex(dp.index)
    gross_d = X["gross"].reindex(dp.index).fillna(0.0)
    m = (gross_d > 0) & btc.notna() & dp.notna()
    y, x = dp[m], (btc[m] * EQ0)
    beta_real = float(np.polyfit(x, y, 1)[0]) if m.sum() > 10 else np.nan
    r2 = float(np.corrcoef(x, y)[0, 1] ** 2) if m.sum() > 10 else np.nan
    # P&L split by side
    side_pnl = {}
    for sd, name in ((1, "long"), (-1, "short")):
        side_pnl[name] = r6(L.loc[L["side"] == sd, "price"].sum())
        side_pnl[name + "_funding"] = r6(L.loc[L["side"] == sd, "funding"].sum())
    return {
        "hourly_price_pnl_sum": r6(pnl_h.sum()), "reported_price_approx_check": "hourly snap of 15m fills",
        "invested_days": int(m.sum()),
        "mean_gross": r6(inv["gross"].mean()), "mean_abs_net": r6(inv["net"].abs().mean()),
        "max_abs_net": r6(inv["net"].abs().max()),
        "mean_beta_exposure": r6(inv["beta"].mean()), "mean_abs_beta_exposure": r6(inv["beta"].abs().mean()),
        "mean_vol_signed": r6(inv["vol_signed"].mean()), "mean_abs_vol_signed": r6(inv["vol_signed"].abs().mean()),
        "mean_long_vol": r6(inv["long_vol"].mean()), "mean_short_vol": r6(inv["short_vol"].mean()),
        "mean_long_beta": r6(inv["long_beta"].mean()), "mean_short_beta": r6(inv["short_beta"].mean()),
        "realized_beta_slope": r6(beta_real), "r2_btc": r6(r2),
        "daily_price_pnl_std_usd": r6(y.std()), "daily_price_pnl_std_pct": r6(y.std() / EQ0),
        "side_pnl": side_pnl,
    }, pnl_h, X


EXP = {}
for ar in [("E", "PRIMARY"), ("E", "S-NOKILL"), ("W", "S-NOKILL"), ("D", "S-NOKILL"), ("C", "S-NOKILL")]:
    rep, pnl_h, X = exposure_report(*ar)
    EXP[ar] = (pnl_h, X)
    R.setdefault("Q5_Q6_exposure", {})[f"{ar[0]}/{ar[1]}"] = rep

# ---------------- Q4: drawdown into the 2026-04-14 kill (E PRIMARY)
d = pd.read_parquet(TAB / "E_PRIMARY_daily.parquet").set_index("day")
eqh = None
pnl_h, X = EXP[("E", "PRIMARY")]
fp = LEGS[("E", "PRIMARY")][1]
q = positions(fp).reindex(columns=U, fill_value=0.0)
kill = pd.Timestamp("2026-04-14 07:15", tz="UTC")
pre = d[d.index < kill.normalize()]
peak_day = pre["equity"].idxmax()
w0, w1 = peak_day, kill.floor("h")
dP = Ph.diff().shift(-1).reindex(q.index)[U]
win = (q.index >= w0) & (q.index < w1)
contrib = (q[win] * dP[win]).sum()
held = q[win].abs().sum()
legs_in = Lp[(Lp["open"] < w1) & (Lp["close"] > w0)]
moves = {a: r6(float(Ph[a].asof(w1) / Ph[a].asof(w0) - 1)) for a in contrib.index[held > 0]}
R["Q4_kill"] = {
    "peak_day": str(peak_day), "peak_equity": r6(pre["equity"].max()), "kill_ts": str(kill),
    "price_pnl_by_asset_in_window": {a: r6(v) for a, v in contrib[held > 0].items()},
    "asset_moves_peak_to_kill": moves, "btc_move": r6(float(Ph["BTC"].asof(w1) / Ph["BTC"].asof(w0) - 1)),
    "funding_in_window": r6(LEGS[("E", "PRIMARY")][2].pipe(
        lambda s: s[(s["period_start"] >= w0) & (s["period_start"] < w1)]["amount"].sum())),
    "legs": legs_in[["asset", "side", "open", "close", "notional", "price", "funding", "exit_reason"]]
    .assign(open=lambda x: x["open"].astype(str), close=lambda x: x["close"].astype(str)).round(4).to_dict("records"),
    "equity_path": {str(k): r6(v) for k, v in d.loc[(d.index >= w0 - pd.Timedelta("3D")) & (d.index <= kill + pd.Timedelta("1D")), "equity"].items()},
}

# ---------------- Q7: per-asset funding vs price
for ar in [("E", "PRIMARY"), ("E", "S-NOKILL"), ("W", "S-NOKILL"), ("D", "S-NOKILL"), ("C", "S-NOKILL")]:
    L = LEGS[ar][0]
    g = L.groupby(["asset", "side"]).agg(legs=("pid", "count"), hours=("hours", "sum"), funding=("funding", "sum"),
                                         price=("price", "sum"), costs=("fees", "sum"), ss=("ss", "sum"))
    g["costs"] = g["costs"] + g["ss"]
    g = g.drop(columns="ss").round(3).reset_index()
    R.setdefault("Q7_assets", {})[f"{ar[0]}/{ar[1]}"] = g.to_dict("records")

# ---------------- Q8/Q9/Q10: forecast vs realized (legs pooled, and the universe FL30 book)
pool = []
for ar, (L, *_r) in LEGS.items():
    if ar[1] == "S-NOKILL":
        pool.append(L.assign(arm=ar[0]))
PL = pd.concat(pool)
uniq = PL.drop_duplicates(["asset", "side", "dec"])


def ratio(df, col):
    e = df["exp_c"].sum()
    return r6(df[col].sum() / e) if e else None


R["Q8_legs"] = {
    "n_legs_unique": len(uniq),
    "mean_exp_c_annual": r6(uniq["exp_c"].mean() * 8760), "mean_r24_annual": r6(uniq["r24"].mean() * 8760),
    "mean_r72_annual": r6(uniq["r72"].mean() * 8760), "mean_r168_annual": r6(uniq["r168"].mean() * 8760),
    "mean_r_hold_annual": r6(uniq["r_hold"].mean() * 8760),
    "ratio_r24": ratio(uniq, "r24"), "ratio_r72": ratio(uniq, "r72"), "ratio_r168": ratio(uniq, "r168"),
    "ratio_hold": ratio(uniq, "r_hold"),
    "by_side": {s: {"n": int((uniq["side"] == s).sum()), "exp": r6(uniq.loc[uniq["side"] == s, "exp_c"].mean() * 8760),
                    "r168": r6(uniq.loc[uniq["side"] == s, "r168"].mean() * 8760),
                    "hold": r6(uniq.loc[uniq["side"] == s, "r_hold"].mean() * 8760)} for s in (1, -1)},
    "share_r168_sign_flipped": r6((uniq["r168"] < 0).mean()),
}
# leg age buckets: realized funding per hour by age since entry
ages = [(0, 24), (24, 72), (72, 168), (168, 336), (336, 720), (720, 5000)]
rows = []
for _, l in uniq.iterrows():
    t0 = l["open"].floor("h")
    for a0, a1 in ages:
        s0, s1 = t0 + pd.Timedelta(hours=a0), min(t0 + pd.Timedelta(hours=a1), l["close"].floor("h"))
        if s1 <= s0:
            continue
        v = F.loc[s0: s1 - pd.Timedelta("1h"), l["asset"]]
        rows.append(dict(bucket=f"{a0}-{a1}h", rate=-l["side"] * v.mean(), n=v.notna().sum(), exp=l["exp_c"]))
A = pd.DataFrame(rows)
A["w"] = A["n"]
agg = A.groupby("bucket", sort=False).apply(
    lambda g: pd.Series({"legs": len(g), "hours": g["n"].sum(),
                         "realized_annual": (g["rate"] * g["w"]).sum() / g["w"].sum() * 8760,
                         "expected_annual": (g["exp"] * g["w"]).sum() / g["w"].sum() * 8760}))
R["Q9_age_decay_legs"] = agg.round(4).reset_index().to_dict("records")

# universe-wide ideal FL30 top-2/bottom-2 book at each day 00:00 (eligibility gates ignored)
U_rows = []
for t in pd.date_range(START, CUTOFF - pd.Timedelta("8D"), freq="D"):
    fl = fl30(t).dropna()
    if len(fl) < 4:
        continue
    srt = fl.sort_values()
    longs, shorts = list(srt.index[:2]), list(srt.index[-2:])
    exp = (fl[shorts].sum() - fl[longs].sum()) / 4
    row = {"t": t, "exp": exp, "fl_spread": fl[shorts].mean() - fl[longs].mean()}
    for h in (24, 72, 168):
        rz = realized(t, h)
        row[f"r{h}"] = (rz[shorts].sum() - rz[longs].sum()) / 4
        p0 = Ph.loc[t]
        p1 = Ph.loc[t + pd.Timedelta(hours=h)]
        ret = p1 / p0 - 1
        row[f"px{h}"] = (ret[longs].sum() - ret[shorts].sum()) / 4  # per unit gross, equal notional
        row[f"btc{h}"] = ret["BTC"]
        row[f"long_ret{h}"] = ret[longs].mean()
        row[f"short_ret{h}"] = ret[shorts].mean()
        # cross-sectional rank correlation FL30 vs realized
        both = pd.concat([fl, rz.reindex(fl.index)], axis=1).dropna()
        row[f"ic{h}"] = both.iloc[:, 0].rank().corr(both.iloc[:, 1].rank())
        # price: does FL30 rank predict price return (cross-section)?
        both2 = pd.concat([fl, ret.reindex(fl.index)], axis=1).dropna()
        row[f"pic{h}"] = both2.iloc[:, 0].rank().corr(both2.iloc[:, 1].rank())
    U_rows.append(row)
UB = pd.DataFrame(U_rows).set_index("t")
R["Q10_universe_book"] = {
    "days": len(UB),
    "mean_expected_annual_per_gross": r6(UB["exp"].mean() * 8760),
    **{f"mean_realized_{h}h_annual": r6(UB[f"r{h}"].mean() * 8760) for h in (24, 72, 168)},
    **{f"ratio_{h}h": r6(UB[f"r{h}"].mean() / UB["exp"].mean()) for h in (24, 72, 168)},
    **{f"mean_rank_ic_{h}h": r6(UB[f"ic{h}"].mean()) for h in (24, 72, 168)},
    **{f"mean_price_rank_ic_{h}h": r6(UB[f"pic{h}"].mean()) for h in (24, 72, 168)},
    **{f"book_price_ret_{h}h_mean": r6(UB[f"px{h}"].mean()) for h in (24, 72, 168)},
    **{f"book_price_ret_{h}h_std": r6(UB[f"px{h}"].std()) for h in (24, 72, 168)},
    **{f"book_funding_{h}h_mean": r6(UB[f"r{h}"].mean() * h) for h in (24, 72, 168)},
    "noise_to_carry_168h": r6(UB["px168"].std() / (UB["r168"].mean() * 168)),
    "corr_book_px168_btc168": r6(UB["px168"].corr(UB["btc168"])),
    "long_basket_ret168_mean": r6(UB["long_ret168"].mean()), "short_basket_ret168_mean": r6(UB["short_ret168"].mean()),
    "share_days_realized168_lt_half_expected": r6((UB["r168"] < 0.5 * UB["exp"]).mean()),
    "by_month": UB.resample("ME").agg({"exp": "mean", "r168": "mean", "px168": "mean"}).mul(
        [8760, 8760, 1]).round(4).rename(index=str).to_dict("index"),
}
# entry hurdle (E PRIMARY costs): required (c_s + c_l) per hour for ENTER value > 0 at N = 66.25
N = 0.25 * EQ0
eb_cost = (eb["switch_cost"] + eb["safety_buffer"]).median()
need = eb_cost / (N * 168)
R["Q1"]["hurdle"] = {"median_entry_cost_usd": r6(eb_cost),
                     "required_cs_plus_cl_per_hour": r6(need),
                     "required_pair_spread_annual": r6(need * 8760),
                     "universe_book_fl_spread_annual_median": r6(UB["fl_spread"].median() * 8760 * 2),
                     "note": "pair = one short + one long; c_s + c_l = FL_s - FL_l"}
# pair c_s+c_l available in universe (best pair) per day
best = []
for t in UB.index:
    fl = fl30(t).dropna().sort_values()
    best.append(fl.iloc[-1] - fl.iloc[0])
R["Q1"]["hurdle"]["best_pair_annual_median"] = r6(np.median(best) * 8760)
R["Q1"]["hurdle"]["share_days_best_pair_above_hurdle"] = r6(np.mean(np.array(best) > need))
OUT.write_text(json.dumps(R, indent=1, default=str), encoding="utf-8")
print("ok", OUT)
