# V5 backtest implementation: outcome-ready record (2026-10-11)

> Yalnızca implementasyon ve doğrulama. Hiçbir tarihsel V5 outcome'u (W/D/C/E, variant, sensitivity, köprü adımları, I) gerçek veride koşulmadı. Bütün testler sentetik veya uydurma veriyle çalışır. Gerçek veride koşan tek şey §5 W parity'sidir (zaten yayımlanmış V4 sonucunun yeniden üretimi).

## Frozen girdiler

| Kalem | Değer |
|---|---|
| Protokol | rev 3, tag `v5-backtest-protocol-frozen` (protokol commit'i `8285c31`, freeze kaydı commit'i `af96d11`) |
| Protokol sha256 | `4bd90f5a169025e0483d13c636964a937d01f10fce3355ced1c81058dfcaba54` |
| Frozen girdi manifest'i sha256 | `eecdc6a21626ccd5e2677cce2503f304a5730845de552554281a6ea54cc53f14` |

## Bileşenler

| Modül | İçerik |
|---|---|
| `luna/carry/history.py` | §3.1 builder: mark mid, trade fallback, 5 dakikalık karar ve fill pencereleri (T+5m; S-PIT10 için T+10m), hacim, sonsuz derinlik. Point-in-time funding görünümü (`FundingPIT`). S-SPREAD-FIX. S-FB-CONS lookback p90. Holdout guard. Gerçek veride yetki şartı. |
| `luna/carry/backtest.py` | Gecikmeli executor (t+15m, bant yok). Zamanlanmış rebalance izni (4 sayılan deneme; bloklu döngüler sayılmaz). Arm, variant ve satır tanımları. Fill zaman damgasına göre günlük muhasebe. Fallback exposure ve FALLBACK-AFFECTED. I referansı. S-UNWIND. Teleskopik attribution. Switch ledger. `evaluate` (kill kuralı, PIT-SENSITIVE, birleştirme, ikincil Holm, Δnet CI, post hoc MDE). `run_study`. Gerçek veri yükleyicisi. |
| `luna/carry/stats.py` | V4 ile birebir moving block bootstrap (seed 20261010, b=28/14), risk metrikleri, G1–G6, satır hükmü |
| `luna/carry/verdict.py` | §10 birleştirme (8 kombinasyonluk matris) |
| `luna/carry/parity.py` | §5.5 köprü adımları 1–4, ileri ve ters sıra, ikisi de W'de biter |
| `luna/carry/authz.py`, `outcome_guard.py` | Tek outcome koşusunun guard'ı (§12): tag'in HEAD'in atası olması, temiz ağaç, protokol diff'i ve sha'sı, pinned manifest, commit edilmiş config ve frozen DEFAULT değerleri, açık onay cümlesi, sabit konumlu ve exclusive iki marker. Kontrol edilen repo, çalışan checkout'tur. |
| `luna/carry/report.py` | `summary.json`, `manifest.json` (summary ve bütün tabloların sha'ları), `report.md` (beyan edilen yorumlar), arm × satır tabloları |
| `luna/cli.py` `v5-backtest` | Bütün guard'lar geçmeden reddeder |
| `luna/carry/core.py` | Yalnız bilgi amaçlı `CycleResult.equity_start` alanı eklendi; davranış aynı |

## Outcome öncesi zorunlu kapı: fallback attribution regression

**Test:** `tests/carry/test_backtest_study.py::test_hard_gate_fallback_attribution_on_the_replay_path`
- **Akış:** Gerçek replay yolu: `run_study` → `CarryCore.run_cycle` → `DelayedExecutor` fill günlüğü → `fallback_exposure` → `evaluate`.
- **Kurgu:**
  - A'nın karar penceresi boş: yalnız karar fallback'i. A ayrıca farklı fiyatlı olduğu için qty ağırlığı ile notional ağırlığı birbirinden ayrılır.
  - C'nin funding'i sıçrar ve fill penceresi boştur: yalnız fill fallback'i.
- **Doğrulananlar:**
  - arm, varlık ve günün saati bazında kırılım;
  - karar ile fill fallback'inin ayrı tutulması;
  - notional ağırlığı;
  - FALLBACK-AFFECTED'in tetiklenmesi ve hükme S-FB-CONS satırının eklenmesi.
- **Mutasyon kontrolü:** Bayrakların karıştırılması, qty ağırlığı, eşik değişikliği, yanlış arm'ın fill'leri ve fallback bayrağının silinmesi: hepsi kırmızı. Eşik birim testi `>`, `≥` ve abs mutasyonlarını yakalıyor.

## S-PIT10 ve S-FB-CONS
- **S-PIT10:** Aynı replay hattında yalnız `known_after` değişiyor (`RowSpec` alan farkı testli). Testte her saatin :55 snapshot'ı 400 bps: primary T+5m'de 00:00 girişi engelleniyor, T+10m'de giriliyor.
- **S-FB-CONS:**
  - Fallback'ler lookback'in [2025-10-06, 2025-11-10) varlık başına p90'ı ile fiyatlanıyor. Testte A'nın p90'ı 30 bps: primary 10 bps ile girer, S-FB-CONS gate'i geçmez.
  - p90 yalnız lookback penceresinden ve geçerli değerlerden hesaplanıyor (testli).

## Review
- **Bağımsız code review:** İlk turda 1 Critical (gerçek veride yetkisiz bir yol), 3 Major (bloklu döngünün zamanlanmış izni tüketmesi, config'in env ile değişebilmesi, guard'ın çalışan checkout'u kontrol etmemesi) ve 6 Minor. Doğrulama turu: **Critical 0, Major 0.**
- **Metodolojik review (implementasyon ↔ frozen spec):** İlk turda 2 Major (N5 izin tüketimi; tek koşunun tablolarının ve bazı §6 sayaçlarının eksikliği) ve 12 Minor/yorum. Doğrulama turu: **CONFORMS WITH DECLARED INTERPRETATIONS, Critical 0, Major 0.** Kalan 2 Minor da sonradan kapatıldı (beyan ifadesi; b=14 sabitlemesi).
- Beyan edilen yorumlar `report.DECLARATIONS` içinde ve her `report.md`'ye yazılıyor.

## Bilinen sınırlar (beyan)
- Yetki token'ı kod içinde kazara koşuya karşı bir guard'dır; güvenlik sınırı değildir.
- Gerçek outcome koşusunun tahmini süresi yaklaşık 2 saat (39 arm × satır koşusu; sentetik 20 varlıklı dry-run ile ölçüldü).
