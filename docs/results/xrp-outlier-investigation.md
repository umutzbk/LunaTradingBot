# XRP +300R outlier: investigation

**Verdict: genuine. The sizing and the price path explain it; no bug found.** The trade is a 4.53x
XRP rally held for the full 30-day `max_hold`, on a position sized against a 0.975% stop. With that
setup, a result of about 300R follows directly from the definitions. The engine's numbers match a
hand calculation from the stored bars to within 1e-6.

Pinned by `tests/investigations/test_xrp_outlier.py`. The synthetic tests always run. The tests on
real data need `var/bars/kraken_futures` and `var/instruments/perp.json`, and are skipped when those
are missing.

## Run

The baseline is `NaiveMomentum` with its defaults: EMA(96)/EMA(480) cross, stop 3×ATR(96),
max_hold 2880. The run uses Kraken perp, BTC/ETH/SOL/XRP, 2024-09-01 → 2026-07-01, capital $265,
risk 0.75%, gross leverage ≤ 2x, `max_drawdown=1.0`, `funding_mode="known_only"` and the real
instruments. The run produces 287 trades. This trade is `naive_momentum:XRP:202411052115`.

| field | value |
|---|---|
| decision bar (close) | 2024-11-05 21:15, close 0.51307 |
| entry fill | 21:30 open 0.51307 × (1 + 5 bp) = **0.513327** |
| exit decision / fill | 2024-12-05 21:15 close 2.32413 → 21:30 open 2.32413 × (1 − 5 bp) = **2.322968** |
| qty | 332 XRP (qty_step 1) |
| gross_notional | 332 × 0.513327 = **170.4244** |
| risk_amount | **2.002862** |
| pnl_gross / fees / spread_slip / funding | 601.2719 / 0.4708 / 0.4710 / 0 (720 unknown hours) |
| pnl_net / r_multiple | **600.3301 / 299.736** |

## 1. Price path and data integrity

- Close moved from 0.51307 to 2.32413, a **4.530x** move. The highest high in the hold was 2.91489
  on 2024-12-03 12:00 (5.68x). The lowest low was 0.50920, at 22:15 on the entry day. The lowest
  close was 0.51103.
- The window has 2881 bars, all with `filled=False`. There are no gaps: every step is 15 minutes,
  for 2880 steps. No bar has inconsistent OHLC or a non-positive price.
- The largest 15-minute log return was 7.4% (2024-12-02 14:30). The widest bar range was 14.6%
  (2024-12-03 13:45, the blow-off top). Volume is high on those bars, and both fall inside a smooth
  daily path: 0.51 → 0.71 (11-12) → 1.12 (11-16) → 1.48 (11-22) → 1.96 (11-30) → 2.73 (12-02) →
  2.32 (12-05). That path matches the well-known November 2024 XRP rally. No single bar carries
  the result.
- **Spot cross-check is not possible yet.** `var/bars/kraken_spot` holds only ADA/AVAX/BNB/BTC/ENA/ETH
  because the spot backfill is still running. Once XRP spot lands, repeat the check.

## 2. Sizing

- Stop distance = 3 × ATR(96) / close = 3 × 0.0016681 / 0.51307 = **0.9753%**. The stop level is
  0.508066.
- Equity at the decision bar was 267.0483, after earlier trades. Risk = 0.75% × 267.0483 =
  **2.002862**.
- XRP round trip = 2 × (taker 5 bp + half spread 3 bp + slippage 2 bp) = 0.20%. Gross =
  2.002862 / (0.009753 + 0.002) = **170.41**. Qty = floor(170.41 / 0.51307) = 332. The gross
  notional at the fill is 170.42, which matches.
- The caps did not bind. Leverage headroom was 2 × 267.05 = 534.1, and the per-leg cap was
  1.0 × 267.05 = 267.05. Gross leverage at entry was 0.64x. So `risk_amount` is the planned risk
  (0.75% of equity).
- `r_multiple = pnl_net / risk_amount = 600.3301 / 2.002862 = 299.736`. The engine computes this in
  `finish()` and the tests confirm it.
- Caveat (not relevant here): when the leverage cap or per-leg cap clips gross, `risk_amount`
  becomes `gross × (dist + rt)`, which is less than 0.75% of equity. That makes |R| larger.
  `test_risk_amount_shrinks_when_gross_is_clipped` pins this behaviour.

## 3. Why the exit reason is "time"

- **The stop never triggered.** `NaiveMomentum._stop_hit` checks the bar close against the stop
  level. The lowest close was 0.51103, which is 0.58% above the 0.508066 level. The lowest
  intrabar low was 0.50920, still 0.22% above it. Both happened in the first hour, after which
  price never came back.
- **There was no signal exit.** EMA(96) stayed above EMA(480) for every bar of the hold. The first
  cross down came on 2024-12-09 21:00, four days after the time exit.
- **There is no trailing stop.** NaiveMomentum has no trailing logic. The position gave back about
  $197 (about 98R) from its peak open profit of about $797 at the 2024-12-03 high.
- **The time exit is correct.** `bars_held` reached 2880 at the 2024-12-05 21:15 decision. The fill
  came at the next open (21:30), exactly 30 days after the entry fill.
- **The emergency stop was not relevant.** Its threshold is 2% of equity, about $5.33. The worst
  intrabar loss against the entry reference was 332 × (0.50920 − 0.51307) = −$1.28, plus about
  $0.17 of entry costs. After that, the position stayed in profit, so the stop never came close.
- The many `BLOCK_ENTRIES_TODAY` / R3 events during the hold came from daily equity swings on this
  open position. They block new entries only and do not close positions.

## 4. Accounting

- Cash P&L: 332 × (2.322968 − 0.513327) = 600.8008. Fees are 5 bp × (170.4244 + 771.2254) = 0.4708.
  Net = **600.3301**, which matches the engine.
- Attribution identity: gross 601.2719 (332 × (2.32413 − 0.51307), at reference mids) − fees
  0.4708 − spread/slippage 0.4710 + funding 0 = 600.3301.
- The synthetic reproduction also checks that account equity changes by exactly `pnl_net`.
- **Funding is unknown, and booked as 0.** Kraken XRP funding history starts 2025-10-06, so all 720
  hourly funding events are counted in `funding_unknown_bars`. The position's hourly notionals add
  up to 305,481 USD·h. The size of the missing funding depends on the hourly rate assumed:

  | assumed hourly rate | funding cost | effect on R |
  |---|---|---|
  | XRP known mean, 1.4e-7 | $0.04 | none |
  | p99 hourly rate every hour, 4.1e-5 | about $12.7 | about −6R |
  | 1e-4 every hour (about 88% APR) | $30.5 | the trade would still be about +285R |

  Missing funding does not change the verdict.

## 5. Why about 300R follows by construction

Per unit of notional, R ≈ (P_exit / P_entry − 1) / (stop_distance + round_trip):

(2.32413 / 0.51307 − 1) / (0.009753 + 0.002) = 3.5298 / 0.011753 = **300.3R** before costs. Costs
bring it to 299.7R.

The R unit is about 1.2% of notional, while the trend lasted a month. Any 30-day hold with a
4–5x move gives 250–350R. The synthetic test reproduces this through `run_backtest`: a 4.48x
rally with a 0.96% ATR stop gives 299.2R, inside the 250–330R range the test checks, which reconciles exactly with the hand-computed
P&L.

## 6. Bugs

None found. The fills (next open ± costs), fees, sizing, R denominator, stop and exit logic, and
bar data in the window all reconcile.

The outlier is a real feature of the baseline: a tight ATR stop on 15-minute bars combined with a
long time stop gives a heavily right-skewed R distribution. Across the 287 trades, mean R is 1.437
and median R is −1.05. **Without this trade, mean R drops to 0.394.** Any conclusion about the
baseline's edge depends on this one trade, so read baseline statistics with that in mind; the
`drop top N` bootstrap is the right check. The trade is real, but one 30-day XRP mania episode
produces the outcome.
