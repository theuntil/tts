"""Tını yük ve stres testi.

    python tests/load.py http://localhost:8000 admin <şifre> [senaryo ...]

Senaryolar: baseline sync burst jobs mixed races ratelimit responsiveness (varsayılan: hepsi)
Test için kendi projelerini ve anahtarlarını oluşturur; sonunda siler.
"""
import asyncio
import json
import statistics
import sys
import time

import httpx

BASE, USER, PW = sys.argv[1], sys.argv[2], sys.argv[3]
ONLY = set(sys.argv[4:])

SHORT = [
    "Yeni kampanyamız bugün başladı! Tüm ürünlerde yüzde yirmi indirim sizi bekliyor.",
    "Siparişiniz 14 Ekim'de saat 15:30'da kargoya verildi, yarın elinizde olacak.",
    "Bu akşam saat dokuzda canlı yayındayız, sorularınızı şimdiden yorumlara yazın.",
    "Hafta sonu mağazamız 10:00 ile 22:00 arasında açık olacak. Hepinizi bekliyoruz!",
    "Yeni koleksiyonumuz raflarda. Sınırlı sayıda üretildi, kaçırmayın.",
]
LONG = ("Kentte bu sabah saatlerinden itibaren etkili olan sağanak yağış, bazı ilçelerde ulaşımı aksattı. "
        "Meteoroloji yetkilileri, yağışın akşam saatlerine kadar aralıklarla süreceğini açıkladı. ") * 30

results = {}


def pct(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


def summary(name, lat, codes, wall, extra=None):
    s = {"istek": sum(codes.values()), "kodlar": dict(sorted(codes.items())), "sure_sn": round(wall, 2),
         "p50_ms": round(pct(lat, 50) * 1000), "p95_ms": round(pct(lat, 95) * 1000),
         "p99_ms": round(pct(lat, 99) * 1000), "max_ms": round(max(lat or [0]) * 1000),
         "istek_sn": round(sum(codes.values()) / wall, 2) if wall else 0}
    if extra:
        s.update(extra)
    results[name] = s
    print(f"\n[{name}] " + json.dumps(s, ensure_ascii=False))
    return s


async def admin():
    a = httpx.AsyncClient(base_url=BASE, timeout=120)
    me = (await a.post("/admin/api/auth/login", json={"username": USER, "password": PW})).json()
    a.headers["X-CSRF-Token"] = me["csrf"]
    return a


async def make_project(a, name, **limits):
    body = {"name": name, "rate_limit_rpm": 100000, "max_concurrency": 8, "max_chars_per_request": 200000}
    body.update(limits)
    p = (await a.post("/admin/api/projects", json=body)).json()
    k = (await a.post(f"/admin/api/projects/{p['id']}/keys", json={"name": "load"})).json()
    return p, httpx.AsyncClient(base_url=BASE, timeout=300, headers={"Authorization": f"Bearer {k['key']}"},
                                limits=httpx.Limits(max_connections=500, max_keepalive_connections=100))


async def wait_jobs(c, ids, timeout=900):
    t0 = time.perf_counter()
    pending = set(ids)
    done = {}
    while pending and time.perf_counter() - t0 < timeout:
        for i in list(pending)[:50]:
            j = (await c.get(f"/v1/jobs/{i}")).json()
            if j["status"] not in ("queued", "running"):
                pending.discard(i)
                done[i] = j
        await asyncio.sleep(0.5)
    return done, pending


async def main():
    a = await admin()
    projects = []
    sys_info = (await a.get("/admin/api/system")).json()
    print("motor:", sys_info["engine"].get("status"), "işçi:", sys_info["workers"])

    # ------------------------------------------------------------------ 1) tek istek hızı
    p, c = await make_project(a, "load-base")
    projects.append(p)
    if not ONLY or "baseline" in ONLY:
        lat, codes, audio = [], {}, 0
        for t in SHORT * 2:
            t0 = time.perf_counter()
            r = await c.post("/v1/speech", json={"text": t})
            lat.append(time.perf_counter() - t0)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
            if r.status_code == 200:
                audio += r.json()["audio"]["duration"]
        r = await c.post("/v1/speech", json={"text": LONG[:3000]})
        lj = r.json()
        summary("1_tek_tek_kisa", lat, codes, sum(lat), {
            "ses_sn_toplam": round(audio, 1), "gercek_zaman_kati": round(audio / sum(lat), 1),
            "3000_karakter_ms": lj.get("processing_ms"), "3000_karakter_ses_sn": (lj.get("audio") or {}).get("duration")})

    # ------------------------------------------------------------------ 2) eşzamanlı sync patlaması
    if not ONLY or "sync" in ONLY:
        for conc in (10, 50):
            sem = asyncio.Semaphore(conc)
            lat, codes, audio = [], {}, [0.0]

            async def one(i):
                async with sem:
                    t0 = time.perf_counter()
                    r = await c.post("/v1/speech", json={"text": SHORT[i % len(SHORT)], "reference_id": f"s{i}"})
                    lat.append(time.perf_counter() - t0)
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                    if r.status_code == 200:
                        audio[0] += r.json()["audio"]["duration"]

            t0 = time.perf_counter()
            await asyncio.gather(*(one(i) for i in range(conc * 2)))
            wall = time.perf_counter() - t0
            summary(f"2_eszamanli_sync_{conc}", lat, codes, wall,
                    {"ses_sn": round(audio[0], 1), "ses_sn_per_sn": round(audio[0] / wall, 2)})

    # ------------------------------------------------------------------ 3) iş kuyruğu patlaması + API tepkisi
    if not ONLY or "burst" in ONLY or "responsiveness" in ONLY:
        n = 200
        lat, codes, ids = [], {}, []

        async def sub(i):
            t0 = time.perf_counter()
            r = await c.post("/v1/jobs", json={"text": SHORT[i % len(SHORT)], "reference_id": f"j{i}"})
            lat.append(time.perf_counter() - t0)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
            if r.status_code in (200, 202):
                ids.append(r.json()["id"])

        t0 = time.perf_counter()
        await asyncio.gather(*(sub(i) for i in range(n)))
        sub_wall = time.perf_counter() - t0
        summary("3a_200_is_gonderimi", lat, codes, sub_wall)
        # kuyruk doluyken API ne kadar hızlı yanıt veriyor?
        rl = []
        for _ in range(30):
            t1 = time.perf_counter()
            await c.get("/v1/usage")
            rl.append(time.perf_counter() - t1)
            await asyncio.sleep(0.1)
        hl = []
        for _ in range(20):
            t1 = time.perf_counter()
            await a.get("/admin/api/overview")
            hl.append(time.perf_counter() - t1)
        results["3b_yuk_altinda_api"] = {"usage_p50_ms": round(pct(rl, 50) * 1000), "usage_p95_ms": round(pct(rl, 95) * 1000),
                                         "panel_overview_p50_ms": round(pct(hl, 50) * 1000),
                                         "panel_overview_p95_ms": round(pct(hl, 95) * 1000)}
        print("\n[3b_yuk_altinda_api]", results["3b_yuk_altinda_api"])
        done, pending = await wait_jobs(c, ids)
        total = time.perf_counter() - t0
        st = {}
        for j in done.values():
            st[j["status"]] = st.get(j["status"], 0) + 1
        audio = sum((j.get("audio") or {}).get("duration", 0) for j in done.values())
        results["3c_200_is_tamamlanma"] = {"sure_sn": round(total, 1), "durumlar": st, "bitmeyen": len(pending),
                                          "is_sn": round(len(done) / total, 2), "ses_sn_per_sn": round(audio / total, 2)}
        print("\n[3c_200_is_tamamlanma]", results["3c_200_is_tamamlanma"])

    # ------------------------------------------------------------------ 4) uzun iş varken kısa istekler beklemeli mi?
    if not ONLY or "mixed" in ONLY:
        long_ids = []
        for _ in range(3):
            r = await c.post("/v1/jobs", json={"text": LONG})
            long_ids.append(r.json()["id"])
        await asyncio.sleep(1.0)
        lat, codes = [], {}
        p2, c2 = await make_project(a, "load-mixed")
        projects.append(p2)

        async def short(i):
            await asyncio.sleep(i * 0.3)
            t0 = time.perf_counter()
            r = await c2.post("/v1/speech", json={"text": SHORT[i % len(SHORT)]})
            lat.append(time.perf_counter() - t0)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1

        t0 = time.perf_counter()
        await asyncio.gather(*(short(i) for i in range(10)))
        summary("4_uzun_isler_varken_kisa_sync", lat, codes, time.perf_counter() - t0,
                {"uzun_metin_karakter": len(LONG)})
        done, _ = await wait_jobs(c, long_ids)
        results["4_uzun_isler_varken_kisa_sync"]["uzun_is_ms"] = [j.get("processing_ms") for j in done.values()]
        await c2.aclose()

    # ------------------------------------------------------------------ 5) yarış durumları
    if not ONLY or "races" in ONLY:
        # aynı Idempotency-Key ile 40 eşzamanlı istek -> tek iş
        rs = await asyncio.gather(*(c.post("/v1/jobs", json={"text": "Tekrar koruması testi."},
                                           headers={"Idempotency-Key": "race-1"}) for _ in range(40)))
        ids = {r.json().get("id") for r in rs if r.status_code in (200, 202)}
        codes = {}
        for r in rs:
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
        results["5a_idempotency_yarisi"] = {"istek": 40, "farkli_is": len(ids), "kodlar": codes, "gecti": len(ids) == 1}
        print("\n[5a_idempotency_yarisi]", results["5a_idempotency_yarisi"])
        # günlük kota 1000 karakter, 40 eşzamanlı x 100 karakter -> en fazla 10 kabul
        pq, cq = await make_project(a, "load-quota", daily_char_quota=1000)
        projects.append(pq)
        rs = await asyncio.gather(*(cq.post("/v1/jobs", json={"text": "a" * 99 + "b"}) for _ in range(40)))
        ok = sum(1 for r in rs if r.status_code == 202)
        results["5b_kota_yarisi"] = {"istek": 40, "kabul": ok, "beklenen_en_fazla": 10, "gecti": ok <= 10,
                                     "red_kodu": sorted({r.json()["error"]["code"] for r in rs if r.status_code != 202})}
        print("\n[5b_kota_yarisi]", results["5b_kota_yarisi"])
        await cq.aclose()

    # ------------------------------------------------------------------ 6) hız sınırı doğruluğu
    if not ONLY or "ratelimit" in ONLY:
        pr, cr = await make_project(a, "load-rpm", rate_limit_rpm=30)
        projects.append(pr)
        rs = await asyncio.gather(*(cr.post("/v1/jobs", json={"text": "Hız sınırı."}) for _ in range(60)))
        ok = sum(1 for r in rs if r.status_code == 202)
        lim = [r for r in rs if r.status_code == 429]
        results["6_hiz_siniri"] = {"istek": 60, "kabul": ok, "429": len(lim), "gecti": ok == 30,
                                   "retry_after": lim[0].headers.get("retry-after") if lim else None,
                                   "ratelimit_basliklari": bool(rs[0].headers.get("x-ratelimit-limit"))}
        print("\n[6_hiz_siniri]", results["6_hiz_siniri"])
        await cr.aclose()

    # temizlik: test projelerinin işleri bitince projeleri sil
    for _ in range(600):
        q = (await a.get("/admin/api/overview")).json()["queue"]
        if not q["queued"] and not q["running"]:
            break
        await asyncio.sleep(1)
    for p in projects:
        await a.delete(f"/admin/api/projects/{p['id']}")
    with open("/tmp/load_results.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("\nBİTTİ")


asyncio.run(main())
