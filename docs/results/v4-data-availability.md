# V4 veri envanteri: funding / basis (2026-10-09)

**Durum:** Audit. Kod yazılmadı, getiri hesaplanmadı. Holdout'taki (2026-07-01 → 2026-10-09) hiçbir funding, prim veya getiri istatistiği okunmadı ya da raporlanmadı.

**Kaynaklar:**
- `var/` içindeki mevcut parquet dosyaları (bu PC'de 2026-10-08/09'da yeniden indirildi).
- Kraken'in public, salt-okunur endpoint'leri. Probe çıktısı `var/ops/v4_api_audit.json` dosyasında.
- Proxy kontrolü `var/ops/v4_funding_proxy_check.json` dosyasında; yalnızca development penceresini kapsar.

**Evren:** V2/V3 ile aynı 21 varlık: ADA, AVAX, BNB, BTC, DOGE, ENA, ETH, FET, HYPE, LINK, MUBARAK, NEAR, PEPE, PUMP, SOL, SUI, TAO, UNI, XLM, XRP, ZEC.

## 1. Özet

| Seri | Kaynak | Geçmiş başlangıcı | Çözünürlük | Elimizde mi? |
|---|---|---|---|---|
| **Gerçek funding** (relativeFundingRate, saatlik) | `derivatives/api/v4/historicalfundingrates` | **2025-10-06 08:00 UTC**, 21 varlıkta da | 1 saat | Evet: `var/funding/*.parquet`, 2026-10-08 22:00'e kadar |
| Funding (analytics) | `api/charts/v1/analytics/<sym>/funding` | 2026-02-11 | 1 dk – 1 sa | Hayır (indirilebilir; yukarıdakinin alt kümesi) |
| Perp trade barları | `charts/v1/trade` | 2023-01-01 (config `history_start`) veya listeleme tarihi | 15 dk | Evet: `var/bars/kraken_futures`, perp bot güncelliyor |
| **Mark fiyatı** | `charts/v1/mark` | 2022-03-21 veya listeleme | 15 dk / 1 sa | Hayır (indirilebilir) |
| **Index fiyatı** (Kraken referans endeksi) | `charts/v1/spot` | 2022-03-21 veya listeleme | 15 dk / 1 sa | Hayır (indirilebilir) |
| future-basis (analytics) | `analytics/future-basis` | 2022-03-21 | 1 sa | Hayır; 4 ondalık (1 bp) yuvarlanmış, kaba |
| **Open interest** | `analytics/open-interest` | **2023-03-06** veya listeleme | 1 dk'ya kadar (2023'te de) | Hayır (indirilebilir) |
| Perp best bid/ask, spread, likidite, slippage | `analytics/spreads`, `liquidity`, `orderbook`, `slippage` | 2023-05-30 | 1 dk – 1 sa | Hayır (indirilebilir) |
| Long/short oranı, top-trader OI | `analytics/long-short-*`, `top-traders` | 2023-05-30 | 1 sa | Hayır |
| CVD, trade-volume, liquidation-volume, aggressor | `analytics/*` | 2022-03-22 | 1 sa | Hayır |
| Kraken **spot** 15m OHLCVT | OHLCVT arşivi (2026Q2) + /Trades gap-fill | Varlığa göre 2013–2026 | 15 dk | Arşiv 2026-06-30'a kadar; 2026-07-01 → bugün **gap-fill sürüyor** |
| Kraken spot bid/ask / spread geçmişi | — | **Yok** | — | Yok; sadece forward toplanabilir |
| Kraken spot order book geçmişi | — | **Yok** | — | Yok; sadece forward toplanabilir |
| 2025-10-06 öncesi funding | — | **Yok** | — | **Uydurulmayacak** (bkz. §3) |

### Varlık bazında istisnalar

| Durum | Varlıklar | Ayrıntı |
|---|---|---|
| Geç listelenen | BNB, ENA, FET, HYPE, MUBARAK, PEPE, PUMP, SUI, TAO | Seriler listeleme tarihinde başlıyor. HYPE için 2025-01, MUBARAK 2025-05, PUMP 2025-07. |
| 2022'de mark/index/basis yok | DOGE, NEAR, XLM, ZEC | 2022 sonlarında başlıyor. 2023 öncesi zaten kullanılmıyor. |
| Tarihsel boşluk | FET | Çeyreklik örneklemede 2024-07-01 ve 2024-10-01 probları **boş** çıktı (mark, index, OI, spread). Muhtemel neden FET→ASI birleşmesi; neden doğrulanmadı. |
| Spot verisi seyrek | MUBARAK | 15m spot barlarının medyan aralığı 30 dk. |

Gerçek funding'de başlangıçta istisna yok: 21 varlığın hepsinin funding'i 2025-10-06 08:00'de başlıyor.

**Boşluk kontrolünün kapsamı:** Çeyreklik örnekleme 2022-04 → 2026-06 aralığında yapıldı. Bunun dışında ayrıntılı bir gap taraması yapılmadı. Bu tarama V4 Stage 0'ın işi.

## 2. Gerçek funding geçmişi: sınır API'den geliyor

- `historicalfundingrates` sayfalama parametresi almıyor. BTC için 2026-10-09'daki çağrı 8.825 satır döndürdü: 2025-10-06 08:00 → 2026-10-09 08:00.
- Bir gün önceki indirme de aynı tarihte başlıyordu. Pencere kaymıyor; başlangıç sabit. Önceki tarih bu API'den alınamıyor.
- Analytics `funding` serisi daha da kısa: 2026-02-11'de başlıyor.
- **Sonuç:** Bu PC'de veya Kraken'den 2025-10-06 öncesine ait gerçek funding elde edilemiyor. Eski PC'nin `var/`'ı da aynı API'den geldi, yani orada da daha eski veri yoktu.

**Holdout kesimiyle kullanılabilir gerçek funding (development):** 2025-10-06 → 2026-07-01, yani **268 gün (~8,8 ay)**. V2 düzeniyle (6 ay train / 2 ay test) en fazla **1** OOS penceresi çıkar. V3 spec'i funding'i bu yüzden ertelemişti; bu bulgu o kararı doğruluyor.

## 3. Funding proxy'si: gerçek verinin yerine geçmez

Kraken perp funding'i mark ile index arasındaki primden türetilir. Mark ve index geçmişi 2022'ye kadar gidiyor, bu yüzden bir **prim proxy'si** kurulabilir. Proxy'nin gerçek funding'e ne kadar benzediği **yalnızca development penceresinde** (2025-10-06 → 2026-07-01) ölçüldü. Ölçülen şey korelasyon; getiri değil.

Proxy tanımı: `(mark_close − index_close) / index_close`, 1 saatlik barlardan.

| Varlık | n (saat) | corr(funding_h, prim_h) | corr(funding_h, prim_{h−1}) | corr (24 sa ortalamalar) |
|---|---|---|---|---|
| BTC | 6.416 | 0,73 | 0,83 | **0,96** |
| ETH | 6.416 | 0,46 | 0,80 | **0,93** |
| SOL | 6.417 | 0,63 | 0,78 | **0,93** |
| DOGE | 6.417 | 0,44 | 0,66 | **0,81** |
| TAO | 6.417 | 0,34 | 0,51 | **0,89** |

**Yorum:**
- Günlük ölçekte proxy, gerçek funding'in yönünü ve seviyesini iyi izliyor: 24 saatlik ortalamalarda korelasyon 0,81–0,96.
- Saatlik ölçekte gürültülü, özellikle altcoin'lerde.
- Kural: proxy hiçbir yerde "funding" adını almaz ve gerçek funding'le aynı kolona yazılmaz.
  - Ayrı bir seri olarak saklanır: `premium_proxy`.
  - Kaynağı ve kalitesi raporlarda etiketlenir.
  - Gerçek funding'in olduğu dönemde proxy kullanılmaz.

**Kontaminasyon notu:** V3 ailesi A (perp–spot primi cross-section, NO-GO), 2023 → 2026-07 primini zaten görmüştü. Primin **fiyat-getirisi tahmincisi** olarak kullanılması bu development setinde kanıt değeri taşımaz. Primin **carry geliri tahmincisi** olarak kullanılması ise yeni bir hipotez. Yine de V4 bu örtüşmeyi açıkça beyan etmeli.

## 4. Basis (perp–spot) için iki ayrı tanım

| Tanım | Formül | Geçmiş | Ne ölçer |
|---|---|---|---|
| **Index basis** | `(perp_mark − index) / index` | 2022 → (indirilebilir) | Funding'in itici gücü; Kraken'in kendi endeksine göre |
| **Venue basis** (Kraken spot ↔ Kraken perp) | `(perp_px − spot_px) / spot_px` | 15m bar kapanışlarıyla 2023 → 2026-06-30; gap-fill bitince bugüne kadar | Gerçek cash-and-carry'de iki bacağın fiyat farkı |

Venue basis'in 15m barlarla ölçülmesi gürültülü:
- Trade fiyatları kullanılıyor, mid değil.
- İki bacakta işlem zamanları farklı.
- Spot tarafında bid/ask geçmişi yok, bu yüzden spot spread'i tarihsel olarak ölçülemiyor.
- Perp tarafında spread geçmişi 2023-05'ten beri var. Bu, V1/V2 failure analysis'teki "spread geçmişi yok" ifadesini perp için düzeltiyor; spot için ifade geçerli.

## 5. Holdout ve dataset durumu

- **Holdout [2026-07-01, `collector_start_ts`) (bkz. V4 spec R1):** `var/funding` ve perp barlarında bu döneme ait satırlar fiziksel olarak var; paper bot onlara ihtiyaç duyuyor. Bu audit onları okumadı. V4 yükleyicileri `assert_before_cutoff` (2026-07-01) korumasını kullanmalı.
- **Development:**
  - Gerçek funding ile: 2025-10-06 → 2026-07-01.
  - Proxy ve OI/spread ile: 2023 → 2026-07-01. Bu dönem V1–V3'te kullanıldı (fiyat sinyalleri için).
- **Forward (temiz):** `collector_start_ts` ve sonrası. Toplayıcı (`docs/superpowers/specs/luna-v4-forward-collector-design.md`) bugünden itibaren kaydetmeye başlarsa V4'ün **tek kirlenmemiş kanıt kaynağı** bu olur.

## 6. Eksik olan ve forward toplanması gerekenler

- Kraken spot bid/ask, spread ve top-of-book derinliği. Geçmişi hiç yok.
- Aynı anda alınmış perp ve spot mid'leri; venue basis'i ölçmek için tek doğru ölçüm.
- `fundingRatePrediction` / `relativeFundingRatePrediction`. Ticker'da anlık olarak var, geçmişi yok. "Bilinen bir sonraki funding" sinyali için gerekli.
- Settle edilmiş funding. API'de var, ancak aynı zamanda kendi kopyamızı saklamak, API'nin geçmişi bir gün kesme riskine karşı korur. Bugünkü başlangıç tarihi bu riskin gerçek olduğunu gösteriyor.
