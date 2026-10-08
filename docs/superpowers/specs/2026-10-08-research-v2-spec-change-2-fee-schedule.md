# Spec-change record SC-2: Kraken fee schedule correction (external-data correction)

- **Type:** correction of an external input, not parameter tuning. It changes no strategy parameter, risk value, grid or GO/NO-GO criterion. Only the cost input is corrected to the exchange's published schedule.
- **Date:** 2026-10-08
- **Requested by:** the user, in the V2 closure instruction of 2026-10-08, after the first V2 run.
- **Amends:** `config/default.yaml` `costs.spot`. This is used by V1 (spot paper and research) and by Research V2 spot.
- **Affects:**
  - `docs/superpowers/specs/2026-10-06-luna-trading-bot-design.md` §2: "Spot komisyon 0.25% / 0.40%". This record supersedes that number; the frozen spec text itself is not edited.
  - Every spot result that used the old fee.

## Values

| Account | Field | Old (used until 2026-10-08) | New (corrected) |
|---|---|---|---|
| spot (Kraken Pro, lowest tier) | maker_fee | 0.0025 (0.25%) | **0.0040 (0.40%)** |
| spot (Kraken Pro, lowest tier) | taker_fee | 0.0040 (0.40%) | **0.0080 (0.80%)** |
| perp (Kraken Futures, lowest tier) | maker_fee | 0.0002 (0.02%) | 0.0002, unchanged |
| perp (Kraken Futures, lowest tier) | taker_fee | 0.0005 (0.05%) | 0.0005, unchanged |

Spread and slippage assumptions are unchanged.

## Source

- **Official schedule:** Kraken fee schedule, https://www.kraken.com/features/fee-schedule, fetched 2026-10-08.
  - Kraken Pro spot crypto, Tier 1 (30-day volume from $0): maker 0.40%, taker 0.80%.
  - Futures, Tier 1: maker 0.02%, taker 0.05%.
- **Secondary source:** BrokerChooser, https://brokerchooser.com/news/coinbase-vs-kraken-fee-structures-and-trading-features-compared--bbeea493, reports that Kraken restructured its Pro fee framework effective 2026-07-09, with a base tier of 0.40% / 0.80%.

## Note on the historical period

If the restructure date above is right, the old 0.25% / 0.40% schedule was in force during the development OOS (2024-09 → 2026-07). The corrected schedule is what this account would actually pay from now on, live or paper, and it is the more conservative assumption. Spot results are therefore re-run with the corrected fees as the forward-looking cost basis. Results produced with the old fees stay on disk and are marked superseded; they are not overwritten.

## Consequences

- **Research V2 spot:** re-run from scratch with the corrected fees. Perp is not re-run, because its fees are unchanged.
- **V1 spot reports:** `docs/results/walkforward-spot.md` and the spot remarks in `docs/results/interim-findings-2026-10-06.md` are marked "superseded by corrected fee schedule". V1 spot was not re-run: a V1 walk-forward re-run loads bars after 2026-07-01 for its holdout fit window, and the V2 closure rule is to touch nothing in that period.
- **Paper bot (`bot-spot`):** still runs the image built before this change, so it uses the old fee until the image is rebuilt. It opens no positions at present (the model has no accepted cell). The rebuild is listed as an open item.
