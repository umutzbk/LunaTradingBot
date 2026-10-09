# Research V3: sonuçlar (keşif; reused development walk-forward OOS, 2024-09 → 2026-07)

> **Önce okuyun: dataset uyarısı.** V3, V1 ve V2'de defalarca incelenmiş development setini yeniden kullanır. Buradaki
> her sonuç **keşif** niteliğindedir; **final generalization kanıtı değildir** (spec E2). Final generalization seti
> (2026-07 → 2026-10) okunmadı; `var/holdout_registry.json` yok. Canlı emir yok, gerçek para yok.

- **Spec:** `docs/superpowers/specs/luna-research-v3-design.md`. FROZEN; tag `research-v3-spec-frozen`, commit `babbc59`.
  Sonuç öncesi açıklama: `luna-research-v3-clarification-1.md`.
- **Kod:** `a4760e1` (`feat/research-v3`). Tam suite 722 passed. Freeze öncesi ve sonrası iki bağımsız review yapıldı.
- **Koşular:** 2026-10-09; overnight controller üzerinden, `--jobs 2`. Çıktılar SHA256 ile checkpoint'lendi ve salt-okunur:
  - spot `var/reports/research_v3_spot_20261009/` (04:26 → 05:30 UK),
  - perp `var/reports/research_v3_perp_20261009/` (05:31 → 07:33 UK).
- **Aileler:**
  - A: perp–spot primi cross-section (crowding).
  - B: low-volatility cross-section.
  - C: vol-managed spot exposure.

  Haftalık rebalance, V2 motoru ve risk değerleri (SC-1, SC-2) değiştirilmeden kullanıldı.
- **Kriter 8:** Adayın günlük Sharpe'ı (edge varyantı), aile null baseline'ının ortalama günlük Sharpe'ından büyük olmalı.
  Null tanımları:
  - A/B: aynı uygun varlık kümesinde karıştırılmış skorlar, 5 seed.
  - C: aynı kitap, vol hedeflemesi olmadan.

  Kriter 1–7, V1/V2 ile aynı.
- **Kümülatif deneme sayısı** (aynı development seti): V1 208; V2 perp 184 + spot 88 grid noktası; V3 10 konfigürasyon,
  124 grid noktası.

## Karar (hesap başına ayrı)

| Hesap | Karar | Aday | İşlem | Ort. R (%90 CI) | Günlük Sharpe (aday / null) |
|---|---|---|---|---|---|
| **Perp** | **NO-GO** | **aday yok** | 32 | −0.16 [−0.33, −0.01] | −0.89 / +0.43 |
| **Spot** | **NO-GO** | **aday yok** | 97 | −0.02 [−0.06, +0.02] | −0.44 / −1.80 |

## Perp: NO-GO (aday yok)

- Run: `var/reports/research_v3_perp_20261009` (code `a4760e1`, git_dirty=False, last bar read 2026-06-30T23:45:00+00:00, data end 2026-07-01T00:00:00+00:00)
- Headline: NO-GO: failed expectancy_ci_lo, profit_factor, worst_day, cost_x2, funding_known_net, windows_positive, complexity_adds_value (complexity does not add value)

| Metric | Realistic | Edge |
|---|---|---|
| Trades | 32 | 32 |
| Mean R (90% bootstrap CI) | -0.16 [-0.33, -0.01] | -0.16 [-0.33, -0.01] |
| Profit factor | 0.61 | 0.61 |
| Net P&L | -20.08 $ | -20.08 $ |
| Max drawdown | 7.89% | 7.89% |
| Worst day | -3.23% | -3.23% |
| Positive windows / total | 0/11 | 0/11 |

| Sensitivity variant | Trades | Mean R (90% CI) | PF | Net | Positive windows |
|---|---|---|---|---|---|
| 2x costs, realistic (re-run under 2x costs) | 32 | -0.16 [-0.33, -0.01] | 0.61 | -20.19 $ | 0/11 |
| 2x costs, edge (frozen check cost_x2) | 32 | -0.16 [-0.33, -0.01] | 0.61 | -20.19 $ | 0/11 |
| alternative order model, edge | 32 | -0.16 [-0.33, -0.01] | 0.61 | -20.24 $ | 0/11 |

Realistic and edge are identical: the 15% drawdown kill switch never fired.

**GO/NO-GO checks (kriter 1–7 V2 §3.5; kriter 8 = V3 spec §4, null baseline, günlük Sharpe):**

| Check | Variant | Result | Detail |
|---|---|---|---|
| min_trades | edge | pass | n=32 (need >= 30) |
| expectancy_ci_lo | edge | FAIL | expectancy -0.157 R, 90% CI [-0.329, -0.010] |
| profit_factor | edge | FAIL | PF=0.61 (need >= 1.2) |
| max_drawdown | realistic | pass | max DD 7.89% (need < 15%) |
| worst_day | realistic | FAIL | worst day -3.23% (need > -3%) |
| daily_cvar5 | realistic | pass | daily CVaR5 -0.68% (need > -2%) |
| cost_x2 | edge, costs x2 | FAIL | expectancy under 2x costs -0.158 R (n=32, net -20.19 USD; need >= 0) |
| funding_known_net | edge | FAIL | funding-known period: 16 trades, net -14.88 USD (need > 0) |
| windows_positive | edge | FAIL | 0/11 windows net > 0 (0%; need >= 60%) |
| complexity_adds_value | edge | FAIL | [V3 null baseline] daily Sharpe -0.894 vs null 0.426 (mean of 5/5 seeds that traded); need strictly greater (info: expectancy -0.157 R vs null 0.098 R) |

**Naive momentum (V1, aynı hesap ve dönem; V3'te yalnızca bilgi):** n=287, mean R +1.44 [+0.08, +3.52], PF 1.54, net +1191.87 $, max DD 28.48%.

**Robustness (diagnostic only):**

- Full: n=32, mean R -0.16 [-0.33, -0.01]; without top 1 trade -0.22; without top 3 -0.32; trimmed mean -0.19; median trade -0.25; fragile=False.
- P&L concentration: top 1% of trades n/a, top 5% n/a, top 10% n/a of net (shares above 1 mean the rest lost money); without the top 1% of trades mean R -0.22; asset HHI 0.26.
- Asset leave-one-out (nested re-selection): ADA -0.13 / -26.26 $ (n=50), AVAX -0.16 / -20.08 $ (n=32), BNB -0.16 / -20.08 $ (n=32), BTC -0.20 / -12.69 $ (n=16), DOGE -0.16 / -20.08 $ (n=32), ETH -0.16 / -20.08 $ (n=32), LINK -0.01 / -3.89 $ (n=68), PEPE -0.16 / -20.08 $ (n=32), SOL -0.08 / -5.20 $ (n=16), SUI n/a / +0.00 $ (n=0), UNI -0.11 / -14.63 $ (n=32), XRP -0.24 / -15.33 $ (n=16), ZEC -0.08 / -5.20 $ (n=16)
- Period leave-one-out (by test window): 2024-09 -0.16, 2024-11 -0.16, 2025-01 -0.16, 2025-03 -0.16, 2025-05 -0.16, 2025-07 -0.24, 2025-09 -0.16, 2025-11 -0.16, 2026-01 -0.08, 2026-03 -0.16, 2026-05 -0.16
- Without best month (2025-08): -0.24; without best quarter (2025Q3): -0.24.

**Cost attribution (candidate, realistic):** fees +0.55 $, spread +0.30 $, slippage +0.22 $, known funding +0.09 $ (2154 unknown funding hours); cost/gross n/a - no gross edge; gross R -0.15 vs net R -0.16.

**Nested selection:** 2 window(s) traded, 9 flat (no accepted configuration).
- 2025-07: perp|v3_lowvol|1d|broad L90_k1_g1.5_v0.15_lowvol (train objective +0.14)
- 2026-01: perp|v3_premium|1d|broad L90_k1_g1.5_v0.1_premium (train objective +0.40)

**Configurations (each fit on train data only; edge variant):**

| Config | Trades | Mean R | Net |
|---|---|---|---|
| perp\|v3_premium\|1d\|broad | 16 | -0.24 | -15.33 $ |
| perp\|v3_lowvol\|1d\|broad | 16 | -0.08 | -5.20 $ |
| 2 other configs (no train-accepted window) | 0 | n/a | 0 |

**Trials (§3.3):** {'configs': 4, 'grid_points': 96, 'strategy_params_key_combinations': 0, 'cumulative_trials': {'v1': 208, 'v2': {'perp': 184, 'spot': 88}, 'v3': {'configs': 4, 'grid_points': 96}}}

## Spot: NO-GO (aday yok)

- Run: `var/reports/research_v3_spot_20261009` (code `a4760e1`, git_dirty=False, last bar read 2026-06-30T23:45:00+00:00, data end 2026-07-01T00:00:00+00:00)
- Headline: NO-GO: failed expectancy_ci_lo, profit_factor, cost_x2, funding_known_net, windows_positive

| Metric | Realistic | Edge |
|---|---|---|
| Trades | 97 | 97 |
| Mean R (90% bootstrap CI) | -0.02 [-0.06, +0.02] | -0.02 [-0.06, +0.02] |
| Profit factor | 0.72 | 0.72 |
| Net P&L | -7.47 $ | -7.47 $ |
| Max drawdown | 5.69% | 5.69% |
| Worst day | -1.68% | -1.68% |
| Positive windows / total | 0/11 | 0/11 |

| Sensitivity variant | Trades | Mean R (90% CI) | PF | Net | Positive windows |
|---|---|---|---|---|---|
| 2x costs, realistic (re-run under 2x costs) | 97 | -0.02 [-0.06, +0.02] | 0.69 | -8.50 $ | 0/11 |
| 2x costs, edge (frozen check cost_x2) | 97 | -0.02 [-0.06, +0.02] | 0.69 | -8.50 $ | 0/11 |
| alternative order model, edge | 97 | -0.02 [-0.06, +0.02] | 0.71 | -7.83 $ | 0/11 |

Realistic and edge are identical: the 15% drawdown kill switch never fired.

**GO/NO-GO checks (kriter 1–7 V2 §3.5; kriter 8 = V3 spec §4, null baseline, günlük Sharpe):**

| Check | Variant | Result | Detail |
|---|---|---|---|
| min_trades | edge | pass | n=97 (need >= 30) |
| expectancy_ci_lo | edge | FAIL | expectancy -0.019 R, 90% CI [-0.060, 0.023] |
| profit_factor | edge | FAIL | PF=0.72 (need >= 1.2) |
| max_drawdown | realistic | pass | max DD 5.69% (need < 15%) |
| worst_day | realistic | pass | worst day -1.68% (need > -3%) |
| daily_cvar5 | realistic | pass | daily CVaR5 -0.48% (need > -2%) |
| cost_x2 | edge, costs x2 | FAIL | expectancy under 2x costs -0.022 R (n=97, net -8.50 USD; need >= 0) |
| funding_known_net | edge | FAIL | no funding (spot): all trades: 97 trades, net -7.47 USD (need > 0) |
| windows_positive | edge | FAIL | 0/11 windows net > 0 (0%; need >= 60%) |
| complexity_adds_value | edge | pass | [V3 null baseline] daily Sharpe -0.437 vs null -1.801 (mean of 5/5 seeds that traded); need strictly greater (info: expectancy -0.019 R vs null -0.318 R) |

**Naive momentum (V1, aynı hesap ve dönem; V3'te yalnızca bilgi):** n=158, mean R +0.98 [-0.52, +3.51], PF 1.26, net +150.18 $, max DD 49.58%.

**Robustness (diagnostic only):**

- Full: n=97, mean R -0.02 [-0.06, +0.02]; without top 1 trade -0.03; without top 3 -0.03; trimmed mean -0.02; median trade -0.02; fragile=False.
- P&L concentration: top 1% of trades n/a, top 5% n/a, top 10% n/a of net (shares above 1 mean the rest lost money); without the top 1% of trades mean R -0.03; asset HHI 0.19.
- Asset leave-one-out (nested re-selection): ADA +0.01 / +2.57 $ (n=101), AVAX -0.02 / -3.50 $ (n=54), BNB -0.02 / -3.50 $ (n=54), BTC +0.00 / +0.61 $ (n=34), DOGE n/a / +0.00 $ (n=0), ETH +0.01 / +2.09 $ (n=95), LINK -0.02 / -7.20 $ (n=81), PEPE -0.02 / -6.58 $ (n=99), SOL +0.04 / +4.48 $ (n=29), SUI +0.00 / +0.45 $ (n=109), UNI -0.02 / -7.47 $ (n=97), XRP -0.06 / -2.45 $ (n=10), ZEC -0.02 / -3.50 $ (n=54)
- Period leave-one-out (by test window): 2024-09 -0.02, 2024-11 -0.02, 2025-01 -0.02, 2025-03 -0.02, 2025-05 -0.02, 2025-07 -0.02, 2025-09 -0.02, 2025-11 -0.02, 2026-01 -0.02, 2026-03 -0.02, 2026-05 -0.02
- Without best month (2025-04): -0.03; without best quarter (2025Q2): -0.03.

**Cost attribution (candidate, realistic):** fees +1.02 $, spread -0.00 $, slippage +0.00 $, known funding +0.00 $ (0 unknown funding hours); cost/gross n/a - no gross edge; gross R -0.02 vs net R -0.02.

**Nested selection:** 3 window(s) traded, 8 flat (no accepted configuration).
- 2025-01: spot|v3_volmanaged|1d|broad L30_k99_g1_v0.1 (train objective +0.09)
- 2025-03: spot|v3_volmanaged|1d|broad L30_k99_g1_v0.15 (train objective +0.14)
- 2025-11: spot|v3_volmanaged|1d|broad L30_k99_g1_v0.15 (train objective +0.06)

**Configurations (each fit on train data only; edge variant):**

| Config | Trades | Mean R | Net |
|---|---|---|---|
| spot\|v3_volmanaged\|1d\|broad | 97 | -0.01 | -3.46 $ |
| 5 other configs (no train-accepted window) | 0 | n/a | 0 |

**Trials (§3.3):** {'configs': 6, 'grid_points': 28, 'strategy_params_key_combinations': 0, 'cumulative_trials': {'v1': 208, 'v2': {'perp': 184, 'spot': 88}, 'v3': {'configs': 6, 'grid_points': 28}}}


## Yorum

1. **V3'ün araştırma sorusunun cevabı bu veride "hayır".** Üç yeni aile de, maliyet sonrası, kendi null baseline'ını ve
   V1/V2 kriterlerini geçen bir edge göstermedi. Her iki hesap da NO-GO. Bu kez işlem sayısı n ≥ 30'u geçti, yani
   karar örneklem yetersizliğinden değil, negatif ya da sıfır civarı sonuçtan geliyor.
2. **Perp:**
   - **Seçilen konfigürasyonlar:** İç içe seçim yalnızca iki pencerede bir konfigürasyonu kabul etti: 2025-07'de low-vol
     (geniş evren), 2026-01'de premium (geniş evren).
   - **Sonuç:** −0.16 R. CI tamamen negatif.
   - **Null ile karşılaştırma:** Karıştırılmış skorlu null, aynı varlıklarda daha iyi sonuç verdi (Sharpe +0.43). Yani bu
     iki sinyalin sıralaması, rastgele bir sıralamadan kötü.
   - **Konfigürasyon bazında:** Premium −0.24 R, low-vol −0.08 R (her biri 16 işlem). BTC/ETH evreninde hiçbiri kabul
     edilmedi.
3. **Spot:**
   - **Kabul edilen aile:** Yalnızca vol-managed (geniş evren) kabul edildi, üç pencerede. 97 işlem, −0.02 R.
   - **Null ile karşılaştırma:** Vol hedeflemesi, hedeflemesiz al-tut null'ından belirgin şekilde daha iyi bir Sharpe
     verdi (−0.44'e karşı −1.80). Yani riski azaltıyor, ama getiri sıfırın altında.
   - **Premium ve low-vol:** SC-2 spot ücretleri altında hiçbir train penceresinde kabul edilmedi.
4. **Bu sonuçlar keşif niteliğindedir** ve V1/V2'de defalarca kullanılmış development setine dayanır. Kümülatif deneme
   sayısı yüksek. Pozitif bir sonuç çıksaydı bile final generalization kanıtı sayılmazdı.

## Açık bilinen issue'lar

- **Funding geçmişi kısa:** 2025-10-06'dan itibaren. Perp'te bilinmeyen funding uydurulmuyor (`funding_known_net`
  kontrolü bu yüzden kısıtlı bir dönemi ölçüyor).
- **Premium günü kuralı:** İki venue'dan birinde 23:45 barı ileri doldurulmuşsa o günün primi NaN (clarification 1).
  MUBARAK pratikte premium ailesinin dışında kalıyor.
- **Low-vol perp kitabı dolar-nötr ama beta-nötr değil:** Adayın BTC korelasyonu −0.15. Bu bilgi amaçlı raporlanıyor.
- **Train seçimi V2 hedefini (ortalama R) kullanıyor:** Spec §5 böyle sabitledi. Bu, aileler arası seçimde vol-managed'ı
  dezavantajlı bırakabilir.
- **Survivorship bias:** Yalnızca bugün listelenen varlıklar veride var.
- **HTML raporu:** V2'den kalan bazı açıklama metinlerini taşıyor (ör. naive tablosunun başlığı). Yetkili metin
  summary.json'daki `v3` bloğu ve kontrol açıklamalarıdır.

## Sonuç

V3 kapsamındaki üç yeni alpha ailesi, bu development setinde aday üretmedi. Spec dondurulmuş olduğu için sonuçlara bakılarak
yeni bir iterasyon, parametre ya da kriter tasarlanmadı (kullanıcı talimatı ve 10:00 UK deadline'ı).

Bir sonraki adımlar ayrı bir kullanıcı kararıdır. Olası yönler:
- funding verisinin ileriye dönük toplanması (V4 adayı);
- final generalization setinin kullanımı (D7);
- araştırmanın askıya alınması.
