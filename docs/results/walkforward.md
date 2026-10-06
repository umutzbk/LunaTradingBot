# P1.5: walk-forward sonuçları (perp)

- **Çalıştırma:** 2026-10-06, kod `2727a66`, `luna walkforward --account perp`, süre 17,5 dakika
- **Out-of-sample dönem:** 2024-09-01 → 2026-07-01. 11 test penceresi; her pencere 6 ay eğitim + 2 ay test. Holdout (2026-07 → 2026-10) dokunulmadan duruyor.
- **Denenen kombinasyon:** 208 (strateji × parametre × varlık/parite)
- **Rapor:** `var/reports/walkforward_perp_20261006T124844/report.html`

## Karar: **INSUFFICIENT DATA** (n=24 < 30), diğer kriterlerin çoğu da başarısız

| Kontrol | Varyant | Sonuç |
|---|---|---|
| min_trades | edge | ❌ n=24 (en az 30 gerekli) |
| expectancy_ci_lo | edge | ❌ −0.121 R, %90 CI [−0.49, +0.40] |
| profit_factor | edge | ❌ PF 0.76 (en az 1.2 gerekli) |
| max_drawdown | gerçekçi | ✅ %7.0 |
| worst_day | gerçekçi | ✅ −%1.46 |
| daily_cvar5 | gerçekçi | ✅ −%0.22 |
| cost_x2 | edge, 2× maliyet | ❌ −0.203 R |
| funding_known_net | edge | ❌ funding'in bilindiği dönemde 0 işlem |
| windows_positive | edge | ❌ 0/11 pencere net pozitif |
| complexity_adds_value | edge | ❌ Luna −0.121 R, en iyi naive +1.436 R |

## Varyantlar ve ablation'lar

| Koşu | İşlem | Ort. R | %90 CI | Net $ |
|---|---|---|---|---|
| Luna gerçekçi = edge | 24 | −0.12 | [−0.49, +0.40] | −5.9 |
| 2× maliyet | 24 | −0.20 | [−0.57, +0.31] | −9.7 |
| Rejim filtresi kapalı (no_regime) | 0 | — | — | 0 |
| Ranker yerine rastgele seçim (random_ranker) | 1122 | −0.23 | [−0.29, −0.16] | **−223.8** (max DD %85) |
| Funding 0× / tahmini / 2× tahmini | 24 | −0.12 | — | ≈ −5.9 |

Ranker'ın değeri açıkça görülüyor: filtrelenmemiş sinyallerle sistem $265'in $224'ünü kaybediyor, ranker'la $6 kaybediyor. Ancak ranker çok az şeyi kabul ettiği için ortada ölçülebilir bir edge kalmıyor.

## Naive baseline'lar: aynı OOS dönemi, evren {BTC, ETH, SOL, XRP}

| Strateji | İşlem | Ort. R | %90 CI | Net $ |
|---|---|---|---|---|
| naive momentum (EMA 96/480) | 287 | **+1.44** | [+0.08, +3.53] | **+1192** |
| naive mean reversion | 925 | −0.21 | — | −201 |
| naive pairs ETH/BTC | 175 | −0.17 | [−0.27, −0.10] | — |

### Naive momentum'un sağlamlık kontrolü

| Senaryo | Ort. R | %90 CI |
|---|---|---|
| Tüm işlemler | +1.44 | [+0.08, +3.53] |
| En büyük 1 işlem çıkarılınca | +0.39 | [−0.04, +0.87] |
| En büyük 3 işlem çıkarılınca | +0.13 | [−0.19, +0.48] |

- Medyan işlem −1.05 R, kazanma oranı %22. Bu, klasik bir "trend takibi" profili: çoğu işlem küçük kayıpla kapanıyor, kârı birkaç büyük kazanç getiriyor.
- Net kârın ~%84'ü XRP'den geliyor ($1001 / $1192).
- Tek bir işlem tabloyu belirliyor: **XRP long, 2024-11-05 → 12-05, +300 R** (Kasım 2024'teki XRP rallisi, ~5 kat).

## Yorum

1. **Luna'nın karmaşık hali bu dönemde değer katmadı.** Ranker'ın sıkı kuralları (n ≥ 30, CI alt sınırı > 0) neredeyse her hücreyi reddetti. Kalan 24 işlem de negatif. Spec'teki "karmaşıklık değer katmıyorsa NO-GO" kuralı tam olarak bu durum için konmuştu.
2. **Mean reversion ve pairs OOS'ta da güvenilir şekilde negatif.** Bu sonuç in-sample bulguyla tutarlı.
3. **Naive momentum'un ortalaması tek bir işlemin eseri.** En büyük işlem çıkarılınca güven aralığı sıfırı kesiyor. Gerçek bir sinyal olabilir (kripto trendleri gerçekten büyük), ama kanıtı zayıf ve kuyruk riskine bağlı. Yine de bu, araştırmayı sürdürmek için **en umut verici yön**.
4. **Risk katmanı amacına uygun çalıştı.** Max DD %7. Rastgele seçim yapılsaydı DD %85 olurdu.

## Önerilen sonraki adımlar (araştırma)

- Momentum'u **daha yavaş zaman dilimlerinde** (1 saat, 4 saat, günlük) ve **daha geniş evrenle** test etmek.
- Tek işlem bağımlılığını kontrol etmek için **varlık bazında leave-one-out** testi yapmak.
- Ranker'ın kabul kuralını momentum gibi kalın kuyruklu stratejiler için gözden geçirmek. Örneğin ortalama yerine **trimmed** ya da **medyan bazlı** beklenen getiri kullanmak. Bu, spec'te bir değişiklik gerektirir; senin kararın.
- **Holdout şimdilik çalıştırılmamalı.** Holdout yalnızca bir kez kullanılabiliyor; onu umut verici bir aday strateji çıktığında kullanmak lazım.
