# P1.5: walk-forward sonuçları (spot)

- **Çalıştırma:** 2026-10-08, kod `b9bbf1e`, `luna walkforward --account spot`, süre 18,8 dakika
- **Veri:** Kraken spot OHLCVT resmi arşivi (2026Q2'ye kadar, pinned SHA256) + Trades endpoint ile boşluk doldurma. 21 varlıklık panel: ADA, AVAX, BNB, BTC, DOGE, ENA, ETH, FET, HYPE, LINK, MUBARAK, NEAR, PEPE, PUMP, SOL, SUI, TAO, UNI, XLM, XRP, ZEC.
- **Out-of-sample dönem:** 2024-09-01 → 2026-07-01 (development walk-forward OOS). 11 test penceresi; her pencere 6 ay eğitim + 2 ay test. Daha önceki 7 pencere `min_history_days` kuralı nedeniyle boş evrenle atlandı. **Holdout (2026-07 → 2026-10) dokunulmadan duruyor.**
- **Spot kısıtları:** yalnızca long, 1x, funding yok, pairs yok.
- **Denenen kombinasyon:** 208 (strateji × parametre × varlık)
- **Rapor:** `var/reports/walkforward_spot_20261008T054124/report.html`

## Karar: **INSUFFICIENT DATA** (n=0 < 30)

Ranker 11 pencerenin hiçbirinde bir hücre kabul etmedi. Luna bu dönemde **hiç işlem açmadı.**

| Kontrol | Varyant | Sonuç |
|---|---|---|
| min_trades | edge | ❌ n=0 (en az 30 gerekli) |
| expectancy_ci_lo | edge | ❌ işlem yok |
| profit_factor | edge | ❌ işlem yok |
| max_drawdown | gerçekçi | ✅ %0 (işlem yok) |
| worst_day | gerçekçi | ✅ %0 (işlem yok) |
| daily_cvar5 | gerçekçi | ✅ %0 (işlem yok) |
| cost_x2 | edge, 2× maliyet | ❌ işlem yok |
| funding_known_net | edge | ❌ spot'ta funding yok; 0 işlem |
| windows_positive | edge | ❌ 0/11 |
| complexity_adds_value | edge | ❌ Luna işlem yok, en iyi naive +1.39 R |

Geçen üç kontrolün geçmesinin nedeni hiç işlem açılmaması. Bu kontroller burada bir şey kanıtlamıyor.

## Pencereler (edge)

| Test başlangıcı | Evren | Kabul edilen hücre | İşlem |
|---|---|---|---|
| 2024-09 | BTC, ETH, SOL, XRP | 0 | 0 |
| 2024-11 | BTC, SOL, ETH, DOGE | 0 | 0 |
| 2025-01 | BTC, ETH, SOL, XRP, DOGE, PEPE, LINK, ADA, SUI | 0 | 0 |
| 2025-03 | BTC, ETH, SOL, XRP, DOGE | 0 | 0 |
| 2025-05 | BTC, ETH, SOL, XRP, SUI, DOGE, ADA | 0 | 0 |
| 2025-07 | BTC, ETH, SOL, XRP, DOGE, SUI, UNI | 0 | 0 |
| 2025-09 | ETH, BTC, SOL, XRP, SUI, LINK, ADA, DOGE | 0 | 0 |
| 2025-11 | BTC, ETH, SOL, XRP, DOGE, BNB, ZEC, SUI, AVAX, ADA | 0 | 0 |
| 2026-01 | BTC, ETH, SOL, ZEC, XRP, SUI | 0 | 0 |
| 2026-03 | BTC, ETH, SOL, XRP, ZEC, DOGE | 0 | 0 |
| 2026-05 | BTC, ETH, SOL, XRP, ZEC | 0 | 0 |

## Varyantlar ve ablation'lar

| Koşu | İşlem | Ort. R | %90 CI | Net $ | Max DD |
|---|---|---|---|---|---|
| Luna gerçekçi = edge = 2× maliyet | 0 | — | — | 0 | %0 |
| Rejim filtresi kapalı (no_regime) | 0 | — | — | 0 | %0 |
| Ranker yerine rastgele seçim (random_ranker) | 426 | −0.26 | [−0.37, −0.14] | **−139.8** | **%54** |

Rastgele seçimde brüt P&L zaten negatif (−$20). Fee'ler ($133) kaybı büyütüyor. Ranker'ın her şeyi reddetmesi bu dönemde sermayeyi korudu, ama ortaya çıkarılabilecek bir edge yok.

## Naive baseline'lar: aynı OOS dönemi, spot kısıtlarıyla

| Strateji | İşlem | Ort. R | %90 CI | PF | Max DD | Fee $ | Net $ |
|---|---|---|---|---|---|---|---|
| naive momentum (EMA 96/480) | 158 | **+1.39** | [−0.42, +4.48] | 1.39 | **%46** | 207 | **+253.5** |
| naive mean reversion | 415 | −0.31 | — | 0.44 | %60 | 152 | −156.5 |
| naive pairs | — | — | — | — | — | — | atlandı (spot'ta short yok) |

- Naive momentum'un **güven aralığı sıfırı kesiyor**, max DD %46 ve kazanma oranı %17. Perp'teki tabloyla aynı: kâr birkaç büyük işlemden geliyor ve kanıt zayıf.
- Spot fee'leri (maker %0.25) brüt kârın neredeyse yarısını götürüyor (brüt $438 → net $254).
- Benchmark'lar (rapordaki toplam getiri, OOS dönemi): nakit 0; BTC al-tut −%0.8; eşit ağırlıklı evren al-tut −%0.2.

## Yorum

1. **Spot'ta da Luna'nın karmaşık hali değer katmadı.** Perp'teki gibi, ranker'ın kabul kuralı (n ≥ 30, shrinkage, bootstrap alt sınır > 0) hiçbir hücreyi geçirmedi. Spot'ta bu yüzden hiç işlem açılmadı.
2. **Rastgele seçim açıkça zararlı** (−0.26 R, CI tamamen negatif). Ranker'ın reddi doğru yönde çalışıyor.
3. **Mean reversion spot'ta da güvenilir biçimde negatif.** Bu sonuç perp ve in-sample bulgularla tutarlı.
4. **Naive momentum yine tek umut verici yön**, ama kalın kuyruklu ve CI sıfırı kesiyor. Bu, Research V2'nin (haftalık kesitsel momentum, D1) ele aldığı soru. V2 spec'i dondurulmuş durumda; bu sonuç spec'te hiçbir kriteri veya parametreyi değiştirmiyor.
5. **Holdout çalıştırılmadı.** `var/holdout_registry.json` yok.

## Notlar

- İlk spot walk-forward denemesi, eski (12 varlıklık) instrument cache ile başlamıştı. Cache yenilendi (21 varlık), koşu baştan yapıldı. Yukarıdaki sonuçlar yeni koşuya ait.
- Rapordaki uyarılar geçerli: survivorship bias (yalnızca bugün listelenen varlıklar var), rolling rejim eşikleri, 2026-07-01 → 07-08 arasında 7 günlük boşluk, işlemsiz barlar ileri doldurulur ve bu barlarda fill olmaz.
