# Cloud Run'a taşıma

Amaç: zamanlanmış görevlerin bir Mac'e bağlı kalmaması ve ekip arkadaşının
kurulum yapmadan bağlanması. Claude'un özel bağlayıcıları sunucuya
Anthropic'in bulutundan bağlanır; sunucu genel internetten erişilebilir olur.
**Bu yüzden kimlik doğrulamasız bir uç nokta hiçbir zaman yayına alınmaz:**
sunucu, `GOOGLE_ADS_MCP_ALLOWED_EMAILS` boşsa ya da OAuth ayarı yoksa HTTP
modunda **başlamaz** (`ads_mcp/server.py`, `require_network_auth`).

Dağıtımı **sen** çalıştırırsın (`gcloud auth login`, faturalandırma onayı,
OAuth istemcisi). Bu klasördeki betikler adımları tekrarlanabilir kılar;
hiçbir betik sır yazdırmaz ve hiçbir sır depoya girmez.

## Katmanlar

| Katman | Ne | Nerede |
|---|---|---|
| Taşıma | Streamable HTTP, `/mcp` yolu; stdio yerelde aynen çalışır | `GOOGLE_ADS_MCP_TRANSPORT=http` |
| Kimlik | Google OAuth (FastMCP `GoogleProvider`) + e-posta izin listesi | `GOOGLE_ADS_MCP_ALLOWED_EMAILS` |
| Sırlar | Developer token, OAuth client id/secret, JWT imza anahtarı | Secret Manager, `--set-secrets` |
| Sınırlar | `limits.yaml` | `gs://<kova>/limits.yaml` (`GOOGLE_ADS_MCP_LIMITS_GCS_URI`) |
| Planlar | 30 dk yaşayan planlar | `gs://<kova>/plans/` (`GOOGLE_ADS_MCP_PLAN_GCS_BUCKET`) |
| Denetim kaydı | Her yazma bir JSON satırı | stderr → Cloud Logging **ve** `gs://<kova>/audit/` |
| Kota | Günlük işlem sayacı, %70'te uyarı | `gs://<kova>/quota/` (`GOOGLE_ADS_MCP_QUOTA_GCS_BUCKET`) |
| OAuth istemci kayıtları | FastMCP'nin dinamik istemci deposu | Firestore (`GOOGLE_ADS_MCP_STORAGE_TYPE=firestore`) |

Planların kovada tutulmasının nedeni `min-instances=0`: plan ile uygulama
arasında örnek kapanabilir; diskteki plan giderdi.

## Sıra

1. **Proje ve bölge.** `md-kampus` (454953518344), `europe-west1`.
   ```
   gcloud auth login
   gcloud config set project md-kampus
   ```
2. **OAuth istemcisi** (Google Cloud → APIs & Services → Credentials → OAuth
   client, *Web application*). İlk dağıtımdan sonra çıkan URL'yi
   `https://<servis-url>/auth/callback` olarak yetkili yönlendirme URI'sine
   ekle. Onay ekranı **published** kalsın (Testing modunda refresh token 7
   günde ölür).
3. **Sırlar.** Değerleri panelden/`.env`'den al; komut satırına yazma,
   dosyadan oku:
   ```
   printf '%s' "$(sed -n 's/^GOOGLE_ADS_DEVELOPER_TOKEN=//p' .env)"       | gcloud secrets create google-ads-developer-token --data-file=-
   printf '%s' "$(sed -n 's/^GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=//p' .env)"    | gcloud secrets create google-ads-oauth-client-id --data-file=-
   printf '%s' "$(sed -n 's/^GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=//p' .env)"| gcloud secrets create google-ads-oauth-client-secret --data-file=-
   python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")'  | gcloud secrets create google-ads-jwt-signing-key --data-file=-
   ```
4. **Sınırlar dosyası.** Gerçek tavanları içeren `limits.yaml`'ı kovaya koy
   (betik kovayı yaratır; ilk dağıtımdan sonra da yükleyebilirsin):
   ```
   gcloud storage cp ~/.google-ads-mcp/limits.yaml gs://md-kampus-google-ads-mcp/limits.yaml
   ```
   Kaydı olmayan hesaba SPEND/DESTRUCTIVE yazma yapılmaz; dosya 5 dakikada
   bir yeniden okunur, yeniden dağıtım gerekmez.
5. **Dağıt.**
   ```
   ALLOWED_EMAILS=sen@example.com,ekip@example.com \
   LOGIN_CUSTOMER_ID=<MCC id, tiresiz> \
   deploy/deploy.sh
   ```
   Betik: API'leri açar, Artifact Registry deposunu, servis hesabını
   (yalnız kovaya ve sırlara erişim) ve kovayı yaratır, imajı Cloud Build ile
   kurar, `min-instances=0` ve istek günlüğü açık dağıtır, URL'yi
   `GOOGLE_ADS_MCP_BASE_URL` olarak ikinci adımda yazar.

   `--allow-unauthenticated` yalnız Cloud Run'ın ağ katmanıdır: OAuth
   akışının (`/auth/...`) çalışabilmesi için gerekir. Sunucunun kendisi izin
   listesinde olmayan her Google hesabını reddeder.
6. **Geri alma.** `deploy/rollback.sh` bir önceki revizyona, ya da
   `deploy/rollback.sh <revizyon-adı>` belirli bir revizyona döner.
7. **Doğrulama.**
   ```
   URL=$(gcloud run services describe google-ads-mcp --region=europe-west1 --format='value(status.url)')
   curl -s -o /dev/null -w '%{http_code}\n' "$URL/mcp"      # 401: kimlik istiyor, doğru
   gcloud run services logs read google-ads-mcp --region=europe-west1 --limit=50
   ```
   Denetim satırları Cloud Logging'de `ads_mcp.audit` logger'ı altında JSON
   olarak görünür; aynı satırlar `gs://<kova>/audit/<yıl-ay>/` altındadır.

## Sonrası (kullanıcı)

- Claude → Customize → Connectors → özel bağlayıcı ekle → `https://<url>/mcp`
  → Google ile giriş → araç izinlerini `arac-yetki-matrisi.md`'ye göre ayarla
  (READ ve PROTECTIVE araçlar "her zaman izin ver" olabilir; SPEND ve
  DESTRUCTIVE araçlar zaten plan üretir, `plans_apply_plan` onay ister).
- Yerel MCP kaydını kaldır (stdio kaydı kalırsa aynı araçlar iki kez görünür).
- Eklentide `python3 scripts/build_agents.py --google <yeni sunucu adı>`
  çalıştır: araç adı öneki değişir.
- `/rav-ajans:ekip-testi`.
- Müşteri dosyaları: bulutta çalışan görevler yerel klasörü göremez.
  `rav-ajans-musteriler` özel deposunu GitHub bağlayıcısıyla ya da Drive
  klasörü olarak eriştir; hangisi seçilirse `ajans-anayasa` §4'teki klasör
  kökü cümlesini güncelle.

## Kota

Basic Access günde 15.000 işlem verir. Sunucu her API çağrısını sayar (okuma
= 1, mutate = işlem sayısı; Google'ın sayımına yaklaşık) ve gün içinde %70'i
geçince her araç sonucuna `quota_warning` alanı ekler. Sayaç kovada tutulduğu
için paralel analistlerin çağrıları tek sayımda toplanır. Sınır
`GOOGLE_ADS_MCP_DAILY_OPERATIONS` ile değiştirilebilir (Standard Access'e
geçilirse).

## Ortam değişkenleri (tam liste)

| Değişken | Ne |
|---|---|
| `GOOGLE_ADS_MCP_TRANSPORT` | `stdio`, `http` ya da `auto` (OAuth ayarı varsa http) |
| `GOOGLE_ADS_MCP_ALLOWED_EMAILS` | Virgülle ayrılmış izinli Google hesapları; HTTP'de zorunlu |
| `GOOGLE_ADS_MCP_ACCESS_LOG` | `true`: her isteği günlüğe yaz |
| `GOOGLE_ADS_MCP_LIMITS_PATH` / `GOOGLE_ADS_MCP_LIMITS_GCS_URI` | Sınırlar dosyası (yerel yol ya da `gs://`) |
| `GOOGLE_ADS_MCP_PLAN_DIR` / `GOOGLE_ADS_MCP_PLAN_GCS_BUCKET` | Plan deposu |
| `GOOGLE_ADS_MCP_PLAN_TTL_MINUTES` | Plan ömrü, varsayılan 30 |
| `GOOGLE_ADS_MCP_REQUIRE_DRY_RUN` | `true`: her planın ilk uygulaması yalnız doğrulama |
| `GOOGLE_ADS_MCP_AUDIT_DIR` / `GOOGLE_ADS_MCP_AUDIT_GCS_BUCKET` | Denetim kaydı |
| `GOOGLE_ADS_MCP_QUOTA_DIR` / `GOOGLE_ADS_MCP_QUOTA_GCS_BUCKET` | Kota sayacı |
| `GOOGLE_ADS_MCP_DAILY_OPERATIONS` | Günlük işlem sınırı, varsayılan 15000 |
| `GOOGLE_ADS_MCP_ACTOR` | stdio'da denetim kaydına yazılacak kullanıcı adı (yoksa OS kullanıcısı) |
