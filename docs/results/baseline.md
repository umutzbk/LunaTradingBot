# P0.5 — Naive baseline results (perp)

- Run date: 2026-10-06, code `22a6bd5`, command `luna baseline --account perp`
- Period: 2024-07-02 → 2026-10-06 (start = history_start + min_history_days, spec §2)
- Universe at start (ADV ≥ $10M, ≥ 548 days history): **BTC, ETH, SOL**
- Capital $265, risk 0.75%/trade, max 3 positions, gross ≤ 2x, Kraken perp costs (taker 0.05%, half-spread 1–3 bps, slippage 2 bps), funding `known_only`
- Expectancy CI: 90% block bootstrap over trades grouped by exit day

## A. Realistic run (all risk rules incl. 15% drawdown kill-switch)

| Run | Trades | Exp. R | 90% CI | PF | Max DD | Gross | Fees | Spread+slip | Net |
|---|---|---|---|---|---|---|---|---|---|
| combined | 66 | −0.13 | [−0.51, +0.34] | 0.80 | 15.2% | −3.3 | 8.6 | 5.8 | **−17.7** |
| naive_momentum | 23 | +0.42 | [−0.66, +1.87] | 1.41 | 15.1% | +22.7 | 3.2 | 2.2 | **+17.3** |
| naive_mean_reversion | 145 | −0.15 | [−0.29, −0.01] | 0.70 | 15.0% | −5.3 | 19.6 | 13.4 | **−38.3** |
| naive_pairs (ETH/BTC) | 97 | −0.22 | [−0.34, −0.11] | 0.40 | 15.1% | −22.6 | 10.8 | 6.5 | **−39.9** |

Every run hit the 15% drawdown kill-switch and stopped trading early (exposure ≈ 5–10%), so these samples are truncated.

## B. Edge measurement over the full period (kill-switch disabled: `LUNA_ACCOUNTS__PERP__MAX_DRAWDOWN=1.0`)

| Run | Trades | Exp. R | 90% CI | PF | Max DD | Gross | Fees | Spread+slip | Funding | Net |
|---|---|---|---|---|---|---|---|---|---|---|
| combined | 912 | −0.10 | [−0.23, +0.05] | 0.83 | 62% | +11.6 | 95.5 | 65.6 | −0.5 | **−150.0** |
| naive_momentum | 329 | +0.18 | [−0.18, +0.56] | 1.09 | 27% | +181.0 | 64.0 | 45.1 | −1.9 | **+70.0** |
| naive_mean_reversion | 925 | −0.19 | [−0.26, −0.13] | 0.67 | 74% | −56.8 | 81.3 | 55.7 | +0.1 | **−193.8** |
| naive_pairs (ETH/BTC) | 220 | −0.19 | [−0.27, −0.11] | 0.42 | 28% | −28.8 | 26.7 | 16.0 | +0.1 | **−71.4** |

## Reading

- **The bar Luna must beat:** the best naive baseline is momentum at **+0.18 R/trade (CI crosses 0)**. Mean reversion and pairs are reliably negative after costs (the whole CI is below 0).
- **Costs dominate:** for momentum, fees + spread/slippage (≈ $109) took 60% of gross profit ($181). For mean reversion, gross was already negative and costs tripled the loss.
- **The kill-switch works and binds early:** with naive rules, the 15% drawdown limit stops all baselines within the first months.
- The small universe (3 assets at the start date) is a direct consequence of the 18-month history rule against data that starts in 2023-01.
