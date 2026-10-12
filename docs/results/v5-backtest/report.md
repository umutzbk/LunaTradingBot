# V5 historical backtest

**EXPLORATORY — reused development period**

- Window: 2025-11-10T00:00:00+00:00 → 2026-07-01T00:00:00+00:00
- Verdict: **ADVANTAGE** KILL-AFFECTED
- Rows used: S-NOKILL, S-PIT10/NOKILL
- summary.json sha256: `c0b7adc615a4bb5d5e23cc74df0d1a2461ff54d311ca1089e6299781eb77b3db`

| Arm | Row | Net | Funding | Fees | Spread/slip | Price | carry_net mean | Sharpe | MaxDD |
|---|---|---|---|---|---|---|---|---|---|
| W | PRIMARY | -36.1926 | 0.0909 | 0.9392 | 0.9117 | -34.4326 | -3.059e-05 | -1.986 | 0.1366 |
| W | S-COST2X | -38.9383 | 0.0916 | 1.8747 | 1.7955 | -35.3597 | -6.223e-05 | -2.062 | 0.1469 |
| W | S-SPREAD-FIX | -35.1650 | 0.1362 | 0.7249 | 1.0149 | -33.5614 | -2.763e-05 | -2.260 | 0.1327 |
| W | S-NOKILL | -26.4718 | 6.2284 | 5.5484 | 5.0281 | -22.1237 | -7.615e-05 | -0.441 | 0.2110 |
| W | S-PIT10 | -36.2089 | 0.0909 | 0.9392 | 0.9280 | -34.4326 | -3.088e-05 | -1.986 | 0.1366 |
| W | S-FB-CONS | -36.1926 | 0.0909 | 0.9392 | 0.9117 | -34.4326 | -3.059e-05 | -1.986 | 0.1366 |
| W | S-PIT10/NOKILL | -26.5368 | 6.2283 | 5.5483 | 5.0893 | -22.1275 | -7.730e-05 | -0.443 | 0.2110 |
| W | S-FB-CONS/NOKILL | -26.4718 | 6.2284 | 5.5484 | 5.0281 | -22.1237 | -7.615e-05 | -0.441 | 0.2110 |
| W | S-COST2X/NOKILL | -34.3622 | 6.1554 | 10.8749 | 9.7736 | -19.8691 | -2.611e-04 | -0.618 | 0.2211 |
| W | S-UNWIND | -36.1926 | 0.0909 | 0.9392 | 0.9117 | -34.4326 | -3.059e-05 | -1.986 | 0.1366 |
| D | PRIMARY | -35.1416 | 0.8880 | 2.7194 | 2.9974 | -30.3128 | -8.455e-05 | -1.100 | 0.1389 |
| D | S-COST2X | -32.3085 | 0.8219 | 4.5707 | 5.1363 | -23.4233 | -1.527e-04 | -0.995 | 0.1406 |
| D | S-SPREAD-FIX | -25.7845 | 0.3422 | 2.4260 | 3.3963 | -20.3044 | -9.219e-05 | -0.852 | 0.1257 |
| D | S-NOKILL | -29.3073 | 8.8942 | 14.3958 | 13.3523 | -10.4534 | -3.418e-04 | -0.346 | 0.2251 |
| D | S-PIT10 | -34.9777 | 0.7639 | 2.2702 | 2.5431 | -30.9283 | -7.012e-05 | -1.098 | 0.1385 |
| D | S-FB-CONS | -35.1416 | 0.8880 | 2.7194 | 2.9974 | -30.3128 | -8.455e-05 | -1.100 | 0.1389 |
| D | S-PIT10/NOKILL | -24.9224 | 9.0117 | 14.7997 | 13.7222 | -5.4121 | -3.493e-04 | -0.267 | 0.2022 |
| D | S-FB-CONS/NOKILL | -29.3073 | 8.8942 | 14.3958 | 13.3523 | -10.4534 | -3.418e-04 | -0.346 | 0.2251 |
| D | S-COST2X/NOKILL | -47.8577 | 8.5205 | 27.8094 | 25.7900 | -2.7788 | -8.435e-04 | -0.717 | 0.2604 |
| D | S-UNWIND | -35.1416 | 0.8880 | 2.7194 | 2.9974 | -30.3128 | -8.455e-05 | -1.100 | 0.1389 |
| C | PRIMARY | -16.6734 | 0.2193 | 0.4797 | 0.5312 | -15.8817 | -1.338e-05 | -0.639 | 0.1325 |
| C | S-COST2X | -17.5238 | 0.2195 | 0.9593 | 1.0616 | -15.7224 | -3.023e-05 | -0.672 | 0.1333 |
| C | S-SPREAD-FIX | -4.2772 | 0.3000 | 0.6108 | 0.8551 | -3.1112 | -1.971e-05 | -0.116 | 0.0969 |
| C | S-NOKILL | -25.0920 | 1.7847 | 0.8255 | 0.8238 | -25.2273 | 2.799e-07 | -0.564 | 0.1853 |
| C | S-PIT10 | -16.6579 | 0.2193 | 0.4797 | 0.5157 | -15.8817 | -1.313e-05 | -0.639 | 0.1325 |
| C | S-FB-CONS | -16.6734 | 0.2193 | 0.4797 | 0.5312 | -15.8817 | -1.338e-05 | -0.639 | 0.1325 |
| C | S-PIT10/NOKILL | -24.9894 | 1.7859 | 0.8273 | 0.8123 | -25.1357 | 4.573e-07 | -0.562 | 0.1850 |
| C | S-FB-CONS/NOKILL | -25.0920 | 1.7847 | 0.8255 | 0.8238 | -25.2273 | 2.799e-07 | -0.564 | 0.1853 |
| C | S-COST2X/NOKILL | -26.5808 | 1.7849 | 1.6509 | 1.6468 | -25.0680 | -2.751e-05 | -0.603 | 0.1883 |
| C | S-UNWIND | -16.6734 | 0.2193 | 0.4797 | 0.5312 | -15.8817 | -1.338e-05 | -0.639 | 0.1325 |
| E | PRIMARY | -0.2011 | 2.0733 | 0.7597 | 0.8515 | -0.6632 | 5.772e-06 | 0.112 | 0.1462 |
| E | S-COST2X | -1.6716 | 2.0377 | 1.5136 | 1.7024 | -0.4933 | -2.034e-05 | 0.075 | 0.1472 |
| E | S-SPREAD-FIX | 5.3292 | 0.3355 | 0.6102 | 0.8543 | 6.4583 | -1.895e-05 | 0.290 | 0.0935 |
| E | S-NOKILL | 12.3706 | 2.7737 | 0.8235 | 0.9030 | 11.3234 | 1.506e-05 | 0.410 | 0.1525 |
| E | S-PIT10 | 0.9906 | 2.0721 | 0.7620 | 0.8547 | 0.5353 | 5.591e-06 | 0.142 | 0.1456 |
| E | S-FB-CONS | -0.2011 | 2.0733 | 0.7597 | 0.8515 | -0.6632 | 5.772e-06 | 0.112 | 0.1462 |
| E | S-PIT10/NOKILL | 13.6660 | 2.7721 | 0.8257 | 0.9045 | 12.6241 | 1.486e-05 | 0.439 | 0.1519 |
| E | S-FB-CONS/NOKILL | 12.3706 | 2.7737 | 0.8235 | 0.9030 | 11.3234 | 1.506e-05 | 0.410 | 0.1525 |
| E | S-COST2X/NOKILL | 10.7156 | 2.7227 | 1.6412 | 1.8045 | 11.4384 | -1.298e-05 | 0.373 | 0.1529 |
| E | S-UNWIND | -0.2011 | 2.0733 | 0.7597 | 0.8515 | -0.6632 | 5.772e-06 | 0.112 | 0.1462 |

## Declared interpretations

- Depth: no historical order book; the builder uses infinite book sizes, so the depth gate passes (§3.1).
- Instruments: frozen V4 file (qty step, tick, min qty, min cost); maintenance margin is not in that file and is 0, so the liquidation-distance pre-check (equity / leg) effectively always passes.
- S-NOKILL: R4 disabled by setting risk.max_drawdown = 1.0 (it can only fire at equity <= 0); leg stop and R3 unchanged.
- G6 when KILL-AFFECTED: read from an S-COST2X/NOKILL row (S-COST2X with the R4 kill off), because §10 moves every guardrail to the kill-off rows; it is not a trial (§7).
- G6 for the S-PIT10 and S-FB-CONS row verdicts: the same S-COST2X delta-carry as the base row.
- FALLBACK-AFFECTED: fallback shares from the base row's fills (S-NOKILL when KILL-AFFECTED).
- P-C8 'interval 8 h': scheduled slots at 00:00, 08:00, 16:00 UTC with the §3.2 completion / 4-attempt rule (the paper Trigger's 'since the last rebalance' is not used in replay).
- Valid snapshot for the 4-attempt counter: at least one asset has a quote at t.
- Terminal unwind (S-UNWIND): |q| x mark x row cost multiplier x (fee + decision-window half spread at the end + 2 bps slip).
- Secondary comparisons: Holm over D-W, C-W, E-C separately for carry and net and for b = 28 and b = 14, on the bootstrap one-sided p-values.
- Switch ledger: hit = realized incremental funding over the actual holding > realized cost (fees + spread/slip of the new leg's fills); bias = expected incremental scaled to the holding duration - realized.
- Fallback hour-of-day: the hour of the decision that sent the order.
- Outcome authorization: a guard against accidental runs inside this codebase (tag, clean tree, protocol and manifest sha, frozen config, approval phrase, exclusive markers), not a security boundary.
