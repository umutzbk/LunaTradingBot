# Research V2: sonuçlar (development walk-forward OOS, 2024-09 → 2026-07)

- **Çalıştırma:** 2026-10-08, kod `d395c66` (temiz çalışma ağacı). Komutlar: `luna research-v2 --account perp --jobs 2` ve `--account spot --jobs 2`.
- **Süre:** Perp 17:16 → 20:18 (3 saat), spot 17:16 → 19:20 (2 saat).
- **Raporlar:**
  - `var/reports/research_v2_perp_20261008T141559/` (`report.html`, `summary.json`, `manifest.json`)
  - `var/reports/research_v2_spot_20261008T141600/` (`report.html`, `summary.json`, `manifest.json`)
- **Spec ve kurallar:**
  - Dondurulmuş V2 spec'i ve SC-1 değişiklik kaydı.
  - Önceden sabitlenmiş değerler P1–P12 (`docs/superpowers/plans/2026-10-08-luna-research-v2.md`). Sonuçlar görüldükten sonra hiçbir kriter ya da parametre değiştirilmedi.
- **Veri:**
  - Okunan son bar 2026-06-30 23:45 UTC (`V2_DATA_END = 2026-07-01`). `manifest.json` bunu kaydediyor.
  - 21 varlık. Geniş evren 13 varlık: ADA, AVAX, BNB, BTC, DOGE, ETH, LINK, PEPE, SOL, SUI, UNI, XRP, ZEC.
- **Final generalization seti** (2026-07 → 2026-10) **hiç okunmadı;** bu belgede onunla ilgili hiçbir metrik yok. `var/holdout_registry.json` yok.

## Karar (hesap başına ayrı)

| Hesap | Karar | Aday |
|---|---|---|
| **Perp** | **INSUFFICIENT DATA** (n=27 < 30) | **aday yok** |
| **Spot** | **INSUFFICIENT DATA** (n=0) | **aday yok** |

Hiçbir hesap GO almadı. Holdout'un kullanılıp kullanılmayacağı ayrı bir kullanıcı kararıdır (D7). Mevcut sonuçlar bunun için bir aday ortaya koymuyor.

## Perp: aday walk-forward'ı (iç içe, yalnızca train verisiyle seçim)

Her pencerede, kabul edilen konfigürasyonlar arasından train hedefi en yüksek olan seçildi. 11 pencerenin 8'inde hiçbir konfigürasyon ranker kuralını (n ≥ 30, shrinkage, bootstrap alt sınırı > 0) geçemedi; bu pencerelerde hesap pozisyonsuz kaldı.

| Test penceresi | Seçilen konfigürasyon | Parametre | Train hedefi (R) |
|---|---|---|---|
| 2025-01 | TSMOM 15m, geniş evren | F96_S480_k2 | 1.84 |
| 2025-11 | breakout 15m, BTC/ETH | N960_k3 | 0.14 |
| 2026-03 | breakout 1h, geniş evren | N240_k2 | 0.30 |
| diğer 8 pencere | — (pozisyonsuz) | | |

| Kontrol (§3.5) | Varyant | Sonuç |
|---|---|---|
| min_trades | edge | ❌ n=27 |
| expectancy_ci_lo | edge | ❌ +0.274 R, %90 CI [−0.37, +0.96] |
| profit_factor | edge | ✅ 1.44 |
| max_drawdown | gerçekçi | ✅ %14.95 (sınır %15) |
| worst_day | gerçekçi | ❌ −%7.14 |
| daily_cvar5 | gerçekçi | ✅ −%0.96 |
| cost_x2 | edge, 2× maliyet | ✅ +0.189 R |
| funding_known_net | edge | ✅ 21 işlem, +$10.04 |
| windows_positive | edge | ❌ 2/11 |
| complexity_adds_value | edge | ❌ +0.274 R < naive momentum +1.436 R |

**En kötü gün (−%7.14, 2025-01-07):** Hata değil. Kârlı bir DOGE long pozisyonu, DOGE'un gün içi düşüşüyle kârının büyük kısmını geri verdi. Pozisyon +$12.8 net kârla kapandı, ama equity aynı gün 300'den 278'e indi. V1'in %2'lik acil stop'u giriş fiyatına göre zararı ölçtüğü için kârın geri verilmesinde tetiklenmez.

### Varyantlar

Kill switch hiç tetiklenmedi, bu yüzden gerçekçi ve edge varyantları aynı.

| Varyant | İşlem | Ort. R | %90 CI | Net $ |
|---|---|---|---|---|
| gerçekçi = edge | 27 | +0.27 | [−0.37, +0.96] | +14.1 |
| 2× maliyet (edge) | 27 | +0.19 | [−0.46, +0.89] | +9.3 |
| 2× maliyet (gerçekçi) | 14 | −0.08 | [−0.88, +0.84] | −2.7 |
| maker emir (edge) | 26 | +0.30 | [−0.38, +1.09] | +14.7 |
| funding 0× / tahmini / 2× | 27 | +0.27 | — | ≈ +14.1 |

### Robustness (yalnızca diagnostic, kararı etkilemez): **"kırılgan"**

- **En iyi işlemler çıkarılınca:** Ortalama R tam haliyle +0.27, en büyük işlem çıkarılınca +0.02, en büyük 3 işlem çıkarılınca **−0.43**.
- **Merkezi eğilim:** Trimmed ortalama +0.08, **medyan işlem −0.58 R**. Payoff oranı 2.9.
- **Kâr yoğunlaşması:**
  - Net kârın %99'u tek bir işlemden geliyor (top-1 payı 0.99).
  - En büyük 3 işlemin toplamı net kârın 2,5 katı; geri kalan işlemler toplamda zararda.
- **Varlık bazında leave-one-out:** Varlık çıkarılınca seçim yeniden yapılıyor. DOGE ya da SUI çıkarılınca ortalama negatife dönüyor (−0.02 / −0.05). BTC, SOL, ZEC, LINK ve XRP çıkarılınca sıfıra yakın kalıyor (0.00–0.05).
- **Dönem bazında:** En iyi ay çıkarılınca +0.03. Zaman leave-one-out'unda 2026-03 penceresi çıkarılınca +0.01.
- **Maliyet:**
  - Fee $2.65, spread $1.07, slippage $1.06, bilinen funding +$0.40.
  - Maliyet / brüt kâr oranı %26; cap-adjusted R ile planned R aynı (+0.27).
  - Maker emir varyantında doluluk oranı %90. Dolmayan 3 girişin taker olarak alınsaydı ortalama getirisi +1.05 R olurdu.

### Konfigürasyonlar (her biri ayrı, yalnızca train verisiyle fit edildi; edge varyantı)

| Konfigürasyon | Kabul edilen pencere | İşlem | Ort. R | Net $ |
|---|---|---|---|---|
| TSMOM 15m, geniş | 1 | 6 | +0.37 | +4.1 (kırılgan) |
| breakout 15m, BTC/ETH | 1 | 13 | −0.16 | −4.2 |
| breakout 15m, geniş | 2 | 36 | −0.07 | −5.5 |
| breakout 1h, geniş | 1 | 8 | +0.90 | +14.2 |
| cross-sectional (haftalık), geniş | 1 | 16 | +0.09 | +5.8 (kırılgan) |
| cross-sectional (günlük, sensitivity), BTC/ETH | 2 | 244 | +0.00 | +0.6 (kırılgan) |
| cross-sectional (günlük, sensitivity), geniş | 3 | 480 | −0.01 | −18.9 |
| kalan 13 konfigürasyon (TSMOM 1h/4h/1d, breakout 1h/4h/1d BTC/ETH ve 4h/1d, cross-sectional BTC/ETH haftalık) | 0 | 0 | — | 0 |

**Naive momentum (aynı hesap ve dönem, V1):** 287 işlem, +1.44 R, %90 CI [+0.08, +3.53], +$1192, max DD %28. Bu sonuç XRP'deki +300R'lik tek işlemden geliyor (`docs/results/xrp-outlier-investigation.md`).

## Spot

- **18 konfigürasyonun 16'sı:** 11 OOS penceresinin hiçbirinde ranker kuralını geçemedi. Aday walk-forward'ı **0 işlem** yaptı.
- **Sensitivity (günlük cross-sectional, geniş evren):** Tek bir pencerede kabul edildi. 59 işlem, −0.17 R, CI [−0.35, …], −$44, max DD %18.
- **Naive momentum (spot, long-only):** 158 işlem, +1.39 R, CI [−0.42, +4.48], +$254, max DD %46.

## Deneme sayıları (§3.3)

| | Perp | Spot |
|---|---|---|
| Konfigürasyon (birincil + günlük sensitivity) | 20 | 16 |
| Grid noktası | 184 | 88 |
| Strateji × parametre × varlık kombinasyonu (sinyal aileleri) | 572 | 416 |

## Yorum

1. **V2'nin araştırma sorusunun cevabı bu veride "hayır".** Momentum ailesinde, maliyetler düşüldükten sonra OOS'ta tekrar eden ve tek işleme, tek varlığa ya da tek döneme bağlı olmayan bir edge bulunmadı.
   - Perp'teki küçük pozitif sonuç (+0.27 R, 27 işlem) birkaç işleme dayanıyor; en büyük 3 işlem çıkarılınca negatife dönüyor.
   - Kâr esas olarak tek bir DOGE işleminden geliyor.
2. **Yavaş timeframe'ler sorunu çözmedi.** 1h/4h/1d TSMOM ve breakout, train pencerelerinde neredeyse hiç kabul edilmedi. Bu timeframe'lerde 6 aylık train penceresi n ≥ 30 işlem üretmekte de zorlanıyor (spec §10'da öngörülen risk).
3. **Cross-sectional momentum (haftalık, market-neutral)** yalnızca bir pencerede kabul edildi ve orada da kırılgan çıktı. Günlük versiyonu çok işlem yaptı ama sıfır civarında ya da negatif.
4. **Spot'ta, perp'teki V1 sonucunda olduğu gibi, maliyet bariyeri belirleyici.** Spot fee'leri (maker %0.25) altında hiçbir konfigürasyon train'de güvenilir bir edge göstermedi.
5. **Naive momentum'un yüksek ortalaması hâlâ tek bir uç işleme bağlı.** Luna'nın sıkı kabul kuralı bu tür kalın kuyruklu getirileri yakalamıyor. Bu, spec'in bilerek kabul ettiği bir sınır (D3: kabul kuralı değişmez).

## Sonraki adımlar (karar senin)

- **Holdout:** Mevcut sonuçlara göre holdout'u harcamak için bir aday yok; D7 gereği ayrı karar.
- **Yeni araştırma yönleri:** Ancak yeni bir spec ile (V3) ele alınabilir:
  - kabul kuralının kalın kuyruklu stratejiler için yeniden tanımlanması;
  - funding/basis carry gibi momentum dışı edge kaynakları;
  - daha uzun train pencereleri.

  V2 spec'i dondurulmuş olduğu için bunlar V2 sonuçlarına göre yapılmış ayarlamalar sayılmaz; ayrı bir deney olarak tanımlanmaları gerekir.
- **Paper bot'lar:** V1 modelleriyle çalışmaya devam ediyor. Kabul edilen hücre olmadığı için işlem açmıyorlar.
