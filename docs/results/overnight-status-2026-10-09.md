# Overnight status: 2026-10-09

_Son güncelleme 2026-10-09 07:50 UK (agent). Deadline 10:00 UK; bütün aşamalar 07:45'te bitti, zamanlanmış görev erken kapatıldı._

- **Final aşama:** DONE. V2 CLOSED — NO CANDIDATE → yerel merge → V3 spec frozen → implement → test/review → V3 koşuları → V3 raporu.
- **holdout touched: NO.** 2026-07-01 sonrası hiçbir bar okunmadı; `var/holdout_registry.json` yok; `luna holdout` hiç çalışmadı.
- **live trading: NO.** Yalnızca research; canlı broker yok; paper bot'lar bu gece çalışmadı.
- **Son başarılı işlem:** 2026-10-09 07:41:56 UK: v3_final_tests succeeded and verified. Ardından status ve commit (07:50).
- **Checked-out branch:** `feat/research-v3`. Hiçbir şey push edilmedi.

## V2 final

| Hesap | Durum | Sonuç |
|---|---|---|
| Spot | succeeded (`var/reports/research_v2_spot_20261008T222035`) | **INSUFFICIENT DATA**, aday: aday yok. İşlem 0, ort. R n/a (%90 CI [n/a, n/a]), PF n/a, net +0.00 $, max DD 0.00%, en kötü gün 0.00%, pozitif pencere 0/11, 2× maliyet (edge) n/a R. Naive momentum: 158 işlem, +0.98 R. |
| Perp | succeeded (`var/reports/research_v2_perp_20261009T000905`) | **INSUFFICIENT DATA**, aday: aday yok. İşlem 27, ort. R +0.27 (%90 CI [-0.37, +0.96]), PF 1.44, net +14.11 $, max DD 14.95%, en kötü gün -7.14%, pozitif pencere 2/11, 2× maliyet (edge) +0.19 R. Naive momentum: 287 işlem, +1.44 R. |

- **Tekrarlanabilirlik:**
  - Spot sonucu, eski PC'deki final koşuyla birebir aynı.
  - Perp'in ana metrikleri, `d395c66` koşusuyla aynı.
- **Testler:**
  - `feat/research-v2`: 672 passed, 3 deselected.
  - Merge sonrası `feat/luna-core`: 672 passed, 3 deselected.
- **Review:** Bağımsız closure review "ready with fixes" verdi. Integrity temiz; bulgular yalnızca dokümanla ilgiliydi ve `98e7fb5`'te düzeltildi.
- **V2 closure:** **CLOSED — NO CANDIDATE.**
  - `docs/results/research-v2.md` (yalnızca final koşular) ve `docs/results/research-v2-closure.md`.
  - Commit'ler: `a38f243`, `98e7fb5`, `1f27a78`.
- **Merge:**
  - Yerel `feat/luna-core` (`origin/feat/luna-core` `b70f3db`'den), `--no-ff` merge `8204dda`. Çakışma yok.
  - Closure notu `f353edd`. Push yok.

## Retry'lar ve kesintiler

- **Controller'ın otomatik retry'ı:** 0. Controller'ın izlediği bütün işler ilk denemede başarılı oldu.
- **Controller kurulmadan önceki kesintiler (spot V2):**
  - 00:15: Claude Code'un düşük bellek koruması koşuyu durdurdu (kaynak). `--resume`, `--jobs 1` ile devam edildi.
  - 00:40: `--jobs 2`'ye geçmek için elle durduruldu ve tekrar `--resume` edildi.
  - Hiçbir checkpoint kaybolmadı; manifest kontrolü geçti.
- **Spot backfill:**
  - İlk deneme (Trades gap-fill), checkpoint yazarken `PermissionError` ile düştü.
  - Yerine yalnızca arşivden okuyan bir backfill kullanıldı; V2 bu veriyi zaten 2026-07-01'de kesiyor.
- **Controller hataları (03:42–03:43, iki tick):**
  - İş isteği JSON'unda ters eğik çizgi kaçışı hatalıydı.
  - `Git` adlı yardımcı fonksiyon kendi kendini çağırıyordu (sonsuz özyineleme).
  - İkisi de düzeltildi (`Invoke-Git`); hiçbir işi etkilemedi.

| İş | Tür | Durum | Exit | Başlangıç | Bitiş |
|---|---|---|---|---|---|
| spot_v2 | research_v2 | succeeded | 0 |  | 2026-10-09 01:24:04 |
| perp_v2 | research_v2 | succeeded | 0 | 2026-10-09 01:09:02 | 2026-10-09 03:40:22 |
| v2_tests | tests | succeeded | 0 | 2026-10-09 03:43:38 | 2026-10-09 04:00:54 |
| v2_merge_tests | tests | succeeded | 0 | 2026-10-09 04:00:55 | 2026-10-09 04:16:32 |
| v3_tests | tests | succeeded | 0 | 2026-10-09 04:16:32 | 2026-10-09 04:25:57 |
| v3_spot | research_v3 | succeeded | 0 | 2026-10-09 04:25:57 | 2026-10-09 05:30:55 |
| v3_perp | research_v3 | succeeded | 0 | 2026-10-09 05:30:56 | 2026-10-09 07:33:43 |
| v3_final_tests | tests | succeeded | 0 | 2026-10-09 07:34:34 | 2026-10-09 07:41:56 |

## V3

- **V3'e geçildi mi:** **EVET** (04:17). Sebep: V2'nin final sonucu iki hesapta da "aday yok"; talimat bu durumda V3'e geçilmesini istiyor.
- **Seçilen alpha aileleri:**
  - A: perp–spot primi cross-section (crowding/funding proxy'si).
  - B: low-volatility cross-section.
  - C: vol-managed spot exposure.
- **Ertelenenler:** Funding (veri yalnızca 2025-10-06'dan beri), basis carry, event anomalileri.
- **Reddedilenler:** Spread/maker, order book, rejim-koşullu momentum.
- **Gerekçeler:** `docs/results/v1-v2-failure-analysis.md`.
- **Spec:**
  - `docs/superpowers/specs/luna-research-v3-design.md`, commit `babbc59`, tag **`research-v3-spec-frozen`**.
  - Sonuç öncesi açıklama: `luna-research-v3-clarification-1.md`.
  - Plan: `docs/superpowers/plans/luna-research-v3-plan.md`.
- **Implement edilen task'lar:** Plan task 1–6, TDD ile (`498d32d` … `9c70bbb`, `a4760e1`).
  - V2'ye yalnızca varsayılan davranışı koruyan kancalar eklendi ve golden testlerle sabitlendi; V1/V2 davranışı değişmedi.
  - Yeni kod `luna/v3/` altında; `luna research-v3` CLI'ı eklendi.
- **Test ve review:**
  - Final tam suite **724 passed, 3 deselected** (`9209f26`).
  - Freeze öncesi review "ready with fixes" verdi; bulgular freeze'den önce düzeltildi.
  - Freeze sonrası review "ready to run" verdi.
- **V3 koşuları:** İkisi de **bitti**, controller üzerinden: spot 04:26–05:30, perp 05:31–07:33. Çıktılar SHA256 ile checkpoint'lendi.
- **V3 sonuçları** (keşif niteliğinde; aynı development setinin yeniden kullanımı, generalization kanıtı değil):
  - **Perp:** **NO-GO**, aday: aday yok. İşlem 32, ort. R -0.16 (%90 CI [-0.33, -0.01]), PF 0.61, net -20.08 $, max DD 7.89%, en kötü gün -3.23%, pozitif pencere 0/11, 2× maliyet (edge) -0.16 R. Naive momentum: 287 işlem, +1.44 R. Günlük Sharpe -0.89 vs null +0.43 (5/5 seed işlem yaptı).
  - **Spot:** **NO-GO**, aday: aday yok. İşlem 97, ort. R -0.02 (%90 CI [-0.06, +0.02]), PF 0.72, net -7.47 $, max DD 5.69%, en kötü gün -1.68%, pozitif pencere 0/11, 2× maliyet (edge) -0.02 R. Naive momentum: 158 işlem, +0.98 R. Günlük Sharpe -0.44 vs null -1.80 (5/5 seed işlem yaptı).
  - Belge: `docs/results/research-v3.md`.

## Çözülmemiş konular

- Nothing pushed: feat/research-v2 (a38f243, 98e7fb5, 1f27a78), local feat/luna-core (merge 8204dda + f353edd) and feat/research-v3 (+ tag research-v3-spec-frozen) exist only locally — push is your decision
- Paper bots not running on this PC (Docker not started); old PC paper DBs (luna_perp.db / luna_spot.db) not transferred, so the 8-week paper clock restarts here
- Spot bars after 2026-07-01 not downloaded (archive-only backfill for V2/V3); needed only for paper bots: run `uv run luna backfill --venue spot` (Trades gap-fill takes hours; a partial ADA gap-fill is parked in var/raw/gapfill_hold)
- Operational: spot Trades gap-fill checkpoint write can raise PermissionError on Windows (os.replace while the file is read)
- V3 HTML reports still carry some V2 captions (authoritative text: summary.json v3 block and check details)

## Ayrıntılar

- Controller log'u: `var/overnight/overnight.log`.
- Durum dosyaları: `var/overnight/state.json`, `var/overnight/agent.json`.
- Checkpoint'ler: `var/overnight/checkpoints/`.
