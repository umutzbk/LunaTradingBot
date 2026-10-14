> **NON-NORMATIVE — MERGED INTO DRAFT 2** (`2026-10-14-luna-v7-ml-research-design.md`). Kept only as the approval record. Where this file differs from Draft 2, Draft 2 governs; superseded values quoted here (e.g. 0.55, 54 configs, inner-block calibration, the pro-forma P11) are not rules.

# V7 — DRAFT 1 REVIEW PATCH / OPEN DECISIONS MEMO REV 2

| | |
|---|---|
| Status | **Proposal for user approval.** Draft 1 (`2026-10-14-luna-v7-ml-research-design.md`) is not edited by this document; once approved, the patch is applied as Draft 2. |
| Restrictions | Nothing committed or frozen. No Stage 0 download, no training, no holdout inspection, no V6 change. |
| Kept unchanged (user) | hedged perp-leg architecture; deterministic portfolio/risk boundary; Binance primary / Kraken exploratory; horizons 24/72/168 h; Elastic Net + Logistic + LightGBM only; taker as the only PASS-capable execution model; maker-first secondary; point-in-time universe; sealed V7 holdout; leakage canaries; "no viable edge" as a valid result |

Each item: what Draft 1 got wrong or left open → the proposed replacement rule → what needs your decision (**DECIDE**).

---

## 1. V6 embargo: derived from the canonical V6 records

### 1.1 Sources (read-only)

| Fact | Value | Source |
|---|---|---|
| V6 sealed holdout | [2026-07-01T00:00:00Z, 2026-10-09T11:52:48.861565107Z) | `luna-v6-frozen-constants.json` → `holdout.start` / `holdout.end` (sha `152af4cb…`) |
| V6 guarded gap | [holdout end, T_F): refused without the holdout token | constants → `holdout.guard_window` = "[2026-07-01, T_F) refused without the holdout token" |
| V6 forward evidence start | T_F = 2026-10-14T00:00:00Z (`day1` = T_F, since T_F is a midnight) | `docs/results/v6-code-freeze-record.json`; `forward.day1` at tag `v6-code-frozen` (`ea78102`) |
| V6 looks | day 90 = 2027-01-12T00:00Z; day 180 = 2027-04-12T00:00Z; day 365 = 2027-10-14T00:00Z | `verdict.LOOKS` and `forward.look_date` at `ea78102`, computed with V6's own function |
| Masking | "pnl_masked_until: holdout decision and then each scheduled look" | constants → `forward.pnl_masked_until`; spec S-GUARD-3 |
| Cumulative windows | each look evaluates from day 1 (candidates) or day 91 (FEAS confirmation) up to the look date, so days 1–90 stay inside the day-180 and day-365 evaluations | spec S-STAT-F / S-H1-F; `forward.forward_look` |
| End of the masked V6 evaluation | the day-365 look (S-STAT-X terminal rule); a FINAL GO is monitored until day 365 (GO REVOKED) | spec lines on FINAL GO monitoring and S-STAT-X |

### 1.2 Consequence under the strict policy (default)

The V6-masked span is **M6 = [2026-07-01T00:00:00Z, R6)**, where **R6** is the earliest of:
- **(a)** completion and report of V6's day-365 look (≥ 2027-10-14T00:00Z);
- **(b)** completion and report of a V6 look after which every one of the four slots is `CLOSED`, with no `PENDING` slot and no `FINAL GO` under monitoring, so that no V6 evaluation remains;
- **(c)** an explicit written release by the user.

M6 also requires that W_V6H has been released, i.e. that the V6 holdout was opened and reported, or that the user released it in writing. If the V6 holdout is never opened, M6 stays masked until (c).

**Rule S-1.3′ (replaces S-1.3 and S-1.5).** No V7 code path may generate any outcome from any venue (Binance or Kraken) for any timestamp inside M6 before R6. This covers labels, P&L, IC, fills/paper equity, or any metric computed from post-entry prices or funding. Guards refuse overlap, not only containment.

**Raw recording isn't outcome generation.** Writing raw public market data with `recv_ts` (ticker, book, funding records) is allowed during M6, provided nothing derived from it is computed or displayed before R6.

**Effect on V7's own schedule — stated explicitly:**
- V7 development (`W_DEV` < 2025-07-01) and the V7 holdout (`W_HOLD` = [2025-07-01, 2026-07-01)) do **not** overlap M6 and are unaffected.
- **The V7 forward test does overlap M6.** Under the strict default, no V7 forward outcome may be computed before R6. **The earliest possible V7 forward verdict is therefore R6 ≥ 2027-10-14**, unless V6 closes earlier (b) or the user releases it (c).
- V7's code freeze and the start of forward raw recording (T_F7) **do not have to wait**. Only outcome computation and the verdict wait (see §10, "delayed-reveal forward").

### 1.3 Alternatives (NOT default; each needs explicit user approval)

| Option | What changes | Risk to V6 blindness |
|---|---|---|
| **ALT-A: masked live paper** | A V7 paper bot runs live on Binance during M6, but its equity, fills and metrics are written to a sealed store that nobody reads before R6 (as V6 masks its own forward P&L) | Low if the seal holds, but a running bot *generates* outcomes during M6, which the strict policy forbids |
| **ALT-B: coexistence** | Binance V7 forward outcomes computed and viewed during M6 | **High**: Binance funding co-moves with Kraken, so V7's forward carry results would reveal V6's masked market regime |

**Recommendation: keep the strict default** (delayed reveal, §10). ALT-A / ALT-B only with your explicit approval.

**DECIDE 1:** confirm the strict default (R6-gated forward verdict, earliest 2027-10-14), or choose ALT-A or ALT-B.

---

## 2. D0: one internally consistent, outcome-free rule

**Problem.** S-1.1 referred to the "point-in-time eligible universe (§2)", i.e. U1–U7, but computed it from a subset.

**Rule S-1.1′.** D0 is the first calendar-month start M ≥ 2021-01-01 such that, at **every** decision time t in M (00:05 / 08:05 / 16:05 UTC), the count of USD-M symbols satisfying the **breadth gates G_D0 = U1 ∧ U2 ∧ U3 ∧ U4** (S-2.3) is ≥ 30.

Why these four gates:
- They depend only on listing metadata, traded volume, funding-record existence and bar coverage: data availability and breadth, not outcomes.
- **U5** (spread) is excluded because its value depends on the O-1 proxy rule. **U6** (announcement) is inactive or optional. **U7** (freshness) is a transient per-decision condition.
- No label, return, IC or P&L is used.

The rule is frozen before Stage 0; Stage 0 only evaluates it. The 30-symbol threshold equals 6 × K at K = 5 (two sides × K legs × a breadth margin of 3).

**DECIDE 2:** approve G_D0 = U1–U4 and threshold 30.

---

## 3. Nested calibration: a temporally valid protocol

**Problem.** Draft 1 fitted calibrators on the inner block and then refit the base model on the full outer training set, so the calibrator was applied to a different model's score scale.

**Rule S-9.4′ / S-9.5′: three disjoint blocks, with no base-model absorption of the calibration tail.** For an outer training set ending at cutoff C (= test start − purge), split it chronologically into:

| Block | Span | Role |
|---|---|---|
| `T_tr` | [start, C − 12 mo) | fit candidate models for hyperparameter selection |
| `T_sel` | [C − 12 mo, C − 6 mo) | inner selection (date-level IC) and LightGBM early stopping (inner loss); never P&L |
| `T_cal` | [C − 6 mo, C) | **calibration tail only.** Never used to fit base-model weights, select hyperparameters or early-stop |

- **Purge between blocks:** a sample in an earlier block is admissible only if its `label_known_at` ≤ the next block's start − 24 h. The same rule as the outer purge.
- **Base model for the fold:** refit on `T_fit = T_tr ∪ T_sel` with the selected hyperparameters (LightGBM: the early-stopped tree count found on `T_sel`). `T_cal` is **never** absorbed.
- **Calibrators** are fitted on predictions of **that exact refit base model** on `T_cal`. Therefore no observation trains the prediction used as its own calibration target, and the calibrator always matches the scale of the model it is applied to.

**Regression recalibration** (used for the entry hurdle and the P8 slope; ranking is unaffected, because the map is monotone):
- ŷ_cal = a + b·ŷ.
- (a, b) by weighted OLS of realized `y_reg` on ŷ over `T_cal`, with every decision date weighted equally (weight 1/n_date per row).
- b is clipped to ≥ 0. If b = 0, ŷ_cal = a is constant: entries happen iff a > 0, and ranking falls back to ŷ order.

**Probability calibration:**
- Isotonic regression of `y_pos` on the classifier's P̂ over `T_cal`, with date-equal weights.
- Outputs clipped to [0.001, 0.999].

**Outer-fold evaluation:** for each outer test quarter:
1. select on `T_sel`;
2. refit on `T_fit`;
3. calibrate on `T_cal`;
4. predict the test quarter with the refit model and its calibrator.

**Minimum training span:** `T_tr` ≥ 12 months, so the first outer test quarter starts at D0 + 24 months. With D0 = 2021-01-01 that gives **10 outer folds, 2023Q1–2025Q2** (Draft 1 had 14). This is the honest cost of a clean calibration tail. If Stage 0 gives a later D0 and fewer than 8 folds, that is reported, and no rule changes.

**Final model before the sealed holdout:**
- `T_tr` = [D0, 2024-07-01), `T_sel` = [2024-07-01, 2025-01-01), `T_cal` = [2025-01-01, 2025-07-01), each with purge.
- One frozen model and calibrator pair (hashed), applied unchanged to the whole `W_HOLD`.
- **No refit inside the holdout.**

**DECIDE 3:** approve the three-block protocol (and the resulting 10 folds for D0 = 2021-01).

---

## 4. P2: cost/funding defined mathematically

Over an evaluation window, at book level (legs and the single residual hedge, including the terminal unwind and forced exits):

| Symbol | Definition |
|---|---|
| F_leg | Σ of signed funding cash flows on all legs (credits > 0, debits < 0) |
| F_hedge | Σ of signed funding cash flows on the BTC residual hedge |
| **F_net** | F_leg + F_hedge (net funding P&L of the executed book) |
| **C_exec** | Σ over every fill (legs, hedge, resizes, forced exits, terminal unwind) of fees + spread + slippage, all ≥ 0 |
| PR | price P&L of legs + hedge (mark-to-market, including the terminal unwind at reference prices) |
| NET | F_net − C_exec + PR |

**P2′ passes iff F_net > 0 and ρ := C_exec / F_net ≤ 0.60.** If F_net ≤ 0, P2 fails: a carry strategy whose net funding doesn't cover anything can't pass, whatever ρ would be.

Always reported separately:
- **leg funding** (credits, debits, net);
- **hedge funding**;
- **F_net**;
- **C_exec**, split into fees, spread and slippage, and legs vs hedge.

The development kill S-9.7 uses the same definitions: kill if F_net ≤ 0 or ρ > 1.0.

**DECIDE 4:** approve P2′ (net funding including hedge funding; F_net ≤ 0 means fail).

---

## 5. Price-residual dependence guard (new criterion P11)

**Purpose.** V7 must not pass because a few favourable residual price moves carry weak carry economics.

Definitions:
- **CAC** (carry after cost) = F_net − C_exec. NET = CAC + PR.
- **Entry cluster c:** consecutive entries of the same asset and side whose holding periods overlap or lie within 72 h of each other (as in P4).
- **ρ_c** = the cluster's pro-forma residual price P&L: for each leg in c, s·n·[(P_exit/P_entry − 1) − β̂·(B_exit/B_entry − 1)], summed over the cluster.

Options:

| Option | Rule | Strength / weakness |
|---|---|---|
| R1 carry sufficiency | CAC > 0 | Forbids a book whose profit exists *only* because of price P&L. Binds only when PR > 0, because NET > 0 with PR ≤ 0 already implies CAC > 0. Doesn't catch a weakly positive CAC topped up by a few lucky moves |
| R2 winner-trim robustness | NET_trim := NET − Σ_{c ∈ Top_m} max(ρ_c, 0) > 0, with Top_m the m clusters with the largest positive ρ_c, m = max(3, ⌈0.05 · n_clusters⌉) | Directly tests "remove the biggest price winners — is anything left?". Needs a cluster definition (above) |
| R3 concentration caps | top-1 asset ≤ 25 % and top-5 clusters ≤ 50 % of positive PR | Descriptive about concentration, but doesn't test the economics after removal |

**Recommendation: P11 = R1 ∧ R2**, both required (R3 reported only). Thresholds are fixed now:
- CAC > 0;
- m = max(3, ⌈5 %⌉ of clusters);
- NET_trim > 0.

The guard also applies to:
- the development kill: CAC ≤ 0 on `W_DEV` out-of-fold → kill;
- the forward test (§10).

**DECIDE 5:** approve P11 = R1 ∧ R2 (or choose R1 alone / R2 alone).

---

## 6. Bootstrap hypothesis tests, exactly

**Data.**
- Daily UTC returns of the normalized book: r_t = (E_t − E_{t−1}) / E_{t−1}, t = 1..T, where E_t is the equity at 00:00 UTC. The last day includes the terminal unwind.
- Arms ML\* and A0 are run over the same days. The paired difference is x_t = r_t^{ML} − r_t^{A0}.

**Hypotheses.**
- H0_abs: μ_ML ≤ 0 vs H1_abs: μ_ML > 0.
- H0_diff: μ_x ≤ 0 vs H1_diff: μ_x > 0.

**Statistic.** The sample means r̄ = (1/T) Σ r_t and x̄ = (1/T) Σ x_t.

**Resampling: circular block bootstrap** (Politis–Romano), block length ℓ ∈ {14, 28} days.
- Draw k = ⌈T/ℓ⌉ start indices i_1..i_k uniformly from {1..T}.
- Each block is (i_j, i_j + 1, …, i_j + ℓ − 1) taken mod T (wrapping around).
- Concatenate the blocks and truncate to T.
- **The same index draws are used for r and x** (paired).
- B = 10,000 replicates, fixed seed (recorded in the run manifest).

The circular scheme gives E*[r̄*] = r̄ exactly; the moving-block version under-weights the edges.

**Null imposition: centered.** The bootstrap distribution of (r̄* − r̄) approximates that of (r̄ − μ). At the null boundary μ = 0:

  **p_abs(ℓ) = (1 + #{b : r̄*_b − r̄ ≥ r̄}) / (B + 1)**, and likewise **p_diff(ℓ)** with x.

Each p is taken over both block lengths: p_abs = max_ℓ p_abs(ℓ), p_diff = max_ℓ p_diff(ℓ). Taking the maximum is conservative.

**Intersection-union.**
- H_P is the claim H1_abs ∧ H1_diff. Its null is the union H0_abs ∪ H0_diff.
- The IUT rejects at α iff each component rejects at α: **p_P = max(p_abs, p_diff) < 0.05**.
- **Validity** (Berger 1982): if each component test has (asymptotic) level α, the IUT has level ≤ α. No multiplicity adjustment is needed inside the IUT. The circular block bootstrap of the mean is asymptotically valid for stationary, weakly dependent series.
- **Caveat** (reported, not corrected): regime non-stationarity within one year.

The same procedure is used for the 6 secondary comparisons. Their p-values are Holm-adjusted across the family of 6.

**DECIDE 6:** approve the CBB / centered / max-over-ℓ / IUT definition.

---

## 7. Research equity: three layers (replaces O-3)

| Layer | Definition | Role |
|---|---|---|
| **L-norm (primary scientific)** | Normalized book (E = 1): fractional quantities, no rounding, no minimum notional; the liquidity cap is evaluated at E_ref = 10,000 USD | All P1–P11, the primary test |
| **L-265 (mandatory deployment feasibility)** | The same frozen strategy at E = 265 USD with exchange rounding (step size), per-symbol minimum notional (Binance `MIN_NOTIONAL`, read in Stage 0), K = 5 legs per side (26.5 USD each), and the residual-hedge rule including BTC's minimum notional. Infeasible legs or hedges are **not** traded (fail closed); the deterministic beta rule then forbids entries if a required hedge can't be placed | Deployment gate (below); never part of the scientific verdict |
| **L-10k (secondary capacity diagnostic)** | E = 10,000 USD with rounding and minimum notional | Capacity / liquidity-cap diagnostic only |

**Deployment feasibility gate at 265 USD** (all required for any deployment proposal; evaluated on `W_HOLD` and again on the forward test):
- **D1:** ≥ 90 % of decisions at which L-norm holds a non-empty book are constructible at 265, legs and any required hedge included.
- **D2:** the L-265 mean daily net > 0 (point estimate, rounding and minimum-notional effects included).
- **D3:** **end-to-end deployment economics:** annualized L-265 net USD − annual fixed infrastructure cost (currently VPS 12 USD/mo = 144 USD/yr, about 54 % of 265 USD per year) > 0.

Rules for this gate:
- Infrastructure cost never enters trade labels or L-norm results. It is shown only in the separate deployment-economics report.
- A $10k simulation alone can never establish deployability for a $265 account.
- The likely binding constraint, stated now: BTCUSDT's minimum notional may exceed the residual-hedge size at 265 USD. Stage 0 reads the actual filter.

**DECIDE 7:** approve the three layers and D1–D3.

---

## 8. O-9 (A1 veto threshold)

There's no pre-outcome decision-theoretic derivation that gives 0.55 rather than 0.50:
- with a calibrated P̂ and symmetric treatment, "veto if a loss is more likely than a gain" is P̂ < 0.50;
- any margin above that is an untested risk preference.

**Recommendation: fixed calibrated threshold 0.50.**
- Never tuned on P&L.
- A1 remains secondary; expected-net regression ranking is the primary ML mechanism.

**DECIDE 8:** 0.50.

---

## 9. O-15 (model-selection budget): explanation and recommendation

**Fits per outer fold and horizon:**
- regression: 6 Elastic Net + 6 LightGBM candidates trained on `T_tr`, scored on `T_sel` (IC);
- classification: 3 logistic + 3 LightGBM;
- then 1 refit per (model, task) on `T_fit` (4 refits).

That is 22 fits per fold per horizon. With 10 folds × 3 horizons that is 660 fits, plus 66 for the final pre-holdout model.

**Effective economic choices on `W_DEV`:** only S-9.6, i.e. 6 candidates (2 families × 3 horizons) compared by out-of-fold economic lower bound. Hyperparameter selection inside folds uses IC only, never P&L.

**Why the holdout protects the final claim:**
- The 54-config search and the 6-way economic selection can inflate *development* estimates.
- The final claim is a single pre-specified test (H_P) on `W_HOLD`, which none of these choices saw.
- The selection is completed and frozen before the opening, so the holdout p-value isn't affected by the search size.
- The search size still matters for *what* gets frozen (a noisier selection can freeze a worse model), which is why it should stay small.

**Is the classifier search needed?** Classifiers serve only A1 (secondary) and P8 (calibration of P̂). Neither needs a tuned classifier; a fixed, sensible configuration is enough.

**Recommendation (on simplicity grounds, not outcome):**
- Keep the regression grid: 6 Elastic Net + 6 LightGBM per horizon. This is the primary mechanism, with equal budget for linear and nonlinear.
- Replace the classifier grids with **one fixed configuration each:**
  - logistic EN α = 1e-2, l1_ratio 0.5;
  - LightGBM classifier num_leaves 15, min_data_in_leaf 2000, early stopping on `T_sel` logloss.
- **Total 14 per horizon, 42 in total.** No new model family.

**DECIDE 9:** 42 (recommended) or keep 54.

---

## 10. Forward protocol, fully frozen before training (replaces O-6 / S-12.5)

**Precondition.** The V7 holdout verdict is `V7 HOLDOUT ADVANCE`. Otherwise there is no forward test.

**Forward model.**
- ML\* is refit **mechanically**, with no re-selection: the same hyperparameters and the three-block protocol on W_DEV ∪ W_HOLD (`T_cal` = the last 6 months of `W_HOLD`).
- Hashed and frozen before T_F7.
- A0 runs alongside as the reference.

**Start-time rule.** T_F7 = the first 00:00 UTC ≥ 24 h after the independent review PASS of the forward recorder + replay code. The forward recorder is a separate Binance public-data service with its own directory. It doesn't touch the Kraken collector.

**Mode (strict default, §1): delayed-reveal replay.**
- From T_F7 the recorder stores raw public data with `recv_ts`: tickers / book top, a depth snapshot every 5 minutes, funding records, mark / index / premium bars.
- **No outcome is computed** before the look (and never before R6).
- At the look, the frozen code replays the recorded data deterministically, as V6 does.

**Duration.** 180 calendar days: [T_F7, T_F7 + 180 d).

**Looks.** **Exactly one**, at L7 = max(T_F7 + 180 d, R6) + 1 d. Nothing about the forward results is computed or shown before L7.

**Evidence requirements:**
- **Evaluable day:** ≥ 90 % of 15-minute quarters have a full-universe ticker snapshot ≤ 180 s old and a BTC book snapshot ≤ 180 s old.
- The look is **INCONCLUSIVE** if evaluable days < 80 % of 180 (i.e. < 144).
- P4 scaled to 180 / 365: ≥ 30 active days, ≥ 50 closed legs, ≥ 10 independent clusters.

**Statistical rule.**
- H_F: μ_ML > 0 on the forward daily returns (normalized, `E_T`).
- One-sided, CBB centered (§6), blocks 14 and 28, max p, B = 10,000, α = 0.05.
- With a single look, no alpha spending is needed.
- ML\* − A0 is reported, not required.

**GO criteria** (all required):
- H_F p < 0.05;
- P2′;
- P3 (drawdown ≤ 15 %, no kill);
- P6 (cost × 1.5 → mean ≥ 0);
- P11 (R1 ∧ R2);
- the evidence requirements;
- D1–D3 at 265 USD for a *deployment* proposal (a scientific GO without D1–D3 is "GO — not deployable at 265 USD").

**Verdicts:**

| Verdict | When |
|---|---|
| INVALID RUN | integrity failure |
| NO-GO | any criterion fails, or the replay hits the drawdown kill |
| INCONCLUSIVE | coverage or evidence not met |
| GO | all of the above hold |

**Missing-data policy:**
- **Decision timing:** a decision at an 8 h grid point uses the first fresh snapshot within 2 h after the grid time. If there is none, the decision is skipped (no entries; pending risk exits run at the first fresh snapshot).
- **Funding records** missing in the recorder: fetched from the official Binance REST funding history after the period, hashed; never interpolated. If unavailable → INVALID RUN.
- **Prices** are never backfilled or interpolated. A stale quarter is a no-op for risk and decisions (as V6 D-X4).

**Restart / data-gap policy:**
- Recorder restarts don't change the window.
- A gap > 24 h makes the affected days non-evaluable.
- **One** pre-registered 90-day extension is allowed only if the look is INCONCLUSIVE because of coverage alone (same rules, same single look at the extended end). There is no extension for statistical non-significance or any other reason.

**Kill criteria in the forward test:**
- the replay's own drawdown kill → NO-GO;
- a guard breach (any outcome computed before L7 / R6) → INVALID RUN, with an incident report to the user.

**Stated consequence:** with T_F7 inside M6, the V7 forward verdict date is ≥ 2027-10-14 under the strict default.

**DECIDE 10:** approve the forward protocol (and the delayed-reveal mode).

---

## Summary of decisions requested

| # | Decision | Recommendation |
|---|---|---|
| 1 | V6 embargo | Strict default: M6 = [2026-07-01, R6), outcome-free recording allowed, V7 forward verdict ≥ R6 (≥ 2027-10-14); ALT-A / ALT-B only with explicit approval |
| 2 | D0 | G_D0 = U1–U4, ≥ 30 at every decision of the month |
| 3 | Calibration | `T_tr` / `T_sel` / `T_cal` three-block, no absorption; 10 folds at D0 = 2021-01 |
| 4 | P2 | F_net > 0 and C_exec / F_net ≤ 0.60 (hedge funding included) |
| 5 | Residual guard | P11 = CAC > 0 ∧ NET_trim > 0 (m = max(3, ⌈5 %⌉)) |
| 6 | Bootstrap | Circular block, centered, max over ℓ ∈ {14, 28}, IUT p = max(p_abs, p_diff) |
| 7 | Equity | L-norm primary; L-265 deployment gate D1–D3 (infrastructure cost separate); L-10k diagnostic |
| 8 | O-9 | 0.50 |
| 9 | O-15 | 42 (regression 6 + 6 per horizon; classifiers fixed) |
| 10 | Forward | 180 d, delayed-reveal single look at max(T_F7 + 180 d, R6) + 1 d, rules above |
