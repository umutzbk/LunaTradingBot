"""V4 evidence statistics and verdicts (spec §6, §7.3, §7.4, §9; frozen).

Evidence unit: the daily net portfolio return. Centred moving block bootstrap (B = 10,000, seed
20261009) of the mean daily return m, one-sided p = (1 + #{b : m*_b - m_hat >= m_hat}) / (B + 1);
primary block 28 days, mandatory robustness block 14 days. Holm over the 7 configs at
alpha = 0.05 / 3 per forward look (0.05 in Stage 1), separately per block length; C2 needs both.
C6 for H2: Holm over the four H2 ``carry_net`` series, same alphas and blocks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

B = 10_000
SEED = 20261009
BLOCKS = (28, 14)
ALPHA_FORWARD = 0.05 / 3
ALPHA_STAGE1 = 0.05
DAYS_PER_YEAR = 365
STAGE1_LCB_ALPHA = 0.05

MAX_DD, WORST_DAY, ES_LIMIT = 0.15, -0.05, -0.03
C7_MIN_MONTHS, C7_SHARE = 3, 0.60
M1_DAYS, M2_SHARE = 182, 0.90
M3_DAYS = {"carry": 60, "h2": 150}
M4_CARRY_ENTRIES, M4_CARRY_ASSETS = 4, 2


@dataclass(frozen=True)
class BootResult:
    p: float
    m_hat: float
    boot_means: np.ndarray

    def lower_bound(self, alpha: float) -> float:
        """One-sided (1 - alpha) lower confidence bound for the mean."""
        if not len(self.boot_means):
            return math.nan
        return float(self.m_hat - np.quantile(self.boot_means - self.m_hat, 1 - alpha))


def mbb_pvalue(x: np.ndarray, block: int, B: int = B, seed: int = SEED) -> BootResult:  # noqa: N803
    """Moving block bootstrap of the mean: ceil(n / b) overlapping blocks drawn uniformly from the
    n - b + 1 starts, concatenated and truncated to n."""
    x = np.asarray(x, dtype="float64")
    n = len(x)
    if n == 0 or n < block or not np.isfinite(x).all():
        return BootResult(math.nan, float(x.mean()) if n else math.nan, np.array([]))
    m_hat = float(x.mean())
    nb = -(-n // block)
    last = n - (nb - 1) * block
    cs = np.concatenate([[0.0], np.cumsum(x)])
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - block + 1, size=(B, nb))
    full = (cs[starts[:, :-1] + block] - cs[starts[:, :-1]]).sum(axis=1) if nb > 1 else np.zeros(B)
    part = cs[starts[:, -1] + last] - cs[starts[:, -1]]
    means = (full + part) / n
    p = (1 + int(np.sum(means - m_hat >= m_hat))) / (B + 1)
    return BootResult(p, m_hat, means)


def holm(pvals: dict[str, float], alpha: float) -> dict[str, dict]:
    """Holm step-down; NaN p-values never reject. Ties are ordered by id."""
    clean = {k: (v if v is not None and not math.isnan(v) else 1.0) for k, v in pvals.items()}
    order = sorted(clean, key=lambda k: (clean[k], k))
    m = len(order)
    out, going = {}, True
    for i, k in enumerate(order):
        a = alpha / (m - i)
        going = going and clean[k] <= a
        out[k] = {"p": pvals[k], "alpha": a, "reject": bool(going)}
    return out


# --- risk metrics ---------------------------------------------------------------------------------


def sharpe(r: pd.Series) -> float:
    r = pd.Series(r).dropna()
    sd = r.std(ddof=1) if len(r) > 1 else math.nan
    return float(r.mean() / sd * math.sqrt(DAYS_PER_YEAR)) if sd and sd > 0 else math.nan


def max_drawdown(r: pd.Series) -> float:
    eq = np.cumprod(1 + pd.Series(r).fillna(0.0).to_numpy())
    if not len(eq):
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    return float(np.max(1 - eq / peak))


def worst_day(r: pd.Series) -> float:
    return float(pd.Series(r).min()) if len(r) else math.nan


def es975(r: pd.Series) -> float:
    v = np.sort(pd.Series(r).dropna().to_numpy())
    if not len(v):
        return math.nan
    return float(v[: max(1, math.ceil(0.025 * len(v)))].mean())


def monthly_check(r: pd.Series) -> dict:
    """C7: full calendar months only (every day of the month in the series), >= 3 months and the
    monthly mean daily return > 0 in >= 60% of them."""
    r = pd.Series(r)
    if r.empty:
        return {"months": 0, "positive": 0, "pass": False}
    idx = r.index.tz_convert("UTC").tz_localize(None) if r.index.tz is not None else r.index
    per = pd.Series(r.to_numpy(), index=idx).groupby(idx.to_period("M"))
    months = [(p, g) for p, g in per if len(g) == p.days_in_month]
    pos = sum(1 for _, g in months if g.mean() > 0)
    ok = len(months) >= C7_MIN_MONTHS and pos >= C7_SHARE * len(months)
    return {"months": len(months), "positive": pos, "pass": bool(ok)}


def evidence_checks(kind: str, window_days: int, dq_valid_days: int, active_days: int,
                    entry_assets: list[str], decisions: int) -> dict[str, bool]:
    """M1–M4 (§6)."""
    if kind == "carry":
        m4 = len(entry_assets) >= M4_CARRY_ENTRIES and len(set(entry_assets)) >= M4_CARRY_ASSETS
    else:
        m4 = decisions >= window_days // 7 - 2
    return {"M1": window_days >= M1_DAYS, "M2": window_days > 0 and dq_valid_days >= M2_SHARE * window_days,
            "M3": active_days >= M3_DAYS[kind], "M4": bool(m4)}


# --- verdicts ---------------------------------------------------------------------------------------


@dataclass
class ConfigEvidence:
    config_id: str
    kind: str  # "carry" | "h2"
    primary: pd.Series  # PRIMARY daily net returns
    cost2x: pd.Series  # S-COST2X daily net returns
    carry_value: float | None  # H1/H5: sum(funding_cf) - fees - spread (PRIMARY)
    carry_net: pd.Series | None  # H2: daily carry_net (PRIMARY)
    null_sharpes: list[float]
    always_sharpe: float | None
    had_entries: bool
    m: dict[str, bool] = field(default_factory=dict)


def _null_mean(xs: list[float]) -> float:
    vals = [0.0 if x is None or math.isnan(x) else x for x in xs]  # a flat null has Sharpe 0
    return float(np.mean(vals)) if vals else 0.0


def verdict(config_id: str, criteria: dict[str, bool], stage: int) -> str:
    from luna.v4.strategies import is_null

    if is_null(config_id):
        raise ValueError(f"{config_id} is a null model: nulls can never be GO")
    if not criteria.get("C1"):
        return "INSUFFICIENT DATA"
    if all(criteria.get(f"C{i}") for i in range(1, 8)):
        return "INSUFFICIENT DATA" if stage == 1 else "GO"  # Stage 1 alone can never give GO
    return "NO-GO"


def evaluate(evidences: dict[str, ConfigEvidence], stage: int, B: int = B, seed: int = SEED,  # noqa: N803
             prior_go: list[str] | tuple[str, ...] = ()) -> dict[str, dict]:
    from luna.v4.strategies import is_null

    for cid in evidences:
        if is_null(cid):
            raise ValueError(f"{cid} is a null model: nulls can never be GO")
    fam = {c: e for c, e in evidences.items() if c not in set(prior_go)}
    alpha = ALPHA_STAGE1 if stage == 1 else ALPHA_FORWARD
    boots = {c: {b: mbb_pvalue(e.primary.to_numpy(), b, B, seed) for b in BLOCKS} for c, e in fam.items()}
    holms = {f"b{b}": holm({c: boots[c][b].p for c in fam}, alpha) for b in BLOCKS}
    h2 = {c: e for c, e in fam.items() if e.kind == "h2"}
    cboots = {c: {b: mbb_pvalue(e.carry_net.to_numpy(), b, B, seed) for b in BLOCKS} for c, e in h2.items()}
    c6_holm = {f"b{b}": holm({c: cboots[c][b].p for c in h2}, alpha) for b in BLOCKS}
    out = {}
    for c, e in fam.items():
        r = e.primary
        sh = sharpe(r)
        null_mean = _null_mean(e.null_sharpes)
        c4 = e.had_entries and not math.isnan(sh) and sh > null_mean
        if e.kind == "carry":
            alw = 0.0 if e.always_sharpe is None or math.isnan(e.always_sharpe) else e.always_sharpe
            c4 = c4 and sh > alw
            c6 = e.carry_value is not None and e.carry_value > 0
        else:
            c6 = all(c6_holm[f"b{b}"][c]["reject"] for b in BLOCKS)
        mdd, wd, es = max_drawdown(r), worst_day(r), es975(r)
        c7 = monthly_check(r)
        crit = {
            "C1": all(e.m.get(k, False) for k in ("M1", "M2", "M3", "M4")),
            "C2": all(holms[f"b{b}"][c]["reject"] for b in BLOCKS),
            "C3": bool(len(e.cost2x)) and float(e.cost2x.mean()) > 0,
            "C4": bool(c4),
            "C5": mdd <= MAX_DD and wd >= WORST_DAY and es >= ES_LIMIT,
            "C6": bool(c6),
            "C7": c7["pass"],
        }
        crit = {k: bool(v) for k, v in crit.items()}
        out[c] = {
            "verdict": verdict(c, crit, stage),
            "criteria": crit,
            "criteria_pass": all(crit.values()),
            "m": dict(e.m),
            "pvalues": {f"b{b}": boots[c][b].p for b in BLOCKS},
            "lcb95_b28": boots[c][28].lower_bound(STAGE1_LCB_ALPHA),
            "holm": {k: dict(v) for k, v in holms.items()},
            "c6_holm": {k: dict(v) for k, v in c6_holm.items()} if e.kind == "h2" else None,
            "metrics": {"mean_daily": float(r.mean()) if len(r) else math.nan, "sharpe": sh,
                        "null_sharpe_mean": null_mean, "always_sharpe": e.always_sharpe,
                        "max_drawdown": mdd, "worst_day": wd, "es975": es,
                        "cost2x_mean_daily": float(e.cost2x.mean()) if len(e.cost2x) else math.nan,
                        "carry_value": e.carry_value, "c7": c7, "days": len(r)},
        }
    return out


def stage1_candidate(results: dict[str, dict]) -> str | None:
    """Information only (§7.3): among configs passing C1–C7 in Stage 1, the highest b = 28 one-sided
    95% lower bound of the PRIMARY mean; ties -> smaller L, k and Y."""
    from luna.v4.strategies import CONFIGS

    def key(c):
        cfg = CONFIGS[c]
        return (-results[c]["lcb95_b28"], getattr(cfg, "lookback", 0), getattr(cfg, "k", 0),
                getattr(cfg, "y", 0.0), c)

    passing = [c for c, v in results.items() if v["criteria_pass"] and not math.isnan(v["lcb95_b28"])]
    return min(passing, key=key) if passing else None


DEPLOYABLE_REASONS = (
    "(a) UK retail access to perpetual products is restricted; eligibility not confirmed.",
    "(b) No live broker in the code.",
    "(c) No synchronised two-venue execution or margin management.",
    "(d) No maker fill model.",
)


def deployable_now() -> dict:
    return {"deployable_now": "NO", "reasons": list(DEPLOYABLE_REASONS)}
