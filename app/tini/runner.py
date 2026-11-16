"""Arka plan işleri: üretim kuyruğu, webhook gönderimi, saklama süresi temizliği."""
from __future__ import annotations

import base64
import json
import logging
import socket
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx

from . import config, db, security, settings
from .core import iso, now
from .core import engine as engine_client
from .service import _transient_text, delete_audio, notify_done, public

log = logging.getLogger("tini.runner")
BACKOFF = [10, 60, 300, 1800, 7200, 21600, 43200]


class Runner:
    def __init__(self):
        self.cond = threading.Condition()
        self.started = False
        self.running: set[str] = set()
        from concurrent.futures import ThreadPoolExecutor

        self.hook_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="webhook")

    def wake(self):
        with self.cond:
            self.cond.notify_all()

    def start(self):
        if self.started:
            return
        self.started = True
        # yarıda kalan işler: yeniden kuyruğa
        db.execute("UPDATE generations SET status='queued', started_at=NULL WHERE status='running'")
        for i in range(16):
            threading.Thread(target=self._worker, args=(i,), name=f"worker-{i}", daemon=True).start()
        threading.Thread(target=self._webhooks, name="webhooks", daemon=True).start()
        threading.Thread(target=self._janitor, name="janitor", daemon=True).start()

    # ------------------------------------------------------------------ kuyruk
    def _claim(self):
        """Sıradaki işi atomik olarak al.

        - Öncelik: eşzamanlı (sync) ve panel istekleri, kuyruk (async) işlerinden önce.
        - İşçilerin yarısı her zaman kısa/eşzamanlı isteklere ayrılır: uzun async işler hepsini kaplayamaz.
        - Projeler arası adalet: aynı öncelikte, şu an daha az işi çalışan projenin işi önce alınır;
          bir projenin binlerce işi diğer projeleri bekletmez.
        - Proje başına aynı anda çalışan iş sınırı (max_concurrency) uygulanır.
        """
        t = now()
        wc = settings.get("worker_concurrency")
        with db.tx() as conn:
            low_running = conn.execute(
                "SELECT COUNT(*) FROM generations WHERE status='running' AND priority=0").fetchone()[0]
            low_ok = wc < 2 or low_running < max(1, wc // 2)  # işçilerin yarısı her zaman kısa isteklere
            r = conn.execute(
                "UPDATE generations SET status='running', started_at=?, attempts=attempts+1 WHERE id = ("
                " SELECT g.id FROM generations g JOIN projects p ON p.id=g.project_id"
                " WHERE g.status='queued' AND g.not_before<=?"
                f" {'' if low_ok else 'AND g.priority>0'}"
                " AND (SELECT COUNT(*) FROM generations r WHERE r.project_id=g.project_id AND r.status='running')"
                "   < p.max_concurrency"
                " ORDER BY g.priority DESC,"
                " (SELECT COUNT(*) FROM generations r WHERE r.project_id=g.project_id AND r.status='running') ASC,"
                " g.created_at ASC LIMIT 1) RETURNING *", (t, t)).fetchone()
            return dict(r) if r else None

    def _worker(self, index: int):
        while True:
            try:
                if index >= settings.get("worker_concurrency"):
                    time.sleep(5)
                    continue
                job = self._claim()
                if not job:
                    with self.cond:
                        self.cond.wait(timeout=2)
                    continue
                self.running.add(job["id"])
                try:
                    self._process(job)
                finally:
                    self.running.discard(job["id"])
            except Exception:
                log.exception("worker hatası")
                time.sleep(2)

    def _process(self, job: dict):
        params = db.jload(job["params"], {}) or {}
        text = job.get("text") or _transient_text.get(job["id"])
        if not text:
            return self._fail(job, "text_unavailable", "Metin bulunamadı (sunucu yeniden başlamış olabilir).")
        payload = {
            "text": text, "voice": job["voice"], "style": params.get("style") or "neutral",
            "speed": params.get("speed") or 1.0, "pitch": params.get("pitch") or 0.0,
            "pause_scale": 1.0 if params.get("pause_scale") is None else params["pause_scale"],
            "paragraph_pause": 0.7 if params.get("paragraph_pause") is None else params["paragraph_pause"],
            "volume": params.get("volume") or 0.0,
            "loudnorm": True if params.get("normalize_loudness") is None else params["normalize_loudness"],
            "sample_rate": params.get("sample_rate") or 48000, "seed": params.get("seed", 0),
            "format": job["format"], "include_words": True,
        }
        t0 = time.time()
        try:
            r = engine_client.synthesize(payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ReadError) as e:
            return self._retry_or_fail(job, "engine_unavailable", f"Ses motoruna ulaşılamadı ({type(e).__name__}).")
        except httpx.TimeoutException:
            return self._fail(job, "engine_timeout", "Ses motoru zamanında yanıt vermedi.")
        if r.status_code == 503:
            return self._retry_or_fail(job, "engine_loading", "Ses motoru hazırlanıyor.", max_attempts=120, delay=5)
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:
                pass
            code = {"nothing_to_read": "nothing_to_read"}.get(detail, "engine_error")
            if str(detail).startswith("voice_unavailable"):
                code = "voice_unavailable"
            msg = {"nothing_to_read": "Metinde okunacak bir şey bulunamadı.",
                   "voice_unavailable": "Seçilen ses motorda yüklü değil."}.get(code, f"Ses üretilemedi ({r.status_code}).")
            return self._fail(job, code, msg)

        out = r.json()
        data = base64.b64decode(out["audio_b64"])
        d = datetime.fromtimestamp(job["created_at"])
        rel = f"{d:%Y/%m}/{job['id']}.{job['format']}"
        path = config.AUDIO_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        finished = time.time()
        project = db.row("SELECT store_text FROM projects WHERE id=?", (job["project_id"],)) or {"store_text": 1}
        update = {
            "status": "succeeded", "audio_path": rel, "audio_size": len(data), "duration": out["duration"],
            "sample_rate": out["sample_rate"], "seed": out["seed"], "model": out.get("model") or job.get("model"),
            "spoken_text": out.get("spoken_text") if project["store_text"] else None,
            "words": db.jdump(out.get("words", [])) if project["store_text"] else None,
            "peaks": db.jdump(out.get("peaks", [])),
            "finished_at": int(finished), "processing_ms": int((finished - t0) * 1000),
            "error_code": None, "error_message": None,
        }
        cur = db.row("SELECT cancel_requested FROM generations WHERE id=?", (job["id"],))
        if cur is None:  # çalışırken silindi
            path.unlink(missing_ok=True)
            return self._finish_cleanup(job)
        if cur["cancel_requested"]:
            path.unlink(missing_ok=True)
            db.update("generations", job["id"], {"status": "canceled", "finished_at": now(),
                                                 "error_code": "canceled", "error_message": "İptal edildi."})
        else:
            db.update("generations", job["id"], update)
        self._finish_cleanup(job)
        self._after(job["id"])

    def _retry_or_fail(self, job, code, msg, max_attempts=4, delay=None):
        if job["attempts"] < max_attempts:
            wait = delay or min(60, 5 * job["attempts"])
            db.update("generations", job["id"], {"status": "queued", "not_before": now() + wait,
                                                 "error_code": code, "error_message": msg})
            log.warning("%s yeniden denenecek (%s, deneme %d)", job["id"], code, job["attempts"])
            return
        self._fail(job, code, msg)

    def _fail(self, job, code, msg):
        db.update("generations", job["id"], {"status": "failed", "error_code": code, "error_message": msg,
                                             "finished_at": now()})
        log.warning("%s başarısız: %s %s", job["id"], code, msg)
        self._finish_cleanup(job)
        self._after(job["id"])

    def _finish_cleanup(self, job):
        _transient_text.pop(job["id"], None)

    def _after(self, gen_id: str):
        notify_done(gen_id)
        try:
            g = db.row("SELECT * FROM generations WHERE id=?", (gen_id,))
            if g:
                enqueue_webhook(g)
        except Exception:
            log.exception("webhook kuyruğa eklenemedi")

    # ------------------------------------------------------------------ webhook
    def _webhooks(self):
        while True:
            try:
                due = db.rows("SELECT * FROM webhook_deliveries WHERE status='pending' AND next_attempt_at<=? "
                              "ORDER BY next_attempt_at LIMIT 40", (now(),))
                if due:  # yavaş ya da kapalı bir CRM diğer gönderimleri bekletmesin
                    list(self.hook_pool.map(deliver, due))
            except Exception:
                log.exception("webhook döngüsü")
            time.sleep(2)

    # ------------------------------------------------------------------ temizlik
    def _janitor(self):
        time.sleep(10)
        while True:
            try:
                cleanup()
            except Exception:
                log.exception("temizlik hatası")
            time.sleep(300)


runner = Runner()


# ---------------------------------------------------------------------------- webhook yardımcıları

def enqueue_webhook(g: dict, event: str | None = None, force_url: str | None = None) -> str | None:
    project = db.row("SELECT * FROM projects WHERE id=?", (g["project_id"],))
    if not project:
        return None
    url = force_url or g.get("webhook_url") or project["webhook_url"]
    if not url or not project["webhook_secret"]:
        return None
    if event is None:
        event = {"succeeded": "speech.succeeded", "failed": "speech.failed", "canceled": "speech.canceled"}.get(
            g["status"])
        if not event:
            return None
    did = security.new_id("whd", 20)
    payload = {"id": did, "event": event, "created_at": iso(now()), "data": public(g, project)}
    db.insert("webhook_deliveries", {
        "id": did, "generation_id": g["id"], "project_id": project["id"], "url": url, "event": event,
        "payload": db.jdump(payload), "status": "pending", "attempts": 0, "next_attempt_at": now(),
        "created_at": now(),
    })
    return did


def _blocked(url: str) -> str | None:
    p = urlparse(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        return "Geçersiz adres"
    if settings.get("webhook_allow_private"):
        return None
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        return "Alan adı çözümlenemedi"
    for info in infos:
        if security.is_private_host_ip(info[4][0]):
            return "Özel ağ adresine gönderim kapalı (Ayarlar > Webhook)"
    return None


def deliver(d: dict) -> dict:
    project = db.row("SELECT webhook_secret FROM projects WHERE id=?", (d["project_id"],))
    attempts = d["attempts"] + 1
    upd = {"attempts": attempts}
    blocked = _blocked(d["url"])
    if blocked or not project:
        upd.update(status="failed", last_error=blocked or "Proje silinmiş")
        db.update("webhook_deliveries", d["id"], upd)
        return upd
    body = d["payload"].encode()
    ts = now()
    headers = {
        "Content-Type": "application/json", "User-Agent": f"Tini-Webhook/{config.VERSION}",
        "X-Tini-Event": d["event"], "X-Tini-Delivery": d["id"], "X-Tini-Timestamp": str(ts),
        "X-Tini-Signature": security.webhook_signature(project["webhook_secret"], ts, body),
    }
    try:
        r = httpx.post(d["url"], content=body, headers=headers, timeout=settings.get("webhook_timeout_seconds"),
                       follow_redirects=False)
        upd["last_status_code"] = r.status_code
        ok = 200 <= r.status_code < 300
        upd["last_error"] = None if ok else (r.text or "")[:300]
    except Exception as e:
        ok = False
        upd["last_status_code"] = None
        upd["last_error"] = f"{type(e).__name__}: {e}"[:300]
    if ok:
        upd.update(status="delivered", delivered_at=now())
    elif attempts >= settings.get("webhook_max_attempts"):
        upd["status"] = "failed"
    else:
        upd["next_attempt_at"] = now() + BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
    db.update("webhook_deliveries", d["id"], upd)
    return upd


# ---------------------------------------------------------------------------- saklama temizliği

def cleanup() -> dict:
    s = settings.all()
    t = now()
    stats = {"audio_deleted": 0, "records_deleted": 0}
    if s["auto_delete_enabled"]:
        for p in db.rows("SELECT id, retention_days FROM projects"):
            days = s["retention_days"] if p["retention_days"] is None else p["retention_days"]
            if not days:
                continue
            cutoff = t - days * 86400
            olds = db.rows("SELECT * FROM generations WHERE project_id=? AND status='succeeded' AND keep=0 AND "
                           "audio_deleted_at IS NULL AND COALESCE(finished_at, created_at)<? LIMIT 2000",
                           (p["id"], cutoff))
            for g in olds:
                delete_audio(g)
            stats["audio_deleted"] += len(olds)
    if s["log_retention_days"]:
        cutoff = t - s["log_retention_days"] * 86400
        olds = db.rows("SELECT * FROM generations WHERE keep=0 AND created_at<? AND status NOT IN "
                       "('queued','running') LIMIT 5000", (cutoff,))
        for g in olds:
            delete_audio(g)
            db.execute("DELETE FROM generations WHERE id=?", (g["id"],))
        stats["records_deleted"] = len(olds)
        db.execute("DELETE FROM webhook_deliveries WHERE created_at<?", (cutoff,))
        db.execute("DELETE FROM audit_log WHERE ts<?", (t - max(365, s["log_retention_days"]) * 86400,))
    db.execute("DELETE FROM sessions WHERE expires_at<?", (t,))
    # 45 dakikadır "running" kalan ve bu süreçte gerçekten çalışmayan iş: yeniden kuyruğa
    for g in db.rows("SELECT id FROM generations WHERE status='running' AND started_at<?", (t - 2700,)):
        if g["id"] not in runner.running:
            db.execute("UPDATE generations SET status='queued', started_at=NULL WHERE id=?", (g["id"],))
    if stats["audio_deleted"] or stats["records_deleted"]:
        log.info("temizlik: %s", json.dumps(stats))
    return stats
