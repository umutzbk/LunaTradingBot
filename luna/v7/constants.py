"""Frozen V7 constants. Every value comes from the frozen spec; each class cites its section.

Spec: docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md (tag v7-ml-spec-frozen).
Nothing here may be changed after the freeze; a change is a normative amendment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from luna.v7.timeutil import MIN, NS, H


@dataclass(frozen=True)
class Decision:  # S-4.1 / S-4.2
    minutes: tuple[int, ...] = (5, 485, 965)  # 00:05 / 08:05 / 16:05 UTC, minutes after midnight
    fill_delay_min: int = 15


@dataclass(frozen=True)
class Execution:  # S-4.2 / S-6.2
    forced_delay_min: int = 1  # S-4.2: forced risk exits fill on the first 1-minute bar after trigger + 1 min
    maker_window_min: int = 30  # S-6.2: trade-through fills within W = 30 min, then the taker fallback
    adverse_min: tuple[int, ...] = (5, 30, 60)  # S-6.2: reported adverse mid move horizons


@dataclass(frozen=True)
class Notices:  # S-2.5 (DEC-37)
    u6_clear_h: int = 72  # U6 deactivates at scheduled_delist_ts + 72 h if never HALTED in between


@dataclass(frozen=True)
class Fees:  # S-4.3 (USD-M perp fees, DEC-39)
    maker: float = 0.0002
    taker: float = 0.0005
    bnb_discount: bool = False


@dataclass(frozen=True)
class Costs:  # S-4.4 / S-4.5
    slip: float = 2e-4
    fallback_half_spread: float = 25e-4
    fallback_slip_mult: int = 3
    liquidity_cap: float = 0.005
    liquidity_window_h: int = 24  # S-4.4: the symbol's trailing 24 h quote volume (runner qv24)


@dataclass(frozen=True)
class Spread:  # S-4.5 (proxy, DEC-22)
    lam: float = 2.0
    floor: float = 1e-4
    window_ns: int = 24 * H + 15 * MIN
    max_pairs: int = 95


@dataclass(frozen=True)
class Features:  # §9 feature table (F1-F14; F7 / F15 dropped by Stage 0)
    funding_windows_h: tuple[int, ...] = (24, 72, 168, 720)  # F1 FL1, FL3, FL7, FL30
    f13_cap: float = 1e4  # F13: "both capped at 10^4"
    f13_settlements_per_year: int = 3 * 365  # F13: |FL7| * 3 * 365
    winsor: tuple[float, float] = (0.01, 0.99)  # linear models: training-fold winsorization at 1 / 99 %


@dataclass(frozen=True)
class Universe:  # S-2.3 gates U1..U8
    u1_days: int = 60
    u2_min_quote_vol: int = 20_000_000
    u2_days: int = 30
    u3_days: int = 30
    u3_gap_lookback_days: int = 7
    u4_min_coverage: float = 0.95
    u4_window_h: int = 24  # U4: "in the last 24 h"
    u5_max_half_spread: float = 7.5e-4
    u7_slack_min: int = 15
    u8_window: tuple[int, int] = (60, 1)
    d0_min_symbols: int = 30  # S-1.8 (DEC-2): >= 30 G_D0 symbols at every decision of the month


@dataclass(frozen=True)
class Instances:  # S-2.2 (DEC-35)
    dormancy_days: int = 7


@dataclass(frozen=True)
class Funding:  # S-3 / S-4.6
    tol_ns: int = 60 * NS
    std_intervals_h: tuple[int, ...] = (1, 2, 4, 8)


@dataclass(frozen=True)
class Book:  # S-7
    K: int = 5
    side_gross: float = 0.5
    neutral_tol: float = 0.05
    resize_drift: float = 0.25
    exit_rank_slack: int = 2


@dataclass(frozen=True)
class Risk:  # S-7 risk layer
    leg_stop: float = 0.15
    cooldown_h: int = 24
    daily_loss: float = 0.98
    dd_kill: float = 0.85
    gross_max: float = 1.0
    u5_exit: float = 20e-4
    inelig_consecutive: int = 2


@dataclass(frozen=True)
class Hedge:  # S-7 BTC hedge
    open_thr: float = 0.05
    trade_thr: float = 0.02
    max_abs: float = 0.5


@dataclass(frozen=True)
class Beta:  # S-7 / S-9 beta estimate
    days: int = 30
    min_obs: int = 500
    w: float = 0.7
    prior: float = 1.0
    clip: tuple[float, float] = (0.0, 3.0)


@dataclass(frozen=True)
class A0:  # S-7 A0 reference arm
    mult: float = 1.5
    sides: int = 2
    buffer: float = 5e-4


@dataclass(frozen=True)
class TSRef:  # S-7.2 TS reference arms (DEC-20)
    ewma_half_life_h: int = 72
    ar1_days: int = 30


@dataclass(frozen=True)
class CV:  # S-10.2 / S-10.3 / S-10.4
    purge_ns: int = 24 * H
    iso_clip: tuple[float, float] = (0.001, 0.999)
    first_test_months: int = 24  # S-10.2: first test quarter begins >= D0 + 24 months
    min_folds: int = 8  # S-10.2 (DEC-15): fewer outer folds is STAGE-0 FAIL
    block_months: tuple[int, int] = (12, 6)  # S-10.4: T_sel = [C - 12 mo, C - 6 mo), T_cal = [C - 6 mo, C)


@dataclass(frozen=True)
class Search:  # S-10.5 (DEC-9): 14 configurations per horizon
    en_alpha: tuple[float, ...] = (1e-3, 1e-2, 1e-1)
    en_l1: tuple[float, ...] = (0.2, 0.8)
    lgb_leaves: tuple[int, ...] = (7, 15, 31)
    lgb_min_data: tuple[int, ...] = (500, 2000)
    logit: tuple[float, float] = (1e-2, 0.5)  # (alpha, l1_ratio), fixed
    lgb_clf: tuple[int, int] = (15, 2000)  # (num_leaves, min_data_in_leaf), fixed
    seed: int = 20261014  # S-10.5 "Seeds are fixed": one seed for all model / bagging seeds
    lgb_fixed: dict = field(
        default_factory=lambda: dict(
            learning_rate=0.03, lambda_l2=5, feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
            max_trees=1500, early_stopping=100,
        )
    )


@dataclass(frozen=True)
class Stats:  # S-13.1
    B: int = 10_000
    blocks: tuple[int, ...] = (14, 28)
    alpha: float = 0.05
    sel_block: int = 14  # S-10.6 selection bootstrap block length (days)


@dataclass(frozen=True)
class Select:  # S-10.6
    a3_margin_bpd: float = 1.0
    tie_bpd: float = 0.1


@dataclass(frozen=True)
class Kill:  # S-10.7
    cexec_over_fnet: float = 1.0
    min_pos_fold_share: float = 0.30


@dataclass(frozen=True)
class Accept:  # S-13.2 acceptance P1..P11
    p2_ratio: float = 0.60
    p3_mdd: float = 0.15
    p4: dict = field(
        default_factory=lambda: dict(
            active_days=60, gross_frac=0.10, quarters=48, closed_legs=100, clusters=20
        )
    )
    p5: dict = field(default_factory=lambda: dict(min_pos_q=2, max_q_share=0.60, top1=0.30, top3=0.60))
    p6: dict = field(default_factory=lambda: dict(fee=1.5, slip=1.5, spread=3.0))
    p8: tuple[float, float] = (0.5, 1.5)
    p6_min_net: float = 0.0  # P6: stressed mean daily net >= 0
    p9_sharpe: float = 1.0
    ann_days: int = 365  # P9 / §12 Sharpe annualized sqrt(365); D3 annualized net (365 x mean daily net)
    p11_m: tuple[int, float] = (3, 0.05)
    ece_max: float = 0.05
    ece_bins: int = 10


@dataclass(frozen=True)
class Forward:  # S-15 (DEC-10), scaled to 180 days
    days: int = 180
    eval_quarter_share: float = 0.90
    min_eval_days: int = 144
    p4: dict = field(default_factory=lambda: dict(active_days=30, closed_legs=50, clusters=10))
    shift_max_h: int = 2
    gap_h: int = 24
    extension_days: int = 90
    tf7_delay_h: int = 24  # T_F7 = the first 00:00 UTC >= 24 h after the review PASS
    l7_pad_days: int = 1  # L7 = max(T_F7 + 180 d, R_V6) + 1 d


@dataclass(frozen=True)
class Deploy:  # S-14 (DEC-7)
    E_265: int = 265
    E_10k: int = 10_000
    E_ref: int = 10_000
    d1: float = 0.90
    infra_usd_year: int = 144


@dataclass(frozen=True)
class Frozen:
    decision: Decision = field(default_factory=Decision)
    execution: Execution = field(default_factory=Execution)
    notices: Notices = field(default_factory=Notices)
    horizons_h: tuple[int, ...] = (24, 72, 168)  # S-5
    fees: Fees = field(default_factory=Fees)
    costs: Costs = field(default_factory=Costs)
    spread: Spread = field(default_factory=Spread)
    universe: Universe = field(default_factory=Universe)
    features: Features = field(default_factory=Features)
    instances: Instances = field(default_factory=Instances)
    funding: Funding = field(default_factory=Funding)
    book: Book = field(default_factory=Book)
    risk: Risk = field(default_factory=Risk)
    hedge: Hedge = field(default_factory=Hedge)
    beta: Beta = field(default_factory=Beta)
    a0: A0 = field(default_factory=A0)
    a1_veto: float = 0.50  # S-7 A1 probability veto
    ml_hurdle: float = 0.0  # S-7.2 A2 / A3 hurdle: calibrated yhat_cal(h*) > 0 bps
    ts_ref: TSRef = field(default_factory=TSRef)
    cv: CV = field(default_factory=CV)
    search: Search = field(default_factory=Search)
    stats: Stats = field(default_factory=Stats)
    select: Select = field(default_factory=Select)
    kill: Kill = field(default_factory=Kill)
    accept: Accept = field(default_factory=Accept)
    cluster_gap_h: int = 72  # S-8 cluster definition
    k10_tol: float = 1e-9
    invalid_max: float = 0.005
    forward: Forward = field(default_factory=Forward)
    deploy: Deploy = field(default_factory=Deploy)
    d0: str = "2021-01-01"  # S-1.8 (DEC-2), re-verified in Stage 0
    w_dev_end: str = "2025-07-01"  # S-1.4
    w_hold_end: str = "2026-07-01"  # S-1.4


FROZEN = Frozen()
