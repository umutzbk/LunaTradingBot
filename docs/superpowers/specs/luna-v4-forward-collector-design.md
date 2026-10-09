# Luna V4: Forward collector spec (rev 2)

**Durum:** rev 2, 2026-10-09.
- Kullanıcı, collector'ın research spec freeze'ini beklemeden yazılıp deploy edilmesine izin verdi. Şart: TDD ve independent review.
- Collector hiçbir sinyal veya getiri hesaplamaz.

**İlgili belgeler:** envanter `docs/results/v4-data-availability.md`; research spec `luna-research-v4-design.md` (rev 2).

**rev 1'den farklar:**
- Warmup ile evidence ayrımı (§2).
- Backup zorunlu (§6).
- Eşzamanlılık ve data-quality ölçümü (§7).
- Spot saat kontrolü.
- Kesinleşmiş dosya formatı (§5).

## 1. Amaç ve sınırlar

Kraken'in public perp ve spot verisini zaman damgalarıyla, **ham** ve **append-only** olarak kaydetmek.

Collector şunları **yapmaz**:
- basis, mid, spread, getiri, sinyal hesaplamak;
- boşluk doldurmak;
- veriyi yeniden örneklemek.

Tek dönüşüm, V4 evrenine süzmedir. Yalnızca `fut_tickers` ve `fut_instruments` süzülür ve ikisi de zarfta `filtered_to_universe=true` ile işaretlenir.

**Evren:** Collector config'inde sabit 21 varlık.
- ADA, AVAX, BNB, BTC, DOGE, ENA, ETH, FET, HYPE, LINK, MUBARAK, NEAR, PEPE, PUMP, SOL, SUI, TAO, UNI, XLM, XRP, ZEC.
- Perp sembolü `luna.instruments.perp_symbol`, spot çifti `luna.instruments.spot_pair` ile üretilir.

## 2. Zaman damgaları: warmup ve evidence

- **`collector_start_ts`:** collector'ın ilk başarılı başlangıcında `var/forward/meta.json` dosyasına bir kez yazılır, **bir daha üzerine yazılmaz**.
- **`V4_EVIDENCE_START_TS`:** V4 research spec'inin frozen tag zamanıdır. Collector bunu **bilmez ve saklamaz**; research tarafında spec'te sabitlenir.
  - `recv_ts < V4_EVIDENCE_START_TS` olan her kayıt **warmup**'tır: yalnızca data-quality ve sinyal lookback girdisi olarak kullanılabilir, kanıt olmaz.
- Collector tarihsel backfill yapmaz.
- **İstisna:** `fut_funding` yanıtı API'nin bütün funding geçmişini içerir (2025-10-06'dan itibaren). Bu geçmiş, sealed holdout'u da (2026-07-01 → `collector_start_ts`) kapsar.
  - Ham yanıt değiştirilmeden saklanır.
  - `forward-dq` ve research yükleyicileri `settle_ts < collector_start_ts` olan satırları **atar**. Bunu testler zorlar.

## 3. Kaynaklar ve sıklık

| source id | Endpoint | Sıklık | Not |
|---|---|---|---|
| `fut_tickers` | `GET futures.kraken.com/derivatives/api/v3/tickers` | 60 sn, :00 sn hizalı | Evren sembollerine süzülür |
| `spot_ticker` | `GET api.kraken.com/0/public/Ticker?pair=<21 çift>` | 60 sn, `fut_tickers`'tan **hemen sonra** | Eşzamanlılık ölçümü için ikili snapshot; `pair_seq` aynı |
| `fut_book` | `GET .../derivatives/api/v3/orderbook?symbol=X` | 5 dk, :02:30 ofsetli | 21 çağrı, aralarında ≥0,5 sn; yanıt tam saklanır |
| `spot_depth` | `GET .../0/public/Depth?pair=X&count=10` | 5 dk, :02:30 ofsetli | 21 çağrı, ≥1 sn aralıkla |
| `fut_funding` | `GET .../derivatives/api/v4/historicalfundingrates?symbol=X` | saatte bir, :05:00'da | Disk büyümesini sınırlamak için yalnızca `timestamp ≥ now − 48 sa` olan satırlar tutulur. Satırlar değiştirilmez; zarfta `filtered_since_ts` işaretlenir. |
| `fut_funding` (tam) | aynı | günlük 00:20 | Süzülmemiş tam yanıt (`filtered_since_ts=null`). API geçmişinin kesilmesine karşı denetim izi. |
| `fut_analytics` | `GET futures.kraken.com/api/charts/v1/analytics/<sym>/<type>?since&to&interval=300` | saatte bir, :07:00'da; son 3 saat | Tipler: open-interest, spreads, liquidity, future-basis, funding, long-short-info |
| `spot_time` | `GET api.kraken.com/0/public/Time` | 5 dk | Spot saat kayması ölçümü için |
| `fut_instruments` | `GET .../derivatives/api/v3/instruments` | günlük 00:10 + açılışta | Evrene süzülür. Listeleme yaşı, kontrat boyutu ve tick için. |
| `spot_assetpairs` | `GET .../0/public/AssetPairs?pair=<21 çift>` | günlük 00:12 + açılışta | `ordermin`, `costmin`, lot ondalıkları |

Günlük kaynaklar için bayatlık eşiği 26 saattir. Bu eşik yalnızca uyarı üretir; health çıkış kodunu etkilemez.

**Spot bot ile çakışmamak:**
- Spot bot :00/:15/:30/:45 + 20 sn'de tick atar. 5 dakikalık kaynaklar :02:30 ofsetiyle çalışır.
- Ağır kaynaklar (`fut_book`, `spot_depth`, `fut_analytics`) sırayla çalışır, aynı anda değil.

**Hata davranışı:**
- Her çağrı `luna.http.get_json` ile yapılır (retry'li).
- Başarısız çağrı da bir zarf satırı olarak yazılır: `ok=false`, `error=...`, `payload=null`. Böylece boşluk kanıtı ham veride kalır.
- Hata collector'ı çökertmez.
- Art arda 30 dakika boyunca **hiçbir** ticker çağrısı başarılı olmazsa süreç çıkış kodu 3 ile çıkar; compose `restart: unless-stopped` onu yeniden başlatır.

## 4. Zarf (her satır)

```json
{"source": "fut_tickers", "seq": 12345, "pair_seq": 678, "req_ts": "<UTC ISO ns>",
 "recv_ts": "<UTC ISO ns>", "latency_ms": 123.4, "ok": true, "error": null,
 "url": "...", "params": {...}, "filtered_to_universe": true, "payload": <ham JSON>}
```

- `seq`: kaynak başına monoton artan sayaç; restart'ta `meta.json`'dan devam eder.
- `pair_seq`: aynı dakikadaki `fut_tickers` ve `spot_ticker` çağrıları için ortak sayaç; yalnızca bu iki kaynakta bulunur.
- `req_ts` / `recv_ts`: `time.time_ns()` ile alınır, UTC.

## 5. Depolama formatı

```
var/forward/
  meta.json                                    # collector_start_ts, seq sayaçları, version
  status.json                                  # her döngüde atomik yazılır (health/dashboard)
  raw/<source>/date=YYYY-MM-DD/HH.jsonl        # açık saat: satır ekle + flush + fsync
  raw/<source>/date=YYYY-MM-DD/HH.jsonl.gz     # kapanmış saat (tmp → os.replace), .jsonl silinir
  manifest/date=YYYY-MM-DD.json                # gün kapanışı: dosya, sha256, satır, ok/hata sayıları, beklenen/gelen
  backup/date=YYYY-MM-DD.json                  # backup makbuzu (§6)
```

- **Saat kapanışı:** UTC saat bitip 2 dakika geçince o saatin `.jsonl` dosyası gzip'lenir.
  - Sıra: tmp dosyaya yazılır → `os.replace` → doğrulanır → `.jsonl` silinir. Silme de `PermissionError` backoff'u ile yeniden denenir.
  - Kapanış açılışta da çalışır, böylece önceki çalışmadan kalan açık saatler de kapatılır.
- **Doğrulanmış bir `.gz` asla yeniden yazılmaz.**
  - Zaten kapanmış bir saate gelen geç satırlar `HH.late.jsonl` dosyasına yazılır, bu da `HH.late.N.jsonl.gz` olarak kapanır.
  - `.gz` mevcutsa ve içeriği açık dosyanın baytlarıyla bitiyorsa (çökme replace ile silme arasında olmuştur) açık dosya yalnızca silinir; satırlar ikinci kez eklenmez.
- **Yarım yazma:** Yazma, flush ya da fsync başarısız olursa dosya, yazımdan önceki boyutuna geri kesilir.
- **Bozuk satır:** Gün kapanışını durdurmaz. Manifest'te `bad_lines` olarak sayılır; baytlar olduğu gibi saklanır.
- **Gün izolasyonu:** Her günün kapanışı ayrı ayrı denenir; bir gündeki hata diğerlerini bloke etmez.
- **Uyarı:** Bitiminden 4 saat geçmiş, ham verisi olan ama manifest'i olmayan her gün için status/health uyarısı ve dashboard'da kırmızı banner gösterilir.
- **Gün kapanışı:** UTC gün bitip 3 saat geçince, ve `fut_funding`/`fut_analytics`'in o güne ait son çağrıları da yazıldıktan sonra, manifest yazılır. Manifest `collector_start_ts`'i de içerir. Sonra o günün dosyaları salt okunur yapılır.
  - Manifest'i yazılmış bir güne yeni satır yazılmaya çalışılırsa hata verilir. Gece yarısını geçen geç bir yanıt da yeni günün dosyasına gider; dosya `recv_ts`'e göre seçilir.
- **Atomik yazma:** `os.replace` `PermissionError` verirse 0,2 / 0,4 / 0,8 / 1,6 / 3,2 sn backoff ile 5 kez yeniden denenir. Bu Windows sorunu spot gap-fill'de görülmüştü.
- **Normalize parquet:** Collector'ın işi değil. Research tarafı ham `.jsonl.gz`'den okur. Ham veri tek doğruluk kaynağıdır.

## 6. Backup (zorunlu davranış, hedef konfigürasyonla)

- **Hedef:** `LUNA_FORWARD_BACKUP_DIR` (bir dizin). OneDrive veya Google Drive senkron klasörü, harici disk ya da rclone mount olabilir.
  - Dizinde `.luna-backup-target` adlı bir işaret dosyası olmalı. Yoksa hedef "başlatılmamış" sayılır ve uyarı verilir.
  - Her backup'ta `meta.json` da kopyalanır.
  - Backup ayrı bir arka plan thread'inde çalışır; collection'ı hiçbir zaman bekletmez.
- **Docker:** Host'taki `LUNA_FORWARD_BACKUP_HOST_DIR` konteynerde `/backup`'a mount edilir. Env değişkeni yalnızca o ayarlıysa set edilir.
- **Kopyalama:** Manifest'i yazılmış her gün için:
  1. dosyalar `<hedef>/forward/raw/...` ve `<hedef>/forward/manifest/...` altına kopyalanır (tmp → replace);
  2. **kopyaların SHA256'sı manifest'e göre doğrulanır**;
  3. ancak bundan sonra `var/forward/backup/date=….json` makbuzu yazılır.
- **Tekrar deneme:** Doğrulama başarısız olursa makbuz yazılmaz ve kopyalama saatte bir yeniden denenir.
- **Hedef ayarlı değil veya erişilemiyor:** Collection **durmaz**. `status.json`'da `backup_ok=false` ve bir uyarı yazılır. `collector-health` uyarıyı basar. Dashboard'daki "Forward data" sayfası kırmızı uyarı gösterir.
- **Gecikme uyarısı:** Gün kapanışından sonra 36 saat içinde makbuzu yazılmamış bir gün varsa uyarı verilir.

## 7. Data-quality ölçümü (çıktısı sonuç içermez)

`luna forward-dq [--since --until]` yalnızca şunları raporlar:

| Ölçüm | Tanım |
|---|---|
| **Paired skew** | Aynı `pair_seq`'teki `fut_tickers` ve `spot_ticker` için `recv_ts` farkı (ms). Ayrıca futures ticker'ın `lastTime`'ı ile `recv_ts` arasındaki fark. |
| **Clock skew** | `recv_ts − serverTime` (futures) ve `recv_ts − spot_time.unixtime` (spot). |
| **Coverage** | Kaynak ve gün başına gelen başarılı snapshot / beklenen snapshot. |
| **Gaps** | Ardışık başarılı snapshot'lar arasında sıklığın 2 katını aşan boşluklar; her biri başlangıç, bitiş ve süreyle. |
| **Asset completeness** | Snapshot başına payload'da bulunan evren sembolü sayısı; eksik ve askıya alınmış (`suspended`) semboller. |
| **Quote sanity** | `bid ≤ ask` ve pozitif fiyat oranı (yalnızca sayım). |

**Sınırlama:**
- Kraken spot `Ticker` yanıtı kotasyon zaman damgası taşımıyor. Spot tarafında skew yalnızca istek ve yanıt zamanı üzerinden ölçülebilir.
- `spot_depth` seviye başına zaman damgası taşıyor; 5 dakikalık skew için tamamlayıcı ölçüm.

**Kapsam dışı:** Rapor fiyat seviyesi, basis, spread büyüklüğü, getiri veya P&L **göstermez**.

## 8. Health, status, dashboard

- **`luna collector-health [--strict]`:**
  - Exit 0 olması için `fut_tickers` ve `spot_ticker`'ın son başarılı yazımı < 3 dk, `fut_funding`'inki < 2 sa önce olmalı.
  - Backup uyarısı stdout'a `WARNING` olarak basılır. Yalnızca `--strict` ile exit 2 döner.
- **`status.json`:**
  - Kaynak başına: son başarılı `recv_ts`, son hata, bugünkü ok/hata sayısı.
  - Ayrıca `backup_ok` (ve neden), açık saat sayısı, `collector_start_ts`.
- **Dashboard sayfası "Forward data":** Yalnızca `status.json` ve manifest'leri okur. Gösterdiği tek şey sağlık, kapsam ve boşluklar; fiyat ya da getiri göstermez.

## 9. İzolasyon

- `luna/forward/` paketi şunları **import etmez**: `luna.engine`, `luna.execution`, `luna.store`, `luna.strategy`, `luna.portfolio`, `luna.risk`, `luna.v2`, `luna.v3`, `luna.data.backfill`. Statik bir test bunu doğrular.
- `var/forward/` dışına yazmaz. Tek istisna backup hedefidir. Bunu bir test doğrular: collector bir tmp `var` ile çalıştırılır ve `var/forward` dışında hiçbir dosya oluşmadığı kontrol edilir.
- Docker'da ayrı servis: `collector`, `command: ["collect"]`, `restart: unless-stopped`, healthcheck `luna collector-health`, bind mount `./var:/app/var`.
- **Ayrı lock:** `var/forward/collector.lock` (pid, host, heartbeat).
  - İkinci bir collector exit 1 ile reddedilir.
  - Bayat lock (heartbeat > 5 dk) devralınır. Aynı host'ta pid'i ölmüş bir lock da devralınır.
  - Compose `hostname: luna-collector` sabittir.
  - Status, heartbeat ve dead-feed kontrolü her dakika turundan sonra çalışır. Uzun turlar, sonraki slot geldiğinde kesilir.

## 10. Testler (TDD; ağ çağrısı yok, `httpx.MockTransport`)

1. **Ham veri koruma:** Ticker payload'ı zarfa tam ve tipleri değişmeden girer. Golden fixture ile JSON round-trip yapılır.
2. **Evren süzmesi:** Yalnızca 21 sembol kalır, bayrak set edilir; evrendeki eksik semboller payload'da yoktur (doldurulmaz).
3. **Hata zarfı:** Endpoint 500 veya timeout verirse süreç çökmez ve `ok=false` satırı yazılır. Sonraki tick normal devam eder.
4. **Dead-feed:** 30 dk boyunca hiç ticker başarısı yoksa çıkış kodu 3 (sahte saat ile).
5. **Saat kapanışı:** `.jsonl` → `.jsonl.gz`; içerik birebir aynı, açık dosya silinmiş. Kesintiden sonra açılışta kalan açık saatler kapatılır.
6. **Gün kapanışı:** Manifest SHA256 ve satır sayıları doğru; dosyalar salt okunur. Kapanmış güne yazma hata verir. Bozuk satır `bad_lines` olarak sayılır, kapanışı durdurmaz. Silme bir kez başarısız olursa satırlar tekrarlanmaz.
7. **`os.replace` `PermissionError`:** 2 kez hata verip sonra başarılı olursa veri kaybı yoktur; 6 kez hata verirse istisna yükselir.
8. **Backup:** Kopyalar ve makbuz yazılır. Bozuk kopyada (hash uyuşmazlığı) makbuz yazılmaz. Hedef yoksa `backup_ok=false` olur ve collection devam eder.
9. **`collector_start_ts`:** İkinci başlangıçta değişmez. `seq` restart sonrası devam eder.
10. **Lock:** İkinci süreç reddedilir; bayat lock devralınır.
11. **İzolasyon:** Yasaklı import yoktur (statik); `var/forward` dışına yazım yoktur.
12. **`forward-dq`:** Sentetik zarflardan skew, coverage ve gap'ler doğru hesaplanır. Çıktıda fiyat, basis veya getiri alanı yoktur.
13. **Zamanlama:** Sahte saat ile 10 dakikada `fut_tickers`/`spot_ticker` 10 kez, `fut_book` 2 kez çağrılır. `pair_seq` iki kaynakta eşit.
14. **Günlük kaynaklar:** `fut_instruments` ve `spot_assetpairs` açılışta ve 00:10/00:12'de çağrılır; 26 saatlik bayatlık uyarısı verilir.
15. **Holdout satırları:** `forward-dq`, `settle_ts < collector_start_ts` funding satırlarını sayımlara katmaz.
