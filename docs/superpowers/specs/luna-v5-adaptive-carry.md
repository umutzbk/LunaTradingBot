# V5 Adaptive Funding Carry: engine design (implementation milestone 1)

**Status:** implemented on `feat/v5-adaptive-carry`; paper only (`risk.live_enabled: false`).
Separate from the frozen Research V4 experiment: no V4 code, spec, config or result is imported or
changed. The historical backtest protocol is a separate, later design step; no parameter here was
chosen from a backtest result.

## 1. Idea

V4 H2 economics, run as a continuously monitoring service instead of a weekly rebalance:

- short the perps with the highest trailing 30-day realized funding (`FL30`), long the lowest;
- `k` legs per side (`k = 2`), each `0.5 / k x equity` (gross 1.0, net ~0);
- trade only when the expected funding advantage beats the cost of trading:
  **looking continuously != trading continuously.**

## 2. Architecture

| Module | Role |
|---|---|
| `luna/carry/config.py` | `carry:` section of `config/default.yaml` (all knobs; validated) |
| `luna/carry/funding.py` | Kraken PF_ funding semantics, `FundingStore` (known-only trailing means), `FundingLedger` (pro-rata settlement) |
| `luna/carry/market.py` | `fut_tickers` / `fut_book` envelopes -> `MarketSnapshot` (quotes, books, band depth) |
| `luna/carry/source.py` | read-only reader of the collector's `var/forward/raw` files |
| `luna/carry/signals.py` | per-asset FL30 (rank), FL7, current/next rate, percentile, persistence, spread, depth, cost; eligibility |
| `luna/carry/strategy.py` | **pure** `decide()`: target book, cost-aware switching, hysteresis, neutrality |
| `luna/carry/core.py` | deterministic layer: forced exits, sizing, risk-engine checks, two-phase execution, funding P&L, state |
| `luna/carry/engine.py` | long-running paper engine: loop, lock, persistence, restart reconciliation |
| `luna/carry/replay.py` | the same core over a snapshot stream with simulated fills (backtest adapter) |
| `luna/carry/store.py`, `views.py` | evaluation/decision tables; dashboard queries |
| `luna/cli.py` | `luna carry [--once]`, `luna carry-health` |
| `docker-compose.yml` | service `bot-carry` |
| `dashboard/app.py` | page "Adaptive Carry" |

Paper and replay call the same `CarryCore.run_cycle`, which calls the same `strategy.decide`; only
the executor (PaperBroker vs simulated fills) and the snapshot source differ.

## 3. Event loop

`CarryEngine.run_forever` (no cron, no Task Scheduler): every `evaluation.poll_s` (5 s) it stats the
collector's current `fut_tickers` hour file; it evaluates when a new snapshot arrived (collector
cadence 60 s) or `evaluation.max_interval_s` (60 s) passed. Funding is merged from the collector's
hourly `fut_funding` round every 5 min, and at once when that file appears, grows or is replaced
(stat check every evaluation: a round is written symbol by symbol, so a read can land mid-round),
and at start from the daily `fut_funding_full`; order books from `fut_book` every 5 min. The Db lock is refreshed every 60 s.

One evaluation (`CarryCore.run_cycle`):

1. mark at `markPrice`; settle ended funding periods; roll the risk day; R3/R4/R18 portfolio actions;
2. signals and eligibility; stale snapshot -> no orders; global data outage -> freeze;
3. forced exits; `decide()`; sizing; risk pre-check per action group; re-decide on vetoes;
4. phase 1 closes/reduces -> book fills; phase 2 opens re-checked by the risk engine -> book fills;
5. persist: state, positions, funding rows, evaluation + every alternative weighed, equity.

`evaluation.trigger` decides when *economic* changes are allowed: `event` (every evaluation),
`interval` (`interval_hours`), `daily`, `weekly` (`weekday`, `at_hour`). Monitoring, funding,
stops and forced exits run on every evaluation regardless.

## 4. Signals and eligibility

Funding (Kraken PF_, verified for V4): 1-hour periods; API `timestamp` = start of the period the rate
applies to; `relativeFundingRate` is a fraction per hour; positive -> longs pay shorts. A decision at
`t` uses only periods with `T + 1h <= t`.

- `FL30` = mean relative rate of the ended periods starting in `[t - 720h, t - 1h]` (ranking signal);
- `FL7`, current period rate, next-period estimate, percentile, persistence (share of the last 168
  periods with the sign of FL30): logged, not used for ranking (no new alpha feature).

Entry eligibility: valid two-sided quote (not suspended / post-only), snapshot age <= 180 s, full
spread <= 15 bps, 24h quote volume >= $1M, book depth within the order band (+/- 25 bps, thinner
side, latest `fut_book` <= 10 min old, else top of book) >= one leg, FL30 coverage >= 90 % of 720
periods, newest funding period ended <= 3 h ago. Universe: the 20 assets passing the general V4
rules U1-U7 (MUBARAK excluded).

## 5. Decision rule

Notation: `N = leg_frac x equity`, `H = switch_horizon_hours`, `c(a, side) = -side x FL30_a`
(expected funding income per hour per USD; shorts earn positive funding),
`cost(a) = taker fee + live half spread + slippage_bps` per side, `m = switch_cost_multiplier`,
`B = safety_buffer_bps x N` per leg traded. Values are USD over `H`.

| Alternative | Value (trade iff > 0 and all gates pass) |
|---|---|
| ENTER pair (free long + short slot) | `N (c_s + c_l) H - m N 2 (cost_s + cost_l) - 2B` |
| SWITCH held `o` -> candidate `x`, same side | `N c_x H - n_o c_o H - m (n_o cost_o + N cost_x) - B` |
| EXIT pair (funding edge reversed) | `-(n_s c_s + n_l c_l) H - m (n_s cost_s + n_l cost_l) - 2B` |

SWITCH gates (hysteresis): `o` held >= `min_hold_hours`; `o` ranks outside `k + hold_rank_buffer` on
its side; `x` is in the ideal book; `(c_x - c_o) H >= min_carry_improvement_bps`. The switch cost is
exit of `o` (fee + half spread + slippage) plus entry of `x`; the eventual exit of `x` replaces that
of `o` and is not charged. Entries charge a full round trip. Greedy: best value first, at most
`max_actions_per_cycle`; an asset is touched once per evaluation. Ties break by name (the long
side is the short ranking reversed, so equal funding never puts one asset on both sides). While
entries are blocked (R3 day loss, pause, outage) EXIT-pair on funding-edge reversal still runs;
ENTER and SWITCH do not. A scheduled (daily/weekly/interval) rebalance is only used up when entries
were allowed.

Mandatory changes (no value test): forced exits; leg-count neutrality (enter the best light-side
leg if that entry's own value is > 0 and beats exiting the worst heavy-side leg, else exit that leg;
entry only when opens and rebalancing are allowed); net notional above `max_net_frac x equity` -> reduce the heavier side's largest leg by
the excess (reduce-only: the risk engine forbids growing a leg, R10).

`decision_mode: rebalance_to_target` reproduces the V4 weekly semantics with `trigger: weekly`:
at an allowed rebalance every leg not in the ideal book is exited and the missing ones entered,
ignoring costs. Known difference to V4: held legs are not re-sized to `0.5/k x equity` (R10).

## 6. Exits

A leg exits when: kill switch (R4 drawdown or external flag); adverse mark move >= `leg_stop_frac`
(15 %) from entry (then `stop_cooldown_hours` without re-entry); the asset left the universe; the
asset stays ineligible for `ineligible_exit_grace_s` (900 s) for a holding reason (no/suspended
quote, stale funding, insufficient coverage, low volume, spread above `exit_spread_bps`); funding
edge reversed (EXIT pair); a better candidate after costs (SWITCH); neutrality. Never by calendar.
A global data outage (>= half the universe without a fresh quote or fresh funding) freezes the book
instead of exiting it.

## 7. Risk integration

Every order passes `luna.risk.engine.check` (R1-R19) with the carry profile `carry.risk`
(V1 values except: R5 0.04 and R6 0.16 cover the 15 % leg stop on 25 %-of-equity legs, R7 = 2k
legs, R8 1.25 x equity headroom over the 1.0 target). Each action group is pre-checked as a unit on
the account state after its own closes; any veto rejects the whole group (a pair never opens one
leg), the asset gets a 10-minute veto cooldown and the decision is re-run (max 3 passes); state
vetoes (R1-R4, R18, R19) turn entries off for the cycle. Opens are re-checked after the closes
filled. Closes go in two steps: forced exits first; only if all of them filled completely are the
other closes (neutrality unwinds, trims, switches, edge exits) sent, and only if every close filled
are opens sent. An open that gets no fill puts its asset in the 10-minute cooldown. An open on an
asset already held on the same side is vetoed by R10 (``increases_abs_position``). IOC limits are
`mid x (1 +/- order_band)` inside the R15 band; `exit_spread_bps < 2 x order_band` is enforced so a
held leg's exit can cross the spread it is exited for. Config validation refuses a profile that
cannot hold the target book, and refuses `live_enabled: true`.

## 8. Persistence and restart

One SQLite file per account (`var/luna_carry.db`, single writer). The core state (portfolio, legs
incl. those registered for an order in flight, funding ledger, cooldowns, risk state, last booked
fill id, in-flight reference prices) is committed before the broker sees an order and after every
evaluation. On start: lock; reload; book fills recorded after the last booked id exactly once; cancel
orders still open (IOC never rests); drop legs that never filled; reconcile holdings = sum of legs =
net of all recorded fills (mismatch -> pause + risk event). Legs keep their entry time, so a restart
never closes and reopens them; order client ids are unique and the broker ignores a known one.
`last_fill_id` advances only after the core booked the fills. Legs registered for an order that never
filled are written as closed (`unfilled`). Funding rows are written in the same transaction as the
state whose ledger produced them. The same restore runs after any failed evaluation.
`luna carry-resume` clears the kill/pause flags and makes the engine reset its drawdown peak and
day-start equity to the current equity (else R4 would fire again).

## 9. Observability

Per evaluation (`carry_evaluations`): time, snapshot age, trigger/entry permissions, equity,
expected book carry, eligible count, ideal book, holdings, per-asset signals (pruned after
`eval_retention_days`), summary. Per alternative (`carry_decisions`): HOLD / ENTER / EXIT / SWITCH /
REJECT, reason, side, out/in asset, expected, current and incremental carry, switch cost, buffer,
value. Dashboard page "Adaptive Carry": P&L split (funding, price, fees + spread, net, turnover),
current book, every alternative of the latest evaluation, the universe with FL30 ranks and funding,
the decision log and the equity curve. Docker healthcheck `luna carry-health`.

## 10. Configurable parameters (initial values, not tuned)

`k` 2, `gross` 1.0, `signal_hours` 720, `decision_mode` cost_aware, `switch_horizon_hours` 168,
`min_carry_improvement_bps` 5, `switch_cost_multiplier` 1.5, `safety_buffer_bps` 5,
`min_hold_hours` 24, `hold_rank_buffer` 0, `max_actions_per_cycle` 4, `leg_stop_frac` 0.15,
`stop_cooldown_hours` 24, `max_net_frac` 0.10, `slippage_bps` 2, `order_band` 0.0025,
`funding_unknown_grace_hours` 3, gates (section 4), evaluation trigger (section 3), risk profile
(section 7).

## 11. Known limitations

- The collector is a hard dependency (no direct market-data fallback): without fresh snapshots the
  bot sends no orders and its healthcheck fails.
- Paper fills walk the live public book at submission (no queue, no latency, no market impact).
- No top-up of shrunken legs (R10); net drift is handled by reduce-only trims.
- An exit whose spread exceeds 2 x `order_band` cannot fill inside the R15 band; it is retried every
  evaluation (REJECT `close_not_filled`) and blocks all opens meanwhile.
- A funding period whose rate is not in the collector's data `funding_unknown_grace_hours` (3 h) after
  it ended is settled at 0 for good and counted as unknown (V4 known_only convention); a late rate is
  not re-settled.
- A forced exit that does not fill completely holds back every other order of that evaluation
  (dependent unwinds, switches, entries); it is retried next evaluation.
- After a failed evaluation the engine rebuilds its book from the Db (state committed before every
  order + fills recorded after the last booked one); 10 failures in a row exit with code 3.
- The historical backtest (snapshot construction from bars/analytics, protocol, comparisons of
  weekly / daily / interval / event-driven) is designed separately.
