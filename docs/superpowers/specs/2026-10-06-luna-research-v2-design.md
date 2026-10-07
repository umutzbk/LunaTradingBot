# Luna Research V2: Momentum Odaklı Araştırma · Tasarım Spec'i (Final Revizyon)

- **Tarih:** 2026-10-07 (rev 2: kullanıcı kararları sabitlendi)
- **Durum:** **ONAY BEKLİYOR.** Uygulamaya ancak (a) P2 final acceptance tamamlanıp (b) kullanıcı bu spec'i açıkça onayladıktan sonra başlanır.
- **Ön koşullar:** Luna Core + Crypto (alt proje #1) P0–P2 COMPLETE.
- **Girdiler:** `docs/results/walkforward.md`, `docs/results/baseline.md`, `docs/results/interim-findings-2026-10-06.md`, `docs/results/xrp-outlier-investigation.md`

---

## 0. Neden V2?

V1'in perp walk-forward sonucu (OOS 2024-09 → 2026-07) şöyle:
- **Tam sistem:** INSUFFICIENT DATA (24 işlem, −0.12R, 0/11 pencere pozitif).
- **Mean reversion ve pairs:** OOS'ta güvenilir şekilde negatif. Brüt bazda bile zararda.
- **Naive momentum:** +1.44R, ama tek bir XRP işlemine (+300R) bağlı. O işlem çıkarılınca +0.39R kalıyor ve CI sıfırı kesiyor. Brüt kârın ~%60'ını maliyet yiyor.

V2'nin araştırma sorusu:
> Momentum ailesinde, maliyet sonrası, tek işleme / tek varlığa / tek döneme bağlı olmayan, OOS'ta tekrar eden bir edge var mı? Varsa hangi timeframe'de, hangi evrende, hangi emir/maliyet modelinde ve hangi hesap tipinde (perp / spot)?

## 1. Sabit kararlar (kullanıcı, 2026-10-07)

| # | Karar |
|---|---|
| D1 | **Cross-sectional momentum: birincil rebalance haftalık.** Günlük yalnızca sensitivity. Tüm seçimler (parametre, evren, k, lookback) **yalnızca train verisiyle** yapılır. |
| D2 | **Perp market-neutral cross-sectional risk yönetimi portföy seviyesinde yapılır:** gross/net exposure limitleri, volatility targeting, varlık başına üst sınır (asset cap), concentration/correlation limitleri, portföy risk bütçesi. **Pozisyon başına stop bir alpha exit değildir;** yalnızca catastrophic/emergency safety stop kullanılabilir. |
| D3 | **Ranker kabul kuralı değişmez:** n ≥ 30, hiyerarşik shrinkage, bootstrap alt sınırı > 0. **Top-winner removal bir hard veto değildir.** Robustness çıktıları (§5) her konfigürasyon için **zorunludur.** |
| D4 | **Spot, V2'ye ayrı bir account/experiment olarak dahildir:** long-only, 1x; 1h/4h/1d; TSMOM, breakout, cross-sectional top-k long. **Perp ve spot sonuçları birleştirilmez; her biri ayrı GO/NO-GO alır.** |
| D5 | **Raporlama genişletilir** (§6): planned R, cap-adjusted realized R, equity contribution, gross/net R, fee/spread/slippage/funding atribüsyonu, cost/gross-edge oranı, turnover, holding period, maker fill rate, missed-fill opportunity cost, MFE/MAE, payoff ratio, winner concentration. |
| D6 | **Mean reversion ve pairs V2 kapsamı dışındadır.** V1 sonuçları nedeniyle dondurulur. Kod ve sonuçları referans olarak kalır, V2'de yeni deney yapılmaz. |
| D7 | **Holdout kesinlikle kullanılmaz.** V2 hiçbir koşulda `luna holdout` çalıştırmaz. `run_walkforward` kayıtlı holdout ile çakışan pencereleri zaten reddediyor; V2 ayrıca holdout dönemini tamamen veri dışında bırakır (§3.4). Holdout'un ileride kullanımı, V2 kapsamı dışında, ayrı bir kullanıcı kararıdır. |
| D8 | V1 strateji parametreleri ve V1 sınıfları değiştirilmez. V2 yeni sınıflar ve yeni grid'ler ekler. |

## 2. Kapsam

### 2.1 Strateji aileleri

| Aile | Perp | Spot | Tanım |
|---|---|---|---|
| **TSMOM** (time-series momentum) | long/short | long-only | Varlık başına trend işareti (EMA kesişimi veya lookback getirisi işareti), volatiliteye göre boyutlandırma; çıkış trend tersine döndüğünde veya time-stop'ta. V1 naive momentum bu ailenin referans üyesidir. |
| **Volatility breakout** | long/short | long-only | N-bar Donchian veya ATR/Keltner kanalı kırılımı; opsiyonel volatilite sıkışması filtresi; trailing ATR çıkışı. |
| **Cross-sectional momentum** | market-neutral long top-k / short bottom-k | long-only top-k | Evren içinde lookback getirisine göre sıralama, **haftalık** rebalance (D1), portföy seviyesi risk (D2). |

### 2.2 Timeframe'ler
- TSMOM ve breakout: **15m (V1 referansı), 1h, 4h, 1d.**
- Cross-sectional: 1d bar üzerinde sıralama; **haftalık rebalance birincil**, günlük yalnızca sensitivity.
- Spot: **1h, 4h, 1d** (15m spot maliyetiyle anlamsız, deney dışı).

### 2.3 Evrenler
- **BTC/ETH-only** ve **geniş evren** (V1 kuralı: ADV ≥ $10M ve ≥ 548 gün geçmiş, pencere başında seçilir) her zaman karşılaştırmalı raporlanır.
- Spot evreni = spot store'da verisi olan ve aynı kuralı geçen varlıklar.

### 2.4 Kapsam dışı
Mean reversion, pairs (D6); holdout (D7); canlı emir; ML ranker; LLM.

## 3. Doğrulama disiplini (V1'den devralınan + V2 ekleri)

### 3.1 Walk-forward
- V1 ile aynı: 6 ay train / 2 ay test, ardışık test pencereleri, pencereler arası state taşıma, sızıntı korumaları (eğitimde yalnızca `exit_ts < train_end` olan işlemler).
- 1h/4h/1d stratejiler 15m veriden **causal resample** ile beslenir. Karar yalnızca kapanmış üst-timeframe barının ardından verilir; fill bunu izleyen ilk 15m bar açılışında olur.
- Konfigürasyon seçimi (timeframe × aile × parametre × evren) **yalnızca train pencerelerinde** yapılır. Test pencereleri ve holdout seçimde hiç kullanılmaz.

### 3.2 Kabul ve ranker
- Ranker kabul kuralı V1'deki gibidir (D3). Robustness sonuçları ranker'ın kabul kararını **değiştirmez**; GO/NO-GO raporunda ayrı bölüm olarak yer alır ve okuyucuya sunulur.

### 3.3 Çoklu test dürüstlüğü
- Denenen her konfigürasyon sayılır ve raporlanır. Kalan **tek bir aday** seçimi train performansına dayanır; OOS sonuçları seçim sonrası raporlanır.

### 3.4 Holdout izolasyonu
- V2 veri yükleyicileri `data_end = holdout_start` ile çağrılır (perp: V1 kaydına göre son 3 ay). Spot için aynı takvim aralığı V2 dışında bırakılır.
- Test: V2 hiçbir koşuda holdout dönemine ait bir barı okumaz (assert + birim testi).

### 3.5 GO/NO-GO (hesap başına, ayrı)
V1 kriterleri aynen uygulanır:
- n ≥ 30
- Bootstrap alt sınırı > 0
- PF ≥ 1.2
- Max DD < %15, en kötü gün > −%3, günlük CVaR5 > −%2
- 2× maliyetle net ≥ 0
- Funding'in bilindiği dönemde net > 0 (yalnızca perp)
- Pencerelerin ≥ %60'ında net > 0
- En iyi naive baseline'ı geçmek

V2 eki: **perp ve spot ayrı karar alır, sonuçları toplanmaz** (D4).

## 4. Risk yönetimi

### 4.1 TSMOM ve breakout (perp ve spot)
- V1 risk motoru (R1–R19) aynen geçerlidir. İşlem başı risk ve stop mesafesine göre boyutlandırma V1'deki gibidir.
- Spot'ta ek kısıtlar: long-only, gross ≤ 1x, R13.

### 4.2 Cross-sectional (perp market-neutral), D2
Portföy seviyesi risk motoru eklenir. V1 risk motoru emir seviyesinde (R1–R19) aynen kalır, bu motor onun üstüne konur.

| Kontrol | Tanım | Varsayılan (train'de doğrulanacak, OOS'ta değiştirilmez) |
|---|---|---|
| Gross exposure | Σ\|notional\| / equity | ≤ 1.5x |
| Net exposure | \|Σ notional\| / equity | ≤ 0.10x (market-neutral) |
| Volatility targeting | Hedef portföy volatilitesi; ölçek = hedef / tahmini vol (causal EWMA kovaryans) | yıllık %15, ölçek tavanı 1.0 |
| Asset cap | Tek varlık \|notional\| / equity | ≤ %30 |
| Concentration / correlation | Aynı yönde, 30 günlük korelasyonu > 0.8 olan varlıkların toplam ağırlığı | ≤ %50 |
| Portföy risk bütçesi | Tahmini 1-günlük %95 VaR / equity | ≤ %2 |
| Emergency safety stop | Varlık başına felaket kaybı (equity'nin %3'ü) veya portföy seviyesinde günlük kayıp; **alpha exit değil** | V1 R3/R4 + acil stop |

Rebalance haftalık yapılır (D1); hedef ağırlıklar bu kısıtlar altında projekte edilir. Çıkışlar yalnızca rebalance ile olur, emergency stop hariç. Cross-sectional için "işlem" tanımı **rebalance dönemi × varlık bacağı**dır. R için planned risk, portföy risk bütçesinin bacağa düşen payıdır (§6'da tanımlı).

### 4.3 Spot cross-sectional (top-k long)
Long-only, gross ≤ 1x, eşit veya vol-ağırlıklı top-k, asset cap %40, haftalık rebalance.

## 5. Zorunlu robustness çıktıları (D3)

Her konfigürasyon ve her hesap için, OOS sonuçları üzerinde hesaplanır:

| Çıktı | Tanım |
|---|---|
| Full | Tüm OOS işlemleri: ortalama R, bootstrap CI, PF, net $ |
| ex-top1 / ex-top3 / ex-top1% | En büyük 1, 3 ve en büyük %1 kazanan çıkarıldıktan sonra aynı metrikler |
| Trimmed mean R | Her iki uçtan %5 kırpılmış ortalama |
| Median R | Medyan işlem R'si |
| P&L concentration | En büyük %1 / %5 / %10 işlemin net P&L içindeki payı; varlık bazında pay (HHI) |
| Asset leave-one-out | Her varlık sırayla evrenden çıkarılıp walk-forward yeniden koşar → net R ve net $ |
| Time-period leave-one-out | Her test penceresi sırayla çıkarılır → kalan OOS metrikleri |
| Best-month / best-quarter removal | En iyi takvim ayı ve en iyi çeyrek çıkarıldıktan sonra metrikler |

Bu çıktılar ranker kabulünü değiştirmez (D3), ama GO/NO-GO raporunda zorunlu bölüm olarak yer alır. Yorum kuralı: bir sonuç yalnızca full'da pozitif, ex-top1 veya leave-one-out'ta negatifse rapor bunu açıkça **"kırılgan"** diye etiketler.

## 6. Raporlama (D5)

| Metrik | Tanım |
|---|---|
| Planned R | Girişte planlanan risk tutarı (stop mesafesi × notional + gidiş-dönüş maliyet). Cross-sectional'da risk bütçesi payı. |
| Cap-adjusted realized R | Net P&L / *cap öncesi planlanan* risk. V1'deki "cap küçültünce R şişer" sorununu görünür kılar. Engine `r_multiple` ile birlikte raporlanır. |
| Gross / net R | Maliyet öncesi ve sonrası R |
| Equity contribution | Konfigürasyon, varlık ve işlem bazında equity değişimine katkı ($ ve %) |
| Maliyet atribüsyonu | Fee / spread / slippage / funding (bilinen ve bilinmeyen ayrı) |
| Cost / gross-edge oranı | Toplam maliyet / brüt P&L (brüt ≤ 0 ise "n/a – brüt edge yok") |
| Turnover | Yıllıklaştırılmış notional / ortalama equity |
| Holding period | Dağılım: medyan, p10, p90 |
| Maker fill rate | Post-only emirlerin dolma oranı (perp maker modeli ve spot) |
| Missed-fill opportunity cost | Dolmayan maker girişlerinin counterfactual net R'si (V1 labeling altyapısı) |
| MFE / MAE | İşlem içi maksimum lehte ve aleyhte hareket (R cinsinden) |
| Payoff ratio | Ortalama kazanç / ortalama kayıp |
| Winner concentration | Kârın en büyük k işlemdeki payı; Gini |

R tanımları raporda açıkça yazılır. Research `r_net` = net getiri / stop mesafesi; engine `r_multiple` = net P&L / risk tutarı; ve yeni cap-adjusted R.

## 7. Maliyet ve emir modeli
- **Taker:** V1 modeli.
- **Maker (perp ve spot):** post-only + "trade-through" fill kuralı (V1 spot modeli perp'e de uygulanır). Dolmayan emir kaçan işlem demektir; bu fırsat maliyeti olarak raporlanır (§6).
- **Sensitivity (zorunlu):**
  - 2× maliyet
  - Maker-only ve taker-only
  - Perp için funding 0× / tahmini / 2×

## 8. Uygulama planı taslağı
Onaydan sonra writing-plans ile detaylandırılır. Her task V1'deki gibi implementer + reviewer döngüsünden geçer.

| # | Task | İçerik |
|---|---|---|
| V2-1 | Holdout izolasyonu + causal resampler | `data_end = holdout_start` zorlaması ve testi; 15m → 1h/4h/1d causal resample, karar/fill zamanlaması, nedensellik testleri |
| V2-2 | Zaman-ölçekli parametreler | Uzunlukların zaman cinsinden tanımı; timeframe başına grid |
| V2-3 | TSMOM ailesi (perp + spot) | Tradelist ↔ engine eşdeğerlik testi (V1 kalıbı) |
| V2-4 | Volatility breakout (perp + spot) | Eşdeğerlik testi |
| V2-5 | Portföy risk motoru | Gross/net, vol targeting, asset cap, concentration/correlation, VaR bütçesi, emergency stop; birim testleri |
| V2-6 | Cross-sectional momentum | Haftalık rebalance (günlük sensitivity), perp market-neutral + spot top-k long; rebalance tabanlı işlem tanımı |
| V2-7 | Perp maker emir modeli | SimBroker + tradelist; maker fill rate ve missed-fill counterfactual |
| V2-8 | Raporlama genişlemesi | §6 metrikleri; R tanımları; maliyet atribüsyonu |
| V2-9 | Robustness motoru | §5 çıktılarının tamamı (leave-one-out koşuları dahil) |
| V2-10 | V2 walk-forward orkestrasyonu | Konfigürasyon grid'i, train-only seçim, deneme sayımı; perp ve spot ayrı |
| V2-11 | V2 GO/NO-GO raporu | Hesap başına ayrı karar, robustness bölümü, "kırılgan" etiketleri, aday ya da "aday yok"; holdout yok |

## 9. Çıktılar
- Hesap başına (perp, spot) ayrı V2 raporu: HTML + `summary.json`
- `docs/results/research-v2.md`: deneme sayıları, aday(lar) ya da "aday yok", robustness özeti, maliyet atribüsyonu
- Holdout kullanımı V2'nin çıktısı **değildir**. Bir aday çıkarsa, holdout'un kullanılıp kullanılmayacağı ayrı bir kullanıcı kararıdır (D7).

## 10. Riskler
- **Çoklu test:** timeframe × aile × evren × parametre kombinasyonları denenen konfigürasyon sayısını büyütür. Sayım ve train-only seçim zorunlu.
- **Az işlem:** 1d timeframe ve haftalık rebalance OOS'ta az işlem üretebilir. n ≥ 30 sağlanamazsa sonuç "INSUFFICIENT DATA" olur, bu da dürüst bir sonuçtur.
- **Funding geçmişi kısa:** V1'deki gibi bilinmeyen dönem uydurulmaz.
- **Spot verisi:** P2 kapanışında spot backfill'in tamamlanmış olması gerekir.
- **Hesaplama yükü:** leave-one-out koşuları walk-forward sayısını katlar. İşlem listesi cache'i ve üst timeframe'lerdeki az bar sayısı bunu dengeler; gerekirse leave-one-out yalnızca finalist konfigürasyonlara uygulanır (bu, train-only seçim sonrası yapılır).
