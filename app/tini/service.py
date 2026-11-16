"""İş mantığı: istek doğrulama, sınırlar/kotalar, üretim kaydı oluşturma, yanıt biçimi, dosya bağlantıları."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import threading
import time
from pathlib import Path
from typing import Optional

from . import config, db, security, settings
from .core import APIError, day_start, iso, month_start, next_day, next_month, now, voice_catalog

FORMAT_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg", "flac": "audio/flac"}
PRIORITY = {"sync": 10, "panel": 8, "async": 0}
ACTIVE = ("queued", "running")

_last_base_url = {"value": ""}


def remember_base_url(url: str):
    if url:
        _last_base_url["value"] = url.rstrip("/")


def base_url() -> str:
    return (settings.get("public_api_url") or config.PUBLIC_API_URL or _last_base_url["value"]).rstrip("/")


# ------------------------------------------------------------------ hız sınırı (bellek içi, kayan pencere)

class RateLimiter:
    def __init__(self):
        self.hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def check(self, key: str, limit: int, window: int = 60) -> tuple[bool, int, int]:
        """(izin, kalan, sıfırlanma saniyesi)"""
        t = time.time()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= t - window:
                q.popleft()
            if len(q) >= limit:
                return False, 0, max(1, int(q[0] + window - t) + 1)
            q.append(t)
            reset = int(q[0] + window - t) + 1
            return True, limit - len(q), reset


limiter = RateLimiter()


# ------------------------------------------------------------------ kullanım

def usage(project_id: str) -> dict:
    d0, m0 = day_start(), month_start()
    q = ("SELECT COALESCE(SUM(chars),0) c, COUNT(*) n, COALESCE(SUM(duration),0) s FROM generations "
         "WHERE project_id=? AND created_at>=? AND status NOT IN ('failed','canceled')")
    day = db.row(q, (project_id, d0))
    month = db.row(q, (project_id, m0))
    pending = db.val("SELECT COUNT(*) FROM generations WHERE project_id=? AND status IN ('queued','running')",
                     (project_id,), 0)
    return {"day": {"chars": day["c"], "requests": day["n"], "audio_seconds": round(day["s"], 1),
                    "resets_at": iso(next_day())},
            "month": {"chars": month["c"], "requests": month["n"], "audio_seconds": round(month["s"], 1),
                      "resets_at": iso(next_month())},
            "pending": pending}


# ------------------------------------------------------------------ ses ve parametre çözümü

def resolve_voice(project: dict, requested: Optional[str]) -> dict:
    catalog = {v["id"]: v for v in voice_catalog()}
    allowed = set(db.jload(project.get("allowed_voices"), []) or [])
    vid = requested or project.get("default_voice") or settings.get("default_voice")
    if vid == "default":
        vid = project.get("default_voice") or settings.get("default_voice")
    if not catalog:  # motorla hiç konuşulamadı (kapalı ya da açılıyor)
        raise APIError(503, "engine_unavailable", "Ses motoruna şu anda ulaşılamıyor. Birazdan tekrar deneyin.",
                       {"Retry-After": "15"})
    v = catalog.get(vid)
    if not v or not v["enabled"]:
        raise APIError(400, "voice_not_found", f"'{vid}' adlı ses yok ya da kapalı. GET /v1/voices ile listeleyin.")
    if allowed and vid not in allowed:
        raise APIError(403, "voice_not_allowed", f"Bu proje '{vid}' sesini kullanamaz.")
    if not v["loaded"]:
        raise APIError(503, "voice_unavailable", f"'{vid}' sesi şu anda yüklü değil.", {"Retry-After": "30"})
    return v


def project_voices(project: dict) -> list[dict]:
    allowed = set(db.jload(project.get("allowed_voices"), []) or [])
    default = project.get("default_voice") or settings.get("default_voice")
    out = []
    for v in voice_catalog():
        if not v["enabled"] or (allowed and v["id"] not in allowed):
            continue
        out.append({"id": v["id"], "name": v["name"], "model": v["model"], "language": v["language"],
                    "styles": v["styles"], "available": v["loaded"], "default": v["id"] == default,
                    "description": v["description"], "attribution": v["attribution"]})
    return out


# ------------------------------------------------------------------ üretim oluşturma

_create_lock = threading.Lock()


def create_generation(project: dict, body: dict, *, key: Optional[dict], source: str, mode: str, ip: str,
                      idempotency_key: Optional[str] = None, explicit: Optional[set] = None) -> tuple[dict, bool]:
    """Kontrolleri yapar ve kuyruğa ekler. (kayıt, yeni_mi) döner.

    Kota, kuyruk ve idempotency kontrolleri ile kayıt ekleme tek bir kilit altında yapılır; aynı anda
    gelen istekler kotayı birlikte aşamaz.
    """
    from . import presets

    body = presets.apply(project, body, set(body) if explicit is None else explicit)
    with _create_lock:
        return _create_generation(project, body, key=key, source=source, mode=mode, ip=ip,
                                  idempotency_key=idempotency_key)


def _create_generation(project: dict, body: dict, *, key: Optional[dict], source: str, mode: str, ip: str,
                       idempotency_key: Optional[str] = None) -> tuple[dict, bool]:
    s = settings.all()
    if s["maintenance_mode"] and source != "panel":
        raise APIError(503, "maintenance", s["maintenance_message"], {"Retry-After": "300"})
    if project["status"] != "active":
        raise APIError(403, "project_disabled", "Bu proje durdurulmuş.")

    text = body["text"]
    chars = len(text)
    limit = min(project["max_chars_per_request"], s["global_max_chars"])
    if chars > limit:
        raise APIError(413, "text_too_long", f"Metin {chars} karakter; bu proje için sınır {limit}.",
                       limit=limit, chars=chars)

    text_hash = hashlib.sha256(text.encode()).hexdigest()
    if idempotency_key:
        prev = db.row("SELECT * FROM generations WHERE project_id=? AND idempotency_key=?",
                      (project["id"], idempotency_key))
        if prev:
            if prev["text_hash"] != text_hash:
                raise APIError(409, "idempotency_conflict",
                               "Bu Idempotency-Key farklı bir metinle daha önce kullanılmış.")
            return prev, False

    voice = resolve_voice(project, body.get("voice"))
    style = body.get("style") or "neutral"
    if style not in voice["styles"]:
        style = "neutral"

    u = usage(project["id"])
    if project["daily_char_quota"] and u["day"]["chars"] + chars > project["daily_char_quota"]:
        raise APIError(429, "daily_quota_exceeded", "Günlük karakter kotası doldu.",
                       {"Retry-After": str(max(1, next_day() - now()))},
                       quota=project["daily_char_quota"], used=u["day"]["chars"], resets_at=u["day"]["resets_at"])
    if project["monthly_char_quota"] and u["month"]["chars"] + chars > project["monthly_char_quota"]:
        raise APIError(429, "monthly_quota_exceeded", "Aylık karakter kotası doldu.",
                       {"Retry-After": str(max(1, next_month() - now()))},
                       quota=project["monthly_char_quota"], used=u["month"]["chars"],
                       resets_at=u["month"]["resets_at"])
    total_pending = db.val("SELECT COUNT(*) FROM generations WHERE status IN ('queued','running')", (), 0)
    if total_pending >= s["max_queue"]:
        raise APIError(503, "queue_full", "Kuyruk dolu, biraz sonra tekrar deneyin.", {"Retry-After": "15"})
    if u["pending"] >= s["max_pending_per_project"]:
        raise APIError(429, "too_many_pending", f"Bu projenin bekleyen {u['pending']} işi var. Bir kısmı bitince "
                       "tekrar deneyin.", {"Retry-After": "10"}, limit=s["max_pending_per_project"])

    params = {k: body.get(k) for k in ("speed", "pitch", "pause_scale", "paragraph_pause", "volume",
                                       "normalize_loudness", "sample_rate", "seed", "include", "preset")}
    params["style"] = style
    gen = {
        "id": security.new_id("gen", 22),
        "project_id": project["id"],
        "key_id": key["id"] if key else None,
        "source": source,
        "mode": mode,
        "status": "queued",
        "priority": PRIORITY.get("panel" if source == "panel" else mode, 0),
        "text": text if project["store_text"] else None,
        "text_hash": text_hash,
        "chars": chars,
        "voice": voice["id"],
        "model": voice["model"],
        "params": db.jdump(params),
        "format": body.get("format") or "mp3",
        "reference_id": body.get("reference_id"),
        "metadata": db.jdump(body["metadata"]) if body.get("metadata") is not None else None,
        "webhook_url": body.get("webhook_url"),
        "idempotency_key": idempotency_key,
        "created_at": now(),
        "client_ip": ip,
    }
    # metni yalnız iş bitene kadar tutmak için (store_text kapalıysa) geçici alan
    if not project["store_text"]:
        _transient_text[gen["id"]] = text
    try:
        db.insert("generations", gen)
    except Exception as e:  # aynı anda gelen aynı idempotency key
        if idempotency_key and "UNIQUE" in str(e):
            prev = db.row("SELECT * FROM generations WHERE project_id=? AND idempotency_key=?",
                          (project["id"], idempotency_key))
            if prev:
                return prev, False
        raise
    from .runner import runner

    runner.wake()
    return gen, True


_transient_text: dict[str, str] = {}


# ------------------------------------------------------------------ bekleme (eşzamanlı istekler için)

_waiters: dict[str, list] = collections.defaultdict(list)
_waiters_lock = threading.Lock()


async def wait_for(gen_id: str, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    ev = asyncio.Event()
    with _waiters_lock:
        _waiters[gen_id].append((loop, ev))
    try:
        g = db.row("SELECT status FROM generations WHERE id=?", (gen_id,))
        if g and g["status"] not in ACTIVE:
            return True
        await asyncio.wait_for(ev.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        with _waiters_lock:
            lst = _waiters.get(gen_id, [])
            if (loop, ev) in lst:
                lst.remove((loop, ev))
            if not lst:
                _waiters.pop(gen_id, None)


def notify_done(gen_id: str):
    with _waiters_lock:
        for loop, ev in _waiters.pop(gen_id, []):
            loop.call_soon_threadsafe(ev.set)


# ------------------------------------------------------------------ yanıt biçimi

def retention_days(project: dict | None) -> Optional[int]:
    if not settings.get("auto_delete_enabled"):
        return None
    if project and project.get("retention_days") is not None:
        return project["retention_days"] or None  # 0 = bu projede silme
    return settings.get("retention_days")


def expires_at(g: dict, project: dict | None) -> Optional[int]:
    if g.get("keep") or g["status"] != "succeeded":
        return None
    days = retention_days(project)
    if not days:
        return None
    return (g.get("finished_at") or g["created_at"]) + days * 86400


def file_url(g: dict, download: bool = False) -> str:
    ttl = settings.get("file_url_ttl_hours")
    exp = 0
    if ttl:
        exp = (now() // 3600 + ttl) * 3600  # saat başına yuvarla: aynı saatte aynı bağlantı
    url = f"{base_url()}/files/{g['id']}.{g['format']}?s={security.file_sig(g['id'], exp)}"
    if exp:
        url += f"&e={exp}"
    if download:
        url += "&download=1"
    return url


def public(g: dict, project: dict | None = None, include: Optional[set] = None) -> dict:
    project = project or db.row("SELECT * FROM projects WHERE id=?", (g["project_id"],))
    params = db.jload(g.get("params"), {}) or {}
    include = include if include is not None else set(params.get("include") or [])
    out = {
        "id": g["id"],
        "object": "speech",
        "status": g["status"],
        "mode": g["mode"],
        "project_id": g["project_id"],
        "voice": g["voice"],
        "model": g.get("model"),
        "style": params.get("style", "neutral"),
        "preset": params.get("preset"),
        "format": g["format"],
        "text_chars": g["chars"],
        "reference_id": g.get("reference_id"),
        "metadata": db.jload(g.get("metadata")),
        "audio": None,
        "error": None,
        "created_at": iso(g["created_at"]),
        "started_at": iso(g.get("started_at")),
        "completed_at": iso(g.get("finished_at")),
        "processing_ms": g.get("processing_ms"),
        "expires_at": None,
    }
    if g["status"] == "succeeded" and g.get("audio_path") and not g.get("audio_deleted_at"):
        exp = expires_at(g, project)
        out["expires_at"] = iso(exp)
        out["audio"] = {
            "url": file_url(g),
            "download_url": file_url(g, download=True),
            "format": g["format"],
            "mime_type": FORMAT_MIME.get(g["format"], "application/octet-stream"),
            "duration": g.get("duration"),
            "size_bytes": g.get("audio_size"),
            "sample_rate": g.get("sample_rate"),
            "seed": g.get("seed"),
        }
    elif g["status"] == "succeeded" and g.get("audio_deleted_at"):
        out["status"] = "expired"
        out["error"] = {"code": "audio_deleted", "message": "Ses dosyası saklama süresi dolduğu için silindi."}
    if g["status"] in ("failed", "canceled"):
        out["error"] = {"code": g.get("error_code") or g["status"], "message": g.get("error_message") or ""}
    if g["status"] == "queued":
        out["queue_position"] = db.val(
            "SELECT COUNT(*) FROM generations WHERE status IN ('queued','running') AND "
            "(priority>? OR (priority=? AND created_at<?))", (g["priority"], g["priority"], g["created_at"]), 0)
    if "spoken_text" in include:
        out["spoken_text"] = g.get("spoken_text")
    if "words" in include:
        out["words"] = db.jload(g.get("words"), [])
    return out


def audio_path(g: dict) -> Optional[Path]:
    if not g.get("audio_path") or g.get("audio_deleted_at"):
        return None
    p = config.AUDIO_DIR / g["audio_path"]
    return p if p.is_file() else None


def delete_audio(g: dict):
    p = audio_path(g)
    if p:
        p.unlink(missing_ok=True)
    db.update("generations", g["id"], {"audio_deleted_at": now()})
