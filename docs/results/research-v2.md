# Research V2: final sonuçlar (development walk-forward OOS, 2024-09 → 2026-07)

> **Durum: CLOSED — NO CANDIDATE (2026-10-09).** Perp ve spot için final, düzeltilmiş koşuların ikisi de
> INSUFFICIENT DATA verdi; hiçbir hesap aday üretmedi. Bu belge yalnızca final koşuları içerir ve
> 2026-10-08 tarihli önceki sürümün (kod `d395c66`, SC-2 öncesi spot ücreti) yerini alır.

- **Kod:** `e0203c8`. Kaynak kodu `1cc450a` ile aynı; aradaki tek commit handoff dokümanı.
  `git diff 1cc450a e0203c8 -- luna tests pyproject.toml uv.lock config` boş. Çalışma ağacı temiz (`git_dirty=false`).
- **Spec ve kurallar:**
  - Dondurulmuş V2 spec'i, SC-1 (cross-sectional emir limitleri) ve SC-2 (Kraken spot ücreti 0.40/0.80).
  - Önceden sabitlenmiş P1–P12. Sonuçlar görüldükten sonra hiçbir kriter ya da parametre değiştirilmedi.
- **Koşular** (yeni PC, 2026-10-08/09; perp `--jobs 2`, spot kısmen `--jobs 1` ile, birim checkpoint'lerinden devam ederek; kesintiler aşağıda):
  - Spot: `var/reports/research_v2_spot_20261008T222035/`.
    - Bellek baskısı yüzünden iki kez kesildi; manifest doğrulamalı `--resume` ile devam etti.
  - Perp: `var/reports/research_v2_perp_20261009T000905/` (00:09 → 02:37 UTC; `var/research_v2_perp_final.log`).
  - Çıktıların SHA256 değerleri `var/overnight/checkpoints/` altında kayıtlı; dosyalar salt-okunur.
- **Veri** (PC değişimi nedeniyle yeniden indirildi; eski `var/` taşınamadı):
  - Perp: eski koşudaki 21 varlık (`--assets` ile açıkça verildi), Kraken Futures API'sinden. Funding verisi 2025-10-06'dan itibaren (`var/funding`, Kraken'ın döndürdüğü geçmiş).
  - Spot: yalnızca SHA256 ile sabitlenmiş Kraken OHLCVT arşivi (`Kraken_OHLCVT_Full_2026Q2`); 2026-07-01 sonrası için boşluk doldurma yapılmadı.
  - Instrument cache'leri 2026-10-08'de yeniden çekildi.
  - Okunan son bar 2026-06-30 23:45 UTC (`V2_DATA_END = 2026-07-01`); son funding kaydı 2026-06-30 23:00.
  - **Tekrarlanabilirlik kontrolü:**
    - Spot sonucu, eski PC'deki final koşuyla birebir aynı: 0 işlem; naive 158 işlem, +0.98 R, +$150.
    - Perp sonucunun ana metrikleri (işlem sayısı, ortalama R, CI, GO/NO-GO kontrolleri) `d395c66` koşusuyla aynı: 27 işlem, +0.27 R, CI [−0.37, +0.96]. Closure düzeltmeleri (M1–M12, M-B, I-1) bazı ikincil metrikleri değiştirdi: örneğin maliyet/brüt oranı %26'dan %24'e indi, naive CI üst sınırı +3.53'ten +3.52'ye.
- **Final generalization seti** (2026-07 → 2026-10) **hiç okunmadı.** Bu belgede onunla ilgili hiçbir metrik yok.
  `var/holdout_registry.json` yok.
- **Özet script'i:** `uv run python scripts/v2_results_summary.py <summary.json> ...`. Aşağıdaki hesap bölümleri bu
  script'in çıktısıdır.

## Karar (hesap başına ayrı)

| Hesap | Karar | Aday | İşlem | Ort. R (%90 CI) | Naive momentum |
|---|---|---|---|---|---|
| **Perp** | **INSUFFICIENT DATA** (n=27 < 30) | **aday yok** | 27 | +0.27 [−0.37, +0.96] | 287 işlem, +1.44 R [+0.08, +3.52] |
| **Spot** | **INSUFFICIENT DATA** (n=0) | **aday yok** | 0 | n/a | 158 işlem, +0.98 R [−0.52, +3.51] |

İki hesap da GO alamadı. V2 **CLOSED — NO CANDIDATE** olarak kapatıldı. Final generalization setinin kullanımı ayrı bir
kullanıcı kararıdır (D7); bu sonuçlar onun için bir aday ortaya koymuyor.

## Perp: INSUFFICIENT DATA (aday yok)

- Run: `var/reports/research_v2_perp_20261009T000905` (code `e0203c8`, git_dirty=False, last bar read 2026-06-30T23:45:00+00:00, data end 2026-07-01T00:00:00+00:00)
- Headline: INSUFFICIENT DATA: n=27 (need >= 30) (also failing: expectancy_ci_lo, worst_day, windows_positive, complexity_adds_value)

| Metric | Realistic | Edge |
|---|---|---|
| Trades | 27 | 27 |
| Mean R (90% bootstrap CI) | +0.27 [-0.37, +0.96] | +0.27 [-0.37, +0.96] |
| Profit factor | 1.44 | 1.44 |
| Net P&L | +14.11 $ | +14.11 $ |
| Max drawdown | 14.95% | 14.95% |
| Worst day | -7.14% | -7.14% |
| Positive windows / total | 2/11 | 2/11 |

| Sensitivity variant | Trades | Mean R (90% CI) | PF | Net | Positive windows |
|---|---|---|---|---|---|
| 2x costs, realistic (re-run under 2x costs) | 14 | -0.08 [-0.88, +0.84] | 0.87 | -2.66 $ | 1/11 |
| 2x costs, edge (frozen check cost_x2) | 27 | +0.19 [-0.46, +0.89] | 1.27 | +9.33 $ | 2/11 |
| alternative order model, edge | 26 | +0.30 [-0.38, +1.09] | 1.50 | +14.67 $ | 2/11 |

Realistic and edge are identical: the 15% drawdown kill switch never fired.

**GO/NO-GO checks (frozen §3.5):**

| Check | Variant | Result | Detail |
|---|---|---|---|
| min_trades | edge | FAIL | n=27 (need >= 30) |
| expectancy_ci_lo | edge | FAIL | expectancy 0.274 R, 90% CI [-0.367, 0.964] |
| profit_factor | edge | pass | PF=1.44 (need >= 1.2) |
| max_drawdown | realistic | pass | max DD 14.95% (need < 15%) |
| worst_day | realistic | FAIL | worst day -7.14% (need > -3%) |
| daily_cvar5 | realistic | pass | daily CVaR5 -0.96% (need > -2%) |
| cost_x2 | edge, costs x2 | pass | expectancy under 2x costs 0.189 R (n=27, net 9.33 USD; need >= 0) |
| funding_known_net | edge | pass | funding-known period: 21 trades, net 10.04 USD (need > 0) |
| windows_positive | edge | FAIL | 2/11 windows net > 0 (18%; need >= 60%) |
| complexity_adds_value | edge | FAIL | Luna 0.274 R vs best naive 1.436 R (need strictly greater) |

**Naive momentum baseline (V1, same account and span):** n=287, mean R +1.44 [+0.08, +3.52], PF 1.54, net +1191.87 $, max DD 28.48%.

**Robustness (diagnostic only):**

- Full: n=27, mean R +0.27 [-0.37, +0.96]; without top 1 trade +0.02; without top 3 -0.43; trimmed mean +0.08; median trade -0.58; fragile=True.
- P&L concentration: top 1% of trades 0.99, top 5% 1.90, top 10% 2.51 of net (shares above 1 mean the rest lost money); without the top 1% of trades mean R +0.02; asset HHI 0.34.
- Asset leave-one-out (nested re-selection): ADA +0.27 / +14.11 $ (n=27), AVAX +0.27 / +14.11 $ (n=27), BNB +0.27 / +14.11 $ (n=27), BTC +0.00 / -3.55 $ (n=61), DOGE -0.02 / -2.80 $ (n=39), ETH +0.26 / +21.33 $ (n=43), LINK +0.05 / +2.65 $ (n=31), PEPE +0.27 / +14.11 $ (n=27), SOL +0.03 / +1.04 $ (n=45), SUI -0.05 / -24.04 $ (n=43), UNI +0.27 / +14.11 $ (n=27), XRP +0.05 / +2.65 $ (n=31), ZEC +0.01 / -0.06 $ (n=19)
- Period leave-one-out (by test window): 2024-09 +0.27, 2024-11 +0.27, 2025-01 +0.25, 2025-03 +0.27, 2025-05 +0.27, 2025-07 +0.27, 2025-09 +0.27, 2025-11 +0.67, 2026-01 +0.27, 2026-03 +0.01, 2026-05 +0.27
- Without best month (2025-01): +0.03; without best quarter (2026Q2): +0.11.

**Cost attribution (candidate, realistic):** fees +2.65 $, spread +1.07 $, slippage +1.06 $, known funding +0.40 $ (306 unknown funding hours); cost/gross 0.24; gross R +0.35 vs net R +0.27.

**Nested selection:** 3 window(s) traded, 8 flat (no accepted configuration).
- 2025-01: perp|tsmom|15m|broad F96_S480_k2 (train objective +1.84)
- 2025-11: perp|breakout|15m|btc_eth N960_k3 (train objective +0.14)
- 2026-03: perp|breakout|1h|broad N240_k2 (train objective +0.30)

**Configurations (each fit on train data only; edge variant):**

| Config | Trades | Mean R | Net |
|---|---|---|---|
| perp\|tsmom\|15m\|broad | 6 | +0.37 | +4.07 $ |
| perp\|breakout\|15m\|btc_eth | 13 | -0.16 | -4.17 $ |
| perp\|breakout\|15m\|broad | 36 | -0.07 | -5.49 $ |
| perp\|breakout\|1h\|broad | 8 | +0.90 | +14.16 $ |
| perp\|xsec\|1d\|broad | 16 | +0.09 | +5.84 $ |
| perp\|xsec\|1d\|btc_eth\|daily | 244 | +0.00 | +0.63 $ |
| perp\|xsec\|1d\|broad\|daily | 480 | -0.01 | -18.95 $ |
| 13 other configs (no train-accepted window) | 0 | n/a | 0 |

**Trials (§3.3):** {'configs': 20, 'grid_points': 184, 'strategy_params_key_combinations': 572}

## Spot: INSUFFICIENT DATA (aday yok)

- Run: `var/reports/research_v2_spot_20261008T222035` (code `e0203c8`, git_dirty=False, last bar read 2026-06-30T23:45:00+00:00, data end 2026-07-01T00:00:00+00:00)
- Headline: INSUFFICIENT DATA: n=0 (need >= 30) (also failing: expectancy_ci_lo, profit_factor, cost_x2, funding_known_net, windows_positive, complexity_adds_value)

| Metric | Realistic | Edge |
|---|---|---|
| Trades | 0 | 0 |
| Mean R (90% bootstrap CI) | n/a [n/a, n/a] | n/a [n/a, n/a] |
| Profit factor | n/a | n/a |
| Net P&L | +0.00 $ | +0.00 $ |
| Max drawdown | 0.00% | 0.00% |
| Worst day | 0.00% | 0.00% |
| Positive windows / total | 0/11 | 0/11 |

| Sensitivity variant | Trades | Mean R (90% CI) | PF | Net | Positive windows |
|---|---|---|---|---|---|
| 2x costs, realistic (re-run under 2x costs) | 0 | n/a [n/a, n/a] | n/a | +0.00 $ | 0/11 |
| 2x costs, edge (frozen check cost_x2) | 0 | n/a [n/a, n/a] | n/a | +0.00 $ | 0/11 |
| alternative order model, edge | 0 | n/a [n/a, n/a] | n/a | +0.00 $ | 0/11 |

Realistic and edge are identical: the 15% drawdown kill switch never fired.

**GO/NO-GO checks (frozen §3.5):**

| Check | Variant | Result | Detail |
|---|---|---|---|
| min_trades | edge | FAIL | n=0 (need >= 30) |
| expectancy_ci_lo | edge | FAIL | expectancy nan R, 90% CI [nan, nan] |
| profit_factor | edge | FAIL | PF=nan (need >= 1.2) |
| max_drawdown | realistic | pass | max DD 0.00% (need < 15%) |
| worst_day | realistic | pass | worst day 0.00% (need > -3%) |
| daily_cvar5 | realistic | pass | daily CVaR5 0.00% (need > -2%) |
| cost_x2 | edge, costs x2 | FAIL | expectancy under 2x costs nan R (n=0, net 0.00 USD; need >= 0) |
| funding_known_net | edge | FAIL | no funding (spot): all trades: 0 trades, net 0.00 USD (need > 0) |
| windows_positive | edge | FAIL | 0/11 windows net > 0 (0%; need >= 60%) |
| complexity_adds_value | edge | FAIL | Luna nan R vs best naive 0.981 R (need strictly greater) |

**Naive momentum baseline (V1, same account and span):** n=158, mean R +0.98 [-0.52, +3.51], PF 1.26, net +150.18 $, max DD 49.58%.

**Robustness / asset LOO / period LOO:** not applicable: the candidate walk-forward made 0 trades.

**Cost attribution (candidate, realistic):** fees +0.00 $, spread +0.00 $, slippage +0.00 $, known funding +0.00 $ (0 unknown funding hours); cost/gross n/a - no gross edge; gross R n/a vs net R n/a.

**Nested selection:** 0 window(s) traded, 11 flat (no accepted configuration).

**Configurations (each fit on train data only; edge variant):**

| Config | Trades | Mean R | Net |
|---|---|---|---|
| 16 other configs (no train-accepted window) | 0 | n/a | 0 |

**Trials (§3.3):** {'configs': 16, 'grid_points': 88, 'strategy_params_key_combinations': 416}

## Yorum

1. **V2'nin araştırma sorusunun cevabı bu veride "hayır".** Momentum ailesinde (TSMOM ve breakout 15m–1d,
   cross-sectional haftalık/günlük), maliyet sonrası, OOS'ta tekrar eden ve tek işleme, varlığa ya da döneme bağlı
   olmayan bir edge bulunmadı.
2. **Perp'teki küçük pozitif sonuç kırılgan.** +0.27 R'lik sonuç 27 işleme dayanıyor:
   - En büyük işlem çıkarılınca +0.02, en büyük 3 işlem çıkarılınca −0.43.
   - Medyan işlem −0.58 R.
   - DOGE ya da SUI evrenden çıkarılınca negatife dönüyor.
3. **Spot'ta maliyet bariyeri bağlayıcı.** SC-2 ücretleri altında 16 konfigürasyonun hiçbiri hiçbir train penceresinde
   ranker kuralını geçmedi.
4. **Yavaş timeframe'ler örneklem sorununu çözmedi.** 1h, 4h ve 1d TSMOM/breakout konfigürasyonlarından yalnızca
   breakout 1h (geniş) bir pencerede kabul edildi; geri kalanların hiçbiri hiçbir train penceresinde ranker kuralını
   (n ≥ 30 dahil) geçmedi (konfigürasyon tablosu).
5. **Naive momentum'un yüksek ortalaması tek uç işlemlere bağlı.** Perp'te +300R'lik XRP işlemi var; spot'ta CI sıfırı
   kesiyor.

Ayrıntılı teşhis: `docs/results/v1-v2-failure-analysis.md`.

## Açık bilinen issue'lar

- **Veri kökeni (PC değişimi):** Perp verisi API'den yeniden indirildi. Manifest veri hash'i tutmadığı için eski veriyle
  birebir aynı olduğu kanıtlanamıyor. Sonuçların eski koşularla birebir aynı çıkması (yukarıda) bunu destekliyor.
  Spot verisi pinned arşivden geldiği için birebir aynı.
- **Funding geçmişi kısa:** Perp adayında 306 bilinmeyen funding saati var. Bilinmeyen funding uydurulmuyor; V1'in
  worst-case kuralı yalnızca train hedefinde uygulanıyor.
- **En kötü gün −%7.14 (perp):** Hata değil. Önceki koşudaki inceleme (`git show 93aa3ef:docs/results/research-v2.md`) bunu 2025-01-07'deki kârlı bir DOGE pozisyonunun gün içinde kârının çoğunu geri vermesine bağlamıştı; değer final koşuda aynı.
  %2'lik acil stop girişe göre ölçüldüğü için kâr erimesinde tetiklenmiyor.
- **Survivorship bias:** Yalnızca bugün listelenen varlıklar veride var (raporun uyarısı).
- **M9 (kabul edildi):** `order_model` trade-list cache anahtarında; her V1 cache'i bir kez yeniden hesaplanıyor.
- **Review'ın "declined to judge" maddeleri** (`2026-10-08-research-v2-final-review.md`) spec ve P-kararlarıyla önceden
  sabitlendiği için geçerli kalıyor.
- **Operasyonel (sonuçları etkilemez):**
  - Spot gap-fill backfill'i (Trades endpoint), checkpoint dosyası aynı anda okunursa Windows'ta `os.replace`
    sırasında `PermissionError` ile düşebiliyor.
  - Claude Code oturum dosyaları (`.claude/`) repoda takip edilmeyen dosya olarak görünüp `git_dirty`'yi
    bozabiliyordu. Yerel `.git/info/exclude` ile çözüldü.
- **V1 spot walk-forward:** SC-2 öncesi ücretle kaldı ve superseded olarak işaretli. Yeniden koşulmadı, çünkü
  2026-07-01 sonrası barları okurdu.

## Deneme sayıları (§3.3)

| | Perp | Spot |
|---|---|---|
| Konfigürasyon (birincil + günlük sensitivity) | 20 | 16 |
| Grid noktası | 184 | 88 |
| Strateji × parametre × varlık kombinasyonu (sinyal aileleri) | 572 | 416 |
