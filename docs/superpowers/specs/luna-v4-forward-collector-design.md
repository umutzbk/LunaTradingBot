# Luna V4: Forward funding/basis dataset collector (tasarım)

**Durum:**
- TASLAK, kullanıcı onayı bekliyor. Kod yazılmadı.
- Envanter: `docs/results/v4-data-availability.md`.
- Spec: `luna-research-v4-design.md`.

## 1. Amaç

2026-10-09'dan itibaren V4 hipotezlerinin ihtiyaç duyduğu, geçmişi olmayan veya API'nin kesebileceği serileri zaman damgasıyla ve değiştirilemez biçimde kaydetmek. Bu veri V4'ün **tek kirlenmemiş kanıt kaynağıdır**. Bu yüzden toplayıcı:
- hiçbir sinyal hesaplamaz;
- hiçbir şeyi filtrelemez;
- hiçbir boşluğu doldurmaz.

## 2. Ayrışma kuralları (paper ve research ile karışmaz)

- **Ayrı süreç:** docker-compose'ta ayrı bir servis, `collector`, kendi lock'uyla.
  - Paper bot'ların DB'lerine (`var/luna_*.db`) ve `var/bars/` dosyalarına **yazmaz**.
  - Engine, broker ya da strateji kodu import etmez.
- **Ayrı depolama:** Yalnızca `var/forward/` altına yazar.
- **Research erişimi:** Research kodu `var/forward/`'ı yalnızca frozen V4 spec'i ve onun açıkça adlandırdığı forward değerlendirme penceresi üzerinden okur. Ara bakış ("peeking") olmaması için forward verisinden getiri istatistiği hesaplayan her çalıştırma, ledger'a bir deneme olarak yazılır.
- **Sadece public endpoint:** API anahtarı yok, emir yok.

## 3. Kaydedilecekler

| Kaynak | Endpoint | Sıklık | Alanlar |
|---|---|---|---|
| Futures tickers (21 PF_ perp, tek çağrı) | `derivatives/api/v3/tickers` | **60 sn** | symbol, bid, ask, bidSize, askSize, last, lastTime, markPrice, indexPrice, fundingRate, relativeFundingRate, fundingRatePrediction, relativeFundingRatePrediction, openInterest, vol24h, volumeQuote, suspended, postOnly |
| Spot ticker (21 çift, tek çağrı) | `0/public/Ticker` | **60 sn** | a (ask px/size), b (bid px/size), c (last), v (volume today/24h), p (vwap), t (trade count), l/h/o |
| Spot top-of-book derinlik | `0/public/Depth?count=10` | 5 dk | ilk 10 seviye (px, qty, ts), 21 çift |
| Perp top-of-book derinlik | `derivatives/api/v3/orderbook` | 5 dk | ilk 10 seviye, 21 perp |
| Settle edilmiş funding | `derivatives/api/v4/historicalfundingrates` | 1 sa (+5 dk) | timestamp, fundingRate, relativeFundingRate; önceki saatlerle tekrarsız birleştirilir |
| Analytics | `api/charts/v1/analytics/<sym>/{open-interest, spreads, liquidity, future-basis, funding, long-short-info}` | 1 sa; son 3 saat tekrar istenir | 5 dk çözünürlük |

**Türetilmiş alanlar:** basis, mid, spread gibi alanlar toplayıcıda **hesaplanmaz**. Ham veriden research tarafında, kodu test edilmiş tek bir fonksiyonla türetilir. Böylece bir hesaplama hatası ham veriyi bozmaz.

**Zaman damgaları:** Her satırda şunlar bulunur:
- `recv_ts`: yerel UTC ns, yanıt geldiği an;
- `req_ts`: istek atıldığı an;
- `server_ts`: futures `serverTime`, spot için yok;
- varsa exchange alan zamanı (`lastTime`);
- `latency_ms`.

**Hız limitleri:** Dakikada en fazla ~2 ticker çağrısı yapılır; 5 dakikada bir 42 derinlik çağrısı, aralarında ≥1 sn bekleyerek. Bu, Kraken'in public limitlerinin altında. Spot bot'un `Trades` ve `Depth` çağrılarıyla aynı IP'yi paylaşır, bu yüzden spot derinlik çağrıları bot tick'inin olduğu dakikaya (:00/:15/:30/:45) denk getirilmez.

## 4. Depolama ve bütünlük

- **Düzen:** `var/forward/<source>/date=YYYY-MM-DD/HHMM.parquet`.
  - Saatlik dosyalar yalnızca eklenir (append-only) ve ham JSON'dan dönüştürülür.
  - Ayrıca `var/forward/raw/<source>/date=…/HH.jsonl.gz` altında ham yanıtlar saklanır (denetim izi).
- **Atomik yazma:** önce tmp dosyaya yazılır, sonra `os.replace` ile yerine konur. Windows'ta `PermissionError` görülürse kısa backoff ile 5 kez yeniden denenir; spot gap-fill'deki hata bu yüzden ele alındı.
- **Günlük kapanış:** Günün dosyaları için `var/forward/manifest/date=YYYY-MM-DD.json` yazılır: SHA256, satır sayısı, beklenen ve gelen snapshot sayısı, boşluk listesi. Kapanmış günler salt okunur yapılır.
- **Hacim tahmini:**
  - Ticker: günde ~60 bin satır.
  - Derinlik: günde ~12 bin snapshot.
  - Toplam sıkıştırılmış hacim günde onlarca MB değil, birkaç MB'tir.
- **Yedekleme:** `var/` git'e girmez. PC değişince (bu sefer olduğu gibi) forward veri **kaybolur ve geri getirilemez**. Bu yüzden haftalık bir harici kopya önerilir: bulut klasörü ya da `rclone`. Hedefi kullanıcı seçecek.

## 5. Sağlık ve izleme

- `luna collector-health`: Her kaynağın son başarılı snapshot'ı kendi eşiğinden gençse 0 döner.
  - Ticker eşiği: 3 dk.
  - Funding eşiği: 2 sa.
  - Compose healthcheck bu komutu kullanır.
- **Boşluk kaydı:** Kaçan her snapshot `gaps.parquet` dosyasına yazılır (kaynak, beklenen zaman, neden). Boşluk doldurulmaz; yalnızca kaydedilir.
- **Saat kayması:** `recv_ts − server_ts` 5 sn'yi aşarsa uyarı verilir.
- **Kendini iyileştirme:** Paper bot'lardaki `feed_dead_exit` desenini izler. Art arda 30 dk hiç yanıt alınamazsa süreç çıkış kodu 3 ile çıkar ve `restart: unless-stopped` onu yeniden başlatır.
- **Dashboard:** "Forward data" sayfası; son snapshot yaşı, günlük satır sayısı ve boşluklar. Sinyal veya getiri **göstermez**.

## 6. Tek seferlik tarihsel indirme (toplayıcıdan ayrı)

Kraken'de **gerçekten var olan** geçmiş, ayrı bir `luna backfill-v4` komutuyla `var/history_v4/` altına indirilir:
- mark ve index 15m (2022-03 →);
- OI, spreads, liquidity, future-basis (2023-03/05 →; 1 sa);
- gerçek funding (2025-10-06 →).

Kurallar:
- Her dosyaya bir provenance kaydı (endpoint, parametreler, çekim zamanı, SHA256) eşlik eder.
- **2025-10-06 öncesi funding üretilmez.** Prim proxy'si ayrı bir `premium_proxy` dosyasıdır.

## 7. Test planı (uygulama onaylanırsa, TDD)

- **Ham veri koruma:** Ticker JSON'undan satıra dönüşümde alan kaybı veya tip değişikliği olmaz. Golden fixture kullanılır.
- **Tekrarsızlık:** Aynı funding saati iki kez gelirse tek satır kalır.
- **Kapanmış güne yazma:** Manifest'i yazılmış bir güne yazma reddedilir.
- **Kesinti:** Endpoint hatası sürecin çökmesine yol açmaz; boşluk kaydı oluşur, sonraki snapshot normal alınır.
- **Atomik yazma:** `os.replace` `PermissionError` verdiğinde yeniden denenir; veri kaybı olmaz.
- **İzolasyon:** Toplayıcı modülü `luna.engine`, `luna.execution` veya `luna.store.db` import etmez. Statik import testi ile doğrulanır.
