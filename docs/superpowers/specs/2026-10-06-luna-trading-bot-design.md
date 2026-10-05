# Luna Trading Bot — Tasarım Spec'i

- **Tarih:** 2026-10-06
- **Durum:** Taslak, kullanıcı onayı bekliyor
- **Kapsam:** P0–P2 (iskelet, veri, backtest, paper trading). P3–P5 yalnızca yön olarak tanımlandı.

---

## 1. Amaç ve başarı kriteri

**Amaç:** Kraken'de spot kripto üzerinde otomatik sinyal + execution sistemi kurmak. Temel soru şu:
> Backtest'te görünen edge, **komisyon + spread + slippage + gerçek execution** sonrası hâlâ var mı?

Para kazanmak birincil hedef değil. Projenin değeri, bu soruyu güvenilir şekilde cevaplayabilen bir **altyapı**.

**Başarı (P0–P2 için):**
1. 2 yıllık 15 dakikalık Kraken verisi indirilmiş, doğrulanmış ve Parquet'te saklanıyor.
2. Üç strateji, maliyet modeli dahil backtest edilebiliyor. Walk-forward out-of-sample bir HTML rapor üretiliyor; raporda brüt ve net sonuç yan yana duruyor.
3. Paper trading Docker'da 7/24 çalışıyor. Kararları, simüle edilmiş fill'leri ve equity'yi DB'ye yazıyor. Dashboard'dan izlenebiliyor.
4. Risk motorunun her kuralı testlerle doğrulanmış ve bypass edilemiyor.
5. Testlerin tamamı ağ erişimi olmadan geçiyor.

**Kapsam dışı (şimdilik):** gerçek emir (P3), LLM/AI katmanı (P5), kaldıraç/margin/short, birden fazla borsa.

## 2. Sabit kararlar

| Konu | Karar |
|---|---|
| Borsa | Kraken (spot). Erişim ccxt üzerinden |
| Pariteler | `ETH/BTC` (işlem), `BTC/GBP`, `ETH/GBP` (sleeve kurulumu, değerleme, trend baseline) |
| Bar | 15 dakika. Yalnızca **kapanmış** barlar kullanılır |
| Dil / araçlar | Python 3.12, `uv`, pandas, numpy, statsmodels, ccxt, SQLAlchemy, pydantic, Streamlit, plotly, pytest, ruff |
| Çalışma ortamı | Docker Compose, ilk günden. Lokal Docker Desktop (WSL2) → canlı aşamada VPS (aynı compose dosyası) |
| Depolama | Mumlar: Parquet. Operasyonel kayıtlar: SQLite (WAL). Postgres'e geçiş yalnızca URL değişikliği |
| İzleme | Streamlit dashboard + Telegram (opsiyonel; token yoksa no-op) + healthchecks.io ping (opsiyonel) |

Kraken'in public verisi (2026-10-06): `ETH/XBT` minimum emir 0.001 ETH (~£2), tick 0.000001 (~3 bps), fiyat ~0.0316. `XBT/GBP` minimum 0.00005 BTC. Komisyon API'den dönmüyor, bu yüzden config'den okunuyor. Varsayılan Kraken Pro taban kademesi: **maker 0.25%, taker 0.40%**.

## 3. Mimari

### 3.1 İlke
Strateji, risk, portföy ve emir planlama kodu **tek**. Modu belirleyen yalnızca `Feed` ve `Broker` adaptörleri:

| Mod | Feed | Broker |
|---|---|---|
| `backtest` | `HistoricalFeed` (Parquet replay) | `SimBroker` (bar tabanlı kötümser fill modeli) |
| `paper` | `KrakenLiveFeed` (OHLC + order book + public trades) | `PaperBroker` (canlı book/trade akışına göre simüle fill) |
| `live` (P3) | `KrakenLiveFeed` | `KrakenBroker` (gerçek emir) |

### 3.2 Tick döngüsü (her 15 dakikalık bar kapanışında)
```
Clock → Feed.snapshot(t)                       # yalnızca t'de kapanmış barlar
      → features (saf, nedensel fonksiyonlar)
      → Strategy.decide(snapshot, portfolio) → Decision(target, reason, features)
      → OrderPlanner: target − mevcut pozisyon → aday emirler
      → RiskEngine.check(aday emirler, context) → onaylanan emirler + vetolar
      → Broker.submit / yönet (fiyat güncelleme, iptal)
      → Portfolio fill'leri uygular, GBP mark-to-market
      → Store: decision, order, fill, equity, risk_event
      → Notifier (fill, risk olayı, hata)
```
Backtest'te sinyal t barının kapanışında üretilir. Emir **t+1 barında** dolabilir; aynı bar içinde dolum yoktur.

### 3.3 Paket yapısı
```
luna/
  config.py            # pydantic Settings: YAML + .env
  cli.py               # luna backfill | backtest | walkforward | paper | report
  data/
    kraken_history.py  # Trades endpoint'inden sayfalı backfill (checkpoint ile kaldığı yerden devam)
    ohlcv.py           # trade → 15m OHLCV aggregation, boşluk/doğrulama
    store.py           # Parquet okuma/yazma, eklemeli güncelleme
  features/
    spread.py          # log oran, EWMA z-score, AR(1) half-life, spread volatilitesi
    trend.py           # EMA'lar
  strategy/
    base.py            # Strategy protokolü, Decision
    eth_btc_spread.py
    btc_trend.py
    hold_5050.py
  portfolio/
    portfolio.py       # bakiyeler, ledger, GBP değerleme, günlük P&L referansı
  planner.py           # hedef → aday emirler (hangi parite, hangi yön, miktar)
  risk/
    engine.py          # RiskEngine (saf fonksiyon) + kurallar
    state.py           # kalıcı kill-switch / pause (DB'de)
  execution/
    base.py            # Broker protokolü, Order, Fill, durum makinesi
    sim.py             # SimBroker + CostModel
    paper.py           # PaperBroker
  engine/
    backtest.py        # bar replay döngüsü
    live.py            # 15 dakikalık scheduler, mutabakat, yeniden başlatma güvenliği
    walkforward.py
  report/
    metrics.py
    html.py            # plotly ile HTML rapor
  store/
    db.py, models.py   # SQLAlchemy tabloları
  notify/
    telegram.py        # gönderim + komutlar (/status /pause /resume /kill)
    heartbeat.py
dashboard/app.py       # Streamlit, salt okunur
config/default.yaml
docker/Dockerfile, docker-compose.yml
tests/
```

### 3.4 Ana arayüzler
```python
@dataclass(frozen=True)
class Decision:
    ts: datetime
    target_gbp: dict[str, float]   # {"BTC": 10.0, "ETH": 10.0}: sleeve içindeki hedef değer
    signal: Literal["BUY_ETH", "BUY_BTC", "NEUTRAL", "HOLD"]
    reason: str                    # insan diliyle gerekçe ("z=-2.4 < -2.0, maliyet kapısı geçti")
    features: dict[str, float]

class Strategy(Protocol):
    name: str
    def decide(self, snap: MarketSnapshot, pf: PortfolioView) -> Decision: ...

class Broker(Protocol):
    def submit(self, order: OrderRequest) -> Order: ...
    def poll(self, now: datetime) -> list[Fill]: ...
    def cancel(self, order_id: str) -> None: ...
    def balances(self) -> dict[str, float]: ...
    def open_orders(self) -> list[Order]: ...

def check(orders: list[OrderRequest], ctx: RiskContext) -> RiskResult: ...  # saf fonksiyon, I/O yok
```
Strateji doğrudan BUY/SELL emri vermez, **hedef pozisyon** üretir. Planner aradaki farkı emre çevirir. Bu sayede yeniden başlatma, kısmi fill ve elle müdahale sonrası sistem kendini düzeltir.

## 4. Veri

- **Kaynak:** Kraken'in OHLC endpoint'i yalnızca son 720 barı verdiği için geçmiş veri **public Trades endpoint'inden** sayfalı çekilir (`since` cursor'ı ile). Ardından 15 dakikalık OHLCV + VWAP + trade sayısına aggregate edilir. İndirme checkpoint'li: kesilirse kaldığı yerden devam eder. ccxt rate limit'ine uyulur.
- **Aralık:** varsayılan son 24 ay (config'den değiştirilebilir). 3 parite için tahmini süre 1–3 saat.
- **Alternatif kaynak:** Kullanıcı Kraken'in indirilebilir OHLCVT CSV arşivini `var/raw/` klasörüne koyarsa importer bunu da okur (opsiyonel).
- **Doğrulama:** monoton zaman, tekrarlanan bar yok, boşluk raporu. İşlemsiz bar: OHLC önceki kapanışla doldurulur, hacim 0 ve `filled=True` bayrağı konur.
- **Canlı:** `KrakenLiveFeed` her tick'te son barları OHLC endpoint'inden çeker ve Parquet store'a ekler. Paper modda order book (top 10) anlık görüntüsü de `book_snapshots` tablosuna yazılır; bu veri spread/slippage kalibrasyonu için kullanılır.

## 5. Stratejiler

### 5.1 Sermaye ve sleeve
- Başlangıç: **£100 GBP**. Kripto sleeve **£20**: başlangıçta £10 BTC + £10 ETH (BTC/GBP ve ETH/GBP üzerinden alınır, maliyetler dahil). Kalan GBP'de durur.
- Strateji yalnızca sleeve içinde hedef belirler. Sleeve'in GBP değeri piyasa ile değişir; hedefler **o anki sleeve değerinin oranları** olarak hesaplanır.

### 5.2 `EthBtcSpread` (ana strateji)
- `r_t = ETH/BTC` kapanış fiyatı, `s_t = log r_t`.
- `z_t = (s_t − EWMA_mean(s, W)) / EWMA_std(s, W)`. EWMA'nın span'i W bar.
- **Durumlar:** `NEUTRAL` (50/50), `ETH_TILT` (%100 ETH), `BTC_TILT` (%100 BTC).
- **Giriş:** `NEUTRAL` durumda
  - `z < −z_in` ve kapılar geçiyorsa → `ETH_TILT`
  - `z > +z_in` ve kapılar geçiyorsa → `BTC_TILT`
- **Çıkış (→ `NEUTRAL`):** `|z| < z_out`, **veya** pozisyonda `max_hold` bar geçti, **veya** stop tetiklendi (bkz. 6).
- Tilt'ten doğrudan ters tilt'e geçiş yoktur, önce `NEUTRAL`'e dönülür.
- **Maliyet kapısı:** `expected_move = |z − z_out·sign(z)| · σ_s` (log birimi). Giriş yalnızca `expected_move ≥ k_cost · round_trip_cost` ise yapılır. `round_trip_cost = 2·(fee_maker + half_spread + slippage)`, varsayılan `k_cost = 2`.
- **Rejim filtresi:** rolling AR(1) (`Δs_t = a + b·s_{t-1}`) ile hesaplanan half-life `−ln2/ln(1+b)`. `b ≥ 0` veya half-life > `max_half_life` ise giriş yok. Spread'in 1 günlük gerçekleşen volatilitesi, 30 günlük medyanının `vol_mult` katını aşarsa giriş yok.
- **Parametre grid'i** (walk-forward'da seçilir): `W ∈ {96, 288, 672}` (1/3/7 gün), `z_in ∈ {1.5, 2.0, 2.5}`, `z_out ∈ {0.0, 0.5}`, `max_hold ∈ {96, 288}`. Toplam **36 kombinasyon**. Sabitler: `max_half_life = 192` bar, `vol_mult = 3`, `k_cost = 2`.

### 5.3 `BtcTrend` (baseline)
- BTC/GBP üzerinde `EMA(96)` > `EMA(480)` ise sleeve'in tamamı BTC, değilse sleeve GBP. Parametreler sabit; walk-forward'a girmez.

### 5.4 `Hold5050` (benchmark)
- Sleeve £10 BTC + £10 ETH olarak kurulur ve hiç dokunulmaz. **Spread stratejisinin rakibi budur:** performans "Hold5050'ye göre fazla getiri" olarak ölçülür.

## 6. Risk motoru

`RiskEngine.check` saf bir fonksiyondur. Aday emirleri ve bir `RiskContext` (portföy, gün başı equity, başlangıç equity, kalıcı durum, son bar zamanı, o günkü emir sayısı, pariteye ait limitler) alır. Kurallar **sırayla** uygulanır, ilk başarısız kural emri veto eder ve bir `risk_event` yazılır.

| # | Kural | Varsayılan | Tetiklenince |
|---|---|---|---|
| R1 | Kill-switch aktif | — | Tüm emirler veto |
| R2 | Pause aktif (`/pause`) | — | Tüm emirler veto |
| R3 | Günlük kayıp (UTC 00:00 equity'sine göre, GBP mark-to-market) | £3 | O gün veto + bildirim; ertesi gün otomatik açılır |
| R4 | Toplam drawdown (başlangıç equity'sine göre) | £15 | **Kalıcı kill-switch**. Yalnızca `/resume` ile açılır |
| R5 | Emir başına notional | ≤ £10 | Veto (planner emirleri zaten ≤ £10 parçalara böler) |
| R6 | Toplam kripto değeri, emir sonrası | ≤ £30 | Veto |
| R7 | Günlük emir sayısı | ≤ 8 | Veto |
| R8 | Limit fiyat ile mid arası fark | ≤ %0.5 | Veto |
| R9 | Bayat veri (son bar yaşı) | > 2 bar | Veto |
| R10 | Kraken minimumları (`ordermin`, `costmin`) | parite bazlı | Veto |
| R11 | Yetersiz bakiye | — | Veto |
| R12 | Mutabakat uyuşmazlığı (borsa bakiyesi ≠ defter, tolerans ±%1) | paper/live | Halt (pause) + bildirim |

**Stop (strateji seviyesi, risk motorunda zorunlu tutulur):** Tilt pozisyonu giriş oranına göre BTC cinsinden %2 zarardaysa `NEUTRAL` zorunlu olur. ETH_TILT için kayıp `1 − r_t/r_0`, BTC_TILT için `r_t/r_0 − 1`. Stop'un tetiklediği "NEUTRAL'e dön" emirleri R3 ve R7'den muaftır; pozisyon küçültmek her zaman serbesttir. R1 ve R2 ise bunlar için de geçerlidir.

**Market emri yoktur.** Yalnızca limit emir kullanılır.

**API anahtarı (P3):** yalnızca *Query Funds, Query Orders, Create & Modify Orders, Cancel Orders* yetkileri. **Withdraw yetkisi yok.**

## 7. Execution ve maliyet modeli

### 7.1 Emir yaşam döngüsü
`NEW → OPEN → (PARTIAL) → FILLED | CANCELED | REJECTED`. Her emrin benzersiz bir `client_id`'si vardır (canlıda Kraken `userref`). Gönderimden önce DB'ye yazılır; böylece aynı emir iki kez gönderilmez.

**Fiyatlama:** alışta best bid'e, satışta best ask'e post-only limit. Dolmazsa 5 dakikada bir yeniden fiyatlanır (en fazla 2 kez). 15 dakikalık pencere sonunda hâlâ dolmamışsa iptal edilir; bir sonraki tick hedefi yeniden değerlendirir.

### 7.2 `SimBroker` (backtest)
- Bar t'de üretilen limit emir, bar t+1'de **fiyat limitin içinden geçerse** dolar: alışta `low < limit`, satışta `high > limit`. Fiyat yalnızca limite *dokunursa* dolmaz (kötümser). Dolum fiyatı = limit fiyat.
- Limit fiyatı: `close_t ± half_spread` (alışta bid'e eşdeğer).
- Komisyon: maker oranı. Ek slippage varsayılanı 0; parametre olarak açık.
- Dolmayan emir t+1 sonunda iptal edilir.
- Varsayılan `half_spread`: ETH/BTC 3 bps, BTC/GBP 2 bps, ETH/GBP 3 bps. Paper verisiyle kalibre edilecek.
- **Duyarlılık:** Rapor aynı stratejiyi taker fill varsayımıyla da gösterir: sonraki bar open + half_spread fiyatından, taker komisyonuyla. Böylece fill modeline ne kadar bağımlı olduğumuz görünür.

### 7.3 `PaperBroker`
- Emir canlı best bid/ask'e göre fiyatlanır. Dolum kuralı: emir açıkken gelen public trade'lerde alış için `price < limit`, satış için `price > limit` görülürse dolar (kötümser).
- Trade akışı 30 saniyede bir Kraken'den çekilir. Bakiyeler simüle edilir ama aynı `Portfolio` ve `RiskEngine` kodundan geçer.

### 7.4 Dayanıklılık (paper/live)
- **Açılışta:** DB'deki durum yüklenir. Açık emirler borsadan (paper'da simülasyondan) okunup mutabakat yapılır, ardından devam edilir.
- **Ağ hatası:** exponential backoff ile 3 deneme. Başarısız olursa o tick atlanır ve bir hata olayı yazılır. Art arda 4 tick başarısız olursa bildirim gönderilir.
- **Çakışma koruması:** aynı DB üzerinde ikinci bir bot süreci başlatılamaz (DB'de lock satırı).

## 8. Backtest metodolojisi

- **Nedensellik testi:** Feature'lar tüm veri üzerinde vektörel hesaplanır. Bir test, veri t'de kesildiğinde t'ye kadarki feature değerlerinin değişmediğini doğrular. Bu, geleceği görmeyi (lookahead) yakalar.
- **Walk-forward:** eğitim 6 ay, test 2 ay, adım 2 ay. Her eğitim penceresinde 36 kombinasyonun her biri çalıştırılır; **günlük fazla getiri (vs Hold5050) Sharpe'ı** en yüksek olan seçilip sonraki 2 aylık test penceresinde uygulanır. Rapor out-of-sample parçaları birleştirerek gösterir.
- **Holdout:** son 3 ay walk-forward'a girmez. Ayrı bir komutla (`luna backtest --holdout`), yalnızca walk-forward sonuçlarına bakıldıktan sonra **bir kez** çalıştırılır. Çalıştırıldığı DB'ye kaydedilir.
- **Metrikler:** toplam/yıllık getiri (GBP), Hold5050'ye göre fazla getiri, Sharpe (günlük, yıllıklaştırılmış), max drawdown, işlem sayısı, kazanma oranı, turnover, **brüt ve net P&L**, maliyetin brüt kârdaki payı, **işlem başı ortalama brüt edge / işlem başı maliyet**, fill oranı, denenen kombinasyon sayısı.
- **Paper'a geçme kriteri** (raporda otomatik "GO/NO-GO" olarak gösterilir):
  1. OOS net fazla getiri > 0
  2. Walk-forward test pencerelerinin ≥ %60'ında net fazla getiri > 0
  3. İşlem başı brüt edge ≥ 1.5 × işlem başı maliyet
  4. OOS işlem sayısı ≥ 20 (daha azı istatistiksel olarak anlamsız; o durumda "YETERSİZ VERİ")
- NO-GO sonucu da geçerli bir sonuçtur. Paper trading altyapı testi olarak yine çalıştırılabilir.
- **Sentetik doğrulama:** Motorun doğruluğu iki sentetik seriyle test edilir. (a) Güçlü OU mean-reversion serisi: maliyet sıfırken strateji brüt kâr etmeli. (b) Rastgele yürüyüş: beklenen brüt kâr ≈ 0, net kâr < 0.

## 9. Depolama şeması (SQLite)

| Tablo | Alanlar (özet) |
|---|---|
| `runs` | id, mode, strategy, params(json), started_at, git_sha |
| `decisions` | run_id, ts, signal, target(json), features(json), reason |
| `orders` | client_id, run_id, ts, pair, side, qty, limit_price, status, reprice_count, exchange_id |
| `fills` | client_id, ts, price, qty, fee, fee_ccy |
| `equity` | run_id, ts, gbp_total, gbp_crypto, balances(json) |
| `risk_events` | run_id, ts, rule, detail, action |
| `state` | key, value, updated_at (kill_switch, paused, day_start_equity, lock) |
| `book_snapshots` | ts, pair, bids(json), asks(json) (paper modda) |

Backtest sonuçları DB'ye değil `var/reports/<run_id>/` klasörüne yazılır (HTML + Parquet).

## 10. İzleme

- **Streamlit** (`:8501`, salt okunur):
  - *Live*: equity eğrisi, Hold5050 ile karşılaştırma, bakiyeler, z-score grafiği (±z_in/z_out çizgileriyle), son 50 karar ve gerekçeleri
  - *Orders/Fills*
  - *Risk*: olaylar, kill-switch/pause durumu
  - *Backtests*: `var/reports` listesi, iframe içinde rapor
- **Telegram** (`TELEGRAM_TOKEN` ve `TELEGRAM_CHAT_ID` yoksa no-op): fill, risk olayı ve hata bildirimleri, günlük 08:00 UTC özeti. Komutlar `/status`, `/pause`, `/resume`, `/kill`; yalnızca whitelist'teki chat ID'den kabul edilir.
- **Heartbeat:** `HEALTHCHECK_URL` tanımlıysa her tick'te ping atılır.
- **Loglar:** JSON formatında stdout'a (`docker logs`).

## 11. Docker

- `docker/Dockerfile`: `python:3.12-slim` + `uv sync --frozen`, non-root kullanıcı.
- `docker-compose.yml` servisleri:
  - `bot`: varsayılan komut `luna paper`, `restart: unless-stopped`, healthcheck (son tick < 20 dk)
  - `dashboard`: Streamlit, port 8501
- Volume: `./var:/app/var` (Parquet, SQLite, raporlar). Secrets `.env` dosyasında; git'e girmez.
- Tek seferlik işler: `docker compose run --rm bot luna backfill`, `... luna walkforward`.
- **Host:** Lokal Docker Desktop (WSL2). Uyku modunun kapalı olması gerekir. P3'te aynı compose dosyası bir VPS'e taşınır; seçenekler §13'te.

## 12. Test stratejisi

- **Birim:** feature'lar (bilinen girdi → bilinen çıktı), nedensellik testi, strateji durum makinesi, planner, risk kurallarının her biri (tablo tabanlı, başarılı ve başarısız durumlar), portföy muhasebesi (komisyon ve GBP değerleme), SimBroker fill kuralları, metrikler.
- **Entegrasyon:** sentetik verilerle backtest motoru (§8); kaydedilmiş Kraken fixture'larıyla PaperBroker; geçici SQLite ile yeniden başlatma ve mutabakat.
- **Ağ yok:** ccxt çağrıları fake/fixture ile değiştirilir. Gerçek ağa giden tek test `-m network` ile işaretlenir ve varsayılan olarak atlanır.
- **TDD:** her birimde önce test yazılır.

## 13. Yol haritası

| Aşama | Teslimat | Para |
|---|---|---|
| **P0** | Repo iskeleti, config, Docker, veri backfill + doğrulama, feature'lar, testler | £0 |
| **P1** | Strateji, planner, risk, portföy, SimBroker, backtest motoru, walk-forward, HTML rapor, GO/NO-GO | £0 |
| **P2** | PaperBroker, live engine, SQLite store, Streamlit, Telegram, heartbeat; Docker'da paper modda sürekli çalışma | £0 |
| P3 | `KrakenBroker`, gerçek £10–20, en az 2 hafta. Paper ile gerçek fill'lerin karşılaştırması; VPS'e taşıma | £10–20 |
| P4 | £100 ile seçilen strateji | £100 |
| P5 | AI katmanı (rejim sınıflandırma / haber sentiment filtresi), baseline ile baseline+AI A/B | — |

**VPS seçenekleri (P3):** AWS Lightsail 1 GB (~$5/ay) ya da Hetzner CX22 (~€4/ay). İhtiyaç ~1 vCPU / 1 GB. Kraken için latency önemsiz.

## 14. Riskler ve açık noktalar

- **Edge çıkmayabilir.** Beklenen sonuç budur ve rapor bunu dürüstçe gösterecek şekilde tasarlandı.
- **Trades endpoint'iyle backfill süresi/kesintileri:** Checkpoint'le çözülüyor. Çok uzun sürerse aralık 12 aya düşürülür.
- **Rotasyon modeli market-neutral değil:** sleeve her zaman kriptoda. Bu yüzden değerlendirme Hold5050'ye göre yapılıyor.
- **Bar tabanlı fill modeli** gerçek kuyruk önceliğini modellemez. Kötümser kural ve taker duyarlılık analizi bunu dengeliyor; paper verisi kalibre ediyor.
- **Komisyon kademesi** hesaba göre değişebilir. Config'den güncellenmesi gerekir.
