# Hız, stres ve güvenlik test raporu (sürüm 2.1.0)

**Tarih:** 16 Kasım 2026, sürüm 2.1.0 (e-posta ile giriş ve hazır ayarlar eklendikten sonra yeniden çalıştırıldı)

**Ortam:**
- 2 çekirdek, 7 GB RAM.
- Gerçek EMA Lightning ile aynı mimari ve boyutta (8,6M parametre, 4 adım) model; ağırlıklar rastgele, konuşma hızı gerçeğe göre ayarlı.
- Ses kalitesi bu testlerin konusu değildir. Ölçülen şey hız, kuyruk davranışı ve dayanıklılıktır.

> Sizin sunucunuzda (Hostinger KVM 8, `TORCH_THREADS=6`) üretim hızının buradakinin yaklaşık **3 katı** olması
> beklenir. Bu bir tahmindir; gerçek sunucuda ölçülmedi. Aşağıdaki "sn ses / sn" değerleri doğrudan bununla çarpılabilir.

Testleri tekrar çalıştırmak için:

```bash
python tests/load.py     https://api.alanadiniz.com siz@alanadiniz.com <şifre>   # hız ve stres
python tests/security.py https://api.alanadiniz.com siz@alanadiniz.com <şifre>   # güvenlik (67 kontrol)
```

## 1. Hız

| Ölçüm | Sonuç |
|---|---|
| Kısa gönderi (~80 karakter, ~4 sn ses), tek istek | **0,41 sn** (p50), en kötü 0,56 sn |
| Üretim hızı (tek istek) | gerçek zamanın **8,2 katı** |
| 3.000 karakter (~2,5 dk ses) | 15,7 sn |
| Ses biçimi dönüştürme (MP3) payı | isteğin yaklaşık %17'si |

## 2. Aynı anda gelen istekler

| Senaryo | Başarı | Bekleme (p50 / p95 / en kötü) | Toplam üretim |
|---|---|---|---|
| 10 eşzamanlı, 20 istek | 20/20 | 2,9 / 3,8 / 4,1 sn | 11,7 sn ses / sn |
| 50 eşzamanlı, 100 istek | **100/100** | 13,4 / 14,8 / 15,6 sn | 13,1 sn ses / sn |
| 200 iş bir anda (`/v1/jobs`) | **200/200** kabul, 200/200 tamamlandı | kabul: 0,55 sn (p50) | 12,0 sn ses / sn |
| 200 iş kuyruktayken API yanıt süresi | — | `/v1/usage` 8 ms, panel 6 ms (p50) | — |
| 3 uzun haber (5.580 karakter) üretilirken 10 kısa istek | **10/10** | **5,3 / 8,9 sn** | — |

Hiçbir senaryoda hata, zaman aşımı ya da kayıp istek olmadı. Uygulama ve motor loglarında hata kaydı yok.

## 3. Yarış durumları

| Test | Sonuç |
|---|---|
| Aynı `Idempotency-Key` ile 40 eşzamanlı istek | **1** iş üretildi, 39'u aynı işi döndürdü ✅ |
| Günlük kota 1.000 karakter, eşzamanlı 40 × 100 karakter | tam **10** kabul, 30 `daily_quota_exceeded` ✅ |
| Dakikalık sınır 30, eşzamanlı 60 istek | tam **30** kabul, 30 `429` + `Retry-After` ✅ |

## 4. Testlerde bulunan ve düzeltilen sorunlar

1. **Uzun metin kısa istekleri kilitliyordu.** 3 uzun haber üretilirken gelen kısa isteklerin hepsi 60 saniye bekleyip 202 alıyordu.
   - **Neden:** Motor uzun metnin tüm cümlelerini sıraya bir kerede koyuyordu.
   - **Düzeltme:**
     - Uzun metinler 3 cümlelik gruplar hâlinde sıraya giriyor; kısa istekler aralara giriyor.
     - İşçilerin yarısı her zaman kısa (anlık) isteklere ayrıldı.
     - Projeler arası adil sıra eklendi: bir projenin yüzlerce işi diğer projeleri bekletmiyor.
   - **Sonuç:** Bekleme 60 sn (zaman aşımı) → **5,3 sn**.
2. **Motor istekleri tek tek işliyordu.** EMA'nın istekleri toplu üretebilme özelliği açıldı. Ahenk Cep'in üslup ayarı modelin üzerinde tutulduğu için yalnız o sırayla çalışıyor. 2 çekirdekte verim %16 arttı; çekirdek sayısı arttıkça kazanç büyür.
3. **Kota yarışı olasılığı.** Kota/kuyruk kontrolü ile kayıt ekleme aynı kilit altına alındı; eşzamanlı istekler kotayı birlikte aşamaz.
4. **Olay döngüsü engellenmesi.** Eşzamanlı uçtaki veritabanı işleri iş parçacığına taşındı ve iş parçacığı havuzu 200'e çıkarıldı. Yük altında API yanıtları milisaniyeler düzeyinde kaldı.
5. **Webhook gönderimleri sıralıydı.** Yavaş ya da kapalı bir CRM'in diğer gönderimleri bekletmemesi için 8 paralel gönderici eklendi.
6. **Motor kapalıyken yanıltıcı hata.** API "ses bulunamadı" (400) diyordu; artık `503 engine_unavailable` + `Retry-After` dönüyor.
7. **Tek proje kuyruğu doldurabiliyordu.** Proje başına bekleyen iş sınırı eklendi (varsayılan 200, Ayarlar'dan değiştirilebilir).

## 5. Güvenlik: 67/67 kontrol geçti

| Alan | Kontroller |
|---|---|
| API anahtarı | Anahtarsız, biçimsiz ya da uydurma anahtar → 401. Anahtar veritabanında düz metin değil. Tam anahtar listelerde görünmüyor. |
| Yetki | Yalnız-okuma anahtarı ses üretemez ve silemez (403). |
| Projeler arası erişim (IDOR) | Bir projenin anahtarıyla başka projenin işi görülemez, silinemez, iptal edilemez, listelenemez. |
| Dosya bağlantıları | İmza değiştirme, imzasız erişim, başka işin imzası, yanlış uzantı ve yol aşımı (`../`) → 404. Süreli bağlantının süresi uzatılamaz. Silinen ses → 410. |
| Girdi doğrulama | Bilinmeyen alan, bozuk JSON, aralık dışı değer, 2 MB üstü gövde, karakter sınırı, büyük metadata, `file://` webhook → reddedildi. |
| CORS / IP | İzinsiz kaynak 403. IP izin listesi çalışıyor. `X-Forwarded-For` taklidi işe yaramıyor. |
| Durum | Durdurulmuş proje 403, bakım modu 503. |
| SSRF | `127.0.0.1`, `localhost`, `10.x`, `169.254.169.254` (bulut meta verisi) adreslerine webhook gönderilmiyor. |
| Giriş | E-posta ile giriş (büyük/küçük harf duyarsız). Geçersiz e-posta reddediliyor. Aynı e-posta iki kullanıcıya verilemiyor. |
| Panel | Oturumsuz 401. CSRF belirteci yoksa ya da yanlışsa 403. Zayıf şifre reddediliyor. İzleyici rolü hiçbir şeyi değiştiremiyor. Kaba kuvvet girişi 429 ile engelleniyor. |
| Enjeksiyon | Arama kutusunda SQL enjeksiyonu etkisiz. `<img onerror>` içeren proje adı panelde düz metin olarak basılıyor (tarayıcıda doğrulandı). |
| Başlıklar | CSP (satır içi betik yok), `X-Frame-Options: DENY`, `nosniff`, API'de `no-store`, her yanıtta `X-Request-ID`. Çerez HttpOnly ve SameSite=Strict. |
| Ses motoru | Token olmadan çağrılamıyor; dışarıya hiç açılmıyor. |
| Gizlilik | "Metinleri sakla" kapalıyken metin veritabanına yazılmıyor (veritabanında doğrulandı). |

## 6. Kapasite planlama (tahmini, KVM 8)

| Kullanım | Tahmini karşılama |
|---|---|
| Sosyal medya gönderisi (~80 karakter) | saniyede ~10, dakikada ~600 |
| Haber metni (~3.000 karakter) | ~5 sn'de bir haber |
| Aynı anda 50 kısa istek | çoğu 5 sn altında |

**Sınırlar:**
- İşlemci paylaşımlı olduğu için yoğun saatlerde hız düşebilir.
- Uzun metinleri `POST /v1/jobs` ile gönderin; `POST /v1/speech` en çok 60 sn bekler, sonra 202 döner.
- Hız sınırları ve giriş deneme sayaçları bellekte tutulur, servis yeniden başlayınca sıfırlanır. Kotalar veritabanından hesaplandığı için etkilenmez.
- `X-Real-IP`'nin doğruluğu Traefik'e (Dokploy) bağlıdır. Servisi Traefik olmadan doğrudan internete açmayın.
