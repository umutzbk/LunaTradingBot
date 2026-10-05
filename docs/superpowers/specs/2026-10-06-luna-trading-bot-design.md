# Luna Trading Bot — Tasarım Spec'i

- **Tarih:** 2026-10-06 (rev 2: perp futures eklendi)
- **Durum:** Taslak, kullanıcı onayı bekliyor
- **Kapsam:** P0–P2 (iskelet, veri, backtest, paper trading), iki "book" ile: **Spot** ve **Perp**. P3–P5 yalnızca yön olarak tanımlandı.

---

## 1. Amaç ve başarı kriteri

**Amaç:** Kraken'de otomatik sinyal + risk + execution sistemi kurmak. Temel soru şu:
> Backtest'te görünen edge, **komisyon + spread + slippage + funding + gerçek execution** sonrası hâlâ var mı?

Sistem iki ayrı **book** üzerinde aynı ETH/BTC relative-value fikrini test eder:

| Book | Enstrüman | Strateji | Neden |
|---|---|---|---|
| **Spot** | Kraken spot `ETH/BTC`, `BTC/GBP`, `ETH/GBP` | Rotasyon (short yok, kaldıraç yok) | UK retail için yasal olarak canlıya çıkabilir |
| **Perp** | Kraken Futures `PF_ETHUSD`, `PF_XBTUSD` (USD-margined, linear) | Market-neutral pairs: ETH long / BTC short (veya tersi), **en fazla 2x** | Komisyon ~10 kat düşük, gerçek hedge |

**Başarı (P0–P2 için):**
1. İki book'un 15 dakikalık geçmiş verisi (perp için funding dahil) indirilmiş, doğrulanmış ve Parquet'te saklanıyor.
2. Her iki book backtest edilebiliyor. Walk-forward out-of-sample bir HTML rapor üretiliyor; raporda brüt ve net sonuç yan yana duruyor, otomatik GO/NO-GO kararı var. Rapor iki book'u karşılaştırıyor.
3. Paper trading, iki book için de Docker'da 7/24 çalışıyor. Dashboard'dan izlenebiliyor.
4. Risk motorunun her kuralı testlerle doğrulanmış ve bypass edilemiyor.
5. Testlerin tamamı ağ erişimi olmadan geçiyor.

**Kapsam dışı (şimdilik):** gerçek emir (P3), LLM/AI katmanı (P5), 2x'ten büyük kaldıraç, birden fazla borsa, hisse/ETF.

### ⚠️ Yasal not (perp)
UK FCA, kripto türevlerinin (futures/perp/CFD) **retail** yatırımcılara satışını yasaklıyor (2021'den beri, 2026'da yürürlükte). Perp book'u **backtest ve paper** aşamasında yalnızca public veri kullanıyor; bu aşama serbest. **Perp'in canlıya alınması (P3)** kullanıcının yasal uygunluğuna bağlı (örn. profesyonel müşteri statüsü). Uygun değilse canlıya yalnızca Spot book çıkar. Kod, canlı perp modunu bir config bayrağı olmadan başlatmaz (`perp.live_enabled: false` varsayılan).

## 2. Sabit kararlar

| Konu | Karar |
|---|---|
| Borsa | Kraken spot + Kraken Futures. Erişim ccxt üzerinden (`kraken`, `krakenfutures`) |
| Bar | 15 dakika. Yalnızca **kapanmış** barlar kullanılır |
| Dil / araçlar | Python 3.12, `uv`, pandas, numpy, statsmodels, ccxt, SQLAlchemy, pydantic, Streamlit, plotly, pytest, ruff |
| Çalışma ortamı | Docker Compose, ilk günden. Lokal Docker Desktop (WSL2) → canlı aşamada VPS |
| Depolama | Mumlar ve funding: Parquet. Operasyonel kayıtlar: SQLite (WAL). Postgres'e geçiş yalnızca URL değişikliği |
| İzleme | Streamlit + Telegram (opsiyonel) + healthchecks.io (opsiyonel) |
| Risk parametreleri | **Yüzde cinsinden**, book başına config. Sermaye değişince (£100, £200, ...) kurallar aynen ölçeklenir |

**Kraken'den doğrulanan bilgiler (2026-10-06):**
- **Spot:** `ETH/XBT` minimum emir 0.001 ETH (~£2), tick 0.000001 (~3 bps). Komisyon config'den okunur; varsayılan maker 0.25% / taker 0.40%.
- **Perp:** `PF_XBTUSD` ve `PF_ETHUSD` için initial margin %1, maintenance %0.5, hassasiyet 0.0001 BTC / 0.001 ETH. Varsayılan komisyon maker **0.02%** / taker **0.05%**. Funding saatlik. Public API: 15 dakikalık mumlar 2023-01'den beri, funding geçmişi son ~12 ay.

## 3. Mimari

### 3.1 İlke
Strateji, risk, portföy ve emir planlama kodu **tek**. Modu belirleyen yalnızca `Feed` ve `Broker` adaptörleri. Portföy modeli hem spot bakiyeleri hem işaretli (±) perp pozisyonlarını destekler.

| Mod | Feed | Broker |
|---|---|---|
| `backtest` | `HistoricalFeed` (Parquet replay) | `SimBroker` (spot ve perp fill + maliyet + funding modeli) |
| `paper` | `KrakenSpotLiveFeed` / `KrakenFuturesLiveFeed` | `PaperBroker` (canlı order book'a göre simüle fill) |
| `live` (P3) | aynı | `KrakenSpotBroker` / `KrakenFuturesBroker` |

Her book ayrı bir süreç ve ayrı bir `run` olarak çalışır (`luna paper --book spot`, `luna paper --book perp`). Kendi risk durumu ve kendi kill-switch'i vardır.

### 3.2 Tick döngüsü (her 15 dakikalık bar kapanışında)
```
Clock → Feed.snapshot(t)                       # yalnızca t'de kapanmış barlar (+ perp için funding)
      → features (saf, nedensel fonksiyonlar)
      → Strategy.decide(snapshot, portfolio) → Decision(target, reason, features)
      → OrderPlanner: target − mevcut pozisyon → aday emirler
      → RiskEngine.check(aday emirler, context) → onaylanan emirler + vetolar
      → Broker.submit / yönet (fiyat güncelleme, iptal, eksik bacak koruması)
      → Portfolio fill'leri ve funding'i uygular, mark-to-market
      → Store: decision, order, fill, funding, equity, risk_event
      → Notifier
```
Ek olarak perp book'ta her bar içinde **acil stop kontrolü** yapılır (bkz. §6.3).

### 3.3 Paket yapısı
```
luna/
  config.py              # pydantic Settings: YAML + .env, book bazlı bölümler
  cli.py                 # luna backfill | backtest | walkforward | paper | report  (--book spot|perp)
  instruments.py         # Instrument: symbol, kind(spot|perp), base/quote, min qty, tick, fee'ler, margin oranları
  data/
    kraken_spot_history.py   # Trades endpoint'inden sayfalı backfill → 15m OHLCV (checkpoint'li)
    kraken_futures_history.py# charts API 15m mumlar + historicalfundingrates
    store.py                 # Parquet okuma/yazma, eklemeli güncelleme, doğrulama
  features/
    spread.py            # log spread, rolling OLS beta, EWMA z-score, AR(1) half-life, spread volatilitesi
    trend.py
  strategy/
    base.py              # Strategy protokolü, Decision
    spot_rotation.py     # EthBtcSpotRotation
    perp_pair.py         # EthBtcPerpPair
    btc_trend.py, hold_5050.py   # baseline/benchmark (spot)
  portfolio/
    portfolio.py         # bakiyeler + işaretli pozisyonlar, ortalama giriş, realized/unrealized P&L,
                         # funding, margin kullanımı, tahmini liquidation fiyatı
  planner.py             # hedef → aday emirler (spot: parite seçimi; perp: bacak başına delta)
  sizing.py              # risk tabanlı pozisyon büyüklüğü (perp)
  risk/
    engine.py            # RiskEngine (saf fonksiyon) + kurallar
    state.py             # kalıcı kill-switch / pause (DB'de, book bazlı)
  execution/
    base.py              # Broker protokolü, Order, Fill, durum makinesi
    costs.py             # CostModel (fee, half-spread, slippage, funding)
    sim.py               # SimBroker
    paper.py             # PaperBroker (spot + futures)
  engine/
    backtest.py, live.py, walkforward.py
  report/
    metrics.py, html.py
  store/
    db.py, models.py
  notify/
    telegram.py, heartbeat.py
dashboard/app.py
config/default.yaml
docker/Dockerfile, docker-compose.yml
tests/
```

### 3.4 Ana arayüzler
```python
@dataclass(frozen=True)
class Decision:
    ts: datetime
    target: dict[str, float]       # spot: varlık → hedef değer (book para birimi); perp: sembol → hedef işaretli notional
    signal: str                    # "LONG_SPREAD" | "SHORT_SPREAD" | "FLAT" | "HOLD" | "BUY_ETH" | ...
    reason: str                    # insan diliyle gerekçe ("z=-2.4 < -2.0, maliyet kapısı geçti, risk 1% → $118/bacak")
    features: dict[str, float]

class Strategy(Protocol):
    name: str
    def decide(self, snap: MarketSnapshot, pf: PortfolioView) -> Decision: ...

class Broker(Protocol):
    def submit(self, order: OrderRequest) -> Order: ...
    def poll(self, now: datetime) -> list[Fill]: ...
    def cancel(self, order_id: str) -> None: ...
    def balances(self) -> dict[str, float]: ...
    def positions(self) -> dict[str, float]: ...     # perp: işaretli miktar
    def open_orders(self) -> list[Order]: ...

def check(orders: list[OrderRequest], ctx: RiskContext) -> RiskResult: ...  # saf fonksiyon, I/O yok
```
Strateji doğrudan emir vermez, **hedef** üretir. Planner aradaki farkı emre çevirir. Bu sayede yeniden başlatma, kısmi fill ve elle müdahale sonrası sistem kendini düzeltir.

## 4. Veri

| Book | Kaynak | Aralık |
|---|---|---|
| Spot | Kraken public **Trades** endpoint'i (OHLC endpoint'i yalnızca son 720 barı veriyor) → 15m OHLCV + VWAP + trade sayısı. Checkpoint'li, kaldığı yerden devam eder | Son 24 ay (tahmini indirme süresi 1–3 saat) |
| Perp | Kraken Futures `charts/v1/trade/{symbol}/15m` + `historicalfundingrates` | Mumlar 2023-01 → bugün; funding son ~12 ay |

- **Doğrulama:** monoton zaman, tekrarlanan bar yok, boşluk raporu. İşlemsiz bar önceki kapanışla doldurulur (`filled=True`).
- **Funding boşluğu:** Funding geçmişinin olmadığı dönem için her sembol, mevcut geçmişin ortalama saatlik oranıyla modellenir. Rapor bunu ayrıca 0× ve 2× funding duyarlılığıyla gösterir.
- **Canlı:** feed her tick'te son barları çekip Parquet'e ekler. Paper modda order book (top 10) görüntüleri `book_snapshots` tablosuna yazılır; spread/slippage kalibrasyonu için kullanılır.

## 5. Stratejiler

### 5.1 Ortak spread modeli
- `s_t = log P_ETH − β_t · log P_BTC`. Spot book'ta `P = ETH/BTC` paritesi ve **β = 1** (tek parite işlem görüyor). Perp book'ta β, W barlık rolling OLS'ten gelir **ya da** 1 olarak sabitlenir (grid parametresi).
- `z_t = (s_t − EWMA_mean(s, W)) / EWMA_std(s, W)`.
- **Rejim filtresi:** AR(1) half-life > `max_half_life` (192 bar) ise veya `b ≥ 0` ise giriş yok. Spread'in 1 günlük volatilitesi 30 günlük medyanının 3 katını aşarsa giriş yok.
- **Maliyet kapısı:** `expected_move = |z − z_out·sign(z)| · σ_s`. Giriş yalnızca `expected_move ≥ 2 × round_trip_cost` ise yapılır. `round_trip_cost`, book'un maliyet modelinden gelir: fee + half-spread + slippage, ve perp için beklenen tutma süresi boyunca funding.

### 5.2 `EthBtcSpotRotation` (Spot book)
- Sermaye £100. Kripto sleeve %20 (£10 BTC + £10 ETH), geri kalan GBP.
- Durumlar `NEUTRAL` (50/50), `ETH_TILT` (%100 ETH), `BTC_TILT` (%100 BTC). Tilt'ten doğrudan ters tilt'e geçilmez, önce `NEUTRAL`'e dönülür.
- **Giriş:** `z < −z_in` → `ETH_TILT`; `z > +z_in` → `BTC_TILT`.
- **Çıkış:** `|z| < z_out`, `max_hold` doldu, veya stop tetiklendi.
- **Stop:** BTC cinsinden göreli %2 zarar (ETH_TILT için `1 − r_t/r_0`, BTC_TILT için `r_t/r_0 − 1`).
- **Grid:** `W ∈ {96, 288, 672}`, `z_in ∈ {1.5, 2.0, 2.5}`, `z_out ∈ {0.0, 0.5}`, `max_hold ∈ {96, 288}` → **36 kombinasyon**.
- **Benchmark:** `Hold5050` (sleeve kurulup hiç dokunulmaz) ve `BtcTrend` (EMA 96/480, BTC ya da GBP).

### 5.3 `EthBtcPerpPair` (Perp book)
- Sermaye config'den: varsayılan **$250** (~£200). Book para birimi USD; raporda GBP karşılığı da gösterilir (sabit kur config'den).
- **Pozisyonlar:**
  - `LONG_SPREAD` (`z < −z_in`): ETH long `N`, BTC short `β·N`
  - `SHORT_SPREAD` (`z > +z_in`): ETH short `N`, BTC long `β·N`
  - `FLAT`
- Aynı anda **en fazla 1** spread pozisyonu. Pozisyona ekleme yok (pyramiding/martingale yok).
- **Çıkış:** `|z| < z_out`, `max_hold = 288` bar, spread stop'u veya acil stop (§6.3).
- **Spread stop:** z, giriş z'sinden `stop_dz = 1.5` daha fazla aleyhe giderse (örn. −2.0'da girildi, −3.5'e ulaştı) pozisyon kapatılır.
- **Boyutlandırma** (`sizing.py`; kaldıraç pozisyonu belirlemez, risk ve stop mesafesi belirler):
  ```
  risk_budget    = risk_per_trade_pct × equity                  # varsayılan %1
  stop_distance  = stop_dz × σ_s + round_trip_cost_pct           # log-spread birimi ≈ yüzde
  N              = risk_budget / stop_distance                    # ETH bacağı notional
  gross          = N + β·N
  gross'u şu sınıra kırp:  max_leverage × allocated_collateral    # 2 × (%50 × equity) = 1.0 × equity
  ```
  Örnek: equity $250, risk $2.50, stop mesafesi %2 → N = $125 → gross $250 → sınırda (1.0× equity). Gerekirse kırpılır.
- **Grid:** `W ∈ {96, 288, 672}`, `z_in ∈ {1.5, 2.0, 2.5}`, `z_out ∈ {0.0, 0.5}`, `β_mode ∈ {fixed_1, rolling_ols}` → **36 kombinasyon**.
- **Benchmark:** nakit (0 getiri). Strateji market-neutral olduğu için ayrıca getirilerin BTC ile korelasyonu raporlanır; ≈0 olması beklenir.

## 6. Risk motoru

`RiskEngine.check` saf bir fonksiyondur. Kurallar **sırayla** uygulanır, ilk başarısız kural emri veto eder ve bir `risk_event` yazılır. Tüm limitler **book equity'sinin yüzdesi** olarak tanımlıdır.

### 6.1 Ortak kurallar

| # | Kural | Spot varsayılan | Perp varsayılan | Tetiklenince |
|---|---|---|---|---|
| R1 | Kill-switch aktif | — | — | Tüm emirler veto |
| R2 | Pause aktif (`/pause`) | — | — | Tüm emirler veto |
| R3 | Günlük kayıp (UTC 00:00 equity'sine göre, mark-to-market) | %3 (£3) | %2 | O gün yeni pozisyon yok + bildirim |
| R4 | Toplam drawdown (equity zirvesinden) | %15 | %15 | **Kalıcı kill-switch** + açık pozisyonlar kapatılır. Yalnızca `/resume` ile açılır |
| R5 | Emir başına notional | ≤ %10 equity | ≤ %60 equity (bacak başına) | Veto |
| R6 | Toplam maruziyet, emir sonrası | kripto ≤ %30 equity | gross notional ≤ `max_leverage × allocated_collateral` = 2 × %50 = **%100 equity** | Veto |
| R7 | Günlük emir sayısı | ≤ 8 | ≤ 12 | Veto |
| R8 | Limit fiyat ile mid arası fark | ≤ %0.5 | ≤ %0.3 | Veto |
| R9 | Bayat veri (son bar yaşı) | > 2 bar | > 2 bar | Veto |
| R10 | Borsa minimumları (min qty, tick) | parite bazlı | sembol bazlı | Veto |
| R11 | Yetersiz bakiye / margin | — | — | Veto |
| R12 | Mutabakat uyuşmazlığı (borsa ≠ defter, tolerans ±%1) | paper/live | paper/live | Halt (pause) + bildirim |

### 6.2 Perp'e özel kurallar

| # | Kural | Varsayılan |
|---|---|---|
| P1 | **Kaldıraç tavanı:** gross notional / allocated collateral | ≤ **2.0x** |
| P2 | **Martingale / pyramiding yasak:** açık pozisyonu mutlak değerde büyüten emir | Veto |
| P3 | **İşlem başı risk:** girişte `N × stop_distance` | ≤ %1 equity |
| P4 | **Liquidation mesafesi:** her bacağın tahmini liquidation fiyatına uzaklığı, stop mesafesinin en az 10 katı olmalı | Veto |
| P5 | **Eksik bacak koruması:** bir bacak dolup diğeri dolmazsa | §7.1'deki prosedür |
| P6 | `perp.live_enabled == false` iken `live` modda emir | Veto (yasal koruma) |

### 6.3 Stop'lar
- **Spot stop:** §5.2 (BTC cinsinden göreli %2).
- **Perp spread stop:** §5.3 (`stop_dz`), bar kapanışında değerlendirilir.
- **Perp acil stop:** Açık spread pozisyonunun mark-to-market zararı equity'nin **%2'sine** ulaşırsa (normal riskin 2 katı, yani bir gap ya da korelasyonun kopması durumu) pozisyon hemen kapatılır.
  - Backtest'te bar içi high/low ile kontrol edilir; çıkış, stop seviyesine slippage eklenmiş fiyattan yapılır (kötümser).
  - Paper ve live'da 1 dakikalık ticker kontrolüyle yapılır.
  - P3'te ayrıca her bacak için borsa tarafında bir stop emri konur (felaket senaryosu için, bot ölürse bile).
- **Stop zararı garanti etmez.** Gap ve slippage, backtest'te bu kötümser kuralla ve paper'da gerçek book ile modellenir.
- Pozisyon küçülten ve kapatan emirler R3, R5 ve R7'den muaftır; R1 ve R2 kapatmalar için de geçerlidir. İstisna: R4'ün kill-switch'i önce pozisyonları kapatır, sonra kilitler.

**API anahtarı (P3):** yalnızca sorgulama + emir verme/iptal yetkisi. **Withdraw yetkisi yok.**

## 7. Execution ve maliyet modeli

### 7.1 Emir yaşam döngüsü
`NEW → OPEN → (PARTIAL) → FILLED | CANCELED | REJECTED`. Her emrin benzersiz bir `client_id`'si vardır. Gönderimden önce DB'ye yazılır; aynı emir iki kez gönderilmez.

- **Spot:** post-only limit, alışta best bid / satışta best ask. 5 dakikada bir yeniden fiyatlanır (en fazla 2 kez). 15 dakika sonunda hâlâ dolmamışsa iptal edilir.
- **Perp:** iki bacak aynı anda **agresif IOC limit** emirle gönderilir (karşı taraf ± bant ≤ %0.3, taker komisyonu). Bu, fiyat korumalı bir market emri gibi çalışır. Düz market emri hiç yoktur.
  - **Eksik bacak:** bir bacak dolup diğeri dolmazsa, dolmayan bacak 3 kez daha denenir ve her denemede bant genişler (en fazla %0.5). Yine olmazsa dolan bacak kapatılır, bir risk olayı yazılır ve bildirim gider.

### 7.2 `SimBroker` (backtest)
- **Spot limit:** bar t'de üretilen emir, bar t+1'de fiyat limitin **içinden geçerse** dolar (yalnızca dokunması yetmez). Maker fee uygulanır, dolmazsa iptal.
- **Perp IOC:** bar t+1 açılış fiyatı ± half_spread ± slippage, taker fee. İki bacak aynı bar'da dolar. Eksik bacak riski backtest'te modellenmez; paper'da ölçülür.
- **Varsayılanlar:** half_spread ETH/BTC spot 3 bps, BTC/GBP 2, ETH/GBP 3; perp her sembol için 1 bps. Slippage spot 0, perp 2 bps.
- **Funding:** perp pozisyonuna her saat `pozisyon notional × relativeFundingRate` uygulanır (long öder / short alır, oran negatifse tersi).
- **Duyarlılık:** Rapor spot'u taker fill varsayımıyla, perp'i 2× maliyet ve 0×/2× funding ile tekrar gösterir.

### 7.3 `PaperBroker`
- **Spot:** canlı best bid/ask'e göre fiyatlanır. Emir açıkken gelen public trade'lerde fiyat limitin içinden geçerse dolar.
- **Perp:** IOC emir, canlı order book derinliğinde yürütülerek simüle edilir (gerçekçi ortalama fill fiyatı). Funding, canlı ticker'daki orandan saatlik uygulanır.
- Aynı `Portfolio` ve `RiskEngine` kodu kullanılır.

### 7.4 Dayanıklılık
- **Açılışta:** durum yüklenir, borsadan (paper'da simülasyondan) mutabakat yapılır, sonra devam edilir.
- **Ağ hatası:** 3 deneme backoff ile. Başarısız olursa o tick atlanır. Art arda 4 tick başarısız olursa bildirim gider. Perp'te açık pozisyon varken 2 tick üst üste veri alınamazsa `pause` + bildirim.
- **Çakışma koruması:** aynı book için ikinci bir süreç başlatılamaz (DB'de lock).

## 8. Backtest metodolojisi

- **Nedensellik testi:** Veri t'de kesildiğinde t'ye kadarki feature değerleri değişmemeli (lookahead'i yakalar). Rolling OLS beta da aynı testle doğrulanır.
- **Walk-forward:** eğitim 6 ay, test 2 ay, adım 2 ay. Her eğitim penceresinde 36 kombinasyonun en iyisi seçilir; seçim kriteri spot için *Hold5050'ye göre günlük fazla getiri Sharpe'ı*, perp için *günlük net getiri Sharpe'ı*. Raporda yalnızca out-of-sample parçalar birleştirilerek gösterilir.
  - Spot: ~24 ay veri → 6 pencere
  - Perp: ~42 ay veri → 15 pencere
- **Holdout:** son 3 ay walk-forward'a girmez. `luna backtest --book X --holdout` ile **bir kez** çalıştırılır ve bu DB'ye kaydedilir.
- **Metrikler:** toplam/yıllık getiri, benchmark'a göre fazla getiri, Sharpe, max drawdown, işlem sayısı, kazanma oranı, turnover, **brüt / fee / spread+slippage / funding / net P&L kırılımı**, işlem başı brüt edge ÷ işlem başı maliyet, fill oranı, (perp) ortalama kaldıraç ve maksimum kaldıraç, BTC ile korelasyon, stop tetiklenme sayısı, denenen kombinasyon sayısı.
- **GO/NO-GO** (book başına, raporda otomatik gösterilir):
  1. OOS net (fazla) getiri > 0
  2. Walk-forward pencerelerinin ≥ %60'ında net > 0
  3. İşlem başı brüt edge ≥ 1.5 × işlem başı maliyet
  4. OOS işlem sayısı ≥ 20 (altındaysa sonuç "YETERSİZ VERİ")
  5. (perp) 2× maliyet senaryosunda da OOS net ≥ 0
- **Sentetik doğrulama:** (a) Güçlü OU serisi + sıfır maliyet → brüt kâr > 0. (b) Rastgele yürüyüş → brüt ≈ 0, net < 0. (c) Perp: iki bacak aynı oranda hareket ederse (spread sabit) P&L ≈ −maliyet.
- NO-GO da geçerli bir sonuçtur.

## 9. Depolama şeması (SQLite)

| Tablo | Alanlar (özet) |
|---|---|
| `runs` | id, book, mode, strategy, params(json), started_at, git_sha |
| `decisions` | run_id, ts, signal, target(json), features(json), reason |
| `orders` | client_id, run_id, ts, symbol, side, qty, limit_price, tif, status, reprice_count, exchange_id, leg_group |
| `fills` | client_id, ts, price, qty, fee, fee_ccy, liquidity(maker/taker) |
| `funding` | run_id, ts, symbol, position, rate, amount |
| `equity` | run_id, ts, equity, gross_exposure, leverage, balances(json), positions(json) |
| `risk_events` | run_id, ts, rule, detail, action |
| `state` | book, key, value, updated_at (kill_switch, paused, day_start_equity, peak_equity, lock) |
| `book_snapshots` | ts, symbol, bids(json), asks(json) |

Backtest çıktıları `var/reports/<run_id>/` klasörüne yazılır (HTML + Parquet).

## 10. İzleme

- **Streamlit** (`:8501`, salt okunur), sayfa başında book seçici (Spot/Perp):
  - *Live*: equity, benchmark, pozisyonlar, kaldıraç göstergesi, z-score grafiği (eşik ve stop çizgileriyle), son kararlar ve gerekçeleri
  - *Orders/Fills/Funding*
  - *Risk*: olaylar, kill-switch durumu
  - *Backtests*: raporlar ve iki book'un karşılaştırması
- **Telegram** (token yoksa no-op): fill, stop, risk olayı ve hata bildirimleri, günlük özet. Komutlar `/status`, `/pause [book]`, `/resume [book]`, `/kill [book]`; yalnızca whitelist'teki chat ID'den kabul edilir.
- **Heartbeat** (opsiyonel URL) ve JSON loglar.

## 11. Docker

- `docker/Dockerfile`: `python:3.12-slim` + `uv sync --frozen`, non-root kullanıcı.
- `docker-compose.yml` servisleri:
  - `bot-spot`: `luna paper --book spot`
  - `bot-perp`: `luna paper --book perp`
  - `dashboard`: Streamlit
  - Bot servislerinde `restart: unless-stopped` ve healthcheck (son tick < 20 dk).
- Volume: `./var:/app/var`. Secrets `.env` dosyasında (git dışı).
- Tek seferlik işler: `docker compose run --rm bot-perp luna backfill --book perp`, `... luna walkforward --book perp`.
- **Host:** lokal Docker Desktop (uyku modu kapalı). P3'te VPS: AWS Lightsail 1 GB (~$5/ay) veya Hetzner CX22 (~€4/ay).

## 12. Test stratejisi

- **Birim:** feature'lar, nedensellik testi, rolling OLS, iki stratejinin durum makineleri, sizing (örnek hesaplar ve kırpma), planner, risk kurallarının her biri (tablo tabanlı), portföy muhasebesi (spot bakiyeleri, perp ortalama giriş, realized/unrealized P&L, funding, kısmi kapatma, işaret değişimi), liquidation fiyatı tahmini, SimBroker fill ve maliyet kuralları, metrikler.
- **Entegrasyon:** sentetik verilerle backtest motoru (§8); kaydedilmiş fixture'larla PaperBroker; geçici SQLite ile yeniden başlatma ve mutabakat; perp eksik bacak senaryosu.
- **Ağ yok:** ccxt çağrıları fake ile değiştirilir. Gerçek ağa giden testler `-m network` ile işaretlenir ve varsayılan olarak atlanır.
- **TDD:** her birimde önce test yazılır.

## 13. Yol haritası

| Aşama | Teslimat | Para |
|---|---|---|
| **P0** | İskelet, config, Docker, iki book için veri + doğrulama, feature'lar | £0 |
| **P1** | Stratejiler, sizing, planner, risk, portföy, SimBroker, backtest, walk-forward, HTML rapor, GO/NO-GO, iki book'un karşılaştırması | £0 |
| **P2** | PaperBroker (spot + perp), live engine, SQLite, Streamlit, Telegram, heartbeat; iki book Docker'da sürekli paper modda | £0 |
| P3 | Gerçek emir: Spot £10–20. **Perp yalnızca yasal uygunluk teyidinden sonra.** Paper ile gerçek fill'ler karşılaştırılır, VPS'e taşınır | £10–20 |
| P4 | Ölçekleme (£100–200), seçilen book/strateji | — |
| P5 | AI/ML rejim katmanı: hangi stratejinin hangi rejimde aktif olacağı (trend → momentum, yatay → mean reversion, volatilite patlaması → maruziyeti azalt). Baseline ile baseline+AI A/B karşılaştırması | — |

## 14. Riskler ve açık noktalar

- **Edge çıkmayabilir.** Rapor bunu dürüstçe gösterecek şekilde tasarlandı.
- **Perp yasal uygunluğu** kullanıcıya bağlı; kodda varsayılan olarak kapalı (§1, P6).
- **Funding geçmişi yalnızca ~12 ay.** Öncesi ortalama ile modelleniyor ve duyarlılık analizi raporlanıyor.
- **Spot backfill süresi:** checkpoint'li. Çok uzun sürerse aralık 12 aya düşürülür.
- **Bar tabanlı fill modeli** kuyruk önceliğini modellemez. Kötümser kurallar, duyarlılık analizi ve paper kalibrasyonu bunu dengeliyor.
- **Perp eksik bacak riski** backtest'te modellenmiyor; paper'da ölçülüyor ve raporlanıyor.
- **Komisyon kademeleri** hesaba göre değişebilir; config'den güncellenmesi gerekir.
