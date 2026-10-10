# Luna

**A systematic crypto trading research and paper-trading platform, built to say "no edge" when there isn't one.**

Luna scans a liquid crypto universe on Kraken Futures (USD-margined perpetuals) and Kraken Spot,
generates candidate trades from several strategy families, ranks them by historical risk-adjusted
expectancy, and only lets the best one to three through a deterministic risk engine. Most of the
time it does nothing, by design.

The project is as much about **research discipline** as about trading: walk-forward validation,
pessimistic fills, full cost modelling, naive baselines a strategy must beat, frozen specs, and a
holdout period that has never been touched.

> **Paper only.** There is no live broker in the code (`live_enabled: false`). Retail access to crypto
> derivatives is restricted in the UK; perp results are for research.

> **Branches:** active development lives on [`feat/luna-core`](../../tree/feat/luna-core); `main` still holds the
> initial skeleton. The links below point at files on `feat/luna-core`.

---

## Architecture

```
MARKET DATA  Kraken Futures (perp, funding) · Kraken Spot (OHLCVT + trade gap-fill)
   → Feature engine (causal, 15-minute bars, resampled to 1h / 4h / 1d)
   → Regime (rule-based, per asset)
   → Strategies: momentum · mean reversion · pairs   → candidate trades
   → Ranker (historical expectancy cells, conservative acceptance)
   → Portfolio constructor (≤ 3 positions, correlation buckets, risk budget)
   → Risk engine (pure function, cannot be bypassed)
   → Execution (SimBroker / PaperBroker) → SQLite store → Streamlit dashboard / Telegram
```

| Package | Responsibility |
|---|---|
| `luna/data` | backfill, Parquet bar store, spot archive verification (pinned SHA256), `/Trades` gap-fill |
| `luna/features`, `luna/regime` | indicators and regime labels, lookahead-tested |
| `luna/strategy`, `luna/ranking`, `luna/portfolio` | candidate generation, ranking, portfolio construction |
| `luna/risk` | rules R1–R19 (kill switch, daily loss, drawdown, leverage, stop-less orders, stale data, reconciliation…) |
| `luna/engine`, `luna/execution` | bar-by-bar portfolio simulation, SimBroker, PaperBroker (walks the live order book) |
| `luna/research`, `luna/v2`, `luna/v3` | vectorised trade lists, walk-forward, Research V2/V3 protocols |
| `luna/forward` | forward data collector: 1-minute tickers, 5-minute books, hourly funding, with backup and data-quality checks |
| `luna/carry` | V5 adaptive funding-carry paper engine |
| `luna/store`, `luna/report`, `luna/notify` | SQLite persistence, HTML reports, Telegram alerts |

## Risk engine (excerpt)

`risk_check` is a pure function; rules run in order and the first failure vetoes the order and writes
a `risk_event`.

| Rule | Perp | Spot |
|---|---|---|
| Risk per trade (sized from the stop distance) | 0.75% target, ≤ 1% | same |
| Total open risk | ≤ 3% | ≤ 3% |
| Concurrent positions | ≤ 3 | ≤ 3 |
| Gross leverage after the order | ≤ 2.0x | ≤ 1.0x, long only |
| Daily loss (mark-to-market from UTC 00:00) | 2%, no new positions | 2% |
| Drawdown from peak | 15%, flatten + persistent kill switch | 15% |
| Emergency intrabar stop | position loss ≥ 2% of equity | same |
| Pyramiding / martingale, stop-less positions, stale data | vetoed | vetoed |
| Reconciliation mismatch > 1% | halt and alert | halt and alert |

## Validation protocol

- **Walk-forward:** 6-month train / 2-month test windows; universe, pairs, parameters, expectancy
  tables and regime thresholds are chosen on train only.
- **Pessimistic fills:** signal on bar *t* close, fill on bar *t+1* open, IOC limit orders within a price
  band, taker fees, half-spread and slippage; real hourly funding, never invented.
- **Cost sensitivity:** every result is re-run at 2× costs and 0× / 2× funding.
- **GO / NO-GO**, per account: n ≥ 30, bootstrap 90% CI lower bound > 0, PF ≥ 1.2, max DD < 15%,
  worst day > −3%, positive at 2× cost, ≥ 60% of windows positive, and **beat the best naive
  baseline** out of sample.
- **Multiple-testing honesty:** every configuration tried is counted and reported.
- **Holdout isolation:** 2026-07 → 2026-10 is reserved; research loaders assert they never read past
  the cut-off, and the holdout command has never been run.

## Results so far

Honest negatives are the main output to date.

| Study | Verdict | Key numbers |
|---|---|---|
| V1 in-sample grid, 15m perps (34 parameter sets) | no net edge | none with CI lower bound > 0; momentum has a small *gross* edge that costs (~0.16% round trip) consume |
| V1 walk-forward, perp (11 OOS windows, 208 combinations) | **INSUFFICIENT DATA** | 24 trades, −0.12 R, PF 0.76, 0/11 windows positive; the ranker cut losses from −$224 to −$6 vs random selection |
| Research V2, perp (momentum families, 15m → 1d) | **CLOSED — NO CANDIDATE** | 27 trades, +0.27 R, 90% CI [−0.37, +0.96], fragile (ex-top-3 −0.43 R) |
| Research V2, spot (corrected fee schedule) | **CLOSED — NO CANDIDATE** | 0 configurations accepted on train |
| Naive momentum baseline (EMA 96/480), same OOS period | benchmark | 287 trades, +1.44 R, which the complex system failed to beat |

The lesson the platform keeps teaching: at retail cost levels, simple slow momentum beats the
sophisticated stack, and short-horizon mean reversion and pairs lose money even before costs.

Current work moves away from price-pattern alpha:

- **Forward collector** (Research V4): continuous, outcome-free capture of perp and spot quotes,
  books and funding for unbiased forward evaluation.
- **V5 adaptive funding carry** (paper): short the perps with the highest trailing 30-day funding,
  long the lowest, market-neutral, and switch only when the expected funding advantage beats the
  cost of trading. *Looking continuously is not trading continuously.*

Detailed write-ups live in [`docs/results/`](../../tree/feat/luna-core/docs/results).

## Running it

Requires Python 3.12 and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
uv run pytest -q                       # network tests are excluded by default

uv run luna backfill --venue perp      # perp bars and funding (takes hours)
uv run luna backfill --venue spot      # spot bars, archive verified by pinned SHA256
uv run luna data-report                # coverage and integrity of the bar store
uv run luna baseline --account perp    # naive baselines
uv run luna walkforward --account perp # walk-forward GO / NO-GO report (HTML)
```

Paper trading and services run under Docker Compose:

```bash
cp .env.example .env                   # optional: Telegram, healthchecks, backup folder
docker compose up -d --build
```

| Service | Command | Purpose |
|---|---|---|
| `bot-perp` | `luna paper --account perp` | perp paper engine, own SQLite DB |
| `bot-spot` | `luna paper --account spot` | spot paper engine (starts after spot gap-fill) |
| `collector` | `luna collect` | forward market-data collector |
| `bot-carry` | `luna carry` | V5 adaptive funding-carry paper engine |
| `dashboard` | Streamlit on `:8501` | overview, positions, scanner, risk events, research, carry P&L decomposition |

Health checks: `luna health --account perp`, `luna collector-health`, `luna carry-health`.
Operations, restarts, kill switch and recovery are covered in [`docs/RUNBOOK.md`](../../blob/feat/luna-core/docs/RUNBOOK.md).

## Stack

Python 3.12 · pandas · NumPy · Numba · statsmodels · SciPy · SQLAlchemy + SQLite · Pydantic ·
Typer · Streamlit + Plotly · PyArrow / Parquet · httpx · Docker Compose · pytest · ruff · uv

## Documentation

| Document | Content |
|---|---|
| [`docs/superpowers/specs/`](../../tree/feat/luna-core/docs/superpowers/specs) | design specs (core engine, Research V2, V5 carry) and recorded spec changes |
| [`docs/results/`](../../tree/feat/luna-core/docs/results) | baseline, walk-forward, Research V2 and paper-forward results |
| [`docs/RUNBOOK.md`](../../blob/feat/luna-core/docs/RUNBOOK.md) | running and operating the paper services |
