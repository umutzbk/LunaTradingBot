"""S-STAT-P: p-values. p_mbb = V5 ``stats.mbb`` (one-sided H0: mean <= 0); p_net and p_carry are the
max over the blocks 14 and 28; p_GO = max(p_net, p_carry) (intersection-union). Holm: the V5
``stats.holm`` (ties by name; NaN -> 1). Newey-West: Bartlett kernel, lag 28, one-sided, N(0,1)."""

from __future__ import annotations

import math

import numpy as np

from luna.carry import stats as v5
from luna.v6.spec import constants

_S = constants()["statistics"]
BLOCKS = tuple(_S["blocks_days"])
SEED = _S["seed"]
B_HOLDOUT = _S["B_holdout"]
B_FORWARD = _S["B_forward"]
NW_LAG = 28


def p_go(net: np.ndarray, carry: np.ndarray, B: int) -> dict:
    out = {"p": {}, "lb95": {}}
    for name, x in (("net", net), ("carry", carry)):
        ps, lbs = {}, {}
        for b in BLOCKS:
            r = v5.mbb(np.asarray(x, float), b, B=B, seed=SEED)
            ps[b], lbs[b] = r.p, r.lower_bound(0.05)
        out["p"][name] = ps
        out["lb95"][name] = lbs
        out[f"p_{name}"] = max((v if v is not None and not math.isnan(v) else 1.0) for v in ps.values())
    out["p_go"] = max(out["p_net"], out["p_carry"])
    out["lb_ok"] = all(out["lb95"][n][b] > 0 for n in ("net", "carry") for b in BLOCKS
                       if not math.isnan(out["lb95"][n][b])) and not any(
        math.isnan(out["lb95"][n][b]) for n in ("net", "carry") for b in BLOCKS)
    return out


def holm(p: dict, alpha: float) -> dict:
    return v5.holm(p, alpha)


def newey_west_p(x: np.ndarray, lag: int = NW_LAG) -> float:
    """One-sided p for H0: mean <= 0 (Bartlett-kernel HAC variance, normal reference)."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 2 or not np.isfinite(x).all():
        return math.nan
    m = x.mean()
    d = x - m
    var = float(d @ d) / n
    for lag_ in range(1, min(lag, n - 1) + 1):
        g = float(d[lag_:] @ d[:-lag_]) / n
        var += 2 * (1 - lag_ / (lag + 1)) * g
    se = math.sqrt(var / n) if var > 0 else 0.0
    if se == 0:
        return 0.0 if m > 0 else 1.0
    return float(0.5 * math.erfc((m / se) / math.sqrt(2)))
