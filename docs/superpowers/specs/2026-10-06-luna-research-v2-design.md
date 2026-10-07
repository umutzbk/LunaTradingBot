# Luna Research V2: Momentum Odaklı Araştırma · Tasarım Spec'i (Final Revizyon)

- **Tarih:** 2026-10-07 (rev 3: final cleanup, pre-commit risk değerleri, sabit holdout kesimi, CVaR/ES, tutarlılık gözden geçirmesi)
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
- **Sabit kesim tarihi:** `V2_DATA_END = 2026-07-01T00:00:00Z`. Bu, V1 walk-forward'ın son OOS test penceresinin bitişidir; V1 holdout'u ve V1'in holdout öncesi 5 günlük boşluğu bu tarihten sonradır.
- V2'nin tüm veri yükleyicileri (perp ve spot, tüm timeframe'ler) bu tarihten itibaren hiçbir barı okumaz. Tarih sabittir; veri store'una yeni barlar eklense de kaymaz.
- Test: V2 yükleyicisi `V2_DATA_END` sonrası bar döndürürse hata verir (assert + birim testi). `luna holdout` V2 kodundan çağrılmaz.

### 3.5 GO/NO-GO (hesap başına, ayrı)
V1 kriterleri aynen uygulanır ve **robustness çıktıları (§5) GO/NO-GO kriteri değildir** (D3):
1. n ≥ 30 (altında sonuç "INSUFFICIENT DATA")
2. Bootstrap %90 alt sınırı > 0
3. PF ≥ 1.2
4. Max DD < %15, en kötü gün > −%3, günlük CVaR5 > −%2 (gerçekçi varyant)
5. 2× maliyetle net expectancy ≥ 0
6. Funding'in bilindiği dönemde net > 0 (yalnızca perp)
7. Walk-forward pencerelerinin ≥ %60'ında net > 0
8. Aynı hesap ve aynı OOS aralığında **en iyi naive baseline'ı** OOS net expectancy'de geçmek. Perp için V1 naive momentum; spot için V1 naive momentum'un long-only versiyonu.

V2 eki: **perp ve spot ayrı karar alır, sonuçları toplanmaz** (D4).

## 4. Risk yönetimi

### 4.1 TSMOM ve breakout (perp ve spot)
- V1 risk motoru (R1–R19) aynen geçerlidir. İşlem başı risk ve stop mesafesine göre boyutlandırma V1'deki gibidir.
- Spot'ta ek kısıtlar: long-only, gross ≤ 1x, R13.

### 4.2 Cross-sectional (perp market-neutral), D2
Portföy seviyesi risk motoru eklenir. V1 risk motoru emir seviyesinde (R1–R19) aynen kalır, bu motor onun üstüne konur.

**Pre-commit:** Aşağıdaki değerler bu spec'in onayıyla **sabitlenir**. Hiçbiri OOS sonuçlarına göre ayarlanmaz. Seçim yapılan iki parametre için önceden tanımlı küçük bir grid vardır; seçim **yalnızca train pencerelerinde**, strateji parametreleriyle aynı train-only süreçte yapılır. Grid kombinasyonları §3.3'teki deneme sayımına dahildir. Diğer tüm değerler sabittir ve grid'e girmez.

| Kontrol | Tanım | Değer |
|---|---|---|
| Gross exposure | Σ\|notional\| / equity | **grid: {1.0x, 1.5x}** (train-only seçim) |
| Volatility targeting | Hedef yıllık portföy volatilitesi; ölçek = hedef / tahmini vol (causal EWMA kovaryans, span 30 gün), ölçek tavanı 1.0 | **grid: {%10, %15}** (train-only seçim) |
| Net exposure | \|Σ notional\| / equity | sabit ≤ 0.10x |
| Asset cap | Tek varlık \|notional\| / equity | sabit ≤ %30 |
| Concentration / correlation | Aynı yönde, 30 günlük korelasyonu > 0.8 olan varlıkların toplam ağırlığı | sabit ≤ %50 |
| Portföy risk bütçesi | Tahmini 1-günlük %95 VaR / equity (causal kovaryans) | sabit ≤ %1.5 |
| Per-asset catastrophic safety stop | Tek bir varlık bacağının mark-to-market kaybı equity'nin **%1.5**'ine ulaşırsa o bacak IOC ile kapatılır; **alpha exit değildir**. Bir sonraki haftalık rebalance'a kadar o varlığa yeniden giriş yapılmaz. | sabit %1.5 |
| Portföy günlük kayıp | V1 R3 (%2): o gün yeni pozisyon/rebalance yok. V1 R4 (%15 drawdown): kalıcı kill + flatten. | V1 ile aynı |

**Tutarlılık:**
- Per-asset stop (%1.5) tek başına V1 günlük kayıp limitinin (%2) altında kalır.
- VaR bütçesi (%1.5) de günlük kayıp limitinin altındadır, yani normal koşulda tek günlük beklenen kuyruk kaybı limit içinde kalır.
- Birden fazla bacak aynı gün stop olursa R3 o günün geri kalanında yeni pozisyonu engeller (V1 davranışı).
- TSMOM ve breakout V1'in pozisyon başına %2 acil stop'unu değiştirmeden kullanır (D8). %1.5'lik stop yalnızca cross-sectional bacaklar içindir.

Rebalance haftalık yapılır (D1); hedef ağırlıklar bu kısıtlar altında projekte edilir. Çıkışlar yalnızca rebalance ile olur, catastrophic stop hariç. Cross-sectional için "işlem" tanımı **rebalance dönemi × varlık bacağı**dır. Planned risk = bacağın notional'ı × per-asset stop mesafesi (%1.5 equity sınırına karşılık gelen fiyat hareketi) + gidiş-dönüş maliyet. R bu planned risk'e göre hesaplanır (§6).

### 4.3 Spot cross-sectional (top-k long)
Long-only, gross ≤ 1x, eşit veya vol-ağırlıklı top-k, asset cap %40, haftalık rebalance.

## 5. Zorunlu robustness çıktıları (D3)

**Kapsam:** Raporlanan her konfigürasyon için hesaplanır. Raporlanan konfigürasyon = her hesap × aile × timeframe × evren için train-only süreçle seçilmiş walk-forward sonucu. Grid noktalarının tek tek hepsi değil. Hesaplama OOS sonuçları üzerinde yapılır:

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

Bu çıktılar **yalnızca robustness diagnostic'tir**. Ranker kabulünü ve GO/NO-GO kararını değiştirmez, hiçbiri hard veto değildir (D3). Raporda zorunlu bölüm olarak yer alırlar. Bir sonuç full'da pozitif ama ex-top1 veya herhangi bir leave-one-out'ta negatifse rapor yanına bilgilendirici **"kırılgan"** etiketi koyar. Bu etiket GO/NO-GO verdict'ini değiştirmez; okuyucunun kararına girdi olarak sunulur.

## 6. Raporlama (D5)

| Metrik | Tanım |
|---|---|
| Planned R | Girişte planlanan risk tutarı (stop mesafesi × notional + gidiş-dönüş maliyet). Cross-sectional'da per-asset catastrophic stop mesafesi kullanılır (§4.2). |
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
| Portföy kuyruk riski | Günlük getiriler üzerinden **CVaR / Expected Shortfall %95 ve %99**, historical VaR %95/%99, en kötü gün, en kötü hafta; cross-sectional için ayrıca ex-ante (tahmini) VaR/ES ile gerçekleşen kaybın karşılaştırması |
| Exposure izleme | Zaman içinde gross, net, en büyük varlık payı, vol-targeting ölçeği |

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
| V2-1 | Holdout izolasyonu + causal resampler | Sabit `V2_DATA_END = 2026-07-01Z` zorlaması ve testi (§3.4); 15m → 1h/4h/1d causal resample, karar/fill zamanlaması, nedensellik testleri |
| V2-2 | Zaman-ölçekli parametreler | Uzunlukların zaman cinsinden tanımı; timeframe başına grid |
| V2-3 | TSMOM ailesi (perp + spot) | Tradelist ↔ engine eşdeğerlik testi (V1 kalıbı) |
| V2-4 | Volatility breakout (perp + spot) | Eşdeğerlik testi |
| V2-5 | Portföy risk motoru | §4.2 sabit değerleri ve train-only grid (gross {1.0, 1.5}, vol hedefi {%10, %15}); net/asset cap/korelasyon/VaR %1.5 bütçesi; per-asset %1.5 catastrophic stop + V1 R3/R4 ile tutarlılık testleri |
| V2-6 | Cross-sectional momentum | Haftalık rebalance (günlük sensitivity), perp market-neutral + spot top-k long; rebalance tabanlı işlem tanımı |
| V2-7 | Perp maker emir modeli | SimBroker + tradelist; maker fill rate ve missed-fill counterfactual |
| V2-8 | Raporlama genişlemesi | §6 metriklerinin tamamı (CVaR/ES dahil); R tanımları; maliyet atribüsyonu |
| V2-9 | Robustness motoru | §5 diagnostic'lerinin tamamı (leave-one-out koşuları dahil); GO/NO-GO'ya etki etmez |
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
- **Hesaplama yükü:** leave-one-out koşuları walk-forward sayısını katlar. §5 kapsamı (raporlanan konfigürasyonlar) bunu sınırlar; işlem listesi cache'i ve üst timeframe'lerdeki az bar sayısı yükü dengeler.

## 11. Tutarlılık kontrolü (rev 3)

| Kontrol | Sonuç |
|---|---|
| D3 ↔ §3.2 / §3.5 / §5 | Top-winner removal ve diğer robustness çıktıları yalnızca diagnostic. Ranker kabulü (n/shrinkage/bootstrap) ve GO/NO-GO kriterleri (§3.5 madde 1–8) bunlara bağlı değil. Eski "top-1 çıkarıldığında CI > 0 GO şartı" ifadesi bu revizyonda yok. "Kırılgan" etiketi bilgilendirici. |
| D1 ↔ §2.2 / §4.2 / V2-6 | Cross-sectional rebalance birincil olarak haftalık, günlük yalnızca sensitivity. Tüm seçimler, risk grid'i dahil, yalnızca train'de. |
| D2 ↔ §4.2 | Portföy seviyesi kontroller tanımlı. Per-asset stop yalnızca catastrophic (%1.5) ve alpha exit değil. Çıkışlar rebalance ile. |
| §4.2 ↔ V1 risk (D8) | Per-asset %1.5 < V1 günlük kayıp %2; VaR bütçesi %1.5 < %2; R3/R4 değişmedi; TSMOM/breakout V1'in %2 acil stop'unu kullanıyor. Çelişki yok. |
| Pre-commit ↔ §3.3 | Risk değerleri sabit. Yalnızca 2×2 grid (gross {1.0, 1.5} × vol hedefi {%10, %15}) train-only seçiliyor ve deneme sayımına dahil. OOS'a göre ayar yok. |
| D4 ↔ §2.1 / §3.5 / §9 | Spot ayrı deney (long-only 1x; 1h/4h/1d; TSMOM/breakout/top-k). Perp ve spot ayrı GO/NO-GO; sonuçlar birleştirilmiyor. |
| D5 ↔ §6 / V2-8 | Tüm istenen metrikler ve CVaR/ES §6'da; V2-8 hepsini kapsıyor. |
| D6 ↔ §2.4 | Mean reversion ve pairs kapsam dışı; V2 task listesinde yok. |
| D7 ↔ §3.4 / §9 / V2-1 / V2-11 | Sabit `V2_DATA_END = 2026-07-01Z`, testle zorlanıyor. `luna holdout` V2'de hiç çağrılmıyor. Holdout V2 çıktısı değil. |
| D8 ↔ §2.1 / §4.1 | V1 sınıfları ve parametreleri değişmiyor; V2 yeni sınıflar ekliyor. |
| Task listesi | Tek canonical plan V2-1 … V2-11. Eski 9 görevlik taslak ve açık sorular bölümü bu revizyonda yok. |
