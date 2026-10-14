# V7 ML Research Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the frozen V7 spec as a deterministic, outcome-free-until-authorised research engine (`luna/v7`), tested only on synthetic fixtures, ready for a code-freeze review.

**Architecture:** One package `luna/v7`. It has three layers:
- pure, time-indexed (int64 ns UTC) data / PIT layers;
- a feature, label and model layer;
- a deterministic book engine (constructor → risk → hedge → execution → accounting) consumed by the statistics, selection and verdict modules.

Guards (seal, M6, network allowlist, forward store) sit in front of every data entry point. Every frozen number lives once, in `luna/v7/constants.py`, and a static check compares it with the tables in the frozen spec.

**Tech Stack:** Python 3.12, numpy, pandas, scipy, statsmodels (existing). New pinned dependencies: `scikit-learn` (ElasticNet, LogisticRegression with elasticnet, IsotonicRegression) and `lightgbm`. Tooling: pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md`, frozen at tag `v7-ml-spec-frozen` (commit `29959fc563c0fc58430744810095cefa1d63c9a8`; committed-blob sha256 `ce7370fa5cd49a47ae0912a46430f2612c972108536d85f2d8306e0389db9dd4`).
- Section references below (S-x.y, R-n, P-n, K-n, DEC-n, EC-n) are to that spec.
- Every implementer reads the cited sections before writing code.

## Global Constraints

- **The spec is immutable.** Never edit the spec file or the tag. If a requirement is ambiguous or not literally implementable:
  1. stop that component;
  2. append an entry to `docs/results/v7-impl/AMBIGUITIES.md` (ID `AMB-nn`, spec section, the literal text, the options, a classification `implementation clarification` | `normative amendment required`);
  3. implement only what is unambiguous.
  Never pick a new economic or research rule.
- **No outcome computation on research data.**
  - No training on `W_DEV`, no label / P&L / IC on real data, no hyperparameter selection, no `W_HOLD` access, no V6 outcome access.
  - Tests use synthetic fixtures built in code under `tests/v7/` only.
  - Never read `var/`.
- **Time:** every timestamp is int64 ns since the Unix epoch, UTC (L12). Inputs in ms or s are converted once, at ingest, by `luna.v7.timeutil.to_ns`.
- **PIT:** every input row carries `known_at` (ns). A decision at `t` may only use rows with `known_at ≤ t − 60 s` (S-2.3, L1).
- **Instances:** no computation crosses an instance boundary (S-2.2, E10). Fills, settlements and prices are never taken from another instance.
- **No synthetic fills, no interpolated prices** (S-5.4, §15 "Missing data").
- **Determinism:** fixed seeds; no wall-clock reads in pure modules; stable sort orders (ties by symbol string).
- **Style:** match `luna/carry` and `luna/v6`: module docstrings that cite spec sections, frozen dataclasses, ruff line length 110.
- **Test command:** `uv run pytest tests/v7 -q`. The full suite (`uv run pytest -q`) must stay green.
- **Commits:** one commit per task, message `feat(v7): <task>`, ending with the session's Co-Authored-By line. Never commit to `feat/v7-ml-research` and never move `v7-ml-spec-frozen`.

## Review Focus

1. **Millisecond / second inputs mixed with ns** (archive CSVs in ms, REST in ms): a value off by 10⁶ must be rejected or converted, never silently mis-ordered. Test owner: Task 3 (`test_ingest_rejects_ambiguous_units`).
2. **Boundary inclusivity:** funding exactly at `t_e` is excluded and exactly at `t_x` is included ((`t_e`, `t_x`]); U8 close exactly at `t − 60 min` and `t − 1 min` is included. Test owners: Task 10 (`test_funding_window_half_open`) and Task 8 (`test_u8_window_inclusive_ends`).
3. **An empty eligible universe or one side empty at a decision:** the book stays flat or reduces, with no crash, no forced re-entry and no division by zero in the weights. Test owner: Task 14 (`test_empty_side_keeps_book_flat`).
4. **The same symbol relisted (two instances):** every module (features, U1/U3, labels, halted exits, accounting episodes) treats them as different instruments. Test owners: Task 4 (`test_relist_new_instance`), Task 9 (`test_features_reset_at_new_instance`), Task 10 (`test_no_cross_instance_exit_fill`).
5. **NaN / inf / constant feature columns** at training or prediction: linear models impute with the median plus a missing-indicator; constant columns don't divide by zero in standardization; LightGBM receives NaN natively. Test owner: Task 9 (`test_preprocess_constant_and_nan_columns`).

## File structure

```
luna/v7/__init__.py
luna/v7/timeutil.py          ns helpers
luna/v7/constants.py         every frozen number (single source)
luna/v7/spec.py              spec / tag / V6-artefact pinning and hash checks
luna/v7/windows.py           W_DEV / W_HOLD / D0 / decision clock / folds / blocks / M6
luna/v7/guard.py             holdout authorisation, markers, manifest, window refusals (S-1.3, S-1.7, DEC-19)
luna/v7/netguard.py          exact endpoint allowlist N1–N6 (S-1.7, DEC-41/46)
luna/v7/forward_store.py     write-only forward recorder store + read refusal before L7 (DEC-45, EC-8)
luna/v7/data/schema.py       R1–R12, DT1–DT4 column contracts + known_at rules
luna/v7/data/klines.py       monthly ∪ daily union (DEC-33), unit normalisation
luna/v7/data/funding.py      archive-primary funding merge (DEC-32)
luna/v7/data/instances.py    activity instances, instance_end_known_at (DEC-35/44, EC-1)
luna/v7/data/intervals.py    nominal interval, reconciliation, interval_transition, r8, U3 gap (DEC-29/40/47, EC-9)
luna/v7/data/notices.py      R11 versions, confident match, U6 state machine (S-2.5)
luna/v7/data/terminal.py     terminal events A/B/C, R12 (S-5.4, DEC-43, EC-7)
luna/v7/market.py            reference prices P/M/B, HALTED, data-gap rule (S-5.2, S-5.4, S-4.6)
luna/v7/spread.py            Abdi–Ranaldo proxy ĥ(τ) (S-4.5)
luna/v7/beta.py              β̂ (S-5.2, DEC-16)
luna/v7/universe.py          U1–U8, G_D0, D0 rule (S-2.3, S-1.8)
luna/v7/features.py          F1–F6, F8–F14 + preprocessing (§9)
luna/v7/labels.py            leg labels (S-5.3–S-5.6)
luna/v7/models.py            model families, 42 configs (S-0.4, S-10.5)
luna/v7/cv.py                purge, three blocks, calibration, IC (S-10.3, S-10.4)
luna/v7/arms.py              A0, A1, A2/A3 scoring, TS refs, A0-V5 adapter (S-7.2)
luna/v7/book/constructor.py  K=5 targets, neutrality, entry/exit/resize (S-7.1)
luna/v7/book/risk.py         deterministic risk layer (S-7.3)
luna/v7/book/hedge.py        book-level BTC residual hedge (S-7.4)
luna/v7/book/execution.py    E_T / E_M fills, halts, terminal settlement (S-6, S-5.4)
luna/v7/book/engine.py       event loop: decisions, 15-min marks, funding, terminal unwind (S-8.1)
luna/v7/accounting.py        §8 objects, PR attribution, clusters, MKT, K10, N_a, daily table
luna/v7/stats.py             CBB, p_abs / p_diff, IUT, LB, Holm (S-13.1, S-13.3)
luna/v7/selection.py         S-10.6 validity + choice, S-10.7 kill
luna/v7/verdict.py           P1–P11, S-13.4 classes, K-map, P8-classifier
luna/v7/deploy.py            L-265 / L-10k layers, filters provenance, D1–D3 (§14)
luna/v7/forward.py           T_F7, L7, evaluability, forward GO, EC-11 (§15)
tests/v7/...                 one test module per source module + test_spec_consistency.py + test_end_to_end_synthetic.py
docs/results/v7-impl/AMBIGUITIES.md
```

---

### Task 1: Foundations — time, frozen constants, spec pinning, windows, folds

**Files:**
- Create: `luna/v7/__init__.py`, `luna/v7/timeutil.py`, `luna/v7/constants.py`, `luna/v7/spec.py`, `luna/v7/windows.py`, `docs/results/v7-impl/AMBIGUITIES.md` (header only).
- Test: `tests/v7/__init__.py`, `tests/v7/test_foundations.py`.

**Interfaces:**
- Produces:
  - `timeutil`:
    - `NS=10**9`, `MIN=60*NS`, `H=3600*NS`, `D=86400*NS`;
    - `to_ns(x) -> int`: accepts `pd.Timestamp`, ISO strings and ints in ns. An int < 10**15 raises `ValueError` (ambiguous unit), unless it is passed through `from_ms` / `from_s`;
    - `from_ms(int) -> int`, `from_s(int) -> int`, `ts(ns) -> pd.Timestamp` (UTC).
  - `constants.FROZEN`, a frozen dataclass tree. Every value comes from the spec; field names are listed in the test below.
  - `spec`:
    - `SPEC_TAG`, `SPEC_REL`, `FREEZE_COMMIT`, `SPEC_BLOB_SHA256`;
    - `verify_spec(repo=None) -> None`. It raises `SpecMismatch` unless `git show v7-ml-spec-frozen:<SPEC_REL>` has `SPEC_BLOB_SHA256`, the tag resolves to `FREEZE_COMMIT`, and the working copy equals the blob after CRLF→LF;
    - `v6_artifacts(repo=None) -> V6Bounds`. It reads `git show v6-code-frozen:docs/superpowers/specs/luna-v6-frozen-constants.json` (sha256 `152af4cb19e688b3551feb8210aeec5c471aec78b708592fcbb6eeef08946974`) and `...:docs/results/v6-code-freeze-record.json` (sha256 `2f84499556480b6a715df86b2c20f66f22142fa62b931c1806b4806cc962d413`) and raises on a hash mismatch.
  - `V6Bounds(holdout_start, holdout_end, t_f, looks: tuple[int,int,int])`, all in ns. Looks are day 90 / 180 / 365 after `t_f`, using the V6 `look_date` convention: `day1(T_F) + day·D`, where `day1` is `T_F` floored to the day. These give 2027-01-12, 2027-04-12 and 2027-10-14 at 00:00Z.
  - `windows`:
    - `W_DEV=(D0, 2025-07-01)`, `W_HOLD=(2025-07-01, 2026-07-01)`, `D0 = 2021-01-01` (from constants);
    - `decision_times(lo, hi) -> np.ndarray[int64]`: 00:05 / 08:05 / 16:05 UTC in [lo, hi);
    - `Fold(fold_id, test_lo, test_hi, tr, sel, cal)`; `outer_folds(d0) -> list[Fold]`;
    - `final_blocks() -> Fold`;
    - `m6(repo=None) -> tuple[int, int | None]`: start = the V6 holdout start; end = `None` while R_V6 is undetermined;
    - `overlaps(lo, hi, span) -> bool`.

- [ ] **Step 1: Write failing tests** (`tests/v7/test_foundations.py`)
  - `test_frozen_constants_values`. Assert, among others:
    - `FROZEN.decision.minutes == (5, 485, 965)`, `FROZEN.decision.fill_delay_min == 15`, `FROZEN.horizons_h == (24, 72, 168)`;
    - `FROZEN.fees.maker == 0.0002`, `FROZEN.fees.taker == 0.0005`, `FROZEN.fees.bnb_discount is False`, `FROZEN.costs.slip == 2e-4`;
    - `FROZEN.costs.fallback_half_spread == 25e-4`, `FROZEN.costs.fallback_slip_mult == 3`, `FROZEN.costs.liquidity_cap == 0.005`;
    - `FROZEN.spread.lam == 2.0`, `FROZEN.spread.floor == 1e-4`, `FROZEN.spread.window_ns == 24*H + 15*MIN`, `FROZEN.spread.max_pairs == 95`;
    - `FROZEN.universe.u1_days == 60`, `u2_min_quote_vol == 20_000_000`, `u2_days == 30`, `u3_days == 30`, `u3_gap_lookback_days == 7`, `u4_min_coverage == 0.95`, `u5_max_half_spread == 7.5e-4`, `u7_slack_min == 15`, `u8_window == (60, 1)`;
    - `FROZEN.instances.dormancy_days == 7`, `FROZEN.funding.tol_ns == 60*NS`, `FROZEN.funding.std_intervals_h == (1, 2, 4, 8)`;
    - `FROZEN.book.K == 5`, `side_gross == 0.5`, `neutral_tol == 0.05`, `resize_drift == 0.25`, `exit_rank_slack == 2`;
    - `FROZEN.risk`: `leg_stop == 0.15`, `cooldown_h == 24`, `daily_loss == 0.98`, `dd_kill == 0.85`, `gross_max == 1.0`, `u5_exit == 20e-4`, `inelig_consecutive == 2`;
    - `FROZEN.hedge`: `open_thr == 0.05`, `trade_thr == 0.02`, `max_abs == 0.5`;
    - `FROZEN.beta`: `days == 30`, `min_obs == 500`, `w == 0.7`, `prior == 1.0`, `clip == (0.0, 3.0)`;
    - `FROZEN.a0`: `mult == 1.5`, `sides == 2`, `buffer == 5e-4`; `FROZEN.a1_veto == 0.50`;
    - `FROZEN.cv`: `purge_ns == 24*H`, `iso_clip == (0.001, 0.999)`;
    - `FROZEN.search.en_alpha == (1e-3, 1e-2, 1e-1)`, `en_l1 == (0.2, 0.8)`, `lgb_leaves == (7, 15, 31)`, `lgb_min_data == (500, 2000)`, `logit == (1e-2, 0.5)`, `lgb_clf == (15, 2000)`, `lgb_fixed == dict(learning_rate=0.03, lambda_l2=5, feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, max_trees=1500, early_stopping=100)`;
    - `FROZEN.stats`: `B == 10_000`, `blocks == (14, 28)`, `alpha == 0.05`, `sel_block == 14`;
    - `FROZEN.select`: `a3_margin_bpd == 1.0`, `tie_bpd == 0.1`;
    - `FROZEN.kill`: `cexec_over_fnet == 1.0`, `min_pos_fold_share == 0.30`;
    - `FROZEN.accept`:
      - `p2_ratio == 0.60`, `p3_mdd == 0.15`;
      - `p4 == dict(active_days=60, gross_frac=0.10, quarters=48, closed_legs=100, clusters=20)`;
      - `p5 == dict(min_pos_q=2, max_q_share=0.60, top1=0.30, top3=0.60)`;
      - `p6 == dict(fee=1.5, slip=1.5, spread=3.0)`;
      - `p8 == (0.5, 1.5)`, `p9_sharpe == 1.0`, `p11_m == (3, 0.05)`, `ece_max == 0.05`, `ece_bins == 10`;
    - `FROZEN.cluster_gap_h == 72`, `FROZEN.k10_tol == 1e-9`, `FROZEN.invalid_max == 0.005`;
    - `FROZEN.forward`:
      - `days == 180`, `eval_quarter_share == 0.90`, `min_eval_days == 144`;
      - `p4 == dict(active_days=30, closed_legs=50, clusters=10)`;
      - `shift_max_h == 2`, `gap_h == 24`, `extension_days == 90`;
    - `FROZEN.deploy`: `E_265 == 265`, `E_10k == 10_000`, `E_ref == 10_000`, `d1 == 0.90`, `infra_usd_year == 144`.
  - `test_to_ns_rejects_ms_ints`: `to_ns(1_600_000_000_000)` raises `ValueError`; `from_ms(1_600_000_000_000) == 1_600_000_000_000_000_000`.
  - `test_decision_times_day`: `decision_times(to_ns("2023-01-01"), to_ns("2023-01-02"))` gives exactly 00:05, 08:05, 16:05.
  - `test_outer_folds_d0_2021`: 10 folds.
    - The first has test [2023-01-01, 2023-04-01), tr [2021-01-01, 2022-01-01), sel [2022-01-01, 2022-07-01) and cal [2022-07-01, 2023-01-01).
    - The last test window ends at 2025-07-01.
  - `test_outer_folds_late_d0_fails`: a D0 of 2021-08-01 yields 7 folds, and `windows.stage0_fail(d0)` is True (S-10.2).
  - `test_final_blocks`: tr [2021-01-01, 2024-07-01), sel [2024-07-01, 2025-01-01), cal [2025-01-01, 2025-07-01).
  - `test_v6_bounds_from_tag`:
    - `holdout_start == to_ns("2026-07-01")`;
    - `t_f == to_ns("2026-10-14")`;
    - looks equal 2027-01-12, 2027-04-12 and 2027-10-14;
    - a tampered blob, simulated by monkeypatching the reader, raises `SpecMismatch`.
  - `test_verify_spec_passes_on_freeze`.
  - `test_w_hold_overlaps_nothing_in_m6`.
- [ ] **Step 2: Run, verify failure.** `uv run pytest tests/v7/test_foundations.py -q` must fail on imports.
- [ ] **Step 3: Implement** the modules above. `verify_spec` and `v6_artifacts` shell out to `git -C <repo>` (pattern: `luna/v6/guard.py::_git`).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.** `feat(v7): foundations — constants, spec pinning, windows, folds`.

### Task 2: Guards — holdout seal, M6 embargo, endpoint allowlist, forward store

**Files:**
- Create: `luna/v7/guard.py`, `luna/v7/netguard.py`, `luna/v7/forward_store.py`.
- Test: `tests/v7/test_guard.py`, `tests/v7/test_netguard.py`, `tests/v7/test_forward_store.py`.

**Interfaces:**
- Consumes: `windows.W_HOLD`, `windows.m6`, `spec.verify_spec`.
- Produces:
  - `guard`:
    - `HoldoutBlocked(RuntimeError)`;
    - `HoldoutAuthorization`, constructible only by `authorize_holdout(approval_phrase: str, repo=None)`, following the V6 pattern: spec tag + code tag `v7-code-frozen` present, clean tree, HEAD == code tag, two exclusive markers keyed by `RUN_ID = "v7-" + SPEC_BLOB_SHA256[:16]`, pinned manifest;
    - `check_outcome_window(lo, hi, *, auth=None, now_ns) -> None`. It refuses an outcome window that overlaps `W_HOLD` without auth (raises `HoldoutBlocked`) and refuses any overlap with M6 before R_V6 (raises `EmbargoBlocked`);
    - `check_listing_only(path)`: `W_HOLD` file listings allowed, content refused.
  - `netguard`:
    - `Request(url, params: dict)`;
    - `check(req: Request) -> Allowed(kind="N1".."N6")`, raising `EndpointRefused` before any I/O;
    - `cdx_pair_registry` with `register_cdx_result(rows)`;
    - `check_wayback_response(ts, status, headers, redirected) -> None`, raising `GuardFailure` (N6: HTTP 200, no redirect, `Memento-Datetime` == ts).
  - `netguard.drop_m6_keys(keys: list[str]) -> list[str]` (N2). It drops keys whose period is in M6 and keeps `W_HOLD` keys as name / size / checksum only.
  - `forward_store`:
    - `ForwardStore(root)` with `write_raw(source_url, body: bytes, recv_ts) -> record` (sha256 recorded);
    - `extract_transport_id(catalog_json) -> list[str]` (EC-8: article id / code / URL only);
    - `read(...)`, which raises `SealedBeforeL7` unless `now_ns ≥ L7`.

- [ ] **Step 1: Write failing tests.** One refusal test per class in S-1.7 "Guard tests":
  - `test_refuses_cms_catalog_and_detail` (`www.binance.com/bapi/...`);
  - `test_refuses_current_state_fapi_with_old_endtime` (`fapi/v1/premiumIndex?endTime=...`);
  - `test_fundingrate_requires_bounded_endtime`: missing `endTime` is refused; `endTime ≥ 2025-07-01` is refused; `endTime = 2025-06-30T23:59:59Z` is allowed (N3);
  - `test_cdx_requires_to_bound` (missing `to`, or `to = 20250701000000`, is refused);
  - `test_wayback_only_registered_pairs`;
  - `test_wayback_redirect_or_memento_mismatch_is_guard_failure`;
  - `test_archive_file_whole_period_before_2025_07` (`...monthly/klines/X/15m/X-15m-2025-06.zip` allowed; `-2025-07.zip` refused);
  - `test_n2_drops_m6_keys`;
  - `test_exchangeinfo_status_fields_refused_as_history` (E8: `guard.historical_field("status")` raises).

  Guard tests:
  - `test_hold_window_needs_authorization`;
  - `test_m6_overlap_refused_even_with_auth` (a window straddling 2026-07-01 is refused);
  - `test_marker_exclusivity` (a second `authorize_holdout` raises).

  Forward store tests:
  - `test_forward_store_unreadable_before_l7`;
  - `test_transport_extraction_no_semantics`: `extract_transport_id` returns only ids / codes / urls; it never returns a title or body text (assert on the output keys).
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** The allowlist is an explicit table: host + path regex + required / allowed parameter sets. Anything not matched is refused.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.**

### Task 3: Data schemas and archive normalisation (klines, funding merge)

**Files:**
- Create: `luna/v7/data/__init__.py`, `luna/v7/data/schema.py`, `luna/v7/data/klines.py`, `luna/v7/data/funding.py`.
- Test: `tests/v7/data/test_schema.py`, `tests/v7/data/test_klines.py`, `tests/v7/data/test_funding_merge.py`.

**Interfaces:**
- Produces:
  - `schema.TABLES: dict[str, TableSpec(key: tuple, columns: tuple, known_at_rule: str)]` for R1–R12 and DT1–DT4, with the columns exactly as listed in §3. `R12` is marked `optional=True` and is valid with zero rows (EC-7).
  - `klines.union(monthly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame` (DEC-33), indexed by `open_ts` ns. It raises `IntegrityBlocker` on conflicting OHLCV for the same key.
  - `klines.normalise(df, unit: Literal["ms","us","ns"]) -> pd.DataFrame` (columns `open_ts, o, h, l, c, vol_base, vol_quote, n_trades, taker_buy_quote`, plus `known_at`).
  - `funding.merge(archive: pd.DataFrame, rest: pd.DataFrame) -> pd.DataFrame`.
    - Columns: `funding_ts, rate, interval_h_explicit (nullable), mark_at_settle (nullable), source, known_at`.
    - Archive is primary; REST fills only missing keys (±60 s match).
    - A material conflict raises `IntegrityBlocker`.
    - `known_at = funding_ts + 1 min`.
- [ ] **Step 1: Write failing tests:**
  - `test_union_fills_truncated_monthly_tail`;
  - `test_union_conflict_raises`;
  - `test_ingest_rejects_ambiguous_units` (Review Focus 1);
  - `test_funding_merge_archive_wins_rest_fills_gap`;
  - `test_funding_conflict_raises`;
  - `test_r12_valid_empty`;
  - `test_known_at_rules` (R3 `open_ts + 15 min`, R5 `+1 min`, R2 `+1 min`).
- [ ] **Steps 2–5** as in Task 1.

### Task 4: Contract instances (activity-based) and point-in-time membership

**Files:**
- Create: `luna/v7/data/instances.py`.
- Test: `tests/v7/data/test_instances.py`.

**Interfaces:**
- Consumes: R3 frame from Task 3.
- Produces:
  - `Instance(idx, first_bar_ts, last_trade_ts | None, end_known_at | None)`.
  - `build_instances(bars15: pd.DataFrame, data_end_ns) -> list[Instance]`.
    - Dormancy is ≥ 7 consecutive UTC calendar days without a volume > 0 15-minute bar.
    - `last_trade_ts` = the close of the last volume > 0 bar.
    - `end_known_at = last_trade_ts + 7·24 h` (EC-1).
    - The first instance starts at the first archived bar.
  - `current_instance(insts, t_ns) -> Instance | None`: the latest instance with `first_bar_ts ≤ t`. It returns `None` when that instance's `end_known_at ≤ t`.
  - `instance_of(insts, ts_ns) -> int | None`: membership of a row timestamp (dormant intervals → `None`).
  - `assign(df, insts, ts_col) -> pd.Series[int | None]`.
- [ ] **Step 1: Write failing tests:**
  - `test_dormancy_needs_seven_calendar_days`: 6 inactive days gives one instance; 7 gives two.
  - `test_zero_volume_bars_do_not_prevent_dormancy`.
  - `test_end_known_at_is_close_plus_7x24h`: last close 2022-05-12 16:15 → `end_known_at` 2022-05-19 16:15.
  - `test_current_instance_before_end_known`: it is still current at `end_known_at − 1 ns`, and `None` at `end_known_at`.
  - `test_relist_new_instance` (Review Focus 4): the new `first_bar_ts` is the open of the first new volume > 0 bar.
  - `test_dormant_rows_belong_to_no_instance`.
- [ ] **Steps 2–5.**

### Task 5: Funding intervals, reconciliation, interval_transition, r₈, U3 gap test

**Files:**
- Create: `luna/v7/data/intervals.py`.
- Test: `tests/v7/data/test_intervals.py`.

**Interfaces:**
- Consumes: the funding frame (Task 3) and `instance_of` (Task 4).
- Produces: `annotate(funding: pd.DataFrame, insts) -> pd.DataFrame`, which adds `instance`, `nominal_h` (nullable), `reconciled: bool`, `interval_transition: bool`, `invalid: bool` and `r8` (= rate·8 / nominal_h; NaN when invalid or undetermined).
  - **Nominal interval:** the explicit field; else the standard interval (1, 2, 4 or 8 h) matching the spacing to the previous record of the same instance within ±60 s; else the previous nominal interval (flagged).
  - **Reconciled:** the spacing equals the previous nominal or the record's own explicit interval within ±60 s.
  - **EC-9:**
    - a record with no explicit field and a spacing that matches a standard interval but not the previous nominal (or the previous nominal is undetermined) is reconciled with `interval_transition = True`;
    - the first record of an instance is reconciled only when it carries an explicit interval;
    - otherwise the record is `invalid`;
    - invalidity never propagates.
  - `u3_gap(prev_nominal_h, cur_nominal_h, spacing_ns) -> bool`: spacing > max(prev, cur)·H + 60 s.
- [ ] **Step 1: Write failing tests**, each a hand-built series taken from the Stage-0 cases in `docs/results/v7-stage0/ec/ec9_records.json` (shape only; synthetic values):
  - `test_explicit_controls`;
  - `test_spacing_inferred_transition_valid_flagged` (8 h → 4 h → 2 h with no explicit field: all valid; the 2nd and 3rd records have `interval_transition`);
  - `test_off_schedule_six_hour_invalid_successor_valid` (BNX shape: 6 h spacing → invalid; the next 8 h record → valid transition);
  - `test_explicit_conflicts_spacing_invalid` (FLM shape: spacing 4 h, previous 8 h, explicit 2 h → invalid);
  - `test_first_record_needs_explicit`;
  - `test_never_crosses_instance`;
  - `test_r8_normalisation` (`rate=0.0001`, 4 h → `r8 = 0.0002`);
  - `test_u3_gap_adjacent_rule`.
- [ ] **Steps 2–5.**

### Task 6: Delisting notices (U6 state machine) and terminal events

**Files:**
- Create: `luna/v7/data/notices.py`, `luna/v7/data/terminal.py`.
- Test: `tests/v7/data/test_notices.py`, `tests/v7/data/test_terminal.py`.

**Interfaces:**
- Produces:
  - `NoticeVersion(notice_id, version, kind, release_ts, capture_ts | recv_ts, text, sha256)`.
  - `known_at(v) = max(release_ts, capture_ts)`; forward: `recv_ts`.
  - `confident_match(v, symbol) -> bool`:
    - the exact symbol is a whole token (multiplier prefix included);
    - the context is unambiguously USDⓈ-M futures / perpetual;
    - no fuzzy matching.
  - `scheduled_ts(v, symbol) -> int | None`: unique, explicitly mapped timestamp only (DEC-37).
  - `U6State.active(instance, t_ns, halted_fn) -> bool`, per S-2.5:
    - activation at `known_at`;
    - deactivation by a later matched postponement or cancellation at its `known_at`;
    - the 72 h auto-clear only with a unique `scheduled_delist_ts` and no HALT in [sched, sched + 72 h];
    - a late `known_at` evaluates the 72 h test at `known_at`;
    - U6 ends with the instance.
  - `terminal.TerminalEvent(instance, cessation_ts, klass: Literal["A","B","C"], settle_ts | None, settle_price | None, source)`.
    - `classify(event, notice, settlement_source) -> TerminalEvent`. Class A requires an official time and an exact official price (R12 row); B has the time but no exact price; C has no notice or time, or a time that conflicts with the observed cessation (S-5.4, M3-1 rule: sched ≤ last close ≤ sched + 15 min).
  - `terminal.R12_EMPTY_OK = True`.
  - `terminal.reject_approximate(row)`: raises for any row whose source is not exact / official (EC-7).
- [ ] **Step 1: Write failing tests:**
  - `test_known_at_is_max_release_capture`;
  - `test_current_text_never_backdated` (no capture → never active);
  - `test_whole_token_match_only` (`XXXUSDT` matches; `1000XXXUSDT` doesn't match `XXXUSDT`; `XXX USDT-Margined Contract` without the token → no match);
  - `test_multiple_times_need_explicit_mapping`;
  - `test_postponement_deactivates`;
  - `test_72h_clear_only_with_unique_time`;
  - `test_u6_ends_with_instance`;
  - `test_terminal_class_b_c` (no exact price → B; conflict → C);
  - `test_r12_rejects_approximate_rows`.
- [ ] **Steps 2–5.**

### Task 7: Market references, HALTED, spread proxy, β̂

**Files:**
- Create: `luna/v7/market.py`, `luna/v7/spread.py`, `luna/v7/beta.py`.
- Test: `tests/v7/test_market.py`, `tests/v7/test_spread.py`, `tests/v7/test_beta.py`.

**Interfaces:**
- Produces:
  - `market.halted(bars1m, tau_ns) -> bool`: no volume > 0 1-minute bar opening in [τ − 60 min, τ).
  - `market.P(bars1m, bars15, tau) -> float | None`: the open of the 1-minute bar at τ; fallback, the open of the 15-minute bar at τ.
  - `market.first_bar_within(bars1m, tau, 60 min) -> (ts, open) | None` (data-gap rule).
  - `market.first_executable_after(bars1m, tau, instance) -> (fill_ts = close time, price = close) | None`. It uses only bars of the same instance (E10).
  - `market.mark_at_settle(funding_row, mark15, last15, settle_ts) -> float | None` (S-4.6 fallback order).
  - `spread.half_spread(bars15_instance, tau_ns) -> float | None`, exactly the S-4.5 estimator:
    - pairs of consecutive present bars;
    - bar k opens ≥ τ − 24 h − 15 min;
    - `known_at`(π_k) = the close of bar k+1, which must be ≤ τ − 1 min;
    - Ŝ = sqrt(max(mean π, 0));
    - ĥ = max(2·Ŝ / 2, 1 bp);
    - an empty pair set → `None`.
  - `beta.beta_hat(asset_close_1h, btc_close_1h, t_ns) -> float | None` (DEC-16). It uses the 30 d of hourly log returns ending at the last full hour ≤ t − 1 min, requires ≥ 500 joint observations, and returns clip(0.7·β_OLS + 0.3, 0, 3).
- [ ] **Step 1: Write failing tests:**
  - `test_halted_window_half_open`;
  - `test_no_retroactive_fill` (an exit due while halted fills at the first volume > 0 bar's close time, never at the last traded bar);
  - `test_no_cross_instance_executable` (Review Focus 4);
  - `test_mark_fallback_order`.

  Spread tests (hand-worked):
  - `test_ar_negative_mean_gives_floor`;
  - `test_ar_missing_bar_breaks_pair`;
  - `test_ar_single_pair`;
  - `test_ar_lambda_then_floor_order`;
  - `test_ar_scale_invariance`;
  - `test_ar_constant_prices_floor`;
  - `test_ar_known_at_excludes_unfinished_pair`;
  - `test_ar_matches_independent_reference`: compare against `tests/v7/ar_reference.py`, a separate transcription of CRAN `bidask` 2.1.5 `ar.R` (method `"AR"`, `sign=TRUE`, window mean, negatives → 0). Record AMB-01: CRAN/R is not installed here; literal equality against R output is pending an R run before code freeze.

  Beta tests:
  - `test_beta_requires_500_obs`;
  - `test_beta_shrink_and_clip`.
- [ ] **Steps 2–5.**

### Task 8: Universe gates U1–U8 and the D0 rule

**Files:**
- Create: `luna/v7/universe.py`.
- Test: `tests/v7/test_universe.py`.

**Interfaces:**
- Consumes: instances (Task 4), intervals (Task 5), U6 state (Task 6), `spread.half_spread` (Task 7).
- Produces:
  - `SymbolData(symbol, bars15, bars1m, funding_annot, insts, notices)`.
  - `gates(sd: SymbolData, t_ns) -> GateRow(u1..u8: bool, reasons: tuple[str, ...], max_known_at)`, per the S-2.3 table:
    - U2: median daily quote volume over the 30 UTC days fully ended before t;
    - U4: ≥ 95 % of the 96 last-price 15-minute bars in (t − 24 h, t] present, with zero volume counting as present;
    - U8: a volume > 0 15-minute bar whose close is in [t − 60 min, t − 1 min].
  - `eligible(row) = all(u1..u8)`.
  - `g_d0(row) = u1 ∧ u2 ∧ u3 ∧ u4`.
  - `d0_month(counts_by_decision) -> str | None` (S-1.8: the first month in which every decision has ≥ 30 symbols).
  - Exclusions per S-2.1: `contractType`, `quoteAsset`, `underlyingType`, the stablecoin list, BTCUSDT hedge-only, the 5 unknown-type symbols, unknown identity → exclude.
- [ ] **Step 1: Write failing tests:**
  - `test_u1_uses_current_instance`;
  - `test_u3_resets_at_instance`;
  - `test_u4_zero_volume_counts_present`;
  - `test_u8_window_inclusive_ends` (Review Focus 2);
  - `test_u8_false_blocks_entry_not_exit` (via the GateRow flags);
  - `test_u5_threshold_7_5bps`;
  - `test_u7_freshness`;
  - `test_known_at_assertion` (any input with `known_at > t − 1 min` raises `LeakageError`);
  - `test_instrument_exclusions`;
  - `test_d0_rule`.
- [ ] **Steps 2–5.**

### Task 9: Feature engine F1–F6, F8–F14 and preprocessing

**Files:**
- Create: `luna/v7/features.py`.
- Test: `tests/v7/test_features.py`.

**Interfaces:**
- Consumes: Tasks 4–8.
- Produces:
  - `FEATURES`, an ordered tuple of the exact column names. F7 and F15 are absent (Stage 0 dropped them).
  - `compute(sd, btc, universe_rows, t_ns, side) -> dict[str, float]`, per the §9 table:
    - side adjustment: carry-direction features × −s, plus a `side` indicator;
    - r₈ normalisation;
    - current-instance look-backs only;
    - F11 / F12 computed over the eligible universe at t.
  - `assert_pit(inputs, t_ns)`.
  - `Preprocessor.fit(X_fit) / transform(X)` for linear models: winsorize at the 1 / 99 % fit-fold quantiles, standardize (a zero-variance column → 0, not NaN), median-impute plus a missing-indicator column per feature. LightGBM gets the raw values.
- [ ] **Step 1: Write failing tests:**
  - `test_feature_set_exact` (names and order; F7 / F15 absent);
  - `test_future_spike_does_not_change_features` (L1: inject a spike 1 minute after t; every feature is unchanged);
  - `test_side_adjustment_sign`;
  - `test_features_reset_at_new_instance` (Review Focus 4);
  - `test_f13_caps_1e4`;
  - `test_f14_cost_formula` (`2·f + 2·ĥ + 2·slip`);
  - `test_preprocess_fit_only_on_fit_rows` (L5: transformers refuse test indices);
  - `test_preprocess_constant_and_nan_columns` (Review Focus 5).
- [ ] **Steps 2–5.**

### Task 10: Leg labels

**Files:**
- Create: `luna/v7/labels.py`.
- Test: `tests/v7/test_labels.py`.

**Interfaces:**
- Consumes: market, spread, beta, intervals, terminal.
- Produces:
  - `LabelResult(t, symbol, side, h, components: dict, net, y_reg_bps, y_pos, t_x_eff, label_known_at, invalid_reason | None)`, with exactly the S-5.3 components: `funding_leg, price_resid, fee_in, fee_out, ss_in, ss_out, hedge_cost, hedge_funding`.
  - `leg_label(inputs, t, side, h) -> LabelResult`. It implements S-5.3 – S-5.6:
    - taker fee, slip, `h_fill` at the fill times;
    - the pro-forma hedge is never executed;
    - a halted exit waits for the first executable bar of the same instance (stressed costs: half-spread max(`h_fill`, 25 bps), 3 × slip), with `t_x` = that fill time;
    - B / C terminal → invalid;
    - an A terminal settles at the R12 time and price with the taker fee and no spread;
    - halted at `t_e` (leg or BTC) → invalid;
    - a missing or invalid funding record in (`t_e`, `t_x`] → invalid;
    - a missing entry proxy → invalid;
    - a missing exit proxy → fallback 25 bps + 3 × slip, flagged;
    - `t_x` ≥ the window end → no label.
  - `label_known_at = t_x + 1 min`.
  - `invalid_rate(results) -> float`.
  - `guard.check_outcome_window` is called on every invocation.
- [ ] **Step 1: Write failing tests** (synthetic paths with hand-computed nets):
  - `test_net_identity` (net = the sum of the signed components);
  - `test_funding_window_half_open` (Review Focus 2);
  - `test_hedge_funding_sign`;
  - `test_halted_exit_waits_and_stresses`;
  - `test_terminal_bc_invalid`;
  - `test_terminal_a_settles_without_spread`;
  - `test_no_cross_instance_exit_fill`;
  - `test_entry_halted_invalid_incl_btc`;
  - `test_exit_proxy_fallback_flagged`;
  - `test_label_crossing_window_end_absent`;
  - `test_label_refuses_m6_and_hold_without_auth`.
- [ ] **Steps 2–5.**

### Task 11: Model families and the frozen search space (adds dependencies)

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (`uv add scikit-learn lightgbm`; record the resolved versions).
- Create: `luna/v7/models.py`.
- Test: `tests/v7/test_models.py`.

**Interfaces:**
- Produces:
  - `Config(family: Literal["en","lgb_reg","logit_en","lgb_clf"], params: dict, seed: int)`.
  - `search_space(h) -> list[Config]`: 6 EN + 6 LGB-reg per horizon, plus the fixed logit and LGB-clf. Total regressors across horizons = 36, classifiers = 6, for a total of 42.
  - `fit(cfg, X, y, *, X_sel=None, y_sel=None) -> Fitted`. LGB uses early stopping on (X_sel, y_sel) with l2 / logloss and a maximum of 1500 trees, and records `best_iteration`.
  - `refit(cfg, best_iter, X_fit, y_fit) -> Fitted`.
  - `Fitted.predict(X)`.
  - `ALLOWED_FAMILIES` (any other family raises).
  - Mapping of the spec's (α, l1_ratio) to sklearn: `ElasticNet(alpha=α, l1_ratio=…)` and `LogisticRegression(penalty="elasticnet", solver="saga", C=1/α…)`. The α→C mapping is AMB-02 (implementation clarification): use `C = 1/(α·n)` to match the sklearn ElasticNet objective scaling, documented.
- [ ] **Step 1: Write failing tests:**
  - `test_search_space_42` (counts and exact grids);
  - `test_lgb_fixed_params`;
  - `test_disallowed_family_raises` (`xgboost`, `catboost`, `mlp`);
  - `test_deterministic_fit_on_synthetic` (two fits → identical predictions);
  - `test_early_stopping_uses_sel_only`.
- [ ] **Steps 2–5.**

### Task 12: Temporal CV, purge, calibration, IC

**Files:**
- Create: `luna/v7/cv.py`.
- Test: `tests/v7/test_cv.py`.

**Interfaces:**
- Consumes: `windows.outer_folds`, `models`.
- Produces:
  - `admissible(label_known_at, block_start) -> bool`: `label_known_at ≤ T − 24 h`.
  - `split(samples, fold) -> dict[str, index]` (`tr`, `sel`, `cal`, `test`, purged).
  - `date_ic(pred, y, dates) -> float`: the mean over dates of Spearman correlation.
  - `select_hparams(configs, split) -> Config` (by `T_sel` date-level IC; never P&L).
  - `LinearCalibrator.fit(yhat, y, dates)`: date-equal-weight OLS with b clipped ≥ 0. The `b == 0` rule: entries iff a > 0, ranking by ŷ.
  - `IsotonicCalibrator.fit(p, ypos, dates)`: date-equal weights, outputs clipped to [0.001, 0.999].
  - `run_fold(fold, samples, horizon) -> FoldModel`: selection, refit on `T_fit`, calibrators on `T_cal` only, prediction on the test quarter.
  - `final_model(samples, horizon, cfg) -> FoldModel` (final blocks; no refit inside the holdout).
- [ ] **Step 1: Write failing tests:**
  - `test_purge_24h`;
  - `test_blocks_never_overlap`;
  - `test_cal_never_used_for_fit_or_selection` (instrument the fitter);
  - `test_linear_calibrator_b_clip_and_date_weights`;
  - `test_isotonic_clip`;
  - `test_date_ic_equal_weights`;
  - `test_permutation_canary_ic_near_zero` (L9a on synthetic data: permuting labels within dates gives |IC| ≤ 2 SE).
- [ ] **Steps 2–5.**

### Task 13: Arms and scores

**Files:**
- Create: `luna/v7/arms.py`.
- Test: `tests/v7/test_arms.py`.

**Interfaces:**
- Produces:
  - `a0_score(fl30_r8, side, h_star, cost_side) -> float`, computed as `c·h* − 1.5·2·cost − 5e-4`. `c = luna.carry.funding.carry_per_hour(side, fl30_r8 / 8)` (AMB-03, implementation clarification: §9 FL30 is in r₈ units; V5 `carry_per_hour` expects a per-hour relative rate). `cost_side = taker + ĥ(t) + slip`.
  - `a1_keep(p_cal) -> bool` (p ≥ 0.50).
  - `ml_score(yhat_cal) -> (rank_key, passes = yhat_cal > 0)`.
  - `ts_ewma_r8(series, t) -> float` (half-life 72 h).
  - `ts_ar1_r8(series_30d, steps) -> float`.
  - `a0_v5_reference(...)`: runs `luna.carry.strategy.decide` unchanged with the frozen V5 config (k=2, H=168, mult 1.5, buffer 5 bps, min hold 24 h). Reference only; its adapter is documented in AMBIGUITIES if inputs can't be mapped one-to-one.
- [ ] **Step 1: Write failing tests:**
  - `test_a0_formula_units`;
  - `test_a1_veto_threshold`;
  - `test_ml_hurdle`;
  - `test_ewma_half_life`;
  - `test_ar1_projection`;
  - `test_a0_v5_reference_uses_frozen_config` (config sha check, as in `luna/carry/outcome_guard.frozen_carry_config`).
- [ ] **Steps 2–5.**

### Task 14: Book — constructor, risk layer, residual hedge, execution, engine

**Files:**
- Create: `luna/v7/book/__init__.py`, `constructor.py`, `risk.py`, `hedge.py`, `execution.py`, `engine.py`.
- Test: `tests/v7/book/test_constructor.py`, `test_risk.py`, `test_hedge.py`, `test_execution.py`, `test_engine.py`.

**Interfaces:**
- Consumes: gates, scores, `market`, `spread`, `terminal`, `beta`.
- Produces:
  - `constructor.targets(cands_by_side, held, scores, gates, h_star, now) -> Targets`:
    - K=5 per side, top-K by score above the hurdle;
    - no same-decision flip, no cooldown;
    - equal weights with side gross 0.5·E and neutrality |Σ long − Σ short| ≤ 0.05·gross;
    - minimum hold h*;
    - after the minimum hold, exit on a gate failure, a rank > K+2 or a score below the hurdle;
    - resize only when the drift > 25 %, reduce-only between entries.
  - `risk.RiskLayer`: `pre_decision(state) -> RiskActions`, `on_tick(state)`, `allows_entry(asset)`.
    - Precedence: kill > leg stop > U6 > ineligibility > daily-loss pause > U8.
    - Leg stop: adverse move ≥ 15 % from the average entry → taker exit, 24 h cooldown.
    - Daily loss: equity ≤ 98 % of the 00:00 equity.
    - Drawdown kill: ≤ 85 % of the running 15-minute peak → flat, no re-entry.
    - Gross ≤ 1.0·E; per leg ≤ 0.5·E/K; liquidity cap 0.5 % of the 24 h quote volume (at E_ref = 10,000 in L-norm).
    - U4 / U5 (20 bps) / U7 failing on two consecutive decisions → exit.
    - U6 exit at the first decision ≥ `known_at` + 1 min.
    - Staleness → skip entries.
    - ML has no handle on any of this; enforced by type, since `RiskLayer` takes no score input.
  - `hedge.target(legs, betas, E) -> float`, `hedge.should_trade(target, current, E) -> bool` (S-7.4).
  - `execution.fill_taker(order, refs) -> Fill`; `execution.fill_maker(order, aggtrades, touch) -> Fill` (trade-through only, W = 30 min, remainder taker).
    - A halted instrument → `Pending` until the first executable bar of the same instance.
    - Terminal A → `Settlement`; terminal B / C → `RunInvalid("terminal settlement not reconstructible")`.
  - `engine.run(arm, inputs, window, h_star, model=None, execution="E_T", layer="L-norm") -> BookRun(fills, marks, funding_flows, daily, invalid: str | None, arm)`.
    - Events: decisions at the 8 h grid, 15-minute marks, funding settlements, and the terminal unwind at the last reference minute before the window end.
    - It calls `guard.check_outcome_window`.
- [ ] **Step 1: Write failing tests:**
  - `test_top_k_and_hurdle`;
  - `test_no_same_decision_flip`;
  - `test_equal_weight_and_neutrality`;
  - `test_empty_side_keeps_book_flat` (Review Focus 3);
  - `test_min_hold_blocks_non_risk_exit`;
  - `test_resize_drift_rule`;
  - `test_risk_precedence_and_ml_cannot_override` (the ML score can't add a gate-failing candidate, change size or cancel a forced exit; S-0.6);
  - `test_leg_stop_cooldown`;
  - `test_daily_loss_pause`;
  - `test_drawdown_kill_no_reentry`;
  - `test_hedge_thresholds`;
  - `test_maker_trade_through_only` (a trade at the touch does not fill);
  - `test_halted_pending_exit_no_synthetic_fill`;
  - `test_terminal_bc_held_invalidates_run`;
  - `test_bc_event_not_held_is_harmless`;
  - `test_terminal_unwind_before_window_end`;
  - `test_engine_deterministic`.
- [ ] **Steps 2–5.**

### Task 15: Accounting (§8), K10, P11 objects

**Files:**
- Create: `luna/v7/accounting.py`.
- Test: `tests/v7/test_accounting.py`.

**Interfaces:**
- Consumes: `BookRun` (Task 14).
- Produces:
  - `account(run) -> Ledger` with `F_leg, F_hedge, F_net, C_exec (fees / spread / slip; legs vs hedge), PR, NET, CAC`, `daily` (net, funding, fees, spread+slip, price; the columns sum to the equity change).
  - `episodes(run)`, `clusters(run, gap_h=72)`, `pr_c`, `mkt_c` (β̂ of the episode-opening decision), `rho_c`, `MKT = Σ mkt_c + PR_BTC`.
  - `k10_ok(ledger) -> bool` (tolerance 1e-9·Σ|fill notional|, and PR == the daily price column).
  - `per_asset_net(ledger) -> dict[str, float]` and `H`.
  - `net_trim(ledger) -> float` with m = max(3, ⌈0.05·n_clusters⌉).
  - `stressed(run, fee=1.5, slip=1.5, spread=3.0) -> Ledger` (P6: same fills, times and quantities; cost multipliers only).
- [ ] **Step 1: Write failing tests:**
  - `test_pr_summation_by_parts`;
  - `test_tick_before_fill_same_ts`;
  - `test_cluster_72h`;
  - `test_k10_invariant_holds_and_detects_break`;
  - `test_net_equals_sum_of_assets_plus_hedge`;
  - `test_daily_components_sum`;
  - `test_net_trim_m_rule`;
  - `test_p6_stress_only_costs_change`.
- [ ] **Steps 2–5.**

### Task 16: Statistics, selection, verdicts

**Files:**
- Create: `luna/v7/stats.py`, `luna/v7/selection.py`, `luna/v7/verdict.py`.
- Test: `tests/v7/test_stats.py`, `tests/v7/test_selection.py`, `tests/v7/test_verdict.py`.

**Interfaces:**
- Produces:
  - `stats.daily_returns(equity_00utc) -> np.ndarray`.
  - `stats.cbb_indices(T, ell, B, seed) -> np.ndarray[B, T]`: k = ⌈T/ℓ⌉ uniform starts, blocks wrap mod T, truncated to T. The same draws serve r and x.
  - `stats.p_centered(x, idx) -> float`: (1 + #{mean* − mean ≥ mean}) / (B + 1).
  - `stats.p_max_over_blocks(...)`.
  - `stats.iut(p_abs, p_diff) -> float`.
  - `stats.lower_bound(x, ells) -> float` (the minimum over ℓ of θ̂ − q₀.₉₅(θ̂* − θ̂)).
  - `stats.holm(pvals) -> adjusted`.
  - `stats.sharpe(r)` (√365, plus the Lo / Newey-West adjusted value).
  - `selection.choose(runs: dict[(arm, h), BookRun|Ledger]) -> Choice | Invalid`:
    - the S-10.6 validity precondition: all 6 candidates plus A0 at 24 / 72 / 168 h must be valid, else `INVALID RUN — historical terminal execution not reconstructible` (EC-2);
    - LB with ℓ = 14, B = 10,000;
    - A3 only if it beats the best A2 by ≥ 1 bp/day;
    - a tie (≤ 0.1 bp/day) goes to the longer horizon.
  - `selection.dev_kill(ledger_oof, fold_nets) -> list[str]` (S-10.7).
  - `verdict.holdout(...) -> Verdict`: P1–P11 exactly as tabled, the S-13.4 precedence, P8-classifier only for A1 claims, and the K11 mapping.
  - `verdict.K_TABLE`.
- [ ] **Step 1: Write failing tests:**
  - `test_cbb_shapes_and_wrap`;
  - `test_paired_draws`;
  - `test_p_value_formula`;
  - `test_iut_max`;
  - `test_lb_min_over_blocks`;
  - `test_holm_six`;
  - `test_selection_invalid_if_any_required_invalid` (including A0 at one horizon);
  - `test_simplicity_and_tiebreak`;
  - `test_dev_kill_conditions` (each one alone triggers);
  - `test_verdict_precedence` (`INVALID` > leakage > no-edge > maker-dependent > `INCONCLUSIVE` > `NO-GO` > `ADVANCE`);
  - `test_p4_active_day_definition` (≥ 48 of 96 quarter-hours with gross ≥ 10 % of E);
  - `test_p5_concentration`;
  - `test_p8_slope_band`;
  - `test_p11_m_rule`.
- [ ] **Steps 2–5.**

### Task 17: Equity layers and deployment gates (§14)

**Files:**
- Create: `luna/v7/deploy.py`.
- Test: `tests/v7/test_deploy.py`.

**Interfaces:**
- Produces:
  - `Filters(step, min_qty, min_notional, provenance: Literal["pit_historical","current_approximation","forward_pit"])`.
  - `round_order(qty, price, filters) -> qty | None` (fail closed).
  - `run_layer(run_inputs, E, filters_by_symbol) -> BookRun` (L-265 / L-10k). An infeasible required hedge forbids the entries.
  - `d_gates(lnorm, l265, infra_usd_year=144) -> dict(D1, D2, D3, label)`. The label is `approximate (current filters)` whenever any filter has provenance `current_approximation`.
  - `assert_not_backdated(filters)` (EC-6: `current_approximation` filters never enter R1, gates, features, labels or L-norm; enforced by type, since the L-norm engine path accepts no `Filters`).
- [ ] **Step 1: Write failing tests:**
  - `test_rounding_fail_closed`;
  - `test_hedge_infeasible_blocks_entries`;
  - `test_d1_share`;
  - `test_d3_infra_cost`;
  - `test_label_approximate_when_current_filters`;
  - `test_lnorm_path_rejects_filters`.
- [ ] **Steps 2–5.**

### Task 18: Forward protocol (§15)

**Files:**
- Create: `luna/v7/forward.py`.
- Test: `tests/v7/test_forward.py`.

**Interfaces:**
- Produces:
  - `t_f7(review_pass_ns) -> int` (the first 00:00 UTC ≥ 24 h after the pass).
  - `l7(t_f7, r_v6) -> int`: max(T_F7 + 180 d, R_V6) + 1 d. R_V6 unknown → `None`, and the look is refused.
  - `evaluable_quarter(inputs_present: dict[str,bool]) -> bool`: primary inputs only; book-top / depth / ticker are ignored; the filter snapshot is required only for the D layer.
  - `evaluable_day(quarters) -> bool` (≥ 90 %).
  - `decision_time(grid_t, evaluable_fn) -> int | None` (the first evaluable quarter ≤ 2 h after t).
  - `forward_verdict(...) -> Literal["INVALID RUN","NO-GO","INCONCLUSIVE","GO", "GO — not deployable at 265 USD"]`. GO needs H_F p < 0.05, P2, P3, P6, P11 and the evidence rules. P7 / P8 are reported only.
  - `a0_availability(events) -> (available_until | None, comparison: "complete"|"unavailable/incomplete")` (EC-11). An ML\* B / C held event → `INVALID RUN`.
  - `extension_allowed(inconclusive_reason) -> bool` (coverage only, once, 90 d).
- [ ] **Step 1: Write failing tests:**
  - `test_t_f7_rule`;
  - `test_l7_strict_default` (with `T_F7` in M6, L7 ≥ 2027-10-14);
  - `test_evaluable_ignores_diagnostic_feeds`;
  - `test_inconclusive_below_144_days`;
  - `test_ec11_a0_event_does_not_invalidate`;
  - `test_ml_event_invalidates`;
  - `test_no_outcome_before_l7` (raises);
  - `test_single_extension`.
- [ ] **Steps 2–5.**

### Task 19: Static spec-consistency check and end-to-end synthetic determinism

**Files:**
- Create: `tests/v7/test_spec_consistency.py`, `tests/v7/fixtures_v7.py`, `tests/v7/test_end_to_end_synthetic.py`.

**Interfaces:**
- Consumes: everything.
- [ ] **Step 1: Write the tests:**
  - `test_constants_match_spec_tables`: parse the frozen spec blob (via `spec.read_frozen_spec()`) and assert that every number in the S-2.3 gate table, S-4.3 / S-4.4, S-7.1 – S-7.4, S-10.5, S-13.2 and §15 appears with the value `FROZEN` holds. Regex per row; list every checked (row, value) pair in the test.
  - `test_every_dec_has_code_or_doc_owner`: each DEC-1 … DEC-47 and EC-n ID maps to a module or a documented non-code item in `luna/v7/__init__.py::SPEC_COVERAGE`.
  - `test_end_to_end_synthetic`: a 12-symbol, 400-day synthetic universe (seeded) runs through ingestion → instances → gates → features → labels → models (tiny) → book → accounting → verdict twice. The outputs are byte-identical, and `guard.check_outcome_window` is exercised with synthetic dates set inside a synthetic window object (no real-calendar `W_HOLD` overlap).
- [ ] **Steps 2–5.**

### Task 20: Implementation review package

- [ ] Run `uv run pytest -q` (whole repo) and `uv run ruff check luna/v7 tests/v7`; record the counts.
- [ ] Write `docs/results/v7-impl/IMPLEMENTATION-STATUS.md`: a requirement → module → test traceability table, the AMBIGUITIES summary, and the known gaps.
- [ ] Dispatch an independent implementation red-team on the branch diff vs `29959fc` (spec conformance, leakage, guard bypasses, determinism). Record its findings as Critical / Major / Minor in `docs/results/v7-impl/implementation-redteam-r1.md`.
- [ ] Commit. No code-freeze tag.
