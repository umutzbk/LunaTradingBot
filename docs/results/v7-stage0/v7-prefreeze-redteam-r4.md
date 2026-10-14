# V7 pre-freeze red team — round 4 (independent, adversarial)

**Date:** 2026-10-14. **Object:** `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md` (uncommitted, 1,084 lines), diffed against `spec_before_r3_patch.md`.
**Constraints kept:** no network; nothing dated ≥ 2025-07-01 read; no labels, returns, P&L, IC, fits or price values. Only volume > 0 flags, bar/record timestamps, funding-interval fields and archived notice text (< 2025-07-01) were read. Spec, incident file and data untouched.
**Scratch scripts:** `scratchpad/r4/cap_check.py`, `cap2.py` (notice text from the stored captures), `unrec.py` (funding-reconciliation structure: timestamps and interval fields only).

---

## 1. Round-3 decisions: implementation check

| Decision | Spec lines | Verdict | Notes |
|---|---|---|---|
| M3-1 official automatic settlement; A / B / C; no invented fill | 408, 412–420, 427, 649, 768, 869, 900, R12 l.291, DEC-43 l.1027 | **Faithful** | A: official time, exact price, taker fee, no spread / slippage unless the official rule says so (l.415). B / C: no fill, no later instance, no stale trade, no 1m / 15m approximation (l.416). A held B / C event → that run `INVALID RUN — terminal settlement not reconstructible` (l.418). Mere existence invalidates no book (l.418). W_DEV stops before selection (l.649, K11). The same rule applies in W_HOLD and the forward run (l.419). Small items: m4-4, m4-7, and the R12 ratification (EC-7) |
| M3-1 W_DEV audit | l.420; `m31/` | **Faithful** | 0 A / 14 B / 12 C, re-verified in §3. No approximate value is anywhere |
| M3-2 `instance_end_known_at` | l.190–192, R1 l.281, L2 l.673, DEC-44 | **Faithful with one editor reconciliation** (EC-1) | U8 stays point-in-time (l.191, l.221). Resume before the boundary → same instance; after it → new instance (l.192) |
| M3-3 write-only forward CMS recorder | l.145–149, §15 l.827, L10 l.681, DEC-45 | **Faithful; one narrowing to ratify** (EC-8) | recv_ts, URL and SHA-256; sealed; no parsing / derived / display / search / metric; unreadable before L7 (guard test); no permission for research code |
| M3-4 exact endpoint / parameter allowlist; Wayback guard | l.130–143, L10, DEC-46 | **Faithful; two gaps** (m4-1, m4-2) | N1–N6 are enumerated. `fundingRate` is the only REST market-data endpoint. Current-state `fapi` paths are refused even with an old `endTime`. N5 requires a bounded `to`; N6 takes exact pairs only, redirects off, `Memento-Datetime` = ts, and discards on substitution. Refusal tests are listed |
| M3-5 DEC-29 restored | l.257–271, DEC-47 | **Faithful** (bounded residual flags: EC-9) | Explicit field controls; spacing / local-regime inference; ±60 s adjacent-interval gap test; individual invalid flags; no cascade |
| E6 CMS blocked after the W_HOLD opening while M6 is sealed | l.144 | **Faithful** | W_HOLD notices come only from captures < 2026-07-01 under N5 / N6 |
| E8 current `exchangeInfo` narrow scope | l.142, R1 l.281 | **Faithful, with one residual conflict** (EC-6) | It retains the DEC-21 branch-2 "labelled approximation" use of current filters |
| E10 no cross-instance fill | l.412, L2 l.673, (E10) l.1032 | **Faithful** (placement nit m4-3) | |
| E11 vague table reference removed | l.395 (old) removed; R12; l.420 | **Faithful** | R12 is defined but empty (EC-7) |
| R2-8 fee snapshot | l.319–322, l.1043 | **Still open (operational)** | |

## 2. The eleven required checks

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | No synthetic terminal fill anywhere | **PASS** | l.407 (no retroactive fill), l.413 ("No synthetic fill is ever created"), l.416, l.424 (the old last-bar + 1 min rule is removed). The terminal unwind (l.517) runs only through S-5.4 for halted legs. Only stale register wording remains: DEC-12 l.979 "S-5.4 stressed terminal unwind" (m4-6) |
| 2 | Settlement only from an exact official source or formula | **PASS** | l.415, R12 l.291 ("No approximate value is ever inserted"). Gap: the formula itself isn't pinned to an immutable official source; none of the 52 stored captures states it (`states_index_average_30min` = 0 / 52) (m4-7). Moot for W_DEV, where there are 0 A events |
| 3 | B / C invalidate only a book that actually holds them | **PASS** | l.418. Labels reaching B / C are invalid (l.417, l.427) and still count towards K8 in W_HOLD (wording m4-5) |
| 4 | W_DEV selection can't silently drop an invalid candidate | **PASS** | l.649: all six candidates and A0 at each horizon must be valid; "No candidate is dropped, replaced or re-run". K11 l.900. Hyperparameter selection (l.611, l.644) uses label IC, never book runs, so no book-invalid configuration can be dropped there |
| 5 | Instance-end knowledge delayed seven days | **PASS** | l.190 = max(close + 7 d, end of the 7th inactive UTC day). This always equals 00:00 UTC of day D + 8 (D = last active day), which agrees with the dormancy definition (l.188). RT-5: `end_known` = `((b − 15 min) // D + 8) · D` (`rt5_v3_r3.py` l.67–69); `inst_pit` drops the instance only from then on. `m31_events.py` uses the same formula. EC-1 for ratification |
| 6 | U8 point-in-time | **PASS** | l.221 unchanged; l.191 ("never assert a permanent end"); RT-5 window `[d − 75 min, d − 16 min]` on bar opens = closes in [t − 60, t − 1] |
| 7 | No cross-instance fill | **PASS (text)** | l.408 ("same instance"), l.412, L2. The halt test (l.406) isn't instance-scoped. In one theoretical boundary case (entry 00:20 on D + 1, h = 168, relist exactly at 00:00–00:19 on D + 8) the literal halt test would call `t_x` "not halted" on a new-instance bar. E10 forbids that fill, so the text is consistent, but see m4-3. RT-5's non-halted exit branch has the same theoretical gap, with zero practical effect |
| 8 | M6 forward CMS access write-only and sealed | **PASS** | l.145–149, l.827, L10. Narrowing EC-8: catalog → detail requires extracting article identifiers |
| 9 | Endpoint / Wayback guards enforceable; scripts compliant | **PASS, with 2 Minor gaps** | Enforceable as a single fetch layer with a registry of N5-returned pairs. Gaps: N2 lacks `marker` (m4-1); no enumerated allowlist after the opening for non-CMS endpoints (m4-2). **Scripts:** `m31_notices.py` asserts and clamps `to` ≤ 20250630235959 and uses only N5 parameters. It fetches only N5-returned (ts, original) pairs as `id_`, with a no-redirect opener (3xx returned as a refusal), requires status 200 and `Memento-Datetime` = ts, and stores nothing on refusal. `wayback_prepub_check.py` does the same; a missing `Memento-Datetime` header would crash it, which fails closed. `wayback_notice_lag.py` is CDX only (bounded). Tooling nit: the capture cache is keyed by timestamp only (m4-9). Verified harmless: 54 files, no duplicate timestamp, no timestamp shared by two URLs in `notices.json` |
| 10 | DEC-29 restored (text vs `rt5_v3`) | **PASS** | `reconciled()` (`rt5_v3_r3.py` l.91–111) is line-for-line l.264–269: explicit > standard interval within 60 s > previous nominal (flagged); first record of an instance without an explicit interval → undetermined. U3 gap test with max(prev, cur) + 60 s (`rt5_availability.funding_ok` l.161). Structure check in §6 / EC-9 |
| 11 | RT-5 < 0.5 % | **PASS** | `rt5v3/summary.json`: 834 / 1,536,303 = **0.0543 %** |

## 3. M3-1 audit verification (raw captures, outcome-free)

- **Integrity:** every sampled capture's SHA-256 matches its file-name prefix. All 54 capture timestamps are < 2025-07-01; the latest is 20250429054216.
- **SRM (B):** title "…Will Delist RAYUSDT and SRMUSDT Perpetual Futures Contracts". The text reads "…automatic settlement on the RAYUSDT and SRMUSDT … at 2022-11-15 04:00 and 2022-11-15 04:30 (UTC) respectively". There are 12 `SRMUSDT` tokens. Activity flags: the 04:30 bar is the last with volume > 0, closing at 04:45. **B confirmed**; the strict token is present.
- **COCOS (B):** swap notice. Trading is suspended at 2023-05-25 08:30, and "At 2023-05-25 09:00 (UTC), Binance Futures will close all positions, conduct an automatic settlement… on the USDT-Margined COCOS Perpetual Contract". There is no `COCOSUSDT` token. The 09:00 bar is the last with volume > 0, closing at 09:15. **B confirmed**; it doesn't match under strict DEC-40.
- **XEM (B):** "…automatic settlement on the USDⓈ-M XEMUSDT, ORBSUSDT, and LOOMUSDT … at 2024-12-09 09:00 (UTC)". The only capture is from 2025-04-29, after cessation (last volume > 0 bar 09:00, closing 09:15). **B confirmed**: known only ex post, so U6 offers no protection.
- **AKRO (C, conflict):** "…automatic settlements on the AKRO USDT-Margined Contract at 2022-05-26 09:00 AM (UTC) and DODO … at 2022-05-27 09:00 AM (UTC)". Activity flags: 88 of the 92 15-minute bars from 2022-05-26 09:00 to 2022-05-27 08:00 have volume > 0, and the last is at 06:45. Trading continued about 22 h past the stated time, so **the conflict is real**. The audit's "100 of 104" uses a slightly wider window; the conclusion is the same. C is the conservative reading. DODO (B) matches its stated time.
- **BNX #0 (C, conflict):** the swap notice says trading is suspended at 2023-02-10 14:00 and "At 2023-02-11 04:00 (UTC), Binance Futures will close all positions, conduct an automatic settlement…". The archive has **no 15-minute bar at all** from 2023-02-01 00:00 to 2023-02-22 14:45; the last bar with volume > 0 is 2023-01-31 23:45. The observed "end" is an archive gap, not the cessation. **C confirmed.**
- **Base-only naming (6 B events):** confirmed for LUNA ("LUNA USDT-Margined Contract"), ANC ("USDT-Margined ANC Perpetual Contract"), AKRO / DODO, and TLM / ICP ("TLM USDT-Margined Contracts at 2022-06-09 09:00 … ICP … at 2022-06-10 09:00"). Each has **0 `XXXUSDT` tokens**.
- **Could any A event exist? No.** A requires either an official historical settlement price or the 1-second index over the final 30 minutes:
  - the archive bucket listing shows no settlement or delivery dataset;
  - the finest index-kline folder is `1m`;
  - no current-state endpoint is permitted;
  - no stored capture states a price.
- **B / C misclassification is consequence-free.** B and C trigger identical rules (l.416–418); only A would change anything. The audit's extra B criterion ("last traded bar closes at the scheduled time or ≤ 15 min after") is an auditor rule, but it can't matter for the same reason.
- **Search completeness for the 10 "no notice" C events:** limited by the guard (slugless pre-2022 URLs; HTTP 202 bot pages from 2024–2025). That is the intended consequence of S-1.7, not a defect.

## 4. Editor choices the user didn't make explicitly

| # | Choice | Verdict |
|---|---|---|
| EC-1 | `instance_end_known_at` = max(close + 7 d, end of the 7th inactive day), which is always 00:00 UTC of D + 8 (l.190) | **Faithful / necessary.** The literal close + 7 d falls inside day D + 7, before the calendar-day dormancy can be verified. A resumption later on D + 7 would then be "after the boundary" (new instance) by M3-2 but "no dormancy" (same instance) by l.188. The max() removes the contradiction, and it is never earlier than the user's formula. One-line ratification |
| EC-2 | A0 must be valid at each of the three horizons (l.649) | **Faithful.** h\* is unknown before selection, so "A0 valid before selection" can only mean all three. Stricter in effect; ratify |
| EC-3 | A halted held position with no executable exit before the window end also blocks selection (l.649) | **Faithful / neutral.** DEC-35 already made such a run `INVALID RUN`; l.649 only applies "all must be valid" to it |
| EC-4 | R11 is "present" when the poll succeeded (l.838) | **Neutral.** Evaluability needs presence of the input stream, not of new announcements |
| EC-5 | `exchangeInfo` identity is allowed for currently listed, verified-invariant instances (l.142, R1) | **Faithful to E8.** Older instances of re-listed symbols need a source dated within those instances; current `exchangeInfo` covers only the live instance |
| EC-6 | E8 "never backdate filters" coexists with the DEC-21 / DEC-34 branch-2 labelled approximation (l.142, S-14.1 l.790–791) | **Needs ratification.** The literal E8 forbids applying current filters to historical periods, while DEC-21 branch 2 does exactly that for the W_DEV / W_HOLD L-265 and L-10k diagnostics, labelled "approximate". No effect on P1–P11 (l.798). Options: keep the labelled diagnostic, or drop the historical D1–D3 approximation |
| EC-7 | R12 `terminal_settlements` is defined although no official source exists (l.291) | **Neutral, but literally contrary to "add a table only if a real source exists".** It is empty, forbids approximations, and is the only vehicle for a future exact A event. Ratify, or delete it and state that an A event needs a user-added table |
| EC-8 | M3-3 "no parsing" is rendered as "no title, body or symbol parsing" (l.147) | **Needs ratification (implementability).** To capture bodies, the recorder must extract article identifiers (code / URL) from the raw catalog response to schedule detail fetches. The narrowed text tacitly allows this; the user's broader "no parsing" would forbid it, leaving forward U6 to titles only. Suggest: "mechanical extraction of article identifiers solely to schedule raw detail fetches; nothing else" |
| EC-9 | S-2.6 reconciliation (l.268) flags a spacing-inferred regime switch at its switching record. It also flags the record after an undetermined first record and the record after an off-schedule record | **Neutral / conservative, bounded; ratify.** This is the round-3 proposed rule. It never cascades: W_DEV has 50 flagged records, 19 symbols and 181 labels, against 4,885 records under the literal round-3 text. Structural examples (`unrec.py`): the second record of every REST-only instance start (ALGO, ETH, QTUM, BNX, ICP, TLM …), and the BNX 2022-12 8 h → 4 h → 2 h → (6 h anomaly) → 8 h switches (4 records). This is safe against a hidden missing record (a 4 h regime missing one record looks like an 8 h switch). Residual one-step effect: one off-schedule record also flags its immediate successor |
| EC-10 | DEC-40 strict "exact `XXXUSDT` token" | **Faithful to the user's DEC-40 wording.** Consequence, for information only: 6 genuine B notices name "`<BASE>` USDT-Margined Contract" and give no U6 protection (LUNA, ANC, DODO, TLM, ICP, COCOS). Widening the match is a user choice. This is not a defect |
| EC-11 | Forward `INVALID RUN` if ML\* **or A0** held a B / C event (l.869) | **Needs ratification.** In the forward run A0 is not required (l.848 "ML\* − A0 is reported, not required"). S-13.4's own principle is that "an invalid secondary arm voids only its own comparison" (l.768). l.869 makes the forward run stricter than M3-1's "that arm / run". Conservative, so no leak |
| EC-12 | AKRO / BNX #0 classed C for a time conflict (auditor rule) | Neutral: no consequence (§3) |

## 5. Findings

### Critical
None.

### Major
None. Every round-3 decision is implemented, and no leakage, synthetic-fill or selection-bias path was found.

### Minor

| ID | Finding | Minimal fix (within the decisions) |
|---|---|---|
| m4-1 | N2 (l.135) allows only `delimiter` and `prefix`. S3 returns ≤ 1,000 keys per call, and the Stage-0 `list_archive.py` paginates with `marker` and lists without a delimiter. As written, the guard refuses that tool. Prefix narrowing is a workaround. No leak either way | Add `marker` and `max-keys` to N2, or state "pagination by narrower prefixes only" |
| m4-2 | No enumerated allowlist **after** the W_HOLD opening while M6 is sealed, except CMS / Wayback (E6, l.144). For example, N1 or N3 for W_HOLD periods; current-state `fapi` would return M6-dated data | One sentence: "At and after the opening, while M6 is sealed, N1–N6 apply unchanged with every 2025-07-01 bound replaced by 2026-07-01; everything else stays refused" |
| m4-3 | E10 sits as a sub-bullet of the halted-exit rule (l.412). The halt test (l.406) and P(τ) (l.368) aren't instance-scoped (§2, check 7) | Promote E10 to a top-level S-5.4 bullet, and define "halted" on bars of the position's own instance |
| m4-4 | A label reaching an A event uses "(t_x, P(t_x))" (l.417), but S-5.3 (l.383, l.392) then adds h_a + slip at the exit, contrary to l.415 ("no spread or slippage"). Moot today (0 A) | "For an A exit, X = settlement price, fee f, no h_a / slip; the pro-forma hedge exits at the same time with normal costs" |
| m4-5 | l.418 "invalidates nothing" vs K8 (l.897): B / C labels still count towards the 0.5 % K8 threshold in W_HOLD | "…doesn't by itself invalidate that book; its labels are invalid and count towards K8" |
| m4-6 | Stale register wording: DEC-12 l.979 "S-5.4 stressed terminal unwind" | Annotate "superseded by DEC-35 / DEC-43" |
| m4-7 | The settlement formula (l.414) is stated without a pinned immutable official source; 0 / 52 captures contain it | For any future A event, R12 must also cite the immutable official statement of the formula in force at the event |
| m4-8 | l.247 "U6 also ends with its instance" doesn't say "at `instance_end_known_at`". l.191 covers it generally, and U8 blocks entries anyway | Add "(at `instance_end_known_at`)" |
| m4-9 | Tooling: `m31_notices.py` / `wayback_prepub_check.py` cache captures by timestamp only, not (ts, original) | Key the cache by (ts, sha256(original)) in the frozen fetch layer. No effect on the present audit (verified) |
| m4-10 | RT-5 detail: a first record without an explicit interval takes v1's default 8 h in the gap test (`rt5_availability.py` l.143). The spec doesn't define a gap test against an undetermined interval | No impact: such records are already flagged invalid and lie inside U1's 60-day exclusion. Optionally state "an undetermined interval makes the adjacent spacing a gap" |
| m4-11 | R12 is listed before R11 in the §3 table (l.291–292) | Reorder |

## 6. RT-5 verification

- **Population and rules:** G_D0 grid minus the 5 unknown types, with instance-U1 via `inst_pit` (DEC-44) and U8. Halted exit → same-instance bar or a terminal B / C event (invalid). DEC-47 nominal intervals drive the gap test. DEC-40 flags.
- **Result** (`var/v7/stage0/rt5v3/summary.json`):
  - 1,536,303 labels, 834 invalid = **0.0543 %** < 0.5 %;
  - 719 terminal B / C labels; 175 funding missing; 181 funding unreconciled (50 records);
  - 291 BTC-input labels;
  - 0 entry halted, entry missing, proxy missing, exit gap or mark missing; 0 exit ≤ entry;
  - D0 2021-01 min = 31.
- **Correctness points checked:**
  - `end_known` matches l.190.
  - `instances()` uses W_DEV-only bars. `fetch_klines.py` drops rows ≥ 2025-07-01, and a dormancy still running at the window end is left open (None).
  - `reconciled()` matches l.264–269.
  - The +6 labels over v2 and the 3 LUNA 2022-05-12 16:05 labels come from DEC-44 and are correctly invalid.
- **Sensitivity:** even if every EC-9 extra flag were removed, the rate would only fall.

## 7. Seal: is a reset needed?

**No.**
- The incident file's SHA-256 (`beb3cb08…380e`) matches the value in l.151.
- All M3-1 and Wayback work used W_DEV-dated captures (< 2025-07-01), the purged W_DEV catalog index and bounded CDX queries.
- No CMS request and no M6 or W_HOLD content was involved.
- Embedded "related article" lists inside the captures are capture-dated (< 2025-07-01).
- No outcome contamination was found.

## 8. Summary

- **Counts:** 0 Critical, 0 Major, 11 Minor (m4-1 … m4-11), 12 editor choices.
- Editor choices that need a one-line user ratification: **EC-6, EC-7, EC-8, EC-9, EC-11**. Recommended for confirmation: EC-1 and EC-2. EC-10 is for information.
- **Remaining blocker IDs:** **R2-8 / DEC-39**, the official VIP 0 fee snapshot (operational). It is **the only remaining blocker**. The spec itself forbids the freeze until it exists (l.320, l.1043).
- **Judgement:** the snapshot is a precondition of the freeze act, not of the freeze review. The fee values are fixed constants that the spec already says to replace as a factual correction if the official values differ (l.321). No other open item blocks the review. The ratifications and Minor edits can be settled inside the freeze review.

READY FOR SPEC FREEZE REVIEW
