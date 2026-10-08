# Luna: P0–P2 acceptance durumu (2026-10-08 08:46 TR)

> **Karar: P2 COMPLETE.** Perp ve spot paper bot'ları Docker'da healthy çalışıyor, testler ve final review temiz, perp DB taşıması kayıpsız, spot backfill ve spot walk-forward tamam. Holdout'a dokunulmadı. Research V2 kodlamasına başlanmadı.

## 1. Mimari (final review düzeltmeleri sonrası)

| Konu | Durum |
|---|---|
| Veritabanı | **Hesap başına ayrı SQLite:** `var/luna_perp.db`, `var/luna_spot.db`. İki container aynı dosyaya yazmıyor. Dashboard yalnızca `mode=ro` ile okuyor. |
| Eski `var/luna.db` | `luna migrate-db --account perp` ile **taşındı** (2026-10-07 12:41 UTC). Eski dosya artık yok. |
| Telegram | `/kill [perp\|spot\|all]`, `/pause`, `/resume` hedeflenebilir; her bot yalnızca kendi hesabına ya da `all`'a tepki verir. `/resume` kill-switch'i de kaldırır ve drawdown referansını sıfırlar. |
| Ağ kesintisinde kendini toparlama | 8 tick üst üste veri alınamazsa engine `feed_dead_exit` kaydeder, kilidi bırakır ve çıkar; Docker yeniden başlatır. Bu, 2026-10-07 18:00 UTC'de gerçek ortamda tetiklendi ve çalıştı. |
| Spot post-only fiyat güncellemesi | Emir gönderildiği andaki referans mid ± risk bandı ile sınırlı. |
| Kraken spot arşivi | Doğrulanmış 2026Q2 parça hash'leri koda sabitlendi. Uzak SUMS listesi değişse de etkilenmiyor. |
| Aday kayıtları | Model hiçbir hücreyi kabul etmese de adaylar `rejected / ranker` olarak parametreleriyle kaydediliyor. |

## 2. Testler ve inceleme
- **Tam test suite:** 525 geçti, 3 hariç (ağ testleri), ruff temiz. Bu, `0843b43` sonrası yalnızca doküman değişikliği olan HEAD'de yeniden çalıştırıldı.
- **Final branch incelemesi:** "düzeltmelerle merge'e hazır". **İki önemli bulgunun ikisi de** (spot fiyat güncellemesi, paylaşılan SQLite) ve 8 küçük bulgu düzeltildi. Düzeltmeler ayrıca kontrol edildi: **10/10 kapandı, yeni kritik ya da önemli sorun yok.**
- **Sonradan eklenen düzeltmeler de incelemeden geçti:** ağ kesintisinde kendini toparlama, spot arşivinde checksum sabitleme, model kabul etmese de aday kaydı.
- **Holdout'a dokunulmadı:** `var/holdout_registry.json` yok.

## 3. Perp veritabanı taşıma
Tablo bazında satır sayıları korundu: candidates 11, risk_events 16, runs 3; orders, positions ve fills 0.
- **Equity +2:** "önce" sayımı ile bot'un durdurulması arasında 2 tick daha yazıldı.
- **State 7 → 6:** temiz kapanışta kilit satırı bırakıldı (beklenen). Diğer bütün state anahtarları korundu.
- Yeniden başlatma sonrası kilit yeniden alındı, model ve state yüklendi, tick yazıldı, emir tekrarı yok.

## 4. Paper durumu (2026-10-08 05:45 UTC)
| | Perp | Spot |
|---|---|---|
| Container | `bot-perp` **healthy**, 12 saattir kesintisiz | `bot-spot` **healthy** (2026-10-08 05:22 UTC'de başlatıldı) |
| `luna health` | 0 | 0 |
| Equity | $265.00 (76 tick) | $265.00 (2 tick) |
| Emir / fill / pozisyon | 0 / 0 / 0 | 0 / 0 / 0 |
| Aday | 12 (hepsi `rejected / ranker`) | 0 (henüz sinyal yok) |
| Model | kabul edilen hücre yok, yeni giriş yok | 9 varlıkla eğitildi (BTC, ETH, SOL, ZEC, XRP, HYPE, NEAR, DOGE, SUI); kabul edilen hücre yok, yeni giriş yok |
| Risk olayları | `no_model` 6, `feed_error` 33, `feed_dead_exit` 2 (host ağ kesintileri) | `no_model` 1 |

Dashboard: http://localhost:8501, HTTP 200.

**Spot başlatılırken yapılanlar:**
- `bot-spot` imajı final review düzeltmelerinden önce derlenmişti. Yeniden derlendi.
- Spot instrument cache'i eskiydi ve yalnızca 12 varlık içeriyordu. Yedeklendi (`var/backup/spot.json.bak`) ve yeniden çekildi (21 varlık).
- Eski cache ile eğitilmiş `live_spot.pkl` ve yalnızca birkaç dakikalık, emirsiz `luna_spot.db` silindi. Bot temiz state ile yeniden başlatıldı.

## 5. Araştırma sonucu (development walk-forward OOS, 2024-09 → 2026-07)
- **Perp:** INSUFFICIENT DATA, 24 işlem, −0.12R (`docs/results/walkforward.md`).
- **Spot:** INSUFFICIENT DATA, 0 işlem; ranker 11 pencerenin hiçbirinde hücre kabul etmedi. Rastgele seçim: −0.26R, −$140, DD %54. Naive momentum +1.39R, ama CI [−0.42, +4.48] ve DD %46 (`docs/results/walkforward-spot.md`).
- **Naive momentum'daki +300R'lik XRP işlemi gerçek,** bug değil (`docs/results/xrp-outlier-investigation.md`).
- Research V2 spec'i **onaylandı ve donduruldu.** Kod yazılmadı.

## 6. P2 kabul kriterleri
- [x] **Spot backfill:** 21 varlığın hepsi tamam (ZEC ve DOGE 2026-10-08 08:18 TR'de bitti). `luna data-report`: duplicate 0, monotonluk ihlali 0.
- [x] **`bot-spot`:** healthy, tick yazıyor, `luna health --account spot` 0 döndürüyor, emir tekrarı yok (0 emir).
- [x] **Spot walk-forward** (`docs/results/walkforward-spot.md`).
- [x] Perp: healthy, DB taşıma kayıpsız, emir tekrarı ya da state kaybı yok.
- [x] Tam test suite ve final review temiz; holdout'a dokunulmadı.

## 7. Bilinen açık konular (P2'yi engellemiyor)
- **Host ağı kararsız:** kurumsal proxy/DNS kesintileri, aralıklı SSL EOF hataları. Bot bunları güvenle karşılıyor (feed_error → pause → feed_dead_exit). Kesintisiz çalışma için **uyku modunu kapalı tut** (prizdeyken kapalı, pilde 10 dakika) **veya sistemi VPS'e taşı** (RUNBOOK).
- **Instrument cache yenileme politikası yok:** `var/instruments/{kind}.json` kendiliğinden yenilenmiyor. Spot'ta eski cache 9 varlığı sessizce düşürmüştü. Periyodik yenileme ya da yaş uyarısı eklenmeli.
- **Spot'ta işlemsiz barlar:** düşük likiditeli varlıklarda (örn. ZEC, MUBARAK, BTC'nin ilk yılları) çok sayıda boş 15 dakikalık bar var. Bunlar ileri doldurulur ve bu barlarda fill olmaz; rapordaki uyarılara bakın.
- **`luna label` henüz çalıştırılmadı:** counterfactual sonuçlar 0. Günlük çalıştırılması önerilir (RUNBOOK).
- Ertelenen küçük bulgular ledger'da. Final review hiçbirini merge öncesi gerekli görmedi. **Canlı broker'a (P3) geçmeden önce** limit fiyatların `price_tick`'e yuvarlanması gerekiyor.
- Her iki hesapta da model şu an hiçbir hücreyi kabul etmiyor, bu yüzden paper bot'lar işlem açmıyor. Bu, walk-forward sonucuyla tutarlı. V1 parametreleri değiştirilmedi (D8).

## 8. Takip (RUNBOOK özeti)
- `docker compose ps`: bot'lar `healthy` olmalı.
- `docker compose exec bot-perp uv run luna health --account perp` (ve `bot-spot … --account spot`): 0 ise son 20 dakikada tick var.
- `docker compose logs -f --tail 100 bot-perp`
- Dashboard → Overview / Positions / Scanner / Risk / Research / Dataset; hesap seçici her hesabın kendi DB'sini açar.
- Telegram (isteğe bağlı): `.env` içine `LUNA_TELEGRAM_TOKEN` ve `LUNA_TELEGRAM_CHAT_ID`; komutlar `/status`, `/kill perp|spot|all`.
