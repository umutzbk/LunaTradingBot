# V4 pre-freeze red-team review (2026-10-09)

> **Superseded by user decisions 2026-10-09** (spec §7.2, §7.5, §10). The 72 h dry-run, the earliest freeze date of 2026-11-09 and the N3 adjustable thresholds no longer apply. The final review is in `v4-final-redteam.md`.

**Kapsam ve yöntem:**
- **Reviewer:** bağımsız bir agent (spec'i yazmadı), salt okunur çalıştı.
- **Okumadıkları:** `var/` altındaki dosyalar okunmadı; getiri ya da P&L hesaplanmadı.
- **Hedef:** V4 Research Spec rev 2 ilk taslağı, Collector Spec rev 2 ve veri envanteri.

**İlk verdict: NOT READY (bir revizyon uzakta).** İki BLOCKER ve on beş MAJOR bulgu vardı; bulguların çoğu, açıkça yazılmamış araştırmacı seçimleriydi.

**Revizyon sonrası durum (bu commit):**
- 25 bulgunun 25'i spec metnine işlendi.
- Kullanıcıya ait üç tercih için önerilen varsayılan yazıldı ve bunlar spec §10'da açık karar olarak işaretlendi.
- Freeze hâlâ şunları bekliyor:
  - kullanıcı onayı;
  - S0-F (funding semantiği);
  - §7.2 DQ eşiklerinin ≥ 72 saatlik dry-run ile sabitlenmesi;
  - en erken 2026-11-09 (§7.5).

| id | sev | Bulgu | Çözüm (spec bölümü) |
|---|---|---|---|
| F1 | BLOCKER | M1 "≥182 geçerli gün" ilk bakışta ancak %100 geçerlilikle sağlanabiliyordu; M5, M1 ile özdeşti | M1 = 182 takvim günü, M2 = ≥%90 DQ-geçerli; M5 kaldırıldı (§6) |
| F2 | BLOCKER | Collector-only lookback'ler (30 gün) evidence'ın ilk ~30 gününü flat bırakıyordu → H2-L30 M3/M4'ü ve H1 M4'ü (6 döngü = yapısal maksimum) yapısal olarak kaçırıyordu | Freeze ≥ collector_start + 31 gün (§7.5); M4(H1/H5) = ≥4 giriş, ≥2 varlık; M4(H2) = ⌊pencere/7⌋−2 (§6) |
| F3 | MAJOR | Bootstrap → Holm eşlemesi tanımsızdı | Merkezlenmiş MBB p-değeri formülü; her bakışta Holm α/3 (§6) |
| F4 | MAJOR | b=14, L30 ve uzun tutmalar için anti-konservatif olabilir | b=14 **ve** b=28 konjonktif (§6); alternatif §10.3 |
| F5 | MAJOR | Bakış sonuçlarının anlamı belirsizdi (optional stopping) | GO kesin ve aileden çıkar; 1–2. bakış NO-GO geçici; 3. bakış kesin; α/3 her bakışta harcanır (§7.4) |
| F6 | MAJOR | Funding zaman damgası, ödeme aralığı ve tolerans teyit edilmemişti | S0-F maddeleri 1–5, sayısal tolerans; aksi halde BLOCKED (§2) |
| F7 | MAJOR | Bar etiketi (open_ts) ile "bar_close ≤ t" ifadesi look-ahead riski taşıyordu | Ortak zamanlama kuralları (§3, §8) |
| F8 | MAJOR | Eksik funding saati için proxy veya tahmin yolu açıktı | known_only = 0 nakit akışı + sayaç + %1 bayrağı (§2, R3) |
| F9 | MAJOR | `fut_funding` ham yanıtı holdout satırlarını içeriyor; collector spec'i bunun tersini söylüyordu | Holdout = [2026-07-01, collector_start); yükleyici ve DQ eski satırları atar, test eklenir (R1; collector §2) |
| F10 | MAJOR | Forward hacim, listeleme yaşı ve hacim formülü tanımsızdı | Formüller ve ≥27/30 kuralı (§3); collector'a `fut_instruments` ve `spot_assetpairs` kaynakları eklendi |
| F11 | MAJOR | Pozisyon tutulurken uygunluk kaybı, delisting, eksik bar, eşitlik ve cooldown günü belirsizdi | Ortak kurallar (§3) |
| F12 | MAJOR | El hesabı testi USD-hedge kuralıyla çelişiyordu; maliyet beklentisi yoktu | T1–T5 test tablosu (0,45 / 0,5446 / 0,2424 / 2,10155 / −1,40915) (§5.1) |
| F13 | MAJOR | S-COST2X'in forward tanımı yoktu | Forward: 2× taker + 2× gerçekleşen yarım-spread/book-walk (§5.4) |
| F14 | MAJOR | Null yapısı belirsizdi | N-RANDENTRY / N-ALWAYS / N-SHUFFLE tam tanım; 0 giriş → C4 FAIL (§4) |
| F15 | MAJOR | DQ eşiklerinin dry-run'dan nasıl türetileceği belirsizdi | Dönüşüm kuralı; geçersiz gün işleme; skew-geçerli snapshot yoksa karar atlanır (§7.2) |
| F16 | MAJOR | Kraken minimum emir boyutu ihmal edilmişti | Freeze anı metadata'sı, yuvarlama, atlama sayacı (§3) |
| F17 | MINOR | Funding'in E'de çift sayılma riski | Funding collector nakdine yazılır (§2, §5.1) |
| F18 | MINOR | Mark kaynağı (tarihsel/forward) ve likidasyon high'ı belirsizdi | §5.1 tablo |
| F19 | MINOR | Analytics spread istatistiği ve book-walk zamanlaması belirsizdi | Saat içi 1 dakikalık ortalama; yürütmeden önceki snapshot (§5.3) |
| F20 | MINOR | Seyrek spot barlarıyla bayat basis riski | `filled` barlar dışlanır; ≥48 bar şartı (§3 H5) |
| F21 | MINOR | H2 rebalance'ları bağımsız sayılıyordu | "Örtüşür, bağımsız değil" ifadesi eklendi (§6) |
| F22 | MINOR | C7'de kısmi aylar belirsizdi | Yalnız tam aylar, ≥3 ay (§6) |
| F23 | MINOR | Ara dönemde simülasyon koşmak geri alınamaz bir bakış olurdu | Evidence simülasyonu bakış tarihinden önce hiç koşmaz; yalnızca sinyal sayıları gösterilir (§7.4) |
| F24 | MINOR | N-ALWAYS fiilen uygulanabilir bir strateji | Null'lar asla GO alamaz (R6) |
| F25 | MINOR | Stage 1'de M2 ve C7 tanımsızdı | Stage 1 M2 = günlük kapsam ≥%90; C7 için "evaluation window" (§7.3) |

**Kullanıcıya bırakılan kararlar:** Spec §10.
1. Onay ve freeze tarihi.
2. Ortak evidence penceresi mi, config başına pencere mi.
3. Blok uzunluğu.
4. Holdout kill-test.
5. Backup hedefi.

## İkinci tur (aynı reviewer, revize metin üzerinde)

**Verdict: READY WITH FIXES.**
- F1–F25: 18'i tamamen, 7'si kısmen düzeltilmişti.
- T1–T5 aritmetiği bağımsız olarak doğrulandı.
- 15 kalan veya yeni madde (N1–N15) bu commit'te metne işlendi:

| id | sev | Konu | Çözüm |
|---|---|---|---|
| N1 | MAJOR | Freeze'i bir ay ertelemek, Stage 1'in freeze öncesi koşulmasına kapı açıyordu | Kural freeze'i ile evidence başlangıcı ayrıldı (§7.5); R9: freeze tag'inden önce getiri hesaplanmaz; metadata snapshot freeze commit'inde |
| N2 | MAJOR | Tag saati yüzünden 182 günün 181 tam güne düşmesi | D0 = 00:00 UTC; bakışlar D0+182/273/365 |
| N3 | MAJOR | DQ dönüşüm kuralı alt-sınır metriklerine uymuyordu | Dry-run penceresi sabit; üst-sınır metrikleri ayarlanabilir, alt-sınır metrikleri değiştirilemez |
| N4 | MAJOR | Snapshot başına skew eşiği ve kesinti günü tanımsızdı | Snapshot başına ≤ 2.000 ms; kesinti günü r = 0, kontroller atlanır ve sayılır |
| N5 | MAJOR | Çok bacaklı işlemlerde kısmi yürütme tanımsızdı | Hep-ya-hiç kuralı |
| N6 | MAJOR | N-ALWAYS belirsizdi | Çıkış, sınır veya cooldown yok; D0'da ve her ayın 1'inde hedeflenir |
| N7–N11 | MINOR | Ücret tabanı, tarihsel mark barı, C6 Holm ailesi, Stage 1 α/M2, flat haftalar | Metne işlendi |
| N12 | MINOR | Ara raporlardaki F7 sayıları funding rejimini ele veriyordu | Yalnızca uygun evren büyüklüğü gösterilir |
| N13–N15 | MINOR | Collector §1/§10, envanter holdout ifadesi, Stage 1 metadata anakronizmi | Metne işlendi |

**Reviewer'ın son durumu:** N1–N15 düzeltildiğinde spec, bekleyen maddeler (kullanıcı onayı, S0-F, DQ dry-run) tamamlanınca **READY TO FREEZE**. Bu son düzeltmeler üçüncü bir review turundan geçmedi.
