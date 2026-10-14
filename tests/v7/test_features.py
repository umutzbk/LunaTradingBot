import math

import numpy as np
import pandas as pd
import pytest

import luna.v7.features as fx
from luna.v7.beta import beta_hat, hourly_close_from_15m, hourly_known_at_from_15m
from luna.v7.constants import FROZEN
from luna.v7.data.instances import build_instances
from luna.v7.data.intervals import annotate
from luna.v7.features import (
    CARRY_DIRECTION,
    FEATURES,
    Preprocessor,
    R4Bars,
    assert_pit,
    compute,
    compute_row,
    features_for_horizon,
    xs_input,
)
from luna.v7.spread import half_spread
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.universe import Identity, LeakageError, SymbolData, eligible
from luna.v7.windows import outer_folds

S = to_ns("2024-01-01")
Q = 15 * MIN
T = S + 70 * D + 5 * MIN  # decision 00:05
END = T + 1 * D  # rows past t exist (known later) in every frame
ID_OK = Identity("ABC", "PERPETUAL", "USDT", "COIN", 1.0, True)


def bars15(seed=0, start=S, end=END, sigma=2e-4, px0=100.0, vol_q=30e6):
    rng = np.random.default_rng(seed)
    ts = np.arange(start, end, Q, dtype=np.int64)
    c = px0 * np.exp(np.cumsum(rng.normal(0.0, sigma, len(ts))))
    o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) * (1 + 5e-5)
    lo = np.minimum(o, c) * (1 - 5e-5)
    return pd.DataFrame(
        dict(open_ts=ts, o=o, h=hi, l=lo, c=c, vol_base=10.0, vol_quote=vol_q / 96.0,
             n_trades=100.0 + (ts // D % 5), known_at=ts + Q)
    )


def rates(ts):
    return 1e-4 * np.sin(np.asarray(ts) / (3 * D)) + 2e-5


def funding(start=S, end=END, step_h=8, rate_fn=rates):
    ts = np.arange(start, end, step_h * H, dtype=np.int64)
    return pd.DataFrame(dict(funding_ts=ts, rate=rate_fn(ts),
                             interval_h_explicit=pd.array([step_h] * len(ts), dtype="Int64")))


def r4(b15, start=S, prem=lambda ts: 1e-4 * np.cos(ts / (2 * D))):
    ts = b15["open_ts"].to_numpy(np.int64)
    ts = ts[ts >= start]
    p = pd.DataFrame(dict(open_ts=ts, o=0.0, h=0.0, l=0.0, c=prem(ts), known_at=ts + Q))
    idx = pd.DataFrame(dict(open_ts=ts, o=100.0, h=100.0, l=100.0, c=100.0, known_at=ts + Q))
    mk = idx.assign(c=100.0 * (1 + prem(ts)))
    return R4Bars(premium=p, mark=mk, index=idx)


def make(symbol="ABCUSDT", seed=0, b15=None, fund=None, insts=None):
    b15 = bars15(seed) if b15 is None else b15
    insts = build_instances(b15, END) if insts is None else insts
    fa = annotate(funding() if fund is None else fund, insts)
    fa["known_at"] = fa["funding_ts"] + MIN
    b1 = pd.DataFrame(dict(open_ts=np.arange(T - 2 * H, T + H, MIN, dtype=np.int64), vol_base=1.0))
    b1["known_at"] = b1["open_ts"] + MIN
    return SymbolData(symbol, b15, b1, fa, insts, [], ID_OK)


def btc():
    return make("BTCUSDT", seed=99)


def universe(t=T, extra=()):
    rows = []
    for k, sym in enumerate(("ABCUSDT", "DEFUSDT", "GHIUSDT")):
        sd = make(sym, seed=k) if sym != "DEFUSDT" else make(
            sym, seed=1, fund=funding(rate_fn=lambda ts: -rates(ts)))
        rows.append(xs_input(sd, r4(sd.bars15), t))
    return rows + list(extra)


def base(side=1, sd=None, t=T, rr=None, rows=None):
    sd = make() if sd is None else sd
    return compute(sd, btc(), universe(t) if rows is None else rows, t, side, r4=r4(sd.bars15) if rr is None
                   else rr)


def same(a, b):
    assert list(a) == list(b)
    for k in a:
        assert (math.isnan(a[k]) and math.isnan(b[k])) or a[k] == pytest.approx(b[k], rel=1e-12, abs=0), k


EXPECTED = (
    "side",
    "f1_fl1", "f1_fl3", "f1_fl7", "f1_fl30",
    "f2_same_sign_share_30d", "f2_autocorr1_30d",
    "f3_slope_7d", "f3_fl1_minus_fl7", "f3_accel",
    "f4_sign_reversals_30d",
    "f5_premium_mean_8h", "f5_premium_mean_24h", "f5_premium_last",
    "f6_half_spread", "f6_half_spread_median_7d",
    "f8_log_median_quote_vol_30d", "f8_amihud_7d", "f8_log_trades_per_day_7d",
    "f9_rvol_24h", "f9_rvol_7d",
    "f10_ret_24h", "f10_ret_7d", "f10_ret_30d", "f10_resid_ret_7d",
    "f11_btc_ret_30d", "f11_btc_rvol_7d", "f11_xs_median_fl1", "f11_xs_share_fl1_pos",
    "f12_rank_fl1", "f12_rank_fl7", "f12_rank_fl30",
    "f12_rank_premium_mean_8h", "f12_rank_premium_mean_24h", "f12_rank_premium_last",
    "f13_rvol_over_carry", "f13_r8_std_over_fl30",
    "f14_cost_bps", "f14_carry_over_cost_24h", "f14_carry_over_cost_72h", "f14_carry_over_cost_168h",
)


def test_feature_set_exact():
    assert FEATURES == EXPECTED
    assert not any(c.startswith(("f7_", "f15_")) for c in FEATURES)
    assert set(CARRY_DIRECTION) < set(FEATURES)
    assert features_for_horizon(72) == tuple(
        c for c in EXPECTED if c not in ("f14_carry_over_cost_24h", "f14_carry_over_cost_168h"))
    with pytest.raises(ValueError):
        features_for_horizon(48)


def test_all_features_finite_in_baseline():
    v = base()
    assert list(v) == list(FEATURES)
    assert all(math.isfinite(x) for x in v.values()), {k: x for k, x in v.items() if not math.isfinite(x)}
    assert all(eligible(r.gate) for r in universe())


def _spike(df, cols, t=T):
    df = df.copy()
    m = df["known_at"] > t - MIN
    for c in cols:
        df.loc[m, c] = df.loc[m, c] * 1000.0 + 1.0
    return df


def test_future_spike_does_not_change_features():
    sd, b = make(), btc()
    rr = r4(sd.bars15)
    before = compute(sd, b, universe(), T, 1, r4=rr)
    cols = ["o", "h", "l", "c", "vol_base", "vol_quote", "n_trades"]
    f = sd.funding_annot.copy()
    f.loc[f["known_at"] > T - MIN, ["rate", "r8"]] = 1.0
    sp = SymbolData(sd.symbol, _spike(sd.bars15, cols), sd.bars1m, f, sd.insts, [], ID_OK)
    spr = R4Bars(*(_spike(x, ["c"]) for x in (rr.premium, rr.mark, rr.index)))
    bs = SymbolData(b.symbol, _spike(b.bars15, cols), b.bars1m, b.funding_annot, b.insts, [], ID_OK)
    rows = universe()
    rows[0] = xs_input(sp, spr, T)
    same(before, compute(sp, bs, rows, T, 1, r4=spr))
    # discriminating: the same spike one minute earlier (known exactly at t - 1 min) is used
    early = _spike(sd.bars15, ["c", "vol_quote"], T - 9 * MIN)  # the bar closing at 00:00, known t - 5 min
    assert compute(SymbolData(sd.symbol, early, sd.bars1m, f, sd.insts, [], ID_OK), b, rows, T, 1,
                   r4=spr)["f10_ret_24h"] != before["f10_ret_24h"]


def test_funding_features_hand_values():
    v = base(side=-1)  # short: carry features x -s = +1, i.e. raw values
    f = funding()
    f = f[(f["funding_ts"] + MIN <= T - MIN)]
    ts, r8 = f["funding_ts"].to_numpy(), f["rate"].to_numpy() * 8 / 8
    fl = {w: r8[ts > T - w * H].mean() for w in (24, 72, 168, 720)}
    assert len(r8[ts > T - 24 * H]) == 3
    assert v["f1_fl1"] == pytest.approx(fl[24]) and v["f1_fl30"] == pytest.approx(fl[720])
    assert v["f3_fl1_minus_fl7"] == pytest.approx(fl[24] - fl[168])
    assert v["f3_accel"] == pytest.approx((fl[24] - fl[72]) - (fl[72] - fl[168]))
    w7 = ts > T - 168 * H
    assert v["f3_slope_7d"] == pytest.approx(np.polyfit((ts[w7] - T) / D, r8[w7], 1)[0])
    x = r8[ts > T - 720 * H]
    assert v["f2_same_sign_share_30d"] == pytest.approx(np.mean(np.sign(x) == np.sign(fl[24])))
    assert v["f2_autocorr1_30d"] == pytest.approx(np.corrcoef(x[:-1], x[1:])[0, 1])
    assert v["f4_sign_reversals_30d"] == pytest.approx(np.sum(x[1:] * x[:-1] < 0) / len(x))
    assert v["f13_r8_std_over_fl30"] == pytest.approx(min(np.std(x, ddof=1) / abs(fl[720]), 1e4))


def test_r8_normalisation_4h_records():
    f4h = funding(step_h=4)
    v8 = base(side=-1)
    v4 = base(side=-1, sd=make(fund=f4h))
    f = f4h[f4h["funding_ts"] + MIN <= T - MIN]
    ts = f["funding_ts"].to_numpy()
    assert v4["f1_fl1"] == pytest.approx((f["rate"].to_numpy() * 2.0)[ts > T - 24 * H].mean())
    assert v4["f1_fl1"] != pytest.approx(v8["f1_fl1"])


def test_invalid_record_is_missing():
    f = funding()
    k = f.index[f["funding_ts"] >= T - 20 * H][0]
    f.loc[k, "funding_ts"] -= 2 * H  # off-schedule: invalid (S-2.6), its r8 is missing
    f.loc[k, "rate"] = 0.5
    sd = make(fund=f)
    v = base(side=-1, sd=sd)
    a = fx.pit_funding(sd.funding_annot, sd.insts[0], T)
    # the shifted record (spacing 6 h) and its successor (spacing 10 h) are invalid; FL1 keeps 00:00 only
    assert a.loc[a["invalid"], "funding_ts"].tolist() == [f.loc[k, "funding_ts"], f.loc[k + 1, "funding_ts"]]
    assert f.loc[k + 2, "funding_ts"] == T - 5 * MIN
    assert v["f1_fl1"] == pytest.approx(f.loc[k + 2, "rate"])


def test_side_adjustment_sign():
    lo, sh = base(side=1), base(side=-1)
    assert lo["side"] == 1.0 and sh["side"] == -1.0
    for c in FEATURES:
        if c == "side":
            continue
        if c in CARRY_DIRECTION:
            assert lo[c] == pytest.approx(-sh[c]) and lo[c] != 0.0, c
        else:
            assert lo[c] == sh[c], c
    # a long pays positive funding: positive raw FL1 is unfavourable -> negative for the long
    assert sh["f1_fl1"] > 0 and lo["f1_fl1"] < 0
    assert {c[:3] for c in CARRY_DIRECTION} == {"f1_", "f3_", "f5_", "f11", "f12"}
    with pytest.raises(ValueError):
        base(side=0)


def test_features_reset_at_new_instance():
    first = bars15(0, S, S + 50 * D)  # ends 20 d before t: inside every 30 d look-back
    second = bars15(5, T - 5 * MIN - 12 * D, END, px0=300.0)  # > 7 d dormancy; new instance 12 d before t
    b = pd.concat([first, second], ignore_index=True)
    insts = build_instances(b, END)
    assert len(insts) == 2
    old = funding(S, S + 50 * D, rate_fn=lambda ts: np.full(len(ts), 5e-3))  # first instance: huge rates
    new = funding(T - 5 * MIN - 12 * D, END)
    both = make(b15=b, fund=pd.concat([old, new], ignore_index=True), insts=insts)
    only = make(b15=second, fund=new, insts=build_instances(second, END))
    rr_both = r4(b)
    rr_only = r4(second)
    rows = universe()
    v_both = compute(both, btc(), rows, T, -1, r4=rr_both)
    v_only = compute(only, btc(), rows, T, -1, r4=rr_only)
    same(v_both, v_only)
    g = new[new["funding_ts"] + MIN <= T - MIN]
    assert v_both["f1_fl30"] == pytest.approx(g["rate"].mean())  # only the 12 d of the new instance
    assert math.isnan(v_both["f10_ret_30d"])  # 30 d look-back reaches before the instance: missing
    assert math.isfinite(v_both["f8_amihud_7d"])
    assert math.isfinite(v_both["f10_ret_7d"])


def test_no_current_instance_all_symbol_features_missing():
    sd = make()
    gone = SymbolData(sd.symbol, sd.bars15, sd.bars1m, sd.funding_annot, [], [], ID_OK)
    v = compute(gone, btc(), universe(), T, 1, r4=r4(sd.bars15))
    assert v["side"] == 1.0 and math.isfinite(v["f11_btc_ret_30d"])
    assert all(math.isnan(v[c]) for c in FEATURES if c.startswith(("f1_", "f6_", "f10", "f12", "f14")))


def test_price_features_hand_values():
    sd = make()
    v = base(sd=sd)
    hc = hourly_close_from_15m(sd.bars15)
    L = (T - MIN) // H * H
    assert v["f10_ret_24h"] == pytest.approx(hc[L] / hc[L - D] - 1)
    assert v["f10_ret_30d"] == pytest.approx(hc[L] / hc[L - 30 * D] - 1)
    r = np.log(hc.loc[L - 7 * D: L].to_numpy())
    r = np.diff(r)
    assert len(r) == 168
    assert v["f9_rvol_7d"] == pytest.approx(math.sqrt(np.mean(r**2) * 24 * 365))
    b = btc()
    bc = hourly_close_from_15m(b.bars15)
    beta = beta_hat(hc, bc, T, hourly_known_at_from_15m(sd.bars15), hourly_known_at_from_15m(b.bars15))
    assert v["f10_resid_ret_7d"] == pytest.approx(
        (hc[L] / hc[L - 7 * D] - 1) - beta * (bc[L] / bc[L - 7 * D] - 1))
    assert v["f11_btc_ret_30d"] == pytest.approx(bc[L] / bc[L - 30 * D] - 1)
    assert v["f13_rvol_over_carry"] == pytest.approx(
        min(v["f9_rvol_7d"] / (abs(v["f1_fl7"]) * 3 * 365), 1e4))
    # F8: 30 UTC days fully ended before t, 30e6 per day; Amihud on daily closes / quote volume
    assert v["f8_log_median_quote_vol_30d"] == pytest.approx(math.log(30e6))
    d_t = T // D
    am = [abs(hc[(d + 1) * D] / hc[d * D] - 1) / 30e6 for d in range(d_t - 7, d_t)]
    assert v["f8_amihud_7d"] == pytest.approx(np.mean(am))
    tr = sd.bars15[(sd.bars15["open_ts"] // D >= d_t - 7) & (sd.bars15["open_ts"] // D < d_t)]
    assert v["f8_log_trades_per_day_7d"] == pytest.approx(math.log(tr["n_trades"].sum() / 7))


def test_f5_premium_and_fallback():
    sd = make()
    rr = r4(sd.bars15)
    v = base(side=-1, sd=sd, rr=rr)
    p = rr.premium[(rr.premium["known_at"] <= T - MIN) & (rr.premium["open_ts"] + Q > T - 8 * H)]
    assert len(p) == 32
    assert v["f5_premium_mean_8h"] == pytest.approx(p["c"].mean())
    last = rr.premium[rr.premium["known_at"] <= T - MIN].iloc[-1]["c"]
    assert v["f5_premium_last"] == pytest.approx(last)
    # premium bars missing -> (mark - index) / index per bar
    no_p = R4Bars(rr.premium.iloc[0:0], rr.mark, rr.index)
    v2 = base(side=-1, sd=sd, rr=no_p, rows=[xs_input(sd, no_p, T)] + universe()[1:])
    assert v2["f5_premium_mean_8h"] == pytest.approx(p["c"].mean(), rel=1e-9)
    # neither -> missing
    none = R4Bars(rr.premium.iloc[0:0], rr.mark.iloc[0:0], rr.index)
    v3 = base(side=-1, sd=sd, rr=none, rows=[xs_input(sd, none, T)] + universe()[1:])
    assert math.isnan(v3["f5_premium_mean_8h"]) and math.isnan(v3["f12_rank_premium_last"])


def test_f6_spread_and_7d_median():
    sd = make()
    v = base(sd=sd)
    assert v["f6_half_spread"] == half_spread(sd.bars15, T)
    vals = [half_spread(sd.bars15, T - k * 8 * H) for k in range(21)]
    assert v["f6_half_spread_median_7d"] == pytest.approx(np.median(vals))


def test_f11_f12_over_eligible_universe():
    rows = universe()
    fl1 = {r.symbol: r.values["fl1"] for r in rows}
    v = base(side=-1, rows=rows)
    assert v["f11_xs_median_fl1"] == pytest.approx(np.median(list(fl1.values())))
    assert v["f11_xs_share_fl1_pos"] == pytest.approx(np.mean([x > 0 for x in fl1.values()]))
    own = fl1["ABCUSDT"]
    rank = (sum(x < own for x in fl1.values()) + (sum(x == own for x in fl1.values()) + 1) / 2) / 3
    assert v["f12_rank_fl1"] == pytest.approx(rank)
    # an ineligible symbol does not enter the cross-section
    bad = make("JKLUSDT", fund=funding(rate_fn=lambda ts: np.full(len(ts), 9e-3)))
    bad = SymbolData(bad.symbol, bad.bars15, bad.bars1m, bad.funding_annot, bad.insts, [], None)
    row_bad = xs_input(bad, r4(bad.bars15), T)
    assert not eligible(row_bad.gate)
    same(v, base(side=-1, rows=rows + [row_bad]))
    with pytest.raises(ValueError):
        base(rows=rows + [xs_input(make("XYZUSDT"), r4(make().bars15), T - 8 * H)])  # other decision time
    with pytest.raises(ValueError):
        base(rows=rows + rows[:1])  # duplicate symbol


def test_f13_caps_1e4():
    zero = make(fund=funding(rate_fn=lambda ts: np.zeros(len(ts))))
    v = base(sd=zero)
    assert v["f13_rvol_over_carry"] == FROZEN.features.f13_cap == 1e4
    assert math.isnan(v["f13_r8_std_over_fl30"])  # 0 / 0 is undefined, not capped
    tiny = make(fund=funding(rate_fn=lambda ts: np.full(len(ts), 1e-12)))
    assert base(sd=tiny)["f13_rvol_over_carry"] == 1e4
    alt = make(fund=funding(rate_fn=lambda ts: 1e-4 * np.where(ts // (8 * H) % 2 == 0, 1.0, -1.0) + 1e-12))
    assert base(sd=alt)["f13_r8_std_over_fl30"] == 1e4  # |FL30| ~ 1e-12 over an even record count
    vn = base()
    assert 0 < vn["f13_rvol_over_carry"] < 1e4


def test_f14_cost_formula():
    sd = make()
    v = base(sd=sd)
    h = half_spread(sd.bars15, T)
    cost = (2 * 5e-4 + 2 * h + 2 * 2e-4) * 1e4
    assert v["f14_cost_bps"] == pytest.approx(cost)
    for hh in (24, 72, 168):
        assert v[f"f14_carry_over_cost_{hh}h"] == pytest.approx(abs(v["f1_fl7"]) * 3 * hh / 24 / (cost / 1e4))


def test_assert_pit():
    df = pd.DataFrame(dict(known_at=[T - 2 * MIN, T - MIN]))
    assert assert_pit({"x": df}, T) == T - MIN
    with pytest.raises(LeakageError):
        assert_pit({"x": pd.DataFrame(dict(known_at=[T - MIN + 1]))}, T)
    with pytest.raises(ValueError):
        assert_pit({"x": pd.DataFrame(dict(a=[1]))}, T)
    row = compute_row(make(), btc(), universe(), T, 1, r4=r4(make().bars15))
    assert row.max_known_at is not None and row.max_known_at <= T - MIN


def test_known_at_required():
    sd = make()
    rr = r4(sd.bars15)
    with pytest.raises(ValueError):
        compute(sd, btc(), universe(), T, 1,
                r4=R4Bars(rr.premium.drop(columns="known_at"), rr.mark, rr.index))
    nb = SymbolData(sd.symbol, sd.bars15.drop(columns="known_at"), sd.bars1m, sd.funding_annot, sd.insts)
    with pytest.raises(ValueError):
        compute(nb, btc(), universe(), T, 1, r4=rr)


# ---------------------------------------------------------------------------- preprocessing (L5, RF5)

FOLD = outer_folds(to_ns("2021-01-01"))[0]


def _frame(ts):
    rng = np.random.default_rng(1)
    X = pd.DataFrame(rng.normal(size=(len(ts), 3)), columns=["a", "b", "c"])
    X.loc[0, "a"] = 1e6  # outlier: winsorized at the fit 99 % quantile
    return X


def test_preprocess_fit_only_on_fit_rows():
    t_fit = FOLD.tr[0] + np.arange(200, dtype=np.int64) * D
    X = _frame(t_fit)
    p = Preprocessor(["a", "b", "c"]).fit(X, t_fit, FOLD)
    for bad in (FOLD.cal[0], FOLD.cal[1] - 1, FOLD.test_lo, FOLD.tr[0] - 1):
        tb = t_fit.copy()
        tb[-1] = bad
        with pytest.raises(LeakageError):
            Preprocessor(["a", "b", "c"]).fit(X, tb, FOLD)
    with pytest.raises(ValueError):
        Preprocessor(["a", "b", "c"]).fit(X, t_fit[:-1], FOLD)
    with pytest.raises(RuntimeError):
        Preprocessor(["a"]).transform(X)
    # parameters come from the fit rows only: transforming other rows never changes them
    Z1 = p.transform(X)
    p.transform(X * 50.0)
    pd.testing.assert_frame_equal(Z1, p.transform(X))
    q99 = np.quantile(X["a"], 0.99)
    w = X["a"].clip(np.quantile(X["a"], 0.01), q99)
    assert Z1.loc[0, "a"] == pytest.approx((q99 - w.mean()) / w.std(ddof=0))
    assert list(Z1.columns) == ["a", "b", "c", "a__missing", "b__missing", "c__missing"]
    assert p.transform(pd.DataFrame(dict(a=[1e9], b=[0.0], c=[0.0])))["a"].iloc[0] == Z1.loc[0, "a"]


def test_preprocess_constant_and_nan_columns():
    t_fit = FOLD.tr[0] + np.arange(50, dtype=np.int64) * D
    X = pd.DataFrame(dict(const=np.full(50, 0.1), nan=np.nan, mix=np.arange(50.0)))
    X.loc[3, "mix"] = np.nan
    X.loc[4, "mix"] = np.inf
    p = Preprocessor(["const", "nan", "mix"]).fit(X, t_fit, FOLD)
    Z = p.transform(X)
    assert np.isfinite(Z.to_numpy()).all()
    assert (Z["const"] == 0.0).all() and (Z["nan"] == 0.0).all() and (Z["nan__missing"] == 1.0).all()
    assert Z.loc[3, "mix__missing"] == Z.loc[4, "mix__missing"] == 1.0 and Z["mix__missing"].sum() == 2
    fin = np.delete(np.arange(50.0), [3, 4])
    w = np.clip(fin, *np.quantile(fin, [0.01, 0.99]))
    z = (w - w.mean()) / w.std()
    assert Z.loc[3, "mix"] == pytest.approx(np.median(z)) and Z.loc[4, "mix"] == Z.loc[3, "mix"]
    # prediction-time NaN / inf / new constant values are imputed, never propagated
    Zp = p.transform(pd.DataFrame(dict(const=[np.nan, 5.0], nan=[1.0, np.nan], mix=[-np.inf, 10.0])))
    assert np.isfinite(Zp.to_numpy()).all() and Zp.loc[1, "const"] == 0.0


def test_lightgbm_raw_matrix_keeps_nan():
    X = pd.DataFrame({c: [1.0, np.nan] for c in FEATURES})
    R = fx.raw_matrix(X)
    assert list(R.columns) == list(FEATURES) and np.isnan(R.iloc[1, 0])
