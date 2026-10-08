# Luna: devir notu (2026-10-09, PC değişimi)

Yeni bir Claude Code oturumu bu dosyayı okuyarak devam edebilir. Kullanıcının son talimatı ve kalan işler aşağıda.

## Repo ve branch'ler
- `feat/research-v2`: **en güncel.** V1 + P2 + Research V2 kodu, SC-1/SC-2, closure düzeltmeleri; son kod commit'i `1cc450a`.
- `feat/luna-core`: V1/P2. V2 henüz buraya merge edilmedi.
- `main`: ilk iskelet.

## Kurulum (yeni PC)
1. `pip install uv`. Kurumsal TLS proxy varsa her uv komutunun başına `UV_SYSTEM_CERTS=1` ekle.
2. `uv sync`
3. `UV_SYSTEM_CERTS=1 uv run pytest -q`: beklenen sonuç **672 passed, 3 deselected**.
4. **Veri:** `var/` git'te yok. Eski PC'den kopyala; ya da yeniden indir: `luna backfill …` (perp + spot, saatler sürer). Spot arşivi pinned SHA256 ile doğrulanır.
5. **Paper bot'lar:** `docker compose up -d --build`. DB'ler `var/luna_perp.db` ve `var/luna_spot.db`. SC-2 ile spot ücreti değişti; spot bot bu PC'de yeniden build edilene kadar eski ücreti kullanıyordu.
6. `.env` yok; Telegram hiç bağlanmadı (`.env.example` şablonu repoda).

## Değişmez kurallar
- **Holdout'a (2026-07 → 2026-10) dokunma.** `luna holdout` çalıştırma, `var/holdout_registry.json` oluşturma.
- **Research V2 spec'i dondurulmuş:** `docs/superpowers/specs/2026-10-06-luna-research-v2-design.md`. Değişiklikler yalnızca kayıtlarla yapılır: SC-1 (cross-sectional emir limitleri) ve SC-2 (Kraken spot ücreti 0.25/0.40 → 0.40/0.80).
- **V2 verisi** 2026-07-01'de kesilir (`V2_DATA_END`). OOS sonuçlarına bakarak parametre ya da kriter değiştirme.
- **V3 için kod yazma;** önce tasarım önerisi.

## Durum
- **Spot V2 final koşusu (kod `1cc450a`, düzeltilmiş ücretler):** bitti. Sonuç **INSUFFICIENT DATA, 0 işlem, aday yok.** Hiçbir konfigürasyon train'de kabul edilmedi. Naive momentum: 158 işlem, +0.98 R, CI [−0.52, +3.51], +$150, DD %50. Rapor: `var/reports/research_v2_spot_20261008T181851/` (yalnızca eski PC'de).
- **Perp V2 final koşusu (kod `1cc450a`):** 2026-10-09 00:30 itibarıyla leave-one-out aşamasındaydı. Rapor dizini `var/reports/research_v2_perp_20261008T200335/`.
  - Eski PC'de bittiyse `var/` ile birlikte kopyala.
  - Bitmediyse yeni PC'de baştan koş: `UV_SYSTEM_CERTS=1 uv run luna research-v2 --account perp --jobs 2`. Yaklaşık 3 saat sürer. Koşu sırasında ağır bir iş (ör. test suite) çalıştırma; bellek yetersizliği riski var.
  - Spot'u da yeni PC'de yeniden koşmak istersen aynı komutu `--account spot` ile çalıştır.
- **Önceki (düzeltme öncesi) perp sonucu:** `docs/results/research-v2.md`, eski sürüm. INSUFFICIENT DATA, 27 işlem, +0.27R, kırılgan. Final sonuç bunun yerini alacak.
- **Kararlar ve gerekçeleri:** `docs/superpowers/ledgers/2026-10-08-research-v2-ledger.md`. Review raporları aynı klasörde.

## Kullanıcının son talimatı (2026-10-09), sırasıyla
1. Perp final koşusunu bitir. Yeni fikir, parametre ya da strateji ekleme; SC-2'yi koru; holdout yok.
2. `docs/results/research-v2.md`'yi **yalnızca final düzeltilmiş sonuçlarla** güncelle. Perp ve spot için ayrı ayrı:
   - GO / NO-GO / INSUFFICIENT DATA
   - işlem sayısı
   - ortalama R ve bootstrap CI
   - PF
   - net P&L
   - max DD
   - en kötü gün
   - pozitif pencere / toplam pencere
   - 2× maliyet sonucu
   - naive baseline karşılaştırması
   - top1/top3 robustness
   - varlık leave-one-out
   - dönem leave-one-out
   - maliyet atribüsyonu
   - açık bilinen issue'lar

   Özet çıkarma script'i: `summary.json` alanlarını okuyan küçük bir Python script'i. Alanlar `verdict`, `checks`, `variants`, `naive_momentum`, `robustness.candidate`, `extended["candidate:realistic"]`, `trials`.
3. Aday yoksa V2'yi **CLOSED — NO CANDIDATE** olarak kapat.
4. Full test suite, final independent review, `feat/research-v2` → `feat/luna-core` local merge, merge sonrası testler.
5. Tek bir V2 closure summary:
   - final commit
   - test sayısı
   - final perp sonucu
   - final spot sonucu
   - holdout touched: NO
   - bilinen critical/major issue'lar
   - merge sonucu
6. Ardından yalnızca V1+V2 failure analysis ve 3–5 **yeni alpha-source ailesi** için V3 research-design önerisi. Momentum varyasyonlarını yeniden optimize etme; kod yok.
