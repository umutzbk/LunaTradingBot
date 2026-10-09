# Luna — paper trading runbook (P2)

Bu doküman Luna'nın Docker ile **paper trading** olarak çalıştırılmasını, izlenmesini ve taşınmasını anlatır.
Gerçek emir yoktur: `accounts.*.live_enabled: false` ve kodda canlı broker (KrakenBroker) yoktur.
Bütün komutlar repo kökünden (`docker-compose.yml`'in olduğu klasör) çalıştırılır.

## 1. Servisler

| Servis | Komut | Not |
|---|---|---|
| `bot-perp` | `luna paper --account perp` | Kraken Futures public verisi + PaperBroker, `restart: unless-stopped`, healthcheck |
| `bot-spot` | `luna paper --account spot` | Kraken spot public verisi + PaperBroker, aynı ayarlar |
| `dashboard` | `streamlit run dashboard/app.py` | http://localhost:8501 (yalnızca `127.0.0.1`'e bağlı, kimlik doğrulaması yok) |

Hepsi `./var` klasörünü `/app/var` olarak paylaşır: **hesap başına bir SQLite dosyası**
`var/luna_perp.db` / `var/luna_spot.db` (her botun tek yazarı kendisidir; iki konteyner aynı WAL veritabanını
Windows bind mount üzerinden paylaşmaz), `var/bars/`, `var/funding/`, `var/models/live_<account>.pkl` (canlı model
cache'i), `var/reports/`. Dashboard her iki dosyayı salt okunur açar. Secrets `.env` dosyasındadır (git'e girmez).

### Eski tek dosyalı DB'den geçiş (`var/luna.db` → `var/luna_perp.db`, bir kez)

Eski sürümler iki hesabı tek `var/luna.db`'de tutuyordu. Yeni kod bu dosya varken ve `var/luna_<account>.db`
yokken `luna paper`'ı başlatmaz (boş bir DB'den başlayıp pozisyonları "unutmamak" için). Geçiş:

```bash
docker compose build bot-perp dashboard                        # yeni kod (migrate-db komutu dahil)
docker compose stop bot-perp                                   # temiz durdurma: lock bırakılır
docker compose run --rm --no-deps bot-perp migrate-db --account perp   # veya host'ta: uv run luna migrate-db --account perp
docker compose up -d bot-perp dashboard
```

`luna migrate-db` yalnızca `var/luna.db` varsa ve `var/luna_perp.db` yoksa çalışır; dosyada taze bir lock varsa
(bot çalışıyor) reddeder; WAL'ı checkpoint edip (`TRUNCATE`) dosyayı yeniden adlandırır ve boş `-wal`/`-shm`
dosyalarını siler. Elle yapmak isterseniz: bot durmuşken `luna.db`'yi (ve varsa `-wal`/`-shm`'yi) aynı adlarla
`luna_perp.db`, `luna_perp.db-wal`, `luna_perp.db-shm` olarak yeniden adlandırın. Eski dosyada spot satırı
varsa (ör. smoke testten) zararsızdır: tüm sorgular hesaba göre filtrelenir.

perp ve spot **alternatif** deneylerdir (her biri $265); bakiyeleri toplanmaz.

## 2. Başlatma / durdurma / durum

```bash
docker compose build                       # kod değiştiğinde imajları yeniden oluştur
docker compose up -d bot-perp dashboard    # perp + dashboard
docker compose up -d                       # hepsi (bot-spot dahil; bkz. §7)
docker compose ps                          # durum + health (starting / healthy / unhealthy)
docker compose stop bot-perp               # durdur (SIGTERM → engine temiz kapanır, lock bırakılır)
docker compose restart bot-perp            # yeniden başlat (state SQLite'tan yüklenir)
docker compose down                        # tüm konteynerleri kaldır (var/ verisi kalır)
```

Yeniden başlatma güvenlidir: engine lock'u yeniden alır, pozisyonları/risk state'ini `var/luna_<account>.db`'den yükler,
açık emirleri iptal eder (yeniden planlanır), mutabakat yapar (R18) ve bilinen bir `client_id`'yi asla tekrar
göndermez. Aynı hesap için ikinci bir süreç başlatılırsa lock nedeniyle çıkış kodu 1 ile reddedilir.

İlk başlangıçta (veya `var/models/live_<account>.pkl` yoksa / `test_months` kadar eskiyse) model tüm geçmiş
üzerinde yeniden fit edilir (bu makinede ~1 dk; küçük bir VPS'te daha uzun sürebilir). İlk tick bir sonraki
15 dakikalık sınırda + 20 sn'de gelir.

## 3. Loglar

```bash
docker compose logs -f bot-perp             # canlı takip
docker compose logs --tail 200 bot-perp
docker compose logs --since 1h dashboard
```

Önemli satırlar: `live model fitted`, `engine started (paper)`, `model has no accepted expectancy cell`,
`feed error`, `stale_data`, `lock lost`. Telegram yoksa bildirimler `notify (telegram disabled): ...` olarak
loga düşer.

## 4. Health

```bash
docker compose ps                                        # STATUS sütununda (healthy)
docker inspect --format '{{.State.Health.Status}}' lunatradingbot-bot-perp-1
docker compose exec bot-perp uv run luna health --account perp     # elle; exit 0 = sağlıklı
```

`luna health --account X [--max-age-min 20] [--db PATH]` (varsayılan DB: `var/luna_X.db`): hesabın son `equity` satırı (her başarılı 15 dk'lık
tick'te yazılır) 20 dakikadan gençse 0, değilse 1 döner (DB yoksa / satır yoksa da 1). Salt okunurdur.

Compose healthcheck: `interval 60s`, `timeout 20s`, `retries 3`, `start_period 30m`. 30 dakika, soğuk
başlangıçtaki model fit'i + ilk 15 dakikalık sınıra kadar bekleme süresini kapsar; bu süre içindeki
başarısızlıklar sayılmaz, ilk başarı konteyneri hemen `healthy` yapar. Tek bir tick Kraken hatası yüzünden
atlanırsa konteyner birkaç dakika `unhealthy` görünebilir; bir sonraki başarılı tick'te düzelir. Docker
`unhealthy` konteyneri kendiliğinden yeniden başlatmaz — bu bir uyarıdır, logları inceleyin.

**Kendini iyileştirme (`feed_dead_exit`):** feed art arda 8 tick (~2 saat) hiç cevap vermezse (`feed_error` /
`no_data`; ör. host ağ değişikliği / uyku sonrası konteynerde bayatlamış DNS → `ConnectError: Name or service not
known`) engine loglar, `risk_events`'e `feed_dead_exit` yazar, state'i kaydeder, lock'u temizce bırakır ve **çıkış
kodu 3** ile çıkar; `restart: unless-stopped` süreci yeniden başlatır (açık pozisyonlar 2. veri kaybı tick'inde
zaten `paused`; yeniden başlatma state'i yükler, güvenlidir). Yeniden başlatmadan sonra da DNS bozuksa
konteyneri yeniden oluşturun: `docker compose up -d --force-recreate bot-perp`.

### healthchecks.io (dış izleme, önerilir)

1. https://healthchecks.io'da bir check oluşturun (period 1 dk, grace 10 dk). Her hesap için ayrı check önerilir.
2. Ping URL'sini `.env`'e yazın: `LUNA_HEALTHCHECK_URL=https://hc-ping.com/<uuid>`
3. `docker compose up -d` (env değişikliği konteyneri yeniden oluşturur).

Engine her dakikalık heartbeat'te bu URL'e GET atar; makine uyursa / internet kesilirse / süreç ölürse
healthchecks.io size e-posta/Telegram ile haber verir. `.env` tüm servislere aynı değeri verir; perp ve spot için
ayrı URL istenirse servis bazında `environment:` ile `LUNA_HEALTHCHECK_URL` override edilebilir.

## 5. Dashboard

http://localhost:8501 — sol menüden hesap ve sayfa seçilir. Hesap listesi mevcut `var/luna_<account>.db`
dosyalarından gelir (hiç yoksa `perp`/`spot`); seçilen hesap kendi dosyasından okunur.

- **Overview:** kill switch / paused, lock heartbeat yaşı (60 sn civarı normal), equity eğrisi, kaldıraç, açık risk.
- **Positions:** açık pozisyonlar.
- **Scanner:** her tick'te üretilen adaylar (`candidates` tablosu): strateji, anahtar, yön, skor, `edge_bps`,
  rejim, `selected` ve **reddedilme nedeni** (`reject_reason`, ör. `below_min_notional`, korelasyon kovası,
  risk kuralı). Seçilmeyen adaylar da kaydedilir ki sonradan etiketlenebilsin.
- **Risk:** `risk_events` (ör. `no_model`, `stale_data`, `R16`, `R18_reconciliation`, `restart_cancel`).
- **Research:** `var/reports/` altındaki walk-forward / baseline raporları.
- **Dataset:** counterfactual etiketleme istatistikleri (strateji başına etiketlenen aday sayısı, seçilen ve
  reddedilen adayların ortalama `outcome_r`'ı, etiketlenemeyen "terminal" sonuçlar). Reddedilen adayların
  ortalaması seçilenlerden iyiyse ranker değer katmıyor demektir.

**İki farklı R tanımı (birbirinin yerine kullanılmaz):** Dataset/Scanner'daki `outcome_r` araştırma
`r_net`'idir = karşı-olgusal işlemin net getirisi / adayın stop mesafesi (ikisi de notional'ın kesri). İşlem R'ı
(engine `r_multiple`, çıkış bildirimleri, rapordaki `expectancy_r`) = `pnl_net / risk_amount` (USD; `risk_amount`
= boyutlandırılmış pozisyonun stop'taki planlı kaybı, gidiş-dönüş maliyet dahil). Raporun "Metric glossary"
bölümüne bakın.

Dashboard'un kimlik doğrulaması yoktur; bu yüzden port yalnızca `127.0.0.1`'e açılır. VPS'te
`ssh -L 8501:localhost:8501 user@vps` ile tünelleyip http://localhost:8501 açın; portu internete açmayın.

### "no accepted cells → no trades" ne demek?

Engine başlarken ranker'ı son 6 aylık veriyle fit eder. Hiçbir (strateji × rejim) hücresi expectancy kapısını
geçmezse logda `model has no accepted expectancy cell: managing positions, opening nothing` ve `risk_events`'te
`no_model` görülür: bot **yeni pozisyon açmaz**, yalnızca varsa açık pozisyonları yönetir, equity'yi yazar ve
heartbeat atar. Stratejilerin ürettiği adaylar yine her tick kaydedilir, ama hepsi `reject_reason = ranker`
ile **reddedilmiş** olarak (Scanner'da görünür, `luna label` ile etiketlenebilir; hiçbiri gönderilmez). Bu bir arıza değil, mevcut walk-forward
sonucunun (perp: INSUFFICIENT DATA, edge kanıtlanmadı — bkz. `docs/results/walkforward.md`) doğal karşılığıdır.
Model `test_months` dolunca yeniden fit edilir; bir hücre kabul edilirse işlem açılmaya başlar. Strateji
parametrelerini paper'ı "çalışır göstermek" için değiştirmeyin.

## 6. Telegram

1. Telegram'da **@BotFather** → `/newbot` → isim ver → verilen **token**'ı alın.
2. Yeni bota bir mesaj yazın (ör. `/start`). Sonra tarayıcıda
   `https://api.telegram.org/bot<TOKEN>/getUpdates` açıp `"chat":{"id": ...}` değerini (**chat id**) not edin.
   Bunu bot çalışmıyorken yapın (engine `getUpdates`'i tüketir).
3. `.env` (repo kökünde, git'e girmez):
   ```
   LUNA_TELEGRAM_TOKEN=123456:ABC...
   LUNA_TELEGRAM_CHAT_ID=123456789
   ```
4. `docker compose up -d` (konteynerler yeni env ile yeniden oluşur).

Komutlar (yalnızca bu chat id'den gelenler işlenir; bot kapalıyken gelen komutlar başlangıçta yok sayılır):

| Komut | Etki |
|---|---|
| `/status` | equity, bayraklar, son bar, entries on/off |
| `/positions` | açık pozisyonlar |
| `/pause [perp\|spot\|all]` | yeni girişleri durdur (açık pozisyonlar yönetilmeye devam eder) |
| `/resume [perp\|spot\|all]` | **hem kill switch'i hem pause'u kaldırır** ve peak equity'yi (drawdown çapası, R4) o anki equity'ye sıfırlar; kill switch'in kapatmakta olduğu pozisyonlar strateji yönetimine geri döner. ⚠️ Bilinçli kullanın: drawdown kill'inden sonra `/resume` drawdown sayacını sıfırlar ve %15'lik limit yeni zirveden tekrar başlar. |
| `/kill [perp\|spot\|all]` | **kill switch**: yeni giriş yok, tüm pozisyonlar kapatılır; yeniden başlatmada da açık kalır |

Hedef argümanı (`/pause`, `/resume`, `/kill`): bot yalnızca argüman kendi hesabıysa veya `all` ise uygular;
argümansız komutu onu okuyan bot uygular; diğer hesaba yazılmış komutu sessizce yok sayar; bilinmeyen argüman
hata mesajı döner. Örnek: `/kill spot`, `/kill all`.

⚠️ Her iki bot **aynı token'ı** kullanırsa Telegram her güncellemeyi yalnızca önce `getUpdates` yapan bota verir
(diğeri hiç görmez) — `/kill all` bile yalnızca bir botu durdurabilir. perp ve spot'u birlikte çalıştırıyorsanız
iki ayrı bot (iki token) kullanın ve servis bazında `environment:` ile `LUNA_TELEGRAM_TOKEN`'ı override edin;
hedef argümanı o zaman aynı sohbette iki botu ayrı ayrı yönetmeyi sağlar.

Telegram'dan bağımsız kill switch (`docker compose stop` pozisyonları kapatmaz): bayrağı hesabın DB'sine yazın;
engine bir dakika içinde (minute check / tick) okur ve tüm pozisyonları kapatır:
`docker compose exec bot-perp uv run python -c "from luna.config import load_settings; from luna.store.db import Db, default_url; Db(default_url(load_settings().var_dir, 'perp')).set_flag('perp', 'kill_switch', True)"`

## 7. bot-spot'u başlatma (spot backfill bittikten sonra)

Spot backfill host üzerinde `var/bars/kraken_spot` dosyalarını yazarken bot-spot başlatılmamalıdır.

```bash
tail var/backfill_spot.log                       # bitti mi?
docker compose run --rm bot-spot backfill --venue spot   # (gerekirse) boşlukları konteynerden doldur
docker compose up -d bot-spot
docker compose logs -f bot-spot                  # model fit + "engine started (paper)"
docker compose ps                                # ~30 dk içinde (healthy)
```

Spot botu kendi dosyasını (`var/luna_spot.db`) ilk başlangıçta oluşturur; geçiş gerekmez.

Tek seferlik deneme (gerçek DB'ye yazmadan): `docker compose run --rm bot-spot paper --account spot --once --db /app/var/smoke_spot.db`
(sonra `var/smoke_spot.db*` dosyalarını silin).

## 8. Counterfactual etiketleme (`luna label`)

Olgunlaşmış adayların (seçilen + reddedilen) kendi stop/çıkış kurallarıyla net R'ını hesaplar; idempotenttir.
Günde bir kez çalıştırmak yeterlidir:

```bash
docker compose exec bot-perp uv run luna label --account perp
docker compose exec bot-spot uv run luna label --account spot
```

- Linux/VPS cron (her gün 00:30 UTC):
  ```
  30 0 * * * cd /opt/luna && docker compose exec -T bot-perp uv run luna label --account perp >> var/label.log 2>&1
  ```
- Windows: Task Scheduler → Basic Task → Daily → Program `docker`, Arguments
  `compose exec -T bot-perp uv run luna label --account perp`, "Start in" = repo klasörü.

## 9. Windows: uyku modu

Bot ancak makine uyanıkken çalışır. Uykuda tick kaçar (dönüşte stale bar → R16 girişleri bloklar; UTC gün
dönümü dönüşte bir kez işlenir).

```powershell
powercfg /change standby-timeout-ac 0      # prizdeyken uyku yok
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 15     # ekran kapanabilir, sorun değil
```

Ayrıca: Settings → System → Power → "When plugged in, put my device to sleep after: Never"; kapak kapatma
eylemi "Do nothing". Docker Desktop → Settings → General → "Start Docker Desktop when you sign in" açık olsun
(`restart: unless-stopped` konteynerleri Docker açılınca geri getirir). Windows Update yeniden başlatmaları
için "Active hours" ayarlayın ve healthchecks.io kullanın.

## 10. Kurumsal TLS proxy (bu makine)

Bu makinede Kaspersky Endpoint Security TLS trafiğini (ör. `api.kraken.com`) kendi kök sertifikasıyla yeniden
imzalıyor. Konteyner bu kökü tanımazsa Kraken çağrıları `CERTIFICATE_VERIFY_FAILED` ile düşer. Çözüm (TLS
doğrulaması **asla kapatılmaz**):

1. Kökü bulun: `openssl s_client -connect api.kraken.com:443 -servername api.kraken.com -showcerts` → `i:` satırı
   (burada `CN=Kaspersky Endpoint Security Personal Certification Authority`).
2. Windows deposundan PEM olarak dışa aktarın (PowerShell):
   ```powershell
   $c = Get-ChildItem Cert:\LocalMachine\Root | Where-Object Subject -like '*Kaspersky*' | Select-Object -First 1
   New-Item -ItemType Directory -Force docker\certs | Out-Null
   $b64 = [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks')
   "-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----`n" | Out-File -Encoding ascii docker\certs\proxy-root.crt
   ```
3. `docker compose build`. Dockerfile `docker/certs/*.crt` varsa onları sistem deposuna ekler
   (`update-ca-certificates`); `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` sistem bundle'ını, `UV_SYSTEM_CERTS=1` uv'nin
   sistem sertifikalarını kullanmasını sağlar. `docker/certs/` git'e girmez. Klasör yoksa adım atlanır, yani
   proxy'siz bir makinede (VPS) hiçbir şey yapmanıza gerek yoktur.

Host'ta uv komutları için: `UV_SYSTEM_CERTS=1 uv run ...`.

## 11. VPS'e taşıma

1. VPS (Ubuntu, ≥ 2 GB RAM önerilir; AWS Lightsail / Hetzner) üzerinde Docker Engine + compose plugin kurun.
2. Lokal botları durdurun: `docker compose stop bot-perp bot-spot` (aynı hesabı iki makinede çalıştırmayın).
3. Kopyalayın: repo (git clone veya rsync), **`var/`** (özellikle `var/luna_perp.db` / `var/luna_spot.db`, `var/bars`, `var/funding`,
   `var/instruments`, `var/models`) ve **`.env`**. `docker/certs/` gerekmez (proxy yok).
   ```bash
   rsync -av --exclude .venv --exclude docker/certs ./ user@vps:/opt/luna/
   ```
4. VPS'te: `cd /opt/luna && docker compose build && docker compose up -d`
5. `docker compose ps`, `docker compose logs -f bot-perp`, dashboard için SSH tüneli (§5).
6. VPS saatinin NTP ile senkron olduğundan emin olun (`timedatectl`).

## 12. Güvenlik notları

- Yalnızca **paper**: `live_enabled: false`; kodda canlı broker yok; API anahtarı gerekmez ve `.env`'e koymayın.
- **Perp yasal not:** Birleşik Krallık'ta perakende yatırımcıya kripto türev erişimi kısıtlıdır. perp yalnızca
  paper/araştırma amaçlıdır; gerçek paraya geçiş (P3) **spot** ile başlar, perp ancak yasal uygunluk teyidinden sonra.
- **Holdout'a dokunulmadı:** `luna holdout` her hesap için bir kez ve yalnızca en sonda çalıştırılır
  (`var/holdout_registry.json`). Paper/deploy işlemleri holdout'u çalıştırmaz; elle de çalıştırmayın.
- Strateji parametrelerini paper sonuçlarına bakarak değiştirmek walk-forward'u geçersiz kılar.
- P2 → P3 kapısı: ≥ 8 hafta paper, maliyet farkı ≤ %50, açıklanamayan risk/mutabakat olayı yok (spec §13).
- `.env`, `var/` ve `docker/certs/` git'e girmez. Telegram token'ı loglarda görünmez (httpx logları WARNING).

## 13. V4 forward data collector (`collector` servisi)

Spec: `docs/superpowers/specs/luna-v4-forward-collector-design.md`. Kraken public perp + spot verisini **ham** ve
append-only olarak `var/forward/` altına yazar; sinyal, getiri, basis veya fiyat hesabı yapmaz. Botlardan bağımsızdır
(ayrı süreç, ayrı lock `var/forward/collector.lock`; ikinci bir collector exit 1 ile reddedilir). Compose servisi sabit
`hostname: luna-collector` kullanır: yeniden yaratılan konteyner aynı host sayılır; eski pid yaşamıyorsa ve heartbeat
180 sn'den eskiyse lock devralınır (aynı pid ise hemen). Farklı host + taze heartbeat (≤ 5 dk) her zaman reddedilir.

- **Başlatma:** `docker compose up -d --build collector` (Docker'sız: `uv run luna collect`). Loglar:
  `docker compose logs -f collector`. 30 dakika boyunca hiçbir ticker çağrısı başarılı olmazsa exit 3 ile çıkar,
  `restart: unless-stopped` yeniden başlatır. Lock başka bir sürece geçerse exit 1.
- **`meta.json` kaybı:** `var/forward/raw/` doluyken `meta.json` yoksa collector **başlamaz** (exit 1), çünkü
  `collector_start_ts` sıfırlanırdı. `meta.json`'u backup hedefinden (`<hedef>/forward/meta.json`) geri koyun.
- **Kaynaklar:** her dakika `fut_tickers` + `spot_ticker` (aynı `pair_seq`); :02:30'da iki ayrı 5 dakikalık tur,
  önce spot (`spot_time`, `spot_depth`), sonra futures (`fut_book`), her birinin kendi süresi var; :05'te `fut_funding` (yalnızca son 48 saatin satırları, `filtered_since_ts`), :07'de
  `fut_analytics`; günlük 00:10 `fut_instruments`, 00:12 `spot_assetpairs`, 00:20 `fut_funding_full` (tam,
  süzülmemiş funding geçmişi; denetim izi, `filtered_since_ts=null`); bu üçü başlangıçta da çalışır (`at_start=true`).
  Bir sonraki slotu gelen uzun tur yarıda bırakılır (atlanan çağrılar `skipped_calls`) ve gelen slot hemen çalışır;
  hiç başlatılamayan slotlar `skipped_slots` olarak `status.json`'da sayılır.
- **Dosyalar:** `var/forward/raw/<source>/date=YYYY-MM-DD/HH.jsonl` (açık saat) → saat bitip 2 dk geçince
  `HH.jsonl.gz`. Kapanmış saate geç gelen satırlar `HH.late.jsonl` → `HH.late.N.jsonl.gz` olur (doğrulanmış gz
  asla yeniden yazılmaz). Gün bitip 3 saat geçince `var/forward/manifest/date=….json` (sha256, satır, ok/hata,
  okunamayan satır `bad_lines`, beklenen/gelen, `collector_start_ts`) ve o günün dosyaları salt okunur olur.
  `meta.json`: `collector_start_ts` (bir kez yazılır) ve seq sayaçları. `status.json`: kaynak başına son
  başarı/hata, bugünkü sayılar, atlanan çağrılar, backup durumu, kapanamayan günler (`unclosed_days`).
- **Health:** `docker compose exec collector uv run luna collector-health [--strict]`. Exit 0: `fut_tickers` ve
  `spot_ticker` son başarı < 3 dk, `fut_funding` < 2 sa (yeni başlayan süreçte funding için 2 sa tolerans).
  `WARNING` satırları: backup sorunu, 26 saatten eski günlük veri (`fut_instruments`, `spot_assetpairs`,
  `fut_funding_full`), bitişinden 4 saat sonra hâlâ manifest'i olmayan gün. `--strict` backup veya kapanamayan gün
  uyarısında exit 2 döner. Dashboard'daki **Forward data** sayfası aynı bilgiyi gösterir (sorunlar kırmızı).
- **Data quality:** `uv run luna forward-dq [--since 2026-10-10T00:00:00] [--until …]` (UTC) JSON basar: paired skew
  (`pair_seq`), clock skew, coverage (`at_start` satırları oranın dışında), gaps, varlık tamlığı, kotasyon sağlık
  sayıları. Fiyat/basis/spread/getiri basmaz. Veriyi akış halinde okur. `meta.json` yoksa exit 1 verir. Funding
  kapsaması `fut_funding` + `fut_funding_full` satırlarından yalnızca `collector_start_ts` sonrası settlement'ları sayar.
- **Backup (zorunlu davranış):** `.env`'de `LUNA_FORWARD_BACKUP_HOST_DIR=<host klasörü>` (OneDrive/Google Drive
  senkron klasörü, harici disk, rclone mount). Compose bunu konteynerde `/backup`'a bağlar ve yalnızca o zaman
  `LUNA_FORWARD_BACKUP_DIR=/backup` set edilir (boş değer = ayarlı değil). Docker'sız çalıştırmada doğrudan
  `LUNA_FORWARD_BACKUP_DIR` verin. **Hedefi bir kez işaretleyin:** hedef klasörün kökünde boş bir
  `.luna-backup-target` dosyası olmalı (ör. PowerShell: `New-Item -ItemType File <hedef>\.luna-backup-target`,
  Linux: `touch <hedef>/.luna-backup-target`). İşaret yoksa (bağlanmamış disk, yanlış klasör) backup yapılmaz ve
  "backup target not initialised" uyarısı verilir. Backup ayrı bir arka plan iş parçacığında çalışır; toplamayı
  bekletmez. Kapanmış her gün `<hedef>/forward/...` altına kopyalanır, sha256 manifest'e göre doğrulanır, sonra
  `var/forward/backup/date=….json` makbuzu yazılır; `meta.json` her backup'ta `<hedef>/forward/meta.json`'a
  doğrulanarak kopyalanır. Başarısızsa saatte bir yeniden denenir. Hedef yoksa/erişilemiyorsa collection **durmaz**,
  `backup_ok=false` olur; gün kapanışından 36 saat sonra hâlâ makbuz yoksa ayrıca uyarı verilir. Ayar değişince:
  `docker compose up -d collector` (konteyner yeniden yaratılır).

## 14. V5 Adaptive Funding Carry (`bot-carry` servisi)

Tasarım: `docs/superpowers/specs/luna-v5-adaptive-carry.md`. **Yalnızca paper** (`carry.risk.live_enabled: false`,
PaperBroker). Sürekli çalışan, olay güdümlü servis: collector'ın `var/forward/raw` dosyalarını salt okunur okur (yeni
`fut_tickers` snapshot'ı geldikçe ya da en geç 60 sn'de bir değerlendirir), kendi veritabanı `var/luna_carry.db`
(tek yazar). Cron / Task Scheduler ile tetiklenmez. Frozen V4 deneyinden bağımsızdır.

- **Başlatma:** `docker compose up -d --build bot-carry` (collector çalışıyor olmalı). Docker'sız:
  `uv run luna carry` (tek değerlendirme: `uv run luna carry --once`). Loglar: `docker compose logs -f bot-carry`.
  Aynı hesap için ikinci süreç lock yüzünden exit 1 alır; art arda 10 değerlendirme hatası exit 3 → Docker yeniden başlatır.
- **Health:** `docker compose exec bot-carry uv run luna carry-health`. Exit 0: son değerlendirme < 5 dk önce ve
  < 10 dk yaşında bir collector snapshot'ıyla yapılmış. Collector durursa bot emir göndermez ve health düşer.
- **Dashboard:** sol menüden **Adaptive Carry**: P&L ayrışımı (funding, fiyat, ücret + spread, net, turnover), mevcut
  long/short bacaklar, son değerlendirmede tartılan her alternatif ve neden HOLD/SWITCH olduğu (USD, switch horizon
  boyunca), FL30 sıralı evren ve funding oranları, karar günlüğü, equity eğrisi.
- **Kill / pause:** paylaşılan bayraklar: `luna_carry.db` `state` tablosunda `kill_switch` / `paused` (bot her
  değerlendirmede okur). Kill → bütün bacaklar kapatılır, yeni giriş yok. R4 drawdown ve R18 uyuşmazlığı bu
  bayrakları botun kendisi de kaldırır.
- **Restart:** state her emirden önce ve her değerlendirmeden sonra kaydedilir. Yeniden başlatmada kayıtlı ama
  işlenmemiş fill'ler tam bir kez işlenir, açık kalan emirler iptal edilir, holdings = bacaklar = fill'lerin neti
  kontrol edilir (uyuşmazlık → `paused` + risk olayı). Mevcut bacaklar kapatılıp yeniden açılmaz.
- **Parametreler:** `config/default.yaml` → `carry:` (env: `LUNA_CARRY__<ALAN>`). Başlangıç değerleridir, hiçbir
  backtest sonucuna göre ayarlanmadı. Değişiklik → `docker compose up -d bot-carry`.
