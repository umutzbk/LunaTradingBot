# V7 implementation ambiguities

Entries are appended as they are found, numbered AMB-01, AMB-02, ... Format of one entry:

`## AMB-nn — <title>`, then: spec section, literal text, options, classification
(`implementation clarification` | `normative amendment required`), and what the code does now.

## AMB-01 — Shape of the Binance announcement catalog response for transport-level ID extraction

- Spec: S-1.7 M3-3, EC-8, DEC-45, section 15.
- Literal text: "the minimum mechanical parsing needed to extract the article ID / code / URL from the raw catalog response".
- Options: (a) collect every `id` / `code` / `url` key anywhere (would also pick up response status codes and catalog ids); (b) collect only from entries of `articles` lists.
- Classification: implementation clarification.
- Code now: `forward_store.extract_transport_id` reads only the entries of lists under an `articles` key (any depth), returns `code` (else `id`) and `url` as strings, de-duplicated in order; never titles, bodies or dates. The real shape is to be confirmed when the recorder is built (Task 18).

## AMB-02 — N2 bucket listing pagination

- Spec: S-1.7 N2.
- Literal text: "`delimiter=/` and `prefix=data/futures/um/...`" and "Refused: every other host, path or parameter".
- Options: (a) allow only `delimiter` and `prefix`; (b) also allow S3 paging parameters (`marker`, `max-keys`), which a listing of more than 1000 keys needs in practice.
- Classification: normative amendment required (if paging is needed).
- Code now: `netguard.check` allows exactly `delimiter` and `prefix`; any other N2 parameter is refused.

## AMB-03 — Numeric materiality threshold for a funding source conflict

- Spec: S-2.6 (Overlap), DEC-32.
- Literal text: "keys, rates and timestamps are compared. If they agree, archive provenance is kept. A material conflict is reported as a data-integrity blocker."
- Options: (a) any rate difference; (b) a tolerance. The spec gives no numeric materiality threshold.
- Classification: implementation clarification.
- Code now: `funding.merge` matches REST to archive keys within the frozen +-60 s tolerance; rates are parsed to float and conflict when `abs(archive - rest) > 1e-12` (`funding.RATE_ATOL`). Also an `IntegrityBlocker`: a REST record matching several archive records, two REST records mapping to the same archive settlement, or two records of one source within 60 s. On agreement the archive rate, timestamp and `source = "archive"` are kept, and a null `mark_at_settle` is filled from REST with `mark_source = "rest"` (S-4.6 first-tier mark; fills a null, overwrites nothing); REST rows with no archive key within 60 s fill the gap with `source = "rest"`. Kline conflicts (S-2.2 DEC-33) use exact equality of every value column after parsing.

## AMB-04 — Funding records outside any instance and undetermined nominal in the U3 gap test

- Spec: S-2.2, S-2.3 U3, S-2.6 (DEC-29/40/47, EC-9).
- Literal text: "funding records ... never cross an instance boundary"; "a gap exists when spacing > max(previous nominal interval, current nominal interval) + 60 s".
- Options: (a) records in a dormant interval (no instance) are treated as invalid; (b) they are excluded from annotation and never used. For `u3_gap` with an undetermined nominal: (a) treat as no gap; (b) use the known one; (c) fail closed.
- Classification: implementation clarification.
- Code now: `intervals.annotate` marks records with no instance as `in_instance=False`, `instance=None`, `reconciled=False`, `invalid=False`, `nominal_h` NA and `r8` NaN; they never serve as the previous record of an instance record. An invalid record keeps its timestamp as the spacing anchor for its successor but contributes the inherited previous nominal (never its own explicit/inferred value). `u3_gap` uses the known nominal when only one is undetermined and returns True (gap, fail closed) when both are undetermined.

Additional interpretations under AMB-04 (all implementation clarifications):
- `last_trade_ts` is the close (open + 15 min) of the last volume > 0 15m bar; `end_known_at = last_trade_ts + 7*24 h` (EC-1).
- A trailing dormancy ends the last instance only when 7 complete UTC calendar days lie between the last active day and `data_end_ns`.
- Instance membership is `first_bar_ts <= ts < last_trade_ts` (end-exclusive): a funding record exactly at the final bar close (e.g. a settlement at the cessation time) falls outside the instance and is never used. `instance_of`/`assign` are retrospective and must not be used as-of t; `current_instance` is the PIT function.
- A record with an explicit interval is reconciled if its spacing equals the previous nominal OR its own explicit interval (spec-literal). So explicit 4 with spacing 8 = previous nominal 8 is reconciled with nominal 4 (no transition flag; flags apply to records without an explicit field).
- `annotate` on a multi-symbol frame requires a dict covering every symbol and a unique index (ValueError otherwise).

## AMB-05 — USDⓈ-M context phrase list, scheduled-time mapping and the 72 h HALT test (Task 6)

- Spec: S-2.5 (DEC-37, DEC-40), S-5.4.
- Literal text: "the notice unambiguously refers to Binance USDⓈ-M futures / perpetuals, or to the exact relevant derivatives product"; "a timestamp ... explicitly maps that timestamp to that exact contract"; "not HALTED at any time in [scheduled_delist_ts, scheduled_delist_ts + 72 h]".
- Options: the spec gives no phrase list, no definition of "explicitly maps", and no sampling grid for "any time".
- Classification: implementation clarification.
- Code now (`luna/v7/data/notices.py`):
  - Symbol: whole token = bounded by non-alphanumeric characters, case-sensitive, on the raw text.
  - Context (after NFKC + casefold, so "USDⓈ-M" = "usds-m"): `USDM_PHRASES` = {"usds-m", "usdt-margined", "usds-margined"}, or both "perpetual contract" and "futures" present. Any "coin-m" / "coin-margined" mention makes the context ambiguous -> no match. A notice meeting neither condition is no match.
  - `scheduled_ts` only parses `YYYY-MM-DD HH:MM[:SS] [AM|PM] (UTC)`. Candidates are taken only from sentences containing the symbol token (sentences split at `.;!?` + whitespace or newline): a sentence with one timestamp maps it to every symbol it names (so "A, B and C ... at T" gives T to each); a sentence with several timestamps maps only via an ordered list "A and B ... T1 and T2 respectively" with equal counts and the symbol listed once. Exactly one distinct candidate overall, else None. 12-hour clocks with an hour outside 1-12 make the result None.
  - 72 h HALT test: `halted_fn` is sampled on a 1-minute grid over [sched, sched + 72 h] inclusive (S-5.4 reference minute); one halted sample means "halted". Each in-force delisting version is evaluated by itself (own scheduled time and known_at); U6 is active while any in-force version has not auto-cleared; a matched postponement/cancellation clears all earlier in-force versions.
  - Terminal classification (`terminal.py`): the R12 `source` must be in `EXACT_SOURCES` = {"official_historical", "official_formula_exact"}; a symbol token continued by `_` or by `-`/`.` plus an alphanumeric (e.g. `XXXUSDT_240628`) is no match; seconds in timestamps are parsed exactly; an R12 row must carry `contract_instance_id` = `<symbol>#<instance idx>` and the venue (default binance) or `classify` raises; a notice without `known_at` gives class C; class A also needs `settlement_ts` == the notice's scheduled time (otherwise B).

## AMB-06 — Spread-estimator reference without R, hourly beta series, window edges (Task 7)

- Spec: S-4.5 (DEC-38 required tests), S-5.2 (DEC-16), S-5.4.
- Literal text: "equality on fixed vectors with an independent reference implementation ... CRAN `bidask` 2.1.5, method `"AR"` with `sign = TRUE`"; "hourly log returns of a and BTC over the 30 d ending at the last full hour <= t - 1 min"; "first 1-minute bar ... within 60 min after tau".
- Options: R/CRAN is not installed here, so equality against R output cannot be run; the hourly price series source (1m vs 15m) and the inclusivity of the 60 min data-gap window are unstated.
- Classification: implementation clarification.
- Code now: R/CRAN not available; equality is checked against a Python transcription of `ar.R` (`tests/v7/ar_reference.py`, transcribed from the CRAN GitHub mirror source of bidask 2.1.5 `R/ar.R`; the R `shift`/`rmean(na.rm)`/signed-sqrt steps reproduced, negatives then set to 0); a run against R output is pending before code freeze. `beta.hourly_close_from_15m` takes the close of the 15m last-price bar that closes at each full hour; returns need two consecutive full-hour prices; joint observations = hours with both returns, hours in (L - 30 d, L] with L = floor((t - 1 min)/1 h)*1 h (max 720), OLS slope = cov/var(BTC) (None if var = 0). `first_bar_within` uses the closed interval [tau, tau + 60 min]. `first_executable_after` requires the bar to open >= `first_bar_ts` and close <= `last_trade_ts` of the (retrospective) instance; this is admissible because the fill is booked only after the bar closes. `spread.half_spread` takes `known_at(pi_k)` = max(bar k+1 `known_at`, its open + 15 min) and relies on the caller passing one instance's bars only. `beta.beta_hat` requires the `known_at` series of both prices (`hourly_known_at_from_15m`, which raises if the bars have no `known_at`) and drops any price with `known_at > t - 1 min`, or without a known_at entry; the caller passes only the current instance's bars.

## AMB-07 - Universe gate interpretations U2/U3/U4/U6/U7 (Task 8)

- Spec: S-2.2, S-2.3, S-2.5, S-1.8.
- Literal text: "median daily quote volume over the 30 UTC days fully ended before t"; "30 d of settled funding records of the current instance known by t, with no gap in the last 7 d"; "95 % of last-price 15-minute bars present in the last 24 h"; "the last settled funding record is at most 1 nominal interval + 15 min old".
- Options: U2 per instance or per symbol, empty day 0 or skipped; U3 span/tail gap; U4 window edges; U7 record validity.
- Classification: implementation clarification (the U2 empty-day rule and the U7 record choice could alter the universe: flagged for review).
- Code now (luna/v7/universe.py), consistent with the Stage-0 evaluation (d0_eval.py):
  - All inputs are filtered to known_at <= t - 1 min (notices too) before use.
  - U2: daily sums of vol_quote of the current instance's 15m bars; a day with no bars counts 0 (Stage 0 "zero" primary); median over the 30 UTC days d-30..d-1. Instance-restricted (conservative reading of the S-2.2 reset wording).
  - U3: current-instance records, invalid records dropped (missing). Pass = first valid record ts <= t - 30 d (Stage 0) and every adjacent valid pair whose later record is in the last 7 d passes u3_gap (the earlier record of the pair may precede the window). No tail gap (last record to t): freshness is U7 (Stage 0 had none either).
  - U4: the 96 bars opening in [L - 24 h, L) where L = last 15m grid close <= t - 1 min; present = finite close > 0 (zero volume counts); pass = count/96 >= 0.95 (at least 92).
  - U7: the last in-instance record known by t - 1 min (any validity), its own nominal interval; undetermined nominal fails.
  - U8: bar close = open_ts + 15 min in [t - 60 min, t - 1 min] inclusive.
  - Gate values are True = pass (U6 passes when inactive). d0_month requires all three decisions of every day of a month to be present and each >= 30; returns the month start YYYY-MM-DD.
  - 1m bars for the U6 72 h HALT test are filtered to known_at <= t - 1 min; a missing known_at column raises ValueError (fix round 1).
  - Fix round 1: U7 uses the last VALID (non-invalid) record of the current instance (invalid = missing, S-2.6). U3/U7 select funding PIT: funding_ts >= cur.first_bar_ts, known_at <= t - 1 min, no upper bound from last_trade_ts (DEC-44); interval flags are recomputed by intervals.annotate on that set with an open-ended instance, so the retrospective in_instance/instance columns are not used.
  - gates() drops rows with known_at > t - 1 min rather than raising LeakageError; LeakageError remains an invariant check (assert_pit) on max_known_at.
  - Missing, non-finite or non-positive multiplier => excluded (fail closed; not literal in S-2.1).

## AMB-08 - §9 feature formulas, side adjustment and preprocessing (Task 9)

- Spec: §9 (feature table, side adjustment, r8, instance boundary, linear / LightGBM preprocessing), S-2.2, S-2.6, S-4.3-S-4.5, S-5.2, S-10.4, L1, L5.
- Literal text: "Carry-direction features are multiplied by -s ... A side indicator is included"; F2-F14 rows of the §9 table (quoted per item below); "training-fold winsorization at 1 / 99 %, standardization, median imputation plus a missing-indicator column".
- Options: which features are "carry-direction"; window edges; log vs simple returns; realized-vol and Amihud definitions; F5 fallback granularity; F6 median sampling; F12 rank definition and which F5 values are ranked; F13 0/0; F14 units and per-horizon exposure; preprocessing order and inf handling.
- Classification: implementation clarification for every item except the side-adjustment scope, which is split out as AMB-09 (normative amendment required - user decision). Flagged for review (could change model inputs, not the economics of labels/books): (b) F14 ratio units; (c) F12 ranking all three F5 values.
- Code now (luna/v7/features.py; t = decision time, L = last full hour <= t - 1 min, W = window):
  - Common: every input row has known_at <= t - 1 min and belongs to the current instance (open_ts / funding_ts >= first_bar_ts; funding via universe.pit_funding, refactored out of the U3/U7 code; no upper bound before end_known_at). No current instance -> every symbol feature NaN (F11 BTC/market values still computed). Missing input -> NaN.
  - side = s (+1 / -1). (Scope of the side adjustment: see AMB-09.) CARRY_DIRECTION (x -s) = F1 (4), F3 (3), F5 (3), F11 cross-sectional median FL1 and share FL1 > 0, F12 (6 ranks). Rule: a feature is carry-direction iff it is a signed funding / premium level or change of the candidate, the market funding direction, or a rank of such a value. Not adjusted: F2, F4, F6, F8, F9, F10 (price momentum), F11 BTC return / vol, F13, F14.
  - F1: FL_W = mean r8 of valid records with funding_ts in (t - W, t], W = 24 h, 72 h, 168 h, 720 h (at 00:05 with 8 h funding: 3 / 9 / 21 / 90 records). Invalid records (S-2.6) and NaN r8 are missing (dropped).
  - F2: share of the 30 d valid records with sign(r8) == sign(FL1); lag-1 autocorrelation = Pearson corr of consecutive valid records in the 30 d window (invalid records dropped, not breaking pairs: a lag-1 pair can bridge a dropped invalid record, i.e. span two nominal intervals); NaN if either side is constant.
  - F3: OLS slope of r8 on time in days over the 7 d valid records (NaN if < 2 distinct times); FL1 - FL7; (FL1 - FL3) - (FL3 - FL7).
  - F4: number of adjacent valid 30 d records with r8_i * r8_{i-1} < 0 (zero is not a reversal) / number of VALID 30 d records (invalid records are neither counted in the denominator nor break a reversal pair: a pair can bridge a dropped invalid record).
  - F5: per 15m bar: premium-index close if finite, else (mark close - index close) / index close of the same open_ts (both finite, index > 0) - fallback per bar, not per window. Means over bars whose close (open + 15 min) is in (t - 8 h, t - 1 min] (32 bars) and (t - 24 h, ...]; "last premium close" = the latest such value with close in the 24 h window (else NaN).
  - F6: h(t) = spread.half_spread(current-instance bars, t) (fraction, not bps); 7 d median = median of the non-missing half_spread(tau) at tau = t - k * 8 h, k = 0..20 (the 8 h decision clock, 21 points incl. t), each point-in-time at tau.
  - F8: log of the U2 median daily quote volume over the 30 UTC days fully ended before t (universe.median_daily_quote_volume; empty day = 0; median <= 0 -> NaN); Amihud = mean over the 7 UTC days fully ended before t of |C(day end) / C(previous day end) - 1| / daily quote volume (C = close of the 15m bar closing at 00:00; days with a missing close or zero volume skipped); log(sum n_trades of those 7 days / 7) (NaN if 0 or no n_trades column). The divisor is always 7, regardless of instance age: days before the current instance start count as 0 trades (consistent with the U2 empty-day = 0 rule), so a < 7 d old instance gets a lower value rather than a missing one.
  - F9: sqrt(mean(r^2) * 24 * 365) of the hourly log returns r ending in (L - W, L] (W = 24 h, 7 d; hourly closes = beta.hourly_close_from_15m; no mean subtraction; returns needing a missing close are skipped; NaN if none). 365-day annualisation, consistent with F13's 3 * 365.
  - F10: simple returns C(L) / C(L - W) - 1 (W = 24 h, 7 d, 30 d; NaN if either close is missing, e.g. before the instance start); residual 7 d = (A(L)/A(L - 7 d) - 1) - beta * (B(L)/B(L - 7 d) - 1) with beta = beta.beta_hat at t (S-5.2), mirroring the S-5.3 price_resid; BTC from its own current instance.
  - F11: BTC 30 d simple return and 7 d realized vol (F10 / F9 definitions); median and share (> 0) of the finite FL1 of the eligible universe at t (rows from xs_input, eligible = universe.eligible(gates at t); rows built at another t, duplicates or max_known_at > t - 1 min raise).
  - F12: percentile rank (average rank of ties / n) of the candidate's FL1, FL7, FL30, and each of the three F5 values within {finite values of eligible symbols} with the candidate's own value included (it is one of them when eligible; a non-eligible symbol is ranked against the eligible set plus itself). Six columns.
  - F13: rvol_7d / (|FL7| * 3 * 365) and std(30 d valid r8, ddof = 1) / |FL30|, each min(., 1e4); denominator 0 with numerator > 0 -> 1e4; 0 / 0 or a missing input -> NaN.
  - F14: cost = 2 * taker (S-4.3: labels are taker, 0.0005) + 2 * h(t) + 2 * slip (0.0002), reported in bps; ratio = (|FL7| * 3 * h / 24) / cost with both in fractions (unitless; the literal bps denominator would only rescale by 1e4). Exposed as one column per horizon (f14_carry_over_cost_{24,72,168}h); features_for_horizon(h) gives the model columns for h with only its own F14 ratio (a spec-determined choice, not data-driven feature selection, L7).
  - L1: compute_row returns max_known_at of every row used (incl. universe rows) and asserts it <= t - 1 min; assert_pit(frames, t) raises LeakageError / ValueError (no known_at column).
  - Preprocessor (linear models): fit(X, decision_ts, fold) raises LeakageError if any decision_ts is outside T_fit = [fold.tr[0], fold.sel[1]) (T_cal, test, pre-D0); the S-10.3 purge of the sample set is the fold builder's job (features use no labels). Per column on finite fit values: numpy linear quantiles 1 / 99 %, clip, mean and population std (ddof 0) of the clipped values, constant clipped column (ptp = 0) -> 0; missing = NaN or +-inf, imputed with the median of the standardized fit values (0 for an all-missing column); indicator column <col>__missing (1.0 / 0.0) appended after the features. LightGBM: raw_matrix (float64, +-inf -> NaN).
  - Preprocessor callers must pass the columns of one horizon (features_for_horizon(h)): a column set with more than one F14 carry/cost horizon column raises ValueError (fix round 1).

## AMB-09 - Scope of the §9 side adjustment: directional price features (Task 9)

- Spec: §9 "Side adjustment. Carry-direction features are multiplied by -s, so that positive means favourable to the candidate's side. A side indicator is included."
- Literal text: only "carry-direction" features are multiplied by -s.
- Options: (A, literal) side-adjust only funding / premium (carry-direction) features; F10 momentum (24 h / 7 d / 30 d returns, 7 d residual return) and F11 BTC 30 d return stay un-adjusted. (B) side-adjust every signed directional feature (x +s for price returns so positive = favourable to the side, or equivalently x -s under a sign convention), which is what "positive means favourable to the candidate's side" suggests for price direction.
- Consequence: both sides are fitted by one model per horizon. Under (A) a linear model sees the same F10 / F11-BTC value for the long and the short candidate of a symbol; its coefficient therefore pushes both sides' scores in the same direction, and the additive side indicator cannot undo that (it only shifts the intercept by side; it cannot flip the sign of a slope). A linear model thus cannot use directional price information (e.g. "positive momentum favours longs") at all. LightGBM can (via splits on side), so (A) also tilts the A2-vs-A3 comparison.
- Classification: **normative amendment required - user decision.** Whether every signed directional feature is side-adjusted is a methodology choice the spec text does not make; it is not an implementation detail.
- Code now (luna/v7/features.py CARRY_DIRECTION): the literal reading (A) - controller ruling, fix round 1 of Task 9 - until the user decides. Switching to (B) is a one-line change of CARRY_DIRECTION plus a sign convention for price returns.
