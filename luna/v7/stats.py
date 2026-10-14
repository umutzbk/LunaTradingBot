"""Bootstrap statistics, Holm and Sharpe (spec S-13.1, S-13.3, S-10.6 / P7 lower bounds, §12 Sharpe; AMB-22).

* **Daily returns (S-13.1):** r_t = (E_t − E_{t−1}) / E_{t−1} at the 00:00 UTC day boundaries, t = 1..T; the
  last day closes at the terminal unwind (the engine's last daily row), so it includes the unwind.
  ``ledger_returns`` reads the engine's daily equity column of an accounted ``Ledger`` (stressed ledgers use
  their cost-shifted path). Pairs (r, x) are formed only over identical day rows (``paired``).
* **Circular block bootstrap:** k = ⌈T/ℓ⌉ uniform start indices in [0, T); each block is ℓ consecutive
  indices wrapped mod T; the blocks are concatenated and truncated to T. One index matrix [B, T] per ℓ is
  drawn from ``numpy.random.default_rng(seed)`` and serves r AND x (paired draws). B = 10,000, ℓ ∈ {14, 28},
  seed = ``SEED`` (fixed, recorded in every result).
* **Centred p-value:** p(ℓ) = (1 + #{b : x̄*_b − x̄ ≥ x̄}) / (B + 1); p = max over ℓ.
* **IUT:** p_P = max(p_abs, p_diff); H_P accepted iff p_P < 0.05.
* **One-sided lower bound:** LB = θ̂ − q₀.₉₅(θ̂* − θ̂) (θ̂ = sample mean), the minimum over the stated block
  lengths; q₀.₉₅ is the empirical quantile ``inverted_cdf`` (the ⌈0.95 B⌉-th order statistic, AMB-22 (b)).
* **Holm–Bonferroni:** step-down adjusted p-values; a voided family member keeps its place in the family and
  never rejects (AMB-22 (c)).
* **Sharpe (§12):** mean / std (ddof 1) × √365, and the Newey–West (Bartlett) HAC-adjusted value with the lag
  L = ⌊4 (T/100)^{2/9}⌋ (Newey–West 1994 rule); report only (AMB-22 (d)).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from luna.v7.accounting import Ledger
from luna.v7.constants import FROZEN

SEED = FROZEN.search.seed  # S-13.1 "seed fixed and recorded" (the frozen V7 seed, AMB-22 (a))
BLOCKS = FROZEN.stats.blocks
B_DEFAULT = FROZEN.stats.B
ALPHA = FROZEN.stats.alpha
_EQ_REL = 1e-12  # contiguity of the daily equity column (equity_end_t == equity_start_{t+1})


def _series(x, what: str) -> np.ndarray:
    a = np.asarray(x, dtype=np.float64)
    if a.ndim != 1 or len(a) == 0:
        raise ValueError(f"{what} must be a non-empty 1-d series")
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{what} has non-finite values")
    return a


# --- daily returns -----------------------------------------------------------------------------------------
def daily_returns(equity_00utc) -> np.ndarray:
    """r_t = (E_t − E_{t−1}) / E_{t−1}, t = 1..T, from E_0..E_T (finite, > 0)."""
    e = _series(equity_00utc, "equity")
    if len(e) < 2:
        raise ValueError("need at least two equity points")
    if np.any(e <= 0):
        raise ValueError("equity must be > 0")
    return np.diff(e) / e[:-1]


def ledger_equity(led: Ledger) -> np.ndarray:
    """E_0..E_T: the engine's day-start equities and the last day's end equity (after the terminal unwind)."""
    d = led.daily_engine
    if d is None or len(d) == 0:
        raise ValueError("ledger without a daily table")
    e0 = d["equity_start"].to_numpy(dtype=np.float64)
    e1 = d["equity_end"].to_numpy(dtype=np.float64)
    if not (np.all(np.isfinite(e0)) and np.all(np.isfinite(e1))):
        raise ValueError("non-finite daily equity")
    if np.any(np.abs(e0[1:] - e1[:-1]) > _EQ_REL * np.maximum(1.0, np.abs(e1[:-1]))):
        raise ValueError("daily equity column is not contiguous")
    return np.concatenate([e0, e1[-1:]])


def ledger_days(led: Ledger) -> np.ndarray:
    return led.daily_engine["start_ts"].to_numpy(dtype=np.int64)


def ledger_returns(led: Ledger) -> np.ndarray:
    """S-13.1 daily returns of one accounted book."""
    return daily_returns(ledger_equity(led))


def paired(led_a: Ledger, led_b: Ledger) -> tuple[np.ndarray, np.ndarray]:
    """(r_a, x = r_a − r_b) over the same days; misaligned day rows are refused."""
    da, db = ledger_days(led_a), ledger_days(led_b)
    if len(da) != len(db) or np.any(da != db):
        raise ValueError("paired series are not aligned on the same days")
    ra, rb = ledger_returns(led_a), ledger_returns(led_b)
    return ra, ra - rb


# --- circular block bootstrap ------------------------------------------------------------------------------
def cbb_indices(T: int, ell: int, B: int, seed: int) -> np.ndarray:
    """[B, T] index matrix: k = ⌈T/ℓ⌉ uniform starts per replicate, blocks wrap mod T, truncated to T."""
    T, ell, B = int(T), int(ell), int(B)
    if T < 1 or ell < 1 or B < 1:
        raise ValueError("T, ell and B must be >= 1")
    k = -(-T // ell)
    starts = np.random.default_rng(int(seed)).integers(0, T, size=(B, k), dtype=np.int64)
    idx = (starts[:, :, None] + np.arange(ell, dtype=np.int64)[None, None, :]) % T
    return idx.reshape(B, k * ell)[:, :T]


def p_centered(x, idx: np.ndarray) -> float:
    """(1 + #{b : x̄*_b − x̄ ≥ x̄}) / (B + 1)."""
    a = _series(x, "x")
    if idx.ndim != 2 or idx.shape[1] != len(a):
        raise ValueError("idx must be [B, T] with T = len(x)")
    m = float(a.mean())
    mb = a[idx].mean(axis=1)
    return float((1 + int(np.count_nonzero(mb - m >= m))) / (idx.shape[0] + 1))


def lower_bound_idx(x, idx: np.ndarray, q: float = 1.0 - ALPHA) -> float:
    """θ̂ − q₀.₉₅(θ̂* − θ̂) for one index matrix (θ̂ = mean; ``inverted_cdf`` quantile)."""
    a = _series(x, "x")
    if idx.ndim != 2 or idx.shape[1] != len(a):
        raise ValueError("idx must be [B, T] with T = len(x)")
    m = float(a.mean())
    d = a[idx].mean(axis=1) - m
    return m - float(np.quantile(d, q, method="inverted_cdf"))


def lower_bound(x, ells: Sequence[int] = BLOCKS, B: int = B_DEFAULT, seed: int = SEED) -> float:
    """S-13.1 one-sided LB: the minimum over ℓ ∈ ``ells`` of θ̂ − q₀.₉₅(θ̂* − θ̂)."""
    a = _series(x, "x")
    if not ells:
        raise ValueError("no block length")
    return min(lower_bound_idx(a, cbb_indices(len(a), ell, B, seed)) for ell in ells)


def p_max_over_blocks(x, ells: Sequence[int] = BLOCKS, B: int = B_DEFAULT, seed: int = SEED) -> float:
    """max over ℓ of the centred p-value of the mean of x."""
    a = _series(x, "x")
    if not ells:
        raise ValueError("no block length")
    return max(p_centered(a, cbb_indices(len(a), ell, B, seed)) for ell in ells)


def iut(p_abs: float, p_diff: float) -> float:
    """Intersection-union p-value p_P = max(p_abs, p_diff) (Berger 1982)."""
    for p in (p_abs, p_diff):
        if not (math.isfinite(p) and 0.0 < p <= 1.0):
            raise ValueError(f"bad p-value {p!r}")
    return max(p_abs, p_diff)


@dataclass(frozen=True)
class CBBResult:
    """S-13.1 test of μ_r > 0 and μ_x > 0 with the same draws (per ℓ) for r and x."""

    mean_r: float
    mean_x: float
    p_abs_by_ell: dict[int, float]
    p_diff_by_ell: dict[int, float]
    B: int
    seed: int

    @property
    def p_abs(self) -> float:
        return max(self.p_abs_by_ell.values())

    @property
    def p_diff(self) -> float:
        return max(self.p_diff_by_ell.values())

    @property
    def p_P(self) -> float:
        return iut(self.p_abs, self.p_diff)

    @property
    def holds(self) -> bool:
        return self.p_P < ALPHA


def cbb_test(r, x, ells: Sequence[int] = BLOCKS, B: int = B_DEFAULT, seed: int = SEED) -> CBBResult:
    """Paired S-13.1 test: one index matrix per ℓ serves both r and x."""
    ra, xa = _series(r, "r"), _series(x, "x")
    if len(ra) != len(xa):
        raise ValueError("r and x lengths differ")
    if not ells:
        raise ValueError("no block length")
    pa: dict[int, float] = {}
    pd_: dict[int, float] = {}
    for ell in ells:
        idx = cbb_indices(len(ra), ell, B, seed)
        pa[int(ell)] = p_centered(ra, idx)
        pd_[int(ell)] = p_centered(xa, idx)
    return CBBResult(float(ra.mean()), float(xa.mean()), pa, pd_, int(B), int(seed))


# --- Holm --------------------------------------------------------------------------------------------------
def holm(pvals: Sequence[float | None] | Mapping[str, float | None]):
    """Holm–Bonferroni step-down adjusted p-values (same order / keys). ``None`` marks a voided member: it
    stays in the family (m unchanged) ranked as p = 1 and its adjusted value is ``None`` (AMB-22 (c))."""
    if isinstance(pvals, Mapping):
        keys = list(pvals)
        adj = holm([pvals[k] for k in keys])
        return dict(zip(keys, adj, strict=True))
    ps = list(pvals)
    if not ps:
        raise ValueError("empty family")
    m = len(ps)
    raw = []
    for p in ps:
        if p is None:
            raw.append(1.0)
            continue
        if not (math.isfinite(p) and 0.0 <= p <= 1.0):
            raise ValueError(f"bad p-value {p!r}")
        raw.append(float(p))
    order = sorted(range(m), key=lambda i: (raw[i], ps[i] is None, i))
    out = [0.0] * m
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * raw[i]))
        out[i] = run
    return [None if p is None else out[i] for i, p in enumerate(ps)]


# --- Sharpe ------------------------------------------------------------------------------------------------
def nw_lag(T: int) -> int:
    """Newey–West (1994) Bartlett lag ⌊4 (T/100)^{2/9}⌋ (AMB-22 (d))."""
    return int(math.floor(4.0 * (T / 100.0) ** (2.0 / 9.0)))


@dataclass(frozen=True)
class Sharpe:
    value: float  # daily mean / std (ddof 1) × √365
    adjusted: float  # Newey–West HAC (Bartlett, lag ``lag``) × √365; report only
    lag: int


def sharpe(r, lag: int | None = None) -> Sharpe:
    """§12 Sharpe: mean / std × √365 and the Newey–West adjusted value (autocovariances over T − 1 so that
    lag 0 gives the plain value). A zero variance gives NaN (never passes P9)."""
    a = _series(r, "r")
    T = len(a)
    L = nw_lag(T) if lag is None else int(lag)
    if T < 2 or L < 0:
        raise ValueError("need T >= 2 and lag >= 0")
    m = float(a.mean())
    d = a - m
    g0 = float(d @ d) / (T - 1)
    lrv = g0
    for k in range(1, min(L, T - 1) + 1):
        lrv += 2.0 * (1.0 - k / (L + 1)) * float(d[k:] @ d[:-k]) / (T - 1)
    ann = math.sqrt(float(FROZEN.accept.ann_days))
    sr = m / math.sqrt(g0) * ann if g0 > 0 else float("nan")
    sr_adj = m / math.sqrt(lrv) * ann if lrv > 0 else float("nan")
    return Sharpe(sr, sr_adj, L)


__all__ = ["ALPHA", "BLOCKS", "CBBResult", "SEED", "Sharpe", "cbb_indices", "cbb_test", "daily_returns",
           "holm", "iut", "ledger_days", "ledger_equity", "ledger_returns", "lower_bound", "lower_bound_idx",
           "nw_lag", "p_centered", "p_max_over_blocks", "paired", "sharpe"]
