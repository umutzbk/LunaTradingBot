# V6 Profitability — Exact Spec

**Durum:** TASLAK (freeze edilmedi). Holdout kapalı; V6 backtest ya da outcome yok.

**Kaynaklar:**
- Plan rev 2 (`luna-v6-profitability-research-plan.md`, commit `25ee363`). Bu spec onun normatif, tek anlamlı halidir.
- Sabitler `luna-v6-frozen-constants.json` dosyasındadır. Bu metinle çelişen bir sabit varsa freeze geçersizdir.

**Hedef:** Maliyetler sonrası, risk kontrollü ve tekrar edilebilir **mutlak** net pozitif getiri.

Kurallar `S-` numaralarıyla anılır. "Must" yerine yalın Türkçe kullanılır: "yapılır" = zorunlu.

---

## 0. Rev 2'den sapmalar (hepsi outcome görülmeden; gerekçeli)

| # | Sapma | Gerekçe |
|---|---|---|
| D1 | **H3 taraf bölüşü:** beta-nötr bölüş yerine **basket-vol risk paritesi** + dolar sınırı + **inşa sonrası \|net β\| ≤ 0.05·equity** sert limiti (aşılırsa ölçek küçültme, hâlâ olmuyorsa INFEASIBLE). Ayrıca ex-ante long/short risk katkıları raporlanır (S-H3-7…10) | Rev 2'nin H3 amacı V5'teki 2–2.5× long/short vol dengesizliğini çözmekti (plan §1.7). Taraf içi inverse-vol her bacağın tek başına riskini eşitler, ama tarafların toplam riskini eşitlemez. Beta-nötr bölüşte taraf risk oranı ≈ `(g_L/g_S)·(σ̄_L/σ̄_S)`. V5'teki tipik değerlerle (β 1.34/1.17, vol 1.5/0.6) bu oran ≈ 0.87 × 2.3 ≈ **2.0** olur; dengesizlik kalır. Risk paritesi oranı 1'e getirir; dolar sınırı bağlarsa kalan oran raporlanır. Beta, kullanıcının istediği sert limitle kontrol edilir |
| D2 | **Forward son bakış:** α_365 0.05 yerine **0.048** | Bakışlar üzerinden Bonferroni: 0.001 + 0.001 + 0.048 = 0.050. Aile hatası tam 0.05 ile sınırlanır (S-STAT-F) |
| D3 | **Ara bakışlar** (90, 180) yalnız FINAL GO verebilir ya da kesin olaylarla kapatabilir (kill, likidasyon, MODEL INVALID). Nokta tahminlerine göre futility kapatması yok | Gürültülü nokta tahminiyle erken kapatma, karar kuralını veriye bağlı hale getirir; tek ve değişmez algoritma için gerekli |
| D4 | **H1 feasibility izolasyonu:** gün 91–365 confirmation'ı ayrı bir replay'dir: düz başlar, 265$ taze sermaye, gün 1–90'dan hiçbir state ya da P&L taşınmaz (S-H1-F) | Kullanıcı maddesi D |
| D5 | **Fee kaynağı:** resmi URL, erişim zamanı ve sayfa hash'i | Kullanıcı maddesi A. Sayfadaki 0.38% maker oranı yalnız seçili pariteler için bir "spot rebate" programına ait; primary değildir |
| D6 | **Holdout bitişi** tam `collector_start_ts` = 2026-10-09T11:52:48.861565107Z | Rev 2'de yaklaşıktı |

H1, H2 ve H3'ün geri kalan tasarımı ve profitability kriterleri rev 2 ile aynıdır.

---

## 1. Veri, zaman ve guard'lar

**S-DATA-1 Funding.**
- `f_i(p)`: Kraken PF saatlik göreli funding'i. `p` dönemin başlangıcıdır; değer `p + 1h`'de bilinir.
- Pozitif değerde longlar short'lara öder.
- Tarihsel kaynak: frozen kopya (`var/history_v5/frozen`) ve holdout için açılışta indirilen funding.
- Forward kaynak: collector `fut_funding`.

**S-DATA-2 Perp fiyat ve spread.**
- Mark: 15 dakikalık bar'lar. `t` anındaki mid = `t − 15m`'de açılan bar'ın kapanışı (V5 §3.1).
- Spread: Kraken analytics interval = 300, V5 rev 3 kuralları aynen:
  - snapshot `T + 5m`'de bilinir;
  - karar penceresi `[t − 1h, t)` içinde ≥ 6/12 geçerli snapshot;
  - fill penceresi `[t, t + 15m)` içinde ≥ 2/3;
  - geçerli değer: sonlu, > 0 ve ≤ 500 bps;
  - fallback 10 bps.

**S-DATA-3 Spot (tarihsel).**
- Spot mid vekili: `t − 15m`'de açılan kraken_spot bar'ının kapanışı.
- Fill fiyatı `t + 15m`'de, `t`'de açılan bar'ın kapanışından; spread kuralı S-COST-2.
- Spot 24 saatlik hacim = son 96 bar'ın `volume × close` toplamı.

**S-DATA-4 Forward.**
- Kaynak: collector'ın fut_tickers, fut_book, fut_funding, spot_ticker (60 saniye) ve spot_depth (5 dakika, 10 seviye) verisi.
- **Evidence başlangıcı:** `T_F` = freeze zaman damgası. Forward değerlendirmesi yalnız `recv_ts ≥ T_F` olan snapshot'ları kullanır. Forward gün 1, `T_F` anında ya da sonrasındaki ilk 00:00 UTC'dir.
- H2a fit'i, `T_F` öncesi funding **verisini** kullanabilir (S-H2-1); bu bir outcome değildir.

**S-DATA-5 Coverage.**
- Bir gün, 15 dakikalık izleme anlarının ≥ %90'ında taze (≤ 180 saniye) ticker snapshot'ı varsa **değerlendirilebilir** sayılır.
- Eksik anlarda strateji emir göndermez; stop'lar ve çıkışlar ilk mevcut snapshot'ta işler.
- Veri geri geldiğinde pozisyonlar mark edilir ve P&L o güne yazılır.
- Bir bakışta değerlendirilebilir gün oranı < %80 ise o bakış her config için INCONCLUSIVE olur.
- **Veri kaybı asla doldurulmaz;** interpolasyon ya da uydurma snapshot yok.

**S-GUARD-1 Holdout.**
- V6 yükleyicileri, zaman damgası ≥ 2026-07-01T00:00Z ve < `T_F` olan her satırı reddeder. İstisna: holdout yetki token'ı. Token, V5 `outcome_guard` deseniyle yalnız şu koşullarla üretilir:
  - tag ve temiz ağaç;
  - spec ve sabitler sha'sı;
  - kullanıcının açık onay cümlesi;
  - iki exclusive tek-açılış marker'ı.
- Holdout tek koşuda, bir kez açılır.

**S-GUARD-2 Dev.**
- Dev döneminde (< 2026-07-01) V6 runner'ı **kör** çalışır.
- Yalnız invariant'lar yazılır: işlem/bacak sayıları, gross, inşa kısıtları, muhasebe kimlikleri, PIT testleri.
- P&L, Sharpe, carry ve kalibrasyon sütunları maskelidir. Maske kullanıcı onayı olmadan kalkmaz.

**S-GUARD-3 Forward maskesi.** Forward P&L ve metrikleri, holdout kararından sonraki ilk takvimli bakışa kadar maskelidir. Bakışlar arasında da yalnız takvimli bakışta açılır.

---

## 2. Maliyetler (S-COST)

**S-COST-1 Fee'ler (Kraken public Tier 1, conservative primary).**

| | Maker | Taker |
|---|---|---|
| Spot | 0.40% | 0.80% |
| Futures | 0.02% | 0.05% |

- **Kaynak:** https://www.kraken.com/features/fee-schedule, erişim 2026-10-12T01:12:31Z.
- **Snapshot:** `docs/results/v6-fee-source/kraken-fee-schedule-2026-10-12.html`. sha256 değeri freeze kaydındadır. İlgili tablolar ("Spot Crypto", "Futures") `excerpt.txt` dosyasında.
- **Hesaba özel tier:** Doğrulanmadıkça bu tablo primary'dir. Değişiklik yalnız freeze öncesi ve snapshot ile yapılabilir. Freeze sonrası gerçek fee'ler yalnız bilgi amaçlı **S-FEE-ACTUAL** satırında gösterilir.

**S-COST-2 Spread ve slip.**
- **Perp half spread:** S-DATA-2'deki karar ya da fill penceresi.
- **Spot half spread (tarihsel):** `max(2 × aynı pencerenin perp half spread'i, 5 bps)`.
- **Forward:** gözlenen book (S-H1-X, S-H3-X).
- **Slip:** her yön 2 bps.

**S-COST-3 Karar anı maliyeti.** Kararlardaki maliyet tahminleri yalnız `t`'de bilinen spread'i kullanır. Fill maliyeti fill anının spread'idir.

**S-COST-4 Fill fiyatı (tarihsel, taker).** `px = mid_fill·(1 ± half)·(1 ± slip)`, fee = `fee_rate·qty·px` (V5 DelayedExecutor). Maker yönü spread ve slip ödemez (yalnız S-MAKER-BOUND satırında).

---

## 3. Muhasebe (S-ACC)

**S-ACC-1 Equity.** `E_t` = USD nakit (perp teminatı dahil) + Σ spot qty × spot mid + Σ perp gerçekleşmemiş P&L (mark).

**S-ACC-2 Bileşenler.**
- **Funding:** perp pozisyonlarında, held-hours oranıyla settle edilen funding (V5 FundingLedger).
- **Fee'ler.**
- **Spread/slip:** fill fiyatı ile fill anındaki mid arasındaki fark.
- **Price/basis:** `ΔE − funding + fee + spread/slip` (kalan).
- **Carry:** `carry = funding − fee − spread/slip`.
- **Net:** `ΔE`.

**S-ACC-3 Günler.**
- Gün `d` = `[d 00:00, d+1 00:00)` UTC. Fill ve settlement'lar kendi zaman damgasının gününe yazılır.
- `E_d` = gün sonu (`d+1` 00:00) equity'si.
- Günlük seriler `net_d / E_{d−1}` ve `carry_d / E_{d−1}`; değerlendirme penceresinin **bütün takvim günleri**, düz günler dahil.

**S-ACC-4 Terminal unwind.**
- Değerlendirme penceresinin sonunda açık pozisyonlar, o andaki PRIMARY maliyetleriyle (spread, fee, slip; H1'de iki bacak) kapatılmış sayılır. Bu maliyet son güne yazılır ve PRIMARY sonuçlara dahildir.
- Forward ara bakışlarında unwind **varsayımsaldır**: yalnız değerlendirmede düşülür, pozisyonlar devam eder.

**S-ACC-5 Metrikler.**
- **Sharpe:** günlük net getirilerin `ortalama / std × √365` değeri.
- **MaxDD:** günlük equity üzerinden.
- **En kötü gün.**
- **ES97.5:** en kötü %2.5 günün ortalaması (V5 `stats`).

**S-ACC-6 Attribution.** Her fill ve settlement bir varlığa ve bir episode'a atanır. H1'de spot ve perp aynı episode'dur.

---

## 4. H2 — Tahmin (S-H2)

**S-H2-0 Contamination kuralı.**
- Estimator formu, feature'lar, hyperparameter'lar, pencereleme, regularisation, başlatma, minimum geçmiş, eksik-veri kuralı ve ufuklar bu spec'le freeze edilir.
- Her karar anı `t`'de fit ya da update yalnız `t` öncesi veriyle yapılır (PIT).
- Holdout ve forward koşularında fit'e dev dönemi gözlemleri girebilir.
- Hiçbir seçim outcome'a bakılarak yapılmaz.

**S-H2-1 H2a.**

```
X_i(t)   = mean{ f_i(p) : p ∈ [t−720h, t−1h], known } ; eksik eğer #known < 648
Y_i(s,h) = mean{ f_i(p) : p ∈ [s, s+h−1h], known }    ; eksik eğer #known < 0.9·h ; s+h'de bilinir
H        = {168, 720}
fit(t, h):
    D  = {(i,s) : s günlük 00:00 UTC, S0 ≤ s, s+h ≤ t, X_i(s) ve Y_i(s,h) mevcut}
    S0 = X'in ≥ 10 varlık için mevcut olduğu ilk günlük tarih
    #distinct(s in D) < 60 veya mean_s #{i : (i,s) ∈ D} < 10 ise → MODEL WARMUP
    (lo_X, hi_X) = quantile(X_D, [0.01, 0.99]); (lo_Y, hi_Y) = quantile(Y_D, [0.01, 0.99])
    â, b̂ = OLS( clip(Y_D, lo_Y, hi_Y) ~ 1 + clip(X_D, lo_X, hi_X) )   (eşit ağırlık)
predict(i, t, h) = â(t,h) + b̂(t,h)·clip(X_i(t), lo_X, hi_X)   ; X_i(t) eksikse tahmin yok
```

- Fit her gün 00:00 UTC'de yenilenir.
- Tahmini olmayan varlığa giriş yok. Tutulan pozisyonun tahmini 24 saatten uzun süre yoksa çıkış.

**S-H2-2 H2b (yalnız diagnostic).**
- Feature'lar ve minimum kapsamlar:
  - FL30: 720 saat, ≥ 648 dönem;
  - FL7: 168 saat, ≥ 151;
  - FL1d: 24 saat, ≥ 22;
  - PREM24: son 96 kapanmış 15 dakikalık bar'da `mark/index − 1` ortalaması, ≥ 86 bar.
- Her fit'te önce %1/%99 winsorize, sonra fit örneklemiyle z-skor.
- Ridge: `(1/n)Σ(y − a − Σ b_j z_j)² + 0.01·Σ b_j²`, kesişim cezasız.
- Diğer her şey H2a gibi.
- Hiçbir karar ya da gate'de kullanılmaz.

**S-H2-3 Validity gate** (her config için kendi ufku ve kendi `k`'sı ile; yalnız değerlendirme verisinde).

```
E = {s : değerlendirme penceresinde günlük s, s+h ≤ pencere sonu}
her s ∈ E:
   F̂(s) = s anında yapılmış PIT tahmin (refit değil); uygunluk s'deki gate'lerle
   H3 config (k, h=168): T = top-k, B = bottom-k  (F̂'ye göre, uygun varlıklarda)
        FS(s) = mean F̂(T) − mean F̂(B)
        RS(s) = mean y(T) − mean y(B)
   H1 config (h=720): n⁺ = #{F̂ > 0}; n⁺ = 0 ise s atlanır; T = top-min(3, n⁺)
        FS(s) = Σ F̂(T);  RS(s) = Σ y(T)
y(i,s) = Y_i(s,h)                                 eğer dönemlerin ≥ %90'ı bilinir
       = bilinen dönemlerin ortalaması             eğer %50–%90 bilinir
       = işaretli katkı min(0, tahminin işaretli katkısı)   eğer < %50 (eksik-etiket sayısı raporlanır)
R = Σ_s RS / Σ_s FS ; Σ FS ≤ 1e−9 ise INCONCLUSIVE
c = havuzlanmış OLS eğimi y ~ F̂ ((i,s) üzerinden); kesitsel demean edilmiş eğim yalnız raporlanır
CI'lar: tarihler üzerinde moving block bootstrap, B = 10000, seed 20261010,
        blok 14 gün (h=168) / 30 gün (h=720), iki taraflı yüzdelik 2.5 / 97.5
min_evidence: h=168 → |E| ≥ 60 ;  h=720 → ≥ 8 örtüşmeyen 720 saatlik pencere
INVALID      ⇔ min_evidence ∧ ( CI(R) ⊂ (−∞, 0.8) ∨ CI(R) ⊂ (1.25, ∞) ∨ CI_üst(c) ≤ 0 )
VALID        ⇔ min_evidence ∧ ¬INVALID ∧ 0.8 ≤ R ≤ 1.25 ∧ CI_alt(c) > 0
INCONCLUSIVE ⇔ diğer her durum
```

- Gate'in `y` değerleri kırpılmaz.
- **INVALID** → config P&L ne olursa olsun **MODEL INVALID / NO-GO**.
- **INCONCLUSIVE** → config ADVANCE ya da FINAL GO alamaz (en fazla MODEL INCONCLUSIVE).
- **P12** = VALID. Kalibrasyon aralığı [0.8, 1.25] değişmez.

---

## 5. H3 — Risk bütçeli perp-perp kitap (S-H3)

**S-H3-1 Uygunluk.** Bir varlık uygun sayılır ancak şunların hepsi sağlanırsa:
- §1 ve V5 gate'leri: karar penceresi spread ≤ 15 bps, 24 saatlik hacim ≥ 1M$, funding kapsamı ≥ %90, en yeni funding ≤ 3 saat;
- H2a(168) tahmini mevcut;
- ≥ 60 gün mark geçmişi.

`N_e` = uygun varlık sayısı.

**S-H3-2 Sinyal.** `s_i = F̂_i(t,168)`. `m_t` = uygunlarda `s`'nin medyanı. Bacak carry'si `c_i = −side·(s_i − m_t)` (USD/saat/USD). `cost_i` = taker fee + `t`'de bilinen half spread + slip (tek yön).

**S-H3-3 Boyut, buffer ve bölgeler.**
- `k' = min(k, ⌊N_e/2⌋)`; `k' < 3` ise kitap uygulanamaz (S-H3-12).
- `r = min(2, ⌊N_e/2⌋ − k')`.
- Uygunlar `s`'ye göre azalan sırada (eşitlikte varlık adı). Short bölgesi = ilk `k'`, long bölgesi = son `k'`. Short tutma bölgesi = ilk `k' + r`, long tutma bölgesi = son `k' + r`.

**S-H3-4 Tutma ve çıkış (günlük 00:00 UTC).** Tutulan bacak kalır ancak:
- kendi tutma bölgesinde ise **ve**
- `c_i·168 ≥ −cost_i` ise.

Aksi halde çıkılır. Çıkışlar girişlerden önce işlenir.

**S-H3-5 Giriş.** Her tarafta boş slot sayısı = `k'` − tutulan bacak. Adaylar kendi bölgesinde sıra sırasıyla denenir; aday `x` yalnız `c_x·168 > 2·cost_x` ise girer. Bölge biterse slot boş kalır.

**S-H3-6 Risk modeli** (yalnız `t` öncesi kapanmış bar'lar).
- **Vol:** `σ_i` = saatlik log mark getirilerinin EWMA vol'ü, yarı ömür 336 saat; ilk 30 gün başlatma; yıllıklaştırma √8760.
- **Kovaryans:** Son 60 günün saatlik log getirilerinin örnek kovaryansı → korelasyon matrisi. Ledoit–Wolf (2004) constant-correlation hedefi ve analitik `δ*` ([0, 1]'e kırpılır) ile küçültülür → `ρ̃`. `Σ_ij = ρ̃_ij σ_i σ_j`.
- **Beta:** BTC her zaman kovaryans hesabına girer. `β_i = Σ_i,BTC / Σ_BTC,BTC`.

**S-H3-7 Kitap inşası (deterministik, tek geçiş).**

```
L, S = seçilmiş long ve short bacak kümeleri (tutulanlar + girenler)
u_i  = (1/σ_i) / Σ_{j∈taraf(i)} (1/σ_j)                          # taraf içi inverse-vol
σ̃_L = sqrt(u_Lᵀ Σ_LL u_L) ; σ̃_S = sqrt(u_Sᵀ Σ_SS u_S)            # basket vol'leri
φ_L = σ̃_S / (σ̃_L + σ̃_S) ; φ_S = σ̃_L / (σ̃_L + σ̃_S)              # risk paritesi
eğer |φ_L − φ_S| > 0.2:  büyük olan = 0.6, küçük olan = 0.4         # dolar sınırı (0.2·G)
w⁰_i = +φ_L·u_i (i∈L) ; w⁰_i = −φ_S·u_i (i∈S)                     # birim-G kitap
G    = min(1.0, 0.08 / sqrt(w⁰ᵀ Σ w⁰))                            # σ* = %8/yıl
w    = G·w⁰
β_net = Σ_i w_i β_i                                                # equity'nin kesri
eğer |β_net| > 0.05:  w ← w · (0.05 / |β_net|)                     # bütün kitap ölçek küçültme
n_i  = w_i·E, qty step'e yuvarlanır (sıfıra doğru)
eğer bir |n_i| < max(min_notional_i, 10$):
     o bacağı düşür ve bu adımı kalan bacaklarla bir kez baştan yap
     (ikinci kez olursa → INFEASIBLE)
yuvarlama sonrası kontroller:
     |Σ_i n_i β_i| ≤ 0.05·E  ve  |Σ_{L} n − Σ_{S} |n|| ≤ 0.2·Σ|n| + max qty-step notional
     sağlanmazsa → INFEASIBLE
```

**S-H3-8 Kitap içi risk raporu** (her karar anında loglanır; gate değil).
- Ex-ante Euler risk katkıları `RC_L = Σ_{i∈L} w_i (Σw)_i / (wᵀΣw)` ve `RC_S`.
- Standalone taraf vol oranı `(φ_L σ̃_L)/(φ_S σ̃_S)`.
- `β_net` ve net dolar.
- Bunlar holdout ve forward raporlarında zaman ortalaması ve maksimumuyla raporlanır.

**S-H3-9 Boyut bandı.**
- Tutulan bacak yalnız `|n_hedef − n_tutulan| > 0.25·|n_hedef|` ise yeniden boyutlanır.
- Büyütme V6'ya özel, testli bir R10 istisnasıyla yapılır; V5 paper semantiği değişmez. Küçültme reduce-only.
- Kitabın geri kalanı bant içinde kalsa bile S-H3-7'nin yuvarlama sonrası kontrolleri gerçekleşen kitapta yeniden yapılır. Sağlanmazsa bant içindeki bacaklardan `|n_i β_i|` katkısı en büyük olan hedefe çekilir ve kontroller tekrarlanır. Bu en fazla bacak sayısı kadar tekrarlanır; hâlâ sağlanmazsa INFEASIBLE (S-H3-12).

**S-H3-10 İzleme (15 dakikada bir).** Bacak stop'u: girişe göre %15 ters hareket → çıkış, 24 saat cooldown. Ayrıca R3/R4 (V5 risk profili) ve V5 ineligibility çıkışları.

**S-H3-11 Execution.**
- **Tarihsel:** V5 DelayedExecutor (t + 15 dakika, S-COST).
- **Forward:** Kararın ardından gelen ilk fut_book snapshot'ında taker; book seviyeleri yürünür, taker fee 5 bps (V5 paper broker semantiği).

**S-H3-12 Uygulanamaz kitap.** Tetikleyiciler: `k' < 3`, model yok, ya da S-H3-7'nin INFEASIBLE sonucu.
- 24 saate kadar: mevcut bacaklar tutulur, yeni giriş ve büyütme yok, stop ve zorunlu çıkışlar aktif.
- 24 saatten uzun süren kesintisiz durumda: bütün bacaklardan çıkılır.

**S-H3-13 Varyantlar.** H3-k4, H3-k6. Başka fark yok.

---

## 6. H1 — Hedge'li carry (S-H1)

**S-H1-1 Evren.** 20 varlıktan Kraken spot USD paritesi olanlar; spot 24 saatlik hacim ≥ 1M$.

**S-H1-2 Sermaye.**
- Her pozisyonda spot qty = perp qty (qty-matched hedge).
- Spot tam fonlanır; perp teminatı = perp notional (1×), nakitten ayrılır.
- Varlık başına hedge'li notional ≤ `E/6`; en fazla 3 varlık; toplam ≤ `E/2`.

**S-H1-3 Giriş (günlük 00:00 UTC).** Varlık tutulmuyor ve cooldown'da değil; `F̂_i(t,720) > 0` ve `F̂_i·720 ≥ m·RT_i`.
- `RT_i` = S-COST-3 ile `t`'de bilinen all-in round trip: iki bacak, giriş ve çıkış, taker.
- Adaylar `F̂`'ye göre azalan sırada.
- **Varyantlar:** m ∈ {1.5, 2.5}.

**S-H1-4 Çıkış** (iki bacak birlikte), şunlardan biri olunca:
- günlük 00:00 UTC'de `F̂_i(t,720)·720 < RT_i/2`;
- birleşik stop: spot+perp P&L ≤ −%15 × hedge'li notional (15 dakikalık izleme);
- hedge reset: spot mid girişten ±%40;
- R4 kill;
- ineligibility (V5 kuralları).

Her çıkıştan sonra o varlıkta 24 saat cooldown.

**S-H1-5 Tarihsel execution.** İki bacak taker, t + 15 dakika (S-COST).

**S-H1-6 Sensitivity satırları.**

| Satır | Tanım | Rol |
|---|---|---|
| S-MAKER-BOUND | PRIMARY ile aynı kararlar; fill = t + 15 dakikadaki mid; fee maker/maker; spread ve slip 0; %100 fill | Yalnız NO-GO zorlar |
| S-COST2X | Bütün fee, spot spread varsayımı, perp spread ve slip ×2, iki bacak | Karar taşır (P7) |
| S-PIT10 | Perp spread snapshot'ının bilinme zamanı T + 10 dakika | Karar taşır (P11) |
| S-FB-CONS | Perp spread fallback'i = değerlendirme başlangıcından önceki 35 günün varlık başına p90'ı | Karar taşır (P11) |
| S-MIXED | Spot maker + perp taker | Bilgi |
| S-SPOTSPREAD2X | Spot spread varsayımı ×2 | Bilgi |
| S-FEE-ACTUAL | Doğrulanmış hesap fee'leri | Bilgi |

H3 için de S-COST2X, S-PIT10 ve S-FB-CONS aynı tanımlarla karar taşır.

**S-H1-X Forward execution (freeze).**

| Durum | Kural |
|---|---|
| Spot giriş | Karar anındaki son spot_ticker'ın (≤ t) best bid'ine limit emir |
| Maker fill | `(t, t + 60 dk]` içinde bir spot_ticker'da best ask ≤ emir fiyatı **ve** zaman damgası ≤ o ticker olan en son spot_depth'te bu fiyat ve altındaki ask hacmi ≥ kalan qty → emir fiyatından dolar. Hacim yetersizse görünen hacim kadar kısmi fill, pencere sürer |
| Taker fallback | `t + 60 dk`'da kalan qty için zaman damgası ≤ `t + 60 dk` olan son spot_depth'in ask seviyeleri yürünür (VWAP + taker fee). Derinlik yetmezse kalan iptal edilir |
| Perp hedge | Spot fill tamamlanınca (ya da iptal kesinleşince) ilk fut_book snapshot'ında taker short; qty = dolan spot qty, bid seviyeleri yürünür |
| Çıkış | Simetrik: spot satış best ask'e maker (fill: best bid ≥ fiyat ve bid hacmi), 60 dk sonra taker fallback; perp kapanışı spot fill'inden sonraki ilk snapshot'ta |
| Bacaklar arası gecikme | Spot ve perp fill'leri arasındaki hedge'siz P&L episode'a yazılır ve birleşik stop'a sayılır |
| Collector boşluğu | Pencerede snapshot yoksa maker dolmamış sayılır; taker fallback ilk mevcut snapshot'ta. Karar anında ≥ 6 saatlik boşluk → giriş atlanır. Çıkışlar ilk mevcut snapshot'ta taker |

**S-H1-F Feasibility yolu** (yalnız holdout'ta NO-GO-PRIMARY alan H1 config'leri için).

```
Ölçüm replay'i M: forward gün 1–90, S-H1-X execution, PRIMARY kurallar (S-H1-3/4), başlangıç 265$ düz
  M-a = maker olarak dolan spot notional / toplam spot notional
  M-b = Σ_episodes notional·(ölçülen fee + spread + slip + bacaklar arası gecikme P&L, giriş+çıkış)
        / Σ notional ; gün 90'da açık episode'ların çıkışı = gün 90'da varsayımsal taker unwind
        (zaman damgası ≤ gün 90 olan en son spot_depth ve fut_book'ta seviyeler yürünür)
  M-c = M'nin P1 ve P3 nokta değerleri (terminal unwind dahil)
  ESTABLISHED ⇔ M-a ≥ 0.50 ∧ M-b ≤ 0.0125 ∧ M-c.P1 > 0 ∧ M-c.P3 > 0 ∧ #episode ≥ 3
Confirmation replay'i C (yalnız ESTABLISHED ise): forward gün 91–365, ayrı koşu,
  başlangıç 265$ ve DÜZ (M'den hiçbir pozisyon, nakit, P&L, cooldown ya da stop state'i taşınmaz),
  H2a fit'i yalnız funding verisiyle (S-H2-0) ; execution modeli freeze'deki gibi (M'den öğrenilmez)
  Bütün kriterler ve kanıt gate'i yalnız C üzerinde.
```

C'nin düz başlaması ekonomik olarak gerçekçidir: gün 91'de yeni sermayeyle başlayan bir hesaba denktir; giriş maliyetleri C içinde ödenir. M'nin çıktıları yalnız M-a, M-b ve M-c'dir; C'ye girmez.

**S-H1-7 Varyantlar.** H1-m1.5, H1-m2.5.

---

## 7. Kriterler (S-CRIT)

**S-CRIT-P Mutlak kriterler.** PRIMARY, terminal unwind dahil.

| # | Kriter | Eşik |
|---|---|---|
| P1 | Toplam net | > 0 |
| P2 | İstatistik | Holm ile reddedilmiş (S-STAT) **ve** düzeltmesiz LB95 > 0: net ve carry, b = 14 ve b = 28 |
| P3 | Toplam carry | > 0 |
| P4 | Sharpe | ≥ 1.0 |
| P5 | MaxDD | ≤ %10 |
| P6 | En kötü gün / ES97.5 | ≥ −%3 / ≥ −%2 |
| P7 | S-COST2X net | ≥ 0 |
| P8 | Konsantrasyon | S-CRIT-C |
| P9 | Zaman kararlılığı | S-CRIT-T |
| P10 | Kill / likidasyon | = 0 |
| P11 | S-PIT10 ve S-FB-CONS | P1 ve P3'ün işareti değişmez |
| P12 | H2a gate | VALID (S-H2-3) |

**S-CRIT-E Kanıt gate'i.**
- **Aktif gün:** ≥ 12 saat boyunca gross ≥ %10·E olan gün (15 dakikalık izlemeyle). H1'de gross = hedge'li perp notional.
- **Episode:** Bir varlıktaki tutma dönemi. Aynı varlıkta ≥ 7 gün aradan sonraki tutma yeni episode'dur.
- **Cluster:** Girişler kronolojik sırayla açgözlü gruplanır; yeni cluster, mevcut cluster'ın ilk girişinden ≥ 7 gün sonraki ilk girişte başlar.
- **Eşikler:**
  - H1: aktif gün ≥ 60, episode ≥ 3, cluster ≥ 3, underlying ≥ 3.
  - H3: aktif gün ≥ 60, episode ≥ 10, cluster ≥ 4, underlying ≥ 6.
- Herhangi biri sağlanmazsa → **INSUFFICIENT**.

**S-CRIT-C Konsantrasyon** (yalnız P1 geçtiyse; `C_a` = varlığa atfedilen net, `C⁺_a = max(C_a, 0)`).

| Kontrol | Kural | Değerlendirilemezse |
|---|---|---|
| P8a | `max_a C_a ≤ 0.5·net` | `C_a ≠ 0` olan < 3 varlık → INCONCLUSIVE |
| P8b | ∀a: `net − C_a > 0` | — |
| P8c | `N_eff = (ΣC⁺)²/Σ(C⁺)²`; H1 ≥ 2, H3 ≥ 4 | — |
| P8d | En iyi ⌈0.2·n⌉ kapanmış episode'un net'i ≤ 0.5 × kapanmış episode'ların net'i | < 10 kapanmış episode → INCONCLUSIVE |

Değerlendirilebilir bir kontrol başarısız olursa → NO-GO.

**S-CRIT-T Zaman kararlılığı.**
- Pencere 3 eşit takvim alt penceresine bölünür. `P&L_j / E_başlangıç` hesaplanır.
- **Taban (her alt pencere):** ≥ −%3, yoksa NO-GO.
- **Değerlendirilebilir:** aktif gün ≥ max(15, ⌈0.25·gün⌉).
- **Geçer:** ≥ 2 değerlendirilebilir pencere ve en az 2'sinde net > 0 (tam 2 varsa ikisi de).
- < 2 değerlendirilebilir → INCONCLUSIVE. Aksi halde NO-GO.

---

## 8. İstatistik prosedürü (S-STAT)

**S-STAT-P p-değerleri.**

```
p_mbb(x, b) = V5 stats.mbb(x, b, B=10000, seed=20261010).p   # tek taraflı, H0: ortalama ≤ 0
p_net   = max(p_mbb(net, 14),   p_mbb(net, 28))
p_carry = max(p_mbb(carry, 14), p_mbb(carry, 28))
p_GO    = max(p_net, p_carry)          # intersection–union: H0 = {μ_net ≤ 0 ∨ μ_carry ≤ 0}
```

**S-STAT-H Holm (tek tanım; her yerde aynı).**

```
holm(p[1..m], α):
    sırala: p_(1) ≤ … ≤ p_(m)   (eşitlikte config adı)
    j = 1..m için: p_(j) ≤ α/(m − j + 1) ise reddet, değilse dur
    return reddedilenler
```

**S-STAT-O Holdout (tek açılış).**

```
FAMILY_H = [H3-k4, H3-k6, H1-m1.5, H1-m2.5]                     # m = 4, sabit
her c: PRIMARY ve S-H1-6 satırlarını koş; p_GO(c)
R = holm(p_GO, 0.05)
class(c) = ladder(c, statistik = (c ∈ R ∧ P2'nin LB koşulu), ortam = holdout)
```

**S-STAT-L Karar merdiveni.**
- Bütün kontroller hesaplanır; **tetiklenen en ağır sınıf** sonuçtur. Bütün nedenler raporlanır.
- Sıralama: `INVALID RUN > MODEL INVALID > NO-GO (NO-GO-PRIMARY dahil) > {MODEL INCONCLUSIVE, INSUFFICIENT, INCONCLUSIVE} > ADVANCE / FINAL GO`.

| Kontrol | Sınıf |
|---|---|
| Veri bütünlüğü ya da guard | INVALID RUN (R9) |
| H2a gate INVALID | MODEL INVALID |
| P10 | NO-GO |
| P1 ya da P3 ≤ 0 | H3: NO-GO. H1: S-MAKER-BOUND'da P1 ya da P3 ≤ 0 → NO-GO; aksi halde NO-GO-PRIMARY |
| P4, P5, P6, P7, P11; değerlendirilebilir P8 ya da P9 başarısız | NO-GO |
| P8 ya da P9 değerlendirilemez | INCONCLUSIVE |
| S-CRIT-E | INSUFFICIENT |
| H2a gate INCONCLUSIVE | MODEL INCONCLUSIVE |
| P2 sağlanmıyor | INCONCLUSIVE |
| S-DATA-5 bakış coverage'ı < %80 (forward) | INCONCLUSIVE |
| Hiçbiri | Holdout: ADVANCE (H1 dahil; holdout'ta FINAL GO yok). Forward: FINAL GO |

**S-STAT-C Forward aileleri (holdout kararından sonra, bir kez sabitlenir).**

```
CAND = { c : class(c) ∈ {ADVANCE, INCONCLUSIVE, INSUFFICIENT, MODEL INCONCLUSIVE},
             nedenlerinde NO-GO ya da MODEL INVALID yok, P1 > 0 ve P3 > 0 }
FEAS = { c ∈ H1 : class(c) = NO-GO-PRIMARY }
FAMILY_F = CAND ∪ FEAS ; m_F = |FAMILY_F| (≤ 4) ; forward başlangıcında sabit; hiçbir zaman küçülmez
```

**S-STAT-F Forward sıralı test (tek ve değişmez algoritma).**

```
LOOKS = [(90, 0.001), (180, 0.001), (365, 0.048)]
status[c] = PENDING  (c ∈ FAMILY_F)
her (gün, α) ∈ LOOKS:
    her c ∈ FAMILY_F:
        c ∈ FEAS:
            gün = 90  → S-H1-F ölçümü M; NOT ESTABLISHED → status = CLOSED
            gün < 365 ∨ status = CLOSED → p[c] = 1
            gün = 365 ∧ ESTABLISHED → p[c] = p_GO(c, confirmation replay C, gün 91–365)
        c ∈ CAND:
            status = CLOSED → p[c] = 1
            aksi halde p[c] = p_GO(c, forward gün 1..gün)   # kümülatif pencere
    R = holm(p[FAMILY_F], α)                               # payda her zaman m_F
    her c ∈ FAMILY_F, status = PENDING:
        L = ladder(c, statistik = (c ∈ R ∧ LB koşulu), pencere = c'nin penceresi)
        L = FINAL GO          → status = FINAL GO (kalıcı; sonraki bakışlarda yalnız izleme)
        L'de MODEL INVALID ya da P10 → status = CLOSED (NO-GO)
        gün = 365 ∧ status hâlâ PENDING → status = L'nin sınıfı; aile kapanır
FINAL GO sonrası izleme: P5, P6 ya da P10 ihlali → GO REVOKED (risk); yeniden test yok
```

**S-STAT-T Aile hatası.**
- Her bakış `ℓ`'de Holm, `m_F` sabit paydayla ve geçerli p-değerleriyle (test edilmeyen slot `p = 1`), o bakıştaki gerçek null'lar için aile hatasını `α_ℓ` ile sınırlar.
- FINAL GO, en az bir bakışta Holm reddini gerektirir. Bu yüzden `P(en az bir yanlış FINAL GO) ≤ Σ_ℓ α_ℓ = 0.001 + 0.001 + 0.048 = 0.050` (bakışlar üzerinden Bonferroni; kümülatif pencerelerin korelasyonuna karşı muhafazakâr).
- Aynı config'in sonraki bakışlarda yeniden test edilmesi bu sınırı değiştirmez; her bakışın α'sı sabittir, harcama takvimi değişmez.
- Erken FINAL GO alan config sonraki bakışlarda yeniden test edilmez; p'si yalnız raporlanır. Holm paydası küçülmez.

**S-STAT-X Terminal kural.** Gün 365'te FINAL GO almayan config'ler kapanır. H1, H2 ve H3 aynı veri üzerinde yeniden spec'lenmez.

---

## 9. Güç ve beklenti (bilgi; karar kuralı değil)

| Örneklem | Gereken Sharpe (LB95 > 0, blok şişmesi ~×1.2) |
|---|---|
| 100 gün | ~3.8 (Holm'da ilk config için ~5) |
| 1 yıl | ~2.0 |

- **H3:** Ex-ante maliyet öncesi Sharpe ≈ 0.3 (plan rev 2, §3.2).
- **H1:** Holdout'ta 720 saatlik gate'in minimum kanıtı sağlanamaz; bu yüzden en fazla MODEL INCONCLUSIVE alır.

Holdout'ta çoğu sonuç doğal olarak INCONCLUSIVE ya da NO-GO çıkabilir; **kriterler gevşetilmez.**

---

## 10. Süreç

1. Exact spec.
2. Bağımsız metodolojik red-team (planı yazmamış, temiz bağlamlı).
3. Critical ve Major düzeltmeler ve reviewer'ın taze doğrulaması.
4. Freeze: temiz commit, annotated tag `v6-profitability-spec-frozen`, spec sha256, sabitler sha256, fee snapshot sha256, freeze zaman damgası, red-team raporu ve sha'sı.
5. Implementasyon (TDD).
6. Kör dev invariant koşuları.
7. Bağımsız implementasyon review'u.
8. Kullanıcının açık onayıyla tek holdout açılışı.
9. Forward bakışları (S-STAT-F).

**R9 bug kuralı:** V5'teki gibi aynen. Orijinal sonuç korunur, bağımsız doğrulama yapılır, önce kırmızı test yazılır, düzeltme yalnız frozen kurala uyum sağlar, original ve corrected birlikte raporlanır.
