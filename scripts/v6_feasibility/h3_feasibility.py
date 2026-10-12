"""Outcome-blind H3 mechanical feasibility (red-team M1/M2): risk inputs only, dates < 2026-07-01.
Selection uses FL30 rank (H2a is a monotone transform of FL30 when b > 0). No P&L, no returns forward
of t are used. Approximations: eligibility = funding coverage + 60 d of marks (spread/volume gates
ignored); LW constant-correlation shrinkage with the analytic intensity on standardised returns."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FZ = Path(r"C:\Users\umutzbk\Documents\LunaTradingBot\var\history_v5\frozen")
CUT = pd.Timestamp("2026-07-01", tz="UTC")
U = ["ADA", "AVAX", "BNB", "BTC", "DOGE", "ENA", "ETH", "FET", "HYPE", "LINK", "NEAR", "PEPE", "PUMP", "SOL",
     "SUI", "TAO", "UNI", "XLM", "XRP", "ZEC"]
inst = json.load(open(FZ / "config" / "v4_instruments.json"))["assets"]
P, F = {}, {}
for a in U:
    m = pd.read_parquet(FZ / "history_v4" / "mark" / f"{a}.parquet")["close"]
    m.index = m.index + pd.Timedelta("15min")
    P[a] = m[(m.index >= pd.Timestamp("2025-08-01", tz="UTC")) & (m.index <= CUT)]
    F[a] = pd.read_parquet(FZ / "history_v4" / "funding" / f"{a}.parquet")["funding_rel"]
P = pd.DataFrame(P)
P = P[P.index.minute == 0]
assert P.index.max() <= CUT
R = np.log(P).diff()
F = pd.DataFrame(F).sort_index()
hl = 336
lam = 0.5 ** (1 / hl)


def ewma_vol(r):
    x = r.dropna()
    if len(x) < 30 * 24:
        return np.nan
    v = x.iloc[:720].var()
    for val in x.iloc[720:].values:
        v = lam * v + (1 - lam) * val * val
    return np.sqrt(v * 8760)


def lw_cc(X):
    """Ledoit-Wolf 2004 constant-correlation shrinkage of the correlation matrix (standardised X)."""
    X = (X - X.mean()) / X.std()
    X = X.values
    T, N = X.shape
    S = np.cov(X, rowvar=False, bias=True)
    r = (S.sum() - np.trace(S)) / (N * (N - 1))
    Ftgt = np.full((N, N), r)
    np.fill_diagonal(Ftgt, 1.0)
    Y = X - X.mean(0)
    pi = ((Y ** 2).T @ (Y ** 2)) / T - S ** 2
    pihat = pi.sum()
    gamma = ((Ftgt - S) ** 2).sum()
    rho = np.trace(pi)  # simplified rho (diagonal part); off-diagonal term omitted (approximation)
    kappa = (pihat - rho) / gamma if gamma > 0 else 1.0
    d = float(np.clip(kappa / T, 0, 1))
    C = d * Ftgt + (1 - d) * S
    return C, d


# EWMA vol series (pandas recursion; data from 2025-08-01, first use 2025-12-05 = ~12 half-lives: init negligible)
VOL = {a: np.sqrt((R[a] ** 2).ewm(alpha=1 - lam, adjust=False, ignore_na=True).mean() * 8760) for a in U}


def run(k, cap_usd, sigma_star, ratio_rule):
    rows = []
    for t in pd.date_range("2025-12-05", "2026-06-30", freq="D", tz="UTC"):
        w = F.loc[t - pd.Timedelta("720h"): t - pd.Timedelta("1h")]
        fl = w.mean().where(w.notna().sum() >= 648).dropna()
        hist = R.loc[t - pd.Timedelta("60D"): t - pd.Timedelta("1h")]
        ok = [a for a in fl.index if hist[a].notna().sum() >= 60 * 24 * 0.9]
        fl = fl[ok].sort_values(ascending=False)
        Ne = len(fl)
        kp = min(k, Ne // 2)
        if kp < 3:
            rows.append(dict(t=t, status="INFEASIBLE_k"))
            continue
        S_, L_ = list(fl.index[:kp]), list(fl.index[-kp:])
        sig = {a: float(VOL[a].loc[:t - pd.Timedelta("1h")].iloc[-1]) for a in set(S_ + L_ + ["BTC"])}
        names = sorted(set(S_ + L_ + ["BTC"]))
        C, d = lw_cc(hist[names].dropna())
        sv = np.array([sig[a] for a in names])
        Sig = C * np.outer(sv, sv)
        idx = {a: i for i, a in enumerate(names)}
        L, S = list(L_), list(S_)
        px = P.loc[:t].iloc[-1]
        status = None
        for _ in range(2 * kp + 1):
            if len(L) < 3 or len(S) < 3:
                status = "INFEASIBLE_drop"
                break
            u = {}
            for side in (L, S):
                inv = {a: 1 / sig[a] for a in side}
                z = sum(inv.values())
                u.update({a: inv[a] / z for a in side})
            uL = np.array([u[a] for a in L]); uS = np.array([u[a] for a in S])
            iL = [idx[a] for a in L]; iS = [idx[a] for a in S]
            sL = np.sqrt(uL @ Sig[np.ix_(iL, iL)] @ uL); sS = np.sqrt(uS @ Sig[np.ix_(iS, iS)] @ uS)
            phL, phS = sS / (sL + sS), sL / (sL + sS)
            if abs(phL - phS) > 0.2:
                phL, phS = (0.6, 0.4) if phL > phS else (0.4, 0.6)
            ratio = (phL * sL) / (phS * sS)
            if ratio_rule and ratio > 1.5:
                phL = phL * 1.5 / ratio
            elif ratio_rule and ratio < 1 / 1.5:
                phS = phS * ratio * 1.5
            w0 = np.zeros(len(names))
            for a in L: w0[idx[a]] += phL * u[a]
            for a in S: w0[idx[a]] -= phS * u[a]
            sb = np.sqrt(w0 @ Sig @ w0)
            G = min(1.0 / max(phL + phS, 1e-9), sigma_star / sb)
            wv = G * w0
            beta = Sig[:, idx["BTC"]] / Sig[idx["BTC"], idx["BTC"]]
            bn = float(wv @ beta)
            if abs(bn) > 0.05:
                wv = wv * 0.05 / abs(bn)
            n = {a: wv[idx[a]] * cap_usd for a in L + S}
            mins = {}
            for a in L + S:
                step = inst[a]["perp"]["step"]; mq = inst[a]["perp"]["min_qty"]
                mins[a] = max(max(step, mq) * px[a], 10.0)
            small = [a for a in L + S if abs(n[a]) < mins[a]]
            if not small:
                status = "OK"
                break
            drop = min(small, key=lambda a: (abs(n[a]), a))
            (L if drop in L else S).remove(drop)
        rows.append(dict(t=t, status=status or "INFEASIBLE_iter", Ne=Ne, kp=kp, G=float(np.abs(wv).sum()),
                         nlegs=len(L) + len(S), min_leg=float(min(abs(v) for v in n.values())),
                         med_leg=float(np.median([abs(v) for v in n.values()])), ratio=float(ratio),
                         beta=float(wv @ beta), delta=d, sL=float(sL), sS=float(sS)))
    D = pd.DataFrame(rows)
    ok = D[D["status"] == "OK"]
    return {"k": k, "capital": cap_usd, "sigma_star": sigma_star, "ratio_rule": ratio_rule, "days": len(D),
            "share_ok": round(float((D["status"] == "OK").mean()), 3),
            "status_counts": D["status"].value_counts().to_dict(),
            "median_gross_frac": round(float(D["G"].median()), 3) if "G" in D else None,
            "median_leg_usd_ok": round(float(ok["med_leg"].median()), 2) if len(ok) else None,
            "median_legs_ok": float(ok["nlegs"].median()) if len(ok) else None,
            "median_side_basket_vol_ratio_sL_over_sS": round(float((D["sL"] / D["sS"]).median()), 2) if "sL" in D else None,
            "median_standalone_side_ratio": round(float(D["ratio"].median()), 2) if "ratio" in D else None,
            "ratio_p95": round(float(D["ratio"].quantile(0.95)), 2) if "ratio" in D else None,
            "ratio_max": round(float(D["ratio"].max()), 2) if "ratio" in D else None,
            "share_cap_binds": round(float(((D["sL"] / D["sS"]) > 1.5).mean() + ((D["sL"] / D["sS"]) < 1 / 1.5).mean()), 3) if "sL" in D else None,
            "beta_abs_p95": round(float(D["beta"].abs().quantile(0.95)), 3) if "beta" in D else None,
            "gross_p05": round(float(D["G"].quantile(0.05)), 3) if "G" in D else None,
            "median_abs_beta": round(float(D["beta"].abs().median()), 3) if "beta" in D else None,
            "median_lw_delta": round(float(D["delta"].median()), 2) if "delta" in D else None}


# Usage: h3_feasibility.py <grid.json> <tails.json>
grid = [run(k, cap, 0.08, False) for k in (4, 6) for cap in (265, 1000, 2500)]
grid += [run(4, 265, 0.08, True), run(4, 1000, 0.08, True)]
Path(sys.argv[1]).write_text(json.dumps(grid, indent=1, default=str))
tails = [run(4, 265, 0.08, False), run(6, 265, 0.08, False)]
Path(sys.argv[2]).write_text(json.dumps(tails, indent=1, default=str))
for o in grid + tails:
    print(o)
