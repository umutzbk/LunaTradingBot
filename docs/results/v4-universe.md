# V4 research universe (spec §2a), 2026-10-09

Rules U1–U7 are applied identically to all 21 candidate assets. They use the development window [2025-10-06T08:00Z, 2026-07-01) only, and no return, P&L or strategy result is computed.
This universe is separate from the paper-bot universe, which is unchanged.

**Transparency note:** the U3/U4 consistency thresholds were written after the S0-F check had shown one asset (MUBARAK) failing. The rules are general and outcome-free, but they are not blind to that fact.

| asset | eligible | failed rules | funding cov | perp cov | spot cov | max gap f/p/s (h) | implied px err | corr prev / same |
|---|---|---|---|---|---|---|---|---|
| ADA | YES | — | 0.9989 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.028% | 0.94 / 0.562 |
| AVAX | YES | — | 0.9989 | 1.0 | 0.9978 | 3.0/0.25/7.0 | 0.032% | 0.293 / 0.182 |
| BNB | YES | — | 0.9989 | 1.0 | 0.9986 | 3.0/0.25/6.75 | 0.036% | 0.216 / 0.037 |
| BTC | YES | — | 0.9988 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.013% | 0.83 / 0.733 |
| DOGE | YES | — | 0.9989 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.028% | 0.658 / 0.444 |
| ENA | YES | — | 0.9989 | 1.0 | 0.9623 | 3.0/0.25/6.75 | 0.100% | 0.665 / 0.531 |
| ETH | YES | — | 0.9988 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.015% | 0.804 / 0.462 |
| FET | YES | — | 0.9989 | 1.0 | 0.9934 | 3.0/0.25/7.0 | 0.158% | 0.38 / 0.279 |
| HYPE | YES | — | 0.9989 | 1.0 | 0.999 | 3.0/0.25/0.5 | 0.078% | 0.821 / 0.557 |
| LINK | YES | — | 0.9989 | 1.0 | 0.9987 | 3.0/0.25/7.0 | 0.032% | 0.636 / 0.305 |
| MUBARAK | NO | U3, U4, U6, U7 | 0.9989 | 1.0 | 0.1407 | 3.0/0.25/52.0 | 1.147% | -0.338 / -0.323 |
| NEAR | YES | — | 0.9989 | 1.0 | 0.984 | 3.0/0.25/6.75 | 0.066% | 0.258 / -0.01 |
| PEPE | YES | — | 0.9989 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.052% | 0.459 / 0.319 |
| PUMP | YES | — | 0.9989 | 1.0 | 0.9594 | 3.0/0.25/7.0 | 0.151% | 0.397 / 0.317 |
| SOL | YES | — | 0.9989 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.023% | 0.782 / 0.625 |
| SUI | YES | — | 0.9989 | 1.0 | 0.9987 | 3.0/0.25/6.75 | 0.029% | 0.49 / 0.007 |
| TAO | YES | — | 0.9989 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.063% | 0.506 / 0.34 |
| UNI | YES | — | 0.9989 | 1.0 | 0.9809 | 3.0/0.25/6.75 | 0.044% | 0.42 / 0.141 |
| XLM | YES | — | 0.9989 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.054% | 0.68 / 0.438 |
| XRP | YES | — | 0.9989 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.019% | 0.657 / 0.311 |
| ZEC | YES | — | 0.9989 | 1.0 | 0.9988 | 3.0/0.25/6.75 | 0.059% | 0.712 / 0.577 |

**Eligible (20):** ADA, AVAX, BNB, BTC, DOGE, ENA, ETH, FET, HYPE, LINK, NEAR, PEPE, PUMP, SOL, SUI, TAO, UNI, XLM, XRP, ZEC.

**Excluded:** MUBARAK (U3, U4, U6, U7).
