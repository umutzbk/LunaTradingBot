# V7 implementation red-team, round 2 (branch `feat/v7-ml-implementation`, c5cd954..d77adda)

Reviewer: independent implementation red-team, round 2. Read-only on the worktree, which stayed unchanged: HEAD `d77adda`, and `git status` shows only the two pre-existing untracked files. I did not read `var/`, used no network and ran no full suite. Probes live under the scratchpad `redteam2/`.

**Authorities, in priority order:**
1. the frozen spec (tag `v7-ml-spec-frozen`);
2. CLARIFICATIONS round 1 and round 2, with the AMB-40 / AMB-29(a) / R46 / R38 / R45 addenda;
3. DECISION-DOCKET, including the round-2 status table;
4. AMBIGUITIES AMB-01..43;
5. the controller rulings R1–R54 (`progress.md`);
6. red-team round 1;
7. the C4 and CRAN reports.

**Read in full:**
- `guard`, `release`, `windows`, `forward_store`, `opening`, `leakage`, `fetch`, `netguard`, `localdata`, `forward_model`;
- `runner` (universe, provider, `label_panel`, decision pass, holdout panel registry);
- `runner_select` (`holdout_inputs`, `holdout_verdict`);
- `viewstore`, `runstore` (load path), `timeutil`, `pitindex`;
- `ingest` (driver and ledger), `captures` (R50–R52 kind / release logic);
- the AMB-29(a) commit d78a1c1.

**Skimmed:** `kraken`, `rawstore` (docstring, loaders, `StoreSource`), `metaingest`, `verdict` (inputs / W_HOLD binding), `engine` (DK-77 / DK-78 / DK-69 / DK-70 sites), `constants`, `arms`, `instances`, `spec`, `scripts/v7/dryrun_ingest.py`, `scripts/v7/bench_decision_pass.py`.

**Probes** (scratchpad `redteam2/`):

| Probe | What it tests | Supports |
|---|---|---|
| `probe_seal2.py` P1 | a forged authorization built from the closure key | M4 closure |
| `probe_seal2.py` P2 | a caller repository unseals the forward store | finding MJ5 |
| `probe_gap_instance.py` | a ledgered ingest gap splits a contract instance | finding MJ2 |
| `probe_hash.py` | cross-process stability of the pickle-based model identity | determinism note |

## Summary counts

| Severity | Count |
|---|---|
| Critical | 0 |
| Major | 5 |
| Minor | 6 |

**Seal: no breach found.** Nothing reaches W_HOLD outcomes, M6 data or W_FWD7 outcomes before authorization through the checked paths:
- The opening checks the DEC-19 preconditions, plus the resume path (R45), the verdict marker and the caches.
- The outcome guard, the data-span guard (local files, frames, decision times, label panels, engine), the fetch layer (netguard before I/O, N2 truncation, N6 memento, E6 / R46 bounds), the L11 Kraken guard, the forward refit record (R54) and run-store loading all hold.

**Probes on the seal:**
- A HoldoutAuthorization forged from the closure key is refused while no canonical opening exists (P1).
- The forward store can be unsealed only through its own caller-repository parameter, by a deliberately forged scratch repository (P2, MJ5). The engine, labels and data guards ignore any caller repository and stay sealed.

**Leakage machinery (SC-02) is sound and binding:**
- The verdict and the opening require a verified, passed, pinned LeakageReport.
- L9a / L9b / K7 match DK-26 / DK-27.

**What still blocks a freeze recommendation:**
1. The outcome side of W_DEV (labels, canaries, book replays) is still not shown feasible at real scale (MJ1).
2. Ingest failures silently become data holes (MJ2).
3. The single-shot W_HOLD verdict does not bind its inputs to the canonical window, grid, universe and model (MJ3).
4. DEC-19's "pinned manifest" has no definition, builder or consumer (MJ4).

In addition, the open normative items (AMB-42 above all) make a W_DEV `INVALID RUN — historical terminal execution not reconstructible` likely. See the open normative items below.

---

## Findings

### MJ1 — Major — Real-scale feasibility of the outcome side (labels, canaries, book replays) is unproven; the full-window universe still holds every symbol's 1-minute bars in memory (SC-01 partially open)

- **Where:**
  - `luna/v7/runner.py:337-346`: `ResearchUniverse.from_source` loads `source.load(s, lo, hi)` for every symbol, so every frame, `bars1m` included, sits in RAM.
  - `luna/v7/runner.py:897-955`: `label_panel` runs on that universe.
  - `luna/v7/runner_select.py:188-235`: `dev_selection`, the nine required runs plus the E_M runs, on the same universe.
  - `docs/results/v7-impl/AMBIGUITIES.md` AMB-38 "Not done": "labels at real scale still need the outcome-side 1m data resident (not run on real data in this phase)".
  - `scripts/v7/bench_decision_pass.py:1-17`: the benchmark measures the outcome-free decision pass and a "replay" that only reads stored views back. It runs no engine with 1-minute data and no label.
- **Spec:**
  - §17 (one reproducible run);
  - S-10.4 (label panels for three horizons from D0);
  - S-10.6 (the nine required books plus E_M);
  - L9 (two extra out-of-fold pipelines per horizon);
  - DEC-19 (HEAD == the code tag: no post-freeze rewrites).
- **Evidence:**
  - Measured: decision pass about 4.5 MB per symbol, extrapolated to about 2.5 GB and 7.5 h for 500 symbols.
  - Not measured: anything that needs R5 resident. W_DEV R5 from D0 is about 2.37 M minutes per symbol-life.
  - Estimate: about 1.4 M rows on average × 300–450 symbols × 5 required columns (`REQUIRED_COLS["bars1m"]`, `runner.py:112`) × 8 B ≈ 17–25 GB before pandas overhead. The shared RAM is 14 GB.
  - The label-panel count is about 4,900 decisions × gated candidates × 2 sides × 3 horizons. Its per-label cost at scale (after `refs.InstIndex`) has never been measured.
- **Scenario:** C6 phase B (features and gates) passes. The first real W_DEV research run then runs out of memory, or pages for days, in `label_panel` / `run_book`. The data-access fix lands in frozen modules after `v7-code-frozen`.
- **Fix (before freeze):**
  - Give the outcome side the same lazy, windowed access as the decision pass: per-symbol 1-minute windows loaded on demand around fills, labels and halts.
  - Process labels in chunks per decision block and release them.
  - Benchmark `label_panel` and an engine run with representative 1-minute volume, then add a budget test, as was done for the pass.
  - Alternatively, record a user decision that the outcome side ships under its own review before the tag.

### MJ2 — Major (fail-open, outcome-changing) — Ingest failures become silent month holes; nothing gates the research run on the ingest ledger, and a hole creates a phantom instance end

- **Where:**
  - `luna/v7/ingest.py:400-415, 475-481`: `ingest_months` / the funding driver ledger a per-chunk `IntegrityBlocker` or `FetchError` as `status: gap` in `INGEST_LEDGER.jsonl` and continue.
  - No consumer exists: `grep LEDGER` matches only `ingest.py`.
  - `luna/v7/rawstore.py` `StoreSource` / `TableLoader` treat a missing chunk as "no rows".
  - `luna/v7/metaingest.py:357-377` → `luna/v7/data/instances.py:32-53`: instances are derived from the stored R3 bars with the 7-day dormancy rule.
- **Spec:**
  - DEC-33 / S-2.2: "If a key exists in both with conflicting OHLCV values, the conflict is reported as a **data-integrity blocker**; neither value is chosen silently".
  - S-2.2 (activity-based instances, DEC-35);
  - S-5.4 (HALTED = no volume > 0 1-minute bar);
  - DEC-43 / K11.
- **Ruling:** R48 made per-chunk failures "ledgered and non-fatal — data handling only". That covers continuing the ingestion. It does not cover a research run consuming a store with ledgered holes, and the outcome consequences below are not data handling only.
- **Probe** (`probe_gap_instance.py`): one R3 month missing out of six.
  - Complete store: `[(2023-01-01, None)]`.
  - Store with one ledgered gap month: `[('2023-01-01', last_trade '2023-03-01'), ('2023-04-01', None)]`. The live contract is split into two instances.
- **Scenarios:**
  - **R3 hole.** A transient HTTP error (`FetchError`) or a DEC-33 monthly / daily conflict leaves one R3 month of a live symbol unstored.
    - The instance is split. The first part gets an instance end with a terminal event, class C with no notice. A held leg reaching it is `INVALID_TERMINAL`: a required run is INVALID and W_DEV stops before selection.
    - The second part restarts U1 (60 days ineligible).
  - **R5 hole.** The symbol is HALTED for a month: stressed waits, AMB-29(a) / DK-69 INVALID paths, and different exits.
  - The DEC-33 conflict, which the spec calls a blocker, ends as a silent "both dropped" plus a hole.
- **Fix:**
  - The research entry points (`StoreSource` / `ResearchUniverse.from_source` / `decision_pass`) should refuse a store whose table manifests do not cover the listed months of every symbol, or whose ingest ledger has an unresolved `gap` (a store-level completeness record written by the driver, hashed into the data identity).
  - A DEC-33 conflict stays a blocker until a recorded resolution exists.
  - Log the reading as an AMB / ruling.

### MJ3 — Major (fail-open binding at the single-look verdict) — `holdout_inputs` does not bind the W_HOLD verdict's inputs to the canonical W_HOLD window, decision grid, universe, quarters and pinned final model

- **Where:**
  - `luna/v7/runner_select.py:269-368`. Checked: arm / execution / h* of the four runs, `research`, the pinned LeakageReport / DevSelection / final-model hash.
  - Not checked:
    - that `ml_T` / `a0_T` / `ml_M` / `a0_M` have `window == W_HOLD`;
    - that `quarter_edges` are W_HOLD's calendar quarters (P5);
    - that ML* E_T / E_M were scored by the pinned final model (the runs' per-decision model identity is not compared with `pin["final_model_hash"]`);
    - that `panel` (P7 IC, P8 slope, K8 invalid rate, A1 ECE) was issued by `label_panel` under this opening over the full grid.
  - `luna/v7/runner.py:1008-1019`: `_note_holdout_universe` returns None, and the call continues, for a panel that was not issued.
  - `luna/v7/verdict.py:339-361, 478-497` only asks whether the inputs touch W_HOLD.
- **Spec:**
  - S-13.2 / S-13.4 (P1–P11 on `W_HOLD`; P5 per quarter; P7 / P8 over the holdout);
  - S-10.4 step 6 ("one frozen, hashed model ... applied unchanged to all of W_HOLD");
  - DEC-19 / §17 (single look).
- **Comparison:** ruling R54 round 3 required exactly this binding for the forward-refit panels (`forward_model.bound_samples`: window == W_HOLD, the full canonical grid, the `holdout_inputs` universe), but not for the verdict that matters more.
- **Scenario:**
  - A driver error passes, for example, a W_HOLD sub-window run (a resumed process after a crash), a panel over part of the grid, an ML* run scored with a fold model, or month-based quarter edges.
  - `holdout_verdict` computes P1–P11 on those inputs and writes the verdict marker.
  - The single look is consumed with a wrong verdict (ADVANCE or NO-GO) that cannot be redone.
- **Fix:** in `holdout_inputs`, require:
  - each required run's window == `W_HOLD`;
  - `quarter_edges` == the W_HOLD calendar quarters;
  - the ML* runs' recorded model identity == `pin["final_model_hash"]`;
  - the panel issued under this opening (`holdout_panel_coverage`) with h = h*, window = W_HOLD, coverage == `decision_times(*W_HOLD)`, and the same universe as the runs.

  Refuse otherwise, before anything is computed.

### MJ4 — Major (missing mandatory component) — DEC-19 "pinned manifest": no definition, no builder, no consumer; any 64-hex string passes

- **Where:**
  - `luna/v7/guard.py:639-641`: `preflight` only checks that `manifest_sha256` is 64 hex.
  - `guard.py:737-744`: it is pinned as given.
  - No function builds a W_HOLD file manifest. `grep "def .*manifest"` finds only the raw / meta store manifests.
  - Post-opening ingestion (`ingest.py`, `rawstore.put_chunk` with `auth`) never compares the W_HOLD files it fetches with the pinned manifest.
- **Spec:**
  - DEC-19: "pinned manifest / guards ready";
  - §17: "Holdout opening (DEC-19): token from an explicit approval phrase, exclusive markers, pinned manifest, read-only loaders";
  - S-1.7: listings of W_HOLD are readable before the opening.
- **Scenario:**
  - At the opening the operator must produce a W_HOLD manifest hash with code outside the tagged tree, or pass any string.
  - The W_HOLD files later ingested (Binance can republish or alter archive files) are not bound to what was pinned.
  - The "pinned manifest" condition is satisfied in form only. Adding the builder after the tag is impossible (HEAD == code tag).
- **Fix (before freeze):** a `w_hold_manifest(fetcher, symbols)` that builds the canonical listing of every W_HOLD N1 file of the frozen tables (N2 names / sizes / ETags plus the published `.CHECKSUM`), with sha256 = the value to pin. Post-opening N1 ingestion of a W_HOLD file must then match its pinned entry (else refused, K9-logged).

### MJ5 — Major (fail-open guard, forward stage) — `ForwardStore`, `forward.l7` / `require_l7` / `forward_evaluate`, `windows.m6` and `netguard.partition_m6_keys` still accept a caller repository; a forged scratch repository unseals the forward store (SC-06 partially open)

- **Where:**
  - `luna/v7/forward_store.py:40-61` (`ForwardStore(root, repo=...)`, `_gate(self._repo)`);
  - `luna/v7/forward.py:100-118, 327-333`;
  - `luna/v7/release.py:187-227` (`repo_key(repo)`);
  - `luna/v7/windows.py:315-331`;
  - `luna/v7/netguard.py:276-300`.
- **Contrast:** the opening (round 2, I-B) and `guard._outcome_window` resolve the repository only through `guard._roots()`.
- **Spec:**
  - S-1.7 M3-3 / DEC-45: "researcher-facing tooling can't read the store before L7 (guard test)";
  - §15 L7;
  - SC-06: "one hash-checked release record; refuse caller spans". The `release.py` docstring claims "No caller value can set R_V6, T_F7 or L7".
- **Probe** (`probe_seal2.py` P2): a scratch git repository holds the real V6 artefacts, a basis-(c) R_V6 record at 2026-07-01 and a review-pass record dated 2020, under annotated tags.
  - Output: `canonical L7: None | caller-repo L7: 1782950400000000000`.
  - `ForwardStore(repo=fake).read -> b'announcement body'`, while the canonical `ForwardStore(root).read` stays sealed.
  - With the same repository, `partition_m6_keys(repo=fake)` sees M6 = [2026-07-01, 2026-07-01) (empty), so M6 keys are kept.
- **Scenario:** a researcher tool, or a test helper reused in production, passes `repo=` to read the sealed announcement store or to evaluate a forward verdict before the canonical L7. The engine replay itself stays refused (canonical guard), so no forward outcome is computed. The breach is the raw-content seal and the N2 key filter.
- **Fix:** remove `repo` from every public function and constructor. Use `guard._roots()[0]` (one seam; tests monkeypatch it).

---

### Minor findings

| ID | Where | Spec | Scenario | Fix |
|---|---|---|---|---|
| m1 | `leakage.py:676-706`, `runner_select.py:272, 357` | S-13.4 "NO-GO: leakage", K1 | The holdout L1 re-check calls a caller-supplied `records(t)`. A decoy that returns `max_known_at=None` yields an empty findings report, so the check is "empty = checked" again (the R1 M2 pattern). The primary L1 refusal stays in `DecisionProvider.__call__` (`runner.py:726-727`), so this is defence in depth only. | Require `records` to be the bound provider's `record` (the issued universe), or recompute from the runs' decision logs and stored views. |
| m2 | `forward_model.py:107-147` with `runner.label_panel` (window = W_DEV); AMB-43(a) | §15 "T_fit = [D0, 2026-01-01) … S-10.3 purge" | AMB-43(a) says the W_HOLD start does not purge T_fit. The pinned W_DEV panel (window W_DEV) has no label for decisions whose t_x ≥ 2025-07-01 (about h*/8 h decision times), and the W_HOLD panels start at 2025-07-01. T_fit therefore has a de facto h* hole at the W_HOLD start. The effect is small but unlogged and contrary to the AMB text. | Log it in AMB-43(a) (or label those decisions with window = the forward T_fit). |
| m3 | `runstore.py:366-368` | S-1.7, AMB-36 | `load` guards the window from the unpinned, rewritable store manifest. The record's own window is compared only after `decode_run` has read and decoded the files. A falsified manifest window decodes a W_HOLD run before the integrity refusal. It needs a prior W_HOLD run on disk, so there is no pre-opening breach. | Guard the decoded run's window before returning, and decode only after the pinned `file_sha256` check. Minor. |
| m4 | `viewstore.py:188-206` | §17 "after the verdict nothing opens" | `ViewStore.get(t)` is public and unguarded. Stored W_HOLD decision views (features / gates, no outcomes) stay readable after the verdict, bypassing the provider guard. | Route `get` through `guard.check_data_span(t, t+1)`. |
| m5 | `runner.py:945-953` (`model_hash` = sha256 of a pickle) | DT4, DEC-19 | The fold and final models are never persisted (only the forward model uses `modelcodec`). The opening process must recompute them and reproduce byte-identical pickles, or `holdout_inputs` refuses (I-2). `probe_hash.py` shows stable hashes across two processes with different `PYTHONHASHSEED` for small LightGBM / EN fits, but no repository test covers full FoldModels across processes or libraries. It fails closed, but it could block the opening. | Persist fold and final models with `modelcodec` (pinned sha256) and hash those bytes, or add a cross-process `FoldModel` hash test. |
| m6 | untracked `scripts/v7/dryrun_ingest.py` (and `docs/results/v7-stage0/OVERNIGHT_PROGRESS.md`) | §17, C6 provenance | The C6 real-data driver is not committed, so the dry-run evidence is not reproducible from a commit. It imports only guarded entry points and computes no labels / models / P&L (checked). | Commit it with the C6 report. |

---

## Round-1 closure

| Item | Verdict | Evidence |
|---|---|---|
| M1 real-scale | **partially closed** | Decision pass: `pitindex.py`, `viewstore.py`, `runner.decision_pass` (`runner.py:799-835`), bench extrapolation ~2.5 GB / ~7.5 h. Labels / books / canaries at scale: not done (AMB-38 "Not done"; MJ1). C6 phase B pending. |
| M2 L9 / K7 / LeakageReport | **closed** | `leakage.py:406-646` (L9a per family × horizon, L9b L1 refusal + reported sandbox IC, K7 merged indicator shares, sealed report with sha256 / issuer); verdict refuses unverified / unpassed reports (`verdict.py:362-384`, `runner_select.py:312-340`); opening re-derives `passed` (`opening.py:85-118`). Open normative note on the L9a false-failure rate (below). |
| M3 fetch / loaders / K9 / E6 / W_HOLD guard / Kraken | **closed**, with MJ4 residual | `fetch.py:262-309` (netguard before I/O, redirect refusal, N1 checksum, N2 truncation, N5 / N6 pairs, E6 pin); `localdata.py:33-82`; `guard.py:422-437`; `runner.py:658-663, 909-910`; `labels.py:195-199`; `rawstore` loaders; `kraken.py` (L11 guard; #5 UNAVAILABLE, AMB-41 normative). The pinned W_HOLD manifest is form-only (MJ4). |
| M4 DEC-19 preconditions / forged auth | **closed** | `guard.py:606-645` + `opening.py:151-189` (S-10.6 choice, empty kill, LeakageReport passed, links, commit == HEAD); `guard.py:353-381` canonical re-verification; closure key; probe P1 refused. |
| M5 caller spans (L7 / M6) | **partially closed** | Outcome guard / data guard / fetch: canonical `_roots` only (`guard.py:440-451`, `fetch.py:354-357`). ForwardStore / forward.l7 / windows.m6 / partition_m6_keys still take `repo` (MJ5, probe P2). |
| M6 AMB-05 misclassified | **closed** | DK-05 / DK-06 on the docket, approved; C4 run on Stage-0 (`c4-notice-validation.md`); R50–R52 implemented (`captures.py:380-530`). Residual: AMB-42 (normative, below). |
| M7 T_fit purge | **closed** (logged) | AMB-30 (`AMBIGUITIES.md:366`), DK-39 IC confirmed (R38). |
| M8 A1 veto as exit | **closed** | `arms.py:7, 126-164` (entry-only `vetoed`); `constructor.py:10-15`. |
| m1 ns_array ints | closed | `timeutil.py:55-97` (window applied to int / uint). |
| m2 to_ns floats | closed | `timeutil.py:27-30` (R43: float64 accepted only in `ns_array`). |
| m3 mutable constants | closed | `constants.py:7-22` (`MappingProxyType`). |
| m4 public `_secret` | closed | `guard.py:829-881` (closure key, no class attribute). |
| m5 instance switch at open | closed | `instances.py:56-70` (`first_bar_ts + 15 min <= t − 1 min`, DK-108). |
| m6 hedge outside gross | closed (logged) | `AMBIGUITIES.md` AMB-16 round-2 log (line ~200). |
| m7 maker_exit too broad | closed | `engine.py:29, 320-330, 397` (DK-77: only a posted limit rests). |
| m8 account() raises | closed | `selection.py:40, 109`; `verdict.py:10-16, 80`. |
| m9 empty decisions / killed | closed | schema frames (DK-113, AMB `(d)` line ~389); `verdict.py:218-222`. |
| m10 research flag | closed | `runner.py:1035-1049` (`run_book` → `research=True`); `runner_select.py:309-311`. |
| m11 CRAN equality | closed | `cran-bidask-equality.md` (23/23, real R). |
| SC-01 | **partially closed** | see M1 / MJ1. |
| SC-02 | **closed** | see M2. |
| SC-03 | **closed** (MJ4 residual) | see M3. |
| SC-04 | **implemented, #5 unavailable** | `kraken.py` (guard, frozen-copy verification); AMB-41 (a)(b)(e) + F8 normative (user). |
| SC-05 | **closed** | see M4; R45 resume `guard.py:755-783`, attempt logs, verdict marker `guard.py:803-816`. |
| SC-06 | **partially closed** | see M5 / MJ5. |

## Decided rules, spot-checked

| Rule | Where checked | Result |
|---|---|---|
| AMB-29(a) | `engine.py` tick, d78a1c1 | Matches the user decision: no stale carry, halted or not; B / C at the same tick → `INVALID_TERMINAL`. |
| DK-69 / R42 | AMBIGUITIES AMB-29(b) and execution | Required exits / reduces without a bar within 60 min → `INVALID_EXIT_GAP`. |
| DK-70 | engine tick | mark → same-period last close → INVALID. |
| DK-10 | — | Labels keep τ, the book fills at the substitute bar time. |
| AMB-40 + R49 | `IDENTITY_FIELDS`, `guard.py:103` | quoteAsset whitelisted; multiplier prefix rule. |
| R46 | `netguard.py:105-121, 248-255` | Post-opening N3 bound < 2026-07-01, verified auth, M6 refused. |
| R48 | ingest | Implemented as ruled; its downstream consequence is MJ2. |
| R50–R52 | `captures.py:380-530` | Implemented as ruled, word order included (R52(b) `DELISTING_OF_POSTPONED`). |
| R54 | `guard.py:505-592`, `forward_model.py` | Write-once record, canonical panels, `modelcodec`. |

**IC sample (R38 condition):** DK-26, DK-27, DK-39 / AMB-30, DK-108, DK-77, DK-113. I found no genuine contradiction with the frozen text. The DK-39 reading of "the purge applies at each block boundary" is one admissible reading of S-10.3 / S-10.4. I did not re-audit all 72 ICs.

## Determinism / DT4

- LightGBM runs with `deterministic=True`, `num_threads=1` and fixed seeds (`models.py:110-131`).
- The canary permutation uses `default_rng(seed)` fresh per horizon.
- The view-store key holds the code identity plus the library versions.
- The pre-opening record, LeakageReport and pin all carry commit == HEAD.
- Residual: m5 (pickle-based model identity across processes).

## Forbidden actions

- No path in `luna/v7` computes labels, books, P&L or IC on W_HOLD / M6 / W_FWD7 without the guarded flow (the outcome and data guards are on every entry I traced).
- W_DEV labels and books on real data are not code-blocked. They are the research run itself, and the "not yet" constraint is procedural.
- `scripts/v7/dryrun_ingest.py` uses only `guard` / `ingest` / `fetch` / `rawstore` / `metaingest` entry points.

## Unlogged divergences / invented rules

1. **MJ2:** a ledgered ingest failure leaves a hole that downstream code reads as real inactivity (instance split, halts). R48 covers non-fatal ingestion, not this consequence.
2. **m2:** the de facto T_fit purge at the W_HOLD start in the forward refit, contrary to AMB-43(a).
3. **MJ4:** "pinned manifest" read as "any caller sha256" (AMB-34(g)) with no definition of its content. That is an implementation reading of a mandatory component, not just a clarification.

---

## Open normative items (for the user; not decided here)

### 1. AMB-42: DEC-40 base-only notices, token swaps, mixed notices (U6 coverage) — impact on W_DEV validity: high

**Evidence from C4 and Stage-0** (`c4-notice-validation.md` §5, `docs/results/v7-stage0/m31/table.md`):

| Measure | Value |
|---|---|
| Stage-0 terminations matched with a usable `scheduled_ts` | 6 of 74 |
| Class B: matched with `scheduled_ts` / matched null / missed | 3 / 2 / 9 (of 14) |
| Class C: missed | 12 of 12 |

**Inside the book test windows** (the first outer test quarter starts ≥ D0 + 24 mo = 2023-01-01), the B / C events are:
- BNX#0, COCOS, TOMO, STRAX, WAVES, AGIX, RNDR, MATIC, UNFI, XEM, MAVIA, DAR, FTM, BNX#1, VIDT, EOS: **16 events (8 B, 8 C)**.

U6 can exit a held leg before cessation for only **2** of them:
- MAVIA (matched, scheduled);
- WAVES (matched, scheduled null, but known 2024-06-03 < cessation 2024-06-11).

The others are not covered:
- UNFI and XEM are matched, but their notices are known after cessation (2024-11-01 > 2024-10-30, 2025-04-29 > 2024-12-09).
- COCOS, STRAX, RNDR, MATIC, BNX and TOMO are base-asset-only or token-swap wordings.
- AGIX is unclassified.
- DAR, FTM, VIDT and EOS have no capture.

**So 14 B / C cessations inside the test windows are unprotected, several of them large, liquid contracts (MATIC, FTM, EOS, RNDR, AGIX).** Stage-0 found 719 terminal B / C labels, which means the contracts typically stay gated-eligible up to cessation.

**Quantification** (structural; no outcome data read):
- K = 5 legs per side. If a given required run holds a given contract at its cessation with probability p, then W_DEV is valid only if none of the 9 required runs holds any of the 14.
- p = 2 % (well below random selection from ~150 eligible per side, 10/150 ≈ 6.7 %): P(valid) ≈ 0.98^(14×9) ≈ 8 %. Positive correlation between the 9 runs raises this, but even a single run gives 0.98^14 ≈ 75 %.
- p = 6.7 %: about 38 % per run, and lower jointly.
- Carry ranking tends to favour the extreme funding typical around delistings and swaps, so p is plausibly not small.

**Conclusion:** under the current DEC-40 reading, a W_DEV `INVALID RUN — historical terminal execution not reconstructible` is **more likely than not**. I cannot measure it without running books.

**Holdout / forward:** the same mechanism applies (W_HOLD ML* / A0 required; forward ML*), with the coverage of the E6 / forward recorder.

**Options for the user:**
- (i) amend DEC-40 to accept a base-asset token in an unambiguous USDⓈ-M context and token-swap / rebrand notices as delisting evidence;
- (ii) accept the risk;
- (iii) run an outcome-free exposure diagnostic (eligibility and score rank at cessation, no P&L) before the tag.

### 2. AMB-41 (a) / (b) / (e) and F8: Kraken comparison #5 unavailable

- W_DEV: none.
- Holdout: comparison #5 is reported UNAVAILABLE and stays in the Holm family of 6 as a non-rejection (DK-83). This slightly reduces the power of the other secondary comparisons. No effect on `Verdict.cls`.
- Forward: none.

The user must decide whether to keep m = 6 with #5 unavailable, or amend it (1-minute substitute, funding convention, V5 gate scope, F8 counts).

### 3. AMB-43(d): forward stale-quarter tick (no-op for risk) vs AMB-29(a) INVALID on a missing mark

- Forward stage only.
- Under a strict AMB-29(a), any recorder gap without a mark at a tick makes the forward run INVALID.
- The §15 evaluable-quarter logic suggests skipping instead.
- No effect on W_DEV / W_HOLD.

### 4. New: L9a family-wise false-failure rate (spec-literal, DK-27 approved)

- Six tests (2 families × 3 horizons) at |IC| ≤ 2 SE, each with a false-alarm rate of about 4.55 % under no leakage. The family-wise rate is up to about 24 % (lower with correlation).
- A failure is K1: "fix; restart W_DEV selection from scratch". With the frozen seed the restart reproduces the same canary, so a spurious failure blocks the holdout permanently, with nothing to fix.
- The user may want a ruling: the seed policy on restart, or a family-wise reading. This changes no code now.

### 5. New: definition of DEC-19's "pinned manifest" (MJ4)

What exactly is hashed (which tables, listing fields, symbols), and whether post-opening W_HOLD ingestion must match it.

### 6. New: ingest-gap policy (MJ2)

Whether a ledgered gap or a DEC-33 conflict blocks the research run (spec: "blocker"), or is accepted as missing data with its instance / halt consequences.

---

## Overall assessment and recommendation

Round 2 closed most of round 1 well:
- the seal (DEC-19 preconditions, canonical opening location, R45 resume, verdict marker, K9 logging);
- the fetch layer and the local-data guards;
- the leakage machinery, which the verdict and the opening now consume;
- the real-scale decision pass with exact equivalence;
- all eight round-1 Majors' logic items, and every minor.

I found no seal breach that computes outcomes before authorization, no PIT leak, and no wrong-verdict bug in the computation itself.

**Recommendation: NOT READY for code-freeze review.** Reasons, each independent of the normative items:
1. **MJ1:** the outcome side of W_DEV (labels for three horizons from D0, L9 canaries, nine-plus engine runs with 1-minute data) has not been shown to fit this host. `from_source` keeps all 1-minute bars resident. A fix after the tag means rewriting frozen modules.
2. **MJ3:** the single-look W_HOLD verdict accepts runs, quarters, panel and model scoring that are not bound to the canonical W_HOLD window, grid, universe and pinned final model (the binding R54 added for the forward panels).
3. **MJ4:** DEC-19's pinned manifest has no builder or consumer and cannot be added after the tag.
4. **MJ2:** ingest failures become silent holes that change instances, halts and validity.

MJ5 (caller repository in the forward store and L7) is forward-stage but cheap to fix with the same change. Fix it before the tag.

**Also needed before the tag:**
- C6 phase B (features and gates at real scale);
- the user's decisions on AMB-42 (the U6 coverage makes a W_DEV INVALID RUN likely), AMB-41, AMB-43(d), and the three new items above.

After these fixes and decisions, a focused round 3 on the outcome-side data access, the verdict binding, the manifest builder and the ingest-completeness gate should be enough for a freeze recommendation.
