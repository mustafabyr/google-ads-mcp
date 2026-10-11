# FAZ 3 — Google Ads MCP: güvenlik katmanı, araç düzeltmeleri, buluta taşıma

**Bu belge Claude Code içindir.** Depo: `~/Desktop/google-ads-mcp` (fork: `mustafabyr/google-ads-mcp`, dal `mdk/write-tools`).
Kullanım: depoda Claude Code açın ve "`03-FAZ3-CLAUDE-CODE-BRIEF.md` dosyasını oku; iş paketlerini sırayla uygula, her paketin sonunda testleri çalıştır ve bana özet ver" deyin.

## Çalışma kuralları
- Önce mevcut kodu oku: modül yapısı (`adgroups`, `campaigns`, `conversions`, `extensions`, `shared_sets`, `reports`, `audit`, `agency` …), araç kayıt biçimi, mevcut testler. **Var olan adlandırmayı ve kalıpları koru.**
- Her iş paketi ayrı commit. Canlı hesapta **hiçbir yazma testi yapma**; yazma testleri test hiyerarşisinde (test yönetici → test hesabı; kimlikler özel kurulum notunda) ya da `validate_only=True` ile.
- Sırlar (developer token, refresh token, client secret) koda, loga, commit'e girmez.
- Bir pakette belirsizlik varsa uygulamadan önce sor.

---

## İP-1 — Araç sınıflandırması ve MCP açıklamaları
Her araca bir sınıf ata ve MCP araç açıklamalarını (tool annotations) doldur:

| Sınıf | Tanım | Açıklama |
|---|---|---|
| `READ` | hesabı değiştirmez | `readOnlyHint: true` |
| `PROTECTIVE` | yalnız harcamayı azaltır/korur: negatif **ekleme**, **duraklatma**, teklif/tavan/bütçe **düşürme** | `readOnlyHint: false`, `destructiveHint: false` |
| `SPEND` | harcamayı artırabilir ya da yapıyı değiştirir: oluşturma, **etkinleştirme**, bütçe/teklif artırma, strateji, hedefleme, dönüşüm hedefi, reklam metni | `destructiveHint: false` |
| `DESTRUCTIVE` | kaldırma/silme/ayırma | `destructiveHint: true` |

**Durum araçlarını böl.** `*_set_*_status` araçları hem duraklatıyor hem açıyor; bu yüzden istemci tarafında "her zaman izin ver" yapılamıyor. Her biri için `pause_*` (PROTECTIVE) ve `enable_*` (SPEND) araçları ekle; eski araçları bir sürüm boyunca `SPEND` sınıfında bırak, sonra kaldır.
Aynı mantıkla bütçe ve teklif araçlarında yön kontrolü: yeni değer eskisinden **küçükse** PROTECTIVE yolundan, büyükse SPEND yolundan geçsin.

Kabul: sınıf listesi tek yerde (`guardrails/classes.py` gibi) durur; sınıfı olmayan araç testte **hata** verir (yeni araç eklerken unutulamasın).

## İP-2 — Hesap bazlı sınırlar ("tavan yoksa yazma yok")
`guardrails/limits.yaml` (depoda örnek, gerçek dosya `.gitignore`'da; yol ortam değişkeniyle):
```yaml
defaults:
  max_budget_change_pct: 20        # tek seferde
  min_days_between_budget_changes: 7
accounts:
  "1234567890":                    # örnek hesap
    max_daily_budget_try: 4500
    max_cpc_ceiling_try: 20
    allow_enable: true
  "1111111111": { max_daily_budget_try: 1500, max_cpc_ceiling_try: 45, allow_enable: true }
  "2222222222": { max_daily_budget_try: 800,  max_cpc_ceiling_try: 15, allow_enable: true }
```
(Rakamlar **örnektir** — gerçek tavanları kullanıcı girer.)
Kurallar: hesabın kaydı yoksa `SPEND` ve `DESTRUCTIVE` araçlar **reddedilir** ("bu hesap için sınır tanımlı değil") · sınır aşımı açık hata mesajıyla reddedilir (istenen, izin verilen, hangi anahtar) · `READ` ve `PROTECTIVE` sınır istemez.

## İP-3 — İki adımlı yazma: planla → uygula
`SPEND` ve `DESTRUCTIVE` araçlar doğrudan yazmaz:
1. Araç çağrısı bir **plan** döner: `plan_id`, nesne kimlikleri, **önce → sonra** değerleri (öncesi API'den okunur), günlük ve aylık ₺ etkisi, sınır kontrol sonucu, geri alma tarifi. Plan 30 dakika geçerli.
2. `apply_plan(plan_id)` planı uygular. Uygulamadan önce **ön koşul kontrolü:** nesnenin şu anki durumu plandaki "önce" ile aynı mı; değilse reddet. Uygulama sonrası nesneyi **geri oku** ve sonucu döndür (istenen ↔ gerçek).
3. Aynı `plan_id` ikinci kez uygulanamaz (idempotent).
`DESTRUCTIVE` planlarda `apply_plan(..., confirm_destructive=True)` şart.
Tüm mutate'lerde `validate_only` desteği; `GOOGLE_ADS_MCP_REQUIRE_DRY_RUN=true` iken ilk çağrı her zaman kuru çalışır.

## İP-4 — Denetim kaydı
Her yazma (PROTECTIVE dahil) için JSONL satırı: zaman (UTC + Europe/Istanbul), `customer_id`, araç, sınıf, `plan_id`, önce, sonra, sonuç/hata, çağıran (ortam değişkeninden kullanıcı adı). Yerelde `~/.google-ads-mcp/audit/<yıl-ay>.jsonl`; Cloud Run'da Cloud Logging + GCS kovası. Yeni `READ` aracı: `audit_get_change_log(customer_id, since)` — brifing ve QA bunu okuyacak.

## İP-5 — Sahada bulunan araç eksikleri
1. **Paylaşılan negatif listeden tek terim silme** (`shared_sets_remove_terms`, DESTRUCTIVE). Şu an yok; düzeltme için liste baştan kuruluyor.
2. **Paylaşılan listeye toplu eklemede otomatik parçalama:** ~12 terimi aşan istek reddediliyor → araç içinde 10'luk partilere böl, parti sonuçlarını birleştir.
3. **`extensions_add_call_extension` 'campaign_asset' hatası** — yeniden üret, düzelt, test ekle. Ayrıca çağrı uzantısında `call_conversion_action` boşsa uyaran kontrol.
4. **Yalnızca-arama (call-only) kampanya/reklam oluşturma** — şu an panelden Chrome ile kuruldu.
5. **Dönüşüm eylemi durumu:** `PAUSED` Google enum'unda yok, `HIDDEN`/`REMOVED` reddediliyor → aracın açıklamasına "yalnız panelden kaldırılır" notu ve anlamlı hata mesajı.
6. **Türkçe ek yardımcıları:** `negatives_expand_turkish(term)` (READ, saf işlev): ekli biçimler (-de/-da/-te/-ta, -deki/-daki, -nde/-nda, -ler/-lar) + Türkçe karaktersiz yazım önerir; eklemeyi yapmaz.
7. `agency_account_portfolio_overview` çıktısına hesap başına **çağrı uzantısı tıklaması** ve "son 7 günde harcama var mı" alanı.

## İP-6 — `audit.py`: yaşanan her olay bir kontrol olsun
Kural zaten var: *canlı hesapta bulunan gerçek sorun audit'e kontrol olarak eklenmeden kapanmış sayılmaz.* Eksikse ekle:
- `MAXIMIZE_CLICKS` + boş `cpc_bid_ceiling`
- `TARGET_IMPRESSION_SHARE` hedefi düşük **ve** sıra kaybı yüksek **ve** bütçe kullanılmıyor
- hesap düzeyi `CONTACT`/`PHONE_CALL_LEAD` hedefi biddable iken müşteri sözleşmesi "yalnız form" (müşteri sözleşmesi parametre olarak verilir)
- teklife giren tek hedef son 30 günde 0 kayıt üretmiş
- çağrı uzantısı var, `call_conversion_action` boş
- `PRESENCE_OR_INTEREST` (marka kampanyası hariç tutulabilir) · Arama Ortakları / Display açık · otomatik uygulanan öneri izi (`change_event`)
- mükerrer `OUTBOUND_CLICK` eylemleri
- paylaşılan negatif listede tek kelimelik GENİŞ terimler · negatif ↔ pozitif çakışması
- tek reklam grubu kampanya harcamasının >%50'si ve 0 birincil dönüşüm
- tek RSA'lı grup · 12'den az başlıklı RSA
- son 7 günde panelden yapılmış, karar günlüğünde karşılığı olmayan değişiklik (İP-4 kaydıyla karşılaştır)

## İP-7 — Buluta taşıma (Cloud Run) — **kullanıcı onayı ve girişi gerekir**
Amaç: zamanlanmış görevlerin Mac'ten bağımsız çalışması ve ekip arkadaşının kurulum yapmadan bağlanması. Claude'un özel bağlayıcıları sunucuya **Anthropic'in bulutundan** bağlanır; sunucu genel internetten erişilebilir olmalıdır.
1. Taşıma katmanı: **Streamable HTTP** (stdio yanında; yerel kullanım bozulmasın).
2. Kimlik doğrulama: MCP yetkilendirme belirtimine uygun **OAuth**; yalnız izinli Google hesapları (kullanıcı + ekip arkadaşı e-postaları). **Kimlik doğrulamasız uç nokta asla yayına alınmaz** — bu sunucu reklam bütçesi harcatabilir.
3. Sırlar Secret Manager'da; servis hesabı en az yetki; `limits.yaml` Secret/GCS'ten.
4. Proje: mevcut `md-kampus` (no 454953518344). Bölge Avrupa. `min-instances=0`. İstek günlüğü açık.
5. Günlük işlem kotası (Basic Access: 15.000/gün): paralel analistlerin toplam çağrısını say; %70'te uyarı alanı döndür.
6. Dağıtım betiği + geri alma betiği + README. Dağıtımı **kullanıcı** çalıştırır (`gcloud auth login`, faturalandırma onayı, OAuth istemcisi oluşturma onun işi).
7. Sonrası (kullanıcı): Claude → Customize → Connectors → özel bağlayıcı ekle → URL → OAuth → araç izinlerini `arac-yetki-matrisi.md`'ye göre ayarla → yerel MCP kaydını kaldır → **eklentide `python3 scripts/build_agents.py --google <yeni sunucu adı>` çalıştır** (araç adı öneki değişir) → `/rav-ajans:ekip-testi`.
8. Müşteri dosyaları: bulutta çalışan görevler yerel klasörü göremez. `rav-ajans-musteriler` özel deposunu GitHub bağlayıcısıyla ya da Drive klasörü olarak erişilebilir kıl; hangisi seçilirse `ajans-anayasa` §4'teki klasör kökü cümlesini güncelle.

## Kabul testi (paketlerin tamamı bitince)
- Sınırı tanımsız hesapta bütçe artırma → reddedildi, mesaj açık
- %40 bütçe artışı planı → reddedildi (%20 kuralı); %15 → plan döndü, `apply_plan` uyguladı, geri okuma eşleşti, denetim kaydı yazıldı
- Plan oluşturulduktan sonra panelden değer değiştirildi → `apply_plan` ön koşul hatası verdi
- 38 terimlik negatif ekleme → 4 partide tamamlandı
- `pause_campaign` sınır istemedi; `enable_campaign` plan istedi
- `audit_run_account_audit` yeni kontrolleri raporladı
- stdio ile yerel kullanım hâlâ çalışıyor
