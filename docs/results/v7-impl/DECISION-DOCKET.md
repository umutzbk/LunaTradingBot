# V7 implementation: consolidated decision docket (before code freeze)

| | |
|---|---|
| Frozen spec | `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md`, tag `v7-ml-spec-frozen` -> commit `29959fc563c0fc58430744810095cefa1d63c9a8`. The working-tree file was checked identical to the tagged blob (`git diff v7-ml-spec-frozen` empty). |
| Code | branch `feat/v7-ml-implementation`, HEAD `2dc7344`. All `file:line` citations refer to this HEAD (`luna/v7/...`). |
| Inputs | Independent implementation red-team round 1 (`implementation-redteam-r1.md`: M1-M8, m1-m11, unlogged divergences, misclassified AMBs) is incorporated. `docs/results/v7-impl/AMBIGUITIES.md` (AMB-01 ... AMB-28), `docs/results/v7-impl/CLARIFICATIONS-2026-10-15-round1.md` (user round 1), controller ledger `deferred-and-rulings.md` (R1 ... R37 and deferred minors), the code. |
| Scope | Every item that is not a purely mechanical clarification: every AMB sub-item classified "normative amendment required" / "user decision" / "borderline"; every implementation clarification that can change labels, ranking, trades, P&L, validity, GO / NO-GO or the deployment gates; every controller ruling that interprets the spec in an outcome-relevant way; code defects and missing components found while verifying. Items resolved by the user in round 1 are listed only in the last table (except the R35 reading, which the user asked to confirm). |
| Blocker rule | "Code-freeze blocker = yes" when the item can change a research outcome (labels, model ranking, trades, P&L, validity, verdict class, or a deployment gate) and is unresolved. An item whose recommendation is the current code is resolved by the user's confirmation. "Secondary" = only a reported secondary comparison can change; "deployment" = only D1-D3 / the "not deployable" label can change. |

Abbreviations in the "affects" column: **L** labels, **R** model inputs / ranking / selection, **T** trades, **P** P&L, **V** validity (INVALID RUN / integrity), **G** verdict class (GO / NO-GO / ADVANCE / INCONCLUSIVE), **D** deployment gates only, **S** secondary / reported only.

Classification: **IC** = IMPLEMENTATION CLARIFICATION; **NAR** = NORMATIVE AMENDMENT REQUIRED; **BUG** = CODE BUG — NO USER DECISION REQUIRED.

## Resolution status — user round 2 (2026-10-15)

Binding record: `CLARIFICATIONS-2026-10-15-round2.md`.

| Class | Items | Status |
|---|---|---|
| NAR (31) | DK-01, 05, 06, 10, 16, 28, 29, 36, 37, 52, 54, 59, 62, 63, 64, 66, 67, 72, 75, 79, 84, 89, 94, 95, 96, 97, 99, 100 | **APPROVED as recommended** |
| NAR override | SC-04 | **Implement the Kraken exploratory path before code freeze** (override 1) |
| NAR override | DK-69 | **Overridden:** first executable 1-minute bar within the 60-minute data-gap window, else the required path is INVALID (override 2) |
| NAR override | DK-70 | **Overridden:** no stale carry; mark → same-period last close, else the required path is INVALID (override 3) |
| IC (72) | all IC rows, incl. DK-39, DK-51, DK-76 | **Confirmed** as recommended (controller ruling R38, reading the user's approval as covering them; flagged in the report) |
| BUG (16) | SC-01, 02, 03, 05, 06, DK-09, 42, 77, 78, 105, 106, 110-114 | **Authorized**; implementation status in `IMPLEMENTATION-STATUS.md` |

The per-item "Recommendation" and "Blocker" texts below are kept as written at HEAD `2dc7344`; this table supersedes their open status.

## Summary table

| ID | Title | Affects | Class | Blocker | Recommendation (<= 12 words) |
|---|---|---|---|---|---|
| **Data / PIT** | | | | | |
| DK-01 | AMB-02 N2 bucket-listing paging | V | NAR | yes | No amendment; fail closed on truncated listings, use N1 URLs |
| DK-02 | AMB-03 funding source-conflict materiality | V | IC | yes | Keep exact equality after parsing (1e-12) |
| DK-03 | AMB-04 instance membership, out-of-instance records, undetermined nominal | L R T V | IC | yes | Keep current fail-closed readings |
| DK-04 | R9 explicit interval accepted at previous-nominal spacing | L R | IC | yes | Keep spec-literal reconciliation; report affected records |
| DK-108 | New instance current at first bar open (red-team m5) | none in practice | IC | no | Switch at first bar's known_at (L1 hygiene) |
| DK-05 | AMB-05(a) notice confident-match context | R T V | NAR | yes | Closed list; validate on Stage-0 notice set before freeze |
| DK-06 | AMB-05(b) scheduled time mapping, 72 h halt grid | R T V | NAR | yes | Keep rules; validate on Stage-0 notice set before freeze |
| DK-07 | AMB-05(c) terminal-event classification | L V | IC | yes | Keep exact-source class A; include in notice validation |
| DK-08 | AMB-06(b) beta hourly series and window | L R T | IC | yes | Keep 15m-close hourly series, (L-30 d, L] |
| DK-09 | AMB-06(a) CRAN bidask 2.1.5 equality test pending | L R T P | BUG | yes | Run real R bidask before freeze; no amendment |
| DK-10 | Data-gap substitute price: fill time | L T P | NAR | yes | Labels keep tau; book fills at substitute bar time |
| **Universe / features** | | | | | |
| DK-11 | AMB-07 U2 instance restriction, empty day = 0 | R T | IC | yes | Keep instance-restricted, empty day counts zero |
| DK-12 | AMB-07 U3 span and 7 d gap test | R T | IC | yes | Keep first valid record <= t-30 d, adjacent gaps |
| DK-13 | AMB-07 U4 / U8 window edges | R T | IC | yes | Keep (L-24 h, L] and inclusive U8 ends |
| DK-14 | AMB-07 U7 record choice and undetermined nominal | R T | IC | yes | Keep last valid record; undetermined fails |
| DK-15 | AMB-07 missing multiplier excludes instrument | R T | IC | yes | Keep exclusion (fail closed) |
| DK-16 | AMB-09 side adjustment of directional price features | R T P G | NAR | yes | Keep literal: carry-direction features only |
| DK-17 | AMB-08 F1-F4 windows and invalid-record pairs | R | IC | yes | Break lag pairs at invalid records (align with AR(1)) |
| DK-18 | AMB-08 F5 premium fallback granularity | R | IC | yes | Keep per-bar fallback |
| DK-19 | AMB-08 F6 7 d median sampling | R | IC | yes | Keep 21 points on the 8 h clock |
| DK-20 | AMB-08 F8 Amihud and trades-per-day | R | IC | yes | Keep current definitions, divisor 7 |
| DK-21 | AMB-08 F9 / F10 volatility and returns | R | IC | yes | Keep uncentred RV, 365 d, simple returns |
| DK-22 | AMB-08 F11 / F12 cross-section and ranks | R | IC | yes | Keep average-rank percentile incl. own value |
| DK-23 | AMB-08 F13 caps and 0/0 | R | IC | yes | Keep cap 1e4, 0/0 = NaN |
| DK-24 | AMB-08 F14 cost fee and per-horizon column | R | IC | yes | Keep taker fee; one F14 column per horizon |
| DK-25 | AMB-08 linear preprocessing order | R | IC | yes | Keep winsorize-standardize-impute order |
| DK-107 | AMB-08 feature table ratified as a whole | R | IC | yes | Ratify AMB-08 table (with DK-17 change) |
| DK-26 | K7 feature-dominance audit: definition, no automated check | G | IC | yes | Implement check: per fold and final, indicator merged |
| DK-27 | L9 canaries: pass criteria and scope | G | IC | yes | Fix canary(b) as reported demonstration; (a) per horizon |
| **Labels** | | | | | |
| DK-28 | AMB-10(b) BTC halted at the label exit | L R | NAR | yes | Keep invalid label until user rules |
| DK-29 | AMB-10(g) beta missing makes label invalid | L R | NAR | yes | Keep invalid label (no prior substituted) |
| DK-30 | AMB-10(c)/(i) reaching cessation uses retrospective end | L V | IC | yes | Keep retrospective last_trade_ts |
| DK-31 | AMB-10(d) window end for halted waits | L V | IC | yes | Keep fill strictly before window end |
| DK-32 | AMB-10(f) label funding window and missing rule | L | IC | yes | Keep head/tail DEC-29 gap test |
| DK-33 | AMB-10(h) halted exit without proxy; no instance | L | IC | yes | Keep 25 bps fallback; no instance invalid |
| **Models / CV / selection** | | | | | |
| DK-34 | AMB-11 alpha -> C mapping and solver settings | R S | IC | yes | Keep C = 1/(alpha n) and recorded settings |
| DK-35 | AMB-12(a) decision date = UTC day | R G | IC | yes | Keep UTC day pooling of three decisions |
| DK-36 | AMB-12(a2) side pooling in IC / calibration | R T P G | NAR | yes | Keep pooled cross-section (literal) |
| DK-37 | AMB-12(b) intercept when b clips to 0 | T P G | NAR | yes | Re-estimate a with b fixed at 0 |
| DK-38 | AMB-12(c) selection-phase preprocessing on T_tr | R | IC | yes | Keep T_tr-fitted preprocessing for candidate fits |
| DK-39 | T_fit excludes T_tr rows purged at T_sel start (red-team M7) | R | IC | yes | Keep: S-10.4 step 6 "each with the purge" |
| DK-40 | AMB-12(d)/(e) selection ties, families, classifier stop | R | IC | yes | Keep first-in-order ties, separate families |
| DK-41 | AMB-13(a)/(b) FL30 units; ML rank key | T P | IC | yes | Keep FL30/8 and raw-yhat ranking |
| DK-42 | AMB-13(c) A1 veto applied at exits too (red-team M8) | T S | BUG | yes | Apply veto to entries only (literal); fix code |
| DK-43 | AMB-26 solver non-convergence | R | IC | yes | Record and report; never void a run |
| DK-44 | AMB-14/15/27(a) reference arms not executed as books | S | IC | no | Report as decision-rule references only |
| DK-45 | AMB-22(a)/(b) bootstrap seed and quantile | R G | IC | yes | Keep frozen seed and inverted_cdf quantile |
| DK-46 | AMB-22(f) S-10.6 tie-break reference point | R G | IC | yes | Keep ties measured against the family maximum |
| **Book / risk / execution** | | | | | |
| DK-47 | AMB-16(a) no backfill of blocked top-K slots | T P | IC | yes | Keep no backfill (literal A1) |
| DK-48 | AMB-16(b) symbol qualifying on both sides | T | IC | yes | Keep: enter neither side |
| DK-49 | AMB-16(c) kept legs occupy slots | T | IC | yes | Keep: no push-out of kept legs |
| DK-50 | Partial side sizing and exact-neutrality scaling | T P | IC | yes | Keep per-leg cap binding; scale heavy side to light |
| DK-51 | R35 reading of AMB-16(e) (user confirmation) | T P | IC | yes | Confirm R35 |
| DK-52 | AMB-16(e) detail: risk_gross reduce taker under E_M | S | NAR | yes | Keep taker (risk order) |
| DK-53 | AMB-16(e) detail: post-exit evaluation, neutrality after cuts | T P | IC | yes | Keep; report non-neutral residuals |
| DK-109 | BTC hedge outside the S-7.3 gross limit (red-team m6) | T P | IC | yes | Keep: gross = leg gross; hedge capped by S-7.4 |
| DK-54 | AMB-16(f) "reduce-only between entries" | T P | NAR | yes | Keep: a leg is never increased |
| DK-55 | AMB-16(g) minimum-hold clock | T P | IC | yes | Keep: from entry decision time |
| DK-56 | AMB-16(h) decision exit gates U1-U7 at 7.5 bps | T P | IC | yes | Keep literal U1-U7, entry threshold |
| DK-57 | AMB-16(i)/AMB-25(a) liquidity cap units, qv24 window | T P | IC | yes | Keep E_ref scaling and trailing 96 bars |
| DK-58 | AMB-16(j)/R25 halted flags evaluated at t | T P | IC | yes | Keep PIT flags at t, recheck at fill |
| DK-59 | AMB-16(l)/R21 held leg unscored only because U8 false | T P | NAR | yes | Keep: no rank / hurdle exit that decision |
| DK-60 | AMB-17(a) leg-stop price, cadence, missing mark | T P | IC | yes | Keep fresh-mark ticks, executed avg entry |
| DK-61 | AMB-17(b) daily-loss / kill cadence, day start | T P | IC | yes | Keep 15-minute marks, sticky to 00:00 |
| DK-62 | AMB-17(c) ineligibility streak definition | T P | NAR | yes | Keep disjunction streak |
| DK-63 | AMB-17(d)/(e) cooldown scope; exit precedence | T P | NAR | yes | Keep per symbol from fill; kill>stop>U6>inelig |
| DK-64 | AMB-19(a)/AMB-17(f) ineligibility exit fill timing | T P | NAR | yes | Keep S-4.2 forced timing |
| DK-65 | AMB-18(a) hedge clip at 0.5 E | T P | IC | yes | Keep clip |
| DK-66 | AMB-18(b) hedge when a held leg's beta is missing | T P | NAR | yes | Keep current hedge, no trade |
| DK-67 | AMB-18(c) hedge trades at a stale decision | T P | NAR | yes | Keep hedge rule running at stale decisions |
| DK-68 | AMB-19(c) drawdown-kill details | T P | IC | yes | Keep |
| DK-69 | AMB-19(d) non-halted exit without reference price | T P V | NAR | yes | Keep S-5.4 stressed wait |
| DK-70 | AMB-19(e) stale leg marks | P V G | NAR | yes | Keep stale carry; report stale-mark counts |
| DK-71 | AMB-19(f) same-timestamp event order | P | IC | yes | Keep funding > tick > fills order |
| DK-72 | AMB-19(g) E_M quantity scale and maker reference mid | S | NAR | yes | Keep E_ref = 10,000 USD scaling |
| DK-73 | AMB-19(h) book funding with invalid record / missing mark | P V | IC | yes | Keep book-nothing plus K8 count |
| DK-74 | AMB-19(i)/(j) terminal check, unwind, last decision | T P V | IC | yes | Keep |
| DK-75 | AMB-25(b) staleness flag inputs | T P | NAR | yes | Keep hedge-instrument-only staleness |
| DK-76 | AMB-28 implementation details incl. R36 / R37 | T P | IC | yes | Confirm details (hedge flattened, in-flight entries) |
| DK-77 | maker_exit flag set when no maker order rests | S | BUG | yes | Narrow flag to a resting maker order |
| DK-78 | Hedge open in flight not cancelled by flatten | T P | BUG | yes | Cancel in-flight hedge-open tokens on flatten |
| **Accounting / verdict** | | | | | |
| DK-79 | AMB-21(a)/R27 N_a keyed by symbol | G | NAR | yes | Keep per symbol (conservative) |
| DK-80 | AMB-21(c) K10 tolerance; daily identity as integrity | V | IC | yes | Keep 1e-9 tolerance; daily_ok -> INVALID |
| DK-81 | AMB-21(d) "mean daily net" units (P6, P10, S-10.7) | G | IC | yes | Keep book-unit daily net everywhere |
| DK-82 | AMB-21(e) P11 Top_m details | G | IC | yes | Keep |
| DK-83 | AMB-22(c) Holm with a voided member | S | IC | yes | Keep m = 6, voided as p = 1 |
| DK-84 | AMB-22(g)/R29 E_M runs secondary; undecidable split | G | NAR | yes | Keep NO-GO with maker_split_undecidable |
| DK-85 | AMB-22(h) P4 definitions | G | IC | yes | Keep leg gross vs marked equity |
| DK-86 | AMB-22(i) P5 conventions | G | IC | yes | Keep |
| DK-87 | AMB-22(j) P8-classifier ECE bins | S | IC | yes | Keep equal-row bins |
| DK-88 | AMB-22(d)/(k) P2 / P3 / P7 / P8 / P9 conventions | G | IC | yes | Keep |
| DK-89 | AMB-27(b) K8 missing-funding share | V | NAR | yes | Keep book-level settlement share |
| DK-90 | AMB-27(e) holdout P7 / P8 / K8 input panels | G V | IC | yes | Keep valid eligible-candidate panel |
| DK-91 | AMB-27(d)/AMB-22(m) S-10.7 fold nets and ratio | G | IC | yes | Keep per-quarter nets; ratio only if F_net > 0 |
| **Deployment** | | | | | |
| DK-93 | AMB-23(a)/(b)/(c) step rounding, closes, reduces | D | IC | yes | Keep towards-zero rounding, unfiltered closes |
| DK-94 | AMB-23(d) infeasible required hedge | D | NAR | yes | Keep: drop all entries of that decision |
| DK-95 | AMB-23(e)/AMB-28(a') one-sided book after rounding | D | NAR | yes | Keep: forbid entries when result is one-sided |
| DK-96 | AMB-23(i) DEC-34 symbol without filters | D | NAR | yes | Keep: infeasible, counted non-constructible |
| DK-97 | AMB-23(j) D1 path dependence | D | NAR | yes | Keep path-dependent gate; report scaled share |
| DK-98 | AMB-23(f)/(h)/(k) E_T only, filter timing, D2 / D3 | D | IC | yes | Keep |
| **Forward** | | | | | |
| DK-99 | AMB-24(f) forward verdict precedence | G | NAR | yes | Keep section 15 order, evidence excluded from NO-GO |
| DK-100 | AMB-24(k) thresholds after the 90-day extension | G | NAR | yes | Keep absolute 144 days and 30 / 50 / 10 |
| DK-101 | AMB-24(b)/(c) evaluable day, gaps, decision shift | G | IC | yes | Keep |
| DK-102 | AMB-24(e) evidence counted on the whole book | G | IC | yes | Keep |
| DK-103 | AMB-24(g) "GO" vs "GO — not deployable" | G D | IC | yes | Keep |
| DK-104 | AMB-24(a)/(d)/(h)/(i)/(j) forward inputs, L7, EC-11, H_F | G V | IC | yes | Keep |
| DK-105 | Forward replay integration and forward refit missing | G V | BUG | yes (forward) | Build before the forward review; not a W_DEV blocker |
| **Guards / infra** | | | | | |
| DK-106 | Integer known_at not window-checked in ns_array (red-team m1) | V | BUG | yes | Window-check integer inputs before loaders are written |
| DK-110 | to_ns accepts floats (red-team m2) | V | BUG | yes | Refuse floats or window-check them |
| DK-111 | Frozen constants mutable at runtime (red-team m3) | G | BUG | yes | Freeze dicts (MappingProxyType / tuples) |
| DK-112 | account() raises on a required run (red-team m8) | V | BUG | yes | Map accounting errors to INVALID RUN (K10) |
| DK-113 | Empty decisions frame lacks killed column (red-team m9) | V | BUG | yes | Build frames with schema columns |
| DK-114 | DT4 research flag is a caller convention (red-team m10) | V | BUG | yes | Research runs always record research=True |
| **Scope / completeness (red-team M1-M5)** | | | | | |
| SC-01 | M1 real-scale feasibility (memory / time) | V | BUG | yes | Rewrite data access for scale before freeze |
| SC-02 | M2 L9 canaries, K7 audit, structured LeakageReport | G | BUG | yes | Implement before freeze; verdict requires the report |
| SC-03 | M3 fetch layer, read-only loaders, K9 log, E6 path, W_HOLD feature guard | V G | BUG | yes | Implement before freeze (DEC-19: HEAD == code tag) |
| SC-04 | M3 Kraken exploratory path: ship or defer | S | NAR | yes | User decides: ship now or defer under own review |
| SC-05 | M4 DEC-19 preconditions; marker / pin re-check | V G | BUG | yes | Enforce in authorize_holdout and outcome guard |
| SC-06 | M5 canonical R_V6 / L7 source; M6-span check | V | BUG | yes | One hash-checked release record; refuse caller spans |

---

## A. Data / PIT

### DK-01 — AMB-02: N2 bucket-listing pagination
1. **ID / title:** DK-01, N2 listing paging parameters.
2. **Spec:** S-1.7 table row N2: "`https://s3-ap-northeast-1.amazonaws.com/data.binance.vision` with `delimiter=/` and `prefix=data/futures/um/…` (bucket listing)"; S-1.7: "**Refused:** every other host, path or parameter."
3. **Code now:** `netguard.check` accepts N2 only with exactly `{delimiter, prefix}` (luna/v7/netguard.py:168-171). No ingest code calls N2 yet (no caller outside netguard.py). Nothing detects a truncated listing (`IsTruncated`).
4. **Why ambiguous:** S3 returns at most 1,000 keys per response; a listing of a daily-file directory (e.g. one symbol's daily klines, ~2 files per day over years) exceeds 1,000 keys and needs `marker` / `max-keys`, which the allowlist refuses. Without paging a listing is silently incomplete.
5. **Affects:** V (Stage-0-type completeness of `W_DEV` / `W_HOLD` file sets; indirectly the universe and labels if files are missed).
6. **Recommendation:** no amendment. N2 is used only for listings that fit one page; any response with `IsTruncated=true` fails closed; daily files are addressed directly as exact N1 URLs (+ `.CHECKSUM`) built from the symbol and date. Reason: keeps the allowlist literal and never yields a silently partial listing.
7. **Alternatives:** (b) amend N2 to also allow `marker` (and `max-keys`), with the same prefix check and per-page M6-key dropping.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (only if alternative (b) is chosen; the recommendation needs a truncation check in the future ingest code, no amendment).
9. **Blocker:** yes (the ingest path must be fixed before any `W_DEV` data build).

### DK-02 — AMB-03: materiality of an archive / REST funding conflict
1. **ID / title:** DK-02, funding conflict tolerance.
2. **Spec:** S-2.6: "keys, rates and timestamps are compared. If they agree, archive provenance is kept. A material conflict is reported as a data-integrity blocker. One source never silently overwrites the other." "Timestamps are compared with the frozen ±60 s tolerance."
3. **Code now:** rates conflict when `abs(archive - rest) > 1e-12` (`RATE_ATOL`, luna/v7/data/funding.py:24, :99); keys matched within ±60 s; multiple matches are an `IntegrityBlocker`.
4. **Why ambiguous:** "material" has no number. A loose tolerance hides real conflicts; an exact one may raise blockers for float formatting differences (archive CSV vs REST JSON strings).
5. **Affects:** V (data-integrity blocker stops the build).
6. **Recommendation:** keep exact equality after parsing to float (1e-12). Reason: the most conservative reading; any genuine blocker is surfaced for a human decision rather than absorbed.
7. **Alternatives:** relative tolerance (e.g. 1e-9) or decimal-string equality.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-03 — AMB-04: instance membership, records outside instances, undetermined nominal interval
1. **ID / title:** DK-03.
2. **Spec:** S-2.2: "Bars and funding records dated inside a dormant interval belong to no instance. They never bridge two instances, never enter a feature, gate, label or book cash flow"; S-2.6: "The first record of an instance without an explicit field has no adjacent spacing, so its interval is undetermined and it is flagged." "The first record of an instance is reconciled only if it carries an explicit interval." "A genuinely unreconciled / off-schedule record ... is individually marked **invalid**"; S-2.3 U3: "A gap between adjacent settlements exists when their spacing > max(previous nominal interval, current nominal interval) + 60 s".
3. **Code now:** out-of-instance records get `r8` NaN and are never used (luna/v7/data/intervals.py:69-70); the first record without an explicit field is invalid (intervals.py:71-74); an unreconciled record inherits the previous nominal and is invalid (intervals.py:80-93); `u3_gap` uses the known nominal when one side is undetermined and returns gap = True when both are (intervals.py:135-140); instance membership is `first_bar_ts <= ts < last_trade_ts` (AMB-04 note), so a settlement exactly at the cessation close is outside the instance.
4. **Why ambiguous:** the spec says "flagged" for the first record, and is silent on the gap test with undetermined intervals and on the end-exclusive membership boundary.
5. **Affects:** L (funding window validity), R (U3, features), T, V.
6. **Recommendation:** keep all current readings. Reason: each is the fail-closed reading and none invents an interval.
7. **Alternatives:** first record valid-but-flagged (r8 undetermined -> still missing); undetermined nominal = no gap; end-inclusive membership.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-04 — R9: explicit interval accepted because spacing equals the previous nominal
1. **ID / title:** DK-04.
2. **Spec:** S-2.6: "The explicit historical `funding_interval_hours` field controls where available." "A record is reconciled when its spacing from the previous settlement of the same instance equals, within ±60 s, the previous record's (determined) nominal interval or the record's own explicit interval."; §9 "r₈ = r · 8 / interval_h".
3. **Code now:** a record with explicit 4 h and spacing 8 h (= previous nominal) is reconciled with nominal 4 h, so r8 = rate · 8 / 4 (luna/v7/data/intervals.py:76-84).
4. **Why ambiguous:** literal, but at a regime change it can double r8 for one record (the explicit field controls while the spacing reflects the old regime). Ruling R9 flagged it as a possible amendment.
5. **Affects:** L (funding is paid in raw rate, unaffected), R (r8-based features, U3 nominal).
6. **Recommendation:** keep the spec-literal rule; report the count of such records in the data build. Reason: the frozen text is explicit on both clauses.
7. **Alternatives:** require spacing to match the record's own explicit interval (else invalid).
8. **Classification:** IMPLEMENTATION CLARIFICATION (spec-literal; any change would be normative).
9. **Blocker:** yes (confirmation).

### DK-05 — AMB-05(a): confident-match context of a delisting notice
1. **ID / title:** DK-05.
2. **Spec:** S-2.5 (DEC-40): "the exact contract symbol ... appears as a whole token in the archived title or body; the notice unambiguously refers to Binance USDⓈ-M futures / perpetuals, or to the exact relevant derivatives product. An ambiguous token or context is no match. No fuzzy or semantic inference is permitted."
3. **Code now:** `USDM_PHRASES` = {"usds-m", "usdt-margined", "usds-margined"} or both "perpetual contract" and "futures" (luna/v7/data/notices.py:30-34, :76); any "coin-m" mention makes the context ambiguous; symbol token = case-sensitive, non-alphanumeric bounded, `_`/`-x` continuations refused.
4. **Why ambiguous:** the spec gives no phrase list; context is evaluated notice-wide, not per sentence. Red-team M6: a real notice naming "USDⓈ-M XXXUSDT" plus a COIN-M product (or COIN-M boilerplate) gives no match, so U6 never activates, the leg is held into cessation and a held C event makes all nine `W_DEV` runs INVALID (S-10.6, K11). Stage 0 counts 14 B / 12 C eligible terminations, and U6 is the main protection. "No match is acceptable" is therefore not neutral for validity: the list choice decides whether the selection run is valid at all.
5. **Affects:** R / T (U6 activation, candidate set, U6 exits), V (B / C exposure -> `W_DEV` INVALID RUN).
6. **Recommendation:** keep a closed list with no semantic inference. Before freeze, validate the matcher against the Stage-0 notice set (`docs/results/v7-stage0/m31/notices.json`, `delisting_coverage.csv`), outcome-free: report match / no-match and the reason per notice. The user then ratifies the list, and the notice-level COIN-M veto vs a sentence-level veto, from that report. Reason: DEC-40 forbids fuzzy matching, so the list itself is the methodology choice and has to be fixed from evidence that contains no outcomes.
7. **Alternatives:** larger phrase list; sentence-level (symbol-sentence) context and COIN-M veto.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (reclassified from IC after red-team M6: the phrase list is not pinned by the spec text and decides `W_DEV` validity).
9. **Blocker:** yes.

### DK-06 — AMB-05(b): scheduled delisting time and the 72 h halt test
1. **ID / title:** DK-06.
2. **Spec:** S-2.5 (DEC-37): "If several timestamps appear, one is used only when the notice explicitly maps that timestamp to that exact contract or product; otherwise `scheduled_delist_ts` = null." "if the instrument is not HALTED (S-5.4) at any time in [`scheduled_delist_ts`, `scheduled_delist_ts` + 72 h], U6 deactivates at `scheduled_delist_ts` + 72 h".
3. **Code now:** timestamps parsed only from sentences containing the symbol; one timestamp per sentence maps to every symbol named; several timestamps only via "respectively" lists (luna/v7/data/notices.py:95-125); the halt test samples a 1-minute grid over the closed interval (notices.py:37, :158-166).
4. **Why ambiguous:** "explicitly maps" and "at any time" are undefined. Red-team M6 groups the sentence rules with the phrase list: the mapping decides the 72 h auto-clear, so it decides re-entry into a contract that may later cease (B / C exposure).
5. **Affects:** R / T (U6 auto-clear, re-eligibility), V (re-entry before a B / C cessation).
6. **Recommendation:** keep the sentence-scoped mapping and the 1-minute grid. Validate them in the same outcome-free Stage-0 notice report as DK-05 (`docs/results/v7-stage0/m31/notices.json`, `delisting_coverage.csv`) before freeze: list `scheduled_ts` per matched instance, or null with its reason. Reason: a mapping failure gives null, which keeps U6 active until an explicit cancellation (conservative); the grid equals the S-5.4 halt resolution.
7. **Alternatives:** notice-level single timestamp; coarser grid.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (reclassified with DK-05 after red-team M6).
9. **Blocker:** yes.

### DK-07 — AMB-05(c): terminal-event classification A / B / C
1. **ID / title:** DK-07.
2. **Spec:** S-5.4: "A — exact settlement reconstructible: the official scheduled settlement time comes from an immutable timestamped notice (S-2.5), and the exact official settlement price comes from an official historical source or an exact reproduction of the official formula."; R12 "only exact official values or exact reproductions of the official formula".
3. **Code now:** class A only when the R12 `source` is in {"official_historical", "official_formula_exact"} and its `settlement_ts` equals the notice's scheduled time; otherwise B; a notice without `known_at` gives C (luna/v7/data/terminal.py:28, :52, :92).
4. **Why ambiguous:** the spec does not name source codes or say what to do when R12's time differs from the notice time.
5. **Affects:** L (label invalid), V (a held B / C event is INVALID RUN).
6. **Recommendation:** keep. Reason: strict and consistent with the `W_DEV` audit (0 A / 14 B / 12 C; R12 empty by design). Include the classification of each Stage-0 termination in the outcome-free notice validation of DK-05 (`docs/results/v7-stage0/m31/notices.json`, `delisting_coverage.csv`), before freeze.
7. **Alternatives:** accept R12 time even if it differs from the notice.
8. **Classification:** IMPLEMENTATION CLARIFICATION. Re-evaluated against red-team M6. The red-team's argument (AMB-05 decides validity) is accepted for the matcher (DK-05 / DK-06). It does not change this item: with R12 empty by design no event can be class A, so B vs C cannot change any outcome (both invalidate a held position). The A rule is also pinned by the S-5.4 text ("an official historical source or an exact reproduction of the official formula").
9. **Blocker:** yes (confirmation).

### DK-08 — AMB-06(b): hourly series for beta
1. **ID / title:** DK-08.
2. **Spec:** S-5.2: "inputs: hourly log returns of a and BTC over the 30 d ending at the last full hour ≤ `t` − 1 min, requiring ≥ 500 joint observations; formula: β̂ = clip(0.7·β_OLS + 0.3·1.0, 0, 3)".
3. **Code now:** hourly price = close of the 15m last-price bar closing on the hour (luna/v7/beta.py:24-29); returns need two consecutive hourly closes; joint hours in (L − 30 d, L], L = floor((t − 1 min)/1 h)·1 h; prices with `known_at` > t − 1 min dropped; OLS slope = cov/var (beta.py:67-90).
4. **Why ambiguous:** the price source (1m vs 15m bars) and window edges are not stated.
5. **Affects:** L (q_B, price_resid), R (F10 residual), T (hedge size, entry block when missing).
6. **Recommendation:** keep. Reason: R3 15m closes on the hour equal the 1m close at that minute boundary for complete data and are already instance-assigned.
7. **Alternatives:** 1m closes; [L − 30 d, L].
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-09 — AMB-06(a): CRAN `bidask` 2.1.5 equality test not yet run
1. **ID / title:** DK-09.
2. **Spec:** S-4.5 "Required tests (before the code freeze): ... equality on fixed vectors with an independent reference implementation of the published equation: CRAN `bidask` 2.1.5, method `"AR"` with `sign = TRUE`, negative window values then set to 0".
3. **Code now:** equality is tested against a Python transcription of `ar.R` (`tests/v7/ar_reference.py`); estimator in luna/v7/spread.py:57-65. No run against R output (AMBIGUITIES AMB-06: "a run against R output is pending before code freeze").
4. **Why it diverges:** a transcription by the same team is not the named independent CRAN implementation; the spec makes the test a pre-freeze requirement.
5. **Affects:** if the estimator were wrong: L (spread costs), R (F6, F14, U5), T, P.
6. **Recommendation:** install R + `bidask` 2.1.5, generate the fixed-vector outputs, commit them as test fixtures, and assert equality before the freeze. Reason: literal requirement; no decision needed.
7. **Alternatives:** the user waives the R run and accepts the transcription (that would be a normative amendment of S-4.5).
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED (missing mandatory pre-freeze test).
9. **Blocker:** yes.

### DK-10 — Data-gap substitute price: fill time (AMB-10(a), AMB-10(i), AMB-19(d), AMB-21(b) docket line)
1. **ID / title:** DK-10.
2. **Spec:** S-5.4: "**Data gap at τ (not halted):** use the first 1-minute bar within 60 min after τ. If there is none, the label is invalid."; S-5.2 "`P(τ)` = the open of the 1-minute last-price bar starting at τ"; S-8.1 "Every fill is booked at its own timestamp."; S-4.2 "Forced risk exits fill on the first 1-minute bar after the trigger + 1 min."
3. **Code now:** labels: the substitute bar's open replaces P(τ) and the fill time stays τ (luna/v7/refs.py:82-93; labels.py:194-202, :232-236); the funding window is decided by the nominal τ. Book: `taker_at` books the fill AT τ with the later bar's open (luna/v7/book/execution.py:234-247, ref at :200-203), while `forced_at` books at the substitute bar's open time (execution.py:250-265). B(e) at fills uses the same gap rule (engine.py:267-271).
4. **Why ambiguous / divergence:** the spec fixes which price, not which time. In the book, booking at τ a price that only exists up to 60 min later is a forward-looking substitute and is inconsistent with the forced-exit path and with S-8.1.
5. **Affects:** L (settlement inclusion for 1/2/4 h contracts), T / P (fill price/time, funding at the fill, B(e)).
6. **Recommendation:** labels keep τ (S-5.3 is a fixed-time formula); in the executed book, a fill priced by the gap rule is booked at the substitute bar's open time (as `forced_at` already does), and B(e) is taken at that time. Reason: S-8.1 literal and one consistent rule in the book.
7. **Alternatives:** (i) keep τ everywhere (current); (ii) require exact P(τ) for fills in the book (missing -> no entry / S-5.4 wait for exits).
8. **Classification:** NORMATIVE AMENDMENT REQUIRED. Reclassified after the red-team's misclassified-AMB list (AMB-10(a)/(i)). The spec fixes the price but not the time, three readings are viable, and the choice sets fill prices, funding inclusion and B(e).
9. **Blocker:** yes.

### DK-108 — Red-team m5: a new instance becomes current at its first bar's open
1. **ID / title:** DK-108.
2. **Spec:** S-2.2 "At `t`, the current instance is the latest instance with `first_bar_ts` ≤ `t`"; S-2.3 "All inputs must have `known_at` ≤ `t` − 1 min"; R3 `known_at` = open_ts + 15 min.
3. **Code now:** `current_instance` uses `first_bar_ts <= t` (luna/v7/data/instances.py:56-64).
4. **Why ambiguous / divergence:** the S-2.2 sentence is literal, but the volume > 0 that defines the first bar is known only at its close. For up to 15 min the switch uses data not yet known (L1).
5. **Affects:** none in practice. U1 fails for 60 d at a new instance (no candidate, no label). A held position of the old instance has already been settled, invalidated or terminally checked at `last_trade_ts`, at least 7 d earlier.
6. **Recommendation:** switch on `first_bar_ts + 15 min ≤ t − 1 min` (the bar's `known_at`). Reason: L1 governs every input; no outcome changes.
7. **Alternatives:** keep the literal `first_bar_ts ≤ t`.
8. **Classification:** IMPLEMENTATION CLARIFICATION (two literal rules conflict; immaterial).
9. **Blocker:** no (no outcome effect; L1 hygiene fix).

## B. Universe / features

### DK-11 — AMB-07 U2: instance restriction and empty days
1. **ID / title:** DK-11.
2. **Spec:** S-2.3 U2: "median daily quote volume over the 30 UTC days fully ended before `t` ≥ 20,000,000 USD"; S-2.2: "At a new instance, `first_bar_ts`, U1 listing age, U3 funding history and every rolling funding statistic and feature (§9) reset".
3. **Code now:** daily sums of the current instance's PIT 15m bars; a day without bars counts 0; median of 30 days (luna/v7/universe.py:157-180).
4. **Why ambiguous:** U2 is not listed among the reset quantities; empty-day treatment unstated.
5. **Affects:** R / T (eligibility).
6. **Recommendation:** keep. Reason: conservative and identical to the Stage-0 D0 evaluation.
7. **Alternatives:** per symbol across instances; skip empty days.
8. **Classification:** IMPLEMENTATION CLARIFICATION. Re-evaluated (red-team misclassified list): the rule is outcome-changing, but it is pinned by the frozen D0 evaluation. D0 = 2021-01-01 (S-1.8, S0-7) was established with the "empty day = 0" primary, so any other reading would reopen a frozen Stage-0 result. That reopening would be the normative act.
9. **Blocker:** yes (confirmation).

### DK-12 — AMB-07 U3: 30 d span and the 7 d gap test
1. **ID / title:** DK-12.
2. **Spec:** S-2.3 U3: "≥ 30 d of settled funding records of the current instance known by `t`, with no gap in the last 7 d."; S-2.6: "For U3 and the §9 features, its r₈ is treated as missing."
3. **Code now:** invalid records dropped; pass iff first valid record ≤ t − 30 d and every adjacent valid pair whose later record is in the last 7 d passes `u3_gap`; no tail-gap test (luna/v7/universe.py:297-318).
4. **Why ambiguous:** "30 d of records" (span vs count), whether dropping an invalid record can create a gap, and the tail are unstated.
5. **Affects:** R / T.
6. **Recommendation:** keep. Reason: Stage-0-consistent; freshness of the tail is U7's job.
7. **Alternatives:** record-count rule; tail gap to t.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-13 — AMB-07 U4 / U8 window edges
1. **ID / title:** DK-13.
2. **Spec:** S-2.3 U4 "≥ 95 % of last-price 15-minute bars present in the last 24 h"; U8 "at least one 15-minute last-price bar with volume > 0 whose close lies in [`t` − 60 min, `t` − 1 min]".
3. **Code now:** U4 = the 96 bars opening in [L − 24 h, L), L = last 15m close ≤ t − 1 min, present = finite close > 0, pass ≥ 92 (luna/v7/universe.py:183-191); U8 inclusive ends on `vol_base` > 0 (universe.py:194-200).
4. **Why ambiguous:** "last 24 h" edges unstated.
5. **Affects:** R / T.
6. **Recommendation:** keep. Reason: aligned to the bar grid and PIT.
7. **Alternatives:** window ending at t.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-14 — AMB-07 U7: which record and undetermined nominal
1. **ID / title:** DK-14.
2. **Spec:** S-2.3 U7: "the last settled funding record ≤ 1 nominal interval + 15 min old at `t`".
3. **Code now:** last VALID current-instance record known by t − 1 min, its own nominal; undetermined nominal fails (luna/v7/universe.py:309-310).
4. **Why ambiguous:** "last settled record" may include invalid records.
5. **Affects:** R / T (eligibility and the ineligibility exit, DK-62).
6. **Recommendation:** keep. Reason: invalid = missing (S-2.6).
7. **Alternatives:** last record of any validity.
8. **Classification:** IMPLEMENTATION CLARIFICATION. Re-evaluated (red-team misclassified list): outcome-changing, but pinned by S-2.6 ("its r₈ is treated as missing"; an invalid record is not a valid settled record).
9. **Blocker:** yes (confirmation).

### DK-15 — AMB-07: missing / non-positive multiplier excludes
1. **ID / title:** DK-15.
2. **Spec:** S-2.1: "Multiplier symbols (for example `1000PEPEUSDT`) are included with quantity scaling." "Unknown identity is excluded (DEC-30, DEC-31)."
3. **Code now:** missing, non-finite or ≤ 0 multiplier -> excluded (luna/v7/universe.py:105).
4. **Why ambiguous:** multiplier is an identity field, but its absence is not explicitly an exclusion cause.
5. **Affects:** R / T.
6. **Recommendation:** keep. Reason: unknown identity field -> exclude (DEC-31).
7. **Alternatives:** default multiplier 1.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-16 — AMB-09 / R12: side adjustment of directional price features
1. **ID / title:** DK-16.
2. **Spec:** §9: "**Side adjustment.** Carry-direction features are multiplied by −s, so that positive means favourable to the candidate's side. A side indicator is included."
3. **Code now:** `CARRY_DIRECTION` = F1, F3, F5, F12, F11 cross-sectional median FL1 and share FL1 > 0 (luna/v7/features.py:81-84; applied at :407). F10 momentum and the F11 BTC return are not side-adjusted.
4. **Why ambiguous:** "carry-direction" literally limits the flip; "positive means favourable to the candidate's side" suggests every signed directional feature. With one model for both sides, a linear model cannot use un-adjusted price direction (the side indicator only shifts the intercept); LightGBM can, tilting A2 vs A3.
5. **Affects:** R, T, P, G (model inputs -> selection of ML\* and every outcome).
6. **Recommendation:** keep the literal reading (carry-direction only). Reason: the frozen text names only carry-direction features; extending it is a methodology choice.
7. **Alternatives:** (B) also side-adjust F10 returns, F10 residual and the F11 BTC return (× s so positive = favourable).
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (user decision).
9. **Blocker:** yes.

### DK-17 — AMB-08 F1-F4: windows and pairs across an invalid record
1. **ID / title:** DK-17.
2. **Spec:** §9 F1 "mean r₈ over the last 24 h, 72 h, 7 d, 30 d of settled records"; F2 "lag-1 autocorrelation of r₈ over 30 d"; F4 "sign changes of r₈ over 30 d ÷ number of settlements"; S-2.6 "its r₈ is treated as missing".
3. **Code now:** windows (t − W, t]; invalid records dropped; F2 lag-1 pairs and F4 reversal pairs BRIDGE a dropped invalid record (luna/v7/features.py:202-235, pairs at :220-224 and :232). The TS AR(1) reference instead breaks pairs at an invalid record (arms.py:186-216, AMB-14).
4. **Why ambiguous / divergence:** "treated as missing" does not say whether a missing value breaks a lag pair; the code is inconsistent between F2/F4 and the AR(1) reference.
5. **Affects:** R.
6. **Recommendation:** break lag-1 and reversal pairs at an invalid record (as the AR(1) reference does); keep the windows. Reason: a missing observation never forms a lag pair; one consistent rule.
7. **Alternatives:** keep bridging (current).
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes.

### DK-18 — AMB-08 F5: premium fallback granularity and windows
1. **ID / title:** DK-18.
2. **Spec:** §9 F5 "mean premium index over 8 h and 24 h (R4; fallback (mark − index)/index); last premium close".
3. **Code now:** fallback per bar where the premium close is missing; windows by bar close in (t − 8 h, t − 1 min] / 24 h (luna/v7/features.py:237-257).
4. **Why ambiguous:** fallback per bar vs per window unstated.
5. **Affects:** R.
6. **Recommendation:** keep per-bar fallback. Reason: uses all available data without mixing windows.
7. **Alternatives:** per-window fallback only when the whole window lacks premium.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-19 — AMB-08 F6: 7 d median sampling
1. **ID / title:** DK-19.
2. **Spec:** §9 F6 "ĥ(t) (S-4.5 proxy); its 7 d median".
3. **Code now:** median of ĥ(τ) at τ = t − k·8 h, k = 0..20 (luna/v7/features.py:55, :280-283).
4. **Why ambiguous:** sampling grid unstated.
5. **Affects:** R.
6. **Recommendation:** keep. Reason: the decision clock is the natural PIT grid.
7. **Alternatives:** every 15 min; daily.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-20 — AMB-08 F8: Amihud and trades per day
1. **ID / title:** DK-20.
2. **Spec:** §9 F8 "log median daily quote volume over 30 d; Amihud illiquidity over 7 d; log trades per day over 7 d".
3. **Code now:** Amihud = mean over 7 full UTC days of |C(day end)/C(prev day end) − 1| / daily quote volume (days with missing close / zero volume skipped); trades = log(Σ n_trades / 7), divisor always 7 (luna/v7/features.py:284-298).
4. **Why ambiguous:** Amihud granularity and young-instance handling unstated.
5. **Affects:** R.
6. **Recommendation:** keep. Reason: the classic daily Amihud definition; divisor 7 matches the U2 empty-day = 0 rule.
7. **Alternatives:** hourly Amihud; divisor = observed days.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-21 — AMB-08 F9 / F10: realized volatility and returns
1. **ID / title:** DK-21.
2. **Spec:** §9 F9 "realized vol of hourly log returns over 24 h and 7 d (annualized)"; F10 "returns over 24 h, 7 d, 30 d; residual return vs BTC over 7 d using β̂".
3. **Code now:** RV = sqrt(mean(r²) · 24 · 365), no demeaning (luna/v7/features.py:54, :187-197); F10 simple returns between full-hour closes (features.py:180-184); residual = r_a − β̂ r_BTC (features.py:386-390).
4. **Why ambiguous:** demeaning, day count and return type unstated.
5. **Affects:** R.
6. **Recommendation:** keep. Reason: standard RV, 365-day crypto calendar consistent with F13's 3·365.
7. **Alternatives:** sample std (demeaned); log returns for F10.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-22 — AMB-08 F11 / F12: cross-section and ranks
1. **ID / title:** DK-22.
2. **Spec:** §9 F11 "cross-sectional median FL1; share of eligible symbols with FL1 > 0"; F12 "percentile ranks of FL1, FL7, FL30 and F5 within the eligible universe".
3. **Code now:** eligible = U1-U8 at t; F12 = average-rank percentile (less + (eq+1)/2)/n with the candidate's own value included, for FL1, FL7, FL30 and each of the three F5 values (six columns) (luna/v7/features.py:317-344); ranks are then × −s (carry-direction).
4. **Why ambiguous:** "F5" has three values; rank definition and whether a non-eligible candidate is ranked are unstated.
5. **Affects:** R.
6. **Recommendation:** keep. Reason: covers every F5 value without choosing one; own value included keeps the rank defined.
7. **Alternatives:** rank only the 8 h premium mean; exclude own value.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-23 — AMB-08 F13: caps and 0/0
1. **ID / title:** DK-23.
2. **Spec:** §9 F13 "7 d realized vol ÷ (|FL7| · 3 · 365); 30 d std of r₈ ÷ |FL30| (both capped at 10⁴)".
3. **Code now:** min(ratio, 1e4); denominator 0 with numerator > 0 -> 1e4; 0/0 or missing -> NaN (luna/v7/features.py:151-157, :391-394).
4. **Why ambiguous:** division by zero handling unstated.
5. **Affects:** R.
6. **Recommendation:** keep. Reason: the cap covers the infinite case; 0/0 is undefined.
7. **Alternatives:** 0/0 = 0 or cap.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-24 — AMB-08 F14: fee and per-horizon column
1. **ID / title:** DK-24.
2. **Spec:** §9 F14 "2·f + 2·ĥ(t) + 2·slip (bps); (|FL7| · 3 · h/24) ÷ that cost"; S-5.3 "Labels use **taker** costs only."
3. **Code now:** f = taker 0.0005; one ratio column per horizon, only its own column used in the horizon's model (luna/v7/features.py:87-92, :397-400). (The bps-vs-fraction unit of the ratio is a pure rescale and does not change any model; excluded.)
4. **Why ambiguous:** f is not named taker or maker.
5. **Affects:** R (an additive constant in the denominator changes the ratio non-linearly).
6. **Recommendation:** keep taker. Reason: labels and A0 cost use taker.
7. **Alternatives:** maker fee.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-25 — AMB-08: linear preprocessing order
1. **ID / title:** DK-25.
2. **Spec:** §9 "Linear models: training-fold winsorization at 1 / 99 %, standardization, median imputation plus a missing-indicator column."; L5 "fitted on `T_fit`".
3. **Code now:** per column on finite fit values: linear quantiles 1/99 %, clip, mean / population std, constant -> 0; missing (NaN, ±inf) imputed with the median of the standardized fit values; one indicator column per feature (luna/v7/features.py:456-498).
4. **Why ambiguous:** order and quantile method unstated.
5. **Affects:** R.
6. **Recommendation:** keep. Reason: the listed order, all fitted on T_fit only.
7. **Alternatives:** impute before winsorizing; indicator only for columns with missing fit values.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-107 — AMB-08 feature formulas ratified as a whole
1. **ID / title:** DK-107.
2. **Spec:** §9 feature table (F1-F14) and "The feature set is fixed."
3. **Code now:** luna/v7/features.py (AMBIGUITIES AMB-08 lists every formula choice: windows, F5 per-bar fallback, F6 8 h sampling, F8 Amihud / divisor 7, F9 uncentred 365-day RV, F10 simple returns, F12 average-rank incl. own value, F13 caps, F14 taker fee, preprocessing order).
4. **Why ambiguous:** the §9 table gives names and short definitions, not formulas. Red-team: each choice changes model inputs and therefore rankings.
5. **Affects:** R (and through it T, P, G).
6. **Recommendation:** the user ratifies the AMB-08 table as a whole (DK-17 to DK-25, with the DK-17 change to break lag pairs at invalid records). Reason: one pre-registration act for formulas the spec leaves open; no item uses outcome data.
7. **Alternatives:** item-by-item decisions (DK-17 to DK-25).
8. **Classification:** IMPLEMENTATION CLARIFICATION (ratification line).
9. **Blocker:** yes.

### DK-26 — K7 feature-dominance audit: definition and missing automated check
1. **ID / title:** DK-26.
2. **Spec:** §16 K7: "One feature carries > 50 % of total LightGBM gain, or > 50 % of the absolute standardized EN weight | mandatory leakage audit before the holdout; if unexplained → K1".
3. **Code now:** listed in `verdict.K_TABLE` (luna/v7/verdict.py:88) and `SPEC_COVERAGE` as "review item, no automated check" (luna/v7/__init__.py:184-185); nothing computes gain or weight shares.
4. **Why ambiguous / gap:** which models (each fold model, final model, each horizon, classifiers), whether a feature's missing-indicator column is merged with it, and the denominator (all columns incl. indicators) are unstated; and no code evaluates it.
5. **Affects:** G (a trigger forces an audit and possibly K1 restart before the holdout).
6. **Recommendation:** implement an automated report: evaluated for every fold model and the final model of the selected family at h\* (and reported for all), a feature's share = its column + its `__missing` indicator over the sum of all columns. Reason: covers every model that produces evidence; merging indicators prevents splitting one feature into two.
7. **Alternatives:** final model only; indicators as separate features.
8. **Classification:** IMPLEMENTATION CLARIFICATION (plus a missing check to implement).
9. **Blocker:** yes.

### DK-27 — L9 canaries: pass criteria and scope
1. **ID / title:** DK-27.
2. **Spec:** L9: "(a) labels permuted within decision dates → out-of-fold IC within ±2 SE of 0; (b) a feature equal to the label shifted 1 decision earlier is rejected by L1, and in a sandbox without L1 the out-of-fold IC jumps".
3. **Code now:** machinery for (a) only (`cv.date_ic_se`, luna/v7/cv.py:122-125; SPEC_COVERAGE luna/v7/__init__.py:158); no canary driver; (b) has no numeric criterion.
4. **Why ambiguous:** which family / horizons; SE definition; "jumps" has no threshold.
5. **Affects:** G (a failed canary is a leakage finding -> K1 / NO-GO: leakage).
6. **Recommendation:** (a) run for both regressor families at all three horizons, SE = std of date ICs / sqrt(dates) (as coded), fail if any |IC| > 2 SE; (b) pass iff L1 refuses the shifted-label feature; the sandbox IC is reported as a demonstration (no threshold). Reason: no invented threshold.
7. **Alternatives:** (a) for ML\* only; numeric jump threshold for (b).
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes.

## C. Labels

### DK-28 — AMB-10(b) / R13: BTC halted at the label exit
1. **ID / title:** DK-28.
2. **Spec:** S-5.4: "For a label, `t_x` in S-5.3 and S-5.6 becomes that fill time and P(`t_x`) that bar's close; the pro-forma BTC hedge exits at the same time."; S-5.5: "A label is invalid if a funding record in (`t_e`, `t_x`] is missing or invalid (S-2.6), a required reference price is missing, its instrument is halted at `t_e`, it reaches a B / C terminal event (S-5.4), or its entry proxy is missing (S-4.4)."; S-5.4 "A label whose instrument, or whose BTC reference B(τ), is halted at `t_e` is invalid".
3. **Code now:** BTC halted at t_x_eff -> invalid `btc_exit_halted` (luna/v7/labels.py:264-265).
4. **Why ambiguous:** S-5.5 lists BTC halts only at t_e; the hedge is pro-forma and B(t_x) is computable even while BTC is halted (the 1m bar open, or the gap rule).
5. **Affects:** L, R (training set).
6. **Recommendation:** keep invalid until the user rules. Reason: conservative; expected count near zero (BTC halts are rare).
7. **Alternatives:** price B(t_x_eff) by S-5.2 + gap rule regardless of the BTC halt (literal S-5.5 list).
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-29 — AMB-10(g) / R13: beta missing makes the label invalid
1. **ID / title:** DK-29.
2. **Spec:** S-5.2 "requiring ≥ 500 joint observations"; S-5.3 "`q_B` = β̂_a(t) / B(t_e)"; S-5.5 list (quoted in DK-28) has no "beta missing" cause.
3. **Code now:** invalid `beta_missing` (luna/v7/labels.py:207-209).
4. **Why ambiguous:** no fallback is given, and no invalid cause.
5. **Affects:** L, R.
6. **Recommendation:** keep invalid. Reason: no value can be computed without inventing one; U1 (60 d) makes it rare for eligible candidates.
7. **Alternatives:** β̂ = prior 1.0 (a new rule); drop the hedge term.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-30 — AMB-10(c)/(i): reaching cessation uses the retrospective instance end
1. **ID / title:** DK-30.
2. **Spec:** S-5.4 "Every terminal event (an instance end reached by an open position or label, S-2.2) is classified"; S-2.2 "No decision before `instance_end_known_at` may use the knowledge that the instance has permanently ended."
3. **Code now:** a label reaches cessation iff `last_trade_ts` ≤ scheduled t_x, or the leg is halted at t_x and no executable bar exists before cessation (luna/v7/labels.py:212-229); the book schedules a terminal check at `last_trade_ts` (luna/v7/book/engine.py:346-353).
4. **Why ambiguous:** labels / books are outcomes (may use retrospective ends), but within ~7 d of the window end the permanence is known only after the window end (S-1.5).
5. **Affects:** L, V (B / C -> invalid label / INVALID RUN).
6. **Recommendation:** keep. Reason: the price data used are all before the window end; the decision-time rule (S-2.2) is not violated because no decision uses it.
7. **Alternatives:** treat a cessation whose end is known only after the window end as a halted position (-> no executable exit -> INVALID).
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-31 — AMB-10(d): window end for halted waits
1. **ID / title:** DK-31.
2. **Spec:** S-5.4 "If the instance hasn't reached cessation and no executable bar of the same instance appears before the window's terminal unwind (S-1.5, S-8.1), the label is invalid"; S-1.5 "strictly before that window's end".
3. **Code now:** the waiting fill (bar close) must be strictly before the end (luna/v7/labels.py:215-216; execution.py:273); t_x ≥ end -> no label (labels.py:166-168).
4. **Why ambiguous:** "before the terminal unwind" vs "before the end" differ by one minute.
5. **Affects:** L, V.
6. **Recommendation:** keep. Reason: a bar closing at the end is not data "strictly before" the end.
7. **Alternatives:** fill must close at or before end − 1 min (unwind minute).
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-32 — AMB-10(f): label funding window and the missing-settlement rule
1. **ID / title:** DK-32.
2. **Spec:** S-5.3 "Σ over settlements τ_k ∈ (`t_e`, `t_x`]"; S-5.5 "A label is invalid if a funding record in (`t_e`, `t_x`] is missing or invalid"; S-4.6 mark chain.
3. **Code now:** DEC-29 gap test from the head record (last record ≤ t_e) through the window plus a tail test to t_x_eff; no head record -> missing; any invalid record or non-finite rate -> invalid; mark chain record -> mark 15m -> last 15m, else `mark_missing` (luna/v7/refs.py:133-155).
4. **Why ambiguous:** "missing" needs a definition (no schedule is given).
5. **Affects:** L.
6. **Recommendation:** keep. Reason: reuses the frozen DEC-29 adjacency test; Stage-0 `funding_ok` semantics.
7. **Alternatives:** expected-schedule count by nominal interval.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-33 — AMB-10(h): halted exit without a fill-time proxy; no current instance
1. **ID / title:** DK-33.
2. **Spec:** S-5.4 "half-spread max(`h_fill`, 25 bps) and slippage 3 × slip"; S-4.4 missing proxy at an exit -> "half-spread 25 bps and slippage 3 × slip".
3. **Code now:** missing h_fill -> 25 bps + 3 × slip, flagged (luna/v7/labels.py:219-223); no current instance at t -> invalid `no_instance` (labels.py:184-185).
4. **Why ambiguous:** max with a missing value; `no_instance` is not an S-5.5 cause (but no gated candidate lacks an instance).
5. **Affects:** L.
6. **Recommendation:** keep. Reason: S-4.4 fallback equals max(missing, 25 bps); `no_instance` is unreachable for gated candidates.
7. **Alternatives:** none material.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

## D. Models / CV / selection

### DK-34 — AMB-11: alpha -> C mapping and solver settings
1. **ID / title:** DK-34.
2. **Spec:** S-10.5 "Logistic EN | y_pos | **fixed:** α = 1e-2, l1_ratio = 0.5"; "Seeds are fixed."
3. **Code now:** `LogisticRegression(solver="saga", l1_ratio, C = 1/(α·n))`; `ElasticNet(alpha, l1_ratio)`; max_iter 1000, tol 1e-4; LightGBM deterministic, single thread, seeds = 20261014, early stopping 100 (luna/v7/models.py:117-128, :159-166, :194).
4. **Why ambiguous:** sklearn's logistic uses C, not α; the scaling convention is unstated.
5. **Affects:** R (logistic EN -> A1 veto if ML\* = A2; P8-classifier), S.
6. **Recommendation:** keep. Reason: C = 1/(α n) makes the objective mean-loss + α·penalty, the same convention as ElasticNet.
7. **Alternatives:** C = 1/α (penalty relative to summed loss).
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (secondary only).

### DK-35 — AMB-12(a): "decision date" = UTC calendar day
1. **ID / title:** DK-35.
2. **Spec:** S-10.5 "the mean over decision dates of the Spearman correlation"; S-10.4 "every decision date weighted equally"; P7 / P8.
3. **Code now:** date = UTC day of `decision_ts`; the three decisions of a day pooled; dates with < 2 distinct prediction or label values skipped (luna/v7/cv.py:51-53, :100-119).
4. **Why ambiguous:** "decision date" could mean each decision time.
5. **Affects:** R (selection), G (P7, P8).
6. **Recommendation:** keep. Reason: consistent with the day-indexed bootstrap (S-13.1, L13).
7. **Alternatives:** per decision time.
8. **Classification:** IMPLEMENTATION CLARIFICATION. Re-evaluated (red-team misclassified list): it changes the IC and calibration weights. The word "date" (not "decision time"), the UTC-day bootstrap of S-13.1 and "date-level aggregation" in L13 all point to the UTC day.
9. **Blocker:** yes (confirmation).

### DK-36 — AMB-12(a2) / R16: side pooling in the date-level IC and calibration
1. **ID / title:** DK-36.
2. **Spec:** S-10.5 selection metric (quoted DK-35); S-10.4 step 3 "(a, b) by OLS of realized `y_reg` on ŷ over `T_cal`, every decision date weighted equally"; S-7.1 "The long side is filled from long candidates, the short side from short candidates."
3. **Code now:** one Spearman IC per date over both sides' candidates; one OLS / isotonic fit (luna/v7/cv.py:100-119, :151-161, :284-286); the same pooled IC feeds P7 (verdict.py:257-266).
4. **Why ambiguous:** the book ranks within side, so the economically relevant IC is per side; the spec does not say.
5. **Affects:** R (hyperparameters), T, P, G (P7, P8).
6. **Recommendation:** keep the pooled cross-section. Reason: literal "mean over decision dates" over all candidates.
7. **Alternatives:** IC per (date, side) averaged; per-side calibration.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-37 — AMB-12(b) / R16: intercept when the slope clips to 0
1. **ID / title:** DK-37.
2. **Spec:** S-10.4 step 3: "b is clipped to ≥ 0. If b = 0: entries iff a > 0, ranking by ŷ."
3. **Code now:** with b = 0 (or constant ŷ), a = date-weighted mean of y_reg (OLS with b fixed at 0) (luna/v7/cv.py:158-160).
4. **Why ambiguous:** a may be the unconstrained OLS intercept or the re-estimated one; it decides whether the arm trades at all.
5. **Affects:** T, P, G.
6. **Recommendation:** re-estimate a with b fixed at 0 (current). Reason: with b = 0 the map is the constant a; the least-squares constant is the weighted mean, the only self-consistent value.
7. **Alternatives:** keep the unconstrained intercept a.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-38 — AMB-12(c): preprocessing during hyperparameter selection
1. **ID / title:** DK-38.
2. **Spec:** S-10.4 `T_tr` "fit candidate models for hyperparameter selection"; L5 "Winsorization, scaling and imputation fitted on `T_fit`".
3. **Code now:** candidate fits use a Preprocessor fitted on T_tr rows only; the refit uses one fitted on T_fit (luna/v7/cv.py:311, :338-339).
4. **Why ambiguous:** L5 names T_fit; selection-phase fits on T_tr would otherwise see T_sel statistics.
5. **Affects:** R.
6. **Recommendation:** keep. Reason: T_tr ⊂ T_fit (L5 holds) and the selection never sees T_sel features in its preprocessing.
7. **Alternatives:** T_fit-fitted preprocessing for candidate fits.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-39 — T_fit excludes T_tr rows purged at the T_sel start (deferred minor, Tasks 11-12)
1. **ID / title:** DK-39.
2. **Spec:** S-10.3 "A sample is admissible for any later block or fold starting at T only if `label_known_at` ≤ T − 24 h."; S-10.4 "The S-10.3 purge applies at each block boundary." "refit on `T_fit` = `T_tr` ∪ `T_sel`".
3. **Code now:** T_fit = purged T_tr (admissible for the T_sel start) ∪ purged T_sel (luna/v7/cv.py:88-92, :337); T_tr rows whose labels resolve within 24 h of the T_sel start are dropped from the refit too.
4. **Why ambiguous:** for the refit, the next block is T_cal, so the internal T_tr / T_sel boundary need not purge T_fit. Red-team M7 calls this an unlogged divergence. It costs about (h + 24 h) of decisions before C − 12 mo, roughly 2–4 % of T_fit at h\* = 168 h.
5. **Affects:** R (and through it T, P, IC, LB).
6. **Recommendation:** keep, and log it in AMBIGUITIES.md. Reason: S-10.4 step 6 defines the blocks as "`T_tr` = [D0, 2024-07-01), `T_sel` = [2024-07-01, 2025-01-01), `T_cal` = [2025-01-01, 2025-07-01), each with the purge", and step 1 refits on "`T_fit` = `T_tr` ∪ `T_sel`". The union of the purged blocks is therefore the literal reading, and it is also the conservative one.
7. **Alternatives:** T_fit = all rows in [D0, C − 6 mo) admissible for the T_cal start (the red-team's reading).
8. **Classification:** IMPLEMENTATION CLARIFICATION. Counter-argument to M7: step 6 "each with the purge" pins it. It is not a code bug.
9. **Blocker:** yes (confirmation).

### DK-40 — AMB-12(d)/(e): selection ties, families, classifier stopping
1. **ID / title:** DK-40.
2. **Spec:** S-10.5 search table; "Classifiers aren't selected."; S-7.2 A2 = EN, A3 = LightGBM.
3. **Code now:** EN and LightGBM regressors selected separately; IC ties -> first in search-space order; undefined IC ranks last; LightGBM classifier tree count early-stopped on T_sel logloss (luna/v7/cv.py:289-326, :352-359).
4. **Why ambiguous:** tie-break and classifier early stopping unstated.
5. **Affects:** R.
6. **Recommendation:** keep. Reason: deterministic; S-10.5 fixed settings include early stopping for both tasks.
7. **Alternatives:** tie -> simpler config.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-41 — AMB-13(a)/(b): FL30 units in A0; ML rank key
1. **ID / title:** DK-41.
2. **Spec:** S-7.2 A0 "`c` is the frozen V5 `carry_per_hour` on FL30"; A2 "Ranking: ŷ(h\*) from Elastic Net. Hurdle: calibrated ŷ_cal(h\*) > 0 bps".
3. **Code now:** c = carry_per_hour(side, FL30/8) (luna/v7/arms.py:72-83); rank by raw ŷ, hurdle ŷ_cal > 0 (arms.py:100-104).
4. **Why ambiguous:** FL30 is per 8 h; V5 expects per hour.
5. **Affects:** T, P (A0 score).
6. **Recommendation:** keep. Reason: unit consistency; raw ŷ ranking is literal.
7. **Alternatives:** none sensible.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-42 — AMB-13(c): A1 veto applied at exits as well as entries
1. **ID / title:** DK-42.
2. **Spec:** S-7.2 A1: "A0's entries, minus those with calibrated P̂(net(h\*) > 0) < **0.50** (DEC-8). ... Ranking: A0 order"; S-7.1 "After the minimum hold, a leg exits at a decision if it ... its score falls below the hurdle."
3. **Code now:** A1 `passes_hurdle` = A0 pass AND P̂ ≥ 0.50 (luna/v7/arms.py:145-160); the constructor exits a held leg after h\* when `passes_hurdle` is False (luna/v7/book/constructor.py:203-204), so a held A1 leg exits when it is vetoed; NaN P̂ vetoes.
4. **Why it may diverge:** the spec defines the veto on entries only; the A1 exit hurdle can be read as A0's hurdle.
5. **Affects:** T (A1 only), S (A1 vs A0 comparison).
6. **Recommendation:** apply the veto to entries only; A1's exit hurdle is A0's `s_rule > 0`. Keep a separate veto flag; arms.py:154-159 and constructor.py:203-204 need a code fix. Reason: literal "A0's entries, minus ..."; S-0.6 lets ML remove candidates, not force exits. Confirmed by red-team M8.
7. **Alternatives:** keep the veto at exits (current; would need an amendment).
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED (the literal reading is clear: the veto is defined on entries).
9. **Blocker:** yes (secondary only).

### DK-43 — AMB-26: solver non-convergence
1. **ID / title:** DK-43.
2. **Spec:** S-10.5 frozen α / l1_ratio, "Seeds are fixed"; §17 "every run recorded in DT4".
3. **Code now:** ConvergenceWarnings captured per fit, recorded in DT4 with a count; the solver output at max_iter is used (luna/v7/models.py:142-166; runner.fold_convergence).
4. **Why ambiguous:** the spec is silent on non-convergence.
5. **Affects:** R (coefficients at max_iter).
6. **Recommendation:** keep (record, report, never void). Reason: changing max_iter / tol / labels would alter frozen settings or AMB-11.
7. **Alternatives:** void the run; raise max_iter.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-44 — AMB-14 / AMB-15 / AMB-27(a): reference arms are not executed books
1. **ID / title:** DK-44.
2. **Spec:** S-7.2 "A0-V5 reference | The frozen V5 `strategy.py` function, unchanged, with its frozen config ... on the V7 Binance universe | Reference only (no test)"; "TS references | A0's rule with the FL30 forecast replaced by EWMA ... or by an AR(1) ... projected to h\* | Reference only"; S-10.6 "Reference arms (A0-V5, TS) that are invalid are reported as invalid and have no test."
3. **Code now:** only signals / a decision adapter exist (luna/v7/arms.py:175-216, :218-268); `dev_selection` runs no reference book (runner_select.py:77-112); AR(1) forecast = mean of the projected path, φ unclamped.
4. **Why ambiguous:** "A0's rule" with a TS forecast implies the same constructor book; the V5 adapter does not reproduce V5's own risk layer.
5. **Affects:** S (reported references only; no test).
6. **Recommendation:** report TS references as books through the V7 constructor (A0 scoring with the TS forecast) when run; report A0-V5 as a decision-rule reference with its stated limitations. Reason: no test depends on them.
7. **Alternatives:** omit references.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** no (reference only, never affects a verdict).

### DK-45 — AMB-22(a)/(b): bootstrap seed and quantile
1. **ID / title:** DK-45.
2. **Spec:** S-13.1 "B = 10,000; seed fixed and recorded"; "LB = θ̂ − q₀.₉₅(θ̂\* − θ̂)".
3. **Code now:** seed 20261014 for every test and ℓ; quantile `inverted_cdf` (luna/v7/stats.py:32, :92-120).
4. **Why ambiguous:** quantile method unstated.
5. **Affects:** R (S-10.6 selection by LB), G (P7).
6. **Recommendation:** keep. Reason: an order statistic, no interpolation; frozen seed.
7. **Alternatives:** numpy linear interpolation.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-46 — AMB-22(f): S-10.6 simplicity rule and tie-break reference point
1. **ID / title:** DK-46.
2. **Spec:** S-10.6 "3. Select the highest LB. 4. **Simplicity rule:** A3 is chosen only if its LB exceeds the best A2 LB by ≥ 1 bp/day. 5. **Tie-break** (|ΔLB| ≤ 0.1 bp/day): the longer horizon."
3. **Code now:** family A3 iff max LB(A3) − max LB(A2) ≥ 1 bp/day; within the family every horizon with LB ≥ max − 0.1 bp/day is tied; longest wins (luna/v7/selection.py:69-80).
4. **Why ambiguous:** ties measured against the maximum vs pairwise; whether the tie-break may cross families.
5. **Affects:** R (ML\*, h\*), G.
6. **Recommendation:** keep. Reason: deterministic, non-chained, family first as written.
7. **Alternatives:** chained ties; cross-family tie set.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

## E. Book / risk / execution

### DK-47 — AMB-16(a): no backfill of a blocked top-K slot
1. **ID / title:** DK-47.
2. **Spec:** S-7.1 entry: "it is in the top K of its side by the arm's score; the score is above the arm's hurdle (S-7.2); no cooldown; the asset isn't held on the other side"; S-7.2 A1 "A0's entries, minus those ...".
3. **Code now:** top K taken over all ranked gate-passing candidates (incl. hurdle-failing / vetoed); a slot lost to hurdle, veto, flip, cooldown, halt, missing beta or missing qv24 is not backfilled (luna/v7/book/constructor.py:222-245). Gate-failing candidates are removed before ranking.
4. **Why ambiguous:** "top K" may be over eligible-and-passing candidates.
5. **Affects:** T, P (mainly A1).
6. **Recommendation:** keep. Reason: literal "A0's entries minus vetoed"; ML cannot add a candidate (S-0.6).
7. **Alternatives:** backfill by rank K+1.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-48 — AMB-16(b): symbol qualifying on both sides at one decision
1. **ID / title:** DK-48.
2. **Spec:** S-7.1 "the asset isn't held on the other side (no same-decision flip)".
3. **Code now:** a symbol qualifying on both sides enters neither (luna/v7/book/constructor.py:246-251).
4. **Why ambiguous:** the rule covers held assets only.
5. **Affects:** T.
6. **Recommendation:** keep. Reason: never long and short the same asset.
7. **Alternatives:** keep the better-ranked side.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-49 — AMB-16(c): kept legs occupy slots
1. **ID / title:** DK-49.
2. **Spec:** S-7.1 "at most **K = 5** legs per side"; exit only by gate / rank > K+2 / hurdle after h\*.
3. **Code now:** held legs not exiting keep their slots; entrants fill free slots in rank order; no push-out (luna/v7/book/constructor.py:209-254).
4. **Why ambiguous:** whether a better candidate displaces a kept leg ranked K+1..K+2.
5. **Affects:** T.
6. **Recommendation:** keep. Reason: no exit rule says so.
7. **Alternatives:** displace.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-50 — Partial side sizing and exact-neutrality scaling (AMB-16(d), unresolved part)
1. **ID / title:** DK-50.
2. **Spec:** S-7.1 "Legs are equal-weighted within a side; each side's gross is 0.5 · E. The book is dollar-neutral: |Σ long − Σ short| ≤ 0.05 · gross."; S-7.3 "**Per leg** ≤ (0.5 · E) / K."
3. **Code now:** with n < K legs, target = min(0.5E/n, 0.5E/K) (side gross n·0.1E); if |L − S| > 0.05·gross, every target of the heavier side is scaled by light/heavy (exact neutrality) (luna/v7/book/constructor.py:264-283).
4. **Why ambiguous:** "side gross 0.5 E" conflicts with the per-leg cap when n < K; the neutrality fix (scale to exact vs to the tolerance edge) is unstated. (The empty-side case was resolved by the user.)
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: the risk-layer cap is a hard limit; scaling down to exact neutrality never adds exposure.
7. **Alternatives:** scale the heavy side only to the 5 % band edge.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-51 — R35: implementation reading of the user's AMB-16(e) decision (for confirmation)
1. **ID / title:** DK-51.
2. **Spec:** S-7.1 "**Resizes:** to the equal-weight target only when a leg's notional drifts > 25 % from its target. Reduce-only between entries."; S-7.3 "**Gross** ≤ 1.0 · E. **Per leg** ≤ (0.5 · E) / K. The S-4.4 liquidity cap." User decision 2026-10-15 (round 1): "Constructor / selection caps apply to target exposures and weights. Current marked exposures remain subject to the frozen hard risk limits. ... Target caps are not redefined as continuously binding marked-book caps in a way that makes the drift rule unreachable."
3. **Code now:** per-leg and liquidity caps bind targets / entries / increases; the 25 % drift rule is the marked-book mechanism of a leg; gross ≤ 1.0 E is a hard marked-book limit at every decision, enforced by proportional reduce-only cuts of kept non-halted legs (`risk_gross`) (luna/v7/book/constructor.py:289-337).
4. **Why it needs confirmation:** with equal weights the per-leg target equals the per-leg cap, so a continuous marked per-leg cap would make the drift rule unreachable (forbidden by the decision); R35 therefore treats only gross as the marked hard limit. A per-leg marked exposure can exceed 0.5 E/K by < 25 %.
5. **Affects:** T, P.
6. **Recommendation:** confirm R35. Reason: the only reading satisfying both "hard limits on the marked book" and "drift rule reachable".
7. **Alternatives:** also a marked per-leg hard cap at 1.25 × 0.5 E/K (would coincide with the drift trigger).
8. **Classification:** IMPLEMENTATION CLARIFICATION (reading of a user decision).
9. **Blocker:** yes (until confirmed).

### DK-52 — AMB-16(e) detail: `risk_gross` reduce under E_M
1. **ID / title:** DK-52.
2. **Spec:** S-6.2 "`E_M` ... Normal entries, exits and resizes only. Risk exits, forced exits and hedge trades are always taker."
3. **Code now:** a `risk_gross` reduce is a risk order (forced), taker at t + 15 min under E_T and E_M (luna/v7/book/constructor.py:344; engine.py:618).
4. **Why ambiguous:** it is both a resize and a risk-limit action.
5. **Affects:** S (E_M only; E_T unaffected).
6. **Recommendation:** keep taker. Reason: it enforces an S-7.3 risk limit.
7. **Alternatives:** maker-first resize.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (E_M only).
9. **Blocker:** yes (secondary only).

### DK-53 — AMB-16(e) details: evaluation point and neutrality after cuts
1. **ID / title:** DK-53.
2. **Spec:** S-7.3 gross limit; S-7.1 neutrality.
3. **Code now:** the gross limit is evaluated on the post-exit / post-drift book with halted and pending legs at their marked notional; cuts scale only non-halted kept legs, so the result can be non-neutral when halted legs sit on one side; `cap_gross_entry` scales all entrants by one factor, which can leave the neutrality band (luna/v7/book/constructor.py:318-337; deferred minor "cap_gross_entry can leave neutrality band").
4. **Why ambiguous:** the spec does not order the gross limit against neutrality.
5. **Affects:** T, P.
6. **Recommendation:** keep; report non-neutral post-decision books in the decision log. Reason: halted legs cannot trade (S-5.4); no extra exposure is ever added.
7. **Alternatives:** re-apply neutrality after the gross cut (cut the heavy side more).
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-109 — Red-team m6: the BTC hedge is outside the S-7.3 gross limit
1. **ID / title:** DK-109.
2. **Spec:** S-7.3 "**Gross** ≤ 1.0 · E."; S-7.1 "each side's gross is 0.5 · E"; S-7.4 "|H_B| ≤ 0.5 · E".
3. **Code now:** the gross limit sums leg notionals only (luna/v7/book/constructor.py:319-337); the hedge is capped separately (hedge.py:57-58). Leg gross 1.0 E plus a 0.5 E hedge gives 1.5 E total. Not logged as an AMB.
4. **Why ambiguous:** "Gross" is not qualified.
5. **Affects:** T, P.
6. **Recommendation:** keep (gross = leg gross), and log it. Reason: S-7.1 targets both sides at 0.5 E (1.0 E total). Including the hedge would breach the limit whenever a hedge exists and force leg cuts that S-7.1 / S-7.4 never describe; S-7.4 caps the hedge separately.
7. **Alternatives:** legs + |hedge| ≤ 1.0 E.
8. **Classification:** IMPLEMENTATION CLARIFICATION (pinned by S-7.1 / S-7.4 consistency).
9. **Blocker:** yes (confirmation).

### DK-54 — AMB-16(f): "reduce-only between entries"
1. **ID / title:** DK-54.
2. **Spec:** S-7.1 "Reduce-only between entries."
3. **Code now:** a leg is never increased after its entry (`no_increase`) (luna/v7/book/constructor.py:305-310).
4. **Why ambiguous:** could mean increases are allowed at decisions that have entries.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: conservative; the only exposure increase is an entry.
7. **Alternatives:** increases allowed at entry decisions.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED. Reclassified (red-team misclassified list): "between entries" reads equally well as "increases allowed at entry decisions", and no other frozen rule decides it.
9. **Blocker:** yes (confirmation).

### DK-55 — AMB-16(g): minimum-hold clock
1. **ID / title:** DK-55.
2. **Spec:** S-7.1 "Minimum hold = h\* (S-10.6)."
3. **Code now:** measured from the entry DECISION time (luna/v7/book/constructor.py:191).
4. **Why ambiguous:** decision time vs fill time.
5. **Affects:** T, P (one decision of extra hold if measured from the fill).
6. **Recommendation:** keep. Reason: matches the label horizon t_e + h with exit at decision t + h.
7. **Alternatives:** from the fill.
8. **Classification:** IMPLEMENTATION CLARIFICATION. Re-evaluated (red-team misclassified list): outcome-changing, but pinned by S-4.1 ("24 / 72 / 168 h are 3 / 9 / 21 decision steps") and S-5.3 (exit fill at t_e + h). Measuring from the fill would make h\* = 4 / 10 / 22 steps.
9. **Blocker:** yes (confirmation).

### DK-56 — AMB-16(h): decision exit "fails a gate"
1. **ID / title:** DK-56.
2. **Spec:** S-7.1 "a leg exits at a decision if it fails a gate"; S-7.3 "U8 false: ... it doesn't trigger an exit"; "Ineligibility exit: failing U4, U5 (exit threshold 20 bps half-spread) or U7 on two consecutive decisions".
3. **Code now:** any of U1-U7 fails (U5 at the 7.5 bps entry threshold) after h\* -> decision exit (luna/v7/book/constructor.py:121-125, :195-196).
4. **Why ambiguous:** whether the decision exit uses the 20 bps U5 exit threshold.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: literal gate; 20 bps is defined only for the risk-layer exit.
7. **Alternatives:** U5 at 20 bps for the decision exit.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-57 — AMB-16(i) / AMB-25(a): liquidity cap units and the qv24 window
1. **ID / title:** DK-57.
2. **Spec:** S-4.4 "**Liquidity cap:** a leg's notional ≤ 0.5 % of the symbol's trailing 24 h quote volume."; §14 L-norm "liquidity cap evaluated at E_ref = 10,000 USD".
3. **Code now:** cap = 0.005 · qv24 / usd_per_unit (E_ref for L-norm) (luna/v7/book/constructor.py:268-275); qv24 = Σ vol_quote of current-instance 15m bars closing in (t − 24 h − 1 min, t − 1 min] (luna/v7/runner.py:270-285); missing qv24: no entry; a held leg stays uncapped.
4. **Why ambiguous:** window edges; missing-input handling for held legs.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: PIT, instance-restricted, never invents volume.
7. **Alternatives:** UTC-day sum; reduce a held leg with missing qv24 to 0.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-58 — AMB-16(j) / R25: halted flags evaluated at t
1. **ID / title:** DK-58.
2. **Spec:** S-5.4 HALTED definition; S-7.1 "No order of any kind while the instrument is halted (S-5.4)."
3. **Code now:** constructor uses halted flags at t (1m bars in [t − 60 min, t)) (luna/v7/book/engine.py:550-555); execution rechecks at t + 15 min (execution.py:234-240).
4. **Why ambiguous:** halts are defined at the order time, which is t + 15 min (look-ahead if used at t).
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: removes 15 min of look-ahead; the fill-time check still blocks orders.
7. **Alternatives:** flags at t + 15 min.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-59 — AMB-16(l) / R21: held leg unscored only because U8 is false
1. **ID / title:** DK-59.
2. **Spec:** S-7.1 "After the minimum hold, a leg exits at a decision if it fails a gate, ranks outside K + 2 on its side, or its score falls below the hurdle."; S-7.3 "**U8 false:** no entry or increase; it doesn't trigger an exit."
3. **Code now:** a held leg failing only U8 has no rank (scores cover U1-U8-passing rows); it gets no rank / hurdle exit at that decision; other ranks unchanged (luna/v7/book/constructor.py:197-200).
4. **Why ambiguous:** literally an unranked leg "ranks outside K+2", which would make U8 an exit trigger.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: honours S-7.3 without changing other candidates' ranks.
7. **Alternatives:** scoring pool = U1-U7-passing symbols incl. held legs (the U8-false leg is ranked; U8 still blocks entry).
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-60 — AMB-17(a): leg-stop price, cadence, missing mark
1. **ID / title:** DK-60.
2. **Spec:** S-7.3 "**Leg stop:** adverse move ≥ 15 % from the average entry price → taker exit, 24 h cooldown."
3. **Code now:** evaluated on fresh marks at every 15-minute tick and at decisions; average entry = quantity-weighted executed entry price incl. spread / slippage; a missing mark never triggers (luna/v7/book/risk.py:124-129, :249-277; ledger.py:82-83).
4. **Why ambiguous:** cadence, price type (executed vs reference mid), missing marks.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: S-8.1 marks are the only continuous price; executed price is the "entry price".
7. **Alternatives:** reference-mid entry price; 1-minute cadence.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-61 — AMB-17(b): daily-loss / kill cadence and day start
1. **ID / title:** DK-61.
2. **Spec:** S-7.3 "**Daily loss:** equity ≤ 98 % of the 00:00 equity → no entries until the next 00:00 UTC. **Drawdown kill:** equity ≤ 85 % of the running peak of the 15-minute marked equity".
3. **Code now:** both at 15-minute marks; pause sticky until next 00:00; day start = 00:00 mark (run start before the first 00:00) (luna/v7/book/risk.py:172-196).
4. **Why ambiguous:** cadence of the daily-loss test.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: same marks as the kill.
7. **Alternatives:** evaluate only at decisions.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-62 — AMB-17(c): ineligibility "two consecutive decisions"
1. **ID / title:** DK-62.
2. **Spec:** S-7.3 "**Ineligibility exit:** failing U4, U5 (exit threshold 20 bps half-spread) or U7 on two consecutive decisions → taker exit."
3. **Code now:** streak counts consecutive decisions on which (U4 fails OR ĥ > 20 bps OR U7 fails); missing ĥ counts as failing (luna/v7/book/risk.py:132-135, :222-228).
4. **Why ambiguous:** one streak for the disjunction vs one per gate.
5. **Affects:** T, P.
6. **Recommendation:** keep the disjunction. Reason: "failing U4, U5 or U7" describes one condition; more conservative.
7. **Alternatives:** the same gate on both decisions.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-63 — AMB-17(d)/(e): cooldown scope and exit precedence
1. **ID / title:** DK-63.
2. **Spec:** S-7.3 leg stop "24 h cooldown"; S-7.1 entry "no cooldown".
3. **Code now:** per symbol (both sides), 24 h from the leg-stop exit FILL; only leg stops start it; precedence kill > leg stop > U6 > ineligibility (luna/v7/book/risk.py:54-55, :161-167; engine.py:387-388).
4. **Why ambiguous:** scope and start time unstated.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: conservative and uniquely defined.
7. **Alternatives:** per (symbol, side); from the trigger.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED for the cooldown scope (reclassified, red-team misclassified list: the text does not decide per symbol vs per side, and it changes entries). The precedence part stays an implementation clarification.
9. **Blocker:** yes (confirmation).

### DK-64 — AMB-19(a) / AMB-17(f): ineligibility exit fill timing
1. **ID / title:** DK-64.
2. **Spec:** S-4.2 "Forced risk exits fill on the first 1-minute bar after the trigger + 1 min."; S-7.3 "Ineligibility exit: ... → taker exit."; S-7.3 U6 "filled at that decision's `t` + 15 min (S-4.2) as a taker exit".
3. **Code now:** S-4.2 forced timing from the decision time (≈ t + 1 min) (luna/v7/book/engine.py:91, :327-330; execution.py:250-265).
4. **Why ambiguous:** S-7.3 groups it with the U6 exit (t + 15 min) but calls it a risk-layer exit.
5. **Affects:** T, P (executed prices ~14 min earlier).
6. **Recommendation:** keep S-4.2 timing. Reason: it is a risk-layer exit; only U6 carries an explicit t + 15 min exception.
7. **Alternatives:** t + 15 min.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-65 — AMB-18(a): hedge size clip
1. **ID / title:** DK-65.
2. **Spec:** S-7.4 "If |β_book| > 0.05 · E, the target hedge is H_B = −β_book ... |H_B| ≤ 0.5 · E."
3. **Code now:** H_B clipped to [−0.5E, 0.5E] (luna/v7/book/hedge.py:50-58).
4. **Why ambiguous:** clip vs refuse vs shrink legs.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: ML / constructor cannot change sizing for the hedge (S-0.6).
7. **Alternatives:** none in the frozen text.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-66 — AMB-18(b): hedge when a held leg's beta is missing
1. **ID / title:** DK-66.
2. **Spec:** S-7.4 "β_book = Σ_i n_i · β̂_i(t)".
3. **Code now:** no target, no hedge trade, current hedge kept (`beta_missing:<sym>`) (luna/v7/book/hedge.py:82-84).
4. **Why ambiguous:** no rule for a missing β̂ of a held leg.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: no β is invented; a stale hedge is closer to the true hedge than none.
7. **Alternatives:** β = 1.0 prior for that leg; hedge the remaining legs only; close the hedge.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (borderline).
9. **Blocker:** yes.

### DK-67 — AMB-18(c): hedge trades at a stale decision
1. **ID / title:** DK-67.
2. **Spec:** S-7.3 "**Staleness:** a decision with any required input past its freshness rule is skipped for entries; exits still run."
3. **Code now:** the S-7.4 hedge rule runs at stale decisions (luna/v7/book/engine.py:564).
4. **Why ambiguous:** a hedge trade is neither an entry nor an exit.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: the hedge is risk reduction; skipping only entries is literal.
7. **Alternatives:** skip hedge trades at stale decisions.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (borderline).
9. **Blocker:** yes.

### DK-68 — AMB-19(c): drawdown-kill details
1. **ID / title:** DK-68.
2. **Spec:** S-7.3 "exit everything, no re-entry for the rest of the run".
3. **Code now:** every leg and the hedge exit with S-4.2 timing from the kill tick; unfilled entries cancelled; in-flight exits keep their time except resting E_M maker exits (replaced by taker) (luna/v7/book/engine.py:462-476).
4. **Why ambiguous:** treatment of orders in flight.
5. **Affects:** T, P.
6. **Recommendation:** keep. Reason: literal "no re-entry"; forced exits are taker (S-6.2).
7. **Alternatives:** let in-flight entries fill and exit them.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-69 — AMB-19(d): non-halted exit without any reference price
1. **ID / title:** DK-69.
2. **Spec:** S-4.4 "if the required reference price itself is missing, the existing invalid / gap policy applies (S-5.4, S-5.5). No price is invented."; S-5.4 "Data gap at τ (not halted): use the first 1-minute bar within 60 min after τ. If there is none, the label is invalid."
3. **Code now:** a non-halted exit with no bar within 60 min goes to the S-5.4 wait (first volume > 0 bar of the instance, its close, stressed costs) (luna/v7/book/execution.py:238-240, :268-282; engine.py:310-319). Entries / increases: no trade.
4. **Why ambiguous:** the label policy (invalid) has no book form.
5. **Affects:** T, P, V.
6. **Recommendation:** keep the S-5.4 stressed wait. Reason: the closest frozen book rule; never invents a price.
7. **Alternatives:** INVALID RUN; normal-cost fill at the first bar after the gap.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-70 — AMB-19(e): stale leg marks at a 15-minute tick
1. **ID / title:** DK-70.
2. **Spec:** S-8.1 "Equity is marked to the mark price at every 15-minute tick: E = cash + Σ q · mark."; §15 "Prices are never backfilled or interpolated."; user round 1 AMB-20: "never carries forward a stale mark" (for the terminal mark).
3. **Code now:** mark 15m close -> last-price 15m close -> previous price event carried forward (`stale`) for legs (luna/v7/book/execution.py:213-220; engine.py:411-418); leg stops ignore stale marks; equity, daily-loss and kill use the carried value. (The BTC hedge has no stale carry since AMB-21(b).)
4. **Why ambiguous:** S-8.1 gives no fallback; carrying forward delays a loss into equity, the daily-loss pause, the kill and P3's drawdown.
5. **Affects:** P (path), V, G (P3 drawdown, kill).
6. **Recommendation:** keep the stale carry for leg marks (it moves no P&L; PR uses the next real price) and report the count of stale leg marks per run. Reason: an INVALID RUN on any single missing 15m bar of any held leg would be far stricter than the frozen data rules require; mark -> last close is the S-4.6 chain.
7. **Alternatives:** index price or last 1-minute close before carrying; missing mark = INVALID RUN (the AMB-21(b) treatment for BTC).
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-71 — AMB-19(f): event order at one timestamp
1. **ID / title:** DK-71.
2. **Spec:** S-8.3 "If a fill and a tick share a timestamp, the tick comes first; same-timestamp fills keep booking order."; S-5.3 funding over (t_e, t_x].
3. **Code now:** funding -> tick -> fills (forced, decision exits, neutrality, reduces, entries, hedge) -> terminal -> neutrality check -> decision -> unwind (luna/v7/book/engine.py:86, :781-802); funding at τ uses the position before fills at τ.
4. **Why ambiguous:** only tick-before-fill is fixed.
5. **Affects:** P.
6. **Recommendation:** keep. Reason: matches the label window (exit at τ pays τ's settlement, entry does not).
7. **Alternatives:** fills before funding.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-72 — AMB-19(g): E_M quantity scale and maker reference mid
1. **ID / title:** DK-72.
2. **Spec:** S-6.2 "the filled quantity = min(remaining, cumulative trade-through quantity), from trades with timestamp > `t_e`, within W = 30 min"; "The touch is P(t_e) ∓ ĥ(t)"; §14 L-norm "liquidity cap evaluated at E_ref = 10,000 USD".
3. **Code now:** book-unit quantities scaled by E_ref = 10,000 USD per unit before comparison with aggTrade base quantities; maker fills booked at L with reference mid = L (no spread cost) (luna/v7/book/execution.py:311-356).
4. **Why ambiguous:** L-norm has no USD scale; the scale decides how much fills as maker. The spread capture is shown in PR rather than as negative cost.
5. **Affects:** S (E_M is secondary; also P1(E_M) for the maker-dependent split, DK-84).
6. **Recommendation:** keep E_ref scaling and L as reference mid. Reason: the same scale the spec already uses for L-norm liquidity.
7. **Alternatives:** 265 USD scale; unscaled (E = 1 USD); reference mid = P(t_e).
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes (secondary and the maker split).

### DK-73 — AMB-19(h): book funding with an invalid record or missing mark
1. **ID / title:** DK-73.
2. **Spec:** S-8.1 "Funding is booked at settlement times."; S-4.6 mark chain; K8 "invalid labels or missing funding > 0.5 % ... INVALID RUN".
3. **Code now:** an invalid record or missing mark books nothing and is logged (`funding_invalid:` / `mark_missing:`); a DEC-29 missing settlement is logged per episode (luna/v7/book/engine.py:383-405); these feed K8 (DK-89).
4. **Why ambiguous:** omitting a debit inflates P&L; the spec has no book rule.
5. **Affects:** P, V.
6. **Recommendation:** keep, with K8 as the escalation. Reason: no rate or mark is invented; K8 bounds the omission at 0.5 %.
7. **Alternatives:** INVALID RUN on any missing held-position settlement.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-74 — AMB-19(i)/(j): terminal check, unwind, last decision
1. **ID / title:** DK-74.
2. **Spec:** S-8.1 "**Terminal unwind:** at the last reference minute before the window's end (S-1.5), every leg and the hedge exit taker with S-5.3 cost rules."; S-5.4 terminal rules.
3. **Code now:** terminal check at `last_trade_ts` (A: official time); unwind at end − 1 min, legs then hedge; halted or no bar -> INVALID RUN; decisions only when t + 15 min < end − 1 min (luna/v7/book/engine.py:346-353, :498-525, :783-785).
4. **Why ambiguous:** whether a decision whose fills would fall at / after the unwind runs.
5. **Affects:** T, P, V.
6. **Recommendation:** keep. Reason: no order can fill after the unwind.
7. **Alternatives:** none material.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-75 — AMB-25(b): which inputs make a decision stale
1. **ID / title:** DK-75.
2. **Spec:** S-7.3 "**Staleness:** a decision with any required input past its freshness rule is skipped for entries; exits still run."; S-4.5 "If 𝒫(τ) is empty, the value is missing: a staleness failure at decisions (S-7.3)".
3. **Code now:** stale iff the hedge instrument (BTCUSDT) has no current instance, its last 15m bar is missing / unpriced, or its ĥ(t) is missing (luna/v7/runner.py:286-304). Per-symbol freshness is handled by gates.
4. **Why ambiguous:** "required input" is not listed.
5. **Affects:** T, P (when entries are skipped).
6. **Recommendation:** keep. Reason: per-symbol inputs are already gates (U4, U5, U7, U8); only book-wide inputs make the whole decision stale.
7. **Alternatives:** any held / eligible symbol's missing input makes the decision stale; also a stale held-leg mark.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-76 — AMB-28 implementation details of the resolved one-sided flatten (incl. R36, R37)
1. **ID / title:** DK-76.
2. **Spec / decision:** S-7.4 "**The single book-level residual BTC hedge**"; S-4.2 forced timing; user round 1 AMB-16(d): "The entire ML book is flattened at the next executable opportunity."
3. **Code now:** a tick-level check flattens holdable legs AND the hedge (R37) with S-4.2 timing (R36) (luna/v7/book/engine.py:437-460); a side with an entry in flight is not empty; resting E_M maker exits are replaced by taker; the constructor's empty-side `neutrality` close is forced at t (S-4.2) (constructor.py:295-298), so the surviving side can exit ~14 min before decision exits of the emptying side (t + 15 min); the hedge closes at t + 15 min via S-7.4 when the flatten happens at a decision.
4. **Why it needs confirmation:** "entire ML book" including the hedge, "next executable opportunity" timing and the in-flight-entry exception are readings of the decision.
5. **Affects:** T, P.
6. **Recommendation:** confirm these details. Reason: literal reading of the user decision; never carries one side alone longer than one tick.
7. **Alternatives:** keep the hedge until the next decision; treat in-flight entries as empty.
8. **Classification:** IMPLEMENTATION CLARIFICATION (reading of a user decision).
9. **Blocker:** yes (until confirmed).

### DK-77 — `maker_exit` flag set when no maker order rests (deferred minor, Task 14b)
1. **ID / title:** DK-77.
2. **Spec:** S-6.2 "Risk exits, forced exits and hedge trades are always taker."
3. **Code now:** `exit()` sets `p.maker_exit = True` on every non-forced E_M leg exit, even when `maker_at` returned a `Pending` (halted / no price at t_e) or a taker fill (ĥ missing) (luna/v7/book/engine.py:331-335; execution.py:323-329). Such a leg is then reported as active (`legs()`, engine.py:279) and a later forced exit / flatten replaces its S-5.4 wait (engine.py:459, :472), relabelling the exit reason (e.g. `leg_stop` -> starts a cooldown, engine.py:387-388).
4. **Why it is a defect:** the flag is meant to mark a resting maker order only.
5. **Affects:** S (E_M runs only: exit reason, cooldowns, risk effects).
6. **Recommendation:** set `maker_exit` only when a maker limit actually rests. Reason: matches the documented intent.
7. **Alternatives:** none.
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED.
9. **Blocker:** yes (secondary only; small fix).

### DK-78 — A hedge open in flight is not cancelled by the one-sided flatten (deferred minor, Task 19c)
1. **ID / title:** DK-78.
2. **Spec / decision:** user round 1 AMB-16(d) "The opposite side is never carried alone."; R37 (hedge part of the book).
3. **Code now:** `on_neutral` exits positions in the ledger only (luna/v7/book/engine.py:458-460); a hedge-open token scheduled at a decision for t + 15 min (no position yet) is not cancelled and opens after the flatten, leaving a hedge alone until the next decision.
4. **Why it is a defect:** contradicts the flatten of the entire book (rare: needs a side to empty between the decision and its fill).
5. **Affects:** T, P.
6. **Recommendation:** cancel in-flight hedge-open / hedge-trade tokens when the flatten triggers. Reason: consistent with R37 and the kill.
7. **Alternatives:** none.
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED.
9. **Blocker:** yes (small fix).

## F. Accounting / verdict

### DK-79 — AMB-21(a) / R27: N_a keyed by symbol
1. **ID / title:** DK-79.
2. **Spec:** S-8.4 "N_a = Σ over a's clusters of (leg funding + PR_c − leg C_exec)."; P5 "top-1 N_a ≤ 0.30 · NET and top-3 ≤ 0.60 · NET"; S-5.4 E10 "instances are economically separate instruments".
3. **Code now:** N_a and per-asset ρ pooled per SYMBOL; episodes / clusters per instance (luna/v7/accounting.py:595-609).
4. **Why ambiguous:** "asset" vs instance.
5. **Affects:** G (P5).
6. **Recommendation:** keep per symbol. Reason: conservative for concentration; matches the constructor's "asset".
7. **Alternatives:** per instance.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED. Reclassified (red-team misclassified list): S-8.4 "asset" and E10 "instances are economically separate instruments" pull in opposite directions, and P5 is a gate.
9. **Blocker:** yes (confirmation).

### DK-80 — AMB-21(c): K10 tolerance and the daily identity
1. **ID / title:** DK-80.
2. **Spec:** K10 "|Σ_c ρ_c + MKT − PR| > 1e-9 × Σ|fill notional|, or PR ≠ the daily-table price column | INVALID RUN"; S-8.1 "The components sum exactly to the daily equity change."
3. **Code now:** both K10 clauses and the summation by parts use 1e-9 × Σ|qty · ref_mid|, in total and per day; `daily_ok` failure is an integrity failure -> INVALID RUN (luna/v7/accounting.py:559-593; verdict.py:419-421; selection.py:98-100).
4. **Why ambiguous:** "PR ≠" exact equality is unattainable in floating point.
5. **Affects:** V.
6. **Recommendation:** keep. Reason: reuses the frozen K10 tolerance for the second clause.
7. **Alternatives:** exact equality (would invalidate every run).
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-81 — AMB-21(d): "mean daily net" units (P6, P10, S-10.7)
1. **ID / title:** DK-81.
2. **Spec:** P6 "stressed mean daily net ≥ 0"; P10 "ML\* out-of-fold mean daily net on `W_DEV` > 0"; S-10.7 "mean daily net ≤ 0"; S-13.1 r_t = (E_t − E_{t−1}) / E_{t−1}.
3. **Code now:** mean of the daily table's net (book units, E0 = 1) everywhere; the stressed ledger shifts costs only, no re-simulation of risk (luna/v7/accounting.py:634-636, :662-690).
4. **Why ambiguous:** "net" (units) vs "return" (S-13.1); signs can differ only in edge cases.
5. **Affects:** G.
6. **Recommendation:** keep. Reason: the literal word "net" and P6's "same executed book".
7. **Alternatives:** mean daily return.
8. **Classification:** IMPLEMENTATION CLARIFICATION. Re-evaluated (red-team misclassified list): it drives a kill and two gates, but P6, P10 and S-10.7 all say "mean daily net". S-13.1 uses "daily returns" for r_t, so the spec distinguishes the two terms itself.
9. **Blocker:** yes (confirmation).

### DK-82 — AMB-21(e): P11 Top_m details
1. **ID / title:** DK-82.
2. **Spec:** P11 "Top_m are the m clusters with the largest positive ρ_c and m = max(3, ⌈0.05 · n_clusters⌉) (n_clusters = clusters with ≥ 1 closed episode)".
3. **Code now:** integer ceiling; strictly positive ρ only; ties by (asset, side, first start) (luna/v7/accounting.py:612-631).
4. **Why ambiguous:** ties and fewer positive clusters than m.
5. **Affects:** G.
6. **Recommendation:** keep. Reason: exact arithmetic; ties do not change the sum.
7. **Alternatives:** none material.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-83 — AMB-22(c): Holm with a voided member
1. **ID / title:** DK-83.
2. **Spec:** S-13.3 "Holm–Bonferroni across exactly this family of 6"; S-13.4 "An invalid secondary arm voids only its own comparison".
3. **Code now:** m stays 6; a voided member ranked as p = 1, reported unavailable (luna/v7/stats.py:192-217).
4. **Why ambiguous:** keep m = 6 or adjust over the available members.
5. **Affects:** S.
6. **Recommendation:** keep. Reason: "exactly 6"; conservative.
7. **Alternatives:** m = number of available members.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (secondary only).

### DK-84 — AMB-22(g) / R29: E_M runs secondary; undecidable maker split
1. **ID / title:** DK-84.
2. **Spec:** S-13.4 "`NO-GO: no viable edge` | S-10.7 development kill, or P1 fails under `E_T` while it also fails under `E_M`"; "`NO-GO: maker-dependent` | P1 fails under `E_T` but holds under `E_M`"; "a required arm (ML\*, A0) ... An invalid secondary arm voids only its own comparison"; K11 required runs ML\* and A0.
3. **Code now:** only ML\* E_T and A0 E_T are required; if P1(E_T) fails and P1(E_M) is unavailable, class = NO-GO with `maker_split_undecidable` (outranks INCONCLUSIVE; never ADVANCE) (luna/v7/verdict.py:106-148, :385-396, :449-457).
4. **Why ambiguous:** the S-13.4 split needs P1(E_M) while E_M runs are secondary.
5. **Affects:** G (label of a NO-GO; never ADVANCE).
6. **Recommendation:** keep. Reason: never upgrades a failing verdict; E_M stays secondary as S-6.2 says.
7. **Alternatives:** E_M runs required (INVALID RUN); map undecidable to "NO-GO: no viable edge".
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-85 — AMB-22(h): P4 definitions
1. **ID / title:** DK-85.
2. **Spec:** P4 "≥ 60 active days (gross ≥ 10 % of E for ≥ 48 of 96 quarter-hours), ≥ 100 closed legs, ≥ 20 clusters (S-8.3)".
3. **Code now:** gross = leg gross at the tick (hedge excluded) ≥ 10 % of marked equity at that tick; closed legs = position ids of closed episodes (luna/v7/verdict.py:160-183).
4. **Why ambiguous:** hedge in gross; legs vs episodes.
5. **Affects:** G (INCONCLUSIVE).
6. **Recommendation:** keep. Reason: P4 measures the leg book's activity.
7. **Alternatives:** gross incl. hedge; closed legs = closed episodes.
8. **Classification:** IMPLEMENTATION CLARIFICATION. Re-evaluated (red-team misclassified list): "gross" is used for legs in S-7.1 ("each side's gross is 0.5 · E") and in S-7.3 (see DK-109). Each leg entry is one "leg" (S-7.1 "at most K = 5 legs per side").
9. **Blocker:** yes (confirmation).

### DK-86 — AMB-22(i): P5 conventions
1. **ID / title:** DK-86.
2. **Spec:** P5 "net > 0 in ≥ 2 of the 4 holdout quarters; no quarter > 60 % of NET; ... top-1 N_a ≤ 0.30 · NET and top-3 ≤ 0.60 · NET".
3. **Code now:** quarter net by day start; literal inequalities against NET (NET ≤ 0 fails); top-k = k largest signed N_a (luna/v7/verdict.py:207-223).
4. **Why ambiguous:** NET ≤ 0 and negative N_a handling.
5. **Affects:** G.
6. **Recommendation:** keep. Reason: literal.
7. **Alternatives:** absolute shares.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-87 — AMB-22(j): P8-classifier ECE bins
1. **ID / title:** DK-87.
2. **Spec:** S-13.3 "ECE ≤ 0.05 over 10 equal-frequency bins (date-equal weights)".
3. **Code now:** 10 equal-row-count bins (`array_split`), date-equal weights (luna/v7/verdict.py:244-254).
4. **Why ambiguous:** equal frequency by rows vs by weight.
5. **Affects:** S (A1 claims only).
6. **Recommendation:** keep. Reason: simplest literal reading.
7. **Alternatives:** weighted-quantile edges.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (secondary only).

### DK-88 — AMB-22(d)/(k): P2 / P3 / P7 / P8 / P9 conventions
1. **ID / title:** DK-88.
2. **Spec:** P3 "Maximum drawdown of the 15-minute marked equity ≤ 15 %, and no drawdown kill"; P7 "Date-level IC ... mean > 0 and LB > 0 (S-13.1, ℓ ∈ {14, 28})"; P8 "OLS slope ... over every eligible `W_HOLD` candidate ∈ [0.5, 1.5]"; P9 "Net Sharpe (daily, annualized √365) ≥ 1.0"; §12 "Sharpe (daily mean / std × √365, plus the Lo / Newey-West adjusted value)".
3. **Code now:** P3 over 15m marks plus the final equity; kill = any `killed` decision or `kill` fill (verdict.py:185-205); P7 bootstrap over the date-IC series in date order (gaps skipped) (verdict.py:257-266); P8 unclipped date-weighted slope, constant ŷ_cal -> NaN -> fail (verdict.py:226-241); P9 plain Sharpe with std ddof 1 (stats.py:233-250); NW value report only.
4. **Why ambiguous:** series construction and edge cases.
5. **Affects:** G.
6. **Recommendation:** keep. Reason: literal, fail-closed edge cases.
7. **Alternatives:** P7 over calendar days with 0 for missing dates; P9 on the NW value.
8. **Classification:** IMPLEMENTATION CLARIFICATION. Re-evaluated (red-team misclassified list): P3 including the post-unwind equity follows S-13.1 ("The last day includes the terminal unwind"). For P7, the IC is defined per decision date, so the series is over dates; imputing 0 for skipped dates would invent IC values.
9. **Blocker:** yes (confirmation).

### DK-89 — AMB-27(b) / R30: K8 missing-funding share
1. **ID / title:** DK-89.
2. **Spec:** K8 "During the holdout run: invalid labels or missing funding > 0.5 %, or a Stage 0-type integrity failure | INVALID RUN".
3. **Code now:** over ML\* and A0 E_T runs: numerator = unbooked settlements of held positions (one per settlement) + episodes with a DEC-29 missing settlement (once per episode, a lower bound); denominator = booked settlements (legs and hedge) + numerator; threshold > 0.5 % (luna/v7/runner_select.py:116-131; verdict.py:409-413).
4. **Why ambiguous:** the base (book vs universe data), counting of gaps, hedge inclusion.
5. **Affects:** V.
6. **Recommendation:** keep. Reason: measures funding that actually entered the evaluated book.
7. **Alternatives:** per symbol-day share over the eligible universe; count every missing settlement of a gap; exclude the hedge.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (borderline).
9. **Blocker:** yes.

### DK-90 — AMB-27(e): holdout P7 / P8 / K8 input panels
1. **ID / title:** DK-90.
2. **Spec:** P7 "Date-level IC of the ML\* regression model"; P8 "over every eligible `W_HOLD` candidate"; K8 "invalid labels ... > 0.5 %".
3. **Code now:** panel = valid labels of every gated candidate (both sides) predicted by the final model; P7 on raw ŷ, P8 on ŷ_cal; K8 invalid share over existing labels (S-1.5 non-labels excluded) (luna/v7/runner.py:481-526).
4. **Why ambiguous:** invalid labels excluded from P7/P8; K8 base.
5. **Affects:** G, V.
6. **Recommendation:** keep. Reason: invalid labels have no y; K8 counts them.
7. **Alternatives:** none material.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-91 — AMB-27(d) / AMB-22(m): S-10.7 fold nets and the cost ratio
1. **ID / title:** DK-91.
2. **Spec:** S-10.7 "C_exec / F_net > 1.0"; "fewer than 30 % of outer folds have positive net".
3. **Code now:** fold net = period net of the ML\* ledger over each outer test quarter by day start; ratio tested only if F_net > 0 (luna/v7/selection.py:115-136; runner_select.py:106).
4. **Why ambiguous:** fold attribution in one continuous book; the ratio with F_net ≤ 0.
5. **Affects:** G (development kill).
6. **Recommendation:** keep. Reason: F_net ≤ 0 is its own trigger.
7. **Alternatives:** none material.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

## G. Deployment (L-265 / L-10k; never the scientific verdict)

### DK-93 — AMB-23(a)/(b)/(c): step rounding, closes, reduces
1. **ID / title:** DK-93.
2. **Spec:** §14 L-265 "exchange step rounding, per-symbol minimum quantity / notional ... Infeasible legs or hedges are not traded (fail closed)".
3. **Code now:** round towards zero; rounded qty must meet min qty / notional (luna/v7/deploy.py:96-111); full closes never filtered; reduces rounded at their fill reference mid, infeasible -> not traded (engine.py:723-737, :772).
4. **Why ambiguous:** rounding direction; filters on closes.
5. **Affects:** D.
6. **Recommendation:** keep. Reason: never exceeds caps; a position can always be closed.
7. **Alternatives:** round to nearest; filter closes.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (deployment only).

### DK-94 — AMB-23(d): infeasible required hedge
1. **ID / title:** DK-94.
2. **Spec:** §14 "if a required hedge is infeasible, the beta rule forbids the entries".
3. **Code now:** ALL of the decision's entries are dropped and the hedge re-decided; if still infeasible, not traded (luna/v7/book/engine.py:751-760).
4. **Why ambiguous:** all entries vs entries one by one.
5. **Affects:** D.
6. **Recommendation:** keep. Reason: literal "forbids the entries".
7. **Alternatives:** drop entries lowest rank first until feasible.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (deployment only).
9. **Blocker:** yes (deployment only).

### DK-95 — AMB-23(e) / AMB-28(a'): one-sided book after rounding
1. **ID / title:** DK-95.
2. **Spec:** §14 "Infeasible legs or hedges are not traded (fail closed)"; user round 1 AMB-16(d) "The opposite side is never carried alone."
3. **Code now:** no re-neutralisation after an infeasible leg; when rounding leaves the post-decision book one-sided, the decision's entries are forbidden (`one_sided:entries_forbidden`) and held one-sided legs are flattened at the next tick (luna/v7/book/engine.py:742-750).
4. **Why ambiguous:** the frozen L-265 text predates the user decision.
5. **Affects:** D.
6. **Recommendation:** keep. Reason: avoids entering and force-flattening 11 minutes later at every decision.
7. **Alternatives:** enter and flatten at the next tick; re-apply S-7.1 neutrality after feasibility.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (deployment only).
9. **Blocker:** yes (deployment only).

### DK-96 — AMB-23(i): DEC-34 symbol without filters
1. **ID / title:** DK-96.
2. **Spec:** S-14.1 step 6 "Otherwise the symbol is excluded from exact L-265 feasibility, and the historical deployment result is flagged approximate/incomplete."
3. **Code now:** the symbol is never traded at that decision (infeasible), the run is flagged `filters_missing`; a missing BTC filter forbids every entry needing a hedge (luna/v7/deploy.py:145-150, :181-189).
4. **Why ambiguous:** "excluded from feasibility" could mean excluded from the D1 base.
5. **Affects:** D.
6. **Recommendation:** keep. Reason: fail closed.
7. **Alternatives:** drop decisions needing that symbol from the D1 denominator.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (deployment only).
9. **Blocker:** yes (deployment only).

### DK-97 — AMB-23(j): D1 path dependence
1. **ID / title:** DK-97.
2. **Spec:** D1 "≥ 90 % of the decisions at which L-norm holds a non-empty book are constructible at 265 USD, legs and any required hedge included".
3. **Code now:** gate = share of L-norm non-empty decisions whose L-265 decision at the same t is constructible (path-dependent L-265 run) (luna/v7/deploy.py:196-212); the path-independent scaled share is reported only (deploy.py:214-247). An unfilled (halted) reduce counts as constructible (deferred minor).
4. **Why ambiguous:** "constructible" for the actual L-265 run or for the scaled L-norm book.
5. **Affects:** D.
6. **Recommendation:** keep the path-dependent gate and report the scaled share. Reason: tests the strategy actually run at 265 USD.
7. **Alternatives:** path-independent gate.
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (deployment only).
9. **Blocker:** yes (deployment only).

### DK-98 — AMB-23(f)/(h)/(k): E_T only, filter timing, D2 / D3
1. **ID / title:** DK-98.
2. **Spec:** §14 D2 "L-265 mean daily net > 0"; D3 "annualized L-265 net USD − annual fixed infrastructure cost > 0"; S-14.1 filter hierarchy.
3. **Code now:** USD layers under E_T only; PIT filters apply when known_at ≤ t − 60 s; D3 = 365 × mean daily net − 144 > 0; an invalid L-265 run fails D1-D3 (luna/v7/deploy.py:136-143, :249-292).
4. **Why ambiguous:** execution model and annualisation unstated.
5. **Affects:** D.
6. **Recommendation:** keep. Reason: E_M maker fills with lot rounding are undefined.
7. **Alternatives:** none material.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (deployment only).

## H. Forward

### DK-99 — AMB-24(f): forward verdict precedence
1. **ID / title:** DK-99.
2. **Spec:** §15 Verdicts table: "`INVALID RUN` | ..."; "`NO-GO` | any GO criterion fails, or the replay hits the drawdown kill"; "`INCONCLUSIVE` | evidence not met"; "`GO` | all GO criteria hold"; GO criteria include "the evidence rules"; S-13.4 ranks INCONCLUSIVE above NO-GO.
3. **Code now:** INVALID > NO-GO > INCONCLUSIVE > GO, NO-GO excluding the evidence rules (luna/v7/forward.py:262-274).
4. **Why ambiguous:** a strict reading makes INCONCLUSIVE unreachable; the table order differs from S-13.4.
5. **Affects:** G (and whether the extension can be earned).
6. **Recommendation:** keep. Reason: the only reading where every row applies; no second look on a failing book.
7. **Alternatives:** S-13.4 order (INCONCLUSIVE first).
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-100 — AMB-24(k): thresholds of the extended forward look
1. **ID / title:** DK-100.
2. **Spec:** §15 "**INCONCLUSIVE** if evaluable days < 144 (80 % of 180)."; "P4 scaled to 180 / 365: ≥ 30 active days, ≥ 50 closed legs, ≥ 10 clusters."; "**One** 90-day extension ... (same rules, single look at the extended end)."
3. **Code now:** absolute 144 days and 30 / 50 / 10 on the 270-day look (luna/v7/forward.py:252-259; FROZEN.forward).
4. **Why ambiguous:** "same rules" = same numbers or same derivation (216 days; 45 / 74 / 15).
5. **Affects:** G.
6. **Recommendation:** keep the absolute numbers. Reason: keeps the frozen numbers; no new rounding rule.
7. **Alternatives:** scaled thresholds (stricter).
8. **Classification:** NORMATIVE AMENDMENT REQUIRED.
9. **Blocker:** yes.

### DK-101 — AMB-24(b)/(c): evaluable day, recorder gaps, decision shift
1. **ID / title:** DK-101.
2. **Spec:** §15 "**Evaluable day:** ≥ 90 % of its quarter-hours are evaluable."; "A recorder gap > 24 h in a required input makes those days non-evaluable."; "the decision is made at the first evaluable quarter within 2 h after `t`".
3. **Code now:** ≥ 87 of 96; every day the gap overlaps; shift by k · 15 min, k = 1..8, keeping t's offset (luna/v7/forward.py:132-192).
4. **Why ambiguous:** rounding, "those days", offset.
5. **Affects:** G.
6. **Recommendation:** keep. Reason: conservative integer rounding.
7. **Alternatives:** gap days fully covered only.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-102 — AMB-24(e): evidence counted on the whole forward book
1. **ID / title:** DK-102.
2. **Spec:** §15 evidence rules and P4 scaled.
3. **Code now:** P4 counts over the whole book, active days not restricted to evaluable days; extension only when the INCONCLUSIVE reasons are exactly {"coverage"} (luna/v7/forward.py:239-259).
4. **Why ambiguous:** restriction to evaluable days unstated.
5. **Affects:** G.
6. **Recommendation:** keep. Reason: same conventions as W_HOLD.
7. **Alternatives:** count only evaluable days.
8. **Classification:** IMPLEMENTATION CLARIFICATION. Re-evaluated (red-team misclassified list): §15 states coverage (evaluable days) and P4 as two separate rules. Coverage already penalises non-evaluable days, so restricting P4 to them as well would count the same gap twice.
9. **Blocker:** yes (confirmation).

### DK-103 — AMB-24(g): "GO" vs "GO — not deployable at 265 USD"
1. **ID / title:** DK-103.
2. **Spec:** §15 "A **real-money deployment proposal** additionally requires: D1–D3 on the forward run with point-in-time filters (S-14.1); the DEC-14 venue/account-eligibility and execution-host verification. A scientific GO without these reads 'GO — not deployable at 265 USD'."
3. **Code now:** "GO" only with exact forward D1-D3 true and DEC-14 verification supplied; else "not deployable" (luna/v7/forward.py:262-274, :324-405).
4. **Why ambiguous:** DEC-14 is verified after a scientific GO, so the first look normally reads "not deployable".
5. **Affects:** G, D.
6. **Recommendation:** keep. Reason: literal.
7. **Alternatives:** label from D1-D3 only.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-104 — AMB-24(a)/(d)/(h)/(i)/(j): forward inputs, L7, EC-11, funding backfill, H_F
1. **ID / title:** DK-104.
2. **Spec:** §15 DEC-42 evaluable quarter; "**L7 = max(`T_F7` + 180 d, R_V6) + 1 d.**"; EC-11; "Funding missing in the recorder: fetched from the official Binance REST funding history after the period, hashed, never interpolated. If unavailable → `INVALID RUN`."; H_F.
3. **Code now:** seven primary input keys; after extension L7 = max(T_F7 + 270 d, R_V6) + 1 d; EC-11 by structured terminal hits; recovered funding needs a SHA-256 digest; H_F = centred CBB p < 0.05 (luna/v7/forward.py:96-130, :194-237, :324-405).
4. **Why ambiguous:** encoding choices of the inputs and the extended L7.
5. **Affects:** G, V.
6. **Recommendation:** keep. Reason: literal, fail closed.
7. **Alternatives:** none material.
8. **Classification:** IMPLEMENTATION CLARIFICATION.
9. **Blocker:** yes (confirmation).

### DK-105 — Forward replay integration and the forward refit are not implemented
1. **ID / title:** DK-105.
2. **Spec:** §15 "**Forward model.** A mechanical refit with **no re-selection** ... `T_fit` = [D0, 2026-01-01); `T_cal` = [2026-01-01, 2026-07-01)"; "Missing data. ... If its quarter isn't evaluable, the decision is made at the first evaluable quarter within 2 h after `t`; otherwise it is skipped ... A stale quarter is a no-op for risk and decisions."; "`T_F7` = the first 00:00 UTC ≥ 24 h after the independent review PASS of the forward recorder and replay code."
3. **Code now:** `forward.py` provides helpers (decision_time, skipped_exit_time, evaluability, verdict) but nothing in the engine / runner consumes them (no caller outside forward.py); `cv.final_model` refuses rows at or after the W_HOLD start (luna/v7/cv.py:373-386), so the forward refit on `W_HOLD` rows has no path.
4. **Why it is a gap:** the forward replay is not wired (stale-quarter no-op, decision shift, skipped-decision exits), and the forward model cannot be built.
5. **Affects:** G, V (forward stage only).
6. **Recommendation:** build and test the forward replay and forward refit before the forward recorder / replay review that sets T_F7. Reason: required by §15; does not affect W_DEV / W_HOLD.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED (missing implementation).
9. **Blocker:** yes for the forward-stage freeze; no for the W_DEV / W_HOLD freeze.

## I. Guards / infra

### DK-106 — Integer `known_at` values not window-checked in `ns_array` (parked, Task 7)
1. **ID / title:** DK-106.
2. **Spec:** L1 "the candidate builder asserts `max_known_at` ≤ `t` − 1 min"; L12 "ns time units everywhere".
3. **Code now:** `ns_array` window-checks non-integer inputs but not integer ones; production paths pass through `klines.to_ns_checked` (2017-2100 window) (ledger: Task 7 parked item).
4. **Why it is a defect:** a hand-built frame with garbage integer `known_at` (e.g. ms values or INT64_MIN) would pass the PIT filter.
5. **Affects:** V (only for frames not built by the ingest path).
6. **Recommendation:** window-check integer inputs too. Reason: defence in depth; the real loaders (SC-03) are still to be written.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED (red-team m1; probe: `ns_array([1_700_000_000_000])` in ms is accepted).
9. **Blocker:** yes (red-team must-fix: a latent L1 leak for loaders not yet written).

### DK-110 — Red-team m2: `to_ns` accepts floats
1. **ID / title:** DK-110.
2. **Spec:** L12 "ns time units everywhere".
3. **Code now:** the float path converts through `pd.Timestamp`, so `to_ns(1.6e12)` becomes a 1970 ns value with no ambiguity error (luna/v7/timeutil.py:22-33). It is used for `recv_ts`, `r_v6` and similar inputs.
4. **Why it is a defect:** it bypasses the unit guard that integer inputs get.
5. **Affects:** V (wrong timestamps -> PIT / seal errors).
6. **Recommendation:** refuse floats, or apply the float window. Reason: L12.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED.
9. **Blocker:** yes (red-team must-fix).

### DK-111 — Red-team m3: frozen constants mutable at runtime
1. **ID / title:** DK-111.
2. **Spec:** §20 "The numeric constants ... are fixed by this draft and freeze with the spec."; §17 reproducibility.
3. **Code now:** dict fields inside frozen dataclasses (luna/v7/constants.py:157-195, :210). `FROZEN.accept.p6["spread"] = 1.0` changes P6 for the whole process.
4. **Why it is a defect:** a frozen number can be changed silently.
5. **Affects:** G (any criterion threshold).
6. **Recommendation:** use `MappingProxyType` or tuples. Reason: one-line hardening.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED.
9. **Blocker:** yes.

### DK-112 — Red-team m8: `account()` raises on a required run instead of yielding INVALID RUN
1. **ID / title:** DK-112.
2. **Spec:** S-13.4 "`INVALID RUN` | integrity, guard or K10 failure"; K10.
3. **Code now:** `selection.choose` (luna/v7/selection.py:98) and `verdict.holdout` (luna/v7/verdict.py:416) call `account()`, which raises `ValueError` (missing tick row, non-flat end, missing B) on a run that is marked valid.
4. **Why it is a defect:** the frozen procedure expects a verdict class, but the code produces an exception.
5. **Affects:** V.
6. **Recommendation:** catch accounting failures and map them to `INVALID RUN — K10 / accounting integrity failure` with the detail. Reason: S-13.4.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED.
9. **Blocker:** yes.

### DK-113 — Red-team m9: empty decisions frame lacks the `killed` column
1. **ID / title:** DK-113.
2. **Spec:** P3 "no drawdown kill".
3. **Code now:** `drawdown_killed` refuses a decisions table without `killed` (luna/v7/verdict.py:195-200); the engine's `_frame` of an empty decision list has no columns (luna/v7/book/engine.py:816-821).
4. **Why it is a defect:** the holdout verdict raises instead of returning a class.
5. **Affects:** V.
6. **Recommendation:** build the frame with the schema columns. Reason: deterministic verdict.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED.
9. **Blocker:** yes.

### DK-114 — Red-team m10: the DT4 `research` flag is a caller convention
1. **ID / title:** DK-114.
2. **Spec:** §17 "every run recorded in DT4"; DT4 columns.
3. **Code now:** `runrecord` refuses a dirty tree only when the caller passes `research=True` (luna/v7/runrecord.py).
4. **Why it is a defect:** a research run can be recorded from a dirty tree.
5. **Affects:** V (provenance).
6. **Recommendation:** `run_book` / `dev_selection` / `holdout_inputs` always record with `research=True`. Reason: §17.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED.
9. **Blocker:** yes.

---

## Scope / completeness items (red-team M1–M5)

These items are implementation work, not interpretations of the spec. The red-team's reasoning: DEC-19 opens the holdout only after code freeze, and the holdout run must come from HEAD == `v7-code-frozen`. Anything the frozen procedure needs before or at the opening must therefore exist, and be reviewed, before the freeze. Writing it afterwards would mean changing frozen code.

### SC-01 — M1: real-scale feasibility (W_DEV cannot run on this host)
1. **ID / title:** SC-01.
2. **Spec:** §17 (one reproducible run); S-10.6 (nine required books plus the E_M runs); S-10.4 (label panels for three horizons from D0).
3. **Code now (red-team file:line):**
   - luna/v7/runner.py:122-158: every symbol's full frames are held in memory.
   - runner.py:212-213, :359-361: an LRU of 2,048 decisions, never hit on a sequential replay.
   - luna/v7/universe.py:343-346: the full 1m history is filtered per decision.
   - luna/v7/data/notices.py:157-164: 4,321 `halted()` calls per 72 h test.
   - luna/v7/labels.py:186-187 -> refs.py:64-74: whole frames are trimmed per label.
   - luna/v7/market.py:110-114: full-frame scans per reference price.
   - labels.py:131-134: β̂ is rebuilt per label.
   - Measured on a synthetic fixture: 0.78 s per decision for 11 symbols, 42 ms per label.
4. **Why it matters:** the estimated real-scale need is 20–30 GB of 1m bars (the host has 14 GB RAM), about 90 s per decision and about 70 h per book run, i.e. months in total. A post-freeze performance rewrite would change frozen modules.
5. **Affects:** V (the run cannot complete); no economic rule changes.
6. **Recommendation:** before freeze:
   - per-symbol sorted numpy / memory-mapped storage with lazy loading;
   - `searchsorted` slices instead of full-frame masks;
   - per-instance β̂ / `InstView`;
   - an indexed 72 h test;
   - disk-cached decision views (or all arms in one pass);
   - a memory / time budget test at representative size;
   - an outcome-free dry run (features and gates only).
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED (implementation work before freeze).
9. **Blocker:** yes.

### SC-02 — M2: L9 canaries, K7 audit and a structured LeakageReport
1. **ID / title:** SC-02.
2. **Spec:** L9 (both canaries "before the holdout opening"); K7; DEC-19 "all leakage / canary checks passed"; S-13.4 "NO-GO: leakage"; K1.
3. **Code now:** `leakage` and `integrity` are free caller tuples with "empty = checked" (luna/v7/verdict.py:286-288, :327-330). L9 and K7 are doc rows only (luna/v7/__init__.py:158, :184-185). No canary or gain / weight-share code exists.
4. **Why it matters:** the verdict cannot detect leakage. The only value the code can produce is `leakage=()`, so it is fail-open on the most severe NO-GO class.
5. **Affects:** G.
6. **Recommendation:** before freeze, implement:
   - `canary_permuted_ic` (L9a);
   - `canary_shifted_label` (L9b);
   - `k7_concentration` over every fold model and the final model (definitions per DK-26 / DK-27);
   - a `LeakageReport` object that `holdout_inputs` requires, with its hash recorded in DT4.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED (implementation work before freeze).
9. **Blocker:** yes.

### SC-03 — M3: S-1.7 fetch layer, read-only loaders, K9 logging, E6 / post-opening W_HOLD path, W_HOLD guard on features
1. **ID / title:** SC-03.
2. **Spec:**
   - S-1.7: "every V7 research request goes through one fetch layer that refuses, before any network I/O, everything not listed"; "read-only loaders"; "no `W_HOLD` features or labels" before the opening.
   - E6: the bound is raised to 20260630235959 at the opening.
   - K9: "guard refusal; logged; incident report".
3. **Code now:**
   - `netguard.check` is pure, and nothing in `luna/v7` calls it (luna/v7/netguard.py:129-199).
   - `guard.check_listing_only` is never called (luna/v7/guard.py:111-125).
   - N1 refuses W_HOLD content forever, and the N5 / N6 bound is hard-coded at 20250630235959 (netguard.py:163-164, :195).
   - `DecisionProvider` computes features at any t, including W_HOLD, with no guard (luna/v7/runner.py:335-362).
4. **Why it matters:**
   - At the legitimate opening, W_HOLD files and E6 captures cannot be fetched through frozen code.
   - Before the opening, a W_HOLD file on disk can be featurized with no refusal and no K9 log.
5. **Affects:** V, G (seal; the ability to run the holdout from the frozen tag).
6. **Recommendation:** before freeze, add:
   - one `fetch(req)` that calls `netguard.check` before I/O and appends to a K9 refusal log;
   - loaders that call `check_listing_only` / `check_outcome_window` on the data span;
   - an authorize-gated E6 / N1 opening mode;
   - a W_HOLD guard in `DecisionProvider.__call__`.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED (implementation work before freeze).
9. **Blocker:** yes.

### SC-04 — M3: Kraken exploratory path (S-1.6 / S-2.4 / S-13.3 #5 / L11): ship before freeze or defer
1. **ID / title:** SC-04 (replaces the earlier DK-92).
2. **Spec:** S-1.6 "Every Kraken result is labelled **exploratory (previously observed window)** ... and is computed only at the `W_HOLD` opening (DEC-17)"; S-13.3 "5. ML\* Binance vs the same arm on Kraken (exploratory, S-1.6)"; L11 "the Kraken outcome guard".
3. **Code now:** no Kraken path in `luna/v7`; `__init__.py` L11 row says "the Kraken outcome guard is not implemented" (luna/v7/__init__.py:160-161); comparison #5 is passed through only if supplied (AMB-27(e)).
4. **Why it needs a decision:** the comparison is computed at the opening. If it is not in the frozen tag, it would be new code after `v7-code-frozen`, which DEC-19 forbids for the holdout run. Without it, comparison #5 is permanently "unavailable" (DK-83 keeps m = 6, so no other result changes).
5. **Affects:** S (one Holm family member; never S-13.2 / S-13.4).
6. **Recommendation:** the user decides. The red-team's position is to ship it before freeze. Deferral is acceptable only with an explicit written decision that the Kraken path ships later under its own review, separately pinned, and never changes the primary verdict.
7. **Alternatives:** (a) implement before freeze; (b) defer under its own review; (c) drop it (reported permanently unavailable).
8. **Classification:** NORMATIVE AMENDMENT REQUIRED (user scope decision; implementing it before freeze needs no decision).
9. **Blocker:** yes.

### SC-05 — M4: DEC-19 preconditions in `authorize_holdout`; marker / pin re-check in `check_outcome_window`
1. **ID / title:** SC-05.
2. **Spec:** DEC-19 "only after code freeze + all leakage / canary checks passed + the S-10.7 development gate passed + pinned manifest / guards ready + the explicit user approval phrase; one opening only"; L14.
3. **Code now:**
   - `preflight` / `authorize_holdout` check tags, a clean tree and the phrase, but not S-10.7, the canaries or a pinned manifest (luna/v7/guard.py:202-260).
   - `check_outcome_window` only tests `isinstance(auth, HoldoutAuthorization)` (guard.py:89-108).
   - `_secret` is public (guard.py:51-55).
   - Probe: a forged authorization allows a W_HOLD outcome window while `holdout_opened()` is False.
4. **Why it matters:** the single opening can be consumed before the gates have passed, or W_HOLD outcomes can be computed with no marker at all.
5. **Affects:** V, G (seal).
6. **Recommendation:** before freeze:
   - `authorize_holdout` requires a hashed DevSelection record (valid choice, empty S-10.7 kill, LeakageReport passed);
   - `check_outcome_window` requires `holdout_opened()` and `pinned_manifest()` for W_HOLD windows;
   - the sentinel becomes module-private.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED (implementation work before freeze).
9. **Blocker:** yes.

### SC-06 — M5: one canonical R_V6 / L7 source (forward store, `forward.l7`, `windows.m6`) and the `partition_m6_keys` span check
1. **ID / title:** SC-06 (includes the unlogged R_V6 model contradiction).
2. **Spec:** S-1.2 (R_V6 requires a completed and reported V6 look or a written release); S-1.7 M3-3 / DEC-45 ("researcher-facing tooling can't read the store before L7"); N2 ("Keys whose period is in M6 are dropped in memory before anything is stored"); §15 L7.
3. **Code now:**
   - `ForwardStore.read` / `iter_records` gate on caller-supplied `now_ns` / `l7_ns` (luna/v7/forward_store.py:35-37, :64-78).
   - `forward.l7(t_f7, r_v6)` takes R_V6 as a free input (luna/v7/forward.py:96-117).
   - `windows.m6` always returns end None (luna/v7/windows.py:80-82), so the engine guard (engine.py:861) refuses every W_FWD7 replay. The forward replay is therefore structurally unrunnable while the store can be unsealed by argument. That is the contradiction.
   - `partition_m6_keys` / `drop_m6_keys` trust a caller span (luna/v7/netguard.py:202-223).
   - Probe: a hand-built span keeps an M6 key.
4. **Why it matters:** seal breaches through one wrong argument; two inconsistent models of R_V6.
5. **Affects:** V (M6 seal; forward stage).
6. **Recommendation:** before freeze:
   - derive R_V6 / L7 from one canonical, hash-checked release record shared by `windows.m6`, `forward.l7` and the forward store, sealed regardless of arguments until that record exists;
   - `partition_m6_keys` compares its span with `windows.m6_cached()` and refuses on mismatch, as `check_outcome_window` does.
7. **Alternatives:** —
8. **Classification:** CODE BUG — NO USER DECISION REQUIRED. The literal reading is clear: S-1.2 defines R_V6 by events, not by a caller value.
9. **Blocker:** yes.

---

## Considered and excluded (purely mechanical, or no outcome effect)

| Item | Reason for exclusion |
|---|---|
| AMB-01 transport ID extraction (forward recorder) | outcome-free recorder plumbing |
| AMB-08 F14 ratio units (bps vs fraction) | pure rescale; invariant under standardization and tree splits |
| AMB-12(f) canonical ordering / duplicate keys | determinism only |
| AMB-16(k) float tolerance EPS = 1e-12 | float noise only; exact-boundary cases follow the spec inequality |
| AMB-19(b) INVALID RUN strings | wording mapped to the exact S-5.4 / S-10.6 texts |
| AMB-21(b) episode walk mechanics | exact by S-8.3; fail-closed checks only |
| AMB-22(e) W_DEV K10 failure wording | text only; the INVALID outcome is fixed by K10 |
| AMB-23(l) S-14.1 labels | exact wording |
| AMB-25(c)/(d)/(e) candidate set, label inputs, identity multiplier | direct spec readings, no alternative with a different outcome |
| AMB-27(c) DT4 contents | provenance only |
| Deferred minors on perf / docs / tests (ledger Tasks 1-19) | no behavioural effect (the behavioural ones are DK-39, DK-77, DK-78, DK-97 note, DK-106, DK-110 to DK-114, SC-01) |

## Resolved by user (round 1, 2026-10-15)

| Item | User decision | Status in code |
|---|---|---|
| AMB-16(d) one side empty | Flatten the entire ML book at the next executable opportunity; minimum hold does not block; no replacement; opposite side never carried alone; S-5.4 governs untradable legs | Implemented (Task 19c); details for confirmation in DK-76 |
| AMB-16(e) target caps vs marked book | Caps bind targets; marked exposures subject to hard limits; drift rule stays reachable | Implemented per R35; R35 for confirmation in DK-51 |
| AMB-20 missing terminal-unwind bar | INVALID RUN / INVALID EVALUATION PATH; no stale, interpolated or synthetic price | Implemented (no change needed) |
| AMB-21(b) missing BTC reference price | Invalidates only when required (hedge construction, hedge marking, canonical §8 B(e)); fail closed; propagation per S-13.4 | Implemented (Task 19c); B(e)-at-fill gap-rule question in DK-10 |
| AMB-28 one-sided timing | "Next executable opportunity" (option ii, tick-level, S-4.2) | Implemented (Task 19c); details in DK-76 |
| Task 19b research runner scope | Pure deterministic orchestrator, synthetic fixtures only, fail closed | Implemented |
| Superseded rulings | R22 (superseded by R24, then by the AMB-16(e) decision), R23, R24, R26, R28 (pre-run blockers resolved by the decisions above) | — |
