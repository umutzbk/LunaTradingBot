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

Hepsi `./var` klasörünü `/app/var` olarak paylaşır: `var/luna.db` (SQLite), `var/bars/`, `var/funding/`,
`var/models/live_<account>.pkl` (canlı model cache'i), `var/reports/`. Secrets `.env` dosyasındadır (git'e girmez).

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

Yeniden başlatma güvenlidir: engine lock'u yeniden alır, pozisyonları/risk state'ini `var/luna.db`'den yükler,
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

`luna health --account X [--max-age-min 20] [--db PATH]`: hesabın son `equity` satırı (her başarılı 15 dk'lık
tick'te yazılır) 20 dakikadan gençse 0, değilse 1 döner (DB yoksa / satır yoksa da 1). Salt okunurdur.

Compose healthcheck: `interval 60s`, `timeout 20s`, `retries 3`, `start_period 30m`. 30 dakika, soğuk
başlangıçtaki model fit'i + ilk 15 dakikalık sınıra kadar bekleme süresini kapsar; bu süre içindeki
başarısızlıklar sayılmaz, ilk başarı konteyneri hemen `healthy` yapar. Tek bir tick Kraken hatası yüzünden
atlanırsa konteyner birkaç dakika `unhealthy` görünebilir; bir sonraki başarılı tick'te düzelir. Docker
`unhealthy` konteyneri kendiliğinden yeniden başlatmaz — bu bir uyarıdır, logları inceleyin.

### healthchecks.io (dış izleme, önerilir)

1. https://healthchecks.io'da bir check oluşturun (period 1 dk, grace 10 dk). Her hesap için ayrı check önerilir.
2. Ping URL'sini `.env`'e yazın: `LUNA_HEALTHCHECK_URL=https://hc-ping.com/<uuid>`
3. `docker compose up -d` (env değişikliği konteyneri yeniden oluşturur).

Engine her dakikalık heartbeat'te bu URL'e GET atar; makine uyursa / internet kesilirse / süreç ölürse
healthchecks.io size e-posta/Telegram ile haber verir. `.env` tüm servislere aynı değeri verir; perp ve spot için
ayrı URL istenirse servis bazında `environment:` ile `LUNA_HEALTHCHECK_URL` override edilebilir.

## 5. Dashboard

http://localhost:8501 — sol menüden hesap (`perp`/`spot`) ve sayfa seçilir.

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

Dashboard'un kimlik doğrulaması yoktur; bu yüzden port yalnızca `127.0.0.1`'e açılır. VPS'te
`ssh -L 8501:localhost:8501 user@vps` ile tünelleyip http://localhost:8501 açın; portu internete açmayın.

### "no accepted cells → no trades" ne demek?

Engine başlarken ranker'ı son 6 aylık veriyle fit eder. Hiçbir (strateji × rejim) hücresi expectancy kapısını
geçmezse logda `model has no accepted expectancy cell: managing positions, opening nothing` ve `risk_events`'te
`no_model` görülür: bot **yeni pozisyon açmaz**, yalnızca varsa açık pozisyonları yönetir, equity'yi yazar ve
heartbeat atar. Bu durumda aday satırı da yazılmaz (Scanner boş kalır). Bu bir arıza değil, mevcut walk-forward
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
| `/pause` | yeni girişleri durdur (açık pozisyonlar yönetilmeye devam eder) |
| `/resume` | pause'u kaldır |
| `/kill` | **kill switch**: yeni giriş yok, tüm pozisyonlar kapatılır; yeniden başlatmada da açık kalır |

Her iki bot aynı token'ı kullanırsa komutu ilk okuyan bot alır; perp ve spot'u birlikte çalıştırıyorsanız iki ayrı
bot (iki token) kullanın ve servis bazında `environment:` ile override edin.

## 7. bot-spot'u başlatma (spot backfill bittikten sonra)

Spot backfill host üzerinde `var/bars/kraken_spot` dosyalarını yazarken bot-spot başlatılmamalıdır.

```bash
tail var/backfill_spot.log                       # bitti mi?
docker compose run --rm bot-spot backfill --venue spot   # (gerekirse) boşlukları konteynerden doldur
docker compose up -d bot-spot
docker compose logs -f bot-spot                  # model fit + "engine started (paper)"
docker compose ps                                # ~30 dk içinde (healthy)
```

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
3. Kopyalayın: repo (git clone veya rsync), **`var/`** (özellikle `var/luna.db`, `var/bars`, `var/funding`,
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
