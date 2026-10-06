# Luna — Varlıktan Bağımsız Sistematik Trading Motoru · Tasarım Spec'i

- **Tarih:** 2026-10-06 (rev 4: kullanıcı review düzeltmeleri — gerçek spot verisi, funding dürüstlüğü, tek sermaye bütçesi, konservatif ranking, Sharpe dışı GO/NO-GO, pairs için multiple-testing düzeltmesi, naive baseline'lar, kanıta bağlı yol haritası)
- **Durum:** Onaylandı (rev 4 düzeltmeleriyle)
- **Bu spec'in kapsamı:** Alt proje **#1 — Luna Core + Kripto** (P0–P2: veri, backtest, paper trading)

---

## 0. Büyük resim ve alt projeler

**Nihai hedef:** Varlıktan bağımsız bir sistematik trading motoru. Likit bir evreni (kripto, ABD hisseleri, ETF'ler) tarar, birkaç farklı stratejiden aday işlemler üretir, bunları risk-ayarlı beklenen getiriye göre **sıralar**, en iyi 1–3 tanesini deterministik risk kuralları altında işleme sokar. Çoğu zaman hiçbir şey yapmaması normaldir.

```
MARKET DATA (venue adaptörleri: Kraken Futures, Kraken Spot, [IBKR])
   → Feature Engine
   → Regime (V1: kurallar · V2: ML)
   → Stratejiler: Momentum | Mean Reversion | Pairs   → aday işlemler
   → Ranker (V1: geçmiş beklenen getiri · V2: ML)
   → Portfolio Constructor (en fazla 3 pozisyon, korelasyon, risk bütçesi)
   → Risk Engine (deterministik, bypass edilemez)
   → Execution → Store → Dashboard / Telegram
```

| # | Alt proje | İçerik | Ne zaman |
|---|---|---|---|
| **1** | **Luna Core + Kripto** (bu spec) | Varlıktan bağımsız motor, Kraken Futures (perp) + Kraken Spot, 3 strateji, kural tabanlı rejim, geçmişe dayalı ranker, backtest/walk-forward, paper, dashboard, Telegram | Şimdi |
| 2 | Hisse/ETF venue | IBKR adaptörü, seans takvimi, hisse/ETF veri kaynağı, SPY/TLT, QQQ/SPY gibi pariteler | #1 paper'da çalıştıktan sonra |
| 3 | ML/AI katmanı | ML ranker (işlem sonucunu tahmin), ML rejim modeli, LLM haber/açıklama; baseline ile baseline+ML A/B | #1'in OOS sonuçlarından sonra |
| 4 | Canlıya geçiş (P3+) | `KrakenFuturesBroker` / `KrakenSpotBroker`, borsa tarafı stop'lar, VPS | Paper doğrulandıktan sonra |

**Başarı ölçütü (ilk yıl):** "%X kazandık" değil. Şu soruya live sonuçlarla cevap vermek:
> Fees + spread + slippage + funding sonrası pozitif expectancy, kabul edilebilir drawdown ile var mı?

Ve hangi strateji, hangi varlık sınıfı, hangi rejimde bu edge'i yaratıyor?

## 1. Alt proje #1: amaç ve başarı kriterleri

**Teslimat (P0–P2):**
1. Kripto evreninin 15 dakikalık perp mumları (2023-01'den beri), funding geçmişi ve **gerçek spot OHLCVT** verisi indirilmiş, doğrulanmış, Parquet'te.
2. Üç strateji, rejim kapıları, ranker, portfolio constructor ve risk motoru ile **portföy seviyesinde** walk-forward backtest yapılabiliyor. HTML rapor:
   - strateji × varlık × rejim kırılımı
   - brüt / fee / spread+slippage / funding / net P&L
   - perp hesap ile spot hesap (yalnızca long, 1x) karşılaştırması; aynı $265 bütçenin iki alternatif kullanımı olarak
   - naive baseline'lara karşı karşılaştırma
   - otomatik GO/NO-GO
3. Perp ve spot alternatifleri Docker'da 7/24 paper modda çalışıyor (ayrı standalone koşular); dashboard'dan izlenebiliyor.
4. Risk motorunun her kuralı testlerle doğrulanmış.
5. Testler ağ erişimi olmadan geçiyor.

**Kapsam dışı:** gerçek emir, hisse/ETF, ML/LLM, 2x üstü kaldıraç.

### ⚠️ Yasal not (perp)
UK FCA, kripto türevlerinin (futures/perp/CFD) **retail** yatırımcılara satışını yasaklıyor (2021'den beri, 2026'da yürürlükte). Backtest ve paper yalnızca public veri kullanıyor ve serbest. **Perp'in canlıya alınması** kullanıcının yasal uygunluğuna bağlı (örn. profesyonel müşteri statüsü). Kodda `accounts.perp.live_enabled: false` varsayılandır; bu durumda canlı perp emri risk motorunca veto edilir. Uygunluk yoksa canlıya **spot hesap** çıkar (aynı stratejiler, yalnızca long, 1x). Hisse tarafında (alt proje #2) short ve margin UK retail için yasal.

## 2. Sabit kararlar

| Konu | Karar |
|---|---|
| Venue (#1) | Kraken Futures (USD-margined linear perp `PF_*`) + Kraken Spot (USD pariteleri). ccxt (`krakenfutures`, `kraken`) |
| Hesap tipleri | **perp**: long/short, gross kaldıraç ≤ 2x · **spot**: yalnızca long, 1x. Aynı stratejiler; spot'ta short sinyalleri "flat" olarak yorumlanır |
| Para birimi | Hesaplar USD. Raporda GBP karşılığı (config'deki kur ile) |
| Sermaye | **Toplam deney bütçesi tek: $265 (~£200).** Perp ve spot **alternatif dağıtımlardır**, aynı anda çalışan iki ayrı hesap değildir: "£200'ü perp'e koysam" ile "£200'ü spot'a koysam" karşılaştırılır. Her biri ayrı bir standalone backtest/paper koşusudur ve rapor bunları hiçbir zaman toplamaz. İleride bütçe bölünürse (`accounts.*.capital_share`), toplam yine $265 olur. Tüm limitler yüzde cinsinden |
| Bar | 15 dakika, yalnızca kapanmış barlar |
| Evren (#1) | Kraken PF perp'leri; 30 günlük ortalama hacim ≥ $10M **ve** ≥ 18 ay geçmiş. Bugün itibarıyla tahminen BTC, ETH, SOL, XRP, ADA, DOGE, LINK, NEAR, SUI, ZEC (~10–15 varlık). Liste walk-forward penceresi başında o güne kadarki veriye göre yeniden seçilir |
| Araçlar | Python 3.12, uv, pandas, numpy, statsmodels, ccxt, SQLAlchemy, pydantic, Streamlit, plotly, pytest, ruff |
| Ortam | Docker Compose (lokal Docker Desktop → canlıda VPS) |
| Depolama | Parquet (mum, funding) + SQLite/WAL (operasyon). Postgres'e geçiş yalnızca URL değişikliği |

**Kraken'den doğrulananlar (2026-10-06):** PF perp'lerde initial margin %1, maintenance %0.5. Komisyon maker 0.02% / taker 0.05% (config'den). Funding saatlik. Public charts API'de 15 dakikalık mumlar ≥ 2023-01'den beri var. Funding geçmişi ~12 ay. Spot komisyon 0.25% / 0.40%.

## 3. Mimari

### 3.1 İlkeler
1. **Varlıktan bağımsız çekirdek.** `Instrument` (venue, sembol, tür, tick/lot, fee, margin, seans takvimi) dışında hiçbir kod belirli bir borsayı ya da varlığı bilmez. Yeni bir varlık sınıfı = yeni venue adaptörü + instrument tanımları.
2. **Backtest = paper = live kodu.** Modu yalnızca `Feed` ve `Broker` adaptörleri belirler.
3. **Strateji fırsat önerir, karar vermez.** Stratejiler `Candidate` üretir. Ranker, portfolio constructor ve risk motoru hangisinin işleme gireceğine karar verir.
4. **Risk tamamen deterministik.** Hiçbir model (ileride ML/LLM de dahil) risk kurallarını aşamaz.

### 3.2 Ana tipler
```python
@dataclass(frozen=True)
class Candidate:
    strategy: str                   # "momentum" | "mean_reversion" | "pairs"
    legs: tuple[Leg, ...]           # tek varlık: 1 bacak; pairs: 2 bacak (yön + β ağırlığı)
    direction: Literal[1, -1]
    entry_ref: float                # referans fiyat (veya spread değeri)
    stop: StopSpec                  # zorunlu: stop seviyesi/mesafesi (bps)
    exit: ExitSpec                  # take-profit / trailing / time-stop kuralları
    expected_edge_bps: float        # ranker tarafından doldurulur
    regime: str
    reason: str                     # insan diliyle gerekçe
    features: dict[str, float]

class Strategy(Protocol):
    name: str
    def candidates(self, snap: MarketSnapshot, regime: RegimeMap) -> list[Candidate]: ...
    def manage(self, pos: Position, snap: MarketSnapshot) -> ExitDecision: ...   # açık pozisyonu yönet

class Broker(Protocol):  # Sim / Paper / (Live)
    def submit(self, o: OrderRequest) -> Order: ...
    def poll(self, now) -> list[Fill]: ...
    def cancel(self, oid: str) -> None: ...
    def balances(self) -> dict[str, float]: ...
    def positions(self) -> dict[str, float]: ...

def risk_check(orders: list[OrderRequest], ctx: RiskContext) -> RiskResult: ...  # saf fonksiyon
```

### 3.3 Tick döngüsü (her 15 dakikalık bar)
```
1. Feed.snapshot(t)                         # tüm evren, yalnızca kapanmış barlar + funding
2. features (vektörel, nedensel)
3. regime = classify(features)              # varlık başına: TREND_UP / TREND_DOWN / RANGE / HIGH_VOL
4. Açık pozisyonlar: strategy.manage → stop / TP / trailing / time-stop çıkışları
5. Adaylar: her strateji × varlık (pairs için parite) → Candidate listesi
6. Ranker: expected_edge (geçmiş expectancy, shrinkage, maliyet düşülmüş) → score = edge / risk
7. PortfolioConstructor: score ≥ eşik, max pozisyon, korelasyon, risk bütçesi → seçilenler + boyutlar
8. Planner → emirler → RiskEngine → Broker
9. Portfolio: fill, funding, mark-to-market → Store → Notifier
```
Bar içinde ayrıca **acil stop kontrolü** yapılır (§6.3).

### 3.4 Paket yapısı
```
luna/
  config.py, cli.py
  instruments.py           # Instrument, venue metadata, seans takvimi (kripto 7/24)
  universe.py              # likidite + geçmiş uzunluğu filtresi, tarih bazlı seçim
  data/
    kraken_futures.py      # charts API 15m mumlar (sayfalı) + historicalfundingrates
    kraken_spot_archive.py # resmi OHLCVT arşivi: indir, SHA256 doğrula, 15m USD paritelerini çıkar
    kraken_spot.py         # Trades ile boşluk doldurma + canlı OHLC/book (paper/live için)
    store.py               # Parquet, eklemeli güncelleme, doğrulama
  features/                # returns, EWMA vol, ATR, EMA eğimi, trend gücü, z-score, rolling OLS β, AR(1) half-life, korelasyon
  regime/rules.py          # kural tabanlı rejim sınıflandırıcı
  strategy/
    base.py, momentum.py, mean_reversion.py, pairs.py
  ranking/
    expectancy.py          # strateji × varlık × rejim expectancy tablosu (train penceresi)
    ranker.py
  portfolio/
    constructor.py         # seçim + risk tabanlı boyutlandırma + korelasyon kontrolü
    portfolio.py           # bakiyeler, işaretli pozisyonlar, P&L, funding, margin, liquidation tahmini
  planner.py
  risk/engine.py, risk/state.py
  execution/base.py, costs.py, sim.py, paper.py
  engine/backtest.py, live.py, walkforward.py
  research/tradelists.py   # strateji × varlık bazında vektörel trade listesi (hızlı)
  research/stats.py        # block bootstrap, Benjamini-Hochberg, profit factor, CVaR
  strategy/naive.py        # P0.5 naive baseline'lar
  labeling.py              # reddedilen adaylar için counterfactual sonuç
  report/metrics.py, report/html.py
  store/db.py, store/models.py
  notify/telegram.py, notify/heartbeat.py
dashboard/app.py
config/default.yaml
docker/Dockerfile, docker-compose.yml
tests/
```

## 4. Veri

- **Perp:** Kraken Futures `charts/v1/trade/{symbol}/15m` (sayfalı) + `historicalfundingrates`. Evrendeki her sembol için 2023-01'den bugüne.
- **Funding boşluğu, dürüst yaklaşım:** Funding geçmişi olmayan dönem (~12 aydan öncesi) **"funding unavailable"** olarak işaretlenir. Hayali funding verisi üretilmez.
  - **Ana rapor:** funding P&L yalnızca bilinen dönemde gösterilir. Bilinmeyen dönemin net sonucu "funding hariç" olarak ayrıca etiketlenir ve iki dönemin sonuçları ayrı satırlarda verilir.
  - **Duyarlılık bölümü (ayrı):** bilinmeyen dönem için 0× / tahmini (gözlenen ortalama) / 2× tahmini senaryoları. Bu bölüm ana sonuç değildir.
  - **Optimizasyon (train) hayali funding'e göre yapılmaz:** bilinmeyen dönem içeren train pencerelerinde parametre seçim kriteri, *0× ve 2× tahmini senaryolarının kötü olanıdır* (worst-case). Böylece model, sahte funding avantajına göre optimize edilemez.
- **Spot, gerçek veri (proxy yok):** Spot backtest'leri Kraken'in **gerçek spot 15m OHLCVT** verisini kullanır:
  - Kaynak: Kraken'in resmi arşivi (`Kraken_OHLCVT_Full_2026Q2.zip`, 5 × 2 GB parça, SHA256 doğrulamalı, 2026-06-30'a kadar). Yalnızca evrendeki USD paritelerinin 15m dosyaları çıkarılır.
  - 2026-07-01'den bugüne olan boşluk public Trades endpoint'inden (checkpoint'li) doldurulur, sonrası canlı OHLC.
  - Arşiv yalnızca işlem olan barları içerir. Eksik barlar "işlem yok" olarak doldurulur (`filled=True`) ve fill modeli bu barlarda dolum vermez.
  - Spot ile perp arasındaki basis, likidite ve spread farkları raporda veri olarak gösterilir.
- **Doğrulama:** monoton zaman, tekrar yok, boşluk raporu, aşırı sıçrama (bar getirisi > 10σ) bayrağı.
- **Survivorship bias:** Evren her walk-forward penceresinin başında o tarihe kadar mevcut olan veriyle seçilir. Bugün listelenmiş olup geçmişte delist edilmiş coin'ler bu veride yok; bu kısıt raporda not edilir.

## 5. Stratejiler (V1)

Her stratejinin küçük bir parametre grid'i vardır. Her aday **zorunlu olarak bir stop** taşır.

### 5.1 Momentum / trend (tek varlık, long/short)
- **Sinyal:** `EMA(fast) − EMA(slow)` farkının ATR'ye oranı (normalize trend gücü) > eşik **ve** Donchian(N) kırılımı aynı yönde.
- **Rejim kapısı:** yalnızca `TREND_UP` (long) / `TREND_DOWN` (short).
- **Stop:** giriş − `k_atr × ATR` (long). Çıkış: trailing ATR stop + time-stop.
- **Grid:** `fast/slow ∈ {(48,192), (96,480)}`, `N ∈ {96, 288}`, `k_atr ∈ {2, 3}` → 8 kombinasyon.

### 5.2 Mean reversion (tek varlık, long/short)
- **Sinyal:** fiyatın EWMA(W)'den z-skoru `|z| > z_in` → ters yönde pozisyon.
- **Rejim kapısı:** yalnızca `RANGE`. `HIGH_VOL` ise giriş yok.
- **Stop:** z, girişten `stop_dz` daha aleyhe giderse. TP: `|z| < z_out`. Time-stop var.
- **Grid:** `W ∈ {96, 288}`, `z_in ∈ {2.0, 2.5}`, `z_out ∈ {0, 0.5}` → 8 kombinasyon. `stop_dz = 1.5` sabit.

### 5.3 Pairs / relative value (iki bacak, market-neutral)
- **Parite seçimi** (train penceresi başında, multiple-testing farkındalığıyla): 10–15 varlık 45–105 parite eder. Tümüne p < 0.05 uygulamak false-positive üretir. Bu yüzden:
  1. **Ön filtre** (test sayısını azaltır, p-değerine bakmaz): getiri korelasyonu > 0.7.
  2. Kalan pariteler için Engle-Granger p-değerleri hesaplanır ve **Benjamini-Hochberg FDR** düzeltmesi uygulanır (q = 0.05). Düzeltmeyi geçemeyen elenir. ETH/BTC'nin de geçmesi gerekir; istisna yok.
  3. **Stabilite:** train penceresi iki yarıya bölünür. Parite her iki yarıda da nominal p < 0.10 ve half-life 8–192 bar olmalı; β'nın iki yarı arasında değişimi < %25 olmalı.
  4. Kalanlardan en düşük düzeltilmiş p'ye sahip en fazla 5 parite alınır.
  - Rapor her pencere için test edilen parite sayısını, FDR'ı geçenleri ve stabiliteyi geçenleri gösterir. Hiçbir parite geçmezse o pencerede pairs stratejisi çalışmaz; bu da geçerli bir sonuç.
- `s = log A − β log B` (rolling OLS β), z-score. `|z| > z_in` → spread'e ters pozisyon. Çıkış `|z| < z_out`. Stop `stop_dz`. Time-stop var.
- **Spot hesapta pairs devre dışı** (short gerektirir). Spot için yalnızca ETH/BTC'de "rotasyon" yorumu ileride eklenebilir (YAGNI).
- **Grid:** `W ∈ {96, 288, 672}`, `z_in ∈ {1.5, 2.0, 2.5}`, `z_out ∈ {0, 0.5}` → 18 kombinasyon.

### 5.4 Rejim (V1, kural tabanlı, varlık başına)
- `trend_strength = |EMA(96) − EMA(480)| / ATR(96)`. Bu değer > T ise trend (yön: işaretten), değilse `RANGE`.
- 1 günlük gerçekleşen volatilite, 90 günlük dağılımının 90. yüzdeliğinin üstündeyse `HIGH_VOL`. Bu rejimde yeni pozisyonların risk bütçesi ×0.5 olur, mean reversion girişi yapılmaz.
- T, train penceresinde **sabit** bir yüzdelik ile belirlenir (overfitting'i sınırlamak için optimize edilmez).

### 5.5 Ranker (V1: geçmiş expectancy)
- **Train penceresinde** her `(strateji, parametre, varlık/parite, rejim)` hücresi için vektörel trade listesi çıkarılır. Hesaplanan değerler: net getiri/işlem (bps ve R cinsinden, tüm maliyetler dahil), işlem sayısı, standart sapma.
- **Seçim:** her strateji için, train'deki net expectancy'si en yüksek parametre seti seçilir (hücre bazında değil, strateji bazında; overfitting'i sınırlamak için).
- **Hiyerarşik shrinkage** (konservatif): hücre tahmini, bir üst seviyeye doğru çekilir. Seviyeler: strateji → strateji×varlık → strateji×varlık×rejim.
  - `edge_cell = w·mean_cell + (1−w)·edge_parent`, `w = n/(n + n0)`, `n0 = 50`
  - En üst seviye (strateji) 0'a doğru çekilir.
- **Bootstrap güven aralığı:** her hücre için expectancy'nin %90 güven aralığı hesaplanır (trade'ler üzerinde, gün bloklarıyla 2.000 örnekli block bootstrap).
- **Kabul koşulları:** Hücreden aday kabul edilmesi için şunların hepsi gerekir:
  - strateji×varlık seviyesinde **n ≥ 30** işlem
  - expectancy'nin bootstrap %90 güven aralığının **alt sınırı > 0**
  - shrink edilmiş edge > 0
  - Rejim seviyesindeki hücreler tek başına kabul sebebi olamaz; yalnızca shrink edilmiş bir ayar olarak kullanılır.
- **Skor:** `score = edge / stop_distance_bps`. Bu, işlem başı riske göre beklenen getiriyi (R-çarpanı) verir.
- Expectancy tablosu, güven aralıkları ve kabul/ret nedenleri raporda tablo olarak gösterilir.
- Ranker'ın arayüzü sabit (`score(candidate) -> float`). Alt proje #3'te ML ranker bunun yerine takılır ve A/B testi yapılır.

### 5.6 Portfolio constructor
- Adaylar skora göre sıralanır. Sırayla alınır, şu koşullar sağlandığı sürece:
  - açık pozisyon sayısı < **3**
  - toplam açık risk (stop'lar tetiklenirse kaybedilecek tutar) ≤ **%3 equity**
  - korelasyon kontrolü: 30 günlük getiri korelasyonu > 0.7 olan ve aynı yönde olan pozisyonlar aynı **risk kovası** sayılır; kova başına en fazla 1 pozisyon
  - aynı varlıkta zıt yönlü iki pozisyon yok (pairs bacakları dahil)
- **Boyutlandırma (kaldıraç değil, risk belirler):**
  ```
  risk_amt   = risk_per_trade × equity × regime_mult        # varsayılan %0.75, HIGH_VOL'da ×0.5
  notional   = risk_amt / (stop_distance + round_trip_cost)  # oran olarak
  ```
  Sonuç gross kaldıraç tavanına (2x), bacak başına tavana ve borsa minimumlarına göre kırpılır. Minimumun altında kalan aday atlanır.
  - Örnek: equity $265, risk %0.75 = $2, stop %2 + maliyet %0.15 → notional ≈ $93.

## 6. Risk motoru

`risk_check` saf bir fonksiyondur. Kurallar **sırayla** uygulanır, ilk başarısız kural emri veto eder ve bir `risk_event` yazılır. Tüm limitler hesap equity'sinin yüzdesidir ve hesap başına config'den gelir.

### 6.1 Kurallar

| # | Kural | Perp | Spot | Tetiklenince |
|---|---|---|---|---|
| R1 | Kill-switch aktif | — | — | Tüm yeni emirler veto |
| R2 | Pause aktif (`/pause`) | — | — | Tüm yeni emirler veto |
| R3 | Günlük kayıp (UTC 00:00 equity'sine göre, mark-to-market) | %2 | %2 | O gün yeni pozisyon yok + bildirim |
| R4 | Drawdown (equity zirvesinden) | %15 | %15 | Pozisyonlar kapatılır + **kalıcı kill-switch** (`/resume` ile açılır) |
| R5 | İşlem başı risk (girişte stop'a göre) | ≤ %1 | ≤ %1 | Veto |
| R6 | Toplam açık risk | ≤ %3 | ≤ %3 | Veto |
| R7 | Eşzamanlı pozisyon | ≤ 3 | ≤ 3 | Veto |
| R8 | **Gross kaldıraç** (Σ\|notional\| / equity), emir sonrası | ≤ **2.0x** | ≤ 1.0x | Veto |
| R9 | Bacak başına notional | ≤ %100 equity | ≤ %50 equity | Veto |
| R10 | **Martingale / pyramiding:** açık pozisyonu mutlak değerde büyüten emir | Veto | Veto | Veto |
| R11 | **Stop'suz pozisyon** | Veto | Veto | Veto |
| R12 | Liquidation mesafesi ≥ 10 × stop mesafesi | Kontrol | — | Veto |
| R13 | Short (spot) | — | Veto | Veto |
| R14 | Günlük emir sayısı | ≤ 30 | ≤ 20 | Veto |
| R15 | Limit fiyat ile mid arası fark | ≤ %0.3 | ≤ %0.5 | Veto |
| R16 | Bayat veri (son bar yaşı > 2 bar) | Veto | Veto | Veto |
| R17 | Borsa minimumları / yetersiz margin veya bakiye | Veto | Veto | Veto |
| R18 | Mutabakat uyuşmazlığı (tolerans ±%1) | Halt | Halt | Pause + bildirim |
| R19 | `live` modda `live_enabled == false` | Veto | Veto | Yasal/güvenlik koruması |

Pozisyon kapatan ve küçülten emirler R3, R5–R9 ve R14'ten muaftır. R1 ve R2 kapatmaları engellemez; kill-switch tetiklendiğinde zaten önce pozisyonları kapatır.

### 6.2 Stop'lar
- Her pozisyonun girişte belirlenmiş bir stop'u vardır (R11). Stop'lar bar kapanışında değerlendirilir.
- **Acil stop:** tek bir pozisyonun zararı equity'nin %2'sine (yani planlanan riskin yaklaşık 2–3 katına) ulaşırsa, bar kapanışı beklenmeden kapatılır.
  - Backtest: bar içi high/low ile kontrol edilir. Çıkış = stop seviyesi + slippage, kötümser.
  - Paper/live: 1 dakikalık ticker kontrolü.
  - P3'te ek olarak borsa tarafında stop emri konur (bot ölse bile çalışır).
- Stop zararı garanti etmez. Gap ve slippage kötümser fill ile modellenir.

**API anahtarı (P3):** sorgulama + emir verme/iptal. **Withdraw yetkisi yok.**

## 7. Execution ve maliyetler

- **Emir yaşam döngüsü:** `NEW → OPEN → PARTIAL → FILLED | CANCELED | REJECTED`. Her emrin `client_id`'si var ve gönderimden önce DB'ye yazılır (idempotent).
- **Perp:** giriş ve çıkış **agresif IOC limit** emirle (bant ≤ %0.3, taker fee). Düz market emri yok.
  - **Pairs eksik bacak:** dolmayan bacak 3 kez, genişleyen bantla (en fazla %0.5) tekrar denenir. Yine dolmazsa dolan bacak kapatılır, risk olayı yazılır.
- **Spot:** post-only limit, 5 dakikada bir yeniden fiyatlama (en fazla 2 kez), 15 dakikada dolmazsa iptal.
- **SimBroker:**
  - IOC: sonraki bar açılışı ± half_spread ± slippage, taker fee.
  - Spot limit: sonraki bar fiyat limitin **içinden geçerse** dolar.
  - Varsayılanlar: half_spread BTC/ETH 1 bps, diğerleri 3 bps; slippage 2 bps.
  - Funding saatlik uygulanır.
- **Duyarlılık:** rapor her sonucu 2× maliyet ve 0×/2× funding ile tekrar gösterir.
- **PaperBroker:** canlı order book derinliğinde yürüyerek fill simülasyonu (perp ve spot); funding canlı ticker'dan alınır. Book görüntüleri kalibrasyon için saklanır.
- **Dayanıklılık:** açılışta mutabakat; ağ hatasında 3 deneme backoff; art arda 4 tick başarısız olursa bildirim; açık pozisyon varken 2 tick üst üste veri yoksa pause; hesap başına tek süreç (DB lock).

## 8. Backtest metodolojisi

**İki katman:**
1. **Research katmanı** (`research/tradelists.py`): strateji × parametre × varlık başına vektörel trade listeleri çıkarır. Hızlıdır; parametre seçimi ve ranker'ın expectancy tablosu için kullanılır.
2. **Portföy simülasyonu** (`engine/backtest.py`): bar bar, tam döngü (ranker → constructor → risk → SimBroker). Raporlanan sonuçlar **yalnızca** bu katmandan gelir.

Diğer kurallar:
- **Walk-forward:** train 6 ay, test 2 ay, adım 2 ay (~2023-01 → bugün, holdout hariç ≈ 15 pencere). Her train penceresinde sırasıyla evren seçimi, parite seçimi, parametre seçimi, expectancy tablosu ve rejim eşikleri belirlenir; ardından test penceresi simüle edilir. OOS parçalar birleştirilir.
- **Holdout:** son 3 ay. `luna backtest --holdout` ile **bir kez** çalıştırılır; DB'ye kaydedilir.
- **Nedensellik testleri:** her feature ve rolling β için "veriyi kes, geçmiş değerler değişmesin" testi.
- **Metrikler, öncelik sırasıyla:**
  - **Birincil:** expectancy (işlem başı R ve bps) + bootstrap %90 güven aralığı, profit factor, max DD, tail loss (en kötü gün, günlük CVaR %5, en kötü işlem R'si), turnover, exposure (zamanın yüzde kaçında pozisyonda), **P&L kırılımı** (brüt / fee / spread+slip / funding / net)
  - **İkincil (yalnızca bilgi):** Sharpe ve Sortino. **Günlük** getirilerden hesaplanır, 15 dakikalık getirilerden asla. Örtüşen pozisyonlar ve otokorelasyon nedeniyle yanıltıcı olabileceği raporda not edilir; GO/NO-GO'da kullanılmaz.
  - **Kırılımlar:** strateji, varlık, rejim, hesap tipi (perp/spot). Ayrıca BTC ile korelasyon, ortalama ve maksimum kaldıraç, stop tetiklenmeleri, denenen kombinasyon sayısı.
- **Benchmark'lar:** nakit (0), BTC buy & hold, eşit ağırlıklı evren buy & hold, ve **naive baseline'lar** (§8.1).
- **Ablation:** rejim kapıları kapalı; ranker yerine rastgele seçim. Böylece her katmanın katkısı ölçülür.
- **GO/NO-GO** (hesap başına, **tüm** koşullar gerekir):
  1. OOS işlem sayısı ≥ 30 (altındaysa sonuç "YETERSİZ VERİ")
  2. OOS expectancy'nin bootstrap %90 güven aralığının alt sınırı > 0
  3. Profit factor ≥ 1.2
  4. Max DD < %15, en kötü gün > −%3, günlük CVaR %5 > −%2
  5. **2× maliyet** senaryosunda net expectancy ≥ 0
  6. Funding bilinen dönemde de net > 0 (funding'i bilinmeyen döneme dayanan bir GO verilmez)
  7. Walk-forward pencerelerinin ≥ %60'ında net > 0
  8. **Luna, en iyi naive baseline'ı OOS net expectancy'de geçmeli.** Geçemiyorsa sonuç "NO-GO: karmaşıklık değer katmıyor".

### 8.1 Naive baseline'lar (P0.5)
Rejim filtresi yok, ranker yok, eşit risk ile aynı evren, aynı maliyet modeli ve aynı risk motoru. Grid yok, sabit ve "ders kitabı" parametreler:
- **Momentum:** EMA(96) / EMA(480) kesişimi; long/short (spot'ta long/flat). Stop 3×ATR.
- **Mean reversion:** EWMA(96) z-skoru; `|z| > 2` iken ters yönde gir, `|z| < 0.5` olunca çık. Stop `|z| > 3.5`.
- **Pairs:** yalnızca ETH/BTC; β = 1, 288 barlık z-skoru; `|z| > 2` iken gir, `|z| < 0.5` olunca çık. Stop `|z| > 3.5`.

Luna'nın her katmanı (rejim, ranker, constructor), bu baseline'lara karşı ne kadar katkı sağladığını göstermek zorundadır.
- **Sentetik testler:**
  - OU serisi → mean reversion brüt kâr > 0
  - Trend + gürültü → momentum brüt kâr > 0
  - Rastgele yürüyüş → brüt ≈ 0, net < 0
  - Sabit spread → pairs P&L ≈ −maliyet

## 9. Depolama (SQLite)

| Tablo | Alanlar (özet) |
|---|---|
| `runs` | id, account(perp/spot), mode, config(json), started_at, git_sha |
| `candidates` | run_id, ts, strategy, legs(json), direction, score, edge_bps, regime, selected(bool), reject_reason |
| `positions` | id, run_id, strategy, legs(json), opened_at, closed_at, entry, stop, exit_reason, pnl_net, pnl_breakdown(json) |
| `orders` / `fills` | client_id, position_id, symbol, side, qty, price, tif, status, fee, liquidity |
| `funding` | run_id, ts, symbol, position, rate, amount |
| `equity` | run_id, ts, equity, gross_leverage, open_risk, positions(json) |
| `risk_events` | run_id, ts, rule, detail, action |
| `state` | account, key, value (kill_switch, paused, day_start_equity, peak_equity, lock) |
| `book_snapshots` | ts, symbol, bids, asks |

Backtest çıktıları: `var/reports/<run_id>/` (HTML + Parquet).

**Candidates tablosu, ileride ML için dataset olarak tasarlandı:** Seçilen **ve reddedilen** tüm adaylar, o anki feature'ları ve ret nedeniyle birlikte saklanır. Günlük bir iş (`luna label`), reddedilen adayların **counterfactual sonucunu** hesaplar: aynı stop ve çıkış kurallarıyla girilseydi net R ne olurdu. Sonuç `candidate_outcomes` tablosuna yazılır (`candidate_id`, `outcome_r`, `exit_reason`, `labeled_at`). Backtest koşuları da aynı tabloyu Parquet olarak üretir. Böylece "hangi işlemleri almalıyım?" problemi için etiketli veri kendiliğinden birikir.

## 10. İzleme

- **Streamlit** (`:8501`, salt okunur, hesap seçici):
  - *Overview*: equity, benchmark, kaldıraç, açık risk, kill-switch durumu
  - *Positions*: açık ve kapalı pozisyonlar, gerekçeleri
  - *Scanner*: bu bar'daki adaylar, skorları, neden seçilip neden seçilmedikleri
  - *Risk*: olaylar
  - *Research*: raporlar ve strateji × varlık × rejim ısı haritası
- **Telegram** (opsiyonel): giriş/çıkış, stop, risk olayı ve hata bildirimleri, günlük özet. Komutlar `/status`, `/positions`, `/pause`, `/resume`, `/kill`; yalnızca whitelist'teki chat ID'den.
- **Heartbeat** (opsiyonel healthchecks.io URL) + JSON loglar.

## 11. Docker

- `python:3.12-slim` + uv, non-root kullanıcı.
- Compose servisleri:
  - `bot-perp` (`luna paper --account perp`) ve `bot-spot` (`luna paper --account spot`): `restart: unless-stopped`, healthcheck
  - `dashboard`
- Volume: `./var:/app/var`. Secrets `.env` dosyasında.
- Tek seferlik işler: `docker compose run --rm bot-perp luna backfill`, `... luna walkforward --account perp`.
- **Host:** lokal (uyku modu kapalı). Canlıda VPS (AWS Lightsail ~$5/ay veya Hetzner ~€4/ay).

## 12. Test stratejisi

- **TDD**, her birimde önce test.
- **Birim:** feature'lar, nedensellik, rejim kuralları, her stratejinin aday üretimi ve `manage` çıkışları, expectancy + shrinkage, ranker, constructor (sınır, korelasyon kovası, boyutlandırma, kırpma), her risk kuralı (tablo tabanlı), portföy muhasebesi (işaretli pozisyonlar, kısmi kapatma, işaret değişimi, funding, fee), liquidation tahmini, SimBroker, metrikler.
- **Entegrasyon:** §8'deki sentetik testler; fixture'larla PaperBroker; geçici SQLite ile yeniden başlatma ve mutabakat; pairs eksik bacak senaryosu.
- **Ağ yok:** ağ testleri `-m network` ile işaretli, varsayılan olarak atlanır.

## 13. Yol haritası (alt proje #1)

| Aşama | Odak | Teslimat |
|---|---|---|
| **P0** | **Veri bütünlüğü** | İskelet, config, Docker, instruments + evren; perp mumları + funding; gerçek spot OHLCVT (arşiv + boşluk doldurma); doğrulama raporu (boşluk, tekrar, sıçrama, spot ile perp basis) |
| **P0.5** | **Naive baseline'lar** | Feature'lar, portföy muhasebesi, risk motoru, SimBroker, basit backtest döngüsü; §8.1'deki üç baseline'ın tüm veri üzerinde maliyetli sonuçları. "Geçilmesi gereken çıta" bu aşamada belirlenir |
| **P1** | **Strateji doğruluğu** | Rejim, 3 strateji, research trade listeleri, sentetik testler (§8), nedensellik testleri, expectancy + bootstrap + hiyerarşik shrinkage, ranker, constructor |
| **P1.5** | **Walk-forward ve acımasız doğrulama** | Walk-forward, pairs için FDR, funding dürüstlüğü, ablation, duyarlılık analizleri, HTML rapor, GO/NO-GO, baseline karşılaştırması. Holdout yalnızca en sonda ve bir kez çalıştırılır |
| **P2** | **Paper trading (birkaç ay)** | PaperBroker (perp + spot alternatifleri), live engine, SQLite, Streamlit, Telegram, heartbeat, counterfactual etiketleme (§9). Paper ile backtest davranışının karşılaştırma raporu |
| P3 | £10–20 gerçek, **spot** | Execution doğrulaması (alt proje #4). Perp yalnızca yasal uygunluk teyidinden sonra |
| P4 | £200 | Ancak P3 kapısı geçilince |
| P5 | Düzenli aylık ekleme (£100–300) | Ancak ölçekleme kapısı geçilince |

**Kanıta bağlı kapılar (takvime değil):**
- **P2 → P3:** ≥ 8 hafta paper; paper fill'leri ile backtest varsayımları arasında maliyet farkı ≤ %50; risk motorunda veya mutabakatta açıklanamayan bir olay yok.
- **P3 → P4:** ≥ 30 canlı işlem; canlı net expectancy ≥ 0; gerçekleşen maliyetler modelin ≤ 1.5 katı; operasyonel hata yok.
- **P4 → P5 (aylık £100–300):**
  - ≥ 100 canlı işlem ve ≥ 6 ay
  - canlı net expectancy'nin bootstrap %90 güven aralığının alt sınırı > 0
  - maliyetler 2 katına çıkarıldığında (simülasyon) net ≥ 0
  - max DD < %15
  - canlı ile backtest'in aynı dönemdeki davranışı yakın: expectancy, işlem sıklığı ve ortalama tutma süresi ±%50 içinde

Sonrası: alt proje #2 (hisse/ETF) → #3 (ML/AI; ancak yeterli OOS ve canlı veri biriktiğinde).

## 14. Riskler

- **Edge çıkmayabilir.** Rapor ve ablation bunu dürüstçe gösterecek şekilde tasarlandı.
- **Overfitting:** 3 strateji × küçük grid'ler, strateji bazında seçim, shrinkage, holdout ve denenen kombinasyon sayısının raporlanması.
- **Survivorship bias ve funding boşluğu:** raporda açıkça not edilir. Funding boşluğu hiçbir zaman ana sonucu ya da optimizasyonu taşımaz (§4).
- **Spot arşiv boyutu:** ~10 GB indirme. Yalnızca gerekli 15m CSV'ler çıkarılır, sonra arşiv parçaları silinebilir.
- **ML cazibesi:** ML, naive baseline'larda ve V1'de OOS edge görülmeden eklenmez. Edge yokken ML çoğunlukla yalnızca backtest'i daha güzel gösterir.
- **Perp yasal uygunluğu:** kullanıcıya bağlı; kodda varsayılan olarak kapalı.
- **Küçük sermayede borsa minimumları:** $265 ile bazı adaylar minimumun altında kalabilir. Constructor bunları atlar ve raporda sayısı gösterilir.
- **Sermaye uyarısı:** Bu bir deney bütçesi. Tamamen kaybedilmesi kişisel finansal durumu etkilemeyecek bir tutar olmalı.
