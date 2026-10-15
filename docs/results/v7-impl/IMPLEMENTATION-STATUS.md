# V7 implementation status (2026-10-15)

| | |
|---|---|
| Branch / HEAD | `feat/v7-ml-implementation` @ 2dc7344 (forked from frozen spec `29959fc`; tag `v7-ml-spec-frozen` unchanged) |
| Code | `luna/v7/**`: foundations, guards, data layer, instances, intervals, notices / terminal, market / spread / β̂, universe, features, labels, models, cv, arms, book (constructor, risk, hedge, execution, ledger, engine), accounting, stats, selection, verdict, deploy, forward, runner, runrecord |
| Tests | `tests/v7`: 558 passed (`-W error`); rest of the repo: 977 passed, 2 skipped, 3 deselected (network). ruff clean. Synthetic fixtures only. |
| Spec traceability | `luna/v7/__init__.py::SPEC_COVERAGE` (202 IDs) plus `tests/v7/test_spec_consistency.py` (125 checked numeric rows, 14 NOT_CODED with file:line) |
| Ambiguities | `docs/results/v7-impl/AMBIGUITIES.md` (AMB-01 … AMB-28) |
| User decisions | `docs/results/v7-impl/CLARIFICATIONS-2026-10-15-round1.md` (AMB-16(d), AMB-16(e) / R35, AMB-20, AMB-21(b)), implemented in Task 19c |
| Implementation red-team r1 | `docs/results/v7-impl/implementation-redteam-r1.md`: Critical 0 / Major 8 / Minor 11 |
| Decision docket | `docs/results/v7-impl/DECISION-DOCKET.md`: 119 items (72 implementation clarification, 31 normative amendment required, 16 code bug), 117 code-freeze blockers |
| Never done | training on W_DEV, real-data labels / P&L / IC, economic selection, W_HOLD access, V6 masked outcomes |

**Status: NOT READY FOR CODE-FREEZE REVIEW.** The following remain open:
- the 31 normative docket items need user decisions;
- the 16 code-bug items, including SC-01 … SC-06 (red-team M1–M5), need implementation;
- the clarification items need confirmation;
- a second red-team round and an outcome-free real-scale dry run (features and gates only) need to follow.

No code-freeze tag exists.
