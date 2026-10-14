# Binance USDⓈ-M fee evidence (DEC-39, as replaced by the user decision of 2026-10-14)

This file is a **research provenance record**. It does not claim that its bytes are the original Binance page. Under the replaced DEC-39, qualified official-source retrieval evidence is sufficient. A locally preserved raw HTML/PDF artefact is no longer required.

## Frozen baseline constants

| Venue / product | Tier | Maker | Taker | BNB fee discount |
|---|---|---|---|---|
| Binance USDⓈ-M (USDS-Margined) Futures, USDT-margined | Regular User (VIP 0) | **0.0200 %** | **0.0500 %** | **OFF** (baseline does not pay fees in BNB; no discount percentage is needed) |

## Official sources

Both are the same official Binance Support article, "Binance Futures Fee Structure & Fee Calculations", article 360033544231, on binance.com, in two official locales:

1. https://www.binance.com/en-AU/support/faq/detail/360033544231
2. https://www.binance.com/en-PH/support/faq/detail/360033544231

These are official Binance Support pages, not Binance Square, community or user-generated content. No blog, Reddit or third-party site is used to establish the constants.

## Supporting content

| | |
|---|---|
| Section | "How to calculate trading fee of USDS-Margined contracts?" |
| Content (identical in both locales) | a Regular User's **maker fee is 0.02 %** and **taker fee is 0.05 %** |

## Verification date

2026-10-14 (UTC).

## Provenance caveat (must remain explicit)

- **Claude's research environment could not independently render or read** the en-AU / en-PH Binance article, because of regional redirect and AWS WAF behaviour.
- This record is **not independently verified raw-page evidence**. The DEC-39 closure relies on the user's pre-freeze governance decision (2026-10-14) that accepts qualified official-source provenance.
- The baseline constants remain maker 0.0200 %, taker 0.0500 %, BNB discount OFF.

## Retrieval method and limitation

- **Retrieval method:** an official-web retrieval of the two locale URLs above. It was supplied with the user's explicit DEC-39 decision of 2026-10-14, and that decision accepts it as qualified evidence.
- **From this research environment (UK), raw preservation was blocked:**
  - Direct HTTP retrieval of binance.com fee / support pages returned an AWS WAF challenge (HTTP 202, `x-amzn-waf-action: challenge`, 0 bytes). The challenge was not bypassed.
  - The page-reading tool and the local browser were region-redirected to the UK homepage, or to the en-GB site, which shows only the spot fee table.
  - Neither Wayback captures nor location-circumvention methods are used to establish the constants.
- **Earlier capture attempts** are recorded in `var/v7/stage0/fees/README_ATTEMPTS.txt` and `var/v7/stage0/fees/official_retrieval_2026-10-14/PROVENANCE_NON-FREEZE-EVIDENCE.txt`.
  - None of those files is the evidence for this decision.
  - The UK spot-fee PDF is explicitly not fee evidence.

## Integrity

A file cannot contain its own hash. The SHA-256 of this record is stored in `binance-fee-evidence.md.sha256` next to it and in the spec (S-4.3, R9, DEC-39).
