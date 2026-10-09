# Research V4 — Stage 1 sonuçları (2026-10-09)

> **EXPLORATORY — reused development period; not generalization evidence.**
>
> Stage 1, V1–V3'te defalarca kullanılmış development döneminde (gerçek funding'in olduğu 2025-11-05 → 2026-07-01 aralığı, 238 gün) yapılan bir keşiftir.
> - Stage 1 **GO veremez.** Config başına yalnızca ADVANCE TO FORWARD ya da KILL kararı verir.
> - Tek GO kanıtı, `V4_EVIDENCE_START_TS = 2026-11-10T00:00:00Z` sonrasında toplanan forward veridir.
> - **holdout touched: NO** · **live trading: NO** · **DEPLOYABLE NOW: NO**

**Koşu bilgileri:**
- **Spec ve kod:** Dondurulmuş spec, tag `research-v4-spec-frozen` (commit 9d83382, spec sha256 `81f5d2e9…bea1e`). Kod `b074b69`. Freeze kontrolü PASSED (tag HEAD'in atası, çalışma ağacı temiz, spec'te fark yok).
- **Çıktılar:** `docs/results/v4-stage1/` (`summary.json` sha256 `ae4825bc…97454`, `manifest.json`, `report.md`, `proxy_diagnostics.json`). Bu `summary.json`, Stage 2'nin KILL edilmiş config kümesinin kaynağıdır.
- **Evren:** §2a'ya göre 20 varlık; MUBARAK U3/U4/U6/U7 kurallarını geçemedi (`v4-universe.md`).

## Config bazında Stage 1 kararı (önceden kayıtlı kural, §7.3)

| config | karar | ort. günlük net | b=28 UB95 | b=28 LB95 | b=14 LB95 | carry bileşeni |
|---|---|---|---|---|---|---|
| H1-Y10 | **KILL** (a, b) | −0,000136 | −0,000097 | −0,000195 | −0,000205 | funding − ücret − spread = −16,14 USD |
| H1-Y20 | **KILL** (a, b) | −0,000046 | −0,000042 | −0,000092 | −0,000091 | −5,91 USD |
| H2-L7-k1 | **KILL** (b) | +0,000141 | +0,002829 | −0,003213 | −0,003679 | ort. carry_net −0,000015 |
| H2-L7-k2 | **KILL** (b) | +0,000283 | +0,002220 | −0,001419 | −0,001757 | ort. carry_net −0,000004 |
| H2-L30-k1 | **ADVANCE TO FORWARD** | −0,001120 | +0,002954 | −0,004773 | −0,005364 | ort. carry_net +0,000127 |
| H2-L30-k2 | **ADVANCE TO FORWARD** | +0,000469 | +0,002065 | −0,001705 | −0,001884 | ort. carry_net +0,000113 |
| H5-HFWB | **KILL** (a, b) | −0,000169 | −0,000139 | −0,000244 | −0,000243 | −21,19 USD |

**Hipotez özeti:**
- **H1 (cash-and-carry): KILL.** Funding geliri (Y10 için 2,02 USD), spot taker ücretini (15,87 USD) ve spread'i karşılamıyor. Ortalama getiri istatistiksel olarak da negatif.
- **H2 (perp funding carry): forward'a geçti.** Yalnızca L30 configleri. L7 configlerinde funding, ücret ve spread'i karşılamıyor. H2-L7-k2'nin pozitif toplamı fiyat P&L'inden geliyor ve kural gereği "funding edge" sayılmıyor.
- **H5 (funding × basis): KILL.**

**Not (KILL kuralı muhafazakâr çalıştı):** H2-L30-k1'in ortalaması negatif (−0,00112/gün). Ancak üst güven sınırı pozitif, yani negatiflik istatistiksel olarak anlamlı değil. Carry bileşeni de pozitif. Önceden kayıtlı kural gereği ADVANCE edildi. Bu, H2-L30-k1'in iyi olduğu anlamına gelmez; yalnızca tarihsel veride elenemediği anlamına gelir.

Hiçbir config Stage 1'de C1–C7'nin hepsini geçmedi, bu yüzden Stage-1 adayı yok. C2 (Holm, b = 28 ve b = 14) yedi config'in hiçbirinde geçmiyor (en düşük p: H2-L7-k2, b=28 için 0,355).

## Maliyet ayrışımı (PRIMARY, toplam USD, değerlendirme penceresi boyunca)

| config | spot fiyat | perp fiyat | funding | ücret | spread | net etki |
|---|---|---|---|---|---|---|
| H1-Y10 (E0 530) | −32,81 | +32,04 | +2,02 | 15,87 | 2,29 | negatif; ücret baskın |
| H1-Y20 | −35,72 | +35,89 | +0,95 | 5,99 | 0,88 | negatif |
| H5-HFWB | −5,13 | +5,44 | +1,90 | 20,06 | 3,03 | negatif |
| H2-L7-k1 (E0 265) | — | −13,56 | +9,35 | 6,52 | 3,96 | ~0 |
| H2-L7-k2 | — | +10,52 | +7,76 | 5,26 | 3,00 | pozitif, fiyat kaynaklı |
| H2-L30-k1 | — | −91,82 | +10,86 | 2,56 | 1,24 | negatif, fiyat kaynaklı |
| H2-L30-k2 | — | +9,95 | +10,69 | 2,27 | 1,08 | pozitif; carry bileşeni +7,3 |

**Sensitivity:**
- H1/H5'te maker satırı bile negatif: Sharpe −2,1 / −1,5 / −3,9. Spot ücretinin yarıya inmesi carry'yi kurtarmıyor.
- S-COST2X'te H2-L30-k2 hâlâ pozitif (+0,000416/gün).
- H2 için S-SPREAD10 satırı PRIMARY ile aynı. Bu beklenen bir durum: H2 yalnızca perp kullanıyor, spot spread'i uygulanmıyor.

## Null karşılaştırması (PRIMARY, Sharpe)

- **H1-Y10:** −4,18. Rastgele girişli 5 null'ın ortalaması ≈ −6,26; N-ALWAYS −3,75. Rastgele girişten iyi ama her-zaman-carry'den kötü (C4 FAIL).
- **H1-Y20:** −2,30. Null ortalaması ≈ −4,33; N-ALWAYS −3,75 (C4 PASS, ama getiri negatif).
- **H5:** −5,22. Null ortalaması ≈ −6,53; N-ALWAYS −3,75 (C4 FAIL).
- **H2-L30-k2:** 0,447. Karıştırılmış sıralamalı 5 null'ın ortalaması +0,159 (C4 PASS). Null'ların dağılımı geniş (−1,68 … +2,34). Bu, H2 sonuçlarının tek varlık/tek hafta oynaklığına çok duyarlı olduğunu gösteriyor.
- **H2-L30-k1:** −0,681. Null ortalaması +0,346 (C4 FAIL).

## Robustness ve diagnostic'ler (GO/NO-GO'ya girmez)

- **D-PERSIST:**
  - Haftalık örtüşmeyen pencerelerde F7 ile sonraki 7 günün funding'i arasındaki Spearman korelasyonu 0,45 (33 hafta). Kesitsel sıralama kalıcı; ön koşul uyarısı yok.
  - Buna karşılık günlük AR(1) 0,29, yarılanma süresi ~0,6 gün: seviye hızla dönüyor.
- **D-DISP:** H2'nin günlük getirisi yüksek funding dağılımı dönemlerinde daha iyi (bütün H2 configlerinde "above" ortalaması > "below"). Kural gereği bundan strateji türetilmez.
- **D-H5CELLS:** Yüksek funding + geniş basis hücresinde (n = 64) sonraki 7 günün funding'i yıllık ≈ %0,1. Bu, carry'nin ücretleri karşılaması için çok düşük.
- **H2 maruziyeti:**
  - Residual BTC beta −0,03 … +0,08; ortalama net exposure ≈ %0. Dollar-neutral tasarım tuttu.
  - k = 1'de yoğunlaşma yüksek (Herfindahl ≈ 0,49). k = 2'de 0,25.
  - P&L'i birkaç varlık domine ediyor (ör. TAO, ZEC, HYPE).
- **Veri sayaçları:**
  - Funding'i bilinmeyen saat sayısı config başına 3–16; %1 bayrağı tetiklenmedi.
  - Perp spread fallback'i 0, `valuation_stale` 0, likidasyon 0.
  - Minimum emir nedeniyle atlanan hedge düzeltmeleri: H1-Y10'da 57, H5'te 66. Hedge hatası bu yüzden raporlanıyor.

## Açıklanan sapmalar ve uygulama tercihleri

1. **Enstrüman metadata snapshot'ı:** `config/v4_instruments.json` (sha256 `d14a82bb…`) freeze commit'ine değil, tag'den sonraki `b074b69` commit'ine eklendi (spec §7.1'den sapma). İçerik saf borsa metadata'sıdır (adım ve minimum emir) ve sonuç içermez. Collector'ın 2026-10-09 kaydından alındı ve geçmişe uygulandı (bilinen anakronizm).
2. **Perp spread:** Karar öncesi [t−1h, t) içindeki 1 dakikalık değerlerin ortalaması alınıyor. En az 30 geçerli dakika yoksa 10 bps fallback kullanılır (spec'te açık değil, sayılır; bu koşuda 0 kez).
3. **H1 giriş boyutu:** Ayrıca `spot nakdi / (1 + ücret + yarım spread)` ile sınırlı.
4. **Spec başlığı:** Dondurulmuş spec'in başlığında hâlâ eski "Frozen değil" satırı var. Tag ve freeze bölümleri belirleyicidir; spec dondurulduğu için düzeltilmedi.
5. **Spec'te açık olmayan diğer uygulama tercihleri:** V4 implementer raporunda tek tek listelendi (en muhafazakâr okuma kullanıldı).

## Test ve review

- **Final red-team (spec):** İki tur. Birinci tur 0 Critical / 8 Major; düzeltildi. İkinci tur READY TO FREEZE, 19/19 PASS (`v4-final-redteam.md`).
- **Code review:**
  - İlk tur READY FOR STAGE 1: 1 Major (Stage 2 aile kaynakları) ve 10 Minor.
  - Bunlardan F1, F4, F5, F7, F8 ve F9 düzeltildi. Doğrulama turu: Critical 0 / Major 0.
  - Kalan Minor'lar (F3, F6, F10, F11) yukarıdaki sapmalar bölümünde açıklandı.
- **Testler:**
  - `tests/v4`: 217 passed.
  - Tam suite (b074b69'un atası f8fd92c), art arda iki koşu: ikisinde de **1111 passed, 2 skipped, 3 deselected, 0 failed**.
  - Flaky v2 pool testi deterministik yapıldı (30/30).

## Sonraki adım

H2-L30-k1 ve H2-L30-k2 Stage 2'de forward veriyle izlenecek.
- **Evidence başlangıcı:** 2026-11-10.
- **Bakış tarihleri:** 2027-05-11, 2027-08-10, 2027-11-10.
- **Holm ailesi:** 7 config; KILL edilenler p = 1 ile ailede kalır.
- Bakış tarihinden önce evidence simülasyonu yapılmaz.
- Tarihsel sonuçlar görüldükten sonra spec, kriter ya da config değiştirilmez.
