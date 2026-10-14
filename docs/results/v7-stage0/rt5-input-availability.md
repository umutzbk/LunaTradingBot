# RT-5 / S0-14: `W_DEV` label-input availability audit (DEC-27)

## Check after the FINAL EDITORIAL CLARIFICATIONS (EC-1, EC-9), 2026-10-14 — **current status**

Outcome-free, `W_DEV` only; no label value, return, P&L or IC was computed.

- **EC-1 (`instance_end_known_at` = last close + 7 × 24 h; v3 used 00:00 after the seventh inactive day).**
  - Script `scripts/ec1_check.py` (sha256 `e9681f5567ff9a0d…`).
  - 69 decisions fall in [close + 7 d, the old 00:00 form) for some ended instance. **U8 is true at none of them**, so the label population is unchanged. 14 are on the G_D0 grid (2022-01 or later); the full v4 D0 re-check gives **2021-01 minimum = 31, D0 = 2021-01-01 unchanged**.
- **EC-9 (DEC-29 regime transitions valid, `interval_transition = true`).**
  - Script `scripts/ec9_check.py` (sha256 `4d3dd0ce4ce72a45…`); records in `ec/ec9_records.json`.
  - The 52 v3-flagged records split into:
    - 22 transitions, now valid;
    - 10 genuinely unreconciled (BNT, BNX, FLM, FTT, OMG, RIF, STMX, TUSDT, YGG ×2), still invalid;
    - 20 first records of an instance without an explicit interval, undetermined and still flagged.
  - Most are outside any instance or before `W_DEV`.
- **RT-5 v4 (`scripts/rt5_v4_ec.py`, sha256 `9ff4e0fdd8b9e999…` = v3 + EC-1 + EC-9) is incomplete.** Claude Code stopped it for low system memory after 326 of 381 symbols. Per-symbol results are cached in `var/v7/stage0/rt5v4/`, so a re-run resumes.
  - On those 326 symbols: the labels are identical to v3 (1,274,013).
  - The only change is BNXUSDT: invalid 102 → 66, funding-unreconciled 69 → 33.
- **Bound:** EC-1 changes no population member and EC-9 can only turn invalid records valid, so the v3 figure **834 / 1,536,303 = 0.054 % is an upper bound: PASS** (threshold 0.5 %). The expected exact figure is 798 (834 − 36), unverified until the remaining 55 symbols finish.

## Re-run after the ROUND-3 PATCH (DEC-43, DEC-44, DEC-47), 2026-10-14 — last complete run (EC-1 / EC-9 check above)

- **Script:** `scripts/rt5_v3_r3.py` (sha256 `4b1b6f7b5fedbd57…`), which is v2 plus three changes:
  - **DEC-44, point-in-time instance membership:** an instance counts as live at `t` until `instance_end_known_at`, i.e. 00:00 UTC after the seventh inactive day.
  - **DEC-47, funding intervals:** the restored DEC-29 inference, instance-aware. The nominal interval is the explicit field, else the standard interval matching the spacing, else the previous record's interval. A record is reconciled against the previous nominal interval or its own explicit one.
  - **DEC-43, terminal events:** every label that reaches a terminal event is invalid. The M3-1 audit found no class A event, so all terminal events are B or C.
- **Output:** `var/v7/stage0/rt5v3/summary.json` (sha256 `a1eb530c7ddf9b8f…`).

| Measure | New population |
|---|---|
| Labels | 1,536,303 |
| **Invalid** | **834 = 0.054 %** (threshold 0.5 %: **PASS**) |
| entry halted / entry missing / entry proxy missing / exit gap / mark missing | 0 / 0 / 0 / 0 / 0 |
| terminal event B / C (halted at `t_x`, no executable exit in the instance) | 719 |
| funding missing / funding unreconciled | 175 / 181 |
| unreconciled funding records | 50; individual flags, no cascade (the spec-literal round-3 text gave 4,885) |
| BTC inputs invalid | 291 |
| By year (invalid / labels) | 2021: 66 / 304,506 · 2022: 347 / 304,275 · 2023: 79 / 300,810 · 2024: 214 / 428,160 · 2025: 128 / 198,552 |

- **The 111 v1 "exit at or before entry" labels:**
  - 108 are outside the new population.
  - 3 are now inside it because of DEC-44: LUNAUSDT on 2022-05-12 at 16:05, where the instance end wasn't yet known. All 3 are classified **invalid** (terminal event).
  - None is valid.
- **D0 = 2021-01-01 holds:** the 2021-01 minimum is 31.

## Re-run after DEC-35 / DEC-40 (2026-10-14, 14:10–14:55 UK) — superseded

Outcome-free, `W_DEV` only, same constraints as below.
- **Script:** `scripts/rt5_v2_dec35.py` (sha256 `d192b703a085b67f…`; reuses `rt5_availability.py`).
- **Output:** `var/v7/stage0/rt5v2/summary.json` (sha256 `7dcea76bcd00514d…`), `instances.json`, per-symbol files.
- C: had 24 GB free throughout; 1-minute files streamed; the collector was not touched.

**Population.** As in v1 (the G_D0 grid minus the 5 unknown-type symbols), with two additions:
- **U1** on the activity-based instance (S-2.2): the current instance must have started ≥ 60 d before t. U3's 30-day instance history follows from this, because the grid's U3 already passed.
- **U8:** a volume > 0 15-minute bar must close in [t − 60 min, t − 1 min].

U5–U7 are still not applied, so the population remains a superset of the true candidate set. Total: **1,536,297 labels**.

**Rules applied per label:**
- **Entry:** HALT check at `t_e`; the 1-minute bar or the 60-minute data-gap rule; and an entry proxy pair.
- **Exit:** a halted exit waits for the first volume > 0 1-minute bar of the same instance, filled at its close; with no such bar, the label is "no executable exit". Otherwise, the bar at `t_x` or the 60-minute gap rule.
- **Funding:** the DEC-29 gap rule plus DEC-40 reconciliation, where an unreconciled record makes the label invalid.
- **Mark:** the mark tiers as in v1.

| Measure | New population |
|---|---|
| Labels | 1,536,297 |
| **Invalid (any cause)** | **831 = 0.054 %** (threshold 0.5 %: **PASS**) |
| entry halted / entry bar missing / entry proxy missing | 0 / 0 / 0 |
| exit halted → filled later in the same instance | 0 |
| exit halted → **no executable exit** (instance ended) | 716 |
| exit data gap | 0 |
| funding settlement missing | 172 |
| funding record unreconciled (DEC-40) | 181 (32 unreconciled records; BNX, ICP, OMG, STMX, TLM) |
| mark missing | 0 |
| BTC inputs invalid | 291 labels: 3 BTC (decision × horizon) cells are halted at `t_e` (no volume > 0 BTC 1-minute bar in the prior 60 min) |
| exit-proxy fallback (25 bps + 3 × slip) | 1,074 (cost fallback only, not invalid) |
| **exit at or before entry** | **0** |
| By year (invalid / labels) | 2021: 66 / 304,506 · 2022: 344 / 304,272 · 2023: 79 / 300,807 · 2024: 214 / 428,160 · 2025: 128 / 198,552 |

**The 111 v1 "exit at or before entry" labels are all outside the new population** (U8 or the instance-based U1). None survives, valid or invalid.

**Activity-based instances:** six symbols have more than one instance: BNX, CTK, CVC, ICP, MAVIA and TLM. AERGO is the seventh relist but is excluded as an unknown type.

**D0 re-check** (G_D0 = U1–U4 with activity instances): the 2021-01 minimum is still **31**, so **D0 = 2021-01-01 and the 10 folds stand.**

**Structural exposure, recorded and not counted against the threshold.** The 716 "no executable exit" labels come from **25 instance terminations** that eligible candidates could reach within 168 h before the final trade:
- AGIX, AKRO, ANC, BNX, BTT, BZRX, COCOS, DAR, DODO, EOS, FTM, ICP, KEEP, LUNA, MATIC, MAVIA, RNDR, SRM, STRAX, TLM, TOMO, UNFI, VIDT, WAVES, XEM.
- Under S-5.4, an executed-book run that holds a leg into one of these terminations without a prior U6 exit is `INVALID RUN`.

**Notice-snapshot feasibility (DEC-36), metadata only (superseded: these lags carried a 1-hour timezone bug; corrected figures are in `m31/M3-1-settlement-feasibility.md`, appendix):** `wayback_notice_lag.json`, from `scripts/wayback_notice_lag.py`.
- Of the 47 stored `W_DEV` futures notices, **46 have an HTTP 200 Wayback capture before 2025-07-01**.
- The first capture lags `releaseDate` by a median of 4.1 h: 38 within 24 h, 40 within 72 h, 43 within 7 d.
- Queries were server-side bounded (`to` = 20250630235959). Capture content was not downloaded, so whether each capture contains the notice text is unverified.

## v1 (before DEC-35) — historical

**2026-10-14, 11:25–12:01 UK.** Outcome-free.
- Only the **existence** of label inputs was tested.
- No label value, return, P&L, IC or spread level was computed.
- `W_DEV` only; no file dated ≥ 2025-07-01 was read.

**Script:** `scripts/rt5_availability.py` (sha256 `946ff21e0cf96ea7…`).
**Output:** `rt5-summary.json` (sha256 `6ef12ee5ff5261ca…`); per-symbol files in `var/v7/stage0/rt5/`.

**Disk:** 1-minute monthly files were streamed and discarded, so the working space stayed below 1 GB while C: had 24 GB free. The collector was not touched.

## Population (the denominator)

- **Who:** every (symbol, decision, horizon) on the Stage 0 G_D0 grid (U1–U4), minus the 5 unknown-type symbols (DEC-30). That is 381 symbols and 514,015 symbol-decisions, 2021-01-01 → 2025-06-30.
- **Horizons:** h ∈ {24, 72, 168} h, keeping only labels with `t_x` < 2025-07-01 (S-1.5).
- **Sides:** both sides of a symbol have identical inputs, so each pair is counted once.
- **Not applied:** U5–U7, S-2.5 and DEC-24 instance splitting, which the grid doesn't implement. The population is therefore a superset of the true candidate set.

**Total: 1,538,625 labels.** The BTC hedge inputs were checked for every (decision, horizon) pair: 14,745.

## What counts as an available input

| Check | Rule |
|---|---|
| A: entry reference | 1-minute bar opening at `t_e` (hh:20) |
| B: exit reference | 1-minute bar at `t_x`; else the S-5.4 instance-stopped exit (volume > 0 1-minute bar within the last traded 15-minute bar); else any 1-minute bar in [`t_x`, `t_x` + 60 min] |
| C: BTC hedge | A and B for BTCUSDT |
| D: funding | no missing settlement in (`t_e`, `t_x`_eff] for the leg and for BTC: archive ∪ REST, DEC-29 gap rule including head and tail |
| E: mark at settle | REST markPrice, else mark 15-minute close, else last-price 15-minute close (S-4.6) |

Monthly ∪ daily 1-minute files (DEC-33): 286 daily files were requested, filling **298,080** minutes missing from truncated monthly files.

## Result

| Measure | Count |
|---|---|
| Labels | 1,538,625 |
| A entry missing | 3 |
| B exit missing | 0 (exact 1,538,309; S-5.4 path 316; 60-minute gap rule 0) |
| C BTC inputs missing | 0 |
| D funding settlement missing | 33 |
| E mark fallback missing | 0 (settlements: markPrice 10,387,858; mark15/last15 fallback 9,347,359) |
| **Invalid (any input missing)** | **36 = 0.0023 %** |
| By year | 2021: 0 / 304,506 · **2022: 36 / 304,950** · 2023: 0 / 301,497 · 2024: 0 / 428,706 · 2025: 0 / 198,966 |
| Worst symbols | BNXUSDT 33 / 5,016 (0.66 %), LUNAUSDT 3 / 3,636 |
| REST-filled funding records | 4,875 |
| Funding records with an unexplained interval | 1 (flagged per DEC-29) |

**Verdict: the frozen 0.5 % invalid-input threshold is structurally satisfiable** (0.0023 %, more than 200 times below it).

## Structural observations (not counted as invalid)

- **111 labels have the S-5.4 exit at or before the entry.** The symbol had stopped trading before `t_e`, yet it was still on the U1–U4 grid: zero-volume bars count as present for U4 (DEC-28), and U2's 30-day median lags.
  - This is the round-2 red-team finding C2-1 (dead contracts that keep publishing bars), seen independently here. These inputs exist, but such a label is economically meaningless.
- **The S-5.2 15-minute fallback can never apply at hh:20.** Every entry and exit reference is a 1-minute bar (red team m2).
- **The DEC-33 union is material:** without the daily files, about 298 k required minutes would have been missing.
