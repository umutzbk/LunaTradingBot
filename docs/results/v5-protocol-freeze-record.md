# V5 historical backtest protocol: freeze record

| Alan | Değer |
|---|---|
| Protokol dosyası | `docs/superpowers/specs/luna-v5-historical-backtest-protocol.md` (rev 3, FROZEN) |
| Protokol commit'i | `8285c31039a91ddcaa3a61b201f0f8d39195d292` |
| Annotated tag | `v5-backtest-protocol-frozen` (bu kaydı içeren commit'te; protokol dosyası `8285c31` ile birebir aynıdır) |
| Protokol SHA256 (commit edilmiş LF içerik) | `4bd90f5a169025e0483d13c636964a937d01f10fce3355ced1c81058dfcaba54` |
| Frozen girdi manifest'i SHA256 | `eecdc6a21626ccd5e2677cce2503f304a5730845de552554281a6ea54cc53f14` (`var/history_v5/frozen/manifest.json`, 161 dosya) |
| Stage 0 audit | `docs/results/v5-stage0-audit.md` §0: **PASS** (20 varlık, 5 dakikalık snapshot'lar; ham audit `var/ops/v5_stage0/audit_5m.json` sha256 `9136a1f8…`) |
| Parity raporu | `docs/results/v5-parity-report.md`: **PARITY PASS**. Nihai frozen kopyada tekrar PASS (`var/ops/v5_parity/final_frozen`, freeze öncesi taze koşu `var/ops/v5_parity/prefreeze`): referans 71/71 (Δ = 0), Katman A, B1, B2, B3 |
| Red-team hükmü | Rev 3 bağımsız metodolojik red-team (3 tur, protokol §16): **READY FOR FREEZE**, Critical 0, Major 0 |
| Freeze zamanı (UTC) | 2026-10-11T12:47:00Z |

**Freeze öncesi taze doğrulama (2026-10-11):**
- worktree temiz;
- frozen manifest pinned sha ile doğrulandı, max ts 2026-06-30T23:59;
- holdout PASS;
- `history_v4` 80/80 provenance sha'sı eşleşti;
- V4 Stage 1 `summary.json` sha `aa652503…`;
- V4 worktree temiz @ 222efd8; `research-v4-spec-frozen` → 9d83382, `v5-adaptive-carry-m1` → e59b643;
- config, core ve engine değişmedi; `live_enabled: false`.

**Outcome öncesi zorunlu kapı (kullanıcı kararı):** Outcome koşusundan önce şunların hepsi gerekir:
- fallback attribution regression testi, gerçek replay yolu üzerinde: karar ve fill fallback'i; arm, varlık ve günün saati kırılımı; dolan notional ağırlığı;
- S-FB-CONS, S-PIT10 / PIT-SENSITIVE ve FALLBACK-AFFECTED implementasyonu;
- tam suite;
- bağımsız code review ve implementasyon ↔ frozen spec review (Critical 0, Major 0);
- tek historical outcome koşusu için kullanıcının ayrıca açık onayı.

**No V5 historical outcome had been run before this freeze.**
