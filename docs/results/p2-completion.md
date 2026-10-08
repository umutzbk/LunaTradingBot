# Luna: P0–P2 acceptance durumu (2026-10-08 08:00 TR)

> **Karar: P2 henüz COMPLETE değil. Tek engel spot tarafı.** Perp tarafı, kod, testler ve final review temiz. Spot'u bekleyen işler §6'da.

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
- **Tam test suite (HEAD `0843b43`):** 525 geçti, 3 hariç (ağ testleri), ruff temiz.
- **Final branch incelemesi:** "düzeltmelerle merge'e hazır". **İki önemli bulgunun ikisi de** (spot fiyat güncellemesi, paylaşılan SQLite) ve 8 küçük bulgu düzeltildi. Düzeltmeler ayrıca kontrol edildi: **10/10 kapandı, yeni kritik ya da önemli sorun yok.**
- **Sonradan eklenen düzeltmeler de incelemeden geçti:** ağ kesintisinde kendini toparlama, spot arşivinde checksum sabitleme, model kabul etmese de aday kaydı.
- **Holdout'a dokunulmadı:** `var/holdout_registry.json` yok.

## 3. Perp veritabanı taşıma
Tablo bazında satır sayıları korundu: candidates 11, risk_events 16, runs 3; orders, positions ve fills 0.
- **Equity +2:** "önce" sayımı ile bot'un durdurulması arasında 2 tick daha yazıldı.
- **State 7 → 6:** temiz kapanışta kilit satırı bırakıldı (beklenen). Diğer bütün state anahtarları korundu.
- Yeniden başlatma sonrası kilit yeniden alındı, model ve state yüklendi, tick yazıldı, emir tekrarı yok.

## 4. Paper durumu
| | Perp | Spot |
|---|---|---|
| Container | `bot-perp` **healthy**, 11 saattir kesintisiz | henüz başlatılmadı (spot verisini bekliyor) |
| Equity | $265.00 (72 tick) | — |
| Emir / fill / pozisyon | 0 / 0 / 0 | — |
| Aday | 12 (hepsi `rejected / ranker`) | — |
| Risk olayları | `no_model` 6, `feed_error` 33, `feed_dead_exit` 2 (host ağ kesintileri) | — |

Dashboard: http://localhost:8501, HTTP 200.

## 5. Araştırma sonucu (değişmedi)
- **Perp walk-forward:** INSUFFICIENT DATA, 24 işlem, −0.12R (`docs/results/walkforward.md`).
- **Naive momentum'daki +300R'lik XRP işlemi gerçek,** bug değil (`docs/results/xrp-outlier-investigation.md`).
- Research V2 spec'i **onaylandı ve donduruldu.** Kod yazılmadı.

## 6. P2'yi engelleyenler (yalnızca spot)
- [ ] **Spot backfill:** 19 varlık tamam (XLM ve XRP dahil). ZEC iniyor, DOGE (XDGUSD) sırada; bitişi 08:00'den sonra.
- [ ] **`bot-spot`:** indirme bitince otomatik başlayacak (`var/spot_autostart.ps1`). Healthy olduğu, tick yazdığı, `luna health --account spot` 0 döndüğü ve emir tekrarı olmadığı doğrulanacak.
- [ ] **Spot walk-forward** (`docs/results/walkforward-spot.md`).

Bu üçü tamamlanınca P2 = COMPLETE.

## 7. Bilinen açık konular (P2'yi engellemiyor)
- **Host ağı kararsız:** kurumsal proxy/DNS kesintileri, aralıklı SSL EOF hataları. Bot bunları güvenle karşılıyor (feed_error → pause → feed_dead_exit). Kesintisiz çalışma için **uyku modunu kapalı tut** (prizdeyken kapalı, pilde 10 dakika) **veya sistemi VPS'e taşı** (RUNBOOK).
- **`luna label` henüz çalıştırılmadı:** counterfactual sonuçlar 0. Günlük çalıştırılması önerilir (RUNBOOK).
- Ertelenen küçük bulgular ledger'da. Final review hiçbirini merge öncesi gerekli görmedi. **Canlı broker'a (P3) geçmeden önce** limit fiyatların `price_tick`'e yuvarlanması gerekiyor.

## 8. Takip (RUNBOOK özeti)
- `docker compose ps`: bot'lar `healthy` olmalı.
- `docker compose exec bot-perp uv run luna health --account perp`: 0 ise son 20 dakikada tick var.
- `docker compose logs -f --tail 100 bot-perp`
- Dashboard → Overview / Positions / Scanner / Risk / Research / Dataset; hesap seçici her hesabın kendi DB'sini açar.
- Telegram (isteğe bağlı): `.env` içine `LUNA_TELEGRAM_TOKEN` ve `LUNA_TELEGRAM_CHAT_ID`; komutlar `/status`, `/kill perp|spot|all`.
