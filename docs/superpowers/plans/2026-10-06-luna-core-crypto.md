# Luna Core + Crypto (P0 → P2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Luna's asset-agnostic systematic trading core for Kraken crypto (perp + spot alternatives): verified data, naive baselines, three strategies with regime gating and an expectancy ranker, brutal walk-forward validation with GO/NO-GO, and a 24/7 Docker paper-trading runtime with dashboard and Telegram.

**Architecture:** One codebase for backtest/paper/live; only `Feed` and `Broker` adapters differ. Strategies are vectorized signal generators (precomputed arrays in backtest, last row of a rolling buffer in paper), so research trade lists, the portfolio backtest and the live tick all consume identical signals. Risk is a pure function that nothing can bypass.

**Tech Stack:** Python 3.12, uv, pandas, numpy, numba, statsmodels, scipy, httpx, pydantic + pydantic-settings, PyYAML, SQLAlchemy 2 (Core), typer, plotly, streamlit, pytest, ruff, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-06-luna-trading-bot-design.md` (rev 4). Read it before any task; section numbers below (§) refer to it.

## Global Constraints

- Python `>=3.12,<3.14`; package name `luna`; managed by `uv` (`pip install uv` if missing); lockfile `uv.lock` committed.
- All tests run offline. Any test touching the network is marked `@pytest.mark.network` and excluded by default (`addopts = "-m 'not network'"`).
- Bars: 15-minute, index = UTC bar **open** time (`DatetimeIndex`, tz=UTC). A bar with index `t` is closed at `t + 15min`. At decision time `T` only bars with `t + 15min <= T` may be used.
- Bar frame columns exactly: `open, high, low, close, volume, trades, filled` (`filled` bool; volume in base units).
- Assets use canonical names (`BTC`, not `XBT`). Perp symbol = `PF_{X}USD` with `X = "XBT" if asset == "BTC" else asset`. Spot pair = `XBTUSD` / `{asset}USD`.
- Account currency USD. Total experiment budget **$265**; perp and spot are **alternative standalone runs**, never summed.
- All risk limits are fractions of equity, read from config (values in Task 1). No market orders anywhere; perp uses IOC limits within a band, spot uses post-only limits.
- `accounts.perp.live_enabled` and `accounts.spot.live_enabled` default `false`; no `KrakenBroker` (live) is built in this plan.
- Funding before the first available funding timestamp is **unknown**: never filled with invented values in main results or optimization (§4).
- Sharpe/Sortino are computed only from **daily** returns and never used in GO/NO-GO (§8).
- Commit after every task with a conventional-commit message ending in:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_0149doVK1pgcUnz8Hk6FiVYX
  ```
- Data and runtime artefacts live under `var/` (git-ignored): `var/bars/{venue}/{asset}.parquet`, `var/funding/{asset}.parquet`, `var/raw/`, `var/reports/<run_id>/`, `var/luna.db`.

## Review Focus

1. **Exchange returns empty/partial data or HTTP 429/5xx mid-run.** Expect: retry with backoff, then skip the tick, never trade on stale bars (R16), never crash. Test in Task 25 (`test_tick_skips_on_feed_error`, `test_stale_bars_block_entries`).
2. **Restart with open positions and open orders.** Expect: state reloaded from SQLite, reconciliation, no duplicate `client_id` submission. Test in Task 25 (`test_restart_resumes_positions_without_duplicate_orders`).
3. **Sized order below exchange minimum (small $265 account).** Expect: candidate skipped with reason `below_min_notional`, counted in report. Test in Task 12 (`test_constructor_skips_below_minimum`).
4. **Warm-up NaNs / asset with missing history inside a window.** Expect: no candidates for that asset, no exceptions, no NaN reaching sizing. Test in Task 15 (`test_strategies_emit_nothing_during_warmup`) and Task 12 (`test_backtest_handles_asset_listed_midway`).
5. **UTC day boundary while bot is down.** Expect: `day_start_equity` reset exactly once per UTC day on the first tick after midnight. Test in Task 9 (`test_day_rollover_resets_once`) and Task 25 (`test_day_start_set_after_downtime`).

---

## File Structure

```
pyproject.toml, uv.lock, .env.example, config/default.yaml
docker/Dockerfile, docker-compose.yml
luna/
  __init__.py
  config.py              # Settings (pydantic), load_settings()
  types.py               # Instrument, Leg, StopSpec, ExitSpec, Candidate, OrderRequest, Fill
  cli.py                 # typer app: backfill, data-report, baseline, walkforward, holdout, paper, label
  http.py                # make_client(), get_json() with retry/backoff
  instruments.py         # perp/spot instrument metadata, asset<->symbol mapping
  universe.py            # select_universe()
  data/store.py          # BarStore, validate_bars(), fill_missing()
  data/kraken_futures.py # fetch_candles(), fetch_funding(), list_perps()
  data/kraken_spot_archive.py  # download_archive(), MultiPartFile, extract_15m()
  data/kraken_spot.py    # fetch_trades_page(), backfill_trades(), aggregate_trades(), fetch_ohlc(), fetch_depth()
  data/report.py         # data integrity report
  features/core.py       # ema, atr, ewma_zscore, realized_vol, trend_strength, rolling_corr
  features/pairs.py      # rolling_beta, spread, half_life
  regime/rules.py        # classify_regime()
  strategy/base.py       # Strategy protocol, SignalFrame
  strategy/naive.py      # NaiveMomentum, NaiveMeanReversion, NaivePairs
  strategy/momentum.py, strategy/mean_reversion.py, strategy/pairs.py
  research/kernel.py     # numba trade simulation kernel
  research/tradelists.py # build_tradelist()
  research/stats.py      # block_bootstrap_ci, benjamini_hochberg, profit_factor, cvar
  research/pairs_select.py
  ranking/expectancy.py, ranking/ranker.py
  portfolio/portfolio.py, portfolio/constructor.py
  planner.py
  risk/engine.py, risk/state.py
  execution/costs.py, execution/sim.py, execution/paper.py
  engine/backtest.py, engine/walkforward.py, engine/live.py, engine/feeds.py
  report/metrics.py, report/gonogo.py, report/html.py
  store/models.py, store/db.py
  notify/telegram.py, notify/heartbeat.py
  labeling.py
dashboard/app.py
tests/ (mirrors luna/; fixtures in tests/fixtures/)
```

---

# P0 — Data integrity

### Task 1: Scaffold, config, core types, CLI, Docker skeleton

**Files:**
- Create: `pyproject.toml`, `config/default.yaml`, `.env.example`, `luna/__init__.py`, `luna/config.py`, `luna/types.py`, `luna/cli.py`, `docker/Dockerfile`, `docker-compose.yml`, `tests/test_config.py`, `tests/test_types.py`

**Interfaces:**
- Produces:
  ```python
  # luna/types.py
  Venue = Literal["kraken_futures", "kraken_spot"]; AccountKind = Literal["perp", "spot"]; Side = Literal["buy", "sell"]
  @dataclass(frozen=True) class Instrument: asset: str; symbol: str; venue: Venue; kind: AccountKind; qty_step: float; price_tick: float; min_qty: float; min_notional: float; maker_fee: float; taker_fee: float; initial_margin: float = 0.0; maintenance_margin: float = 0.0
  @dataclass(frozen=True) class Leg: asset: str; weight: float          # signed weight per unit of direction; pairs: (A, +1), (B, -beta)
  @dataclass(frozen=True) class StopSpec: kind: Literal["price", "zscore"]; level: float; distance_frac: float   # distance_frac = loss fraction of gross notional if hit
  @dataclass(frozen=True) class ExitSpec: max_hold_bars: int; take_profit_z: float | None = None; trailing_atr_mult: float | None = None
  @dataclass(frozen=True) class Candidate: ts: pd.Timestamp; strategy: str; key: str; legs: tuple[Leg, ...]; direction: int; entry_ref: float; stop: StopSpec; exit: ExitSpec; regime: str; reason: str; features: dict[str, float]; score: float = 0.0; edge_bps: float = 0.0
  @dataclass(frozen=True) class OrderRequest: client_id: str; ts: pd.Timestamp; asset: str; side: Side; qty: float; limit_price: float; tif: Literal["IOC", "POST_ONLY"]; intent: Literal["open", "close", "reduce"]; position_id: str; leg_group: str
  @dataclass(frozen=True) class Fill: client_id: str; ts: pd.Timestamp; asset: str; side: Side; qty: float; price: float; fee: float; liquidity: Literal["maker", "taker"]
  # luna/config.py
  class RiskLimits(BaseModel): capital_usd: float; risk_per_trade: float; max_risk_per_trade: float; max_open_risk: float; max_positions: int; max_gross_leverage: float; max_leg_notional_frac: float; daily_loss_limit: float; max_drawdown: float; max_orders_per_day: int; max_price_band: float; emergency_stop_frac: float; liq_distance_mult: float; allow_short: bool; live_enabled: bool; highvol_risk_mult: float; corr_bucket_threshold: float
  class CostConfig(BaseModel): maker_fee: float; taker_fee: float; half_spread_bps: dict[str, float]; slippage_bps: float     # half_spread_bps has key "default"
  class Settings(BaseSettings): var_dir: Path; accounts: dict[AccountKind, RiskLimits]; costs: dict[AccountKind, CostConfig]; universe: UniverseConfig; walkforward: WalkForwardConfig; ranking: RankingConfig; pairs: PairsConfig; telegram_token: str | None; telegram_chat_id: str | None; healthcheck_url: str | None; gbp_usd: float
  def load_settings(path: Path | None = None) -> Settings   # YAML defaults + env overrides (LUNA_ prefix, .env)
  ```
- Values in `config/default.yaml` (verbatim from spec):
  - perp: capital_usd 265, risk_per_trade 0.0075, max_risk_per_trade 0.01, max_open_risk 0.03, max_positions 3, max_gross_leverage 2.0, max_leg_notional_frac 1.0, daily_loss_limit 0.02, max_drawdown 0.15, max_orders_per_day 30, max_price_band 0.003, emergency_stop_frac 0.02, liq_distance_mult 10, allow_short true, live_enabled false, highvol_risk_mult 0.5, corr_bucket_threshold 0.7
  - spot: same except max_gross_leverage 1.0, max_leg_notional_frac 0.5, max_orders_per_day 20, max_price_band 0.005, allow_short false
  - costs.perp: maker 0.0002, taker 0.0005, half_spread_bps {BTC: 1, ETH: 1, default: 3}, slippage_bps 2; costs.spot: maker 0.0025, taker 0.004, half_spread_bps {BTC: 2, ETH: 3, default: 3}, slippage_bps 0
  - universe: min_adv_usd 10_000_000, min_history_days 548, prefilter_volume_usd 3_000_000, history_start "2023-01-01"
  - walkforward: train_months 6, test_months 2, step_months 2, holdout_months 3
  - ranking: n0 50, n_min 30, bootstrap_samples 2000, ci 0.90
  - pairs: corr_min 0.7, fdr_q 0.05, stability_p 0.10, half_life_min 8, half_life_max 192, beta_change_max 0.25, max_pairs 5, always_watch ["ETH/BTC"]
  - gbp_usd 1.32

- [ ] **Step 1:** `pip install uv`; `uv init --package --name luna` content replaced by our `pyproject.toml` (deps from Tech Stack; dev deps pytest, ruff; `[tool.pytest.ini_options] addopts = "-m 'not network'"`, markers `network`); `uv python install 3.12`; `uv sync`.
- [ ] **Step 2: Write failing tests**
  ```python
  def test_default_settings_match_spec():
      s = load_settings()
      assert s.accounts["perp"].max_gross_leverage == 2.0 and s.accounts["spot"].max_gross_leverage == 1.0
      assert s.accounts["perp"].capital_usd == 265 and s.accounts["spot"].allow_short is False
      assert s.accounts["perp"].live_enabled is False
      assert s.ranking.n_min == 30 and s.pairs.fdr_q == 0.05
  def test_env_override(monkeypatch): monkeypatch.setenv("LUNA_GBP_USD", "1.25"); assert load_settings().gbp_usd == 1.25
  def test_candidate_is_frozen(): ... pytest.raises(FrozenInstanceError)
  def test_cli_help_lists_commands():  # typer CliRunner
      out = CliRunner().invoke(app, ["--help"]).output
      for c in ["backfill", "data-report", "baseline", "walkforward", "holdout", "paper", "label"]: assert c in out
  ```
- [ ] **Step 3:** Run `uv run pytest -q` → FAIL (imports missing).
- [ ] **Step 4:** Implement `types.py`, `config.py`, `cli.py` (commands as stubs that raise `typer.Exit(1)` with "not implemented" until their task), YAML, `.env.example` (LUNA_TELEGRAM_TOKEN, LUNA_TELEGRAM_CHAT_ID, LUNA_HEALTHCHECK_URL). Dockerfile: `python:3.12-slim`, install uv, `uv sync --frozen --no-dev`, non-root user `luna`, workdir `/app`, entrypoint `uv run luna`. Compose: services `bot-perp` (`paper --account perp`), `bot-spot` (`paper --account spot`), `dashboard` (`streamlit run dashboard/app.py --server.port 8501`, port 8501), volume `./var:/app/var`, `env_file: .env` (required: false), `restart: unless-stopped` on bots.
- [ ] **Step 5:** `uv run pytest -q` → PASS; `uv run ruff check .` → clean.
- [ ] **Step 6:** Commit `chore: scaffold luna package, config, types, cli, docker`.

### Task 2: Bar store and validation

**Files:** Create `luna/data/store.py`, `tests/data/test_store.py`

**Interfaces:**
- Produces:
  ```python
  class BarStore:
      def __init__(self, root: Path)                                    # root = settings.var_dir
      def read(self, venue: Venue, asset: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> pd.DataFrame
      def write(self, venue: Venue, asset: str, df: pd.DataFrame) -> None          # overwrite
      def append(self, venue: Venue, asset: str, df: pd.DataFrame) -> int          # merge, dedupe on index (new wins), sort; returns rows added
      def assets(self, venue: Venue) -> list[str]
      def read_funding(self, asset: str) -> pd.DataFrame; def write_funding(self, asset: str, df: pd.DataFrame) -> None  # index hourly UTC, column "rate"
  @dataclass class ValidationReport: rows: int; start: pd.Timestamp; end: pd.Timestamp; duplicates: int; non_monotonic: int; gaps: int; filled_bars: int; spikes: list[pd.Timestamp]; ok: bool
  def fill_missing(df: pd.DataFrame, freq: str = "15min") -> pd.DataFrame   # reindex full range; missing bars: OHLC = prev close, volume=0, trades=0, filled=True
  def validate_bars(df: pd.DataFrame, spike_sigma: float = 10.0) -> ValidationReport  # spike: |log return| > spike_sigma * rolling(2880) std
  ```
- [ ] **Step 1: Failing tests:** `test_append_dedupes_new_wins`, `test_read_slices_by_time`, `test_fill_missing_marks_filled_and_carries_close` (gap of 2 bars → 2 rows filled=True, open=high=low=close=prev close, volume 0), `test_validate_detects_duplicates_gaps_spikes` (inject 1 duplicate, 1 gap, one ×3 price jump → counts 1,1 and spike timestamp listed, `ok is False`), `test_validate_clean_series_ok`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (Parquet via pandas/pyarrow, path `var/bars/{venue}/{asset}.parquet`). **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(data): parquet bar store with validation`.

### Task 3: HTTP helper + Kraken Futures history client

**Files:** Create `luna/http.py`, `luna/instruments.py`, `luna/data/kraken_futures.py`, `tests/data/test_kraken_futures.py`, `tests/fixtures/kf_*.json`

**Interfaces:**
- Produces:
  ```python
  # luna/http.py
  def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client
  def get_json(client: httpx.Client, url: str, params: dict | None = None, retries: int = 3, backoff: float = 1.0) -> dict  # retries on 429/5xx/timeouts, exponential backoff; raises httpx.HTTPError after
  # luna/instruments.py
  def perp_symbol(asset: str) -> str; def spot_pair(asset: str) -> str; def asset_from_perp(symbol: str) -> str
  def perp_instrument(meta: dict, costs: CostConfig) -> Instrument   # from /derivatives/api/v3/instruments entry
  # luna/data/kraken_futures.py
  BASE = "https://futures.kraken.com"
  def fetch_candles(client, asset: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame   # /api/charts/v1/trade/{symbol}/15m?from&to (seconds); paginate while more_candles, 2000/page; returns bar frame (trades=0, filled=False)
  def fetch_funding(client, asset: str) -> pd.DataFrame   # /derivatives/api/v4/historicalfundingrates?symbol=; column rate = relativeFundingRate; hourly index
  def list_perps(client, costs: CostConfig) -> list[tuple[Instrument, float]]   # (instrument, 24h quote volume USD) for PF_*USD from /derivatives/api/v3/instruments + tickers
  ```
- [ ] **Step 1: Failing tests** (use `httpx.MockTransport` with recorded fixtures): `test_fetch_candles_paginates_until_no_more` (two pages, `more_candles` True then False → concatenated, no duplicate boundary bar, UTC index), `test_fetch_funding_parses_relative_rate`, `test_get_json_retries_on_429_then_succeeds` (429, 429, 200 → returns, 3 calls; `backoff=0`), `test_get_json_raises_after_retries`, `test_symbol_mapping` (`perp_symbol("BTC") == "PF_XBTUSD"`, `spot_pair("BTC") == "XBTUSD"`, `asset_from_perp("PF_ETHUSD") == "ETH"`), `test_list_perps_builds_instruments` (min_qty from `contractValueTradePrecision` → `10**-p`, tick from `tickSize`, IM/MM from `marginLevels[0]`).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. Record fixtures by trimming real responses (2–3 candles each). **Step 4:** Run → PASS.
- [ ] **Step 5:** Add `@pytest.mark.network test_live_candles_smoke` (fetch 1 day of PF_XBTUSD, expect ~96 rows). Run it once manually: `uv run pytest -m network -k smoke -q` → PASS.
- [ ] **Step 6:** Commit `feat(data): kraken futures candles and funding client`.

### Task 4: Spot OHLCVT archive (download, verify, extract)

**Files:** Create `luna/data/kraken_spot_archive.py`, `tests/data/test_spot_archive.py`

**Interfaces:**
- Produces:
  ```python
  ARCHIVE_BASE = "https://assets.kraken.com/marketing/institutions/"
  PARTS = [f"Kraken_OHLCVT_Full_2026Q2.zip.part0{i}" for i in range(5)]; SUMS = "OHLCVT_Full_PARTS_SHA256SUMS.txt"
  def download_archive(client, dest: Path, parts: list[str] = PARTS) -> list[Path]   # resumable (HTTP Range), verifies SHA256 per part, raises ChecksumError
  class MultiPartFile(io.RawIOBase): def __init__(self, paths: list[Path])          # seekable read-only view of concatenated parts
  def extract_15m(parts: list[Path], pairs: list[str]) -> dict[str, pd.DataFrame]   # zip members matching "{pair}_15.csv" (any folder); CSV no header: time(s),open,high,low,close,volume,trades → bar frame filled=False
  ```
- [ ] **Step 1: Failing tests:** build a tiny zip in `tmp_path` containing `TIMEANDSALES/XBTUSD_15.csv` and `XBTUSD_60.csv`, split bytes into 3 parts → `test_multipart_reads_like_single_file`, `test_extract_15m_only_requested_pairs` (returns only XBTUSD 15m with correct UTC index and columns), `test_checksum_mismatch_raises`, `test_download_resumes_with_range` (MockTransport asserts `Range: bytes=N-` when partial file exists).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (stream to disk in 8 MB chunks; never load a part fully into memory). **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(data): kraken spot OHLCVT archive downloader and extractor`.

### Task 5: Spot trades gap-fill + public live endpoints

**Files:** Create `luna/data/kraken_spot.py`, `tests/data/test_kraken_spot.py`, fixtures

**Interfaces:**
- Produces:
  ```python
  SPOT_BASE = "https://api.kraken.com/0/public"
  def fetch_trades_page(client, pair: str, since_ns: int) -> tuple[pd.DataFrame, int]  # columns price, volume, time(UTC); returns (trades, last cursor)
  def aggregate_trades(trades: pd.DataFrame) -> pd.DataFrame                            # → 15m bar frame (trades=count, filled=False; empty bars omitted)
  def backfill_trades(client, pair: str, start: pd.Timestamp, end: pd.Timestamp, checkpoint: Path, sleep_s: float = 1.0) -> pd.DataFrame  # resumes from checkpoint JSON {"since": ...}; stops when trade time >= end
  def fetch_ohlc(client, pair: str) -> pd.DataFrame        # /OHLC interval=15, last ≤720 bars; drops the still-open last bar
  def fetch_depth(client, pair: str, count: int = 10) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]  # bids, asks (price, qty)
  ```
- [ ] **Step 1: Failing tests:** `test_aggregate_trades_ohlcv` (6 trades across 2 bars → exact OHLC, volume sum, trades count), `test_backfill_resumes_from_checkpoint` (checkpoint since=X → first request uses X; checkpoint updated after each page), `test_fetch_ohlc_drops_open_bar`, `test_fetch_depth_parses`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(data): spot trades gap-fill and public spot endpoints`.

### Task 6: Universe, `backfill` and `data-report` commands, run real backfill

**Files:** Create `luna/universe.py`, `luna/data/report.py`; Modify `luna/cli.py`; Test `tests/test_universe.py`, `tests/data/test_report.py`

**Interfaces:**
- Produces:
  ```python
  def adv_usd(bars: pd.DataFrame, asof: pd.Timestamp, days: int = 30) -> float        # mean daily (volume*close) over [asof-days, asof)
  def select_universe(panel: dict[str, pd.DataFrame], asof: pd.Timestamp, min_adv_usd: float, min_history_days: int) -> list[str]  # only data < asof; sorted by adv desc
  def build_data_report(store: BarStore, out: Path) -> Path    # markdown: per venue/asset ValidationReport, funding coverage (first/last ts), spot-vs-perp basis stats (median/p95 |log(spot/perp)| close), spread proxy
  ```
- CLI: `luna backfill --venue perp` (list_perps → prefilter 24h volume ≥ prefilter_volume_usd → fetch candles from history_start + funding → store); `luna backfill --venue spot --assets <from perp store>` (archive download to `var/raw/` → extract 15m for each `spot_pair` that exists → gap-fill from 2026-07-01 via `backfill_trades` → append; then `fetch_ohlc` to top up). `luna data-report` writes `var/reports/data/data_report.md`.
- [ ] **Step 1: Failing tests:** `test_select_universe_uses_only_past_data` (asset whose volume is huge only after asof is excluded), `test_select_universe_requires_history` (listed 100 days before asof → excluded with min_history_days 548), `test_data_report_contains_sections` (tmp store with 2 assets both venues → report has "Validation", "Funding coverage", "Spot vs perp basis").
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Run real backfill (background, long-running):** `uv run luna backfill --venue perp` then `uv run luna backfill --venue spot` (run with `run_in_background`; spot download ~10 GB, gap-fill may take hours — proceed with Tasks 7+ meanwhile). When finished: `uv run luna data-report`; open the report and confirm every universe asset has `ok` or explained gaps, and funding coverage ≈ last 12 months.
- [ ] **Step 6:** Commit `feat(data): universe selection, backfill and data integrity report`.

# P0.5 — Core machinery + naive baselines

### Task 7: Features (causal)

**Files:** Create `luna/features/core.py`, `luna/features/pairs.py`, `tests/features/test_features.py`, `tests/helpers.py`

**Interfaces:**
- Produces (all take/return pandas Series aligned to the input index; NaN during warm-up):
  ```python
  def ema(x: pd.Series, span: int) -> pd.Series
  def atr(bars: pd.DataFrame, n: int) -> pd.Series                         # Wilder ATR on high/low/close
  def ewma_zscore(x: pd.Series, span: int) -> pd.Series                    # (x - ewm mean) / ewm std, min_periods=span
  def realized_vol(close: pd.Series, window: int) -> pd.Series             # rolling std of log returns
  def trend_strength(close: pd.Series, bars: pd.DataFrame, fast: int = 96, slow: int = 480, atr_n: int = 96) -> pd.Series  # (ema_fast - ema_slow)/atr, signed
  def rolling_corr(a: pd.Series, b: pd.Series, window: int) -> pd.Series   # of log returns
  def rolling_beta(log_a: pd.Series, log_b: pd.Series, window: int) -> pd.Series   # OLS slope of log_a on log_b
  def spread(log_a: pd.Series, log_b: pd.Series, beta: pd.Series | float) -> pd.Series
  def half_life(s: pd.Series) -> float                                     # AR(1) on Δs = a + b s_{t-1}; inf if b >= 0
  # tests/helpers.py
  def assert_causal(fn: Callable[[pd.DataFrame], pd.Series | pd.DataFrame], df: pd.DataFrame, cut_points: int = 5) -> None  # fn(df[:k]) equals fn(df)[:k] for random k
  def synthetic_bars(n: int, kind: Literal["random_walk", "trend", "ou"], seed: int = 0, **kw) -> pd.DataFrame
  def synthetic_pair(n: int, kind: Literal["cointegrated", "independent", "constant_spread"], seed: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]
  ```
- [ ] **Step 1: Failing tests:** each feature against a hand-computed small example (`ema` vs pandas ewm adjust=False, `atr` on 5-bar fixture, `half_life` of simulated AR(1) with b=-0.1 ≈ ln2/0.105 ±10%, `half_life` of random walk is large or inf), `test_all_features_causal` (assert_causal on every feature, including rolling_beta), `test_rolling_beta_recovers_true_beta` (cointegrated synthetic with β=0.8 → mean estimate within 0.05).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(features): causal feature library`.

### Task 8: Cost model + portfolio accounting

**Files:** Create `luna/execution/costs.py`, `luna/portfolio/portfolio.py`, tests `tests/portfolio/test_portfolio.py`, `tests/execution/test_costs.py`

**Interfaces:**
- Produces:
  ```python
  class CostModel:
      def __init__(self, cfg: CostConfig)
      def half_spread(self, asset: str) -> float                 # fraction
      def fee(self, notional: float, liquidity: Literal["maker", "taker"]) -> float
      def round_trip_frac(self, asset: str, liquidity: Literal["maker", "taker"] = "taker") -> float   # 2*(fee + half_spread + slippage)
  @dataclass class Position: id: str; strategy: str; key: str; legs: tuple[Leg, ...]; direction: int; qty: dict[str, float]; entry_price: dict[str, float]; entry_ts: pd.Timestamp; entry_ref: float; stop: StopSpec; exit: ExitSpec; risk_amount: float; regime: str; trail_level: float | None = None; bars_held: int = 0; pnl: dict[str, float] = field(default_factory=lambda: {"gross": 0.0, "fees": 0.0, "spread_slip": 0.0, "funding": 0.0, "funding_unknown_bars": 0.0})
  class Portfolio:
      def __init__(self, kind: AccountKind, capital_usd: float)
      cash: float; holdings: dict[str, float]  # signed qty per asset (spot: >= 0)
      positions: dict[str, Position]
      def apply_fill(self, fill: Fill, position_id: str, ref_price: float) -> None  # ref_price = mid at decision; spread_slip cost = |price-ref|*qty attributed to position
      def apply_funding(self, asset: str, rate: float | None, mark: float, ts: pd.Timestamp) -> None  # perp only; long pays rate*notional; rate None → count unknown, no cash change
      def mark(self, prices: dict[str, float]) -> None
      def equity(self) -> float          # spot: cash + Σ qty*price; perp: cash + Σ unrealized
      def gross_notional(self) -> float; def gross_leverage(self) -> float
      def open_risk(self) -> float       # Σ position.risk_amount for open positions
      def liquidation_distance(self, asset: str, maintenance_margin: float) -> float   # perp: approx fractional adverse move to liquidation under cross margin = (equity - Σ notional*mm) / |notional of asset|
      def close_position(self, position_id: str, ts: pd.Timestamp, reason: str) -> Position   # moves to closed list; requires all legs flat
      closed: list[tuple[Position, pd.Timestamp, str]]
  ```
  Perp accounting: avg-entry per asset; realized P&L to cash on reduce; fees from cash. Spot: buy reduces cash by price*qty+fee; sell adds; selling more than held raises `ValueError`.
- [ ] **Step 1: Failing tests:** `test_perp_long_then_close_realizes_pnl` (buy 1 @100 fee 0.05, sell 1 @110 → cash +10 −fees), `test_perp_short_pnl_sign`, `test_partial_close_and_sign_flip` (long 2, sell 3 → short 1 with new avg entry), `test_spot_cannot_go_negative`, `test_spot_equity_marks_holdings`, `test_funding_long_pays_short_receives`, `test_funding_unknown_counts_without_cash`, `test_gross_leverage`, `test_cost_round_trip` (perp BTC taker: 2*(0.0005+0.0001+0.0002)=0.0016).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(portfolio): cost model and perp/spot accounting`.

### Task 9: Risk engine (pure) + risk state

**Files:** Create `luna/risk/engine.py`, `luna/risk/state.py`, `tests/risk/test_engine.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass class RiskState: kill_switch: bool = False; paused: bool = False; day: date | None = None; day_start_equity: float | None = None; peak_equity: float | None = None; orders_today: int = 0
  def roll_day(state: RiskState, now: pd.Timestamp, equity: float) -> RiskState   # new UTC day → day=now.date(), day_start_equity=equity, orders_today=0; peak=max(peak, equity)
  @dataclass(frozen=True) class OrderCheck: order: OrderRequest; notional: float; new_position_risk: float; new_position_risk_frac: float; stop_distance_frac: float | None; has_stop: bool; increases_abs_position: bool; mid: float
  @dataclass(frozen=True) class RiskContext: limits: RiskLimits; kind: AccountKind; mode: Literal["backtest", "paper", "live"]; state: RiskState; equity: float; open_positions: int; open_risk: float; gross_notional: float; holdings: dict[str, float]; last_bar_ts: pd.Timestamp; now: pd.Timestamp; liq_distance: dict[str, float]; min_qty: dict[str, float]; min_notional: dict[str, float]; available_cash: float; reconciliation_ok: bool = True
  @dataclass class RiskResult: approved: list[OrderRequest]; vetoed: list[tuple[OrderRequest, str]]   # reason = rule id e.g. "R8_gross_leverage"
  def portfolio_actions(ctx: RiskContext) -> list[Literal["FLATTEN_AND_KILL", "BLOCK_ENTRIES_TODAY", "PAUSE_RECONCILIATION"]]   # R4 drawdown from peak ≥ max_drawdown; R3 day loss ≥ daily_loss_limit; R18
  def check(orders: list[OrderCheck], ctx: RiskContext) -> RiskResult    # rules in spec §6.1 order; exemptions for intent close/reduce exactly as spec
  ```
  Rule ids: `R1_kill, R2_pause, R3_daily_loss, R4_drawdown, R5_risk_per_trade, R6_open_risk, R7_max_positions, R8_gross_leverage, R9_leg_notional, R10_pyramiding, R11_no_stop, R12_liquidation, R13_spot_short, R14_orders_per_day, R15_price_band, R16_stale_data, R17_minimums_balance, R18_reconciliation, R19_live_disabled`. Orders are evaluated sequentially; approved orders update a running copy of exposure/positions/orders_today so a batch cannot jointly exceed limits.
- [ ] **Step 1: Failing tests** — one parametrized table `test_rule_vetoes[rule]` with, for each R1–R19, a minimal context that trips exactly that rule (assert reason prefix) and a sibling case that passes; plus: `test_close_orders_exempt_from_daily_loss_and_leverage`, `test_close_orders_blocked_by_pause_but_not_by_kill_flatten` (portfolio_actions returns FLATTEN_AND_KILL; check approves `intent="close"` when kill_switch set by that action — model: kill blocks `open` only), `test_batch_cannot_exceed_leverage_jointly` (two orders each fine alone, together >2x → second vetoed), `test_drawdown_from_peak_triggers_flatten_and_kill`, `test_day_rollover_resets_once` (two calls same UTC day → day_start unchanged; first call next day → reset; downtime across two midnights → reset once to current equity), `test_live_disabled_vetoes_live_mode_only`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(risk): deterministic pure risk engine`.

### Task 10: Order planner + SimBroker

**Files:** Create `luna/planner.py`, `luna/execution/sim.py`, `tests/execution/test_sim.py`, `tests/test_planner.py`

**Interfaces:**
- Consumes: `Instrument`, `CostModel`, `Portfolio`, `Candidate`, `Position`.
- Produces:
  ```python
  def round_qty(qty: float, step: float) -> float      # toward zero
  def plan_entry(cand: Candidate, gross_notional: float, mids: dict[str, float], instruments: dict[str, Instrument], kind: AccountKind, ts: pd.Timestamp, band: float) -> list[OrderRequest]   # per-leg notional = gross * |w| / Σ|w|; IOC for perp (limit = mid*(1±band)), POST_ONLY for spot (limit = mid*(1∓half_spread)); intent "open"; shared position_id/leg_group
  def plan_exit(pos: Position, mids: dict[str, float], instruments, kind, ts, band: float) -> list[OrderRequest]   # flatten every leg, intent "close"
  class SimBroker:
      def __init__(self, kind: AccountKind, costs: CostModel, instruments: dict[str, Instrument])
      def submit(self, order: OrderRequest) -> None
      def on_bar(self, ts: pd.Timestamp, bars: dict[str, pd.Series]) -> list[Fill]
          # IOC: fill at bar open * (1 ± (half_spread + slippage)), taker fee; reject if that price is beyond limit.
          # POST_ONLY: fill at limit only if bar trades THROUGH it (buy: low < limit; sell: high > limit), maker fee; else cancel at end of bar.
          # Bars with filled=True never fill anything.
      def stop_fill(self, asset: str, side: Side, qty: float, stop_price: float, bar: pd.Series) -> Fill   # emergency stop: price = worse of (stop_price, bar.open) adjusted by slippage+half_spread, taker
  ```
- [ ] **Step 1: Failing tests:** `test_ioc_fills_at_next_open_with_costs`, `test_ioc_rejected_outside_band`, `test_post_only_requires_trade_through` (low == limit → no fill; low < limit → fill at limit, maker fee), `test_no_fill_on_filled_bar`, `test_stop_fill_gap_uses_open` (gap below stop → fill at open minus costs), `test_plan_entry_pairs_weights` (legs ETH +1, BTC −0.8, gross 180 → ETH notional 100 buy, BTC 80 sell), `test_plan_entry_rounds_to_step`, `test_plan_exit_flattens_all_legs`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(execution): order planner and pessimistic SimBroker`.

### Task 11: Strategy protocol + naive baselines

**Files:** Create `luna/strategy/base.py`, `luna/strategy/naive.py`, `tests/strategy/test_naive.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass class SignalFrame:          # one per (strategy, key); all columns aligned to bar index
      key: str; legs_beta: pd.Series | None   # pairs: beta series for leg B (None → single asset)
      entry: pd.Series        # int8: +1 long / -1 short / 0, evaluated at bar close
      exit_long: pd.Series; exit_short: pd.Series   # bool, evaluated at bar close
      stop_distance: pd.Series   # fraction of gross notional lost if stop hit, at entry
      stop_kind: Literal["price", "zscore"]; stop_z: float | None    # for zscore stops: |z| level
      z: pd.Series | None; atr: pd.Series | None
      exit_spec: ExitSpec; regime: pd.Series   # str per bar ("NONE" when not used)
  class Strategy(Protocol):
      name: str; params: dict
      def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]   # vectorized, causal
      def candidate_at(self, sf: SignalFrame, i: int, mids: dict[str, float]) -> Candidate | None  # build Candidate from row i if entry != 0 and no NaN
      def should_exit(self, sf: SignalFrame, i: int, pos: Position, bar_close: dict[str, float]) -> str | None  # "signal" | "stop" | "time" | "trailing" | None
  ```
  Naive params (spec §8.1): `NaiveMomentum` EMA(96)/EMA(480) cross, stop 3×ATR(96) (price stop, `stop_distance = 3*atr/close`), max_hold 2880; `NaiveMeanReversion` EWMA(96) z: enter |z|>2 opposite, exit |z|<0.5, z-stop |z|>3.5, max_hold 288; `NaivePairs` ETH/BTC β=1, z span 288, enter |z|>2, exit |z|<0.5, stop |z|>3.5, max_hold 288. On spot (`allow_short=False`) the backtest maps −1 entries to nothing (handled by caller, not strategy). Regime column = "NONE".
- [ ] **Step 1: Failing tests:** `test_naive_signals_causal` (assert_causal on entry/exit columns), `test_naive_momentum_enters_on_cross` (constructed series crossing at bar k → entry[k] == +1 only at k), `test_naive_mr_exit_and_stop_flags`, `test_naive_pairs_uses_beta_one`, `test_candidate_at_none_on_nan`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(strategy): strategy protocol and naive baselines`.

### Task 12: Constructor, backtest engine, metrics/stats

**Files:** Create `luna/portfolio/constructor.py`, `luna/engine/backtest.py`, `luna/report/metrics.py`, `luna/research/stats.py`, `luna/ranking/ranker.py` (only `AcceptAllRanker`, `RandomRanker` here), tests `tests/engine/test_backtest.py`, `tests/portfolio/test_constructor.py`, `tests/research/test_stats.py`

**Interfaces:**
- Produces:
  ```python
  class Ranker(Protocol):
      def score(self, cand: Candidate, ts: pd.Timestamp) -> Candidate | None   # returns candidate with score/edge_bps filled, or None = rejected (reason recorded by caller as "ranker")
  class AcceptAllRanker: ...   # score = 1/stop_distance, edge 0
  class RandomRanker: def __init__(self, seed: int)
  @dataclass class Selection: cand: Candidate; gross_notional: float; risk_amount: float
  def construct(cands: list[Candidate], pf: Portfolio, limits: RiskLimits, costs: CostModel, instruments: dict[str, Instrument], mids: dict[str, float], returns_30d: pd.DataFrame, regime_mult: Callable[[Candidate], float]) -> tuple[list[Selection], list[tuple[Candidate, str]]]
      # sort by score desc; skip reasons: "max_positions", "open_risk", "corr_bucket", "opposite_exposure", "below_min_notional", "short_not_allowed"
      # risk_amt = risk_per_trade*equity*regime_mult; gross = risk_amt / (stop_distance + round_trip); clip to leverage/leg caps
  @dataclass class BacktestResult: equity: pd.Series; trades: pd.DataFrame; candidates: pd.DataFrame; risk_events: pd.DataFrame; funding_known_from: dict[str, pd.Timestamp]
      # trades columns: position_id, strategy, key, direction, entry_ts, exit_ts, exit_reason, gross_notional, risk_amount, pnl_gross, fees, spread_slip, funding, funding_unknown_bars, pnl_net, r_multiple, regime
  def run_backtest(panel: dict[str, pd.DataFrame], funding: dict[str, pd.DataFrame], strategies: list[Strategy], keys: dict[str, list[str]], ranker: Ranker | Callable[[pd.Timestamp], Ranker], kind: AccountKind, settings: Settings, start: pd.Timestamp, end: pd.Timestamp, regime: dict[str, pd.Series] | None = None, cost_mult: float = 1.0, funding_mode: Literal["known_only", "zero", "estimated", "estimated_x2"] = "known_only", initial_portfolio: Portfolio | None = None) -> BacktestResult
      # loop per bar t in [start, end): (1) fills of orders submitted at t-1 via SimBroker.on_bar(t); (2) emergency stops intrabar on bar t (loss ≥ emergency_stop_frac*equity at bar low/high); (3) funding at hour boundaries; (4) mark at close; roll_day; portfolio_actions; (5) exits via should_exit at close; (6) candidates → ranker → construct → plan_entry → risk.check → submit. Spot: entries with direction -1 dropped with reason "short_not_allowed".
  # metrics.py
  def daily_returns(equity: pd.Series) -> pd.Series
  def summary(res: BacktestResult) -> dict[str, float]   # n_trades, expectancy_r, expectancy_bps, profit_factor, max_dd, worst_day, cvar_5_daily, turnover, exposure, win_rate, sharpe_daily, sortino_daily, pnl_gross/fees/spread_slip/funding/net, corr_btc
  # stats.py
  def block_bootstrap_ci(values: np.ndarray, days: np.ndarray, n: int = 2000, ci: float = 0.90, seed: int = 0) -> tuple[float, float, float]   # (mean, lo, hi); resample whole days
  def benjamini_hochberg(pvals: np.ndarray, q: float) -> np.ndarray  # bool mask
  def profit_factor(pnl: np.ndarray) -> float; def cvar(x: np.ndarray, alpha: float = 0.05) -> float
  ```
- [ ] **Step 1: Failing tests:**
  - stats: `test_bh_known_example` (p=[.01,.02,.03,.04,.5], q=.05 → first four True), `test_bootstrap_ci_contains_mean`, `test_profit_factor`, `test_cvar`.
  - constructor: `test_constructor_respects_max_positions`, `test_constructor_corr_bucket` (two 0.9-correlated same-direction longs → second rejected "corr_bucket"), `test_constructor_sizing_example` (equity 265, risk .0075, stop .02, round-trip .0015 → gross ≈ 92.4), `test_constructor_skips_below_minimum`, `test_constructor_clips_to_leverage`.
  - engine (synthetic, via tests/helpers): `test_random_walk_net_negative` (NaiveMR on random walk, 20k bars: |mean gross R| small, net < 0), `test_trend_momentum_profits_gross` (NaiveMomentum on trending series, gross > 0), `test_constant_spread_pairs_loses_costs` (NaivePairs on constant_spread pair with forced entries: pnl_net ≈ −(fees+spread_slip)), `test_risk_invariants_hold_every_bar` (assert gross_leverage ≤ limit and open positions ≤ 3 on every bar of a 5k-bar multi-asset run), `test_no_lookahead_engine` (truncating panel at k leaves trades with exit < k unchanged), `test_backtest_handles_asset_listed_midway` (asset starting halfway: no error, no trades before listing+warmup), `test_funding_unknown_flagged` (funding starts mid-run → trades before it have funding_unknown_bars > 0 and funding == 0 under known_only).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(engine): portfolio constructor, backtest engine, metrics and stats`.

### Task 13: Baseline command + first real baseline run (P0.5 deliverable)

**Files:** Modify `luna/cli.py`; Create `luna/report/html.py` (minimal: summary table + equity plot + P&L breakdown; extended in Task 20), `tests/test_cli_baseline.py`

**Interfaces:**
- Produces: `def render_report(results: dict[str, BacktestResult], out_dir: Path, title: str, extra_sections: list[tuple[str, str]] | None = None) -> Path` (plotly, self-contained HTML; `extra_sections` = (heading, html)).
- CLI: `luna baseline --account perp|spot [--start --end]` runs the three naive strategies together with `AcceptAllRanker` on the universe selected at start (perp: universe from perp store; spot: same assets that exist in spot store), funding_mode known_only; also each naive strategy alone. Writes `var/reports/baseline_<account>_<ts>/report.html` + `summary.json`.
- [ ] **Step 1: Failing test:** `test_baseline_cli_on_fixture_store` (tmp store with 3 synthetic assets incl. BTC/ETH both venues → exit code 0, report.html and summary.json exist, summary has keys "combined", "naive_momentum", "naive_mean_reversion", "naive_pairs"; spot summary has no short trades).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5 (real data, after Task 6 backfill finished):** `uv run luna baseline --account perp` and `--account spot`; record headline numbers (net expectancy R + CI, PF, max DD, n_trades per baseline) in `docs/results/baseline.md`.
- [ ] **Step 6:** Commit `feat: naive baseline runs and report (P0.5)`.

# P1 — Strategy correctness

### Task 14: Regime rules

**Files:** Create `luna/regime/rules.py`, `tests/regime/test_rules.py`

**Interfaces:**
- Produces: `def classify_regime(bars: pd.DataFrame, trend_pct: float = 0.70, vol_pct: float = 0.90, lookback_bars: int = 8640) -> pd.Series` → values `TREND_UP | TREND_DOWN | RANGE | HIGH_VOL`. `|trend_strength|` above its **rolling** (lookback, causal) `trend_pct` quantile → trend by sign; 1-day realized vol above its rolling `vol_pct` quantile → `HIGH_VOL` (takes precedence). Fixed percentiles, never optimized (§5.4). NaN warm-up → `"RANGE"` is **not** allowed; warm-up → `"UNKNOWN"` (strategies emit nothing).
- [ ] **Step 1: Failing tests:** `test_regime_causal`, `test_trend_series_labeled_trend`, `test_flat_noise_labeled_range`, `test_vol_spike_labeled_high_vol`, `test_warmup_unknown`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(regime): rule-based causal regime classifier`.

### Task 15: Momentum, mean-reversion and pairs strategies

**Files:** Create `luna/strategy/momentum.py`, `luna/strategy/mean_reversion.py`, `luna/strategy/pairs.py`, `tests/strategy/test_strategies.py`

**Interfaces:**
- Consumes: `Strategy`/`SignalFrame` (Task 11), features (Task 7), `classify_regime` (Task 14).
- Produces: classes `Momentum(params)`, `MeanReversion(params)`, `Pairs(params)` each with `GRID: list[dict]` exactly per spec §5.1–5.3:
  - Momentum grid: `fast_slow ∈ {(48,192),(96,480)}`, `donchian ∈ {96,288}`, `k_atr ∈ {2,3}` (8). Entry: sign(trend_strength) with |·| > 0.5 **and** close breaks Donchian(N) high/low (previous N bars) in same direction, regime TREND_UP/DOWN matching. Stop price = close ∓ k_atr·ATR(96); trailing ATR k_atr; max_hold 2880.
  - MeanReversion grid: `W ∈ {96,288}`, `z_in ∈ {2.0,2.5}`, `z_out ∈ {0,0.5}` (8). Regime RANGE only. Stop |z| ≥ |z_entry| + 1.5 (zscore stop; `stop_distance = 1.5 * ewm_std/price`); TP |z| < z_out; max_hold 4*W.
  - Pairs grid: `W ∈ {96,288,672}`, `z_in ∈ {1.5,2.0,2.5}`, `z_out ∈ {0,0.5}` (18). Keys `"A/B"`; β = rolling_beta(W); z = ewma_zscore(spread, W); stop |z| ≥ |z_entry| + 1.5; `stop_distance = 1.5*σ_spread`; max_hold 288; legs (A,+1),(B,−β). Pairs is skipped entirely for spot.
  - All: cost gate from spec §5 is **not** in strategies (ranker uses net expectancy); `regime` column filled from classify_regime of leg A.
- [ ] **Step 1: Failing tests:** `test_grids_sizes` (8, 8, 18), `test_strategies_causal`, `test_ou_series_mean_reversion_gross_positive` (run_backtest with AcceptAllRanker, zero costs, regime forced RANGE), `test_trend_series_momentum_gross_positive`, `test_cointegrated_pair_gross_positive`, `test_independent_pair_gross_near_zero`, `test_strategies_emit_nothing_during_warmup` (first 480 bars: no entries; regime UNKNOWN → no entries).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(strategy): momentum, mean reversion and pairs strategies`.

### Task 16: Research trade lists (numba kernel) + equivalence with engine

**Files:** Create `luna/research/kernel.py`, `luna/research/tradelists.py`, `tests/research/test_tradelists.py`

**Interfaces:**
- Produces:
  ```python
  @njit def simulate(open_, high, low, close, entry, exit_long, exit_short, stop_dist, z, stop_z, atr, trail_mult, max_hold, use_price_stop) -> tuple[np.ndarray, ...]  # entry_idx, exit_idx, direction, exit_price_frac, exit_reason_code; one position at a time; entry at next open; price stop checked intrabar from the bar after entry
  def build_tradelist(sf: SignalFrame, panel: dict[str, pd.DataFrame], funding: dict[str, pd.DataFrame], costs: CostModel, kind: AccountKind) -> pd.DataFrame
      # columns: strategy, params_id, key, regime, direction, entry_ts, exit_ts, exit_reason, ret_gross (per unit gross notional), cost (round trip incl. fees+spread+slip), funding (known only), funding_unknown_bars, ret_net, stop_distance, r_net (= ret_net / stop_distance), day
  def build_all_tradelists(strategies: dict[str, type], panel, funding, costs, kind, keys: dict[str, list[str]]) -> pd.DataFrame   # every grid combo × key; cached to var/research/tradelists_<kind>.parquet keyed by data hash
  ```
- [ ] **Step 1: Failing tests:** `test_kernel_entry_next_open`, `test_kernel_intrabar_stop`, `test_kernel_time_stop`, `test_tradelist_matches_backtest_single_strategy` (same synthetic data, one strategy, one key, AcceptAllRanker, capital large, no limits binding → identical entry/exit timestamps and ret_net within 1e-9 per unit), `test_spot_tradelist_drops_shorts`, `test_funding_unknown_counted`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(research): numba trade lists equivalent to engine`.

### Task 17: Expectancy table + ExpectancyRanker

**Files:** Create `luna/ranking/expectancy.py`; Modify `luna/ranking/ranker.py`; Test `tests/ranking/test_expectancy.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass class CellStat: n: int; mean_r: float; lo_r: float; hi_r: float; shrunk_r: float; accepted: bool; reason: str
  def fit_expectancy(trades: pd.DataFrame, n0: float, n_min: int, n_boot: int, ci: float) -> dict[tuple[str, str, str], CellStat]
      # input = trades of the selected params only, within train window, universe-filtered
      # levels: strategy → (strategy,key) → (strategy,key,regime); shrink toward parent, top level toward 0
      # accepted at (strategy,key) iff n >= n_min and bootstrap lo > 0 and shrunk > 0; regime cells inherit acceptance from (strategy,key) and only adjust shrunk_r
  class ExpectancyRanker:
      def __init__(self, table: dict[tuple[str, str, str], CellStat], params_id: dict[str, str])
      def score(self, cand: Candidate, ts) -> Candidate | None   # None if (strategy,key) not accepted or shrunk_r(regime cell) <= 0; score = shrunk_r (already in R units); edge_bps = shrunk_r * stop_distance * 1e4
  ```
- [ ] **Step 1: Failing tests:** `test_small_n_shrinks_to_parent`, `test_rejects_below_n_min` (n=29 strong edge → not accepted), `test_rejects_when_ci_crosses_zero`, `test_accepts_clear_edge` (n=200, mean 0.3R, low noise), `test_regime_cell_cannot_accept_alone`, `test_ranker_scores_and_rejects`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(ranking): hierarchical shrinkage expectancy ranker with bootstrap acceptance`.

### Task 18: Pair selection with Benjamini–Hochberg + stability

**Files:** Create `luna/research/pairs_select.py`, `tests/research/test_pairs_select.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass class PairSelection: tested: int; prefiltered: int; fdr_pass: list[str]; stable: list[str]; selected: list[str]; table: pd.DataFrame   # per pair: corr, p_raw, p_bh, hl_1, hl_2, beta_1, beta_2, reasons
  def select_pairs(panel: dict[str, pd.DataFrame], universe: list[str], train_start, train_end, cfg: PairsConfig) -> PairSelection
      # ordered pairs canonical (A,B) with A having lower adv; corr of 1h log returns > corr_min; Engle-Granger (statsmodels coint) on log close of train window resampled to 1h; BH at fdr_q over prefiltered set; stability: both halves p < stability_p, half-life (in 15m bars) in [8,192], |β1-β2|/|β1| < beta_change_max; top max_pairs by p_bh; ETH/BTC gets no exemption
  ```
- [ ] **Step 1: Failing tests:** `test_independent_random_walks_mostly_rejected` (12 independent random walks → selected empty in ≥ 95 of 100 seeds, run with small n for speed: 20 seeds, assert ≤ 1 non-empty), `test_cointegrated_pair_selected`, `test_unstable_beta_rejected`, `test_uses_only_train_window`, `test_counts_reported`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(research): FDR-controlled stable pair selection`.

# P1.5 — Walk-forward and brutal validation

### Task 19: Walk-forward engine + holdout guard

**Files:** Create `luna/engine/walkforward.py`, `tests/engine/test_walkforward.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass class Window: train_start; train_end; test_start; test_end
  def make_windows(data_start, data_end, cfg: WalkForwardConfig) -> tuple[list[Window], tuple[pd.Timestamp, pd.Timestamp]]   # second = holdout (last holdout_months), excluded from windows
  @dataclass class WindowModel: window: Window; universe: list[str]; pairs: PairSelection; params: dict[str, str]; table: dict; funding_rule: str; ranker: Ranker
  def fit_window(w: Window, tradelists: pd.DataFrame, panel, settings, kind) -> WindowModel
      # only rows with entry_ts < train_end and exit_ts < train_end are used (assert)
      # param choice per strategy: argmax of mean r_net; if any train trade has funding_unknown_bars > 0 (perp), objective = min(mean r_net with unknown funding at 0×, at 2×estimated)
  @dataclass class WalkForwardResult: windows: list[WindowModel]; oos: BacktestResult; per_window: pd.DataFrame
  def run_walkforward(panel, funding, tradelists, settings, kind, ablation: Literal["none", "no_regime", "random_ranker"] = "none", cost_mult: float = 1.0, funding_mode: str = "known_only") -> WalkForwardResult   # simulates test windows sequentially, carrying the Portfolio across windows, switching models at boundaries
  def run_holdout(..., registry: Path) -> WalkForwardResult   # refuses (raises HoldoutAlreadyRun) if registry JSON records a previous holdout for this account; records git sha + timestamp
  ```
- [ ] **Step 1: Failing tests:** `test_windows_layout` (2023-01-01 → 2026-10-06: holdout = last 3 months; train 6/test 2/step 2; no test window overlaps holdout), `test_fit_window_never_sees_future` (tradelists with a planted giant-profit trade after train_end → not chosen), `test_funding_worst_case_rule`, `test_portfolio_carries_across_windows`, `test_holdout_runs_once`, `test_ablation_modes_run`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(engine): walk-forward with leakage guards and one-shot holdout`.

### Task 20: Full report + GO/NO-GO

**Files:** Create `luna/report/gonogo.py`; Modify `luna/report/html.py`, `luna/cli.py`; Test `tests/report/test_gonogo.py`, `tests/report/test_html.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass class GoNoGo: verdict: Literal["GO", "NO-GO", "INSUFFICIENT DATA"]; checks: dict[str, tuple[bool, str]]
  def evaluate(main: BacktestResult, cost_x2: BacktestResult, per_window: pd.DataFrame, best_naive_expectancy_r: float) -> GoNoGo
      # checks exactly §8: n>=30 (else INSUFFICIENT DATA), bootstrap lo>0, PF>=1.2, maxDD<0.15 & worst day>-0.03 & daily CVaR5>-0.02, cost×2 net expectancy>=0, net>0 on funding-known period, >=60% windows net>0, beats best naive (else verdict "NO-GO" with check name "complexity_adds_value")
  ```
  CLI `luna walkforward --account perp|spot` runs: main (known_only), cost×2, funding 0×/estimated/2× (perp only, in a separate "Sensitivity" section), ablations no_regime and random_ranker, naive baseline OOS over the same test windows; renders report sections: Verdict, Summary (primary metrics first; Sharpe/Sortino under "Secondary — informational, daily returns"), P&L breakdown, Funding known vs unknown (separate rows), Strategy × asset × regime heatmap, Expectancy table with CIs and accept/reject reasons, Pair selection per window (tested / FDR pass / stable / selected), Ablations, Baselines, Sensitivity, Data caveats (survivorship, funding gap).
- [ ] **Step 1: Failing tests:** `test_gonogo_insufficient`, `test_gonogo_fails_on_cost_x2`, `test_gonogo_fails_when_naive_better`, `test_gonogo_go_when_all_pass`, `test_report_contains_required_sections` (headings above present; "Sharpe" appears only inside the Secondary section).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(report): walk-forward report and GO/NO-GO`.

### Task 21: Run real walk-forward (perp and spot)

**Files:** Create `docs/results/walkforward.md`

- [ ] **Step 1:** `uv run luna walkforward --account perp` and `uv run luna walkforward --account spot` (background if long).
- [ ] **Step 2:** Verify both reports exist and render; copy the verdicts, primary metrics, ablation deltas and baseline comparison into `docs/results/walkforward.md` verbatim from `summary.json`. **Do not run holdout** (that is a user decision after reading the reports).
- [ ] **Step 3:** Commit `docs: walk-forward results`.

# P2 — Paper trading

### Task 22: SQLite store + persistent risk state

**Files:** Create `luna/store/models.py`, `luna/store/db.py`; Modify `luna/risk/state.py`; Test `tests/store/test_db.py`

**Interfaces:**
- Produces: SQLAlchemy Core tables per spec §9 (`runs, candidates, candidate_outcomes, positions, orders, fills, funding, equity, risk_events, state, book_snapshots`) with `account` column where relevant.
  ```python
  class Db:
      def __init__(self, url: str)                      # sqlite: enable WAL, busy_timeout 5000
      def acquire_lock(self, account: str, owner: str, stale_after_s: int = 1800) -> bool   # one live process per account
      def load_risk_state(self, account) -> RiskState; def save_risk_state(self, account, s: RiskState) -> None
      def record_candidates(self, run_id, rows: list[dict]) -> None; def record_order(...); def record_fill(...); def record_equity(...); def record_risk_event(...); def record_funding(...)
      def upsert_position(self, account, pos: Position, status: Literal["open", "closed"], exit_reason: str | None = None) -> None
      def open_positions(self, account) -> list[Position]; def open_orders(self, account) -> list[OrderRequest]
      def set_flag(self, account, key: Literal["kill_switch", "paused"], value: bool) -> None
  ```
- [ ] **Step 1: Failing tests:** `test_lock_exclusive_and_stale_takeover`, `test_risk_state_roundtrip`, `test_position_roundtrip_preserves_stop_and_legs`, `test_candidates_store_rejected_with_reason`, `test_wal_enabled`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(store): sqlite persistence and process lock`.

### Task 23: Live feeds + PaperBroker

**Files:** Create `luna/engine/feeds.py`, `luna/execution/paper.py`; Test `tests/engine/test_feeds.py`, `tests/execution/test_paper.py`

**Interfaces:**
- Produces:
  ```python
  class LiveFeed(Protocol):
      def refresh(self, now: pd.Timestamp) -> dict[str, pd.DataFrame]   # appends new closed bars to BarStore, returns rolling buffer (last 3000 bars) per asset
      def funding_rates(self) -> dict[str, float | None]                 # perp only (tickers fundingRate relative); spot → {}
      def book(self, asset: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]
      def mids(self) -> dict[str, float]
  class KrakenFuturesLiveFeed(LiveFeed); class KrakenSpotLiveFeed(LiveFeed)   # futures: charts API since last stored bar, /derivatives/api/v3/orderbook, tickers; spot: fetch_ohlc, fetch_depth, Ticker
  class PaperBroker:
      def __init__(self, kind, costs: CostModel, feed: LiveFeed, db: Db, account: str)
      def submit(self, order: OrderRequest) -> None      # persisted before simulated
      def poll(self, now) -> list[Fill]
          # IOC: walk current book depth from best level; fill up to limit; unfilled remainder cancelled; taker fee
          # POST_ONLY (spot): reprice every 5 min up to 2 times; fill when a public trade prints through the limit; cancel after 15 min
      def snapshot_books(self, assets) -> None           # → book_snapshots
  ```
- [ ] **Step 1: Failing tests:** `test_ioc_walks_book_vwap` (book [(100,0.5),(101,1)], buy 1 limit 101 → vwap 100.5), `test_ioc_respects_limit_partial`, `test_post_only_fills_on_trade_through_and_cancels_after_15m`, `test_feed_refresh_appends_only_closed_bars`, `test_feed_error_raises_feed_error` (MockTransport 500 ×3 → `FeedError`).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(paper): live public feeds and paper broker`.

### Task 24: Telegram + heartbeat

**Files:** Create `luna/notify/telegram.py`, `luna/notify/heartbeat.py`; Test `tests/notify/test_telegram.py`

**Interfaces:**
- Produces:
  ```python
  class Notifier(Protocol): def send(self, text: str) -> None; def commands(self) -> list[tuple[str, str]]   # (command, arg)
  class NullNotifier(Notifier)       # used when token/chat missing; logs only
  class TelegramNotifier(Notifier): def __init__(self, token: str, chat_id: str, client: httpx.Client)   # getUpdates offset tracking; ignores any chat_id != configured
  class EngineControl(Protocol): def status(self) -> str; def positions(self) -> str; def pause(self) -> None; def resume(self) -> None; def kill(self) -> None   # implemented by LiveEngine (Task 25)
  def handle_command(cmd: str, arg: str, engine: EngineControl) -> str   # /status, /positions, /pause, /resume, /kill ; /resume clears kill_switch and paused
  def ping(client, url: str | None) -> None   # no-op if None; never raises
  ```
- [ ] **Step 1: Failing tests:** `test_ignores_foreign_chat`, `test_kill_sets_flag_and_flattens` (engine fake), `test_resume_clears_kill`, `test_null_notifier_when_unconfigured`, `test_ping_never_raises`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(notify): telegram commands and heartbeat`.

### Task 25: Live engine (paper mode)

**Files:** Create `luna/engine/live.py`; Modify `luna/cli.py` (`paper --account`); Test `tests/engine/test_live.py`

**Interfaces:**
- Consumes: everything above; model = latest `WindowModel` (fit on the most recent 6 months using `fit_window` with train_end = now) refreshed every 2 months or on start.
- Produces:
  ```python
  class LiveEngine:   # implements EngineControl (Task 24)
      def __init__(self, account: AccountKind, settings: Settings, feed: LiveFeed, broker: PaperBroker, db: Db, notifier: Notifier, clock: Callable[[], pd.Timestamp], mode: Literal["paper", "live"] = "paper")
      def start(self) -> None          # acquire lock (exit if held), load state + open positions/orders, reconcile, fit model
      def tick(self, now) -> None      # one 15-min cycle: same sequence as run_backtest steps (4)-(6) using last row of the rolling buffer; records candidates (selected + rejected with reason), equity, risk events
      def minute_check(self, now) -> None   # emergency stops from mids; apply funding hourly; process telegram commands; heartbeat
      def run_forever(self) -> None    # sleeps to next 15-min boundary + 20 s; minute_check every 60 s
  ```
  Feed errors: catch `FeedError` → skip tick, record `risk_event("feed_error")`; 4 consecutive → notify; with open positions and 2 consecutive → set paused + notify.
- [ ] **Step 1: Failing tests** (fake clock, fake feed from synthetic bars, in-memory SQLite): `test_tick_uses_only_closed_bars`, `test_tick_skips_on_feed_error`, `test_stale_bars_block_entries`, `test_restart_resumes_positions_without_duplicate_orders`, `test_day_start_set_after_downtime`, `test_second_process_refused_by_lock`, `test_paper_equals_backtest_on_replay` (replaying the same synthetic bars through LiveEngine tick-by-tick yields the same trades as run_backtest), `test_live_mode_vetoed_when_disabled`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat(engine): paper live engine with reconciliation and restart safety`.

### Task 26: Counterfactual labeling

**Files:** Create `luna/labeling.py`; Modify `luna/cli.py` (`label`); Test `tests/test_labeling.py`

**Interfaces:**
- Produces: `def label_candidates(db: Db, store: BarStore, costs: CostModel, account: AccountKind, now: pd.Timestamp) -> int` — for unlabeled candidates (selected or rejected) whose max_hold has elapsed, simulate entry at next bar open with the candidate's stop/exit rules using `research.kernel.simulate` semantics, write `candidate_outcomes(candidate_id, outcome_r, exit_reason, labeled_at)`; returns count. Backtests also write `candidates_labeled.parquet` in their report dir.
- [ ] **Step 1: Failing tests:** `test_labels_rejected_candidate_with_stop_hit`, `test_skips_candidates_not_yet_matured`, `test_idempotent`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5:** Commit `feat: counterfactual labeling of candidates`.

### Task 27: Streamlit dashboard

**Files:** Create `dashboard/app.py`, `luna/dashboard_queries.py`; Test `tests/test_dashboard_queries.py`

**Interfaces:**
- Produces in `luna/dashboard_queries.py` (pure, testable): `equity_curve(db, account) -> pd.DataFrame`, `open_positions_df(db, account)`, `recent_candidates(db, account, limit=200)` (selected + rejected + reason + score), `risk_events_df(db, account)`, `report_index(var_dir) -> pd.DataFrame`. `dashboard/app.py` pages: Overview, Positions, Scanner, Risk, Research (embeds report HTML via `st.components.v1.html`), account selector.
- [ ] **Step 1: Failing tests:** each query on a seeded in-memory DB returns expected columns/rows.
- [ ] **Step 2–4:** FAIL → implement → PASS. Smoke: `uv run streamlit run dashboard/app.py --server.headless true` starts without exception (stop after 10 s).
- [ ] **Step 5:** Commit `feat(dashboard): streamlit monitoring dashboard`.

### Task 28: Docker paper deployment + end-to-end verification

**Files:** Modify `docker/Dockerfile`, `docker-compose.yml`; Create `docs/RUNBOOK.md`

- [ ] **Step 1:** Add bot healthcheck: `uv run luna health --account X` (new tiny CLI command: exit 0 iff last equity row < 20 min old). Test `test_health_command` on seeded DB.
- [ ] **Step 2:** `docker compose build` → success. (Requires Docker Desktop running — if the daemon is down, stop and report to the user; do not work around it.)
- [ ] **Step 3:** `docker compose up -d`; wait for two 15-minute ticks; verify: `docker compose ps` shows both bots healthy; `var/luna.db` has equity rows for perp and spot, candidates rows (incl. rejected with reasons); dashboard reachable at `http://localhost:8501`.
- [ ] **Step 4:** Write `docs/RUNBOOK.md`: start/stop, logs, Telegram setup (BotFather token, chat id), `/kill` usage, moving to a VPS (copy repo + `var/` + `.env`, `docker compose up -d`), disabling sleep on Windows.
- [ ] **Step 5:** Commit `chore: docker paper deployment and runbook`.

---

## Self-review notes

- Spec coverage: §0/§1 deliverables → Tasks 6, 13, 20–21, 28; §2 config → Task 1; §3 architecture → Tasks 8–12, 25; §4 data incl. real spot + funding honesty → Tasks 3–6, 12, 16, 19, 20; §5 strategies/regime/ranker/constructor → Tasks 12, 14–18; §6 risk → Task 9 (+ emergency stops in Tasks 12, 25); §7 execution → Tasks 10, 23; §8 + §8.1 validation/baselines → Tasks 11–13, 19–21; §9 store + candidates dataset → Tasks 22, 26; §10 monitoring → Tasks 24, 27; §11 Docker → Tasks 1, 28; §12 tests → every task; §13 roadmap gates P2→P3 are documented, not automated (paper runs for weeks after this plan).
- Spec §5 "maliyet kapısı" (rev 2 wording) is superseded in rev 3/4 by net-of-cost expectancy in the ranker; implemented via Task 16 `ret_net` and Task 17.
