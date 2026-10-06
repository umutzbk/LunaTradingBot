# Luna: P0–P2 tamamlanma özeti (2026-10-06)

> **Durum:** Planın 28 görevinin hepsi tamamlandı ve her biri bağımsız incelemeden geçti. **Tüm branch'in final incelemesi şu an sürüyor.** Bulgu çıkarsa düzeltilecek ve bu belge güncellenecek.

## 1. Neler yapıldı

| Aşama | İçerik | Kanıt |
|---|---|---|
| P0 veri | 21 Kraken perp (2023'ten bugüne, 15m) + 12 aylık funding; resmi spot OHLCVT arşivi + trades gap-fill; doğrulama raporu | `var/bars/*`, `luna data-report` |
| P0.5 çekirdek | Portföy muhasebesi, 19 kurallı risk motoru, planner, kötümser SimBroker, backtest motoru, naive baseline'lar | `docs/results/baseline.md` |
| P1 strateji/araştırma | Rejim, momentum / mean reversion / pairs, engine'e birebir eşdeğer işlem listeleri, bootstrap + shrinkage ranker, FDR'li pair seçimi | eşdeğerlik testleri |
| P1.5 doğrulama | Walk-forward (sızıntı korumalı, tek seferlik holdout kaydı), GO/NO-GO raporu, ablation, duyarlılık | `docs/results/walkforward.md` |
| P2 paper | SQLite + process kilidi, canlı public feed + PaperBroker, live engine (backtest ile replay eşdeğer), Telegram, heartbeat, counterfactual etiketleme, Streamlit dashboard, Docker + runbook | aşağıdaki canlı kanıt |

- **Testler:** 499 geçiyor (3 ağ testi varsayılan olarak hariç), ruff temiz.
- **Commit sayısı:** 57, `feat/luna-core` branch'inde.

İnceleme sürecinde yakalanıp düzeltilen kritik hatalardan bazıları:
- **Kill-switch tetiklenmiyordu.** numpy bool tipi yüzünden %15 drawdown kill-switch'i hiç devreye girmiyordu.
- **"close" etiketiyle pozisyon açılabiliyordu,** kill-switch aktifken bile.
- **Acil stop ve `/kill` çıkışları sessizce düşebiliyordu.** Aynı dakika içindeki ikinci çıkış denemesi aynı emir kimliğini aldığı için gönderilmiyordu.
- **Pairs işleminde tek bacak açık kalabiliyordu.**
- **Backfill yarıda kesilirse veri kaybediliyordu.**
- **Pencereler arasında kill-switch sıfırlanıyordu.**

## 2. Araştırma sonucu (değişmedi)
- **Perp walk-forward:** INSUFFICIENT DATA (24 işlem, −0.12R). Ranker kanıtlanmış edge bulamıyor.
- **Naive momentum** +1.44R gösteriyor, ama bu tek bir XRP işlemine (+300R) dayanıyor. O işlemin gerçek olduğu, bug ya da sizing hatası olmadığı testlerle kanıtlandı (`docs/results/xrp-outlier-investigation.md`).
- **Holdout'a dokunulmadı:** `var/holdout_registry.json` yok.
- Strateji parametreleri değiştirilmedi. Research V2 yalnızca taslak olarak hazır.

## 3. Canlı paper sistemi: kanıt (2026-10-06 17:54 UTC)
- `bot-perp`: **Up, healthy.** Tick'ler :00/:15/:30/:45'te; 12 equity kaydı var, sonuncusu 17:45 UTC. Equity $265.00.
- **Emir sayısı 0.** Beklenen durum bu: model kabul edilmiş hiçbir expectancy hücresi bulamadığı için engine yeni pozisyon açmıyor ("no_model" risk olayı).
- **Aday kayıtları:** düzeltme devrede. Sinyal oluşan ilk barda adaylar "rejected / ranker" olarak yazılacak. Sinyaller barların ~%7'sinde oluşuyor, yani ilk kaydın birkaç saat içinde görünmesi beklenir.
- **Yeniden başlatma testi:** kilit yeniden alındı, durum yüklendi, hiçbir emir iki kez gönderilmedi.
- **Dashboard:** http://localhost:8501 adresinde HTTP 200.
- `bot-spot`: image hazır ve tek tick'lik smoke testi geçti. Spot verisinin kalan 8 varlığı indirildikten sonra başlatılacak.

## 4. Paper sistemini nasıl takip edersin

Ayrıntılar `docs/RUNBOOK.md`'de.

| Ne | Nasıl |
|---|---|
| Durum | `docker compose ps` (healthy olmalı) |
| Sağlık | `docker compose exec bot-perp uv run luna health --account perp` (0 = son 20 dakikada tick var) |
| Loglar | `docker compose logs -f --tail 100 bot-perp` |
| Dashboard | http://localhost:8501: Overview (equity, kill-switch, kilit yaşı), Positions, Scanner (adaylar + ret nedenleri), Risk, Research (raporlar), Dataset (etiketli adaylar) |
| Telegram (opsiyonel) | `.env` içine `LUNA_TELEGRAM_TOKEN` ve `LUNA_TELEGRAM_CHAT_ID`; komutlar `/status`, `/positions`, `/pause`, `/resume` (⚠️ kill-switch'i de kaldırır ve drawdown referansını sıfırlar), `/kill` |
| Dışarıdan izleme (opsiyonel) | healthchecks.io URL'ini `.env` içine `LUNA_HEALTHCHECK_URL` olarak ekle |
| Günlük etiketleme | `docker compose run --rm bot-perp label --account perp` |
| Durdur / başlat | `docker compose stop bot-perp` / `docker compose up -d bot-perp dashboard` |
| Önemli | Windows uyku modunu kapat. Uzun süreli çalışma için VPS'e taşı (runbook'ta adımlar var). |

**Neye bakmalısın:**
1. Haftada bir Scanner ve Dataset sayfaları: kaç aday çıktı, counterfactual sonuçlar ne diyor.
2. Risk sayfası: `feed_error`, `lock_lost` veya `engine_error` olayı varsa logları kontrol et.
3. "0 işlem" şu an beklenen durum. Ranker edge kanıtı biriktikçe (2 ayda bir yeniden eğitim) değişebilir.

## 5. Kalan işler
- [ ] Final branch incelemesi (sürüyor). Bulgular düzeltilecek.
- [ ] Spot verisinin kalan 8 varlığı, ardından `bot-spot` başlatılacak ve spot walk-forward koşulacak.
- [ ] Research V2 için onayın (taslak: `docs/superpowers/specs/2026-10-06-luna-research-v2-design.md`).
