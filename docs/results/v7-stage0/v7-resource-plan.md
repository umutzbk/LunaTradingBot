# V7 resource and implementation preparation (2026-10-14)

These are planning estimates only. Nothing is implemented. Sizes come from the Stage 0 listings; runtimes are extrapolated from the Stage 0 throughput: about 12,000 checksummed files in about 30 minutes (~5–10 MB/s), and parquet ingest of 12,312 monthly 15-minute files to 1.1 GB.

## Hosts

| Host | CPU | RAM | Free disk | Current load |
|---|---|---|---|---|
| PC (Windows) | Ryzen 7 5800H, 8 cores / 16 threads | 14 GB, **~0.6 GB free** (WSL `vmmemWSL`, Docker, ChatGPT, Cursor, Claude, Power BI gateway) | **6.8 GB (C: 99 %)** | authoritative Kraken collector, V6 worktree |
| DigitalOcean VPS (London) | 1 vCPU | 2 GB (~1.0 GB available) | 40 GB | shadow collector, bot-carry, dashboard |

## Storage estimate

| Item | Raw (zipped) | Processed (parquet) | When |
|---|---|---|---|
| klines 15m, last price (`W_DEV`) | 1.38 GB (done, deleted after parse) | 1.1 GB (exists) | build |
| mark / index / premium 15m (`W_DEV`) | 2.3 GB | ~2.5 GB | build |
| klines 1m (`W_DEV`; S-5.2 reference prices) | **17.3 GB** | ~0.3 GB if only the minutes needed for references are kept; ~15 GB in full | build |
| funding REST + archive (`W_DEV`) | 0.09 GB (done) | 0.05 GB | build |
| aggTrades, sampled (`E_M`) | ~5–15 GB (≤ about 10 symbol-days per day × median 1–4 MB) | ~1–3 GB | build |
| R6 bookTicker for B-1 | (a) ~1.06 TB streamed / (b) ~35 GB / (c) 0 | (a) ~2 GB of per-bar spreads / (b) 0.1 GB | Stage 0 continuation |
| DT1 candidates + DT2 labels | – | ~1–2 GB (≈ 1.0 M symbol-sides × ~60 features × 3 horizons) | build |
| `W_HOLD`, at the opening only | ~12 GB (1m 9.4 + 15m kinds 1.9 + aggTrades sample) | ~3 GB | opening |
| Forward recorder (180 d, Binance public) | ~15–30 GB (5-minute depth for the eligible universe ≈ 0.1 GB/day, plus tickers, funding, filters) | – | forward |

**Total for the development build:** about 30 GB free required (more with B-1(a)). **This doesn't fit the PC's current 6.8 GB.**

## Runtime and RAM estimate

| Stage | Duration | Peak RAM | CPU |
|---|---|---|---|
| Remaining downloads (1m, mark/index/premium, aggTrades sample) | ~1–2 h | < 0.5 GB (streamed) | network-bound |
| B-1(a) full R6 parse | days (≈ 7 TB of CSV) | 1–2 GB streamed | multi-core |
| B-1(b) sample | ~1–2 h | < 1 GB | 1–4 cores |
| Point-in-time universe + features DT1 | ~20–40 min | ~2–3 GB | 4–8 cores |
| Labels DT2 (3 horizons) | ~20–40 min | ~2 GB | 4–8 cores |
| Walk-forward: 42 configs × 10 folds + refits + calibration | ~3–6 h (LightGBM dominant) | **~3–4 GB** | 8–16 threads |
| Bootstrap and acceptance (B = 10,000, ℓ ∈ {14, 28}) | minutes | < 1 GB | 1–4 cores |
| Forward recorder | continuous | ~0.2–0.4 GB | < 0.2 core |

## Host recommendation per stage

| Stage | PC | 2 GB VPS | Recommendation |
|---|---|---|---|
| Stage 0 continuation (B-1 sample, 1m download) | OK once there are ≥ 30 GB free | disk OK; CPU and RAM too small for parsing | **PC, after freeing disk** (or a personal external SSD) |
| Feature / label build | OK if ~4 GB of RAM is freed (stop WSL, Cursor, ChatGPT and other heavy apps while it runs) | **No** (2 GB RAM, 1 vCPU) | **PC** |
| Training / walk-forward | OK, same conditions; 16 threads | **No** | **PC**, or a temporary larger cloud box (e.g. 8 vCPU / 32 GB, a few hours) |
| `W_HOLD` opening + acceptance | OK | No | **PC**, dedicated session, clean tree, V6-style preflight |
| Forward recorder (public data, §15) | possible but host restarts break it | **Yes**; separate directory and service, never touching the Kraken collector | **VPS**; watch the 40 GB disk: add a volume or rotate to the PC |

## Proposed directory layout (not created)

```
LunaTradingBot-v7/
  luna/v7/            ingest/ (archive, rest, cms, wayback), universe.py, features.py, labels.py,
                      folds.py, models.py, constructor.py, accounting.py, stats.py, guards.py
  scripts/v7/         stage0_*.py (port of tonight's scripts), build_dev.py, run_dev.py
  tests/v7/           test_guards.py, test_universe_pit.py, test_labels.py, test_accounting_k10.py, ...
  var/v7/raw/         binance/{klines_15m,klines_1m,mark,index,premium,funding,aggtrades}/  (immutable + manifests)
  var/v7/derived/     DT1..DT4 parquet + manifests
  docs/results/v7-stage0/   (tonight's outputs)
```

## Implementation and test plan outline (for after freeze; not started)

1. **Guards first (tests before code):**
   - refuse any request overlapping `W_HOLD` or M6 without the token (S-1.3/S-1.7);
   - REST `endTime` assertion;
   - listing filter at 2026-07-01;
   - V6 hash check (S-1.1).
2. **Ingest with checksums:**
   - header auto-detection (mixed headers found);
   - ms/µs normalization;
   - zero-volume tail handling;
   - a funding-source union (REST ∪ archive);
   - interval inference at switches.
3. **Point-in-time universe U1–U7:**
   - a `known_at` assertion per row;
   - golden tests on the D0 decisions found tonight (2021-01-01 00:05 = 31 symbols);
   - the relist rule per B-3.
4. **Labels (S-5) with the S-1.5 window-end rule;** canary tests L1–L14.
5. **Accounting (§8):** tests for the K10 invariant and the summation-by-parts identity.
6. **Folds and three-block calibration;** model zoo limited to EN / Logistic / LightGBM (42 configurations).
7. **Statistics (S-13):** bootstrap unit tests on synthetic series with known μ.
8. **Independent code review → `v7-code-frozen`.**
