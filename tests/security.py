"""Güvenlik testleri (çalışan app + engine'e karşı).

    python tests/security.py http://localhost:8000 admin <şifre> [veritabanı_yolu] [engine_url]

Kendi test projelerini/kullanıcılarını oluşturur ve sonunda siler.
"""
import json
import sqlite3
import sys
import time

import httpx

BASE, USER, PW = sys.argv[1], sys.argv[2], sys.argv[3]
DB = sys.argv[4] if len(sys.argv) > 4 else None
ENGINE = sys.argv[5] if len(sys.argv) > 5 else None
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  GEÇTİ  " if ok else "  KALDI  ") + name + (f"  ({detail})" if detail and not ok else ""))


def code(r):
    try:
        return r.json()["error"]["code"]
    except Exception:
        return None


def login(user=USER, pw=PW):
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/admin/api/auth/login", json={"email": user, "password": pw})
    if r.status_code != 200:
        return None, r
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return c, r


a, lr = login()
cookie = lr.headers.get("set-cookie", "")
check("Oturum çerezi HttpOnly", "httponly" in cookie.lower())
check("Oturum çerezi SameSite=Strict", "samesite=strict" in cookie.lower())
a.patch("/admin/api/settings", json={"webhook_allow_private": False})

pa = a.post("/admin/api/projects", json={"name": "sec-A", "rate_limit_rpm": 10000}).json()
pb = a.post("/admin/api/projects", json={"name": "sec-B", "rate_limit_rpm": 10000}).json()
ka = a.post(f"/admin/api/projects/{pa['id']}/keys", json={"name": "a"}).json()
kb = a.post(f"/admin/api/projects/{pb['id']}/keys", json={"name": "b"}).json()
kr = a.post(f"/admin/api/projects/{pa['id']}/keys", json={"name": "ro", "scopes": ["speech:read"]}).json()
A = httpx.Client(base_url=BASE, timeout=120, headers={"Authorization": f"Bearer {ka['key']}"})
B = httpx.Client(base_url=BASE, timeout=120, headers={"Authorization": f"Bearer {kb['key']}"})
RO = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {kr['key']}"})
anon = httpx.Client(base_url=BASE, timeout=60)

print("\n# API anahtarı")
check("Anahtarsız istek 401", code(anon.post("/v1/speech", json={"text": "x"})) == "missing_api_key")
check("Biçimsiz anahtar 401", code(anon.get("/v1/voices", headers={"Authorization": "Bearer ' OR 1=1 --"})) == "invalid_api_key")
check("Uydurma anahtar 401", code(anon.get("/v1/voices", headers={"Authorization": "Bearer tini_" + "A" * 40})) == "invalid_api_key")
check("X-API-Key başlığı da çalışır", anon.get("/v1/voices", headers={"X-API-Key": ka["key"]}).status_code == 200)
check("Anahtarın tamamı listede görünmez", ka["key"] not in a.get(f"/admin/api/projects/{pa['id']}/keys").text)
check("Webhook imza anahtarı proje yanıtında yok", "whsec_" not in a.get(f"/admin/api/projects/{pa['id']}").text)
if DB:
    raw = sqlite3.connect(DB).execute("SELECT group_concat(key_hash) FROM api_keys").fetchone()[0] or ""
    check("Veritabanında anahtar düz metin değil", ka["key"] not in raw and ka["key"][5:] not in raw)

print("\n# Yetki ve projeler arası erişim (IDOR)")
check("Yalnız-okuma anahtarı ses üretemez (403)", code(RO.post("/v1/jobs", json={"text": "x"})) == "insufficient_scope")
ga = A.post("/v1/speech", json={"text": "Proje A'nın sesi."}).json()
check("A ses üretti", ga.get("status") == "succeeded", json.dumps(ga)[:200])
check("Yalnız-okuma anahtarı silemez (403)", code(RO.delete(f"/v1/jobs/{ga['id']}")) == "insufficient_scope")
check("B, A'nın işini göremez (404)", B.get(f"/v1/jobs/{ga['id']}").status_code == 404)
check("B, A'nın işini silemez (404)", B.delete(f"/v1/jobs/{ga['id']}").status_code == 404)
check("B, A'nın işini iptal edemez (404)", B.post(f"/v1/jobs/{ga['id']}/cancel").status_code == 404)
check("B'nin listesinde A'nın işi yok", ga["id"] not in B.get("/v1/jobs?limit=100").text)
check("B, reference_id ile A'yı bulamaz", B.get("/v1/jobs", params={"reference_id": "x' OR '1'='1"}).json()["data"] == [])

print("\n# Dosya bağlantıları")
url = ga["audio"]["url"]
check("İmzalı bağlantı çalışır", httpx.get(url).status_code == 200)
check("İmza değiştirilince 404", httpx.get(url.replace("?s=", "?s=AAAA")).status_code == 404)
check("İmzasız 404", httpx.get(url.split("?")[0]).status_code == 404)
gb = B.post("/v1/speech", json={"text": "Proje B'nin sesi."}).json()
sig_a = url.split("s=")[1].split("&")[0]
check("Başka işin imzasıyla erişilemez", httpx.get(f"{BASE}/files/{gb['id']}.mp3?s={sig_a}").status_code == 404)
check("Yol aşımı (../) engellenir", httpx.get(f"{BASE}/files/..%2F..%2F..%2Fetc%2Fpasswd?s=x").status_code in (404, 400))
check("Yanlış uzantı 404", httpx.get(url.replace(".mp3?", ".wav?")).status_code == 404)
a.patch("/admin/api/settings", json={"file_url_ttl_hours": 1})
ttl_url = A.get(f"/v1/jobs/{ga['id']}").json()["audio"]["url"]
e = int(ttl_url.split("e=")[1])
check("Süreli bağlantının süresi uzatılamaz", httpx.get(ttl_url.replace(f"e={e}", f"e={e + 86400}")).status_code == 404)
check("Süreli bağlantı süresi içinde çalışır", httpx.get(ttl_url).status_code == 200)
a.patch("/admin/api/settings", json={"file_url_ttl_hours": 0})
A.delete(f"/v1/jobs/{ga['id']}")
check("Silinen ses 410", httpx.get(url).status_code == 410)

print("\n# İstek doğrulama ve sınırlar")
check("Bilinmeyen alan reddedilir (422)", code(A.post("/v1/speech", json={"text": "x", "hack": 1})) == "invalid_request")
check("Geçersiz JSON 422", A.post("/v1/speech", content=b"{bozuk", headers={"Content-Type": "application/json"}).status_code == 422)
check("Aralık dışı hız 422", code(A.post("/v1/speech", json={"text": "x", "speed": 50})) == "invalid_request")
check("2 MB üstü gövde 413", A.post("/v1/jobs", content=b"{" + b" " * (3 * 1024 * 1024) + b"}",
                                    headers={"Content-Type": "application/json"}).status_code == 413)
check("Proje karakter sınırı 413", code(A.post("/v1/jobs", json={"text": "a" * 6000})) == "text_too_long")
check("Büyük metadata reddedilir", code(A.post("/v1/jobs", json={"text": "x", "metadata": {"a": "b" * 5000}})) == "invalid_request")
check("Geçersiz webhook_url reddedilir", code(A.post("/v1/jobs", json={"text": "x", "webhook_url": "file:///etc/passwd"})) == "invalid_request")
check("Boş metin reddedilir", code(A.post("/v1/jobs", json={"text": "   "})) == "invalid_request")

print("\n# Tarayıcı kaynağı (CORS) ve IP")
check("İzinsiz Origin 403", code(A.get("/v1/voices", headers={"Origin": "https://kotu.example"})) == "origin_not_allowed")
a.patch(f"/admin/api/projects/{pa['id']}", json={"allowed_origins": "https://crm.example"})
time.sleep(16)  # anahtar önbelleği 15 sn
r = A.get("/v1/voices", headers={"Origin": "https://crm.example"})
check("İzinli Origin çalışır + CORS başlığı", r.status_code == 200 and r.headers.get("access-control-allow-origin") == "https://crm.example")
a.patch(f"/admin/api/projects/{pa['id']}", json={"ip_allowlist": "203.0.113.7"})
time.sleep(16)
check("İzin listesinde olmayan IP 403", code(A.get("/v1/voices")) == "ip_not_allowed")
check("X-Forwarded-For'un solu taklit edilemez",
      code(A.get("/v1/voices", headers={"X-Forwarded-For": "203.0.113.7, 127.0.0.1"})) == "ip_not_allowed")
a.patch(f"/admin/api/projects/{pa['id']}", json={"ip_allowlist": "", "status": "paused"})
time.sleep(16)
check("Durdurulmuş proje 403", code(A.get("/v1/voices")) == "project_disabled")
a.patch(f"/admin/api/projects/{pa['id']}", json={"status": "active"})
a.patch("/admin/api/settings", json={"maintenance_mode": True})
time.sleep(1)
check("Bakım modunda API 503", code(B.post("/v1/jobs", json={"text": "x"})) == "maintenance")
a.patch("/admin/api/settings", json={"maintenance_mode": False})

print("\n# Webhook (SSRF)")
for target in ("http://127.0.0.1:8000/healthz", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/x",
               "http://localhost/x"):
    a.patch(f"/admin/api/projects/{pb['id']}", json={"webhook_url": target})
    t = a.post(f"/admin/api/projects/{pb['id']}/webhook/test").json()
    check(f"İç ağ adresine webhook engellenir: {target}", t.get("status") == "failed" and "Özel ağ" in (t.get("last_error") or ""), str(t))

print("\n# Yönetim paneli")
check("Oturumsuz yönetim API'si 401", anon.get("/admin/api/projects").status_code == 401)
nocsrf = httpx.Client(base_url=BASE, cookies=a.cookies)
check("CSRF belirteci olmadan değişiklik 403", code(nocsrf.post("/admin/api/projects", json={"name": "x"})) == "csrf")
check("Yanlış CSRF belirteci 403", code(nocsrf.post("/admin/api/projects", json={"name": "x"}, headers={"X-CSRF-Token": "x"})) == "csrf")
check("Zayıf şifre reddedilir", code(a.post("/admin/api/users", json={"email": "zayif@test.local", "password": "123456"})) == "weak_password")
check("Geçersiz e-posta reddedilir", code(a.post("/admin/api/users", json={"email": "gecersiz", "password": "Izleyici12345"})) == "bad_email")
a.post("/admin/api/users", json={"email": "izleyici@test.local", "name": "İzleyici", "password": "Izleyici12345", "role": "viewer"})
check("Aynı e-posta ikinci kez eklenemez", code(a.post("/admin/api/users", json={"email": "IZLEYICI@test.local", "password": "Izleyici12345"})) == "exists")
v, _ = login("izleyici@test.local", "Izleyici12345")
check("E-posta büyük/küçük harf duyarsız giriş", login("Izleyici@TEST.local", "Izleyici12345")[0] is not None)
check("İzleyici proje oluşturamaz (403)", v.post("/admin/api/projects", json={"name": "x"}).status_code == 403)
check("İzleyici anahtar oluşturamaz (403)", v.post(f"/admin/api/projects/{pa['id']}/keys", json={"name": "x"}).status_code == 403)
check("İzleyici ayar değiştiremez (403)", v.patch("/admin/api/settings", json={"retention_days": 1}).status_code == 403)
check("İzleyici yedek indiremez (403)", v.get("/admin/api/backup").status_code == 403)
check("İzleyici görüntüleyebilir", v.get("/admin/api/projects").status_code == 200)
r = a.get("/admin/api/generations", params={"q": "' OR 1=1; DROP TABLE generations; --"})
check("Arama SQL enjeksiyonuna dayanıklı", r.status_code == 200 and a.get("/admin/api/generations").status_code == 200)
xss = a.patch(f"/admin/api/projects/{pb['id']}", json={"name": "<img src=x onerror=alert(1)>"}).json()
check("Proje adı ham saklanır (ekranda metin olarak basılır)", xss["name"].startswith("<img"))
h = anon.get("/")
csp = h.headers.get("content-security-policy", "")
check("Panelde CSP var (satır içi betik yok)", "script-src 'self'" in csp and "unsafe-inline" not in csp)
check("Panelde X-Frame-Options DENY", h.headers.get("x-frame-options") == "DENY")
check("nosniff başlığı", h.headers.get("x-content-type-options") == "nosniff")
check("API yanıtları önbelleğe alınmaz", B.get("/v1/voices").headers.get("cache-control") == "no-store")
check("Her yanıtta X-Request-ID", bool(B.get("/v1/voices").headers.get("x-request-id")))
bad = 0
for i in range(10):
    r = anon.post("/admin/api/auth/login", json={"email": USER, "password": f"yanlis{i}"})
    bad = r.status_code
check("Kaba kuvvet girişi engellenir (429)", bad == 429)

if ENGINE:
    print("\n# Ses motoru")
    check("Motor token olmadan çalışmaz", httpx.post(f"{ENGINE}/synthesize", json={"text": "x"}).status_code == 401)

print("\n# Saklama ve gizlilik")
a.patch(f"/admin/api/projects/{pb['id']}", json={"store_text": False})
time.sleep(16)
g = B.post("/v1/speech", json={"text": "Gizli müşteri metni 12345."}).json()
d = a.get(f"/admin/api/generations/{g['id']}").json()
check("Metin saklama kapalıyken metin yazılmaz", g.get("status") == "succeeded" and not d.get("text") and not d.get("spoken_text"))
if DB:
    raw = sqlite3.connect(DB).execute("SELECT COUNT(*) FROM generations WHERE text LIKE '%Gizli müşteri%' OR "
                                      "spoken_text LIKE '%Gizli%'").fetchone()[0]
    check("Veritabanında gizli metin yok", raw == 0)

# temizlik
for p in (pa, pb):
    a.delete(f"/admin/api/projects/{p['id']}")
for u in a.get("/admin/api/users").json()["users"]:
    if u["email"] == "izleyici@test.local":
        a.delete(f"/admin/api/users/{u['id']}")

ok = sum(1 for _, o, _ in results if o)
print(f"\nSONUÇ: {ok}/{len(results)} test geçti")
with open("/tmp/security_results.json", "w") as f:
    json.dump([{"test": n, "gecti": o, "ayrinti": d} for n, o, d in results], f, ensure_ascii=False, indent=1)
sys.exit(0 if ok == len(results) else 1)
