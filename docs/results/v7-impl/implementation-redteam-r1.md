# V7 implementation red-team, round 1 (whole branch `feat/v7-ml-implementation`, 29959fc..2dc7344)

Reviewer: independent implementation red-team (read-only; no repo edits; scratch probes only under the session scratchpad).
Authorities: frozen spec (tag `v7-ml-spec-frozen`), `docs/results/v7-impl/CLARIFICATIONS-2026-10-15-round1.md`, `docs/results/v7-impl/AMBIGUITIES.md` (AMB-01..AMB-28), `deferred-and-rulings.md`.
Scope read in full: guard, netguard, windows, forward_store, labels, refs, market, spread, beta, instances, intervals, notices (U6 core), universe, features, cv, models, arms, book/{constructor, risk, hedge, execution, engine, ledger}, accounting, stats, selection, verdict, runner, runner_select; deploy / forward skimmed.

Probes run (scratchpad `redteam/`): `probe_seal.py` (findings M4, M5, m4), `probe_trim.py` and `probe_provider.py` (finding M1). Each probe's output is quoted with the finding it supports.

## Summary counts

| Severity | Count |
|---|---|
| Critical | 0 |
| Major | 8 |
| Minor | 11 |

I found no path where a value with `known_at > t − 1 min`, a later instance, a future label, `T_cal` / test data or M6 data reaches a decision, feature, fit, selection or calibrator, provided the inputs come through the existing ingest functions. Labels (S-5.3–S-5.6), the fold and purge mechanics (S-10.3/S-10.4), the §8 attribution (PR event sum, mkt_c, K10, S-8.4), the S-13.1 bootstrap, IUT, LB, Holm, the S-10.6 choice and the S-13.4 precedence all match the frozen text and the user decisions. The Majors fall into three groups:
- the code cannot run W_DEV at real scale on this host (M1);
- pipeline pieces the spec makes mandatory are missing, so they would have to be written after the freeze (M2, M3);
- several seal guards can be passed by a caller argument (M4, M5).

---

## Findings

### M1 — Major — Real-scale feasibility: a W_DEV run cannot run on this host (memory and time)

- **Where:**
  - `luna/v7/runner.py:122-158`: `ResearchUniverse` holds every symbol's full R5/R3/R4/funding frames in memory at once.
  - `luna/v7/runner.py:212-213, 359-361`: the provider is an LRU of 2048 decisions.
  - `luna/v7/universe.py:343-346`: `pit_rows(sd.bars1m, …)` filters the whole 1m history of every symbol at every decision.
  - `luna/v7/data/notices.py:157-164`: the U6 72 h test samples `halted()` on a 1-minute grid (4,321 calls), each over the whole PIT 1m frame.
  - `luna/v7/labels.py:186-187` → `luna/v7/refs.py:64-74`: `restrict` / `trim` filters the whole 1m and 15m frames of the leg and of BTC for every label.
  - `luna/v7/market.py:110-114`: `_open_at` scans the full frame for every reference price.
  - `luna/v7/labels.py:131-134`: β̂ rebuilds the hourly series from the full instance history for every label.
- **Spec:** §17 (one reproducible run), S-10.6 (nine required books plus the E_M runs), S-10.4 (label panels for three horizons from D0).
- **Measured** (synthetic fixture with 12 symbols, 38k 15m rows and 27k 1m rows per symbol):
  - `trim` / `pit_rows` cost about 25 ms per 1M rows.
  - `DecisionProvider(t)` costs 0.78 s per decision for 11 symbols.
  - `leg_label` costs 42 ms per label.
- **Estimate at real scale** (about 300–450 symbols, 1m history of about 1–2.3M rows per symbol in W_DEV):
  - **Memory:** the 1m bars alone, with the 5 required columns at 40 B per row, need about 20–30 GB. That exceeds the 14 GB shared RAM before any 15m, R4, aggTrades or engine copies.
  - **Provider:** about 90 s per decision. The book window is about 2,700 decisions and the cache holds 2,048, so a sequential replay never hits the LRU (deferred 19b minor). Each of the 11 book runs therefore recomputes every view, which is roughly 70 h per run.
  - **Label panels:** about 4,900 decisions × 3 horizons, plus about 1M labels per horizon at about 0.2–0.3 s each.
  - **Total:** months of compute. A symbol that keeps trading while a notice is in force (OMG-like) adds about 200 s per gate call through the 72 h grid.
- **Scenario:** the first W_DEV run either fails with an out-of-memory error, or runs for weeks and is killed. Either way, freezing the code now would force post-freeze performance rewrites of frozen modules.
- **Fix (before freeze):**
  - Store per-symbol arrays as sorted numpy (or memory-mapped / parquet, loaded lazily per symbol and window).
  - Replace every full-frame boolean mask and `==` scan with `searchsorted` slices.
  - Build β̂ / hourly series and `InstView`s once per instance.
  - Evaluate the 72 h test once per (version, scheduled time) on an indexed 1m array.
  - Cache decision views to disk, or compute all arms in one pass over the decisions.
  - Add a memory / time budget test at representative size.

### M2 — Major — The verdict cannot detect leakage: L9 canaries and K7 are not implemented, and leakage / integrity findings are caller-supplied with "empty = checked"

- **Where:**
  - `luna/v7/verdict.py:286-288, 327-330`: `leakage` and `integrity` are free caller tuples.
  - `luna/v7/__init__.py:158` (L9 "doc: … machinery: tests/v7/test_cv.py") and `:184-185` (K7 "no automated check").
  - No code computes LightGBM gain shares, standardized EN weight shares, the permuted-label canary (L9a) or the shifted-label canary (L9b).
- **Spec:** L9 (both canaries before the opening), K7 (mandatory leakage audit), DEC-19 ("all leakage / canary checks passed"), S-13.4 "NO-GO: leakage", K1.
- **Scenario:** a leaking feature passes into W_DEV. Nothing in the frozen code flags it. The operator passes `leakage=()` (the only value the code can produce) and the verdict can read `V7 HOLDOUT ADVANCE`. The code is fail-open by construction on the most severe NO-GO class.
- **Fix:** before freeze, implement:
  - `canary_permuted_ic` (L9a);
  - `canary_shifted_label` (L9b: L1 rejection plus a sandbox IC jump);
  - `k7_concentration` over every fold model;
  - a `LeakageReport` object that `holdout_inputs` requires (its hash recorded in DT4) instead of a free tuple.

### M3 — Major — Mandatory pipeline pieces missing: no fetch layer or read-only loaders, no post-opening W_HOLD path, no W_HOLD read guard on features, no Kraken arm

- **Where:**
  - `luna/v7/netguard.py:129-199`: `check` is pure, and nothing in `luna/v7` calls it.
  - `luna/v7/guard.py:111-125`: `check_listing_only` is never called.
  - `luna/v7/netguard.py:163-164, 195`: N1 refuses W_HOLD content forever, and the N5/N6 bound is hard-coded at `20250630235959`. E6 (bound raised to `20260630235959` at the opening) is not implemented.
  - `luna/v7/runner.py:335-362`: `DecisionProvider` and features compute at any t, including W_HOLD, with no guard.
  - Kraken is absent from the whole module set: S-1.6, S-2.4, S-13.3 #5, and L11's "Kraken outcome guard" (`__init__.py` L11 row says "not implemented").
- **Spec:**
  - S-1.7: "every V7 research request goes through one fetch layer"; "read-only loaders"; "no W_HOLD features or labels" before the opening.
  - E6, K9 ("guard refusal; logged; incident report").
  - S-1.6 / DEC-17 (Kraken at the opening), S-13.3 (family of exactly 6), L11.
- **Scenarios:**
  - **(a)** At the legitimate opening, W_HOLD files and the E6 captures cannot be fetched through frozen code. New code would be written after `v7-code-frozen`, and DEC-19 requires HEAD == the code tag.
  - **(b)** Before the opening, a W_HOLD file present on disk can be loaded and featurized with no guard refusal and no K9 log.
  - **(c)** Comparison #5 is permanently "unavailable" (AMB-22(c) keeps it as a non-rejection). That is reported, but the spec names it.
- **Fix:** before freeze, add:
  - a single `fetch(req)` that calls `netguard.check` before I/O and appends to a K9 refusal log;
  - loaders that call `check_listing_only` and `check_outcome_window` on the data span;
  - an `authorize`-gated E6 / N1 mode for the opening;
  - a W_HOLD guard in `DecisionProvider.__call__`;
  - the Kraken exploratory path, or a recorded user decision that it ships later under its own review.

### M4 — Major — Holdout opening does not enforce DEC-19 preconditions, and the outcome guard accepts any `HoldoutAuthorization` object

- **Where:**
  - `luna/v7/guard.py:202-226` (`preflight`) and `:229-260` (`authorize_holdout`) check the tags, a clean tree and the phrase. They do not check:
    - that S-10.7 passed;
    - that the canary / leakage checks passed;
    - that a pinned manifest exists.
  - `luna/v7/guard.py:89-108`: `check_outcome_window` only tests `isinstance(auth, HoldoutAuthorization)`. It never re-checks `holdout_opened()` or `pinned_manifest()`.
  - `luna/v7/guard.py:51-55`: `_secret` is a public class attribute.
- **Spec:** DEC-19 (the opening only after code freeze + all leakage / canary checks passed + the S-10.7 gate passed + a pinned manifest + the phrase), L14, §17.
- **Probe** (`probe_seal.py`): `HoldoutAuthorization(HoldoutAuthorization._secret, {})` lets `check_outcome_window` accept a W_HOLD window while `holdout_opened()` is False. Output: `forged auth: W_HOLD outcome window ALLOWED; markers opened? False`.
- **Scenarios:**
  - **(a)** The user gives the phrase after a W_DEV kill, or before the canaries ran. The single opening is consumed and W_HOLD outcomes are computed. Only `verdict.holdout` / `holdout_inputs` refuse later (`runner_select.py:155`, `verdict.py:327`), after the outcomes exist.
  - **(b)** A helper constructs an authorization object directly (tests do something similar). W_HOLD labels and books are then computed with no marker and no record of an opening.
- **Fix:** `authorize_holdout` should require a hashed `DevSelection` record (valid choice, empty kill, `LeakageReport` passed) and refuse otherwise. `check_outcome_window` should require `holdout_opened()` and `pinned_manifest()` for W_HOLD windows. Replace the public sentinel with a module-private one.

### M5 — Major — Seal guards keyed on caller-supplied spans or dates: forward store (L7), `netguard.partition_m6_keys` (M6), and an R_V6 / L7 model that contradicts `windows.m6`

- **Where:**
  - `luna/v7/forward_store.py:35-37, 64-78`: `read` / `iter_records` take `now_ns` and `l7_ns` from the caller.
  - `luna/v7/forward.py:96-117`: `l7(t_f7, r_v6)` takes R_V6 as a free input.
  - `luna/v7/netguard.py:202-223`: `partition_m6_keys` / `drop_m6_keys` trust the caller's `m6_span` (deferred Task-10 minor).
  - `luna/v7/windows.py:80-82`: the canonical M6 end is always None, so the engine guard (`engine.py:861`) refuses every W_FWD7 replay forever.
- **Spec:** S-1.7 M3-3 / DEC-45 ("researcher-facing tooling can't read the store before L7 (guard test)"); N2 ("keys whose period is in M6 are dropped in memory before anything is stored"); S-1.2 (R_V6 needs a completed and reported V6 look or a written release); §15 L7.
- **Probe** (`probe_seal.py`):
  - `ForwardStore.read(sha, now_ns=X, l7_ns=X)` returns the raw announcement bytes before any real L7.
  - `drop_m6_keys([...2026-08.zip], (m6_start, m6_start+1))` keeps the M6 key.
  - With the canonical span (`windows.m6_cached()`), the same key is dropped.
- **Scenario:** a researcher tool calls `forward.l7(T_F7, r_v6=<guess>)` (the only way to obtain an L7) and reads semantic CMS content during M6. Or an N2 listing helper passes a hand-built span and stores M6 key names. Both are seal breaches through one wrong argument. The `check_outcome_window` cross-check exists precisely to prevent this, but these two paths skip it.
- **Fix:**
  - Derive L7 / R_V6 only from a canonical, hash-checked release record (one source shared by `windows.m6` and `forward.l7`). Until that record exists, the forward store must be sealed regardless of arguments.
  - `partition_m6_keys` should compare its span with `windows.m6_cached()` and refuse on mismatch, as `check_outcome_window` does.

### M6 — Major — AMB-05 (U6 context and phrase rules) is classified "implementation clarification" but decides whether held legs exit before B/C cessation

- **Where:** `luna/v7/data/notices.py:28-34, 65-73` (`USDM_PHRASES`, `USDM_PAIR`, notice-level `COIN_M_PHRASES` veto) and `:89-120` (`scheduled_ts` sentence rules).
- **Spec:** S-2.5 / DEC-40 (confident match); S-7.3 (U6 exit); S-5.4 / DEC-43, S-10.6, K11 (a held B/C event makes the whole W_DEV selection `INVALID RUN`).
- **Scenario:** a real delisting notice names "USDⓈ-M XXXUSDT" and, in the same notice (or in boilerplate), a COIN-M product. `_usdm_context` returns False, so U6 never activates, the leg is held into cessation, and the result is a C event and `INVALID RUN — historical terminal execution not reconstructible` for all 9 runs. Stage 0 reports 14 B and 12 C eligible terminations in W_DEV. U6 is the main mechanism that keeps the book away from them.
- **Fix:** reclassify AMB-05 as "normative amendment required" and put it on the decision docket. Before freeze, validate the matcher against the Stage 0 notice set (`docs/results/v7-stage0/m31/notices.json`, `delisting_coverage.csv`) and report each notice's match / no-match with its reason. This outcome-free check needs no labels.

### M7 — Major (unlogged divergence) — T_fit drops T_tr rows that were purged only against the T_sel start

- **Where:** `luna/v7/cv.py:88-93` (the `tr` rows need `label_known_at <= sel_start − 24 h`) and `:337` (`fit_df = tr ∪ sel` reuses those purged indices).
- **Spec:** S-10.4 step 1, "refit on T_fit = T_tr ∪ T_sel", with the S-10.3 purge relative to the next block (`T_cal`). The deferred Tasks 11-12 minor says "undocumented". There is no AMB entry.
- **Scenario:** every fold and the final model lose about (h + 24 h) of training decisions just before C − 12 mo (for h* = 168 h, about 8 days, roughly 2–4 % of T_fit). The base-model weights, and therefore the rankings, trades, IC and LB, differ from the literal reading. The effect is small but changes outcomes.
- **Fix:** either build `T_fit` rows as `ts in [D0, C − 6 mo)` with `label_known_at <= cal_start − 24 h`, or log an AMB entry ("normative" by the brief's test) and put it on the docket.

### M8 — Major (unlogged divergence, secondary arm) — The A1 veto also acts as an exit trigger

- **Where:** `luna/v7/arms.py:154-159` (the veto is folded into `passes_hurdle`) and `luna/v7/book/constructor.py:203-204` (after h*, `not passes_hurdle` gives a "hurdle" exit).
- **Spec:** S-7.2 A1 = "A0's entries, minus those with calibrated P̂ < 0.50 … Ranking: A0 order". The A1 hurdle is A0's (`s_rule > 0`). S-0.6 lets ML remove *candidates*. AMB-13(c) covers entries only.
- **Scenario:** a held A1 leg whose P̂ drifts below 0.50 after h* exits although `s_rule > 0`. A1's trades and costs change, and with them S-13.3 comparison #1 (the A1 claim).
- **Fix:** keep a separate `veto` flag and apply it only to entries, or log an AMB entry and send it to the docket.

---

### Minor findings

| ID | Where | Spec | Scenario | Fix |
|---|---|---|---|---|
| m1 | `luna/v7/timeutil.py:63-64` (parked Task 7) | L1, L12 | **Probe:** `ns_array([1_700_000_000_000])` (ms) is accepted. A ms `known_at` from any ingest path that skips `to_ns_checked` always passes `known_at <= t − 1 min`, so future rows leak. This is latent today: no real loader exists yet. | Apply the window check to integer inputs too. |
| m2 | `luna/v7/timeutil.py:22-33` (`to_ns` float path) | L12 | `to_ns(1.6e12)` becomes 1970 ns with no ambiguity error. It is used for `recv_ts`, `r_v6` and similar inputs. | Refuse floats, or apply the float window. |
| m3 | `luna/v7/constants.py:157-195, 210` (Task 1 deferred) | §17, "frozen numbers" | `FROZEN.accept.p6["spread"] = 1.0` at runtime silently changes P6 for the whole process. | Use `MappingProxyType` or tuples. |
| m4 | `luna/v7/guard.py:51` (Task 2 deferred) | L14 | Covered by M4. Listed separately for the triage. | — |
| m5 | `luna/v7/data/instances.py:56-64` | L1, DEC-44 | The new instance becomes "current" at its first bar's *open*, although volume > 0 is known only at the bar close (`open + 15 min`). Effect: 15 minutes of look-ahead on the instance switch. It is harmless today because U1 fails for 60 days. | Compare with `first_bar_ts + 15 min`, or log an AMB entry. |
| m6 | `luna/v7/book/constructor.py:319-337`, `hedge.py:57-58` | S-7.3 "Gross ≤ 1.0·E" | The hedge is excluded from gross with no AMB entry. A legs gross of 1.0 E plus a hedge of up to 0.5 E gives 1.5 E. | Log the reading (the natural one, since `|H_B| ≤ 0.5 E` is capped separately). |
| m7 | `luna/v7/book/engine.py:273-282` (`legs()`); Task 14b deferred `maker_exit` | S-6.2 | Under E_M, a leg whose maker remainder went to a halted `Pending` wait is still shown as active. The risk layer and the constructor can re-issue exits or start a cooldown, which changes P1(E_M) and therefore the maker-dependent vs no-viable-edge split. | Narrow `maker_exit` to a resting limit order only. |
| m8 | `luna/v7/selection.py:98`, `luna/v7/verdict.py:416` | S-13.4, item 4 of the brief | `account()` raises a `ValueError` (missing tick, not flat, missing B) on a *required* run. The result is an exception instead of the `INVALID RUN — K10 / accounting integrity failure` verdict. | Catch these errors and map them to `INVALID_K10` with the detail. |
| m9 | `luna/v7/verdict.py:195-200` (Task 16 deferred) | P3 | An empty decisions frame has no `killed` column, so `holdout` raises instead of returning a class. | Use `_frame(rows, cols)` with the schema columns. |
| m10 | `luna/v7/runrecord.py` (Task 19b deferred) | §17, DT4 | The `research` flag is a caller convention, so a research run can be recorded from a dirty tree. | Make `run_book` / `dev_selection` / `holdout_inputs` always record with `research=True`. |
| m11 | AMB-06 / `tests/v7/ar_reference.py` | S-4.5 required tests | The check of the transcription against R / CRAN `bidask` 2.1.5 output is still "pending before code freeze". | Run it once on a machine with R, commit the vectors, and close the item. |

---

## Deferred-minor triage

**Must fix (or log) before freeze:**

| Deferred item | Reason |
|---|---|
| T1 constants dicts mutable (m3) | Frozen economic numbers can be mutated at runtime. The fix is one line. |
| T1 `to_ns` accepts floats (m2) | L12 unit guard. Cheap. |
| T7 parked: `ns_array` integer window (m1) | Latent L1 leak. The real loaders (M3) are still to be written. |
| T2 `HoldoutAuthorization._secret` public; no marker re-check (m4 / M4) | Seal (L14). |
| T10 `partition_m6_keys` caller span (M5) | Seal (N2). |
| T11-12 purged `tr` rows excluded from T_fit (M7) | Unlogged divergence that changes the fits. |
| T14b `maker_exit` too broad (m7) | Changes P1(E_M), which feeds the S-13.4 class split. |
| T14b `cap_gross_entry` can leave the neutrality band | Primary book. Under the R35 reading, a proportional entry / `risk_gross` scale preserves an existing L/S imbalance. AMB-16(e) logs only the halted case, so log the general case in AMB-16(e) for the docket. |
| T16 empty decisions → missing `killed` (m9) | A verdict path that raises. |
| T19b LRU never hit on sequential replay | Part of M1 (feasibility). |
| T19b run-record `research` flag is a convention (m10) | §17 provenance. |
| T8 / T9 perf (`U6State` rebuilt per call, `gates` re-annotation, ĥ computed 22× per call) | Part of M1. |

**Can wait** (no effect on outcomes, validity or the seal; fix in the final wave or at the forward-recorder review):
- T1: `overlaps` returns `np.bool_`; `spec._git` has no timeout and its branches are untested.
- T2: Memento tz handling, forward store sha check / fsync / lock, `extract_transport_id` silent `[]`, regex isolation tests, directory key without a trailing slash. These belong to the §15 forward-recorder review, which happens before T_F7.
- T3: missing-symbol `KeyError`, NaN false conflict (fail-closed), docstring and column naming.
- T4-5: empty multi-symbol annotate; retrospective-membership caveat wording.
- T6: notice-level context and closure-keyed cache. Covered by M6's docket item. The cache itself is per call and safe.
- T9: missing F12 non-eligible test; winsor-collapse test; AMB-08/09 wording.
- T11-12: selection test does not recompute IC; `Config.family` str; `uv.lock` churn; canonical pairing not enforced in `select_hparams` (`_build` always canonicalizes).
- T13: tautological test, shadowed parameter, stale AMB-14 text.
- T14b: `order_cancelled_at_unwind` timing, stale AMB text, theoretical exit-token over-fill.
- T16: K8 rejects `np.float32` (fails closed).
- T17-18: D1 optimism and `d1_share_scaled` details. These are deployment-only; log them in AMB-23.
- T19: `ml_hurdle` units, literals.
- T19b: classifier family not checked in `holdout_inputs`; the untracked `OVERNIGHT_PROGRESS.md` (an operational issue).
- T19c: conditional assertion; in-flight hedge token; the 14-minute asymmetry (already logged as AMB-28 c'').

---

## Unlogged divergences

1. **M7:** T_fit excludes `tr` rows purged against the `sel` start (`cv.py:337`).
2. **M8:** the A1 veto acts as an exit trigger (`arms.py:154-159`, `constructor.py:203-204`).
3. **m6:** the hedge is excluded from the S-7.3 gross ≤ 1.0 E limit (`constructor.py:319-337`).
4. **m5:** the current instance switches at the first bar's open, not when that bar's volume is known (`instances.py:56-64`).
5. **R_V6 model inconsistency:** `windows.m6` never lets R_V6 lift, while `forward.l7` accepts a free R_V6. The forward replay is structurally unrunnable and the forward store can be unsealed by argument (M5). AMB-24(d) logs R_V6 as an input but not the contradiction with the canonical M6.
6. **Missing mandatory items treated as "doc / review" rows in `SPEC_COVERAGE`:** L9 canaries, K7, the Kraken path / L11 guard, and the S-1.7 fetch layer / read-only loaders / K9 logging (M2, M3). These are spec requirements with no AMB or decision covering their absence.

---

## Misclassified AMBs

Each of these is labelled "implementation clarification", but the choice can change labels, rankings, trades, P&L, validity or GO / NO-GO. Each should go on the decision docket.

| AMB | Why it is outcome-changing |
|---|---|
| **AMB-05** (U6 phrase list, notice-level COIN-M veto, `scheduled_ts` sentence rules) | It decides U6 activation and the 72 h clear, and therefore exits and B/C exposure, and therefore W_DEV validity (M6). |
| **AMB-10(a) / (i)** (data-gap price taken from a bar up to 60 min later, booked at the nominal τ; also used for book fills and B(e), AMB-21 docket line) | It sets fill prices and which settlements are included. That changes labels and P&L. |
| **AMB-07 U2 empty day = 0 and U7 record choice** | Already "flagged for review", but it alters the universe and therefore D0, eligibility and trades. |
| **AMB-12(a)** "decision date" = UTC day pooling the three decisions | It changes the S-10.5 selection metric (IC weights) and the S-10.4 calibration weights, and therefore the hyperparameters, hurdle and P8. |
| **AMB-16(f)** "reduce-only between entries" read as never increase | The alternative (increases allowed at entry decisions) changes the book's exposure. |
| **AMB-16(g)** minimum-hold clock measured from the decision time | Measuring from the fill shifts every exit by one decision. |
| **AMB-17(d)** cooldown per symbol, blocking both sides | It changes entries. |
| **AMB-21(a)** N_a per symbol | P5 is a gate. It is conservative, but it can move GO / NO-GO. |
| **AMB-21(d)** P6 / P10 / S-10.7 use the mean daily net, not the mean daily return | It is self-labelled "borderline". S-10.7 is a kill and P6 / P10 are gates. |
| **AMB-22(h)** P4 gross excludes the hedge; closed legs = position ids | P4 decides INCONCLUSIVE. |
| **AMB-22(k)** P7 bootstrap over non-contiguous IC dates; P3 includes the post-unwind equity | P7 and P3 are gates. |
| **AMB-08** feature formulas, in particular F6 sampling, F8 Amihud, F9 without demeaning, and F2 / F4 bridging over invalid records | These are model inputs that change rankings. The spec leaves formulas open, so a single docket line that ratifies the AMB-08 table as a whole is enough. |
| **AMB-24(e)** forward active days not restricted to evaluable days | It changes INCONCLUSIVE on the forward run. |

Correctly classified (normative, still open): AMB-02, 09, 10(b), 10(g), 12(a2), 12(b), 16(e) (R35 confirmation and E_M `risk_gross`), 16(l), 17(c), 18(b), 18(c), 19(a), 19(d), 19(e) (leg stale marks), 19(g), 22(g), 23(d), 23(e), 23(i), 23(j), 24(f), 24(k), 25(b), 27(b), 28(a').

---

## Overall assessment

The research logic is careful and close to the frozen text:
- point-in-time discipline, instance separation and fail-closed price handling;
- the label algebra, purge and three-block calibration;
- the §8 attribution with K10;
- the bootstrap, selection and verdict precedence;
- the four user decisions (AMB-16(d), AMB-16(e) / R35, AMB-20, AMB-21(b)), all implemented as written.

I did not find a leak or a wrong-verdict bug in the logic as written.

**The code is not yet acceptable for a code-freeze review, even once the normative docket is decided.** Three things block the freeze independently of the docket:
1. **Feasibility (M1):** the frozen modules cannot run W_DEV on this host. Fixing that after the freeze means rewriting frozen code.
2. **Missing mandatory machinery (M2, M3):** the L9 canaries, the K7 audit, a structured leakage report, the S-1.7 fetch layer and read-only loaders with K9 logging, the post-opening W_HOLD / E6 path, a W_HOLD guard on features, and the Kraken path (or an explicit user deferral). These must exist and be reviewed before `v7-code-frozen`, because DEC-19 pins the holdout to HEAD == the code tag.
3. **Seal hardening (M4, M5):** enforce the DEC-19 preconditions in `authorize_holdout`, make the outcome guard re-check the markers and the pin, and derive every M6 / L7 / R_V6 decision from one canonical source.

In addition:
- log or fix the unlogged divergences (M7, M8, m5, m6);
- add the misclassified AMBs, AMB-05 above all, to the docket;
- clear the must-fix deferred minors.

After those changes and the docket decisions, a second red-team round focused on the new loader / fetch / seal code and a real-scale dry run (outcome-free: features and gates only on W_DEV) should be enough for a freeze recommendation.
