# V5 Stage 0 audit (2026-10-11): **BLOCKED — 1M SPREAD UNAVAILABLE**

> Outcome-free. Bu belgede getiri, P&L, sinyal sıralaması veya spread büyüklüğünün strateji etkisi yoktur.
> Holdout (≥ 2026-07-01) okunmadı. V4 dosyaları değişmedi. Paper config değişmedi. `live_enabled=false`.

## 1. API semantiği (S0.1)

**Endpoint:** `https://futures.kraken.com/api/charts/v1/analytics/<PF_sym>/spreads?since&to&interval`. Resmi dokümantasyon bulunamadı; semantik yanıt davranışından belirlendi. Probe yalnızca 2025-10-20 (cutoff öncesi) penceresini okudu. Ham yanıtlar ve scriptler: `var/ops/v5_stage0/` (`btc_60.json`, `btc_300.json`, `orderbook.json`, `liquidity.json`, `p1–p8.py`).

| Soru | Bulgu |
|---|---|
| İzinli `interval` | 60, 300, 900 kabul ediliyor. 1, 10, 30 ve 120 "Invalid arguments" döndürüyor. |
| Yanıt şekli | `result.timestamp` (epoch s, aralık grid'inde), `data.bid.best_price` / `data.ask.best_price` (string). `more` alanı sayfalama için. |
| **60 s serisi gerçek 1 dakikalık gözlem mi?** | **Hayır.** Değerler 5 dakika boyunca birebir aynı kalıyor ve 5 dakikada bir değişiyor. 20 varlık × 268 gün V4 penceresinde 57.765 değişim aralığının 57.725'i 5'in katı. Kalan 40'ı gün sınırı ya da faz kaymasıyla açıklanıyor. |
| Değer ortalama mı, nokta mı? | Nokta (snapshot). Bid ve ask tick değerli tam sayılar, 5 dakika boyunca birebir aynı. Bir ortalama tick dışı değerler üretirdi. |
| Snapshot saati | Ortak bir 5 dakikalık saat; fazı günden güne kayıyor. İlk değişimin dakika mod 5 değeri 268 günde {3: 115, 4: 60, 2: 41, 0: 31, 1: 21}, 20 varlıkta aynı. 1 dakikalık trade barlarıyla en iyi eşleşme, etiket T'deki değerin [T, T+1m] civarında gözlendiğini gösteriyor (BTC'de medyan sapma 16 USD). Dakika düzeyinde kesin etiket semantiği **güvenilir biçimde doğrulanamadı**. |
| 300 s ile 60 s arasındaki ilişki | 300 s'deki T değeri, 60 s serisinin T..T+4 değerleriyle aynı. |
| Diğer analytics endpoint'leri | `orderbook` (bestPrice, liquidity, slippage) ve `liquidity` aynı 5 dakikalık kadansa sahip. |
| Rate limit / tekrar | Rate-limit başlığı yok. Probe'da tekrar eden zaman damgası görülmedi. |

**Sonuç:** Kraken analytics geçmişi gerçek 1 dakikalık spread vermiyor; 60 s serisi, 5 dakikalık snapshot'ların ileri taşınmış hali. Protokol §2.1'e ve kullanıcı kararına göre Stage 0 durur. 5 dakikaya sessizce geçilmedi ve backfill yapılmadı.

**Reddedilen alternatifler:**

| Alternatif | Neden reddedildi |
|---|---|
| 60 s serisini 1 dakika diye kullanmak (V4'ün de fiilen yaptığı) | Çözünürlüğü yanlış etiketler; kapsam eşikleri (30/60, 8/15) anlamsızlaşır. |
| Farklı interval, sayfalama ya da range biçimi | 1, 10 ve 30 s reddediliyor. 60 s seçeneği istemciden bağımsız olarak 5 dakikalık değerleri tekrarlıyor (ham yanıtta da öyle). |
| `orderbook` / `liquidity` endpoint'leri | Aynı 5 dakikalık kadans. |
| Trade verisinden spread türetmek | Fabrikasyon olur; kotasyon değildir. |
| Interpolasyon | Yasak (gözlem taklidi). |
| Collector snapshot'ları | Yalnızca 2026-10-09 sonrası (forward); tarihsel pencere için yok. |
| Sabit 10 bps'i primary yapmak | Kullanıcı kararıyla yasak; yalnızca fallback ve sensitivity. |

**V4 downloader'ının yalnızca 23:00–00:00 çekmesi:** Bir API kısıtı değil, V4 tasarım tercihi (yalnızca 00:00 kararları). Aynı 5 dakikalık kadans V4'ün sakladığı pencerelerde de var. Bu, V4'ün "1 dakikalık 30/60 örnek" kuralının fiilen ~12 snapshot'ın tekrarlı ortalaması olduğu anlamına gelir. V4 frozen'dır; bu yalnızca bir açıklamadır, V4'e düzeltme yapılmaz.

**Kullanıcı kararı gereken seçenekler (önerim sırayla):**
1. **Gerçek 5 dakikalık snapshot'lar ve açık etiketle protokol değişikliği.** `interval=300`, tekrar yok. Karar penceresi [t−1h, t) içinde ≥ 6/12 snapshot. Fill penceresi [t, t+15m) içinde ≥ 2/3 snapshot. Point-in-time kuralı snapshot zamanına göre ve muhafazakâr: etiket T, en geç T+5m'de bilinir sayılır. Freeze'den önce yazılır ve red-team'e girer.
2. Sabit 10 bps'i primary yapmak. Kullanıcı kararıyla bugün yasak; bu yüzden önerilmiyor.
3. Outcome run'ı ertelemek ve spread'i yalnızca forward collector verisinden almak (§11 forward teyidi).

## 2. Spread'den bağımsız audit kalemleri (frozen parity kopyası üzerinde)

| Kalem | Sonuç |
|---|---|
| Holdout | Kopyada en son ts 2026-06-30T23:59:00Z (V4 spread örneği). Barların son open_ts'i 23:45. **PASS.** Guard testleri: freeze, verify, open ve loader seviyelerinde. |
| 15m bar kapsamı [2025-10-06, 2026-07-01) | 20 varlığın hepsinde gerçek trade barı ve mark barı oranı 1,0. |
| Mark ve trade kapanışı arasındaki göreli fark (514.560 eşleşen bar) | Medyan 2,8 bps, p95 27,6 bps, p99 65 bps, max %33,9 (FET). En uç değerler: SUI %24,9, LINK %23,8, ADA %16,5, PEPE %15,6. Uç değerler tek barlık trade "wick"leri gibi görünüyor. Primary mid'in mark olması bunlara karşı koruyor; ama outcome raporunda bunlar sayılmalı. |
| B2 diagnostic: primary mid (t−15m mark kapanışı) ile parity mid (t+15m trade kapanışı), 680 Pazartesi quote'u | Medyan %0,32, p95 %2,2, max %6,9. Fark, 15 dakikalık fiyat hareketiyle mark–trade farkını birlikte içerir. |
| Provenance ve sha | `history_v4` kaynaklarının 80 dosyası provenance sha'larıyla eşleşiyor (0 uyumsuzluk). Bar dosyalarının provenance'ı yok; frozen manifest kaynak yolunu ve düşürülen satır sayısını kaydediyor. |
| Stage 1 manifest karşılaştırması (§2.2) | V4 Stage 1 `manifest.json` girdi sha'sı **içermiyor**. Doğrudan sha karşılaştırması mümkün değil. Yerine geçen kanıt: frozen kopya üzerinde V4 referans koşusu yayımlanmış sonucu birebir üretiyor (`v5-parity` raporu). Protokol §2.2 metni buna göre güncellenmeli. |

## 3. Frozen kopya

- **Yol:** `var/history_v5/frozen_parity/` (Stage 0 BLOCKED olduğu için, yeni spread'leri içerecek nihai `frozen/` kopyasından ayrı tutuldu).
- **İçerik:** 121 dosya: perp ve spot barları, V4'ün funding, mark, index ve spread dosyaları, enstrüman dosyası.
- **Manifest sha256:** `88633d835f6f3d93d982702dc319e3d899f619c17a76a06080a0c892768abc94`
- **Enstrüman sha'sı:** `d14a82bb…4128`, V4 ile aynı.
- Dosyalar salt-okunur. Yükleyici manifest'i ve kesimi doğrulamadan açmıyor.
