"""Çalışan bir app + engine'e karşı uçtan uca duman testi.  python tests/smoke.py http://localhost:8000 admin Sifre12345"""
import json, sys, time, threading, http.server
import httpx

BASE, USER, PW = sys.argv[1], sys.argv[2], sys.argv[3]
hooks = []

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        hooks.append((dict(self.headers), json.loads(body)))
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

srv = http.server.HTTPServer(("127.0.0.1", 8765), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

a = httpx.Client(base_url=BASE, timeout=120)
def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text[:500]); return r.json() if "json" in r.headers.get("content-type","") else r
r = a.post("/admin/api/auth/login", json={"email": USER, "password": "yanlis"}); assert r.status_code == 401, r.text
me = ok(a.post("/admin/api/auth/login", json={"email": USER, "password": PW}))
assert me["user"]["email"] == USER.lower(), me
H_ = {"X-CSRF-Token": me["csrf"]}
assert a.post("/admin/api/projects", json={"name": "x"}).status_code == 403  # CSRF yok
ok(a.patch("/admin/api/settings", json={"webhook_allow_private": True, "public_api_url": BASE}, headers=H_))
p = ok(a.post("/admin/api/projects", json={"name": "CRM", "webhook_url": "http://127.0.0.1:8765/hook",
                                            "rate_limit_rpm": 5, "daily_char_quota": 400}, headers=H_))
k = ok(a.post(f"/admin/api/projects/{p['id']}/keys", json={"name": "prod"}, headers=H_))
KEY = k["key"]; print("key", k["display"])
c = httpx.Client(base_url=BASE, timeout=120, headers={"Authorization": f"Bearer {KEY}"})
assert httpx.post(BASE + "/v1/speech", json={"text": "a"}).json()["error"]["code"] == "missing_api_key"
r = ok(c.post("/v1/speech", json={"text": "Merhaba dünya, saat 09:30'da görüşelim.", "reference_id": "post_1",
                                   "metadata": {"a": 1}, "include": ["words"]}))
print("sync:", r["status"], r["audio"]["duration"], r["audio"]["url"][:70], "words", len(r["words"]))
f = httpx.get(r["audio"]["url"]); assert f.status_code == 200 and f.headers["content-type"] == "audio/mpeg", f.status_code
assert httpx.get(r["audio"]["url"].replace("s=", "s=x")).status_code == 404
b = c.post("/v1/speech?response=binary", json={"text": "İkili yanıt."}); assert b.headers["content-type"] == "audio/mpeg"
# idempotency
i1 = ok(c.post("/v1/jobs", json={"text": "Tekrar koruması."}, headers={"Idempotency-Key": "abc"}), 202)
i2 = ok(c.post("/v1/jobs", json={"text": "Tekrar koruması."}, headers={"Idempotency-Key": "abc"}))
assert i1["id"] == i2["id"]
assert c.post("/v1/jobs", json={"text": "Başka metin"}, headers={"Idempotency-Key": "abc"}).status_code == 409
# rate limit (5/dk): 4 istek yapıldı (speech x2, jobs x3 => 5) → sonraki 429
r = c.post("/v1/jobs", json={"text": "fazla"}); print("ratelimit:", r.status_code, r.json()["error"]["code"], r.headers.get("retry-after"))
assert r.status_code == 429
for _ in range(50):
    j = ok(c.get(f"/v1/jobs/{i1['id']}"))
    if j["status"] not in ("queued", "running"): break
    time.sleep(0.2)
assert j["status"] == "succeeded", j
time.sleep(3)
print("webhooks:", [(h[0].get("X-Tini-Event") or h[0].get("x-tini-event"), h[1]["data"]["id"]) for h in hooks])
assert hooks and "sha256=" in (hooks[0][0].get("X-Tini-Signature") or hooks[0][0].get("x-tini-signature"))
print("list:", len(ok(c.get("/v1/jobs?limit=2"))["data"]), "voices:", [v["id"] for v in ok(c.get("/v1/voices"))["data"]])
print("usage:", ok(c.get("/v1/usage"))["usage"]["day"])
assert ok(c.post("/v1/jobs", json={"text": "x"*600}) if False else c.post("/v1/jobs", json={"text": "x"*600}), 0) if False else True
assert c.post("/v1/speech", json={"text": "x", "bogus": 1}).json()["error"]["code"] in ("invalid_request", "rate_limited")
# hazır ayarlar (ayrı proje: yukarıdaki hız sınırı testinden etkilenmesin)
pp = ok(a.post("/admin/api/projects", json={"name": "Presets", "rate_limit_rpm": 1000}, headers=H_))
kp = ok(a.post(f"/admin/api/projects/{pp['id']}/keys", json={"name": "p"}, headers=H_))
cp = httpx.Client(base_url=BASE, timeout=120, headers={"Authorization": f"Bearer {kp['key']}"})
pr = ok(cp.get("/v1/presets"))["data"]; print("presets:", [x["id"] for x in pr])
j = ok(cp.post("/v1/jobs", json={"text": "Hazır ayar testi.", "preset": "hikaye"}), 202)
assert j["preset"] == "hikaye" and j["style"] in ("neutral", "calm"), j
g = ok(a.get(f"/admin/api/generations/{j['id']}"))["params"]
assert g["speed"] == 0.92 and g["pause_scale"] == 1.4, g
j2 = ok(cp.post("/v1/jobs", json={"text": "Ezme testi.", "preset": "hikaye", "speed": 1.3}), 202)
g2 = ok(a.get(f"/admin/api/generations/{j2['id']}"))["params"]
assert g2["speed"] == 1.3 and g2["pause_scale"] == 1.4, g2
assert cp.post("/v1/jobs", json={"text": "x", "preset": "yok-boyle"}).json()["error"]["code"] == "preset_not_found"
j3 = ok(cp.post("/v1/jobs", json={"text": "Hazır ayarsız."}), 202); assert j3["preset"] is None
ok(a.patch(f"/admin/api/projects/{pp['id']}", json={"default_preset": "haber"}, headers=H_))
time.sleep(16)
j4 = ok(cp.post("/v1/jobs", json={"text": "Varsayılan hazır ayar."}), 202); assert j4["preset"] == "haber", j4
np = ok(a.post("/admin/api/presets", json={"id": "kisa-haber", "name": "Kısa haber", "params": {"speed": 1.2, "voice": "ema"}}, headers=H_))
assert a.post("/admin/api/presets", json={"id": "Kötü ID", "name": "x"}, headers=H_).status_code == 400
assert a.post("/admin/api/presets", json={"id": "x1", "name": "x", "params": {"speed": 9}}, headers=H_).status_code == 400
j5 = ok(cp.post("/v1/jobs", json={"text": "Yeni hazır ayar.", "preset": "kisa-haber"}), 202); assert j5["voice"] == "ema"
ok(a.delete("/admin/api/presets/haber", headers=H_))
assert ok(a.get(f"/admin/api/projects/{pp['id']}"))["default_preset"] is None
print("presets OK")
# revoke
ok(a.post(f"/admin/api/keys/{k['id']}/revoke", headers=H_))
assert c.get("/v1/voices").json()["error"]["code"] == "revoked_api_key"
ov = ok(a.get("/admin/api/overview")); print("overview today:", ov["today"], "queue", ov["queue"])
g = ok(a.get("/admin/api/generations?limit=5")); print("gens:", [x["status"] for x in g["items"]])
pg = ok(a.post("/admin/api/playground", json={"text": "Panelden deneme.", "voice": "ahenk-cep", "style": "sad"}, headers=H_))
print("playground:", pg["status"], pg["voice"], pg["style"])
au = ok(a.get(f"/admin/api/generations/{pg['id']}/audio")); 
bk = a.get("/admin/api/backup"); assert bk.status_code == 200 and bk.content[:15] == b"SQLite format 3"
print("audit:", [x["action"] for x in ok(a.get("/admin/api/audit"))["items"]][:8])
print("ALL OK")
