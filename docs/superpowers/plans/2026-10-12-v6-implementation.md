# V6 implementation plan (frozen spec `v6-profitability-spec-frozen` → 9994cf9)

**Spec:** `docs/superpowers/specs/luna-v6-profitability-spec.md` (sha256 `c659d0cd…`). Constants: `luna-v6-frozen-constants.json` (sha256 `152af4cb…`). The spec is frozen; the code must follow it word for word. If the code needs an interpretation the spec does not settle, record it in `luna/v6/declared.py` and send it to review. Never resolve such a point by looking at outcomes.

**Global constraints:**
- the holdout stays sealed: no download, no read;
- no V6 outcome, no P&L or Sharpe comparison, no tuning;
- development data only through the blind runner (invariants);
- no V5 module is modified, and V5 paper semantics stay unchanged.

**Order of work:** each step is TDD and must be green before the next starts.

| # | Module (`luna/v6/`) | Spec | Content |
|---|---|---|---|
| 1 | `spec.py` | §0, JSON | Load the frozen constants; sha pinned |
| 2 | `data.py` | §1 | PIT inputs (funding with known-at, mark/index/trade/spot bars, 5-minute spreads); data-window guard |
| 3 | `forecast.py`, `forecast_diag.py` | §4 | H2a fit/predict, gate (S-H2-3); H2b diagnostic in a separate module that no decision path imports |
| 4 | `riskmodel.py` | S-H3-6 | EWMA vol, 60-day LW constant-correlation, beta |
| 5 | `book.py` | S-H3-2…12 | Zones, hold/entry, deterministic build, post-rounding checks, intraday repair, band, infeasible clock |
| 6 | `engine.py`, `market.py` | §3, S-H3-10/11, S-H1 | One replay engine with a pluggable market (historical delayed taker; forward book walk); accounting, funding, R3/R4, stops, ineligibility exits, terminal unwind |
| 7 | `criteria.py`, `verdict.py`, `stats6.py` | §7, §8 | P1–P12, E/C/T rules, p_GO, Holm, Newey–West, **one central ladder**, holdout decision, forward looks |
| 8 | `guard.py`, `blind.py`, CLI | S-GUARD | Holdout token, blind dev runner (masked; no unmask path), forward T_F guard |
| 9 | `h1.py`, `forward_exec.py` | §6 | H1 historical (taker, spot+perp), forward maker adapter |
| 10 | Blind dev run, two independent reviews, fixes, code-freeze report | §10 | — |
