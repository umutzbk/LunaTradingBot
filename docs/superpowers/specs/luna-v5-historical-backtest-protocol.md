# V5 Adaptive Carry: Historical Backtest Protocol (draft rev 3)

**Durum:**
- Draft rev 3, 2026-10-11. **Frozen değil.**
- Bu protokol için hiçbir tarihsel V5 getirisi, P&L'i veya arm karşılaştırması hesaplanmadı.
- **Revizyon geçmişi:**
  - rev 1 → rev 2: kullanıcı kararları (2026-10-11), Stage 0 audit planı (§2.1), W parity planı (§5).
  - **rev 2 → rev 3 (pre-freeze, outcome görülmeden):** spread çözünürlüğü kullanıcı kararıyla gerçek 5 dakikalık Kraken snapshot'ları. §2.1, §3.1, §3.2 ve §13 M2 buna göre yeniden yazıldı. Kayıt §15'te.
  - Bağımsız metodolojik red-team (2026-10-11) NOT READY dedi: 2 Critical, 10 Major, 7 Minor.
  - Bu metin bütün Critical ve Major bulguları ve Minor'ları işler. Bulgu ↔ değişiklik eşlemesi §13'te.
  - Freeze'den önce **yeniden red-team** zorunludur.
  - **2026-10-11 gece bulguları §14'te:** Stage 0 BLOCKED (protokol penceresinde gerçek 1 dakikalık spread yok; yalnızca ~2025-02 öncesinde var), W parity PASS. Metodoloji değişmedi; spread çözünürlüğü kararı kullanıcıda.

**Sıra (kullanıcı kararı, değişmez):**
1. Stage 0 spread/veri backfill + audit
2. W parity verification
3. Protokol rev 2 (parity bulgularıyla güncellenir)
4. Bağımsız metodolojik red-team
5. Critical ve Major düzeltmeleri
6. Tam protokol freeze: commit, annotated tag `v5-backtest-protocol-frozen`, sha256
7. Implementasyon ve code review
8. Ancak bunlardan sonra historical outcome run

**İlgili belgeler:**
- `luna-v5-adaptive-carry.md` (V5 motoru).
- V4 spec `research-v4-spec-frozen` (9d83382).
- V4 Stage 1 kodu `b074b69` ve sonuçları (`docs/results/v4-stage1/summary.json`, sha `aa652503…`).
- `docs/results/v4-data-availability.md`.
- Bu protokol V4'ün hiçbir dosyasını, tag'ini veya sonucunu değiştirmez.

## 0. Değişmez kurallar

| # | Kural |
|---|---|
| R1 | Freeze tag'inden önce hiçbir **yeni** tarihsel V5 getirisi, P&L'i veya arm karşılaştırması hesaplanmaz. Tek istisna §5 parity'sidir. Parity yalnızca zaten yayımlanmış V4 H2-L30-k2 sonucunu ve onun trade listesini yeniden üretir. |
| R2 | **Holdout [2026-07-01T00:00Z, collector_start) sealed.** 2026-07-01 ve sonrası hiçbir kaynaktan okunmaz. **Bütün parity ve outcome girdileri, sha'ları sabitlenmiş ve kesimin altında kesilmiş bir kopyadan okunur:** `var/history_v5/frozen/` (§2.2). Paper bot'un canlı `var/bars` dosyalarından okunmaz. İndirme isteklerinde `to = cutoff − interval`. Yükleyici `assert_before_cutoff` kullanır. Kopyada `ts ≥ cutoff` olan bir satır runner'ı durdurur (test ile). |
| R3 | Paper config değişmez. Strateji parametreleri V5 default'larıdır: `k=2`, `gross=1.0`, `signal_hours=720`, `switch_horizon_hours=168`, `min_carry_improvement_bps=5`, `switch_cost_multiplier=1.5`, `safety_buffer_bps=5`, `min_hold_hours=24`. |
| R4 | P-TIGHT, P-LOOSE ve P-C8 yalnızca önceden kayıtlı robustness'tır (§7). Hiçbir varyant seçilmez ve paper config'e taşınmaz. |
| R5 | Random split yok. Tek, kronolojik, point-in-time replay. |
| R6 | Karşılaştırma arm'ları (W, D, C, E) aynı veriyi, evreni, gate'leri, FL30'u, k'yı, gross'u, sizing'i, risk kurallarını, funding semantiğini, yürütme ve maliyet modelini kullanır. Yalnızca `decision_mode` ve `trigger` farklıdır. |
| R7 | **Yürütme gecikmesi 15 dakika, bütün arm'larda aynı.** Karar t'deki snapshot'la verilir; fill t + 15m'de olur (§3.2). Karar fiyatından anında fill yasaktır (test ile). |
| R8 | Etiket: **"EXPLORATORY — reused development period"**. Sonuç canlıya geçiş, GO kararı veya parametre değişikliği üretmez. |
| R9 | **Tek outcome koşusu ve önceden kayıtlı bug kuralı.** Koşudan sonra bir bug bulunursa her iki sonuç da (orijinal ve düzeltilmiş) diff ile raporlanır. Hükmü **düzeltilmiş koşu** ancak şu üç şart birlikte sağlanırsa verir: (a) bağımsız bir review bug'ı doğrular; (b) düzeltmeden önce kırmızı olan bir test vardır; (c) düzeltme, protokolün yazılı bir kuralına uyumu sağlar ve yeni bir kural getirmez. Aksi halde hükmü orijinal koşu verir. Protokol, kriter ve eşikler sonuçtan sonra değişmez. |
| R10 | `live_enabled=false`. Paper/live risk motoru ve `CarryCore` parity veya backtest için davranış değiştirmez. Replay'e özel her şey replay adaptöründe ya da ayrı modüllerde yaşar. Paper engine bu modları reddeder (test ile). |
| R11 | V4 frozen: V4 worktree'si, tag'leri, spec'i, sonuç dosyaları ve `var/history_v4` değiştirilmez (yalnızca okunur ve kopyalanır). |

## 1. Araştırma sorusu ve kontaminasyon beyanı

**Soru:** Aynı FL30 sinyali, aynı risk kuralları ve aynı maliyet varsayımları altında, 15 dakikalık point-in-time event-driven cost-aware V5 (E), haftalık Pazartesi kontrolünü (W) carry net-of-cost olarak yeniyor mu? Toplam ekonomiyi bozmadan mı yeniyor? Fark nereden geliyor?

**Kontaminasyon (açık beyan):**
- Gerçek funding yalnızca 2025-10-06 → 2026-07-01 aralığında var ve bu pencere V1–V4'te kullanıldı.
- W'nin V4 karşılığı (H2-L30-k2) biliniyor: funding +10,69, ücret 2,27, spread 1,08, fiyat +9,95 USD.
- V5 default'ları bu sonuç görüldükten sonra yazıldı.
- Sonuç bu yüzden **genelleme kanıtı değildir.** Teyit yalnızca forward veriden gelir (§11).

## 2. Veri ve pencere

| Kalem | Kaynak (orijinal) | Not |
|---|---|---|
| Funding | `var/history_v4/funding` | Gerçek, saatlik, known-only |
| Mark | `var/history_v4/mark` 15m | Mid, değerleme, stop, margin |
| Perp trade barları | `var/bars/kraken_futures` 15m (canlı olarak büyüyen dosya) | Hacim; parity'de V4 fiyatı |
| Perp spread (sürekli) | Stage 0: `var/history_v5/spreads` (§2.1), **5 dakikalık tarihsel snapshot verisi** (`interval=300`) | `history_v4/spreads` yalnızca 23:00–00:00 saatini kapsar |
| V4 spread pencereleri | `var/history_v4/spreads` | Yalnızca parity ve tutarlılık kontrolü |
| Enstrüman | `config/v4_instruments.json` (sha `d14a82bb…`) | Bilinen anakronizm |
| Evren | V4 §2a'nın 20 varlığı | Paper evreniyle aynı |

**Pencereler:**
- **Lookback:** 2025-10-06 08:00'den itibaren. Lookback verisi yalnızca sinyal için kullanılır.
- **Parity (§5):** V4 Stage 1 penceresi [2025-11-05, 2026-07-01), 238 gün.
- **Outcome arm'ları:** **[2025-11-10T00:00Z, 2026-07-01T00:00Z), 233 gün.** Pencere ilk Pazartesi'de başlar. Böylece her arm'ın ilk izinli işlemi aynı anda olur ve V5 `Trigger`'ın "ilk değerlendirme her zaman izinli" kuralı W'yi bir Çarşamba'ya kaydırmaz. Bütün arm'lar flat başlar.
- **Son karar:** 2026-06-30 23:30; fill'i 23:45'te olur. 23:45'te yeni karar verilmez, çünkü t + 15m fill'i kesime denk gelir.
- **Terminal değerleme:** `open_ts = 2026-06-30 23:45` mark barının kapanışı (veri damgası kesimden önce). Son funding dönemi (23:00) açıkça settle edilir. Terminal unwind maliyeti ayrı bir sensitivity'dir (§9).

### 2.1 Stage 0: spread backfill ve audit planı (getiri hesaplanmaz)

**Amaç:** Her karar ve fill anı için point-in-time perp spread'i sağlamak.
- **Primary kaynak: Kraken analytics `spreads`, `interval=300`. Adı: "5 dakikalık tarihsel snapshot verisi" (5-minute historical snapshot data).** Her satır, etiketli 5 dakikalık aralık içinde alınmış tek bir bid/ask snapshot'ıdır.
- `interval=60` endpoint'i primary'de **kullanılmaz** ve hiçbir yerde 1 dakikalık veri gibi gösterilmez. Protokol penceresinde 60 s serisi, 5 dakikalık snapshot'ların ileri taşınmış halidir (§14). Yalnızca V4 parity'sinde (V4'ün saklı pencereleri) ve V4 tutarlılık kontrolünde okunur.
- Interpolasyon ve forward-fill ile yeni gözlem üretilmez. Eksik snapshot eksik kalır.
- Sabit 10 bps **primary değildir**; yalnızca fallback (kayıtlı ve sayılır) ve S-SPREAD-FIX sensitivity'sidir.

**Point-in-time kuralı (muhafazakâr bilinme zamanı):**
- Kraken etiket semantiği dakika düzeyinde kesin doğrulanamadığı için, **etiketi T olan snapshot ancak T + 5 dakikadan itibaren bilinir** sayılır.
- Gerekçe (S0.1 probe): `interval=300`'ün T değeri, 60 s serisinin T+faz … T+4 dakikalarındaki değerine eşittir; faz 0–4 dakika arasında günden güne kayar. Snapshot bu yüzden [T, T+5m) içinde bir anda alınır; T anında bilinmez.

**Karar ve fill pencereleri:**

| Pencere | Etiketler | Bilinme koşulu | Azami gözlem | Primary geçerlilik | Altında |
|---|---|---|---|---|---|
| Karar (t) | T ∈ [t−1h, t) | T + 5m ≤ t | 12 (t−60m … t−5m) | ≥ 6/12 geçerli | 10 bps fallback, sayılır |
| Fill (t kararı, fill t+15m) | T ∈ [t, t+15m) | T + 5m ≤ t + 15m | 3 (t, t+5m, t+10m) | ≥ 2/3 geçerli | 10 bps fallback, sayılır |

- İki pencere örtüşmez ([t−1h, t) ve [t, t+15m)), test ile.
- Spread değeri: geçerli snapshot'ların `spread_full` ortalaması; yarım spread bunun yarısı.
- Geçerli snapshot: sonlu, > 0 ve ≤ 500 bps.
- 5 dakikalık grid dışında etiket taşıyan bir seri (örneğin 60 s) primary yükleyici tarafından reddedilir (test ile).

**S0.1 API semantik probe'u:** Yapıldı (`docs/results/v5-stage0-audit.md` §1). Ham çıktılar `var/ops/v5_stage0/`.

**S0.2 Backfill (`scripts/v5_stage0/backfill_spreads.py`, V5 kodu, V4'ü import etmez):**
- 20 varlık, [2025-10-06T00:00Z, 2026-07-01T00:00Z), `interval=300`, `to ≤ cutoff − 300 s`. Dönen `ts ≥ cutoff` satırları yazılmadan atılır ve sayılır.
- Resume-safe, idempotent, gün başına checkpoint (sha256), sınırlı yeniden deneme.
- Dosya başına `provenance.json`: endpoint, parametreler, satırlar, ilk/son, düşürülen satır sayısı, gözlenen kadans, çözünürlük etiketi, sha256.
- Paper servislerine dokunmaz.

**S0.3 Audit raporu (`docs/results/v5-stage0-audit.md`, P&L ve sinyal sıralaması yok):**

| Kontrol | Ölçü | Eşik / aksiyon |
|---|---|---|
| Kapsam | Varlık × gün, ay ve günün saati kırılımında mevcut 5 dakikalık snapshot oranı. 15m noktalarında karar penceresinde ≥ 6/12 ve fill penceresinde ≥ 2/3 geçerli snapshot bulunan nokta oranı. | Raporlanır. Outcome penceresinde **herhangi bir varlıkta veya herhangi bir varlık × günün saati hücresinde** (karar veya fill) nokta kapsamı < %90 ise "spread coverage insufficient" işaretlenir ve kullanıcıya gelir. Havuzlanmış oran kullanılmaz. Eşik sonradan gevşetilmez. |
| Tekrarlayan değer | Ardışık iki snapshot'ın birebir aynı olma oranı ve en uzun seri (kaynağın kendi ileri taşıması olabilir) | Raporlanır; kural değişmez |
| Boşluklar | En uzun boşluk, ≥ 1 saatlik boşlukların listesi | Raporlanır |
| Zaman hizası | (a) Grid: 5 dakika sınırı, tekrar, monotonluk, grid sapması. (b) **V4 tutarlılığı (5 dakikalık snapshot değer ve kadans parity'si; 1 dakikalık satır parity'si değil):** V4'ün her gün [23:00, 00:00) 60 s serisi, ileri taşınmış 5 dakikalık snapshot'lardır. Yeni serinin T etiketli değeri, V4 serisinin T+4m satırına eşit olmalıdır (1e-12); V4 serisindeki değer değişimleri yeni serinin 5 dakikalık snapshot'larıyla aynı kadansta olmalıdır. FAIL ölçütü değer parity'sidir. Gün içinde faz kayması olan günlerde tek bir fazla açıklanamayan V4 dakikaları gün listesiyle raporlanır, FAIL sayılmaz. (c) Point-in-time kuralı: T etiketli snapshot T + 5m'de bilinir. | (b) FAIL ise Stage 0 FAIL; kök neden bulunmadan devam edilmez. |
| Fallback oranı | Bütün 15m noktalarında karar ve fill pencereleri için; varlık, ay ve **günün saati** kırılımı (W, D ve C yalnızca 00:00'da işlem yapar) | Raporlanır; ayrıca outcome raporunda **dolan emirler** üzerinden arm başına fallback payı |
| Değer geçerliliği | ≤ 0, NaN, sonsuz, > 500 bps | Kullanılmaz ve sayılır |
| Mark ve trade | Mark kapanışı ile trade kapanışı arasındaki sapma dağılımı (fiyat seviyesi değil, göreli fark); mark ve bar kapsamı | Raporlanır |
| Holdout | Kopyada max ts < 2026-07-01; guard testleri | Değilse FAIL |

**Stage 0 çıkış koşulu:** Holdout PASS, V4 tutarlılığı PASS, gerçek 5 dakikalık snapshot'lar alındı (gözlenen kadans 300 s), audit kullanıcıya sunuldu.

### 2.2 Dondurulmuş girdi kopyası

Stage 0'ın sonunda, parity'den önce şu dosyalar `var/history_v5/frozen/` altına **kesimin altında kesilerek** kopyalanır:
- funding, mark, **index**, perp trade barları, **spot trade barları** (`kraken_spot`; V4 `load_historical` bunları da yükler), V4 spread pencereleri, yeni spread serisi, enstrüman dosyası.

Kopya kuralları:
- **Kesimden önceki tam geçmiş** korunur, yalnızca 2025-10-06 sonrası değil. V4, listeleme yaşını ilk gerçek perp barından türetir. Kısaltılmış bir kopya `elig_h2_30`'u kaydırır.
- **V4'ün dizin düzeni** korunur (`bars/<venue>/<asset>.parquet`, `history_v4/<kind>/…`). Böylece V4 yükleyicileri değişmeden kopyaya yönlendirilebilir.

Kopya için `manifest.json` yazılır: dosya başına sha256, satır sayısı, ilk/son ts.

Parity ve outcome run yalnızca bu kopyayı okur ve sha'ları doğrular. V4 Stage 1 manifest'indeki girdi sha'larıyla eşleşmeyen dosyalar ayrıca listelenir. Örneğin canlı bar dosyasında geçmiş satırlar revize edildiyse burada görünür.

## 3. Tarihsel snapshot ve yürütme (karşılaştırma arm'ları için ortak)

### 3.1 Snapshot (t, 15 dakikalık grid)

- `mid_t` = `open_ts = t − 15m` mark barının kapanışı (t'de kapanmış bar). Yoksa trade kapanışı kullanılır ve sayılır; ikisi de yoksa quote geçersizdir.
- `dec_spread_t` = etiketi [t−1h, t) içinde olan ve T + 5m ≤ t ile t'de bilinen geçerli 5 dakikalık snapshot'ların ortalaması (azami 12). En az 6 geçerli snapshot gerekir; yoksa 10 bps fallback, sayılır. **Yalnızca gate'ler ve karar maliyeti için** kullanılır.
- `bid/ask_t = mid_t × (1 ∓ dec_spread_t / 2)`. Karar katmanı (`decide`, gate'ler) bunları görür.
- `volumeQuote_t` = t'de kapanmış son 96 trade barının Σ volume·close değeri.
- **Derinlik:** Tarihsel book yok. Builder, book boyutlarını sonsuz kabul eder; böylece derinlik gate'i NaN yüzünden düşmez, geçer. Beyan edilir.
- **Funding, point-in-time:**
  - Replay, `FundingStore`'u t anına kesilmiş bir görünümle sarar. `trailing`, `window` ve **`newest_end`** yalnızca `T + 1h ≤ t` dönemlerini görür.
  - Böylece `funding_stale` gate'i tarihsel API boşluklarında doğru tetiklenir (test ile).
  - Settlement, bir dönemin rate'ini dönem bittikten sonra okur. Eksik dönem 0 nakit akışı olarak sayılır.

### 3.2 Yürütme (replay executor, `CarryCore` değişmez)

- t'de gönderilen emir **t + 15m**'de dolar.
  - Fill fiyatı = `mid_{t+15m} × (1 ± fill_half_{t+15m})`, ardından × (1 ± 2 bps slip), taker ücreti %0,05.
  - `fill_half_{t+15m}`, karar penceresiyle **örtüşmeyen**, etiketi [t, t+15m) içinde olan ve T + 5m ≤ t + 15m ile fill anında bilinen geçerli 5 dakikalık snapshot'ların (azami 3: t, t+5m, t+10m) ortalamasının yarısıdır. En az 2 geçerli snapshot gerekir; yoksa 10 bps fallback'in yarısı kullanılır ve sayılır.
  - Fill zaman damgası t + 15m'dir.
- **IOC bandı replay'de uygulanmaz.** Gecikmeli fill piyasa emri gibi dolar. Gerekçe: Band, canlı fiyat korumasıdır; 15 dakika eski mid'e göre bant uygulamak, zamanlanmış arm'ları sistematik olarak cezalandırır (red-team C1). Bunun dışında fill gerçekleşmeyen tek durum t + 15m snapshot'ının olmamasıdır; sayılır.
- **Zamanlanmış rebalance ancak tamamlanınca tüketilir; yeniden deneme penceresi sınırlıdır.**
  - **Tamamlanma tanımı (W, D, C için aynı):** Rebalance izniyle koşan bir döngünün `CycleResult`'ında:
    - bütün aksiyon grupları gönderilmiş ve tamamen dolmuş;
    - hiçbir REJECT, `close_not_filled` veya `forced_exit_not_filled` kaydı yok.
  - Tamamlanmayan bir zamanlanmış rebalance, izin **en fazla 4 deneme döngüsü** açık tutularak yeniden denenir. Sonra tüketilir. Bu sınır, W ve D'nin hareketli ideal kitabı gün boyu izleyerek E'ye dönüşmesini engeller.
  - **Sayaç yalnızca `opens_allowed=True` olan ve geçerli bir snapshot'ı bulunan döngüleri sayar.** Girişlerin bloklu olduğu döngüler (R3 günlük kayıp, pause, outage) izni sınırsız açık tutar. Engine'in "bloklu bir zamanlanmış rebalance tüketilmez" kuralı böylece korunur.
  - Bloklu gün sayısı arm başına raporlanır.
  - Adaptör bunu kendi `allow_rebalance` argümanıyla yapar; core'un `last_rebalance`'ı yok sayılır. `Trigger` ve core değişmez.
  - Yeniden deneme sayısı ve 1 saat sonunda hâlâ tamamlanmamış rebalance sayısı, arm başına ve nedeniyle raporlanır.
  - E bunu doğası gereği her snapshot'ta yapar.
- **Maliyet ölçümü (red-team C2):**
  - `spread_slip` = Σ |q| × (fill fiyatı − mid_{t+15m}), yönüyle. Yalnızca yarım spread ve slippage'tır.
  - `delay_drift` = Σ q × (mid_{t+15m} − mid_t), yönüyle. **Price P&L'in alt kalemidir**, carry_net'e girmez ve ayrı raporlanır.
- **Look-ahead tanımı (red-team M6):**
  - (a) t'deki `decide()` çıktısı ve phase-1 emir seti, t sonrası veriden bağımsızdır (test: t sonrası bozulunca değişmez).
  - (b) Core'un phase-2 yeniden kontrolü (`core.py` phase 2), t + 15m fill'lerini içeren equity'yi görür. Bu bağımlılık beyan edilmiş ve kabul edilmiştir: gerçek bir emir akışında da open'lar close fill'lerinden sonra kontrol edilir.
- **Gün değerlemesi:** E_d = `open_ts = d 23:45` mark barının kapanışıyla (yani d+1 00:00'daki fiyat) ve o andaki fill'lerden önce yapılır.
  - 23:30'da verilen bir emrin fill'i (23:45) d gününe yazılır.
  - 23:45'te verilen bir emrin fill'i (d+1 00:00) **d+1 gününe** yazılır. Replay, fill'i kendi zaman damgasının (t + 15m) gününe muhasebeler; döngünün zamanına değil (test ile).
- **Settlement:** Point-in-time görünüm yalnızca sinyali kısıtlar. Settlement, `T + 1h ≤ now` olan her dönemin rate'ini okuyabilir.

## 4. Arm'lar (primary karşılaştırma, DEFAULT profil)

| Arm | Ad | `decision_mode` | `trigger` |
|---|---|---|---|
| **W** | **weekly control** (Pazartesi 00:00) | `rebalance_to_target` | `weekly`, weekday 0, at_hour 0 |
| **D** | daily rebalance | `rebalance_to_target` | `daily`, 00:00 |
| **C** | daily cost-aware | `cost_aware` | `daily`, 00:00 |
| **E** | **15-minute point-in-time event-driven cost-aware** | `cost_aware` | `event` (her 15m snapshot) |
| *I* | frictionless ideal (referans) | Her snapshot'ta ideal FL30 kitabı; **sabit notional** 0,25 × E0 leg başına; maliyet ve gecikme sıfır; yalnızca funding toplanır | — |

**Ortak:** R6 + §3 + `carry.risk` profili (R1–R19, leg stop %15, stop cooldown, R3 günlük kayıp %2, R4 drawdown %15) + `max_net_frac` 0,10 + R10 (resize yok) + `k = min(2, uygun // 2)`.

**Adlandırma:**
- W bir **weekly control**'dür, **"V4 baseline" değildir.**
- V4 semantiğini birebir üreten tek şey §5'teki **W-V4** replay'idir; W ile aynı raporda yer alır.
- W ile W-V4 arasındaki her fark §5.3'te adıyla listelenir.

## 5. W parity verification (freeze'den önce zorunlu)

Parity iki katmanlıdır.
- **Katman A:** V4 semantiğini tam üreten ayrı bir replay (W-V4).
- **Katman B:** Arm'ların gerçekten kullandığı V5 bileşenlerinin V4'e karşı doğrulanması (red-team M3).

İkisi de yalnızca V4'ün zaten yayımlanmış sonucunu yeniden üretir.

### 5.1 Referans (V4 kodu, salt-okunur)

- Frozen V4 Stage 1 kodu (`b074b69`), geçici ve detached bir worktree'de çalıştırılır. Worktree koşudan sonra silinir.
- **Çağrı:** Runner'ın `run_stage`'i değil, küçük bir harness kullanılır. Harness V4'ün kendi fonksiyonlarını çağırır:
  - `load_stage_inputs` eşdeğeri, ama `var/history_v5/frozen` kopyasından;
  - `H2Rule` + `run_h2`, `H2-L30-k2` için, cost row'lar **PRIMARY ve S-COST2X**;
  - V4 `stats`.
- Çıktılar (günlük satırlar, trade listesi, sayaçlar) `var/ops/v5_parity/` altına yazılır; `docs/results` değişmez.
- **Tam fill günlüğü:** `run_h2`'nin trade listesi yalnızca rebalance fill'lerini içerir. Harness bu yüzden V4 `_Sim.fill`'i salt-okunur bir sarmalayıcıyla izler ve **her** fill'i kaydeder: rebalance, margin küçültme, likidasyon (ceza dahil), delisting kapanışı. Kayıt alanları ts, varlık, q, px, half spread, ceza ve kaynaktır. V4 kodu değişmez.
- Gün sonu değerleme fiyatları da (V4 `market.value`) kaydedilir.
- **Referans kapısı:** Harness çıktısı, commit edilmiş `summary.json`'daki H2-L30-k2 değerlerini birebir üretmelidir: `mean_daily`, `sharpe`, `max_drawdown`, `worst_day`, `es975`, `cost2x_mean_daily`, `carry_net_mean`, `lb95_b28`, `lb95_b14`, `ub95_b28`. Ayrıca `research-v4.md`'deki funding/ücret/spread/fiyat toplamlarını üretmelidir.
- Üretmezse önce girdi sha'ları Stage 1 manifest'iyle karşılaştırılır (§2.2). Fark kapanmadan parity'ye geçilmez.

### 5.2 Katman A: W-V4 (`luna/carry/parity.py`, replay-only)

**Uygulanan V4 H2 kuralları:**
- günlük adım 00:00, yalnızca Pazartesi rebalance;
- V4 `elig_h2_30`; uygun < 2k ise flat hafta;
- fill = [t, t+1h) içindeki ilk dolu trade barının kapanışı (mid);
- ücret + yarım spread ayrı (V4 §5.3, analytics [t−1h, t), fallback 10 bps);
- hep-ya-hiç yürütme;
- **tutulan leg'lerin 0,5/k × E'ye resize'ı** ve V4 minimum emir kuralı;
- günlük margin buffer ve likidasyon;
- değerleme `d 23:45` trade kapanışında;
- leg stop ve risk motoru yok.

**Kullanılan V5 bileşenleri:** veri yükleyicileri (frozen kopya), `FundingLedger` pro-rata semantiği, ücret ve spread aritmetiği. `CarryCore` kullanılmaz ve değişmez. Paper engine bu profili yükleyemez (test ile).

### 5.3 Katman B: bileşen parity'si (arm'ların hattı)

- **B1, muhasebe:**
  - Referansın **tam fill günlüğü** (§5.1) V5'in `Portfolio` + `FundingLedger` + maliyet aritmetiğinden geçirilir. Mark olarak V4'ün gün sonu değerleme fiyatları kullanılır. V4'ün `liquidation_penalty`'si ayrı bir kalemde karşılaştırılır; V5 Portfolio'da bu kalem yoktur.
  - Arm'ların kullandığı aynı sınıflar kullanılır; replay executor'ın fiyat kaynağı referans fiyatlara bağlanır.
  - Günlük funding, ücret, spread ve fiyat P&L'i, referansla §5.4 toleransında eşleşmelidir.
- **B2, snapshot:**
  - §3 builder'ın parity modu (trade-bar mid, V4 spread penceresi) Pazartesi 00:00'larında V4 `HistoricalMarket.quote` ile aynı mid, half-spread ve fallback bayrağını vermelidir.
  - Ayrıca primary mod (mark mid, sürekli seri) ile parity modu arasındaki fark, fiyat seviyesi değil yalnızca göreli fark olarak raporlanır.
- **B3, sinyal:**
  - V5 FL30 ve funding coverage hesabı, Pazartesi 00:00'larında V4 `fl(…, 30)` ile aynı değeri ve aynı kapsam sayısını vermelidir.

### 5.4 Parity kalemleri ve toleranslar (A ve B)

| Kalem | Tolerans |
|---|---|
| Rebalance zamanları ve fill zaman damgaları | Birebir |
| Uygun evren, long/short seçimleri | Birebir |
| Notionaller (hedef, dolan q × px) | Göreli ≤ 1e-9 |
| Günlük funding, ücret, spread/slippage, price P&L | Mutlak ≤ 1e-9 USD/gün |
| Günlük getiri (238 gün) | max |Δ| ≤ 1e-10 |
| Kümülatif (final equity, max DD, Sharpe, mean_daily) | 1e-6 USD / 1e-9 |
| Sayaçlar (atlanan, min-emir, fallback, margin, likidasyon, flat hafta) | Birebir |
| FL30, kapsam sayısı (B3) | 1e-12 / birebir |

**Hüküm:**
- **PARITY PASS:** A ve B bütün kalemlerde tolerans içinde.
- **PARITY FAIL:** Her farklı kalem için ilk ayrışan gün, varlık ve kök neden yazılır. Ya düzeltilir ya da V4'ün beyan edilmiş davranışı birebir taklit edilir. Kabul edilmiş bir fark bırakılmaz.
- Parity PASS olmadan freeze yok.

**A-priori MDE (red-team m4):**
- Referansın 233 günlük (2025-11-10 → 2026-07-01) günlük `carry_net` serisinin b = 28 bootstrap std'sinden, Δcarry için %80 güçte MDE hesaplanır. Bu seri zaten yayımlanmış V4 sonucudur.
- Protokole **freeze'den önce** yazılır.
- Yalnızca W tarafının dağılımını kullanır. E'nin switch maliyeti sıçramaları varyansı artıracağı için bu bir **alt sınırdır** ve öyle etiketlenir.

### 5.5 W-V4 → W köprüsü (outcome run'da, önceden kayıtlı, sırası sabit)

**Kapsam:**
- Köprü **betimleyicidir ve sıraya bağlıdır.** Adımlar aşağıdaki sırayla açılır. 1–4. adımlar için ters sıra da raporlanır.
- Köprünün bütün adımları outcome penceresinde (233 gün, 2025-11-10'dan itibaren) koşulur.

**`parity.py` içinde (V4 motoru, tek bir kural değiştirilerek):**
1. Fiyat ve yürütme: trade-bar mid + ayrı spread → §3 snapshot'ı ve §3.2 executor'ı (mark mid, örtüşmeyen fill spread'i, 15m gecikme).
2. Slippage 0 → 2 bps.
3. Uygunluk: V4 `elig_h2_30` → V5 gate'leri. Uygun < 2k olduğunda flat → `k = min(2, uygun // 2)`.
4. Resize var → yok (R10).

**Motor değişimi (W-V4 adım 4 → `CarryCore` W):** Tek bir adım olarak ölçülür. İçerdiği farklar adıyla listelenir ve mümkün olanlar sayaçlarla nicelendirilir:
- (a) hep-ya-hiç → grup bazlı, close-before-open yürütme;
- (b) leg stop %15 ve stop cooldown;
- (c) risk motoru R1–R19 (R3 giriş blokajı, R4 kill);
- (d) `max_net_frac` trim'i ve nötrlük unwind'ları;
- (e) V4 günlük margin/likidasyon → core'un kontrolleri;
- (f) sizing'in equity tabanı (V4: rebalance anındaki perp equity; V5: core equity);
- (g) değerleme: trade kapanışı → mark kapanışı;
- (h) ilk Pazartesi başlangıcı (§2: outcome penceresi 2025-11-10'da başlar; parity 2025-11-05'te).

**Test:** Köprünün son değeri W'ye eşittir.

Köprü, freeze'den önce yalnızca sentetik veriyle test edilir. W'nin P&L'i yeni bilgidir (R1).

## 6. Metrikler

**Primary (hüküm girdisi):** `carry_net_d = (funding_pnl_d − fees_d − spread_slip_d) / E_{d−1}`. `spread_slip` §3.2'deki tanımdır (fill anındaki mid'e göre), `delay_drift` hariçtir.

**Secondary, guardrail ve diagnostic (her arm):**

| Metrik | Tanım |
|---|---|
| Net return | `E_end / E_0 − 1`; günlük `r_d` |
| Funding / price P&L | Price = net − funding + fees + spread_slip. Alt kalem `delay_drift`. |
| Fees, spread/slippage | Ayrı ayrı |
| Turnover | Σ |fill notional|; yıllık / ortalama equity |
| Switch sayısı | SWITCH, ENTER, EXIT, forced, stop, trim ayrı; zamanlanmış rebalance yeniden denemeleri |
| Holding duration | Leg başına saat: medyan, ortalama, dağılım |
| Sharpe, max DD, worst day, ES(97,5%) | Günlük |
| **Exposure** | Günlük gross / E ve net / E; flat gün sayısı |
| **Exposure-normalize carry** | `carry_net_d / (gross_d / E_{d−1})`, gross > 0 olan günlerde; Δ'sı zorunlu diagnostic |
| Carry per turnover | `funding / turnover`, `carry_net / turnover` (bps) |
| Capture ratio | Funding / I funding'i |
| Sayaçlar | Dolmayan emir, fallback (dolan emirlerde), bilinmeyen funding, `funding_stale`, R3/R4, stop |

## 7. Parametre bütçesi (önceden kayıtlı robustness, seçilmez)

| Varyant | Arm | H | min_impr bps | m | buffer bps | min_hold h | Trigger |
|---|---|---|---|---|---|---|---|
| **DEFAULT** (primary) | C, E | 168 | 5 | 1,5 | 5 | 24 | C: daily, E: event |
| P-TIGHT | E | 72 | 10 | 2,0 | 10 | 48 | event |
| P-LOOSE | E | 336 | 2,5 | 1,0 | 2,5 | 12 | event |
| P-C8 | C | 168 | 5 | 1,5 | 5 | 24 | interval 8 saat |

- Profiller beş parametreyi aynı yönde yaklaşık ×2 / ÷2 hareket ettirir. `m = 1,0` başa baş noktasıdır.
- Hükme girmez.
- **Deneme kaydı:** 4 arm + 3 varyant = 7. W-V4, köprü, I ve sensitivity satırları (S-PIT10 ve S-FB-CONS dahil) deneme sayılmaz.

## 8. Attribution: E − W

**Teleskopik ayrışım (tam, sıraya bağlı):**

```
E − W = (D − W)   günlük hedef takibi (yalnızca karar sıklığı; kural aynı: rebalance_to_target)
      + (C − D)   cost-aware kural paketi (aynı kadans)
      + (E − C)   15 dakikalık event zamanlaması (aynı kural, daha sık karar)
```

- **"Cost-aware kural paketi" (C − D)** tek bir maliyet kapısı değildir. İçerdiği kurallar: maliyet ve buffer testi, `min_hold_hours`, `min_carry_improvement_bps`, ideal-kitap koşulu, yalnızca çift halinde ENTER, funding-edge EXIT, `max_actions_per_cycle`.
- **"Timing"** tanımı: karar kuralı sabitken yalnızca karar sıklığının değişmesinin etkisi. Yani Δfunding(D − W) ve Δfunding(E − C).
- Her terim Δfunding, Δprice (içinde Δdelay_drift), Δfees ve Δspread_slip olarak ayrılır. Toplamlar E − W'ye birebir eşittir (test ile).
- Her terimde **exposure payı** raporlanır: Δ ortalama gross / E ve exposure-normalize Δcarry. Δcarry'nin ne kadarının daha çok veya daha az yatırımda kalmaktan geldiği görünür.
- **Switch ledger (C, E):** her SWITCH ve ENTER için beklenen artımlı carry, gerçekleşen artımlı funding (gerçek tutulma süresi boyunca) ve maliyet. Raporlananlar: isabet oranı ve beklenen − gerçekleşen bias.

## 9. Sensitivity satırları (zorunlu, dört arm için de)

| Satır | Değişiklik |
|---|---|
| **PRIMARY** | §3 |
| S-COST2X | Fill tarafında 2× ücret, 2× yarım spread, 2× slip. Karar kuralı default maliyetle hesaplar. |
| S-SPREAD-FIX | Sabit 10 bps full spread, hem gate hem fill |
| **S-PIT10** | Bilinme zamanı T + 5m yerine **T + 10m**, karar ve fill pencerelerinde, bütün arm'larda (karar azami 11, fill azami 2 snapshot; eşikler 6 ve 2 aynı) |
| **S-FB-CONS** | Fallback kullanan karar ve fill'ler, bütün arm'larda, 10 bps yerine varlık başına muhafazakâr bir spread ile fiyatlanır: lookback penceresindeki [2025-10-06, 2025-11-10) geçerli snapshot'ların p90'ı (outcome penceresinden önce, sabit) |
| S-NOKILL | R4 drawdown kill-switch kapalı; leg stop ve R3 aynen |
| S-UNWIND | Terminal unwind maliyeti net'e eklenir |

## 10. İstatistik ve önceden kayıtlı hüküm

**Primary statistic:** `Δcarry_d = carry_net_E,d − carry_net_W,d`. Eşleştirilmiş günlük seri, 233 gün.

**Bootstrap:**
- Merkezlenmiş moving block, dairesel değil. V4 metodu V5 kodunda yeniden yazılır ve sentetik testle V4 `stats` ile eşitlenir.
- B = 10.000, seed **20261010**.
- **Primary b = 28, robustness b = 14.** Tek taraflı %95 LB95 ve UB95.
- Δnet için de aynı bootstrap CI raporlanır (secondary).

**Δcarry PASS:** Hem b = 28 hem b = 14 için LB95 > 0.

**R4 kill kuralı (simetrik, red-team M8):** PRIMARY satırında W **veya** E'den herhangi birinde R4 kill-switch tetiklenirse:
- Δcarry ve G1–G4, G6 **S-NOKILL satırından** hesaplanır ve hüküm "KILL-AFFECTED (W/E)" etiketi alır;
- PRIMARY satırı da yanında raporlanır.

Gerekçe: kill sonrası flat kalan arm, Δcarry'yi mekanik olarak belirler.

**Spread veri kuralları (rev 3 red-team F1/F2, önceden kayıtlı):**
- **PIT-SENSITIVE:** S-PIT10 satırının hükmü PRIMARY'den farklıysa hüküm "PIT-SENSITIVE" etiketi alır ve iki hükümden **daha muhafazakâr olanı** raporlanır (sıra: WORSE < NO ADVANTAGE SHOWN < CARRY ONLY < ADVANTAGE).
- **FALLBACK-AFFECTED:** Herhangi bir arm'ın dolan notional içindeki fallback payı (karar veya fill fallback'i) W'ninkinden **1 yüzde puanından fazla** farklıysa hüküm "FALLBACK-AFFECTED" etiketi alır. S-FB-CONS satırı yanında verilir ve iki hükümden daha muhafazakâr olanı raporlanır.
- Arm başına fallback payı, dolan notional üzerinden karar ve fill fallback'i ayrı, varlık ve günün saati kırılımıyla raporlanır.

**G5 her zaman PRIMARY satırında değerlendirilir.** Yalnızca E'de olan bir kill, hükmü hangi satır kullanılırsa kullanılsın en fazla **CARRY ONLY** yapar.

**Guardrail'ler (secondary; "overall economics belirgin bozulmuyor"):**

| # | Guardrail | Bozulma eşiği |
|---|---|---|
| G1 | Total net | Kümülatif net P&L(E) − net P&L(W) ≤ 0 USD |
| G2 | Drawdown | MaxDD(E) > MaxDD(W) + 3,0 yüzde puanı |
| G3 | Kuyruk | ES97,5(E) < ES97,5(W) − 0,5 puan **veya** worst day(E) < worst day(W) − 1,0 puan |
| G4 | Sharpe | Sharpe(E) < Sharpe(W) − 0,30 |
| G5 | Risk olayı | R4 kill yalnızca E'de (W'de değil) tetiklendi |
| G6 | Maliyet dayanıklılığı | S-COST2X'te ortalama Δcarry ≤ 0 (nokta) |

**Eşiklerin kaynağı (sonuç görülmeden):**
- G2: V4 C5 MDD sınırı %15'in beşte biri.
- G3: ES sınırı −%3'ün altıda biri; worst-day sınırı −%5'in beşte biri.
- G4: Eşleştirilmiş Sharpe farkının SE'si tek bir Sharpe'ınkinden farklıdır ve önceden bilinemez. 0,30 bilinçli olarak sıkı bir **nokta** eşiğidir: gürültüyle tetiklenebilir. Bu ADVANTAGE'ı zorlaştırır, kolaylaştırmaz.
- G1 ve G4 fiyat gürültüsüne açık nokta karşılaştırmalarıdır. Bu, kullanıcının "total net E − W ≤ 0 ise CARRY ONLY" kararının bilinen sonucudur.

**Hüküm (DEFAULT profil; satır PRIMARY, ya da kill kuralına göre S-NOKILL):**

| Hüküm | Koşul |
|---|---|
| **ADVANTAGE (EXPLORATORY)** | Δcarry PASS **ve** G1–G6'nın hiçbiri bozulmamış |
| **CARRY ONLY** | Δcarry PASS **ve** G1–G6'dan en az biri bozulmuş. Bozulanlar adıyla yazılır. |
| **NO ADVANTAGE SHOWN** | Δcarry PASS değil ve b = 28 UB95 ≥ 0. A-priori MDE (§5.4) yanında verilir. |
| **WORSE** | b = 28 UB95 < 0 |

**Ek kurallar:**
- **İkincil karşılaştırmalar:** D−W, C−W, E−C için Δcarry ve Δnet; 3 test üzerinde Holm; açıklayıcıdır.
- Varyantlar yalnızca tablo halinde raporlanır.
- Post hoc MDE ayrıca etiketlenir.

**Hüküm ne değiştirir:**
- Paper config ve `live_enabled` değişmez.
- ADVANTAGE çıkarsa → §11 forward teyidi önerilir.
- Diğer hükümlerde "E, W'yi yener" iddiası desteklenmemiş sayılır. Paper modu kararı kullanıcınındır.

## 11. Teyit yolu (öneri, ayrı spec)

- Dört arm, collector snapshot'ları (gerçek bid/ask ve derinlik) üzerinde aynı replay hattıyla offline koşulur.
- Evidence başlangıcı, freeze'den ≥ 31 gün sonraki ilk 00:00 UTC'dir.
- Bakış tarihinden önce simülasyon yapılmaz.
- Paper defteri yalnızca implementasyon kontrolüdür.

## 12. Implementasyon (parity bileşenleri freeze'den önce, gerisi sonra; TDD, sentetik veri)

| Bileşen | İş |
|---|---|
| `luna/carry/history.py` | §3.1 builder (primary ve parity modu), point-in-time funding görünümü, holdout guard, sayaçlar |
| `luna/carry/replay.py` | §3.2 executor (t + 15m, bant yok, örtüşmeyen fill spread'i, delay_drift ayrımı), zamanlanmış rebalance'ın tamamlanınca tüketilmesi, I referansı, günlük ayrışım, ledger'lar |
| `luna/carry/parity.py` | W-V4 (§5.2), bileşen parity'si (§5.3), köprünün 1–4. adımları |
| `luna/carry/backtest.py` | Arm, varyant ve sensitivity satırları, köprü, attribution, bootstrap, guardrail'ler, kill kuralı, hüküm, rapor |
| `luna/cli.py` | `luna backfill-v5-spreads`, `luna v5-freeze-inputs`, `luna v5-parity`, `luna v5-backtest`. Outcome guard: tag HEAD'in atası, temiz ağaç, protokol tag ile aynı, frozen girdi sha'ları manifest ile aynı. |

**Zorunlu testler:**
- Look-ahead (§3.2a).
- Holdout: indirici, kopya, yükleyici ve runner.
- Anında fill yasağı: fill fiyatı ve zamanı t + 15m'den gelir.
- `spread_slip` drift içermez; `delay_drift` ayrı.
- Point-in-time `newest_end`: sentetik API boşluğunda `funding_stale` tetiklenir.
- Zamanlanmış rebalance, eksik fill'den sonra açık kalır.
- Teleskopik ve köprü toplamları tutar.
- Sabit sıralamada W, D, C ve E aynı kitabı tutar. Senaryoda funding, cost-aware ENTER değeri açıkça > 0 olacak kadar büyük seçilir.
- W elle hesaplanmış haftalık senaryoyu üretir.
- Eksik funding saati 0 nakit akışı sayılır.
- Bootstrap V4 `stats` ile eşit çıkar.
- Paper engine parity ve backtest modlarını reddeder.
- Depth gate, book olmayan snapshot'ta geçer.
- Spread (rev 3): bilinme T + 5m (S-PIT10'da T + 10m); karar ≥ 6/12, fill ≥ 2/3; pencereler örtüşmez; forward-fill yok; 5 dakikalık grid dışı seri ve provenance'ı 300 s gerçek snapshot olmayan dosya reddedilir; karar zamanı 15m grid'inde ve tz'li olmalıdır.
- Aynı t'de bütün arm'lar aynı spread'i görür (tek builder örneği).
- Fallback sayaçları arm, varlık, saat ve karar/fill ayrımıyla dolan notional'a atanır; sentetik 1 saatlik veri boşluğunda yalnızca o saatte işlem yapan arm'a yazılır ve FALLBACK-AFFECTED tetiklenir.

**Kalite kapısı:** Code review'da Critical = 0 ve Major = 0 olmadan outcome koşusu yok.

**Çıktılar:** `docs/results/v5-backtest/`: `summary.json` (sha256), `manifest.json` (frozen girdi sha'ları, commit, tag), `report.md`.

## 13. Red-team (2026-10-11) bulgu ↔ değişiklik

| ID | Bulgu (özet) | Bu metindeki karşılık |
|---|---|---|
| C1 | Gecikmeli fill + IOC bandı zamanlanmış arm'ları sakatlar, E yeniden dener | §3.2: replay'de bant yok; zamanlanmış rebalance tamamlanınca tüketilir; yeniden deneme sayaçları |
| C2 | 15m fiyat kayması `spread_slip` üzerinden primary metriğe sızar | §3.2: `spread_slip` fill mid'ine göre; `delay_drift` price P&L alt kalemi; §6 primary tanımı |
| M1 | Fill spread'i karar penceresiyle örtüşür | §3.2: fill spread'i [t, t+15m), örtüşmesiz |
| M2 | 5 dakikalık fallback kapsam kurallarını bozar | rev 2: §2.1 1 dakika zorunlu, eşikler 30/60 ve 8/15. **rev 3 (§15):** gerçek 1 dakika yok; kullanıcı kararıyla açıkça etiketlenmiş 5 dakikalık snapshot'lar, eşikler 6/12 ve 2/3, bilinme T + 5m. Sessiz geçiş yok. |
| M3 | Parity ayrı bir kod yolunu doğrular | §5.3: bileşen parity'si B1–B3 |
| M4 | Köprü artımlı kurulamaz, fark listesi eksik | §5.5: adımlar ve motor değişimi (a)–(h), sıra sabit, ters sıra raporu |
| M5 | V4 referans koşusu tanımsız, girdiler kaymış olabilir | §5.1 harness + S-COST2X; §2.2 frozen kopya ve sha karşılaştırması |
| M6 | Look-ahead testi motorla çelişir; fill ts; gün sınırı | §3.2: look-ahead tanımı (a)/(b), fill ts = t + 15m, gün değerlemesi |
| M7 | `newest_end` point-in-time değil | §3.1: t'ye kesilmiş funding görünümü + test |
| M8 | Kill kuralı tek yönlü; exposure etkisi | §10 simetrik kill kuralı; §6 ve §8 exposure ve exposure-normalize carry |
| M9 | Bug sonrası hangi koşunun hüküm verdiği belirsiz | R9 |
| M10 | Holdout'un diskte işlenmesi ve terminal anlar | R2, §2.2, §2 pencere sonu |
| m1 | "Cost-gate" etiketi yanlış; timing tanımsız | §8 |
| m2 | Depth NaN; hacim barları; başlangıç | §3.1, §2 |
| m3 | G1/G4 gürültüsü; G4 gerekçesi | §10 Δnet CI, G4 metni |
| m4 | Güç yalnızca post hoc | §5.4 a-priori MDE |
| m5 | I sizing'i tanımsız; terminal unwind | §4 I sabit notional; §9 S-UNWIND |
| m6 | Audit kırılımları eksik | §2.1 S0.3 |
| m7 | Test senaryosu ENTER değerine bağlı | §12 |

**Doğrulama turu (aynı reviewer):**
- C1, C2, M1, M2, M4, M6, M7, M9 ve M10 kapandı. M3, M5 ve M8 kısmi kaldı.
- Düzeltmelerin getirdiği 4 yeni Major ve karşılıkları:

| ID | Bulgu | Karşılık |
|---|---|---|
| N1 | Zamanlanmış rebalance'ın "tamamlanma" tanımı yok; W ve D, E'ye dönüşebilir | §3.2: `CycleResult` tabanlı tanım; yeniden deneme en fazla 4 snapshot (1 saat) |
| N2 | Kill olunca hüküm S-NOKILL satırına geçtiği için G5 hiç tetiklenemiyor | §10: G5 her zaman PRIMARY satırında; yalnızca E'de kill → en fazla CARRY ONLY |
| N3 | V4 trade listesi margin, likidasyon ve delisting fill'lerini içermiyor | §5.1 tam fill günlüğü (salt-okunur sarmalayıcı); §5.3 B1 ceza kalemi ve V4 değerleme fiyatları |
| N4 | Frozen kopyada spot barları ve index eksik; listeleme yaşı tam geçmiş gerektiriyor | §2.2: spot barları ve index eklendi, tam geçmiş, V4 dizin düzeni |
| minor | Köprü penceresi; MDE alt sınır; fill günü; settlement okuması | §5.5, §5.4, §3.2 |
| N5 | 4 snapshot sınırı, girişlerin bloklu olduğu döngüleri de sayarsa W'nin haftalık rebalance'ı R3 gününde tükenir | §3.2: sayaç yalnızca `opens_allowed` ve geçerli snapshot olan döngüleri sayar; bloklu günler raporlanır |

**Doğrulama turu sonucu:** N1–N4 kapandı. N5, reviewer'ın önerdiği tek satırla kapatıldı. Reviewer'ın hükmü: bu düzeltmeden sonra **READY FOR FREEZE AFTER STAGE 0 + PARITY**, yeni bir red-team turu gerekmez.

## 14. Stage 0 ve parity bulguları (2026-10-11, outcome-free; metodoloji değiştirilmedi)

**Stage 0: BLOCKED — 1M SPREAD UNAVAILABLE** (`docs/results/v5-stage0-audit.md`)
- Kraken analytics `spreads` 60 s serisi, ~2025-02 sonrasında (protokol penceresinin tamamı), ortak bir saatin 5 dakikalık snapshot'larının ileri taşınmış halidir. Değer bir noktadır, ortalama değildir.
- Bu yüzden §2.1'deki 1 dakika şartı karşılanamıyor. Backfill yapılmadı. 5 dakikaya geçiş kullanıcı kararıdır (seçenekler audit belgesinde).
- §2.1 ve §3'teki kapsam eşikleri (30/60, 8/15) ve "1 dakikalık" ifadeleri rev 3'te kullanıcı kararıyla yeniden yazıldı (§15).

**§2.2 düzeltmesi**
- V4 Stage 1 `manifest.json` girdi sha'sı içermiyor. Girdi tutarlılığının kanıtı, frozen kopya üzerinde V4 referansının yayımlanmış sonucu birebir (71 kontrol, Δ = 0) üretmesidir.
- Parity için kullanılan kopya `var/history_v5/frozen_parity/` (manifest sha `88633d83…abc94`). Spread kararından sonra bütün outcome girdilerini içeren ayrı bir `frozen/` kopyası oluşturulacak.

**§5 sonucu: PARITY PASS** (`docs/results/v5-parity-report.md`)
- Referans, Katman A ve B1/B2/B3 bütün §5.4 toleranslarında.
- Gerçek veride tetiklenmeyen yollar sentetik stres verisinde V4'e karşı PASS: margin, likidasyon, işaret değişimi, eksik bar, atlanan aksiyon, min-emir.
- W-V4, V4'ün iki durumu taklit etmez; ikisi de açık hata verir (`ParityUnsupported`):
  - sıra dışı fill zamanı (V4 funding yolunda örtüşmeyi çift sayar);
  - NaN funding satırı.
  Gerçek veride ikisi de oluşmuyor.

**§5.4 a-priori MDE:** Δcarry için b = 28 → 6,6e-5/gün; b = 14 → 6,3e-5/gün. Alt sınırdır.

## 15. rev 3: spread çözünürlüğü değişikliği (pre-freeze, outcome görülmeden)

**Karar (kullanıcı, 2026-10-11):** Primary tarihsel spread kaynağı gerçek 5 dakikalık Kraken analytics snapshot'larıdır (`interval=300`).

**Kayıt:**
- Bu bir **metodoloji değişikliğidir.** Freeze'den önce, hiçbir tarihsel V5 getirisi, P&L'i, arm karşılaştırması veya spread'in strateji etkisi görülmeden yapılmıştır (R1).
- Tetikleyici yalnızca veri semantiğidir (§14): protokol penceresinde gerçek 1 dakikalık spread yok.

| Kural | rev 2 | rev 3 |
|---|---|---|
| Kaynak çözünürlüğü | 1 dakika (zorunlu) | 5 dakikalık tarihsel snapshot (`interval=300`) |
| Karar kapsamı | ≥ 30/60 örnek, [t−1h, t) | ≥ 6/12 snapshot, [t−1h, t) |
| Fill kapsamı | ≥ 8/15 örnek, [t, t+15m) | ≥ 2/3 snapshot, [t, t+15m) |
| Bilinme zamanı | aralık sonu ≤ kullanım anı | muhafazakâr: T etiketli snapshot T + 5m'de bilinir |
| V4 tutarlılığı | 1 dakikalık satırlar birebir | 5 dakikalık snapshot değer ve kadans parity'si |
| 60 s endpoint | primary | primary'de kullanılmaz |

- **Değişmeyenler:** 10 bps fallback ve S-SPREAD-FIX, geçerlilik tanımı (≤ 0, NaN, sonsuz, > 500 bps), %90 kapsam bayrağı, örtüşmeyen pencereler, 15 dakikalık gecikme, W parity (V4 semantiği, V4'ün kendi 60 s pencereleri).
- Freeze'den önce bağımsız metodolojik red-team zorunludur.

## 16. rev 3 bağımsız metodolojik red-team (2026-10-11)

**Hüküm:** NOT READY (2 Major), ikisi de ucuz düzeltme. `Spread5m` yazılı kurala birebir uyuyor (sınırlar, eşikler, geçerlilik, forward-fill yok, grid reddi, holdout guard).

| ID | Sev | Bulgu | Karşılık |
|---|---|---|---|
| F1 | Major | T + 5m kuralı sınırda: faz-4 günlerinde (≈ %22) t−5 etiketli snapshot ≈ [t−1m, t] içinde alınır; 60 s etiketinin [m, m+1m] gözlem çıkarımı kesin doğrulanmadığı için birkaç saniyelik look-ahead dışlanamaz | Primary T + 5m (kullanıcı kararı) korunur. **S-PIT10** sensitivity satırı ve PIT-SENSITIVE kuralı (§9, §10). `Spread5m(known_after=10m)` + test (t−5 etiketi değişince karar değişmez). |
| F2 | Major | Sabit 10 bps fallback yalnızca E'nin kullanabileceği bir opsiyon: E boşluk saatlerinde yapay ucuz maliyetle işlem yapabilir; W/D/C 00:00'a bağlı | Kural değişmez. Arm başına fallback payı raporu, **FALLBACK-AFFECTED** tetikleyicisi (> 1 puan) ve **S-FB-CONS** satırı (§9, §10); sayaç testi §12'de |
| F3 | Minor | %90 kapsam bayrağı havuzlanmış mı varlık başına mı belirsiz | §2.1: varlık ve varlık × saat hücresi; audit script'i buna göre |
| F4 | Minor | Kaynağın kendi ileri taşıması geçerli snapshot sayılır | §2.1: tekrarlayan değer oranı raporlanır |
| F5 | Minor | `decision(t)` grid dışı t'yi sessizce kabul ediyor | `Spread5m` tz'siz veya 15m grid dışı t'yi reddeder (test) |
| F6 | Minor | 900 s veya inceltilmiş 60 s seri grid kontrolünü geçebilir | `load_spread5m` provenance'ta 300 s istenen + 300 s gözlenen kadans + sha şartı (test) |
| F7 | OK | Arm'lar aynı t'de aynı spread'i görür | §12 testi (tek builder) |
| F8 | OK | 15m fill modeli 5m snapshot'larla tutarlı; fill penceresi karar penceresinden ayrı | — |
| F9 | Minor | Gün içi faz kayması V4 kontrolü (b)'yi düşürebilir | §2.1: FAIL ölçütü değer parity'si; kadans istisnaları gün listesiyle raporlanır |
