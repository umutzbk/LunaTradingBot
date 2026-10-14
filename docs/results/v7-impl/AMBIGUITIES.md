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
- Code now: R/CRAN not available; equality is checked against a Python transcription of `ar.R` (`tests/v7/ar_reference.py`, transcribed from the CRAN GitHub mirror source of bidask 2.1.5 `R/ar.R`; the R `shift`/`rmean(na.rm)`/signed-sqrt steps reproduced, negatives then set to 0); a run against R output is pending before code freeze. `beta.hourly_close_from_15m` takes the close of the 15m last-price bar that closes at each full hour; returns need two consecutive full-hour prices; joint observations = hours with both returns, hours in (L - 30 d, L] with L = floor((t - 1 min)/1 h)*1 h (max 720), OLS slope = cov/var(BTC) (None if var = 0). `first_bar_within` uses the closed interval [tau, tau + 60 min]. `first_executable_after` requires the bar to open >= `first_bar_ts` and close <= `last_trade_ts` of the (retrospective) instance; this is admissible because the fill is booked only after the bar closes. `spread.half_spread` takes `known_at(pi_k)` = max(bar k+1 `known_at`, its open + 15 min) and relies on the caller passing one instance's bars only.
