# ruff: noqa: E501  (the regex table rows quote the spec text verbatim)
"""Static spec-consistency check: every number of the frozen spec's parameter tables equals the value the code
holds (``FROZEN`` or, where named in the row, the owning pinned constant / function), and every spec ID has a
code or documented non-code owner in ``luna.v7.SPEC_COVERAGE``.

The spec text comes from ``spec.read_frozen_spec()`` (the tag blob, sha256-checked). Each row below is
``(section / row, regex with capture groups, expected value)``: the regex must match exactly once and its
captured value(s), parsed by ``_parse``, must equal the expected value. A spec number with no code counterpart is
listed in ``NOT_CODED`` with the reason (and its regex must match too, so the list cannot go stale silently).
"""

from __future__ import annotations

import importlib
import math
import re
from pathlib import Path

import pytest

from luna import v7
from luna.v7 import arms, models, verdict, windows
from luna.v7.constants import FROZEN as F
from luna.v7.forward import FORWARD_BLOCKS
from luna.v7.spec import code_repo, read_frozen_spec
from luna.v7.timeutil import MIN, NS, H, to_ns

SPEC = read_frozen_spec()

def _parse(s: str) -> float | str:
    """A captured spec token -> number (thousands commas dropped) or the raw string (dates)."""
    t = s.strip().replace(",", "")
    if re.fullmatch(r"\d{4}-\d\d-\d\d", t):
        return t
    return float(t)


def pct(x: float) -> float:
    return x / 100.0


def bps(x: float) -> float:
    return x * 1e-4


def _hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _latest_d0_with_8_folds() -> str:
    """The latest month start D0 whose fold count is still >= 8 (S-10.2 says: 2021-07-01)."""
    import pandas as pd

    m = pd.Timestamp("2021-01-01")
    last = None
    while m < pd.Timestamp("2023-01-01"):
        if not windows.stage0_fail(to_ns(m)):
            last = m
        m = m + pd.DateOffset(months=1)
    assert last is not None
    return last.strftime("%Y-%m-%d")


# (section / row, regex, expected value(s), transform applied to the parsed capture(s))
# Expected values are callables so a failure names the row, not an import-time error.
ROWS: list[tuple[str, str, object, object]] = [
    # --- S-0.3 / S-1.4 / S-1.8 / S-2.2 / S-2.6 (windows, instances, funding) ---------------------------------
    ("S-0.3 horizons", r"Horizons \*\*\{(\d+) h, (\d+) h, (\d+) h\}\*\*", lambda: F.horizons_h, int),
    ("S-1.4 W_DEV end", r"\| `W_DEV` \| \[`D0`, (\d{4}-\d\d-\d\d)\)", lambda: F.w_dev_end, None),
    ("S-1.4 W_HOLD", r"\| `W_HOLD` \| \[(\d{4}-\d\d-\d\d), (\d{4}-\d\d-\d\d)\)",
     lambda: (F.w_dev_end, F.w_hold_end), None),
    ("S-1.8 D0 >= 30 symbols", r"satisfying the \*\*breadth gates G_D0 = U1 ∧ U2 ∧ U3 ∧ U4\*\* \(S-2\.3\) is ≥ (\d+)",
     lambda: F.universe.d0_min_symbols, int),
    ("S-1.8 D0 value", r"D0 = (\d{4}-\d\d-\d\d) was re-verified", lambda: F.d0, None),
    ("S-2.2 dormancy days", r"a run of ≥ (\d+) consecutive UTC calendar days", lambda: F.instances.dormancy_days,
     int),
    ("S-2.2 / EC-1 end known_at", r"`instance_end_known_at` = close_time\(last volume > 0 15-minute bar\) \+ (\d+) × 24 h\.\*\*",
     lambda: F.instances.dormancy_days, int),
    ("S-2.6 timestamp tolerance (s)", r"Timestamps are compared with the frozen ±(\d+) s tolerance",
     lambda: F.funding.tol_ns // NS, int),
    ("S-2.6 standard intervals (h)", r"the standard Binance interval \((\d), (\d), (\d) or (\d) h\)",
     lambda: F.funding.std_intervals_h, int),
    # --- S-2.3 gate table ----------------------------------------------------------------------------------
    ("S-2.3 U1 listing age (d)", r"\| U1 listing age \| the current instance's `first_bar_ts` ≤ `t` − (\d+) d",
     lambda: F.universe.u1_days, int),
    ("S-2.3 U2 days", r"\| U2 liquidity \| median daily quote volume over the (\d+) UTC days",
     lambda: F.universe.u2_days, int),
    ("S-2.3 U2 min quote volume (USD)", r"fully ended before `t` ≥ ([\d,]+) USD \(the long-horizon",
     lambda: F.universe.u2_min_quote_vol, None),
    ("S-2.3 U3 history (d)", r"\| U3 funding history \| ≥ (\d+) d of settled funding records",
     lambda: F.universe.u3_days, int),
    ("S-2.3 U3 gap lookback (d)", r"known by `t`, with no gap in the last (\d+) d\.", lambda: F.universe.u3_gap_lookback_days,
     int),
    ("S-2.3 U3 gap slack (s)", r"max\(previous nominal interval, current nominal interval\) \+ (\d+) s \(S-2\.6, DEC-29\)",
     lambda: F.funding.tol_ns // NS, int),
    ("S-2.3 U4 coverage", r"\| U4 bar coverage \| ≥ (\d+) % of last-price 15-minute bars present in the last (\d+) h",
     lambda: (F.universe.u4_min_coverage, F.universe.u4_window_h), "u4"),
    ("S-2.3 U5 half-spread", r"\| U5 spread \| decision-time half-spread estimate `ĥ\(t\)` \(S-4\.4\) ≤ ([\d.]+) bps",
     lambda: F.universe.u5_max_half_spread, bps),
    ("S-2.3 U7 slack (min)", r"\| U7 data freshness \| the last settled funding record ≤ 1 nominal interval \+ (\d+) min old",
     lambda: F.universe.u7_slack_min, int),
    ("S-2.3 U8 window (min)", r"whose close lies in \[`t` − (\d+) min, `t` − (\d+) min\]", lambda: F.universe.u8_window, int),
    # --- S-4.1 / S-4.2 -------------------------------------------------------------------------------------
    ("S-4.1 decision times", r"\*\*S-4\.1 Cadence: every 8 h; decisions at (\d\d:\d\d), (\d\d:\d\d), (\d\d:\d\d) UTC\.\*\*",
     lambda: F.decision.minutes, "hhmm"),
    ("S-4.2 fill delay (min)", r"\*\*S-4\.2 Execution delay\.\*\* Fills happen at `t_e` = `t` \+ (\d+) min",
     lambda: F.decision.fill_delay_min, int),
    # --- S-4.3 fees ----------------------------------------------------------------------------------------
    ("S-4.3 maker", r"\*\*maker ([\d.]+) %, taker [\d.]+ %\*\*", lambda: F.fees.maker, pct),
    ("S-4.3 taker", r"\*\*maker [\d.]+ %, taker ([\d.]+) %\*\*", lambda: F.fees.taker, pct),
    ("S-4.3 BNB discount off", r"\*\*maker [\d.]+ %, taker [\d.]+ %\*\*; BNB fee discount (OFF)", lambda: F.fees.bnb_discount,
     "off"),
    # --- S-4.4 spread and slippage -------------------------------------------------------------------------
    ("S-4.4 fallback half-spread", r"with a valid reference price: half-spread (\d+) bps and slippage \d × slip",
     lambda: F.costs.fallback_half_spread, bps),
    ("S-4.4 fallback slip multiple", r"with a valid reference price: half-spread \d+ bps and slippage (\d) × slip",
     lambda: F.costs.fallback_slip_mult, int),
    ("S-4.4 slip", r"\*\*Slippage\*\* `slip` = (\d+) bps per side", lambda: F.costs.slip, bps),
    ("S-4.4 liquidity cap", r"\*\*Liquidity cap:\*\* a leg's notional ≤ ([\d.]+) % of the symbol's trailing 24 h",
     lambda: F.costs.liquidity_cap, pct),
    # --- S-4.5 proxy ---------------------------------------------------------------------------------------
    ("S-4.5 lambda", r"\*\*λ = ([\d.]+), fixed\.\*\*", lambda: F.spread.lam, None),
    ("S-4.5 window (24 h + 15 min)", r"whose bar k opens ≥ τ − (\d+) h − (\d+) min", lambda: F.spread.window_ns, "hm"),
    ("S-4.5 max pairs", r"\(up to (\d+) pairs\)", lambda: F.spread.max_pairs, int),
    ("S-4.5 floor", r"ĥ\(τ\) = max\(λ · Ŝ\(τ\) / 2, (\d+) bp\)", lambda: F.spread.floor, bps),
    # --- S-5.2 beta (DEC-16) -------------------------------------------------------------------------------
    ("S-5.2 beta days", r"hourly log returns of a and BTC over the (\d+) d ending", lambda: F.beta.days, int),
    ("S-5.2 beta min obs", r"requiring ≥ (\d+) joint observations", lambda: F.beta.min_obs, int),
    ("S-5.2 beta formula", r"β̂ = clip\(([\d.]+)·β_OLS \+ ([\d.]+)·([\d.]+), (\d+), (\d+)\)",
     lambda: (F.beta.w, 1 - F.beta.w, F.beta.prior, *F.beta.clip), None),
    # --- S-7.1 constructor ---------------------------------------------------------------------------------
    ("S-7.1 K", r"at most \*\*K = (\d+)\*\* legs per side", lambda: F.book.K, int),
    ("S-7.1 side gross", r"each side's gross is ([\d.]+) · E", lambda: F.book.side_gross, None),
    ("S-7.1 neutrality", r"\|Σ long − Σ short\| ≤ ([\d.]+) · gross", lambda: F.book.neutral_tol, None),
    ("S-7.1 exit rank slack", r"ranks outside K \+ (\d+) on its side", lambda: F.book.exit_rank_slack, int),
    ("S-7.1 resize drift", r"drifts > (\d+) % from its target", lambda: F.book.resize_drift, pct),
    # --- S-7.2 arms ----------------------------------------------------------------------------------------
    ("S-7.2 A0 mult / sides / buffer", r"c\(a,side\)·h\\\* − ([\d.]+) · (\d) · cost\(a\) − (\d+) bps",
     lambda: (F.a0.mult, F.a0.sides, F.a0.buffer), "a0"),
    ("S-7.2 A1 veto", r"calibrated P̂\(net\(h\\\*\) > 0\) < \*\*([\d.]+)\*\* \(DEC-8\)", lambda: F.a1_veto, None),
    ("S-7.2 A2 hurdle (bps)", r"Hurdle: calibrated ŷ_cal\(h\\\*\) > (\d+) bps", lambda: 0, int),
    ("S-7.2 A0-V5 config (arms.V5_FROZEN_KNOBS)",
     r"frozen config \(k = (\d+), H = (\d+), mult ([\d.]+), buffer (\d+) bps, min hold (\d+) h\)",
     lambda: (arms.V5_FROZEN_KNOBS["k"], arms.V5_FROZEN_KNOBS["switch_horizon_hours"],
              arms.V5_FROZEN_KNOBS["switch_cost_multiplier"], arms.V5_FROZEN_KNOBS["safety_buffer_bps"],
              arms.V5_FROZEN_KNOBS["min_hold_hours"]), None),
    ("S-7.2 TS EWMA half-life (h)", r"EWMA\(r₈, half-life (\d+) h\)", lambda: F.ts_ref.ewma_half_life_h, int),
    ("S-7.2 TS AR(1) days", r"an AR\(1\) on r₈ fitted on the last (\d+) d", lambda: F.ts_ref.ar1_days, int),
    # --- S-7.3 risk layer ----------------------------------------------------------------------------------
    ("S-7.3 leg stop", r"adverse move ≥ (\d+) % from the average entry price", lambda: F.risk.leg_stop, pct),
    ("S-7.3 cooldown (h)", r"→ taker exit, (\d+) h cooldown", lambda: F.risk.cooldown_h, int),
    ("S-7.3 daily loss", r"equity ≤ (\d+) % of the 00:00 equity", lambda: F.risk.daily_loss, pct),
    ("S-7.3 drawdown kill", r"equity ≤ (\d+) % of the running peak", lambda: F.risk.dd_kill, pct),
    ("S-7.3 gross cap", r"\*\*Gross\*\* ≤ ([\d.]+) · E", lambda: F.risk.gross_max, None),
    ("S-7.3 per-leg cap", r"\*\*Per leg\*\* ≤ \(([\d.]+) · E\) / K", lambda: F.book.side_gross, None),
    ("S-7.3 U5 exit threshold", r"U5 \(exit threshold (\d+) bps half-spread\)", lambda: F.risk.u5_exit, bps),
    ("S-7.3 ineligibility count", r"or U7 on (two) consecutive decisions", lambda: F.risk.inelig_consecutive, "two"),
    # --- S-7.4 hedge ---------------------------------------------------------------------------------------
    ("S-7.4 open threshold", r"If \|β_book\| > ([\d.]+) · E, the target hedge", lambda: F.hedge.open_thr, None),
    ("S-7.4 trade threshold", r"\|H_B − current\| > ([\d.]+) · E", lambda: F.hedge.trade_thr, None),
    ("S-7.4 max hedge", r"\|H_B\| ≤ ([\d.]+) · E\.", lambda: F.hedge.max_abs, None),
    # --- S-8.3 / K10 ---------------------------------------------------------------------------------------
    ("S-8.3 cluster gap (h)", r"each episode starts ≤ (\d+) h after the previous one ended", lambda: F.cluster_gap_h, int),
    ("S-8.3 K10 tolerance", r"\*\*Invariant \(K10\):\*\* Σ_c ρ_c \+ MKT = PR, within (1e-9) × Σ\|fill notional\|",
     lambda: F.k10_tol, None),
    # --- S-10.2 / S-10.3 / S-10.4 --------------------------------------------------------------------------
    ("S-10.2 first test quarter (months)", r"the first quarter that begins ≥ D0 \+ (\d+) months",
     lambda: F.cv.first_test_months, int),
    ("S-10.2 fold count for D0 = 2021-01-01 (windows.outer_folds)", r"\*\*(\d+) folds, 2023Q1–2025Q2\.\*\*",
     lambda: len(windows.outer_folds(windows.D0)), int),
    ("S-10.2 first / last fold quarter", r"\*\*\d+ folds, (\d{4})Q(\d)–(\d{4})Q(\d)\.\*\*",
     lambda: _fold_span(), int),
    ("S-10.2 min folds", r"the resulting fold count is < (\d+)", lambda: F.cv.min_folds, int),
    ("S-10.2 latest D0 (windows.stage0_fail)", r"\(i\.e\. D0 later than (\d{4}-\d\d-\d\d)\)",
     lambda: _latest_d0_with_8_folds(), None),
    ("S-10.3 purge (h)", r"admissible for any later block or fold starting at T only if `label_known_at` ≤ T − (\d+) h",
     lambda: F.cv.purge_ns // H, int),
    ("S-10.4 T_tr / T_sel months", r"\| `T_sel` \| \[C − (\d+) mo, C − (\d+) mo\)", lambda: F.cv.block_months, int),
    ("S-10.4 T_cal months", r"\| `T_cal` \| \[C − (\d+) mo, C\)", lambda: F.cv.block_months[1], int),
    ("S-10.4 isotonic clip", r"outputs clipped to \[([\d.]+), ([\d.]+)\]", lambda: F.cv.iso_clip, None),
    ("S-10.4 final blocks (windows.final_blocks)",
     r"`T_tr` = \[D0, (\d{4}-\d\d-\d\d)\), `T_sel` = \[(\d{4}-\d\d-\d\d), (\d{4}-\d\d-\d\d)\), `T_cal` = \[(\d{4}-\d\d-\d\d), (\d{4}-\d\d-\d\d)\)",
     lambda: _final_blocks(), None),
    # --- S-10.5 search space and LightGBM settings ---------------------------------------------------------
    ("S-10.5 configs per horizon / total", r"\*\*S-10\.5 Search space \(DEC-9\): (\d+) configurations per horizon, (\d+) in total\.\*\*",
     lambda: (len(models.search_space(24)), sum(len(models.search_space(h)) for h in F.horizons_h)), int),
    ("S-10.5 EN alpha", r"\| Elastic Net \| y_reg \| α ∈ \{([\de.-]+), ([\de.-]+), ([\de.-]+)\}", lambda: F.search.en_alpha, None),
    ("S-10.5 EN l1_ratio", r"× l1_ratio ∈ \{([\d.]+), ([\d.]+)\} \| (\d+) \|", lambda: (*F.search.en_l1, 6), None),
    ("S-10.5 EN count", r"\| Elastic Net \| y_reg \| .*? \| (\d+) \|",
     lambda: len(F.search.en_alpha) * len(F.search.en_l1), int),
    ("S-10.5 LGB num_leaves", r"\| LightGBM regressor \| y_reg \| num_leaves ∈ \{(\d+), (\d+), (\d+)\}",
     lambda: F.search.lgb_leaves, int),
    ("S-10.5 LGB min_data_in_leaf / count", r"× min_data_in_leaf ∈ \{(\d+), (\d+)\} \| (\d+) \|",
     lambda: (*F.search.lgb_min_data, len(F.search.lgb_leaves) * len(F.search.lgb_min_data)), int),
    ("S-10.5 logistic EN fixed", r"\| Logistic EN \| y_pos \| \*\*fixed:\*\* α = ([\de.-]+), l1_ratio = ([\d.]+) \| (\d) \|",
     lambda: (*F.search.logit, 1), None),
    ("S-10.5 LGB classifier fixed",
     r"\| LightGBM classifier \| y_pos \| \*\*fixed:\*\* num_leaves = (\d+), min_data_in_leaf = (\d+) \| (\d) \|",
     lambda: (*F.search.lgb_clf, 1), int),
    ("S-10.5 LightGBM fixed settings",
     r"learning_rate ([\d.]+); lambda_l2 (\d+); feature_fraction ([\d.]+); bagging_fraction ([\d.]+) \(freq (\d+)\);",
     lambda: tuple(F.search.lgb_fixed[k] for k in ("learning_rate", "lambda_l2", "feature_fraction",
                                                    "bagging_fraction", "bagging_freq")), None),
    ("S-10.5 LightGBM trees / early stopping", r"max (\d+) trees; early stopping after (\d+) rounds",
     lambda: (F.search.lgb_fixed["max_trees"], F.search.lgb_fixed["early_stopping"]), int),
    # --- S-10.6 / S-10.7 -----------------------------------------------------------------------------------
    ("S-10.6 selection bootstrap", r"circular block bootstrap with ℓ = (\d+) days and B = ([\d,]+)\.",
     lambda: (F.stats.sel_block, F.stats.B), int),
    ("S-10.6 simplicity margin (bp/day)", r"exceeds the best A2 LB by ≥ (\d+) bp/day", lambda: F.select.a3_margin_bpd, None),
    ("S-10.6 tie band (bp/day)", r"\(\|ΔLB\| ≤ ([\d.]+) bp/day\)", lambda: F.select.tie_bpd, None),
    ("S-10.7 C_exec / F_net", r"- C_exec / F_net > ([\d.]+);", lambda: F.kill.cexec_over_fnet, None),
    ("S-10.7 positive folds", r"fewer than (\d+) % of outer folds have positive net", lambda: F.kill.min_pos_fold_share,
     pct),
    # --- S-13.1 --------------------------------------------------------------------------------------------
    ("S-13.1 block lengths", r"block length ℓ ∈ \{(\d+), (\d+)\} days", lambda: F.stats.blocks, int),
    ("S-13.1 B", r"- B = ([\d,]+); seed fixed and recorded", lambda: F.stats.B, int),
    ("S-13.1 alpha", r"accepted iff \*\*p_P = max\(p_abs, p_diff\) < ([\d.]+)\*\*", lambda: F.stats.alpha, None),
    # --- S-13.2 P1-P11 -------------------------------------------------------------------------------------
    ("S-13.2 P1 alpha", r"\| P1 \| H_P holds: p_P < ([\d.]+) \(S-13\.1\)", lambda: F.stats.alpha, None),
    ("S-13.2 P2 ratio", r"\| P2 \| F_net > 0 and C_exec / F_net ≤ ([\d.]+) \(§8 objects\)", lambda: F.accept.p2_ratio, None),
    ("S-13.2 P3 max drawdown", r"\| P3 \| Maximum drawdown of the 15-minute marked equity ≤ (\d+) %", lambda: F.accept.p3_mdd,
     pct),
    ("S-13.2 P4 evidence",
     r"\| P4 \| Evidence: ≥ (\d+) active days \(gross ≥ (\d+) % of E for ≥ (\d+) of 96 quarter-hours\), ≥ (\d+) closed legs, ≥ (\d+) clusters",
     lambda: tuple(F.accept.p4[k] for k in ("active_days", "gross_frac", "quarters", "closed_legs", "clusters")), "p4"),
    ("S-13.2 P5 stability",
     r"\| P5 \| Stability: net > 0 in ≥ (\d) of the 4 holdout quarters; no quarter > (\d+) % of NET; using S-8\.4, top-1 N_a ≤ ([\d.]+) · NET and top-3 ≤ ([\d.]+) · NET",
     lambda: tuple(F.accept.p5[k] for k in ("min_pos_q", "max_q_share", "top1", "top3")), "p5"),
    ("S-13.2 P6 stress", r"every fill's fee × ([\d.]+), slippage × ([\d.]+) and spread charge × ([\d.]+)",
     lambda: tuple(F.accept.p6[k] for k in ("fee", "slip", "spread")), None),
    ("S-13.2 P6 stressed mean daily net", r"spreads included\) → stressed mean daily net ≥ (\d) \|", lambda: 0, int),
    ("S-13.2 P7 block lengths", r"\| P7 \| Date-level IC of the ML\\\* regression model: mean > 0 and LB > 0 \(S-13\.1, ℓ ∈ \{(\d+), (\d+)\}\)",
     lambda: F.stats.blocks, int),
    ("S-13.2 P8 slope band", r"over every eligible `W_HOLD` candidate ∈ \[([\d.]+), ([\d.]+)\]", lambda: F.accept.p8, None),
    ("S-13.2 P9 Sharpe", r"\| P9 \| Net Sharpe \(daily, annualized √(\d+)\) ≥ ([\d.]+)", lambda: (365, F.accept.p9_sharpe),
     None),
    ("S-13.2 P11 m rule", r"m = max\((\d+), ⌈([\d.]+) · n_clusters⌉\)", lambda: F.accept.p11_m, None),
    # --- S-13.3 --------------------------------------------------------------------------------------------
    ("S-13.3 family size (verdict.SECONDARY)", r"Holm–Bonferroni across exactly this family of (\d+):",
     lambda: len(verdict.SECONDARY), int),
    ("S-13.3 ECE", r"\*\*P8-classifier:\*\* ECE ≤ ([\d.]+) over (\d+) equal-frequency bins",
     lambda: (F.accept.ece_max, F.accept.ece_bins), None),
    # --- §14 equity layers and deployment ------------------------------------------------------------------
    ("§14 L-norm E_ref", r"liquidity cap evaluated at E_ref = ([\d,]+) USD", lambda: F.deploy.E_ref, None),
    ("§14 L-265 E", r"The same frozen strategy at E = (\d+) USD", lambda: F.deploy.E_265, None),
    ("§14 L-10k E", r"\| \*\*L-10k\*\* \| E = ([\d,]+) USD", lambda: F.deploy.E_10k, None),
    ("§14 D1", r"\| D1 \| ≥ (\d+) % of the decisions", lambda: F.deploy.d1, pct),
    ("§14 D3 infrastructure cost", r"\(currently VPS (\d+) USD/month = (\d+) USD/year\)",
     lambda: (F.deploy.infra_usd_year / 12, F.deploy.infra_usd_year), None),
    # --- §15 forward protocol ------------------------------------------------------------------------------
    ("§15 T_F7 delay (h)", r"`T_F7` = the first 00:00 UTC ≥ (\d+) h after the independent review PASS",
     lambda: F.forward.tf7_delay_h, int),
    ("§15 forward blocks (forward.FORWARD_BLOCKS)",
     r"`T_fit` = \[D0, (\d{4}-\d\d-\d\d)\); `T_cal` = \[(\d{4}-\d\d-\d\d), (\d{4}-\d\d-\d\d)\)",
     lambda: _forward_blocks(), None),
    ("§15 duration (d)", r"\*\*Duration\.\*\* (\d+) calendar days", lambda: F.forward.days, int),
    ("§15 L7 pad (d)", r"\*\*L7 = max\(`T_F7` \+ (\d+) d, R_V6\) \+ (\d+) d\.\*\*",
     lambda: (F.forward.days, F.forward.l7_pad_days), int),
    ("§15 evaluable day", r"\*\*Evaluable day:\*\* ≥ (\d+) % of its quarter-hours", lambda: F.forward.eval_quarter_share,
     pct),
    ("§15 min evaluable days", r"\*\*INCONCLUSIVE\*\* if evaluable days < (\d+) \((\d+) % of (\d+)\)",
     lambda: (F.forward.min_eval_days, round(100 * F.forward.min_eval_days / F.forward.days), F.forward.days), int),
    ("§15 P4 scaled", r"P4 scaled to 180 / 365: ≥ (\d+) active days, ≥ (\d+) closed legs, ≥ (\d+) clusters",
     lambda: tuple(F.forward.p4[k] for k in ("active_days", "closed_legs", "clusters")), int),
    ("§15 H_F test", r"p_abs from S-13\.1 \(ℓ ∈ \{(\d+), (\d+)\}, max p, B = ([\d,]+)\), α = (\d\.\d+)",
     lambda: (*F.stats.blocks, F.stats.B, F.stats.alpha), None),
    ("§15 decision shift (h)", r"the first evaluable quarter within (\d+) h after `t`", lambda: F.forward.shift_max_h, int),
    ("§15 recorder gap (h)", r"A recorder gap > (\d+) h in a required input", lambda: F.forward.gap_h, int),
    ("§15 extension (d)", r"\*\*One\*\* (\d+)-day extension", lambda: F.forward.extension_days, int),
    # --- §16 kill criteria ---------------------------------------------------------------------------------
    ("§16 K8 invalid share", r"\| K8 \| During the holdout run: invalid labels or missing funding > ([\d.]+) %",
     lambda: F.invalid_max, pct),
    ("§16 K10 tolerance", r"\| K10 \| \\\|Σ_c ρ_c \+ MKT − PR\\\| > (1e-9) × Σ\\\|fill notional\\\|", lambda: F.k10_tol, None),
    ("§16 K7 gain share", r"\| K7 \| One feature carries > (\d+) % of total LightGBM gain, or > (\d+) % of",
     lambda: (50, 50), int),  # K7 is a review audit (verdict.K_TABLE text), see NOT_CODED
]


def _fold_span() -> tuple[int, int, int, int]:
    import pandas as pd

    fs = windows.outer_folds(windows.D0)
    a, b = pd.Timestamp(fs[0].test_lo, tz="UTC"), pd.Timestamp(fs[-1].test_lo, tz="UTC")
    return a.year, (a.month - 1) // 3 + 1, b.year, (b.month - 1) // 3 + 1


def _ymd(ns: int) -> str:
    import pandas as pd

    return pd.Timestamp(ns, tz="UTC").strftime("%Y-%m-%d")


def _final_blocks() -> tuple[str, ...]:
    fb = windows.final_blocks()
    assert fb.tr[0] == windows.D0
    return tuple(_ymd(x) for x in (fb.tr[1], fb.sel[0], fb.sel[1], fb.cal[0], fb.cal[1]))


def _forward_blocks() -> tuple[str, ...]:
    (f0, f1), (c0, c1) = FORWARD_BLOCKS["T_fit"], FORWARD_BLOCKS["T_cal"]
    assert f0 == windows.D0
    return tuple(_ymd(x) for x in (f1, c0, c1))


def _transform(kind: object, vals: tuple) -> tuple:
    if kind is None:
        return vals
    if kind is int:
        return tuple(int(v) if isinstance(v, float) and v.is_integer() else v for v in vals)
    if kind == "u4":
        return (pct(vals[0]), int(vals[1]))
    if kind == "hhmm":
        raise AssertionError("handled in the test")
    if kind == "hm":
        return (int(vals[0]) * H + int(vals[1]) * MIN,)
    if kind == "a0":
        return (vals[0], int(vals[1]), bps(vals[2]))
    if kind == "p4":
        return (int(vals[0]), pct(vals[1]), int(vals[2]), int(vals[3]), int(vals[4]))
    if kind == "p5":
        return (int(vals[0]), pct(vals[1]), vals[2], vals[3])
    if callable(kind):
        return tuple(kind(v) for v in vals)
    raise AssertionError(kind)


def _close(a: object, b: object) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-15)


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_constants_match_spec_tables(row):
    name, pattern, expected, kind = row
    hits = list(re.finditer(pattern, SPEC))
    assert len(hits) == 1, f"{name}: regex matched {len(hits)} times (the spec text must pin the row)"
    raw = hits[0].groups()
    if kind == "hhmm":
        got: tuple = tuple(_hhmm_to_min(x) for x in raw)
    elif kind == "off":
        got = (raw[0] != "OFF",)  # OFF -> False (discount not applied)
    elif kind == "two":
        got = ({"two": 2}[raw[0]],)
    else:
        got = _transform(kind, tuple(_parse(x) for x in raw))
    exp = expected()
    exp_t = tuple(exp) if isinstance(exp, (tuple, list)) else (exp,)
    assert len(got) == len(exp_t), f"{name}: {got} vs {exp_t}"
    assert all(_close(a, b) for a, b in zip(got, exp_t, strict=True)), f"{name}: spec {got} != code {exp_t}"


def test_spec_tables_have_rows():
    """Coverage of the brief's sections: every listed section contributes at least one checked row."""
    must = ("S-2.3", "S-4.3", "S-4.4", "S-4.5", "S-7.1", "S-7.2", "S-7.3", "S-7.4", "S-10.2", "S-10.3", "S-10.4",
            "S-10.5", "S-10.6", "S-10.7", "S-13.1", "S-13.2", "S-13.3", "§14", "§15", "§16")
    names = [r[0] for r in ROWS]
    for m in must:
        assert any(n.startswith(m + " ") for n in names), m
    assert len(names) == len(set(names))


# Spec numbers inside the checked sections that have no FROZEN counterpart, with the reason. Each regex must
# still match the frozen text once.
NOT_CODED: list[tuple[str, str, str]] = [
    ("S-2.3 PIT lag 1 min", r"All inputs must have `known_at` ≤ `t` − 1 min\.",
     "structural: the PIT lag is the time unit timeutil.MIN in every assert_pit (universe, features, engine)"),
    ("S-4.3 support article id", r"\(360033544231\), in two official locales",
     "fee-evidence provenance (R9 record / binance-fee-evidence.md); governance, not code"),
    ("S-4.3 fee-evidence sha256", r"sha256 `63a9fa95ca32ea7d0015fd19dd7cce2d3549cec6b3feb740761794f5e135efb7`",
     "provenance hash of a Stage-0 document; governance, not code"),
    ("S-4.4 fill proxy PIT lag", r"using only bars fully known by τ − 1 min\. No R6\.",
     "structural PIT lag (timeutil.MIN) inside spread.half_spread"),
    ("S-4.5 R6 size", r"The full 1\.06 TB R6 dataset is not downloaded",
     "data-volume statement; R6 is diagnostic only and never read (L8)"),
    ("S-4.5 reference package", r"CRAN `bidask` 2\.1\.5, method `\"AR\"`",
     "reference implementation version for the required equality test (tests/v7/ar_reference.py, AMB-06)"),
    ("S-7.3 U6 exit lag", r"an open leg exits at the first decision at or after the matched version's `known_at` \+ 1 min",
     "structural PIT lag (timeutil.MIN), book.constructor / universe U6 (AMB-16/17)"),
    ("S-10.4 T_tr minimum length", r"\| `T_tr` \| \[D0, C − 12 mo\), ≥ 12 months \|",
     "implied by first_test_months = 24 and block_months = (12, 6) (checked rows)"),
    ("S-13.2 P4 quarter-hours per day", r"for ≥ \d+ of (96) quarter-hours",
     "structural: the 15-minute tick grid (96 per UTC day); verdict.active_days counts ticks"),
    ("S-13.2 P5 holdout quarters", r"net > 0 in ≥ \d of the (4) holdout quarters",
     "structural: W_HOLD is one year (S-1.4); the quarter edges are verdict.p5 inputs"),
    ("§15 L7 strict-default date", r"\*\*L7 ≥ (2027-10-14)\*\* under the strict default",
     "derived at runtime from the hash-checked V6 artefacts (forward.r_v6_strict_default, S-1.1); never hard-coded"),
    ("§15 exchange-filter cadence", r"snapshotted at least daily and on change",
     "operational recorder cadence (forward recorder service, not in this engine)"),
    ("§16 K7 50 % gain / weight share", r"\| K7 \| One feature carries > (50) % of total LightGBM gain",
     "mandatory leakage audit before the holdout (review item, verdict.K_TABLE text); no automated check yet"),
]


@pytest.mark.parametrize("row", NOT_CODED, ids=[r[0] for r in NOT_CODED])
def test_not_coded_numbers_are_still_in_the_spec(row):
    name, pattern, reason = row
    assert reason
    assert len(re.findall(pattern, SPEC)) == 1, name


# ------------------------------------------------------------------------------------------- SPEC_COVERAGE

ID_PATTERNS = {
    "S-rule": r"\bS-\d+\.\d+\b",
    "DEC": r"\bDEC-\d+\b",
    "EC": r"(?<![A-Z-])EC-\d+\b",
    "E": r"\bE(?:6|8|10|11)\b",
    "L": r"\bL(?:[1-9]|1[0-4])\b",
    "P": r"\bP(?:[1-9]|1[01])\b",
    "K": r"\bK(?:[1-9]|1[01])\b",
    "D": r"\bD[1-3]\b",
    "R": r"\bR(?:[1-9]|1[0-2])\b",
    "DT": r"\bDT[1-4]\b",
    "U": r"\bU[1-8]\b",
    "S0": r"\bS0-\d+\b",
    "O": r"\bO-\d+\b",
}
DOC_PREFIXES = ("doc:",)


def spec_ids() -> set[str]:
    out: set[str] = set()
    for pat in ID_PATTERNS.values():
        out |= set(re.findall(pat, SPEC))
    return out


def test_spec_id_extraction_is_complete():
    ids = spec_ids()
    assert {f"DEC-{i}" for i in range(1, 48)} <= ids
    assert {f"EC-{i}" for i in (1, 2, 6, 7, 8, 9, 11)} == {i for i in ids if i.startswith("EC-")}
    assert {"E6", "E8", "E10", "E11"} <= ids
    assert {f"L{i}" for i in range(1, 15)} <= ids and {f"P{i}" for i in range(1, 12)} <= ids
    assert {f"K{i}" for i in range(1, 12)} <= ids and {f"R{i}" for i in range(1, 13)} <= ids
    assert {"D1", "D2", "D3", "DT1", "DT2", "DT3", "DT4"} <= ids


def _amb_ids() -> set[str]:
    p = Path(code_repo()) / "docs/results/v7-impl/AMBIGUITIES.md"
    return set(re.findall(r"^## (AMB-\d+)", p.read_text(encoding="utf-8"), flags=re.M))


def test_every_dec_has_code_or_doc_owner():
    cov = v7.SPEC_COVERAGE
    missing = sorted(spec_ids() - set(cov))
    assert not missing, f"spec IDs without an owner in SPEC_COVERAGE: {missing}"
    extra = sorted(set(cov) - spec_ids())
    assert not extra, f"SPEC_COVERAGE IDs not in the frozen spec: {extra}"
    ambs = _amb_ids()
    for sid, owners in cov.items():
        assert isinstance(owners, tuple) and owners, sid
        for o in owners:
            if o.startswith(DOC_PREFIXES):
                assert len(o) > len("doc: ") + 3, (sid, o)
                for amb in re.findall(r"AMB-\d+", o):
                    assert amb in ambs, (sid, amb)
                continue
            assert o.startswith("luna."), (sid, o)
            importlib.import_module(o)  # every named module exists
