# V7 post-freeze implementation clarifications — round 1 (2026-10-15)

| | |
|---|---|
| Frozen spec | `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md`, tag `v7-ml-spec-frozen` → commit `29959fc563c0fc58430744810095cefa1d63c9a8` (blob sha256 `ce7370fa5cd49a47ae0912a46430f2612c972108536d85f2d8306e0389db9dd4`). The tag and the spec are unchanged. |
| Branch | `feat/v7-ml-implementation` |
| Decided by | the user, 2026-10-15 (explicit written decisions) |
| Scope | Implementation clarifications of the frozen text. Each decision resolves an `AMBIGUITIES.md` item that the implementation could not resolve literally. None changes a frozen number. |

## Decisions

### AMB-16(d): one side of the book becomes empty — APPROVED

If either the long side or the short side has zero eligible / holdable legs, the strategy may not continue as a one-sided book.
- The entire ML book is flattened at the next executable opportunity. The minimum hold does not prevent this.
- Replacement positions are never manufactured solely to preserve K.
- The opposite side is never carried alone.
- Normal terminal / untradable rules (S-5.4) still govern any leg that cannot actually be exited.

Rationale (user): dollar-neutral book validity takes precedence over the minimum hold.

### AMB-16(e): target caps vs the marked book — APPROVED

- Constructor / selection caps apply to target exposures and weights.
- Current marked exposures remain subject to the frozen hard risk limits.
- The frozen 25 % drift rule compares the marked current exposure against the target exposure and triggers the prescribed rebalance behaviour.
- Target caps are not redefined as continuously binding marked-book caps in a way that makes the drift rule unreachable.
- No risk limit may be exceeded merely because constructor caps are target-based.

**Implementation reading (for the user to confirm in the decision docket):** S-7.3 lists "Gross ≤ 1.0 · E. Per leg ≤ (0.5 · E)/K. The S-4.4 liquidity cap." With equal weights, the per-leg target already equals the per-leg cap (0.5 · E / n ≥ 0.5 · E / K, capped at 0.5 · E / K). So a per-leg limit enforced continuously on the marked book would trim every upward move and make the drift rule unreachable, which this decision forbids. The implementation therefore applies:
- per-leg and liquidity caps to targets, entries and increases (constructor), with the 25 % drift rule as the marked-book mechanism for an individual leg;
- the gross limit (1.0 · E) as the hard marked-book limit, evaluated at every decision: if the marked gross exceeds 1.0 · E, legs are reduced proportionally (reduce-only, never while halted) back to 1.0 · E.

### AMB-20: missing terminal bar — APPROVED

If a price or bar required to produce the terminal mark for an evaluation window is absent, and no frozen HALTED / terminal-event rule supplies the legitimate handling path, the result is **INVALID RUN / INVALID EVALUATION PATH**. The implementation never carries forward a stale mark, interpolates, synthesizes a price, or silently shortens the evaluation window.

### AMB-21(b): missing BTC reference price — APPROVED

A missing BTC price invalidates the path only when that price is actually required at that timestamp, for one of:
- hedge construction (a hedge trade that must execute);
- hedge marking (a held hedge at a 15-minute mark);
- canonical §8 accounting (B(e) for mkt_c while a leg is held, at a leg fill or a 15-minute tick).

When it is required and unavailable, the path fails closed: no stale, interpolated or synthetic BTC price is used, and the affected evaluation path is INVALID under the existing propagation rules (a required arm ⇒ INVALID RUN; a secondary arm ⇒ only its comparison is void). An otherwise independent step is never invalidated merely because BTC data is absent when BTC is not required by that step.

### Task 19b: research runner

The runner is a pure deterministic orchestrator with no research logic. It:
- connects the existing PIT gates, ĥ, β̂, volume, frozen features and fold-specific model outputs to the existing book engine;
- stitches quarters / folds per S-10.6;
- uses only existing frozen / approved components and preserves known-at semantics;
- fails closed on missing required inputs;
- exposes enough provenance to reproduce every decision;
- contains no hidden thresholds or economic literals;
- uses synthetic fixtures only in this phase.

Tests must prove the runner cannot:
- use the wrong fold model;
- cross temporal boundaries;
- access future features or metadata;
- bypass U1–U8 or risk;
- fill across contract instances;
- continue a one-sided book;
- silently substitute missing required inputs.

### Remaining ambiguities

Every other normative item stays unresolved and is presented in one consolidated decision docket (`docs/results/v7-impl/DECISION-DOCKET.md`) before code freeze. Any unresolved item capable of changing research outcomes is a code-freeze blocker. Provisional behaviour is never finalized silently.

## Implementation status of these decisions

| Item | Code status |
|---|---|
| AMB-16(d) | Implemented as the interim (flatten when a side is empty, even inside the minimum hold). Runner-level test added in Task 19b. |
| AMB-16(e) | To change: add the marked-gross hard limit at decisions (reduce-only, proportional, never while halted). Per-leg and liquidity caps stay target / entry caps, with the 25 % drift rule. |
| AMB-20 | Implemented (INVALID on a missing unwind-minute bar; the detail string distinguishes a data gap from a halt). |
| AMB-21(b) | To change: missing required BTC price → INVALID inside the engine (hedge trade required, hedge held at a tick, or leg held / leg fill needing B(e)), never an exception and never a stale BTC mark; no invalidation when BTC is not required. |
