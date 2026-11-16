# TTS (Tını)

Kendi sunucunuzda çalışan Türkçe metinden konuşma platformu: **API + yönetim paneli + kuyruk + webhook**.
Varsayılan model **EMA Lightning**; ikinci ses **Ahenk Cep**. İşlemcide (CPU) çalışır, GPU gerekmez.

```
                 ┌──────────────── Dokploy sunucusu ────────────────┐
 CRM ─HTTPS──▶  api.alanadiniz.com ─┐                                │
 Siz ─HTTPS──▶ panel.alanadiniz.com ┴▶ app (API + panel + SQLite)    │
                                       │  kuyruk · webhook · temizlik │
                                       └──iç ağ──▶ engine (modeller)  │
                 └────────────────────────────────────────────────────┘
```

- **app**: Herkese açık API (`/v1`), yönetim paneli, veritabanı (SQLite, WAL), ses dosyaları, kuyruk, webhook gönderimi, otomatik silme.
- **engine**: Yalnız ses üretir. Dışarıya kapalıdır; yalnız app, iç parola (`ENGINE_TOKEN`) ile ulaşır.

Model değişse de API uçları ve yanıt biçimi değişmez. Sesler dışarıya kimlikleriyle (`ema`, `ahenk-cep`) açılır;
yeni bir model eklendiğinde panelden "varsayılan ses" yapmanız yeterlidir.

## Özellikler

**API**
- `POST /v1/speech`: Ses hazır olunca **ses dosyasının URL'si** döner. Bekleme süresini aşarsa `202` ile iş kimliği döner.
- `POST /v1/jobs`: Uzun metinler için. Hemen döner; iş bitince **imzalı webhook** gönderilir.
- `Idempotency-Key`: Aynı istek iki kez işlenmez.
- `reference_id` ve `metadata`: Kendi kayıt bilgilerinizi gönderirsiniz; yanıtta ve webhook'ta geri gelir.
- İmzalı, tahmin edilemez dosya bağlantıları. İsterseniz bağlantılara süre sınırı konabilir.
- Tutarlı hata biçimi, `X-Request-ID`, `X-RateLimit-*` ve `Retry-After` başlıkları.
- Denenebilir belgeler: `/docs`

**Projeler ve anahtarlar**
- Her proje için:
  - Dakikalık istek sınırı ve aynı anda çalışan iş sayısı
  - Tek istekte en fazla karakter
  - Günlük ve aylık karakter kotası
  - İzinli sesler ve varsayılan ses
  - Saklama süresi
  - Webhook adresi
  - IP izin listesi ve CORS kaynakları
- Proje başına birden çok API anahtarı:
  - Yetki seçilebilir: ses üret / oku / sil
  - Son kullanma tarihi verilebilir
  - İptal edilebilir
  - Son kullanım zamanı ve IP'si görünür
- Anahtarlar veritabanında yalnız HMAC özeti olarak saklanır; tam hâli yalnız oluşturulduğu anda bir kez gösterilir.

**Panel**
- Genel bakış: grafik, kuyruk ve depolama durumu
- Projeler, anahtarlar ve kullanım
- İstek kayıtları: arama, filtre, dinleme, indirme, yeniden deneme, silme, "silinmesin" işareti
- Deneme alanı: okunan kelime vurgulanır
- Sesler
- Ayarlar
- Kullanıcılar (yönetici / izleyici)
- İşlem kaydı
- Hesap: ad, e-posta ve şifre değiştirme, açık oturumlar
- Hazır ayarlar: haber, hikâye, masal, duyuru, sosyal medya (düzenlenebilir, yenileri eklenebilir)
- Giriş e-posta ve şifreyle yapılır
- Açık/koyu tema, mobil uyumlu

**Saklama**
- Ses dosyaları varsayılan olarak **7 gün** sonra silinir.
- Süre değiştirilebilir, otomatik silme kapatılabilir; her proje kendi süresini belirleyebilir.
- Tek tek sesler "silinmesin" diye işaretlenebilir.
- İstek kayıtları ayrıca belirlenen süre (varsayılan 90 gün) sonunda temizlenir.

**Güvenlik**
- Panel oturumu HttpOnly + SameSite=Strict çerezle tutulur; CSRF belirteci ve hatalı giriş sınırı vardır.
- Şifreler scrypt ile saklanır.
- İçerik Güvenlik Politikası (CSP), HSTS ve X-Frame-Options başlıkları gönderilir.
- Webhook'lar özel ağ adreslerine gönderilmez (SSRF koruması).
- İstek gövdesi boyutu sınırlıdır.
- Panel ve API domainleri ayrılabilir.
- Tüm yönetim işlemleri işlem kaydına yazılır.

## Dokploy'a kurulum

**Gereken:** Dokploy kurulu bir sunucu (Hostinger KVM 8 yeterli: 8 çekirdek, 32 GB RAM) ve iki alt alan adı.

1. **Kodu GitHub'a yükleyin.** Bu klasörün içeriğini repo köküne koyun. Repo gizli olabilir.
2. **DNS kayıtlarını ekleyin.** İki A kaydı açın; ikisi de sunucu IP'sini göstersin:
   - `api.alanadiniz.com`
   - `panel.alanadiniz.com`
3. **Dokploy'da Compose servisi oluşturun:** **Create Service → Compose**.
   - Provider: GitHub, Branch: `main`, Compose Path: `./docker-compose.yml`
   - **Isolated Deployment kapalı kalsın.** Proje Dokploy'un `dokploy-network` ağını kullanır.
4. **Environment sekmesi:** `.env.example` içeriğini yapıştırın ve şunları doldurun:
   - `SECRET_KEY` ve `ENGINE_TOKEN`: İkisi için de sunucuda ayrı ayrı `openssl rand -hex 32` çalıştırıp çıktıyı yapıştırın.
   - `ADMIN_EMAIL` ve `ADMIN_PASSWORD`: Panele bu e-posta ve şifreyle giriş yaparsınız. Şifre en az 10 karakter olsun; büyük harf, küçük harf ve rakam içersin.
   - `PUBLIC_API_URL`, `PANEL_HOSTS`, `API_HOSTS`: Kendi alan adlarınız.
5. **Domains sekmesinde iki domain ekleyin. İkisi de `app` servisine, `8000` portuna gider.** Eski sürümden
   geliyorsanız (`panel` / `api` servisleri) mevcut domainleri düzenleyip servis adını `app` yapın; yoksa Dokploy
   "Domain ... is attached to service "panel" which does not exist" hatası verir.

   | Service | Host | Port | HTTPS |
   |---|---|---|---|
   | `app` | `panel.alanadiniz.com` | 8000 | Açık (Let's Encrypt) |
   | `app` | `api.alanadiniz.com` | 8000 | Açık (Let's Encrypt) |

6. **Deploy'a basın.**
   - İlk kurulum yaklaşık 5–10 dakika sürer.
   - İlk açılışta modeller (~70 MB) indirilir ve kalıcı diske kaydedilir.
   - `engine` loglarında `Hazır. Sesler: ema, ahenk-cep` satırını görünce sistem hazırdır.
7. **İlk kullanım:** `https://panel.alanadiniz.com` adresinde `ADMIN_EMAIL` / `ADMIN_PASSWORD` ile giriş yapın.
   - **Projeler → Yeni proje** ile proje açın.
   - **API anahtarları → Yeni anahtar** ile anahtar oluşturun.
   - Anahtarı CRM'inize kaydedin; pencere kapandıktan sonra bir daha gösterilmez.

> `SECRET_KEY`'i sonradan değiştirmeyin. Değişirse tüm API anahtarları ve ses bağlantıları geçersiz olur.

## CRM entegrasyonu

Tüm isteklerde `Authorization: Bearer <API_ANAHTARI>` başlığı gönderilir. Panelde her projenin
**Entegrasyon** sekmesinde, kendi adresinizle hazırlanmış cURL, Node.js, PHP ve webhook doğrulama örnekleri vardır.

### 1) Kısa metin: hemen ses (önerilen)

```bash
curl -X POST https://api.alanadiniz.com/v1/speech \
  -H "Authorization: Bearer $TINI_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: post-4821" \
  -d '{"text": "Yeni kampanyamız bugün başladı!", "reference_id": "post-4821"}'
```

```json
{
  "id": "gen_8Kx2…",
  "object": "speech",
  "status": "succeeded",
  "voice": "ema",
  "format": "mp3",
  "text_chars": 33,
  "reference_id": "post-4821",
  "metadata": null,
  "audio": {
    "url": "https://api.alanadiniz.com/files/gen_8Kx2….mp3?s=…",
    "download_url": "https://api.alanadiniz.com/files/gen_8Kx2….mp3?s=…&download=1",
    "format": "mp3", "mime_type": "audio/mpeg", "duration": 2.41, "size_bytes": 38912, "sample_rate": 48000
  },
  "error": null,
  "created_at": "2026-11-16T10:12:00Z",
  "completed_at": "2026-11-16T10:12:01Z",
  "processing_ms": 640,
  "expires_at": "2026-11-23T10:12:01Z"
}
```

Yanıt kodları:
- **200**: Ses hazır.
- **202**: Bekleme süresi doldu ama iş sürüyor. `Location` başlığındaki adresten durumu izleyin ya da webhook'u bekleyin.
- Sesi JSON yerine doğrudan dosya olarak almak için `?response=binary` ekleyin ya da `Accept: audio/mpeg` başlığını gönderin.

### 2) Uzun metin: iş kuyruğu + webhook

```bash
curl -X POST https://api.alanadiniz.com/v1/jobs -H "Authorization: Bearer $TINI_API_KEY" \
  -H "Content-Type: application/json" -d '{"text": "…uzun metin…", "reference_id": "video-77"}'
# → 202 {"id": "gen_…", "status": "queued", "queue_position": 0, …}

curl https://api.alanadiniz.com/v1/jobs/gen_… -H "Authorization: Bearer $TINI_API_KEY"
```

İş bitince projenin webhook adresine bir POST gönderilir:

```
X-Tini-Event: speech.succeeded            (ya da speech.failed / speech.canceled)
X-Tini-Delivery: whd_…
X-Tini-Timestamp: 1763287921
X-Tini-Signature: sha256=<HMAC-SHA256(imza_anahtarı, "{timestamp}.{ham gövde}")>

{"id": "whd_…", "event": "speech.succeeded", "created_at": "…", "data": { …yukarıdaki yanıtın aynısı… }}
```

- Webhook'a 2xx ile yanıt verin.
- Yanıt gelmezse artan aralıklarla yeniden denenir: 10 sn, 1 dk, 5 dk, 30 dk, 2 sa, 6 sa.
- İmza anahtarı panelde, projenin **Webhook** sekmesinde.

### Hazır ayarlar ve ses ayarları (hepsi isteğe bağlı)

```jsonc
{ "text": "…", "preset": "haber" }                          // haber ayarlarıyla
{ "text": "…", "preset": "hikaye", "speed": 1.0 }           // hikâye ayarları, ama hız 1.0 (açık alan ezer)
{ "text": "…", "voice": "ema", "speed": 1.1, "pitch": -1 }  // hazır ayar olmadan kendi ayarlarınız
{ "text": "…" }                                              // projenin varsayılan hazır ayarı / sistem varsayılanları
```

Öncelik sırası: **istekte açıkça yazılan alan → istekteki `preset` (yoksa projenin varsayılan hazır ayarı) → sistem varsayılanı**.
Hazır ayarlar panelden (**Ayarlar → Hazır ayarlar**) eklenir ve düzenlenir; bir hazır ayar ses, üslup, hız, perde,
duraklamalar, ses seviyesi, dosya biçimi ve örnekleme hızını içerebilir. Projeye varsayılan hazır ayar
**Projeler → proje → Genel** bölümünden atanır.

### Diğer uç noktalar

| Uç nokta | Ne yapar |
|---|---|
| `GET /v1/jobs?limit=20&starting_after=gen_…&reference_id=…` | İşleri listeler (sayfalı) |
| `POST /v1/jobs/{id}/cancel` | Bekleyen ya da çalışan işi iptal eder |
| `DELETE /v1/jobs/{id}` | Ses dosyasını hemen siler |
| `GET /v1/voices` | Projenin kullanabileceği sesler |
| `GET /v1/presets` | Hazır ayarlar ve içerdikleri değerler |
| `GET /v1/usage` | Sınırlar ve bugünkü / bu ayki kullanım |
| `POST /v1/normalize` | Metnin okunuş biçimini gösterir (ses üretmez) |

### İstek alanları

| Alan | Varsayılan | Açıklama |
|---|---|---|
| `text` | zorunlu | Türkçe metin; paragraflar boş satırla ayrılır |
| `preset` | projenin varsayılanı (yoksa hiçbiri) | Hazır ayar: `haber`, `hikaye`, `masal`, `duyuru`, `sosyal-medya` ya da panelde eklediğiniz. İstekte açıkça yazdığınız alanlar hazır ayarı ezer |
| `voice` | projenin varsayılanı | `ema`, `ahenk-cep` |
| `style` | `neutral` | `calm`, `cheerful`, `empathetic`, `sad`, `angry`, `surprised` (yalnız Ahenk Cep) |
| `speed` | 1.0 | 0.5–2.0 |
| `pitch` | 0 | −4…+4 yarım ton |
| `pause_scale` | 1.0 | 0–3, cümle arası duraklama çarpanı |
| `paragraph_pause` | 0.7 | 0–3 sn |
| `volume` | 0 | −20…+10 dB |
| `normalize_loudness` | true | Ses seviyesini yayın seviyesine dengeler |
| `format` | `mp3` | `mp3`, `wav`, `ogg`, `flac` |
| `sample_rate` | 48000 | 48000, 24000, 16000, 8000 |
| `seed` | 0 | Aynı tohum aynı sesi verir; `null` = rastgele |
| `reference_id` | | Sizin kayıt kimliğiniz (≤128 karakter) |
| `metadata` | | Geri dönen serbest JSON (≤4 KB) |
| `webhook_url` | | Bu istek için projeninkinin yerine kullanılacak webhook adresi |
| `include` | `[]` | `["words", "spoken_text"]`: kelime zamanları ve okunuş metni |

### Hata kodları

Tüm hatalar şu biçimde döner: `{"error": {"code": "...", "message": "...", "request_id": "req_..."}}`

| HTTP | code | Anlamı |
|---|---|---|
| 401 | `missing_api_key`, `invalid_api_key`, `revoked_api_key`, `expired_api_key` | Anahtar sorunu |
| 403 | `insufficient_scope`, `ip_not_allowed`, `origin_not_allowed`, `project_disabled`, `voice_not_allowed` | Yetki ya da erişim kısıtı |
| 409 | `idempotency_conflict` | Aynı `Idempotency-Key` farklı bir metinle kullanılmış |
| 413 | `text_too_long` | Karakter sınırı aşıldı |
| 422 | `invalid_request`, `nothing_to_read` | Hatalı alan, ya da metinde okunacak bir şey yok |
| 429 | `rate_limited`, `daily_quota_exceeded`, `monthly_quota_exceeded` | `Retry-After` başlığına bakın |
| 400 | `preset_not_found`, `voice_not_found` | Böyle bir hazır ayar ya da ses yok |
| 503 | `maintenance`, `queue_full`, `voice_unavailable`, `engine_unavailable` | Geçici durum; `Retry-After` sonra tekrar deneyin |

**CRM için öneri:** Ses URL'leri saklama süresi (varsayılan 7 gün) dolunca çalışmaz. Kalıcı kullanacağınız sesleri
`audio.url` adresinden indirip kendi deponuza kaydedin.

## Model değiştirme / ekleme

1. `engine/engine/models.py` içindeki `VOICES` sözlüğüne yeni sesi ekleyin ve `load()` içinde modeli yükleyin.
2. Yeniden deploy edin.
3. Panelde **Sesler → Varsayılan yap** deyin.

CRM'deki kodunuz değişmez. `voice` göndermeyen istekler yeni varsayılan sesi kullanır.

Ses üretimini ileride başka bir makinede (örneğin GPU'lu) çalıştırmak için `engine` servisini oraya taşıyıp
`ENGINE_URL` değerini değiştirmeniz yeterli.

## Yedekleme

- Panelden indirebilirsiniz: **Ayarlar → Bakım ve yedek → Veritabanı yedeğini indir**. İndirilen dosya tutarlı bir SQLite kopyasıdır.
- Ses dosyaları `tini-data` biriminin `audio/` klasöründedir.

## Sorun giderme

- **Panelde "Ses motoruna ulaşılamıyor":** `engine` loglarına bakın. İlk açılışta modeller indirilir. `ENGINE_TOKEN` iki serviste aynı olmalıdır.
- **"all predefined address pools have been fully subnetted":** Compose ayarında Isolated Deployment'ı kapatın. Gerekirse sunucuda `docker network prune -f` çalıştırın.
- **Webhook gelmiyor:** Projenin Webhook sekmesinde **Test gönder**'e basın; son gönderimlerde hata ayrıntısı görünür. CRM aynı sunucuda ve iç adresle erişiliyorsa **Ayarlar → Kuyruk → Özel ağ adreslerine gönderime izin ver** seçeneğini açın.
- **Yavaşlık:** Hostinger'da işlemci paylaşımlıdır. `TORCH_THREADS` değerini düşürüp yükseltmeyi deneyin. Uzun metinleri `/v1/jobs` ile gönderin.

## Hız, stres ve güvenlik

Sonuçlar ve bulunan/düzeltilen sorunlar: [TEST_RAPORU.md](TEST_RAPORU.md). Kendi sunucunuzda da çalıştırabilirsiniz:

```bash
python tests/load.py     https://api.alanadiniz.com siz@alanadiniz.com <şifre>   # eşzamanlı istekler, kuyruk, yarışlar
python tests/security.py https://api.alanadiniz.com siz@alanadiniz.com <şifre>   # 67 güvenlik kontrolü
```

İlgili ayarlar (panel → Ayarlar → Kuyruk ve sınırlar):

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| Paralel işçi | 8 | Motora aynı anda giden iş sayısı. Yarısı her zaman kısa (anlık) isteklere ayrılır |
| Proje başına bekleyen iş | 200 | Tek bir projenin kuyruğu doldurmasını engeller |
| Yeni projelerde aynı anda çalışan iş | 6 | Proje ayarlarından değiştirilebilir |
| `ENGINE_GROUP_PIECES` (ortam değişkeni) | 3 | Uzun metinlerin motora kaç cümlelik gruplarla verileceği |

## Yerelde geliştirme ve test

```bash
pip install torch ema-lightning==1.1.2 fastapi "uvicorn[standard]" httpx soundfile librosa
python tests/fake_weights.py /tmp/fake          # gerçek mimaride rastgele ağırlıklar (yalnız test)
(cd engine && EMA_WEIGHTS_DIR=/tmp/fake/ema CEP_WEIGHTS_DIR=/tmp/fake/cep ENGINE_TOKEN=t \
   PYTHONPATH=.:vendor uvicorn engine.main:app --port 9000) &
(cd app && DATA_DIR=/tmp/tini ENGINE_URL=http://127.0.0.1:9000 ENGINE_TOKEN=t SECRET_KEY=$(openssl rand -hex 32) \
   ADMIN_EMAIL=siz@ornek.com ADMIN_PASSWORD=Sifre12345 PYTHONPATH=. uvicorn tini.main:app --port 8000) &
python tests/smoke.py http://localhost:8000 siz@ornek.com Sifre12345
```

İkonlar `tools/build_icons.cjs` ile Hugeicons paketinden üretilir.

## Lisans ve atıf

- Bu projenin kodu: Apache-2.0.
- EMA Lightning (Canberk Aslan): Apache-2.0.
- **Ahenk Cep ağırlıkları: CC BY 4.0, atıf zorunludur.** Bu sesi kullanarak yayınladığınız içeriklerde şunu belirtin:
  "Ahenk (https://github.com/ozguradmin/ahenk-turkish-tts), CC BY 4.0".
- `engine/vendor/trnorm`, `ahenk_cep.py`: ahenk-turkish-tts, Apache-2.0.
- İkonlar: Hugeicons Free, MIT.
- Yazı tipleri (Literata, Schibsted Grotesk): SIL OFL 1.1.
- **Bu sesler yapaydır.** Paylaşırken yapay zekâ ile üretildiğini belirtin (AB Yapay Zekâ Yasası md. 50); kimseyi taklit etmek için kullanmayın.
