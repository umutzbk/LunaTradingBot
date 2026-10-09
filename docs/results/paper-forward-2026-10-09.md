# Paper trading: yeni forward dönem (2026-10-09)

**Bu, YENİ bir forward-paper dönemidir.** Eski PC'nin paper veritabanları (`var/luna_perp.db`, `var/luna_spot.db`) bu PC'ye taşınamadı; o makinenin `var/` klasörü (9 GB) aktarılamadı. Bu yüzden:

- Eski dönemin 8 haftalık paper sayacı burada **devam etmez** ve bu dönemle **birleştirilmez**.
- Eski dönemin metrikleri (equity, işlemler, aday etiketleri) bu dönemin metriklerine eklenmez. Karşılaştırma yapılacaksa iki dönem ayrı raporlanır.
- Yeni dönemin sayacı aşağıdaki başlangıç zamanlarından itibaren sıfırdan işler.

| Hesap | DB | Kod | Başlangıç equity | İlk tick (UTC) | Durum |
|---|---|---|---|---|---|
| perp | `var/luna_perp.db` (yeni) | `feat/luna-core` @ f353edd (Docker imajı 2026-10-09) | $265.00 | 2026-10-09 09:00:20 | Çalışıyor, healthy |
| spot | `var/luna_spot.db` (yeni) | aynı | $265.00 | spot gap-fill sonrası (§ Spot) | Bekliyor |

## Ortam

- Paper only: `live_enabled: false`, kodda canlı broker yok.
- Docker Desktop 29.2.0, `docker compose`, servisler `bot-perp`, `bot-spot`, `dashboard` (http://localhost:8501).
- Her hesap ayrı bir SQLite DB kullanır ve kendi DB'sinin tek yazarıdır. Dashboard DB'leri salt okunur açar.
- Ücretler SC-2: spot 0.40/0.80, perp 0.02/0.05.

## Veri

- **Perp barları ve funding:** 2026-10-08'de backfill edildi; bot açılışta eksik barları kendisi tamamlıyor.
- **Spot barları:**
  - 2026-06-30'a kadar Kraken OHLCVT 2026Q2 arşivinden geliyor.
  - 2026-07-01 → bugün aralığı `/Trades` gap-fill ile dolduruluyor. Başlangıç 2026-10-09 09:55 (yerel saat).
  - ADA'nın önceki yarım gap-fill'i (`var/raw/gapfill_hold`) checkpoint'ten devam ettirildi.
  - Spot bot, gap-fill bitmeden başlatılmaz (RUNBOOK §7).

## Doğrulama (perp)

- **Başlangıç:**
  - `live model fitted: train 2026-04-09..2026-10-09`.
  - `engine started (paper); equity 265.00, 0 open positions`.
  - `model has no accepted expectancy cell` uyarısı: RUNBOOK §5'e göre beklenen durum, arıza değil. Bot işlem açmaz ve adayları `reject_reason = ranker` ile kaydeder.
- **Health:** `luna health --account perp` → `ok`; ilk equity satırı 09:00:20 UTC.
- **Restart:** `docker compose restart bot-perp` sonrası:
  - `live model loaded from var/models/live_perp.pkl`;
  - state DB'den yüklendi;
  - Docker health `healthy`, restart sayısı 0.
- **Dashboard:** `/_stcore/health` → `ok`.

## Research ile ilişkisi

- Paper bot'lar V2/V3 araştırma koşularından ve V4 toplayıcısından ayrıdır.
- Paper sonuçları research GO/NO-GO kriterlerinin yerine geçmez.
- Holdout'a dokunulmadı.
