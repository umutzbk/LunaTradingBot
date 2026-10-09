# V4 funding semantics verification (S0-F), 2026-10-09

**Verdict: PASS.** Universe membership is decided by the general §2a rules U1–U7. MUBARAK fails U3, U4, U6 and U7 and is excluded; the U3/U4 thresholds were written after this S0-F check (see `v4-universe.md`).

**What was used:**
- Official Kraken documentation.
- The live API format (fixture `tests/fixtures/v4_historicalfundingrates_PF_XBTUSD.json`, development window 2026-03-02).
- An empirical check on the development window only (`< 2026-07-01`).
- Synthetic hand calculations.

No strategy return, P&L or Sharpe was computed. No holdout data was read.

## Documentation

**Source 1:** Kraken support, "Linear Multi-Collateral Derivatives Contract Specifications" (PF_ contracts):

| Topic | Kraken's wording |
|---|---|
| Funding period | "Every 1 hour at the end of the hour" |
| Settlement | Funding "accumulates as unrealised profit/loss and settles every 1 hour at end of the Funding Period, or when user changes net open position (whichever occurs first)" |
| Accrual | Positions "immediately and continuously receive or send funding while open" |
| Rate window | "Rate for next period is calculated over current 1-hour period" (the 12–13 UTC rate is set during 11–12 UTC) |
| Sign | "If the funding rate is positive, long positions pay short positions; if negative, short positions pay long positions." |
| Absolute rate | "The amount of funding an account will receive by maintaining a 1 contract unit short position for 1 hour." |
| Relative rate | "The absolute funding rate relative to the spot price at the time of funding rate calculation." |
| Payout | "absolute funding payout = # of contracts * absolute funding rate * time elapsed within funding period without position alteration" |

**Source 2:** Kraken Futures API reference, "Historical funding rates":
- `timestamp` = "Start of the period to which the funding rate applies."
- Results are sorted ascending.

## Empirical checks (development window 2025-10-06 → 2026-06-30, 21 assets)

**Implied price** is `fundingRate / relativeFundingRate`, compared with the perp price at T (the close of the 15m bar with `open_ts = T − 15m`).

**Correlation columns:**
- **prev:** correlation of `rel_T` with the 1h mark/index premium of hour [T−1h, T).
- **same:** the same correlation for hour [T, T+1h).

| asset | rows | sign agreement (abs vs rel) | implied price median error | corr prev | corr same |
|---|---|---|---|---|---|
| ADA | 6417 | 1.0 | 0.028% | 0.94 | 0.562 |
| AVAX | 6417 | 1.0 | 0.032% | 0.293 | 0.182 |
| BNB | 6417 | 1.0 | 0.036% | 0.216 | 0.037 |
| BTC | 6416 | 1.0 | 0.013% | 0.83 | 0.733 |
| DOGE | 6417 | 1.0 | 0.028% | 0.658 | 0.444 |
| ENA | 6417 | 1.0 | 0.100% | 0.665 | 0.531 |
| ETH | 6416 | 1.0 | 0.015% | 0.804 | 0.462 |
| FET | 6417 | 1.0 | 0.158% | 0.38 | 0.279 |
| HYPE | 6417 | 1.0 | 0.078% | 0.821 | 0.557 |
| LINK | 6417 | 1.0 | 0.032% | 0.636 | 0.305 |
| MUBARAK | 6417 | 1.0 | 1.147% | -0.338 | -0.323 |
| NEAR | 6417 | 1.0 | 0.066% | 0.258 | -0.01 |
| PEPE | 6417 | 1.0 | 0.052% | 0.459 | 0.319 |
| PUMP | 6417 | 1.0 | 0.151% | 0.397 | 0.317 |
| SOL | 6417 | 1.0 | 0.023% | 0.782 | 0.625 |
| SUI | 6417 | 1.0 | 0.029% | 0.49 | 0.007 |
| TAO | 6417 | 1.0 | 0.063% | 0.506 | 0.34 |
| UNI | 6417 | 1.0 | 0.044% | 0.42 | 0.141 |
| XLM | 6417 | 1.0 | 0.054% | 0.68 | 0.438 |
| XRP | 6417 | 1.0 | 0.019% | 0.657 | 0.311 |
| ZEC | 6417 | 1.0 | 0.059% | 0.712 | 0.577 |

**Findings:**
1. **Hourly period.** Rows are 1 h apart. Every asset has the same API gaps: five 2 h gaps and one 3 h gap. These are treated as missing periods: zero cash flow, counted.
2. **Units.**
   - `fundingRate / relativeFundingRate` equals the perp price to within a median of 0.01–0.16% for 20 assets.
   - So `relativeFundingRate` is a **decimal fraction per hour**, not a percent. `fundingRate` is USD per unit per hour.
3. **Sign.** The absolute and relative rates have the same sign in 100% of rows. Combined with the docs: positive → long pays, short receives; negative → short pays, long receives.
4. **Timestamp.**
   - In 20 of 21 assets the rate stamped T correlates more strongly with the premium of [T−1h, T) than with [T, T+1h).
   - This matches "rate for the next period is calculated over the current hour" and "timestamp = start of the period the rate applies to".
5. **MUBARAK is anomalous:** implied price error 1.15% and negative correlation in both windows. It is excluded by the general §2a rules (U3/U4/U6/U7; thresholds written after this check, see `v4-universe.md`).

## Locked conventions (code: `luna/v4/funding.py`, tests: `tests/v4/test_funding.py`)

- `parse_funding`: frame indexed by UTC period start; duplicates are dropped (last wins) and rows sorted.
- `funding_cashflow(q, abs, frac) = −q · abs · frac`: long pays and short receives when abs > 0; the reverse when abs < 0; frac must be in [0, 1].
- `accrue_funding(path, rates, until)`:
  - Pro rata within each period.
  - Settles at the period end, or at a position change.
  - A missing period means 0 cash flow, counted in `unknown_hours`.
  - A position closed at the period start receives nothing for that period.
- `known_rates(rates, t)`: only periods with `T + 1h ≤ t`. This prevents look-ahead.
- `annualize(rel) = rel · 8760`.
- **Hand calculation (spec T3):** a 1-unit short held 24 h at rel = 1e-4 and price 101 → +0.2424, and −0.2424 when the rate is negative.
- **Test status:** 18 tests pass.
