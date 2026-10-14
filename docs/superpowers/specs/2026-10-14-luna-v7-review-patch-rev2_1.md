> **NON-NORMATIVE — MERGED INTO DRAFT 2** (`2026-10-14-luna-v7-ml-research-design.md`). Kept only as the approval record. Where this file differs from Draft 2, Draft 2 governs; superseded values quoted here (e.g. 0.55, 54 configs, inner-block calibration, the pro-forma P11) are not rules.

# V7 — REVIEW PATCH REV 2.1 (corrections A and B to REV 2)

**Status:** proposal; nothing applied to Draft 2, committed or frozen.

**Approved by the user:** REV 2 decisions 1–4 and 6–10 as recommended, and decision 5 = P11 (R1 + the corrected R2 below). This file replaces only REV 2 §5 (P11) and the P8 row of S-12.2, and lists the downstream lines that change.

---

## A. P11: price-residual dependence guard, on the executed book

**Accounting objects (executed book).**
- **Instrument j:** every leg asset traded by the book, plus `BTCUSDT` (the single residual hedge).
- **Event times of j:** every fill of j and every 15-minute valuation tick while j is held. If a fill and a tick share a timestamp, the tick is ordered first; fills at the same timestamp keep the book's booking order.
- **p_j(e):** the price the book accounting uses at event e: the reference mid at a fill (the mid against which spread and slippage are charged, S-5.2), the mark at a tick.
- **q_j(e⁺):** the position immediately after e.

Every evaluation window starts flat and ends flat (terminal unwind).

**Executed price P&L per instrument:** **PR_j = Σ_k q_j(e_k⁺)·(p_j(e_{k+1}) − p_j(e_k)) = −Σ_{fills f of j} dq_f·p_j(f)** (exact by summation by parts, because q is 0 at both ends).

- **PR = Σ_j PR_j**, and **NET = F_net − C_exec + PR** (P2′ definitions unchanged).
- PR so defined is identical to the "price" column of the daily accounting identity (S-7.5). The invariant is tested.

**Episodes and clusters (legs only).**
- **Episode:** a maximal interval during which a leg asset's position is non-zero with constant sign. Same-decision flips are impossible (S-7.1).
- **Cluster c:** a maximal set of episodes of the same (asset, side) where each episode starts ≤ 72 h after the previous one ended.
- Every leg fill belongs to exactly one episode, and so to exactly one cluster.

**Exact executed-book attribution.**
1. **Cluster raw price contribution:** PR_c = Σ over the episodes of c of the episode's share of PR_j (the event sum restricted to that episode's events). Then Σ_c PR_c = Σ_{j ≠ BTC} PR_j exactly.
2. **Market component of a cluster** (a deterministic decomposition, not a hedge allocation):
   - mkt_c = Σ over the events e_k of c's episodes of q(e_k⁺)·p(e_k)·β̂_episode·(B(e_{k+1}) / B(e_k) − 1).
   - β̂_episode is the point-in-time β̂ of the decision that opened the episode, frozen for the episode.
   - B(e) is BTC's price at the same event: the reference mid at fills and the mark at ticks.
3. **Cluster residual price contribution:** **ρ_c = PR_c − mkt_c.**
4. **One book-level MARKET unit:** **MKT = Σ_c mkt_c + PR_BTC.** This is the executed book's net market-beta price P&L: the legs' beta exposure plus the actual BTC hedge's price P&L.

**Why the hedge isn't allocated to clusters.** The BTC hedge offsets the book's *aggregate* beta. Splitting it across clusters would need weights n_i·β̂_i / β_book, which are unbounded when β_book is near zero and depend on band and resize timing, so any split would be arbitrary. The hedge therefore lives only in the MKT unit, together with the legs' own market components. Nothing is divided by β_book.

**Invariant (exact by construction):** **Σ_c ρ_c + MKT = PR.**
- Check tolerance: |Σ_c ρ_c + MKT − PR| ≤ 1e-9 × Σ|fill notional|.
- Failure → INVALID RUN (K10).

**P11 (frozen thresholds; none chosen from outcomes):**
- **R1 (unchanged):** **CAC = F_net − C_exec > 0.**
- **R2′ (executed-book price-luck stress):** **NET_trim = NET − Σ_{c ∈ Top_m} max(ρ_c, 0) − max(MKT, 0) > 0**, where:
  - Top_m = the m clusters with the largest positive ρ_c;
  - m = max(3, ⌈0.05 · n_clusters⌉), with n_clusters = the number of clusters with at least one closed episode in the window.

  In words: remove the m largest executed residual-price winners **and** any positive net market-beta P&L. What remains must still be positive.

**P11 passes iff R1 ∧ R2′.** It uses the same objects on `W_DEV` out-of-fold (development report), on `W_HOLD` (acceptance) and in the forward test (GO).

**Reported alongside P11, not gates:**
- PR, MKT, Σ ρ_c;
- the top-10 ρ_c;
- the per-asset sums of ρ_c;
- NET_trim for m ∈ {1, 3, 5, 10} (descriptive);
- R3 concentration (top-1 asset share and top-5 cluster share of positive Σ ρ_c).

---

## B. P8 split: primary regression validity vs secondary classifier validity

**P8-primary (S-12.2 acceptance gate; ML\* only).**
- Population: the frozen regression model that determined ML\* trades (A2 or A3 at h\*).
- Pairs: (calibrated prediction ŷ_cal, realized `y_reg(h*)`) for every eligible candidate (both sides) at every `W_HOLD` decision time.
- Fit realized on predicted by OLS, with every decision date weighted equally.
- **P8-primary passes iff the slope b ∈ [0.5, 1.5]** (the frozen realized-on-predicted criterion; a slope ≤ 0 therefore fails).
- The date-level IC requirement stays in P7, unchanged.

**P8-classifier (secondary; reported).**
- The calibrated classifier used by A1 (fixed configuration, Decision 9).
- ECE over 10 equal-frequency bins of P̂, with decision dates weighted equally, plus the reliability curve and the Brier score, on `W_HOLD`.
- **Required only for claims about A1.** A1 is declared better than A0 only if its Holm-adjusted secondary test rejects **and** ECE ≤ 0.05.
- **It cannot change the primary verdict** (`V7 HOLDOUT ADVANCE` or its failure classes).

**Integrity is not split.** The classifier passes the same leakage and integrity checks as every model:
- L1–L14 apply to it;
- an L9 canary or known-at failure in any model is `NO-GO: leakage` for the whole run (K1), because the pipeline is shared.

The split concerns predictive calibration as an economic gate only.

---

## Downstream lines that change

### S-12.2 table

The P8 row is replaced and P11 is added:

| ID | Criterion (on `W_HOLD`, Binance, `E_T`, ML\*) |
|---|---|
| P8 | **P8-primary:** OLS slope (date-equal weights) of realized `y_reg(h*)` on the ML\* regression model's calibrated ŷ_cal over all eligible `W_HOLD` candidates ∈ [0.5, 1.5]. (P8-classifier is secondary, S-12.3.) |
| P11 | **R1 ∧ R2′:** CAC = F_net − C_exec > 0 and NET_trim = NET − Σ_{Top_m} max(ρ_c, 0) − max(MKT, 0) > 0, m = max(3, ⌈0.05·n_clusters⌉), on the executed-book attribution (§A) |

`V7 HOLDOUT ADVANCE` requires **P1–P11** (P8 = P8-primary).

### S-12.3 secondary family

Unchanged family of 6, with this sentence added:

> "A claim that A1 improves on A0 additionally requires P8-classifier (ECE ≤ 0.05). P8-classifier never affects S-12.2 or S-12.4."

### S-12.4 verdict classes

Replace the `NO-GO` row with:

| Class | Meaning |
|---|---|
| `NO-GO` | any of P2, P3, P5, P6, P7, P8-primary, P9, P10, P11 fails |

All other rows are unchanged. `NO-GO: leakage` (any model, including the classifier) and `INVALID RUN` (including K10) still rank above it.

### S-9.7 development kill (as in REV 2)

Kill if any of the following holds on `W_DEV` out-of-fold:
- mean daily net ≤ 0;
- F_net ≤ 0;
- C_exec / F_net > 1.0;
- **CAC ≤ 0** (R1);
- fewer than 30 % of outer folds have positive net.

R2′ is reported on `W_DEV` but isn't a development kill.

### Forward GO (REV 2 §10)

The GO criteria list reads:

> "H_F p < 0.05; P2′; P3; P6; **P11 = R1 ∧ R2′ on the forward executed book**; evidence requirements; D1–D3 for a deployment proposal."

P8-primary and P7 are reported in the forward test, not gates. This is unchanged from REV 2.

### §13 kill criteria (new row)

| ID | Trigger | Consequence |
|---|---|---|
| K10 | Attribution invariant fails (\|Σ_c ρ_c + MKT − PR\| > 1e-9 × Σ\|fill notional\|), or PR differs from the daily-identity price column | INVALID RUN |

### §11 metrics (addition)

Executed-book price decomposition: PR, MKT, Σ ρ_c, top-10 ρ_c, per-asset ρ, NET_trim for m ∈ {1, 3, 5, 10}.
