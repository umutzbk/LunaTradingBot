# V6 Profitability — Exact Spec

**Durum:** **FROZEN** (r4; tag `v6-profitability-spec-frozen`; freeze kaydı `docs/results/v6-spec-freeze-record.md`). Holdout kapalı; V6 backtest ya da outcome yok. Bu dosya artık değiştirilmez.

**Kaynaklar:**
- Plan rev 2 (`luna-v6-profitability-research-plan.md`, commit `25ee363`). Bu spec onun normatif, tek anlamlı halidir.
- Sabitler `luna-v6-frozen-constants.json` dosyasındadır. Bu metinle çelişen bir sabit varsa freeze geçersizdir.

**Hedef:** Maliyetler sonrası, risk kontrollü ve tekrar edilebilir **mutlak** net pozitif getiri.

Kurallar `S-` numaralarıyla anılır. "Yapılır" zorunludur.

---

## 0. Rev 2'den sapmalar ve kullanıcı kararları (hepsi outcome görülmeden)

| # | Sapma / karar | Gerekçe |
|---|---|---|
| D1 | **H3 taraf bölüşü:** beta-nötr bölüş yerine **basket-vol risk paritesi**. Ardından dolar sınırı (0.2), **standalone taraf vol oranı ≤ 1.5** sert kuralı ve **inşa sonrası \|β_net\| ≤ 0.05·E** sert limiti. Ex-ante long/short risk katkıları raporlanır | Rev 2'nin H3 amacı V5'teki 2–2.5× long/short vol dengesizliğini çözmekti. Beta-nötr bölüş taraf risk oranını kontrol etmez. P&L'siz mekanik kontrolde (`docs/results/v6-feasibility`, dev risk girdileri < 2026-07-01) risk paritesi ve dolar sınırıyla standalone oran medyanı 1.0, en kötüsü 1.28 (k=4) ve 1.17 (k=6); \|β_net\| p95 0.047. Dolar sınırı k=4'te günlerin %26'sında bağlar ve oranı tam 1'e indiremez; ≤ 1.5 kuralı bu durum için sigortadır (kullanıcı kararı 2026-10-12) |
| D2 | **Forward son bakış** α_365 = **0.048** (0.05 değil) | Bakışlar üzerinden Bonferroni: 0.001 + 0.001 + 0.048 = 0.050 (kullanıcı kararı 2026-10-12) |
| D3 | **Ara bakışlar** (90, 180) yalnız FINAL GO verir ya da kesin olaylarla kapatır (kill, likidasyon, MODEL INVALID). Nokta tahminine dayalı futility kapatması yok | Tek ve değişmez algoritma; yalnız GO yollarını azaltır |
| D4 | **H1 feasibility izolasyonu:** gün 91–365 confirmation'ı ayrı bir replay'dir; düz başlar, 265$ taze sermaye, hiçbir state ya da P&L taşınmaz (S-H1-F) | Kullanıcı maddesi D |
| D5 | **Fee kaynağı:** resmi URL, erişim zamanı, sayfa hash'i | Kullanıcı maddesi A. Sayfadaki 0.38% maker oranı yalnız seçili pariteler için bir "spot rebate" programına aittir; primary değildir |
| D6 | **Holdout bitişi** tam `collector_start_ts` = 2026-10-09T11:52:48.861565107Z | Rev 2'de yaklaşıktı |
| D7 | **Forward evidence başlangıcı T_F** = bağımsız implementasyon review onayından en az 24 saat sonraki ilk 00:00 UTC; `v6-code-frozen` tag'i o anda atılır. Spec freeze'inin zamanı değildir | Spec ile kod arasındaki haftalarda forward veri görülebilir; implementasyon yorumları bu bilgiyle çözülemesin (kullanıcı kararı 2026-10-12) |
| D8 | **H3 sermayesi 265$ kalır.** P&L'siz mekanik kontrolde k=4 günlerin %90.4'ünde, k=6 her gün uygulanabilir; medyan bacak ~15$ | Kullanıcı kararı 2026-10-12. Dev'de ya da sonrasında görülen uygulanamazlık R9 kapsamında bug **değildir** ve freeze sonrası düzeltilmez |
| D9 | **Rev 2 §6.2 referansları** korunur: holdout koşusunda "cash" (0) ve değişmeden koşulan V5 W/E (S-STAT-O); ikisi de hükümsüzdür | Red-team m9 |
| D10 | **Forward aile** = holdout'taki 4 strateji config'inin tamamı (m_F = 4, sabit). Aday olmayanlar her bakışta p = 1 | Aile, holdout sonuçlarından seçilmez (red-team M4) |

H1, H2 ve H3'ün geri kalan tasarımı ve profitability kriterleri rev 2 ile aynıdır.

---

## 1. Veri, zaman ve guard'lar

**S-DATA-1 Funding.**
- `f_i(p)`: Kraken PF saatlik göreli funding'i. `p` dönemin başlangıcıdır; pozitif değerde longlar short'lara öder.
- **Tarihsel:** Değer `p + 1h`'de bilinir.
- **Forward:** Kayıt "bilinir" ancak `recv_ts ≤ t` ise.
- **Forward'da eksik funding:** Collector'da olmayan tutma saatleri, dönem kapandıktan sonra Kraken'in resmi tarihsel funding API'sinden indirilen ve hash'lenen değerlerle settle edilir; asla interpolasyon yapılmaz. API'de de yoksa o bakış INVALID RUN olur.

**S-DATA-2 Perp fiyat ve spread.**
- `t` anındaki mid = `t − 15m`'de açılan mark bar'ının kapanışı.
- Saatlik getiriler, saat başına denk gelen 15 dakikalık bar kapanışlarından (`t = hh:00`) hesaplanır.
- Spread: Kraken analytics interval = 300 (V5 rev 3):
  - snapshot `T + 5m`'de bilinir;
  - karar penceresi `[t − 1h, t)` içinde ≥ 6/12 geçerli;
  - fill penceresi `[t, t + 15m)` içinde ≥ 2/3;
  - geçerli değer: sonlu, > 0, ≤ 500 bps;
  - aksi halde 10 bps fallback.

**S-DATA-3 Spot (tarihsel).**
- Mid vekili: `t − 15m`'de açılan kraken_spot bar'ının kapanışı.
- Fill: `t`'de açılan bar'ın kapanışı (`t + 15m`).
- 24 saatlik hacim: son 96 bar'ın `volume × close` toplamı.

**S-DATA-4 Forward.**
- Kaynak: collector'ın fut_tickers, fut_book, fut_funding, spot_ticker (60 saniye) ve spot_depth (5 dakika, 10 seviye) verisi.
- **Evidence başlangıcı:** `T_F` = bağımsız implementasyon review'unun onayından **en az 24 saat sonraki ilk 00:00 UTC**. `v6-code-frozen` tag'i o anda atılır (D7). Zamanlama takdire bırakılmaz. Forward değerlendirmesi yalnız `recv_ts ≥ T_F` olan snapshot'ları kullanır. Forward gün 1, `T_F` anında ya da sonrasındaki ilk 00:00 UTC'dir.
- **Model girdileri:** Funding, mark ve index bar'ları `T_F` öncesinden (holdout penceresi dahil) yalnız girdi olarak okunabilir: H2a fit'i, FL30, vol ve kovaryans. Bu yalnız **tek holdout açılışından sonra** mümkündür; o zamana kadar forward koşusu hesaplanmaz.

**S-DATA-5 Coverage.**
- Bir gün, 15 dakikalık izleme anlarının ≥ %90'ında taze (≤ 180 saniye) ticker snapshot'ı varsa değerlendirilebilir sayılır.
- **Eksik anlar:**
  - emir gönderilmez;
  - stop'lar ve çıkışlar ilk mevcut snapshot'ta işler;
  - 00:00 kararı, 6 saat içinde ilk mevcut snapshot'ta verilir; 6 saat geçerse o günün kararı atlanır.
- Veri geri geldiğinde P&L o güne yazılır.
- Bir bakışta değerlendirilebilir gün oranı < %80 ise o bakış INCONCLUSIVE olur.
- **Veri kaybı asla doldurulmaz.**

**S-GUARD-1 Holdout.**
- V6 yükleyicileri `[2026-07-01T00:00Z, T_F)` aralığındaki her satırı reddeder. İstisna: V5 `outcome_guard` deseniyle üretilen holdout yetki token'ı. Token şunları gerektirir:
  - tag ve temiz ağaç;
  - spec, sabitler ve kod sha'ları;
  - kullanıcının açık onay cümlesi;
  - iki exclusive tek-açılış marker'ı.
- Holdout tek koşuda, bir kez açılır. Holdout spread'lerinin indirilmesi bu açılışın parçasıdır; V5 Stage 0 denetim kuralları uygulanır ve başarısızlık INVALID RUN demektir.
- Açılıştan sonra aynı veri yalnız S-DATA-4'teki model girdisi rolüyle okunabilir.

**S-GUARD-2 Dev.**
- Dev döneminde (< 2026-07-01) V6 runner'ı kör çalışır: yalnız invariant'lar (sayılar, gross, inşa kısıtları, muhasebe kimlikleri, PIT testleri). P&L, Sharpe, carry ve kalibrasyon sütunları maskelidir.
- Mekanik fizibilite (uygulanabilir gün oranı, bacak boyutu) bir invariant'tır; outcome değildir.

**S-GUARD-3 Forward maskesi.** Forward P&L ve metrikleri yalnız takvimli bakışlarda açılır.

---

## 2. Maliyetler (S-COST)

**S-COST-1 Fee'ler (Kraken public Tier 1, conservative primary).**

| | Maker | Taker |
|---|---|---|
| Spot | 0.40% | 0.80% |
| Futures | 0.02% | 0.05% |

- **Kaynak:** https://www.kraken.com/features/fee-schedule, erişim 2026-10-12T01:12:31Z.
- **Snapshot:** `docs/results/v6-fee-source/kraken-fee-schedule-2026-10-12.html` (sha256 `118e79185c1794f47593fbbf2a43898a69338dce90cc593f8a7b03d61e620ed8`). İlgili tablolar ("Spot Crypto", "Futures") `excerpt.txt` dosyasında.
- **Hesaba özel tier:** Yalnız freeze öncesi ve snapshot ile değiştirilebilir. Freeze sonrası gerçek fee'ler yalnız bilgi amaçlı **S-FEE-ACTUAL** satırında gösterilir.

**S-COST-2 Spread ve slip.**
- **Perp half spread:** S-DATA-2'deki karar ya da fill penceresi.
- **Spot half spread (tarihsel):** `max(2 × aynı pencerenin perp half spread'i, 5 bps)`.
- **Forward:** gözlenen book (S-H1-X, S-H3-11).
- **Slip:** Tarihsel taker fill'lerde her yön 2 bps. Forward book yürümesinde ek slip yok; yürünen seviyeler gerçek fiyattır.

**S-COST-3 Karar anı maliyeti.** Kararlardaki maliyet tahminleri yalnız `t`'de bilinen spread'i kullanır (karar penceresi).

**S-COST-4 Tarihsel fill fiyatı.** `px = mid_fill·(1 ± half)·(1 ± slip)`, fee = `rate·qty·px` (V5 DelayedExecutor).

---

## 3. Muhasebe (S-ACC)

**S-ACC-1 Equity.** `E_t` = USD nakit (perp teminatı dahil) + Σ spot qty × spot mid + Σ perp gerçekleşmemiş P&L (mark).

**S-ACC-2 Bileşenler.**
- **Funding:** held-hours oranıyla settle edilen funding (V5 FundingLedger).
- **Fee'ler.**
- **Spread/slip:** fill fiyatı ile fill anındaki mid arasındaki fark. Forward'da referans, fill anından önceki son snapshot'ın mid'idir.
- **Price/basis:** `ΔE − funding + fee + spread/slip` (kalan).
- **Carry:** `carry = funding − fee − spread/slip`.

**S-ACC-3 Günler.**
- Gün `d` = `[d 00:00, d+1 00:00)` UTC; fill ve settlement'lar kendi zaman damgasının gününe yazılır.
- Günlük seriler `net_d / E_{d−1}` ve `carry_d / E_{d−1}`, pencerenin bütün takvim günleri.
- Holdout'un son, kısmi günü (00:00 → 11:52:48) tek bir gün sayılır.

**S-ACC-4 Terminal unwind.**
- Pencere sonunda açık pozisyonlar, **pencere sonunda ya da öncesinde bilinen** mid ve karar penceresi spread'iyle, taker fee ve slip ile kapatılmış sayılır. Pencere sonrasına uzanan bir fill penceresi kullanılmaz.
- Maliyet son güne yazılır; PRIMARY'ye dahildir.
- Forward ara bakışlarında unwind varsayımsaldır: yalnız değerlendirmede düşülür, pozisyonlar devam eder.

**S-ACC-5 Metrikler.**
- **Sharpe:** günlük net getirilerin `ortalama / std × √365` değeri.
- **MaxDD:** günlük equity üzerinden.
- **En kötü gün.**
- **ES97.5:** V5 `stats`.

**S-ACC-6 Attribution.** Her fill ve settlement bir varlığa ve bir episode'a atanır. H1'de spot ve perp aynı episode'dur. Terminal unwind ile kapatılan episode kapanmış sayılır.

---

## 4. H2 — Tahmin (S-H2)

**S-H2-0 Contamination kuralı.**
- Estimator formu, feature'lar, hyperparameter'lar, pencereleme, regularisation, başlatma, minimum geçmiş, eksik-veri kuralı ve ufuklar bu spec'le freeze edilir.
- Her karar anında fit ya da update yalnız `t` öncesi veriyle yapılır (PIT).
- Holdout ve forward koşularında fit'e dev gözlemleri girebilir.
- Hiçbir seçim outcome'a bakılarak yapılmaz.

**S-H2-1 H2a.**

```
X_i(t)   = mean{ f_i(p) : p ∈ [t−720h, t−1h], bilinen } ; #bilinen < 648 ise eksik
Y_i(s,h) = mean{ f_i(p) : p ∈ [s, s+h−1h], bilinen }   ; #bilinen < 0.9·h ise eksik ; s+h'de bilinir
H = {168, 720}
fit(t, h):
    D  = {(i,s) : s günlük 00:00 UTC, S0 ≤ s, s+h ≤ t, X_i(s) ve Y_i(s,h) mevcut}
    S0 = X'in ≥ 10 varlık için mevcut olduğu ilk günlük tarih
    #distinct(s) < 60 veya mean_s #{i} < 10 ise → MODEL WARMUP
    (lo_X, hi_X) = quantile(X_D, [0.01, 0.99]); (lo_Y, hi_Y) = quantile(Y_D, [0.01, 0.99])
    â, b̂ = OLS( clip(Y_D) ~ 1 + clip(X_D) )   (eşit ağırlık)
predict(i, t, h) = â + b̂·clip(X_i(t), lo_X, hi_X) ; X_i(t) eksikse tahmin yok
```

- Fit her gün 00:00 UTC'de yenilenir.
- Tahmini olmayan varlığa giriş yok. Tutulan pozisyonun tahmini 24 saatten uzun süre yoksa çıkış.

**S-H2-2 H2b (yalnız diagnostic).**
- Feature'lar ve minimum kapsamlar:
  - FL30: ≥ 648/720 dönem;
  - FL7: ≥ 151/168;
  - FL1d: ≥ 22/24;
  - PREM24: son 96 kapanmış 15 dakikalık bar'da `mark/index − 1` ortalaması, ≥ 86 bar.
- Eksik feature'lı örnek fit'ten atılır; eksik feature'lı varlığa tahmin yapılmaz.
- Önce %1/%99 winsorize, sonra fit örneklemiyle z-skor.
- Ridge: `(1/n)Σ(y − a − Σ b_j z_j)² + 0.01·Σ b_j²`, kesişim cezasız.
- Kararlarda ve gate'lerde kullanılmaz.

**S-H2-3 Validity gate** (her config için kendi ufku ve kendi uygunluk kuralı ile; yalnız değerlendirme verisinde).

```
uygunluk(s): H3 → S-H3-1 ; H1 → S-H1-1 + V5 gate'leri  (s'de PIT)
E = {s günlük : s pencere içinde, s+h ≤ pencere sonu}
her s ∈ E:
   F̂(s) = s anında yapılmış PIT tahmin (refit değil)
   H3 (k, h=168): N_e(s) uygun; k'(s) = min(k, ⌊N_e(s)/2⌋); k'(s) < 3 ise s atlanır
        T = top-k'(s), B = bottom-k'(s) (F̂'ye göre)
        FS(s) = mean F̂(T) − mean F̂(B) ; RS(s) = mean y(T) − mean y(B)
   H1 (h=720): n⁺ = #{uygun, F̂ > 0}; n⁺ = 0 ise s atlanır; T = top-min(3, n⁺)
        FS(s) = Σ_T F̂ ; RS(s) = Σ_T y
y(i,s) = Y_i(s,h)                          dönemlerin ≥ %90'ı bilinir
       = bilinen dönemlerin ortalaması      %50–%90 bilinir
       = min(0, F̂_i)  i ∈ T                 < %50 bilinir
       = max(0, F̂_i)  i ∈ B                 < %50 bilinir (H1'de T için min(0, F̂_i))
       (< %50 durumlarının sayısı raporlanır)
R = Σ_{s∈E'} RS / Σ_{s∈E'} FS (E' = atlanmayan tarihler) ; Σ FS ≤ 1e−9 ise INCONCLUSIVE
c = OLS eğimi y ~ F̂ ; örnek = s ∈ E' için (i,s), i uygun, y'nin ≥ %50'si bilinen
    (kesitsel demean edilmiş eğim yalnız raporlanır)
CI: E' tarihleri üzerinde moving block bootstrap, B = 10000, seed 20261010,
    blok 14 gün (h=168) / 30 gün (h=720); R ve c AYNI yeniden örneklemelerle;
    iki taraflı yüzdelik 2.5 / 97.5
min_evidence: h=168 → |E'| ≥ 60 ; h=720 → ⌊(s_son − s_ilk)/30g⌋ + 1 ≥ 8 (E' üzerinde)
INVALID      ⇔ min_evidence ∧ ( CI(R) ⊂ (−∞, 0.8) ∨ CI(R) ⊂ (1.25, ∞) ∨ CI_üst(c) ≤ 0 )
VALID        ⇔ min_evidence ∧ ¬INVALID ∧ 0.8 ≤ R ≤ 1.25 ∧ CI_alt(c) > 0
INCONCLUSIVE ⇔ diğer her durum
```

- `y` kırpılmaz.
- **INVALID** → **MODEL INVALID / NO-GO** (P&L ne olursa olsun).
- **INCONCLUSIVE** → ADVANCE ya da FINAL GO yok.
- **P12** = VALID. Aralık [0.8, 1.25].

---

## 5. H3 — Risk bütçeli perp-perp kitap (S-H3)

**S-H3-1 Uygunluk.** Bir varlık uygun sayılır ancak:
- V5 gate'leri sağlanmışsa: karar penceresi spread ≤ 15 bps, 24 saatlik hacim ≥ 1M$, funding kapsamı ≥ %90, en yeni funding ≤ 3 saat;
- H2a(168) tahmini mevcutsa;
- ≥ 60 gün mark geçmişi varsa.

`N_e` = uygun varlık sayısı.

**S-H3-2 Sinyal.**
- `s_i = F̂_i(t,168)`; `m_t` = uygunlarda medyan.
- `c_i = −side·(s_i − m_t)`.
- `cost_i` = taker fee + `t`'de bilinen half spread + slip (tek yön).

**S-H3-3 Bölgeler.**
- `k' = min(k, ⌊N_e/2⌋)`; `k' < 3` → uygulanamaz (S-H3-12).
- `r = min(2, ⌊N_e/2⌋ − k')`.
- Sıralama `s` azalan (eşitlikte ad). Short bölgesi ilk `k'`, long bölgesi son `k'`. Tutma bölgeleri ilk ve son `k' + r`.

**S-H3-4 Tutma ve çıkış (günlük 00:00).** Tutulan bacak, tutma bölgesinde ve `c_i·168 ≥ −cost_i` ise kalır; aksi halde çıkılır. Çıkışlar girişlerden önce işlenir.

**S-H3-5 Giriş.** Boş slotlar kendi bölgesinde sıra sırasıyla denenir; aday `x` yalnız `c_x·168 > 2·cost_x` ise girer. Bölge biterse slot boş kalır.

**S-H3-6 Risk modeli** (yalnız `t` öncesi kapanmış bar'lar; saatlik log getiriler).
- **Vol:** `σ_i` = EWMA vol, yarı ömür 336 saat. Başlatma: ilk 30 günün örnek varyansı. Yıllıklaştırma √8760.
- **Kovaryans:**
  - örnek = son 60 gün; varlık kümesi = uygun + tutulan + BTC;
  - korelasyon matrisi standardize getirilerden hesaplanır;
  - Ledoit–Wolf (2004) constant-correlation hedefi, analitik `δ*` ile küçültülür (standardize getiriler üzerinde, [0, 1]'e kırpılır) → `ρ̃`;
  - `Σ_ij = ρ̃_ij σ_i σ_j`.
- **Beta:** `β_i = Σ_i,BTC / Σ_BTC,BTC`.

**S-H3-7 Kitap inşası (deterministik).**

```
build(L, S):
  loop (en fazla |L|+|S| kez):
    |L| < 3 veya |S| < 3 → INFEASIBLE
    u_i  = (1/σ_i)/Σ_{j∈taraf}(1/σ_j)
    σ̃_L = sqrt(u_Lᵀ Σ_LL u_L) ; σ̃_S = sqrt(u_Sᵀ Σ_SS u_S)
    φ_L = σ̃_S/(σ̃_L+σ̃_S) ; φ_S = σ̃_L/(σ̃_L+σ̃_S)                       # risk paritesi
    |φ_L − φ_S| > 0.2 ise: büyük = 0.6, küçük = 0.4                       # dolar sınırı
    q = (φ_L σ̃_L)/(φ_S σ̃_S)                                           # standalone taraf oranı
    q > 1.5 ise: φ_L ← φ_L·1.5/q ;  q < 1/1.5 ise: φ_S ← φ_S·1.5·q        # yalnız büyük-risk tarafı küçülür
    |φ_L − φ_S| > 0.2·(φ_L+φ_S) + 1e−12 ise → INFEASIBLE
    w⁰ = (+φ_L u_L, −φ_S u_S)
    G  = min(1/(φ_L+φ_S), 0.08 / sqrt(w⁰ᵀ Σ w⁰))                        # gross ≤ 1, σ* = %8
    w  = G·w⁰
    β_net = Σ w_i β_i ; |β_net| > 0.05 ise w ← w·0.05/|β_net|            # bütün kitap küçültme
    n_i = w_i·E, qty adımına sıfıra doğru yuvarlanır (adım = max(step, min_qty))
    küçük = {i : |n_i| < max(min_qty_i·mid_i, 10$)}
    küçük boşsa → çık
    küçükte |n_i| en küçük olan (eşitlikte ad) düşer; sıfırdan tekrar
  yuvarlama sonrası kontroller (S-H3-9 ve S-H3-10 aynı üçünü kullanır):
     |Σ n_i β_i| ≤ 0.05·E ;  |Σ_L n − Σ_S |n|| ≤ 0.2·Σ|n| + max adım notional'ı ;
     q (gerçekleşen n ile hesaplanır) ∈ [1/1.5, 1.5]
  sağlanmazsa → INFEASIBLE
```

**Not (oran kuralının etkisi):** Dolar sınırı bağladığında φ tam 0.6 / 0.4'tür; büyük-risk tarafını daha fazla küçültmek dolar kontrolünü bozar. Bu yüzden oran kuralı pratikte "`σ̃_L/σ̃_S > 2.25` ya da `< 1/2.25` ise INFEASIBLE" anlamına gelir. Dev risk girdilerinde hiç bağlamazdı (en kötü q = 1.28).

**S-H3-8 Risk raporu** (her karar anında; gate değil). Euler katkıları `RC_L` ve `RC_S`; standalone oran `q`; `β_net`; net dolar; dolar sınırının ve oran kuralının bağlayıp bağlamadığı. Bunlar zaman ortalaması, p95 ve maksimumla raporlanır.

**S-H3-9 Hedefe uygulama.**
- Hedefi 0 olan tutulan bacaktan çıkılır.
- Tutulan bacak yalnız `|n_hedef − n_tutulan| > 0.25·|n_hedef|` ise yeniden boyutlanır. Büyütme V6'ya özel, testli bir R10 istisnasıyla yapılır; küçültme reduce-only.
- Bant sonrası gerçekleşen kitap S-H3-7'nin yuvarlama sonrası kontrollerini sağlamazsa **bütün bacaklar hedefe çekilir** (band yok sayılır). Hâlâ sağlanmazsa INFEASIBLE.

**S-H3-10 Gün içi izleme (15 dakika).**
- Bacak stop'u: girişe göre %15 ters hareket → çıkış ve 24 saat cooldown. Ayrıca R3/R4 ve V5 ineligibility çıkışları.
- **Herhangi bir gün içi çıkıştan sonra**, bir sonraki 15 dakikalık anda gerçekleşen kitapta sırasıyla, hepsi reduce-only:
  1. `|β_net| > 0.05·E` ise her bacak `0.05·E/|β_net|` ile çarpılır.
  2. Net dolar ya da `q` kontrolü başarısızsa taraf küçültme oranı `a_L/a_S` seçilir. Taraf dolarları `D_L, D_S`, standalone riskleri `R_L, R_S` ile:
     - net dolar kontrolü `a_L/a_S ∈ [2/3·D_S/D_L, 1.5·D_S/D_L]` ister;
     - `q` kontrolü `a_L/a_S ∈ [2/3·R_S/R_L, 1.5·R_S/R_L]` ister.
     İki aralığın kesişiminden 1'e en yakın oran seçilir ve **yalnız küçültmeyle** uygulanır (bir taraf küçülür, hiçbiri büyümez). Kesişim boşsa bütün bacaklardan çıkılır.
  3. Sonra (1) bir kez daha uygulanır.
  4. Her adımdan sonra `max(min_qty·mid, 10$)` altına düşen bacaklardan çıkılır ve (1)–(3) yeniden uygulanır; en fazla bacak sayısı kadar tekrar. Kontroller hâlâ sağlanmıyorsa ya da herhangi bir tarafta < 3 bacak kalırsa bütün bacaklardan çıkılır.
- 00:00 inşası (S-H3-7) her gün çalışır; gün içi durumdan etkilenmez. Başarılı bir inşa önceki INFEASIBLE durumunu temizler.

**S-H3-11 Execution.**
- **Tarihsel:** V5 DelayedExecutor (t + 15 dakika, S-COST).
- **Forward:** Kararın ardından ilk fut_book snapshot'ında taker; book seviyeleri yürünür.

**S-H3-12 Uygulanamaz kitap.** Tetikleyiciler: `k' < 3`, model yok, ya da INFEASIBLE.
- 24 saate kadar: mevcut bacaklar tutulur; giriş ve büyütme yok; stop, zorunlu çıkışlar ve S-H3-10 küçültmesi aktif.
- 00:00 inşası yine çalışır; başarılı olursa INFEASIBLE durumu temizlenir ve sayaç sıfırlanır.
- 24 saatten uzun kesintisiz durumda: bütün bacaklardan çıkılır.

**S-H3-13 Varyantlar.** H3-k4, H3-k6.

---

## 6. H1 — Hedge'li carry (S-H1)

**S-H1-1 Evren.** 20 varlıktan Kraken spot USD paritesi olanlar; spot 24 saatlik hacim ≥ 1M$.

**S-H1-2 Sermaye.**
- Qty = ⌊N / px⌋, `max(spot adımı, perp adımı)`'na yuvarlanır. İki bacağın qty'si eşittir ve ikisi de kendi minimumunun üstündedir; değilse giriş yok.
- Spot tam fonlanır; perp teminatı = perp notional (1×).
- Boyut: varlık başına hedge'li notional ≤ `E/6`, en fazla 3 varlık, ve `2N + giriş maliyetleri ≤ mevcut nakit`.
- Tarihsel: bir bacak fill alamazsa ikisi de fill almaz (atomik).

**S-H1-3 Giriş (günlük 00:00).**
- Koşullar: varlık tutulmuyor ve cooldown'da değil; `F̂_i(t,720) > 0`; `F̂_i·720 ≥ m·RT_i`.
- `RT_i` = `t`'de bilinen all-in taker round trip: iki bacak, giriş ve çıkış.
- Adaylar `F̂`'ye göre azalan sırada. **m ∈ {1.5, 2.5}.**

**S-H1-4 Çıkış** (iki bacak birlikte), şunlardan biri olunca:
- günlük 00:00'da `F̂_i(t,720)·720 < RT_i/2` (ekonomik çıkış; rev 2 tasarımı, değiştirilmedi);
- birleşik stop: spot+perp P&L ≤ −%15 × hedge'li notional;
- hedge reset: spot mid girişten ±%40;
- R4;
- ineligibility.

Çıkıştan sonra o varlıkta 24 saat cooldown.

**S-H1-5 Tarihsel execution.** İki bacak taker, t + 15 dakika.

**S-H1-6 Satırlar.**

| Satır | Tanım | Rol |
|---|---|---|
| S-MAKER-BOUND (yalnız tarihsel) | PRIMARY ile aynı kararlar; fill = t + 15 dakikadaki mid; maker/maker fee; spread ve slip 0; %100 fill | Yalnız NO-GO zorlar ve FEAS'ı belirler |
| S-COST2X | Tarihsel: bütün fee, spread varsayımları ve slip ×2. Forward: `net − 1 × (gerçekleşen bütün fee + spread/slip)` | Karar taşır (P7) |
| S-PIT10 | Tarihsel: perp spread snapshot'ı T + 10 dakikada bilinir. Forward: kararlar yalnız `recv_ts ≤ t − 5 dk` olan snapshot'larla | Karar taşır (P11) |
| S-FB-CONS (yalnız tarihsel) | Perp spread fallback'i = değerlendirme başlangıcından önceki 35 günün varlık başına p90'ı | Karar taşır (P11); forward'da uygulanmaz |
| S-MIXED, S-SPOTSPREAD2X, S-FEE-ACTUAL | Spot maker + perp taker; spot spread ×2; doğrulanmış hesap fee'leri | Bilgi |

H3 için de S-COST2X, S-PIT10 ve S-FB-CONS aynı tanımlarla karar taşır.

**Forward PRIMARY:** H1 için S-H1-X; H3 için S-H3-11.

**S-H1-X Forward execution.**

| Durum | Kural |
|---|---|
| Referans | Karar anındaki son spot_ticker; ≤ 180 saniye eski olmalı. Değilse ilk taze ticker beklenir (en fazla 6 saat; aşılırsa giriş atlanır) |
| Gönderim zamanı | `t_sub` = taze referans ticker'ın zamanı. Bütün pencereler `t_sub`'dan başlar |
| Hemen marketable | `t_sub` sonrası ilk spot_ticker'da ask ≤ limit (alış) → taker fill: zaman damgası ≤ o ticker olan son spot_depth'te **yalnız limit ve altındaki** ask seviyeleri yürünür; kalan qty maker emir olarak bekler |
| Spot giriş (maker) | Referans best bid'ine limit emir |
| Maker fill | `(t_sub, t_sub + 60 dk]` içinde bir spot_ticker'da best ask ≤ limit **ve** zaman damgası ≤ o ticker olan en son spot_depth'te bu fiyat ve altındaki ask hacmi → o hacim kadar fill, limit fiyattan. **Her depth snapshot'ının bir fiyat seviyesindeki hacmi en fazla bir kez tüketilir** |
| Taker fallback | `t_sub + 60 dk`'da kalan qty: zaman damgası ≤ `t_sub + 60 dk` olan son spot_depth'in ask'leri yürünür (VWAP + taker fee); derinlik yetmezse kalan iptal edilir |
| Perp hedge | Spot fill kesinleşince ilk fut_book snapshot'ında taker short; qty = dolan spot qty |
| Çıkış | **Yalnız 00:00 ekonomik çıkışı maker-first** (simetrik kurallar). Risk çıkışları (birleşik stop, hedge reset, R4, ineligibility) ilk snapshot'ta iki bacak taker |
| Bacaklar arası gecikme | Hedge'siz P&L episode'a yazılır ve birleşik stop'a sayılır |
| Collector boşluğu | Pencerede snapshot yoksa maker dolmamış sayılır; taker fallback ilk mevcut snapshot'ta. Karar anında ≥ 6 saatlik boşluk → giriş atlanır |

**S-H1-F Feasibility yolu.**

```
FEAS üyeliği (holdout sonucunda bir kez):
  c ∈ FEAS ⇔ c ∈ H1 ∧ INVALID RUN yok ∧ MODEL INVALID yok ∧ P10 = 0
           ∧ PRIMARY (P1 ≤ 0 ∨ P3 ≤ 0) ∧ S-MAKER-BOUND (P1 > 0 ∧ P3 > 0 ∧ P5 ∧ P6)
  (P4, P7, P8, P9, P11 ve P2 başarısızlıkları raporlanır ama FEAS'ı dışlamaz)
Ölçüm replay'i M: forward gün 1–90, S-H1-X, S-H1-3/4 kuralları, başlangıç 265$ düz
  M-a = maker olarak dolan spot notional / toplam spot notional (giriş + çıkış)
  M-b = Σ_e (fee_e + spreadslip_e − gecikmeP&L_e) / Σ_e N_e^giriş
        (giriş + çıkış; gün 90'da açık episode'un çıkışı = gün 90 varsayımsal taker unwind:
         zaman damgası ≤ gün 90 olan son spot_depth ve fut_book yürünür)
  M-c = M'nin P1 ve P3 nokta değerleri (terminal unwind dahil)
  ESTABLISHED ⇔ M-a ≥ 0.50 ∧ M-b ≤ 0.0125 ∧ M-c.P1 > 0 ∧ M-c.P3 > 0 ∧ #episode ≥ 3
Confirmation replay'i C (yalnız ESTABLISHED ise): forward gün 91–365, ayrı koşu,
  başlangıç 265$ ve düz; M'den pozisyon, nakit, P&L, cooldown ya da stop state'i taşınmaz;
  H2a fit'i yalnız funding verisiyle; execution modeli freeze'deki gibi (M'den öğrenilmez).
  Bütün kriterler ve kanıt gate'i yalnız C üzerinde.
```

C'nin düz başlaması, gün 91'de taze sermayeyle başlayan bir hesaba denktir; giriş maliyetleri C içinde ödenir. M yalnız M-a, M-b ve M-c üretir.

**S-H1-7 Varyantlar.** H1-m1.5, H1-m2.5.

---

## 7. Kriterler (S-CRIT)

**S-CRIT-P Mutlak kriterler.** PRIMARY, terminal unwind dahil.

| # | Kriter | Eşik |
|---|---|---|
| P1 | Toplam net | > 0 |
| P2 | İstatistik | S-STAT ile reddedilmiş **ve** düzeltmesiz LB95 > 0 (net ve carry; b = 14 ve b = 28) |
| P3 | Toplam carry | > 0 |
| P4 | Sharpe | ≥ 1.0 |
| P5 | MaxDD | ≤ %10 |
| P6 | En kötü gün / ES97.5 | ≥ −%3 / ≥ −%2 |
| P7 | S-COST2X net | ≥ 0 |
| P8 | Konsantrasyon | S-CRIT-C |
| P9 | Zaman kararlılığı | S-CRIT-T |
| P10 | Kill / likidasyon | = 0 |
| P11 | S-PIT10 ve S-FB-CONS (forward'da yalnız S-PIT10) | P1 ve P3'ün işareti değişmez |
| P12 | H2a gate | VALID |

**S-CRIT-E Kanıt gate'i.**
- **Aktif gün:** ≥ 12 saat boyunca gross ≥ %10·E olan gün. H1'de gross = hedge'li perp notional.
- **Episode:** Bir varlıktaki tutma dönemi; aynı varlıkta ≥ 7 gün aradan sonraki tutma yeni episode'dur.
- **Cluster:** Girişler kronolojik sırayla açgözlü gruplanır; yeni cluster, mevcut cluster'ın ilk girişinden ≥ 7 gün sonraki ilk girişte başlar.
- **Eşikler:**
  - H1: aktif gün ≥ 60, episode ≥ 3, cluster ≥ 3, underlying ≥ 3.
  - H3: aktif gün ≥ 60, episode ≥ 10, cluster ≥ 4, underlying ≥ 6.
- Sağlanmazsa → INSUFFICIENT.

**S-CRIT-C Konsantrasyon** (yalnız P1 geçtiyse; `C_a` = varlığa atfedilen net, `C⁺_a = max(C_a, 0)`).

| Kontrol | Kural | Değerlendirilemezse |
|---|---|---|
| P8a | `max_a C_a ≤ 0.5·net` | `C_a ≠ 0` olan < 3 varlık → INCONCLUSIVE |
| P8b | ∀a: `net − C_a > 0` | — |
| P8c | `N_eff = (ΣC⁺)²/Σ(C⁺)²`; H1 ≥ 2, H3 ≥ 4 | — |
| P8d | Kapanmış episode'ların net'i ≤ 0 → başarısız; değilse en iyi ⌈0.2·n⌉ kapanmış episode'un net'i ≤ 0.5 × kapanmış net | < 10 kapanmış episode → INCONCLUSIVE |

Değerlendirilebilir başarısızlık → NO-GO.

**S-CRIT-T Zaman kararlılığı.**
- Pencerenin `n` günü 3 alt pencereye bölünür: ilk ikisi ⌊n/3⌋ gün, kalan son pencereye. `P&L_j / E_koşu başlangıcı` hesaplanır.
- **Taban (her alt pencere):** ≥ −%3, yoksa NO-GO.
- **Değerlendirilebilir:** aktif gün ≥ max(15, ⌈0.25·gün⌉).
- **Geçer:** ≥ 2 değerlendirilebilir pencere ve en az 2'sinde net > 0 (tam 2 varsa ikisi de).
- < 2 değerlendirilebilir → INCONCLUSIVE. Aksi halde NO-GO.

---

## 8. İstatistik prosedürü (S-STAT)

**S-STAT-P p-değerleri.**

```
p_mbb(x, b, B) = V5 stats.mbb(x, b, B, seed=20261010).p    # tek taraflı, H0: ortalama ≤ 0
B = 10000 (holdout) ; B = 200000 (forward bakışları)
p_net   = max(p_mbb(net, 14, B),   p_mbb(net, 28, B))
p_carry = max(p_mbb(carry, 14, B), p_mbb(carry, 28, B))
p_GO    = max(p_net, p_carry)          # intersection–union; H0 = {μ_net ≤ 0 ∨ μ_carry ≤ 0}
```

**S-STAT-H Holm (tek tanım).**

```
holm(p[1..m], α):
    sırala p_(1) ≤ … ≤ p_(m) (eşitlikte config adı)
    j = 1..m: p_(j) ≤ α/(m − j + 1) ise reddet, değilse dur
```

**S-STAT-O Holdout (tek açılış).**

```
FAMILY = [H3-k4, H3-k6, H1-m1.5, H1-m2.5]    # m = 4
her c: PRIMARY ve S-H1-6 satırları; p_GO(c)
R = holm(p_GO, 0.05)
class(c) = ladder(c, stat = (c ∈ R ∧ LB koşulu), ortam = holdout)
referanslar (hükümsüz): cash (0) ve değişmeden koşulan V5 W/E
```

**S-STAT-L Karar merdiveni.**
- Bütün kontroller hesaplanır; sonuç tetiklenen en ağır sınıftır ve bütün nedenler raporlanır.
- Sıralama: `INVALID RUN > MODEL INVALID > NO-GO > {MODEL INCONCLUSIVE, INSUFFICIENT, INCONCLUSIVE} > ADVANCE / FINAL GO`.
- NO-GO-PRIMARY, NO-GO sınıfında **ayrı bir etiket** olarak raporlanır (S-H1-F FEAS üyeliği yalnız S-H1-F tanımıyla belirlenir).

| Kontrol | Sınıf |
|---|---|
| Veri bütünlüğü ya da guard | INVALID RUN |
| H2a gate INVALID | MODEL INVALID |
| P10 | NO-GO |
| P1 ya da P3 ≤ 0 | NO-GO. H1'de S-MAKER-BOUND'da P1 ve P3 > 0 ise etiket NO-GO-PRIMARY |
| P4, P5, P6, P7, P11; değerlendirilebilir P8 ya da P9 başarısız | NO-GO |
| P8 ya da P9 değerlendirilemez | INCONCLUSIVE |
| S-CRIT-E | INSUFFICIENT |
| H2a gate INCONCLUSIVE | MODEL INCONCLUSIVE |
| P2 | INCONCLUSIVE |
| Forward bakış coverage'ı < %80 | INCONCLUSIVE |
| Hiçbiri | Holdout: ADVANCE (holdout'ta FINAL GO yok). Forward: FINAL GO |

**S-STAT-C Forward aile (sabit).**

```
FAMILY_F = [H3-k4, H3-k6, H1-m1.5, H1-m2.5] ; m_F = 4 ; hiçbir zaman değişmez
CAND = { c : holdout'ta nedenleri arasında INVALID RUN, NO-GO ya da MODEL INVALID yok ∧ P1 > 0 ∧ P3 > 0 }
FEAS = S-H1-F tanımı
c ∉ CAND ∪ FEAS → her bakışta p[c] = 1 (status = CLOSED)
FAMILY_F'nin üyelik durumu holdout kararında bir kez belirlenir.
Holdout kararı bir bakış tarihinden sonra gelirse o bakış karar anında,
bakış tarihine kadarki veriyle hesaplanır.
```

**S-STAT-F Forward sıralı test (tek ve değişmez algoritma).**

```
LOOKS = [(90, 0.001), (180, 0.001), (365, 0.048)]
her (gün, α) ∈ LOOKS:
    her c ∈ FAMILY_F:
        if   status[c] ∈ {CLOSED, FINAL GO}:      p[c] = 1   (yalnız Holm için; FINAL GO'nun p'si raporlanır)
        elif c ∈ FEAS:
             if gün = 90: ölçüm M; NOT ESTABLISHED → status = CLOSED
             if gün = 365 ∧ ESTABLISHED: p[c] = p_GO(c, C, gün 91–365)
             else: p[c] = 1
        elif c ∈ CAND:                            p[c] = p_GO(c, forward gün 1..gün)
        else:                                     p[c] = 1
    R = holm(p[FAMILY_F], α)                       # payda her zaman 4
    ara bakışlarda (gün 90, 180) ek koşul: c ∈ R ise aynı serilerde Newey–West t-testi
        (Bartlett çekirdeği, lag 28, tek taraflı, referans dağılım N(0,1)) hem net hem carry için
        p ≤ c'nin Holm eşiği; değilse c R'den çıkar
    her c (status = PENDING):
        L = ladder(c, stat = (c ∈ R ∧ LB koşulu), pencere = c'nin penceresi)
        L = FINAL GO             → status = FINAL GO (kalıcı)
        L'de MODEL INVALID ya da P10 → status = CLOSED
        gün = 365 ∧ status = PENDING → status = L'nin sınıfı
FINAL GO sonrası izleme: P5, P6 ya da P10 ihlali → GO REVOKED (risk); yeniden test yok
```

**S-STAT-T Aile hatası.**
- Her bakış `ℓ`'de payda sabit (4) ve p-değerleri geçerliyken Holm, gerçek null'lar için yanlış red olasılığını `α_ℓ` ile sınırlar. Test edilmeyen ya da kapanmış slot `p = 1` olur.
- FINAL GO, en az bir bakışta Holm reddi gerektirir. Bu yüzden `P(en az bir yanlış FINAL GO) ≤ Σ_ℓ α_ℓ = 0.050`.
- Bu sınır **p-değerlerinin (asimptotik) geçerli olması koşuluyla** geçerlidir. Ara bakışlardaki çok küçük eşikler (ilk adım 2.5e-4) için bootstrap kuyruğuna ek olarak Newey–West koşulu ve B = 200000 kullanılır; ikisi de yalnız muhafazakârlaştırır.
- **FEAS:** H1 için `P(ESTABLISHED ∧ C yanlış red) ≤ P(C yanlış red)`; M ile C örtüşmez.

**S-STAT-X Terminal kural.** Gün 365'te FINAL GO almayan config'ler kapanır; H1, H2 ve H3 aynı veri üzerinde yeniden spec'lenmez.

---

## 9. Mekanik fizibilite ve güç (bilgi; karar kuralı değil)

**P&L'siz H3 boyut kontrolü** (`scripts/v6_feasibility/h3_feasibility.py`; çıktılar `docs/results/v6-feasibility`; yalnız < 2026-07-01 risk girdileri; seçim FL30 sırasıyla, çünkü H2a sırası `b̂ > 0` iken aynıdır):

| | k = 4, 265$ | k = 6, 265$ |
|---|---|---|
| Uygulanabilir gün | %90.4 | %100 |
| Medyan gross / E | 0.455 | 0.594 |
| Medyan bacak | ~15$ | ~15$ (medyan 10 bacak) |
| Standalone taraf oranı (medyan / p95 / maks) | 1.0 / 1.13 / 1.28 | 1.0 / 1.02 / 1.17 |
| Dolar sınırının bağladığı gün | %26 | %7 |
| \|β_net\| p95 | 0.047 | 0.047 |

**Yaklaşımlar:** Spread ve hacim gate'leri yok sayıldı; LW yoğunluğu basitleştirildi (yalnız köşegen π). Gerçek uygulamada uygulanabilir gün oranı daha düşük olabilir.

**Güç:**
- LB95 > 0 için gereken Sharpe 100 günde ~3.8 (Holm'da ilk config için ~5), 1 yılda ~2.0.
- H3'ün ex-ante maliyet öncesi Sharpe'ı ≈ 0.3.
- H1 holdout'ta en fazla MODEL INCONCLUSIVE alır.
- Kriterler gevşetilmez.

---

## 10. Süreç

1. Exact spec.
2. Bağımsız red-team.
3. Düzeltmeler ve taze doğrulama.
4. **Spec freeze:** temiz commit, tag `v6-profitability-spec-frozen`; spec, sabitler, fee snapshot ve red-team raporu sha256'ları; zaman damgası.
5. Implementasyon (TDD).
6. Kör dev invariant koşuları.
7. Bağımsız implementasyon review'u.
8. **Kod freeze:** tag `v6-code-frozen`, implementasyon review onayından en az 24 saat sonraki ilk 00:00 UTC'de atılır; bu an **T_F**'dir.
9. Kullanıcının açık onayıyla tek holdout açılışı.
10. Forward bakışları (S-STAT-F).

**R9 bug kuralı:** V5'teki gibi aynen. **Uygulanamazlık bug değildir** (D8).
