# Research V2: closure summary

- **Durum:** **CLOSED — NO CANDIDATE** (2026-10-09)
- **Final kod:** `e0203c8` (kaynak kodu `1cc450a` ile aynı). Sonuç ve closure commit'leri: `a38f243`, `98e7fb5` (review düzeltmeleri) ve bu belgenin commit'i.
- **Testler:** **672 passed, 3 deselected** (tam suite, `uv run pytest -q`, 2026-10-09 03:43–03:49 UK, `feat/research-v2` @ `a38f243`; arada yalnızca doküman değişti).
- **Final perp sonucu:** INSUFFICIENT DATA, aday yok.
  - 27 işlem, ortalama +0.27 R, %90 CI [−0.37, +0.96], PF 1.44, net +$14.11.
  - Max DD %14.95, en kötü gün −%7.14, pozitif pencere 2/11; 2× maliyette +0.19 R.
  - Kırılgan: ex-top1 +0.02, ex-top3 −0.43.
  - Naive momentum: 287 işlem, +1.44 R.
  - Rapor: `var/reports/research_v2_perp_20261009T000905/`
- **Final spot sonucu:** INSUFFICIENT DATA, aday yok.
  - 0 işlem; SC-2 ücretleri altında hiçbir konfigürasyon train'de kabul edilmedi.
  - Naive momentum (long-only): 158 işlem, +0.98 R, CI [−0.52, +3.51].
  - Rapor: `var/reports/research_v2_spot_20261008T222035/`
- **Holdout touched: NO.** 2026-07-01 ve sonrasına ait hiçbir bar okunmadı. `var/holdout_registry.json` yok;
  `luna holdout` çalıştırılmadı.
- **Bilinen critical/major issue'lar:** **Critical: yok. Major: yok.**
  - V2 final review (`2026-10-08-research-v2-final-review.md`) ve closure review (`2026-10-08-research-v2-closure-review.md`) bulgularının hepsi kapatıldı. M9 bir kararla kabul edildi.
  - Bu gece yapılan closure review'ı (bağımsız, en yetenekli model) yalnızca doküman bulguları verdi; hepsi `98e7fb5`'te düzeltildi.
  - Açık minor/operasyonel notlar `research-v2.md` §"Açık bilinen issue'lar" bölümünde.
- **Final review:** Bağımsız closure review: "ready with fixes". Integrity temiz: manifest'ler `e0203c8`, `git_dirty=false`, `data_end` 2026-07-01; `git diff 1cc450a -- luna tests config` boş; holdout registry yok. "CLOSED — NO CANDIDATE" kararı dondurulmuş §3.5'e göre gerekçeli (perp n=27, spot n=0 < 30; perp ayrıca dört kontrolü daha geçemiyor). Düzeltmeler uygulandı.
- **Merge:** `feat/research-v2` → yerel `feat/luna-core` (`--no-ff`, push yok). Sonuç ve merge sonrası test sayısı aşağıda, merge commit'inden sonra eklenecek.
- **Ayrıntılar:** `docs/results/research-v2.md`, `docs/superpowers/ledgers/2026-10-08-research-v2-ledger.md`,
  `docs/results/v1-v2-failure-analysis.md` (V3 branch'inde).
