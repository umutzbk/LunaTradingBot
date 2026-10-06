# Luna — Varlıktan Bağımsız Sistematik Trading Motoru · Tasarım Spec'i

- **Tarih:** 2026-10-06 (rev 3: çok varlıklı, çok stratejili motor)
- **Durum:** Taslak, kullanıcı onayı bekliyor
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
1. Kripto evreninin 15 dakikalık perp mumları (2023-01'den beri) ve funding geçmişi indirilmiş, doğrulanmış, Parquet'te.
2. Üç strateji, rejim kapıları, ranker, portfolio constructor ve risk motoru ile **portföy seviyesinde** walk-forward backtest yapılabiliyor. HTML rapor:
   - strateji × varlık × rejim kırılımı
   - brüt / fee / spread+slippage / funding / net P&L
   - perp hesap ile spot hesap (yalnızca long, 1x) karşılaştırması
   - otomatik GO/NO-GO
3. Perp ve spot hesaplar Docker'da 7/24 paper modda çalışıyor; dashboard'dan izlenebiliyor.
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
| Sermaye | Varsayılan **$265 (~£200)**, hesap başına. Tüm limitler yüzde cinsinden, sermaye büyüdükçe aynen ölçeklenir |
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
    kraken_spot.py         # canlı OHLC/book (paper/live için)
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
- **Funding boşluğu:** Funding geçmişi olmayan dönemde (~12 aydan öncesi) her sembol, kendi gözlenen ortalama saatlik oranıyla modellenir. Rapor ayrıca 0× ve 2× funding duyarlılığını gösterir.
- **Spot (backtest):** Spot fiyatı için aynı varlığın perp mumları **proxy** olarak kullanılır (15 dakikalık barlarda basis ihmal edilebilir); spot komisyon ve spread uygulanır. Bu varsayım raporda açıkça yazılır. Paper/live modda gerçek spot verisi kullanılır.
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
- **Parite seçimi** (train penceresi başında): evrendeki tüm pariteler arasından getiri korelasyonu > 0.7, Engle-Granger p < 0.05 ve half-life 8 ile 192 bar arasında olanların ilk 5'i. ETH/BTC her zaman izlenir.
- `s = log A − β log B` (rolling OLS β), z-score. `|z| > z_in` → spread'e ters pozisyon. Çıkış `|z| < z_out`. Stop `stop_dz`. Time-stop var.
- **Spot hesapta pairs devre dışı** (short gerektirir). Spot için yalnızca ETH/BTC'de "rotasyon" yorumu ileride eklenebilir (YAGNI).
- **Grid:** `W ∈ {96, 288, 672}`, `z_in ∈ {1.5, 2.0, 2.5}`, `z_out ∈ {0, 0.5}` → 18 kombinasyon.

### 5.4 Rejim (V1, kural tabanlı, varlık başına)
- `trend_strength = |EMA(96) − EMA(480)| / ATR(96)`. Bu değer > T ise trend (yön: işaretten), değilse `RANGE`.
- 1 günlük gerçekleşen volatilite, 90 günlük dağılımının 90. yüzdeliğinin üstündeyse `HIGH_VOL`. Bu rejimde yeni pozisyonların risk bütçesi ×0.5 olur, mean reversion girişi yapılmaz.
- T, train penceresinde **sabit** bir yüzdelik ile belirlenir (overfitting'i sınırlamak için optimize edilmez).

### 5.5 Ranker (V1: geçmiş expectancy)
- **Train penceresinde** her `(strateji, parametre, varlık/parite, rejim)` hücresi için vektörel trade listesi çıkarılır. Hesaplanan değerler: net getiri/işlem (bps, tüm maliyetler dahil), işlem sayısı, standart sapma.
- **Seçim:** her strateji için, train'deki net expectancy'si en yüksek parametre seti seçilir (hücre bazında değil, strateji bazında; overfitting'i sınırlamak için).
- **Shrinkage:** `edge = (n / (n + n0)) × mean_net_bps`, `n0 = 30`. Az işlemli hücreler 0'a çekilir.
- **Skor:** `score = edge / stop_distance_bps`. Bu, işlem başı riske göre beklenen getiriyi (R-çarpanı) verir.
- `edge ≤ 0` veya `n < 10` olan hücreden aday kabul edilmez.
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
- **Metrikler:** getiri, Sharpe, Sortino, max DD, Calmar, işlem sayısı, kazanma oranı, ortalama R, **P&L kırılımı** (brüt / fee / spread+slip / funding / net). Kırılımlar: strateji, varlık, rejim, hesap tipi (perp/spot). Ayrıca BTC ile korelasyon, ortalama ve maksimum kaldıraç, stop tetiklenmeleri, denenen kombinasyon sayısı.
- **Benchmark'lar:** nakit (0), BTC buy & hold, eşit ağırlıklı evren buy & hold.
- **Ablation:** rejim kapıları kapalı; ranker yerine rastgele seçim. Böylece her katmanın katkısı ölçülür.
- **GO/NO-GO** (hesap başına):
  1. OOS net getiri > 0
  2. Pencerelerin ≥ %60'ında net > 0
  3. Ortalama R > 0 ve işlem başı brüt edge ≥ 1.5 × maliyet
  4. OOS işlem sayısı ≥ 30 (altındaysa "YETERSİZ VERİ")
  5. 2× maliyet senaryosunda net ≥ 0
  6. Max DD < %15
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

| Aşama | Teslimat |
|---|---|
| **P0** | İskelet, config, Docker, instruments + evren, perp veri backfill + doğrulama, feature'lar, rejim |
| **P1** | 3 strateji, research trade listeleri, expectancy + ranker, constructor, risk, portföy, SimBroker, portföy backtest'i, walk-forward, ablation, HTML rapor, GO/NO-GO |
| **P2** | PaperBroker (perp + spot), live engine, SQLite, Streamlit, Telegram, heartbeat; iki hesap Docker'da sürekli paper modda |

Sonrası: alt proje #4 (canlı, önce spot £10–20; perp yalnızca yasal uygunluk teyidinden sonra) → #2 (hisse/ETF) → #3 (ML/AI).

## 14. Riskler

- **Edge çıkmayabilir.** Rapor ve ablation bunu dürüstçe gösterecek şekilde tasarlandı.
- **Overfitting:** 3 strateji × küçük grid'ler, strateji bazında seçim, shrinkage, holdout ve denenen kombinasyon sayısının raporlanması.
- **Survivorship bias, funding boşluğu, spot için perp proxy:** raporda açıkça not edilir.
- **Perp yasal uygunluğu:** kullanıcıya bağlı; kodda varsayılan olarak kapalı.
- **Küçük sermayede borsa minimumları:** $265 ile bazı adaylar minimumun altında kalabilir. Constructor bunları atlar ve raporda sayısı gösterilir.
- **Sermaye uyarısı:** Bu bir deney bütçesi. Tamamen kaybedilmesi kişisel finansal durumu etkilemeyecek bir tutar olmalı.
