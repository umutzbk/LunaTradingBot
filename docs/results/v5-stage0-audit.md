# V5 Stage 0 audit (2026-10-11): **PASS (protocol rev 3, 5-minute historical snapshots)**

> Outcome-free. Bu belgede getiri, P&L, sinyal sıralaması veya spread büyüklüğünün strateji etkisi yoktur.
> Holdout (≥ 2026-07-01) okunmadı. V4 dosyaları değişmedi. Paper config değişmedi. `live_enabled=false`.
> İlk deneme (aşağıda §1–§4) 1 dakikalık şart yüzünden BLOCKED idi; kullanıcı kararıyla protokol rev 3 gerçek 5 dakikalık snapshot'lara geçti (protokol §15).

## 0. Rev 3 final audit (20 varlık, [2025-10-06, 2026-07-01))

**Kaynak:** Kraken analytics `spreads`, `interval=300`, "5 dakikalık tarihsel snapshot verisi". Çıktı `var/history_v5/spreads/` (+ gün başına checkpoint'ler). Script'ler `scripts/v5_stage0/{backfill_spreads,audit_5m,freeze_final}.py`. Ham audit: `var/ops/v5_stage0/audit_5m.json` (sha256 `9136a1f8…de0c6`).

**Etiket semantiği (yeni probe):** `interval=300`'ün T değeri, 60 s serisinin T+faz … T+4 değerlerine eşit (faz 1, 2, 3, 4 günlerinde birebir). Snapshot [T, T+5m) içinde alınır; bu yüzden primary kural **T + 5m'de bilinir**. S-PIT10 (T + 10m) altyapısı hazır: `Spread5m(known_after=10min)`, `load_spread5m(..., known_after=…)`, testli.

| Kontrol | Sonuç |
|---|---|
| Kapsam | 20/20 varlık, her biri 77.184/77.184 etiket (268 gün × 288). Gün, ay ve saat kırılımında mevcudiyet 1,0. |
| Tekrar / monotonluk / grid | Tekrar 0, monoton, 5 dakikalık grid dışı 0, pencere dışı 0 |
| Gözlenen kadans, provenance | 20/20 kadans 300 s, etiket "5-minute historical snapshots"; dosya sha'ları provenance ile eşleşiyor; checkpoint sha'ları assemble sırasında doğrulandı |
| Geçersiz değerler | Toplam 580 snapshot > 500 bps, 15 NaN (PUMP 10, UNI 5), sonsuz 0, ≤ 0 yok. En çok: FET 185, PUMP 131, XLM 59, HYPE 55, ZEC 54, UNI 53. Kullanılmaz ve sayılır. |
| Etiket boşlukları | ≥ 1 saatlik etiket boşluğu yok (en uzun boşluk 0) |
| Geçersiz değer serileri (≥ 1 saat) | Çoğu outcome penceresinden önce: 2025-11-01 17:10–21:10 (FET, HYPE, PUMP, UNI, XLM, ZEC; PUMP'ta 66 etiket ≈ 5,5 saat), 2025-10-10/11 (PUMP, UNI). Outcome penceresinde tek seri: FET 2026-03-05 22:15'ten 26 etiket (≈ 2 saat 10 dk). |
| Tekrarlayan ardışık değer | Oran 0,28 % – 3,8 % (en yüksek PUMP); en uzun seri 28 etiket (PUMP 66, BNB 42). Kural değişmez; raporlanır. |
| Karar fallback'i (< 6/12) | Bütün 15m noktaları: %0,029. **Outcome penceresi: %0,0031**; yalnızca FET (%0,063). 00:00'da outcome: %0,0215 (yalnızca FET, %0,43). Ay: yalnızca 2026-03. Saat: yalnızca 22, 23, 00. |
| Fill fallback'i (< 2/3) | Bütün noktalar: %0,036. **Outcome penceresi: %0,0092**; FET %0,157, PUMP %0,027. Ay: 2025-11, 2026-02 … 2026-05. Saat: en yüksek 22:00 (%0,086). |
| %90 kapsam bayrağı (varlık ve varlık × saat hücresi, outcome) | Bayrak yok. En düşük varlık: karar 99,94 %, fill 99,84 %. En düşük hücre: karar 99,25 %, fill 98,50 %. |
| V4 23:00–00:00 tutarlılığı (5 dakikalık değer parity'si) | **PASS: 64.320/64.320** etiket (20 × 3.216), T değeri = V4 T+4m satırı (1e-12; PUMP'ın NaN'ları iki tarafta da NaN). Kadans: 321.600 V4 dakikasının 321.500'ü gün başına tek bir fazla açıklanıyor; kalan 100 dakika her varlıkta yalnızca 2025-10-10'daki gün içi faz kaymasında (§2.1'e göre FAIL değil, listelenir). |
| Holdout | Max ts 2026-06-30T23:55 (spread), kopyada 23:59 (V4 60 s). Kesim sonrası düşürülen satır 0. **PASS.** |

**Frozen kopya (nihai):** `var/history_v5/frozen/`, 161 dosya (V4 düzeni + `history_v5/spreads/<asset>.parquet` ve `.provenance.json`), manifest sha256 **`eecdc6a21626ccd5e2677cce2503f304a5730845de552554281a6ea54cc53f14`**.
- 121 ortak dosya `frozen_parity` ile bayt bayt aynı; spread dosyaları kaynakla bayt bayt aynı (provenance sha'sı geçerli kalır).
- İkinci bir freeze 161 dosyanın sha'sını birebir yeniden üretti.
- `open_frozen` + `load_spread5m` 20 varlığı provenance kontrolüyle yükledi; S-PIT10 yükleyicisi de çalışıyor.

**Stage 0 çıkış koşulu (protokol §2.1):** Holdout PASS, V4 tutarlılığı PASS, gerçek 5 dakikalık snapshot'lar alındı (kadans 300 s), audit sunuldu → **PASS.**

**Guard'ların durumu:**
- Primary T + 5m, karar 6/12 ve fill 2/3: `luna/carry/history.py` `Spread5m`, testli.
- S-PIT10: altyapı hazır ve testli; satırın kendisi outcome replay'inde koşar.
- PIT-SENSITIVE / FALLBACK-AFFECTED / S-FB-CONS birleştirmesi: `luna/carry/verdict.py`, 8 kombinasyonluk matris testi.
- **Açık zorunlu kapı:** fallback sayaçlarının arm, varlık ve saat bazında dolan notional'a atanması ve FALLBACK-AFFECTED tetikleyicisinin sentetik regression testi (protokol §12). Outcome replay henüz yazılmadığı için bu test yok. Replay yazılırken eklenmeli ve outcome koşusundan önce yeşil olmalı. S-FB-CONS'un lookback p90 hesabı da replay ile birlikte yazılacak.

---

# Önceki deneme (2026-10-11 gece): BLOCKED — 1M SPREAD UNAVAILABLE

> Outcome-free. Bu belgede getiri, P&L, sinyal sıralaması veya spread büyüklüğünün strateji etkisi yoktur.
> Holdout (≥ 2026-07-01) okunmadı. V4 dosyaları değişmedi. Paper config değişmedi. `live_enabled=false`.

## 1. API semantiği (S0.1)

**Endpoint:** `https://futures.kraken.com/api/charts/v1/analytics/<PF_sym>/spreads?since&to&interval`. Resmi dokümantasyon bulunamadı; semantik yanıt davranışından belirlendi. Probe yalnızca 2025-10-20 (cutoff öncesi) penceresini okudu. Ham yanıtlar ve scriptler: `var/ops/v5_stage0/` (`btc_60.json`, `btc_300.json`, `orderbook.json`, `liquidity.json`, `p1–p8.py`).

| Soru | Bulgu |
|---|---|
| İzinli `interval` | 60, 300, 900 kabul ediliyor. 1, 10, 30 ve 120 "Invalid arguments" döndürüyor. |
| Yanıt şekli | `result.timestamp` (epoch s, aralık grid'inde), `data.bid.best_price` / `data.ask.best_price` (string). `more` alanı sayfalama için. |
| **60 s serisi gerçek 1 dakikalık gözlem mi?** | **Protokol penceresinde [2025-10-06, 2026-07-01) hayır.** Değerler 5 dakika boyunca aynı kalıp 5 dakikada bir değişiyor. 20 varlık × 268 gün V4 penceresinde değişimler arası boşluklar şöyle dağılıyor: 5 dk ×57.296, 10 dk ×404 (değer bir snapshot boyunca değişmemiş), 15 dk ×23, 6 dk ×39 ve 35 dk ×1. Bunlara 31 NaN satırı eşlik ediyor. 6 dakikalık boşlukların hepsi 2025-10-10'daki faz kaymalarından geliyor. 35 dakikalık boşluk ve NaN'lar PUMP'ta, 2025-10-10 23:03–23:33 arasında. **Gerçek 1 dakikalık veri yalnızca yaklaşık Şubat 2025'ten öncesinde var:** PF_XBTUSD için 2025-01-15'te 120 dakikada 116 değişim görüldü; 2025-02-15'te yalnızca 24 değişim var (5 dakikalık kadans). Bu bulgu bağımsız reviewer'dan geldi ve yeniden doğrulandı. O dönemde gerçek funding yok (gerçek funding 2025-10-06'da başlıyor), bu yüzden outcome penceresi için kullanılamaz. |
| Değer ortalama mı, nokta mı? | Nokta (snapshot). Bid ve ask tick değerli tam sayılar, 5 dakika boyunca birebir aynı. Bir ortalama tick dışı değerler üretirdi. |
| Snapshot saati | Ortak bir 5 dakikalık saat; fazı günden güne ve zaman zaman gün içinde kayıyor (örneğin 2025-10-10'da iki kez). İlk değişimin dakika mod 5 değeri 268 günde {3: 115, 4: 60, 2: 41, 0: 31, 1: 21}, 20 varlıkta aynı. 1 dakikalık trade barlarıyla en iyi eşleşme, etiket T'deki değerin [T, T+1m] civarında gözlendiğini gösteriyor (BTC'de medyan sapma 16 USD). Dakika düzeyinde kesin etiket semantiği **güvenilir biçimde doğrulanamadı**. |
| 300 s ile 60 s arasındaki ilişki | 300 s'deki T değeri, 60 s serisinin T..T+4 değerleriyle aynı. |
| Diğer analytics endpoint'leri | `orderbook` (bestPrice, liquidity, slippage) ve `liquidity` aynı 5 dakikalık kadansa sahip. |
| Rate limit / tekrar | Rate-limit başlığı yok. Probe'da tekrar eden zaman damgası görülmedi. |

**Sonuç:** Kraken analytics geçmişi, protokol penceresi için gerçek 1 dakikalık spread vermiyor (yaklaşık Şubat 2025'ten beri); 60 s serisi, 5 dakikalık snapshot'ların ileri taşınmış hali. Protokol §2.1'e ve kullanıcı kararına göre Stage 0 durur. 5 dakikaya sessizce geçilmedi ve backfill yapılmadı.

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

## 4. Düzeltmeler ve ikinci görüş (2026-10-11)

- Bağımsız ikinci reviewer'ın hükmü **PARTIAL**: protokol penceresinde 5 dakikalık kadans **doğrulandı**, ancak "endpoint hiç gerçek 1 dakika vermiyor" genellemesi yanlış (Ocak 2025 ve öncesi gerçek 1 dakika). Metin buna göre düzeltildi.
- İstek hizalaması (`since` +150 s), sembol (`PF_BTCUSD` boş döndü) ve `interval=180` (reddedildi) denemeleri ileri taşımanın istekten değil, saklanan veriden geldiğini doğruladı.
- `var/ops/v5_stage0/v4_cadence.json` içindeki `nonregular_days: 268` değeri `p8.py`'deki bir birim hatasından kaynaklanıyor (ms indeksi ns sanılmış). Veri aslında düzenli. Kadans, `luna.carry.spread_backfill.observed_cadence_s` ile ayrıca ölçüldü: BTC, TAO ve PUMP V4 pencerelerinde 300 s.
- "Nokta snapshot" bulgusu güçlü bir çıkarımdır, kanıt değildir. Dakika düzeyinde etiket zamanlaması belirsizdir. Önerilen muhafazakâr kural (etiket T, T+5m'de bilinir) bu yüzden önerildi.
- Frozen parity kopyası ana repodadır: `LunaTradingBot/var/history_v5/frozen_parity/`.
