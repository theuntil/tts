"""Herkese açık API (v1). Modeller değişse de bu uçlar ve yanıt biçimi sabit kalır."""
from __future__ import annotations

import threading
import time
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db, security, settings
from .core import APIError, engine, iso, now
from .service import (ACTIVE, base_url, create_generation, delete_audio, limiter, project_voices, public,
                      remember_base_url, usage, wait_for, audio_path, FORMAT_MIME)

router = APIRouter(prefix="/v1", tags=["v1"])

SCOPES = ("speech:create", "speech:read", "speech:delete")
Style = Literal["neutral", "calm", "cheerful", "empathetic", "sad", "angry", "surprised"]


# ---------------------------------------------------------------------------- istek gövdesi

class SpeechIn(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {
        "text": "Yeni kampanyamız bugün başladı! Ayrıntılar profilimizde.", "preset": "sosyal-medya", "format": "mp3",
        "reference_id": "post_4821", "metadata": {"account": "magaza-tr"}}})

    text: str = Field(..., min_length=1, description="Seslendirilecek Türkçe metin. Paragraflar boş satırla ayrılır.")
    preset: Optional[str] = Field(None, max_length=40, description="Hazır ayar (ör. haber, hikaye). GET /v1/presets ile "
                                  "listeleyin. İstekte açıkça yazdığınız alanlar hazır ayarı ezer.")
    voice: Optional[str] = Field(None, max_length=64, description="Ses kimliği (GET /v1/voices). Boşsa projenin varsayılanı.")
    style: Optional[Style] = Field(None, description="Üslup (yalnız destekleyen seslerde)")
    speed: float = Field(1.0, ge=0.5, le=2.0, description="Konuşma hızı")
    pitch: float = Field(0.0, ge=-4, le=4, description="Ses perdesi (yarım ton)")
    pause_scale: float = Field(1.0, ge=0, le=3, description="Cümle arası duraklama çarpanı")
    paragraph_pause: float = Field(0.7, ge=0, le=3, description="Paragraf arası sessizlik (sn)")
    volume: float = Field(0.0, ge=-20, le=10, description="Ek ses seviyesi (dB)")
    normalize_loudness: bool = Field(True, description="Ses seviyesini yayın seviyesine dengele")
    format: Literal["mp3", "wav", "ogg", "flac"] = Field("mp3", description="Dosya biçimi")
    sample_rate: Literal[48000, 24000, 16000, 8000] = Field(48000, description="Örnekleme hızı (Hz)")
    seed: Optional[int] = Field(0, ge=0, le=2**31 - 1, description="Aynı tohum aynı sesi verir; null = rastgele")
    reference_id: Optional[str] = Field(None, max_length=128, description="Sizin sisteminizdeki kayıt kimliği")
    metadata: Optional[dict] = Field(None, description="Yanıtta ve webhook'ta aynen geri döner (en fazla 4 KB)")
    webhook_url: Optional[str] = Field(None, max_length=2000, description="Bu istek için webhook adresi")
    include: list[Literal["words", "spoken_text"]] = Field(default_factory=list,
                                                           description="Yanıta eklenecek ek alanlar")

    @field_validator("text")
    @classmethod
    def _text(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Metin boş")
        return v

    @field_validator("metadata")
    @classmethod
    def _meta(cls, v):
        if v is not None and len(db.jdump(v)) > 4096:
            raise ValueError("metadata en fazla 4 KB olabilir")
        return v

    @field_validator("webhook_url")
    @classmethod
    def _hook(cls, v):
        if v and not v.startswith(("https://", "http://")):
            raise ValueError("webhook_url http(s):// ile başlamalı")
        return v or None


class NormalizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=20000)


# ---------------------------------------------------------------------------- kimlik doğrulama

_key_cache: dict[str, tuple[float, dict, dict]] = {}
_key_cache_lock = threading.Lock()
_last_touch: dict[str, float] = {}


def clear_key_cache():
    with _key_cache_lock:
        _key_cache.clear()


def client_ip(request: Request) -> str:
    """Gerçek istemci IP'si. Traefik X-Real-IP'yi kendisi yazar (istemci taklit edemez); yoksa
    X-Forwarded-For'un en sağdaki (vekilin eklediği) değeri kullanılır, en soldaki taklit edilebilir."""
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    fwd = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
    if fwd:
        return fwd[-1]
    return request.client.host if request.client else ""


class Caller:
    def __init__(self, key: dict, project: dict, ip: str):
        self.key, self.project, self.ip = key, project, ip


def _lookup(raw: str):
    h = security.key_hash(raw)
    t = time.time()
    with _key_cache_lock:
        hit = _key_cache.get(h)
        if hit and t - hit[0] < 15:
            return hit[1], hit[2]
    key = db.row("SELECT * FROM api_keys WHERE key_hash=?", (h,))
    project = db.row("SELECT * FROM projects WHERE id=?", (key["project_id"],)) if key else None
    with _key_cache_lock:
        _key_cache[h] = (t, key, project)
    return key, project


def caller(scope: str):
    def dep(request: Request, authorization: Annotated[Optional[str], Header()] = None,
            x_api_key: Annotated[Optional[str], Header()] = None) -> Caller:
        raw = ""
        if authorization and authorization.lower().startswith("bearer "):
            raw = authorization[7:].strip()
        raw = raw or (x_api_key or "").strip()
        if not raw:
            raise APIError(401, "missing_api_key", "API anahtarı gerekli: Authorization: Bearer <anahtar>")
        if not security.looks_like_key(raw):
            raise APIError(401, "invalid_api_key", "API anahtarı geçersiz.")
        key, project = _lookup(raw)
        if not key or not project:
            raise APIError(401, "invalid_api_key", "API anahtarı geçersiz.")
        if key["revoked_at"]:
            raise APIError(401, "revoked_api_key", "Bu API anahtarı iptal edilmiş.")
        if key["expires_at"] and key["expires_at"] < time.time():
            raise APIError(401, "expired_api_key", "Bu API anahtarının süresi dolmuş.")
        if scope not in (key["scopes"] or "").split():
            raise APIError(403, "insufficient_scope", f"Bu anahtarın '{scope}' yetkisi yok.")
        ip = client_ip(request)
        if not security.ip_allowed(ip, project["ip_allowlist"]):
            raise APIError(403, "ip_not_allowed", f"Bu IP adresinden erişim izni yok ({ip}).")
        origin = request.headers.get("origin")
        if origin:
            allowed = [o.strip().rstrip("/") for o in project["allowed_origins"].replace("\n", ",").split(",")
                       if o.strip()]
            if origin.rstrip("/") not in allowed and "*" not in allowed:
                raise APIError(403, "origin_not_allowed",
                               "Tarayıcıdan bu kaynaktan erişim izni yok. API'yi sunucu tarafından çağırın ya da "
                               "projeye izinli kaynak ekleyin.")
            request.state.cors_origin = origin
        if project["status"] != "active":
            raise APIError(403, "project_disabled", "Bu proje durdurulmuş.")
        if scope == "speech:create":
            ok, remaining, reset = limiter.check(project["id"], project["rate_limit_rpm"])
            request.state.ratelimit = (project["rate_limit_rpm"], remaining, reset)
            if not ok:
                raise APIError(429, "rate_limited", "Dakikalık istek sınırı aşıldı.",
                               {"Retry-After": str(reset)}, limit=project["rate_limit_rpm"])
        t = time.time()
        if True:
            if t - _last_touch.get(key["id"], 0) > 30:
                _last_touch[key["id"]] = t
                db.execute("UPDATE api_keys SET last_used_at=?, last_used_ip=?, use_count=use_count+1 WHERE id=?",
                           (int(t), ip, key["id"]))
            else:
                db.execute("UPDATE api_keys SET use_count=use_count+1 WHERE id=?", (key["id"],))
        remember_base_url(str(request.base_url))
        return Caller(key, project, ip)

    return dep


def _gen_or_404(c: Caller, gen_id: str) -> dict:
    g = db.row("SELECT * FROM generations WHERE id=? AND project_id=?", (gen_id, c.project["id"]))
    if not g:
        raise APIError(404, "not_found", "Kayıt bulunamadı.")
    return g


def _error_status(code: str) -> int:
    return {"nothing_to_read": 422, "voice_unavailable": 503, "canceled": 409}.get(code, 502)


# ---------------------------------------------------------------------------- uç noktalar

@router.post("/speech", summary="Metni seslendir (yanıtı bekler)", status_code=200,
             responses={202: {"description": "Bekleme süresi doldu; iş sürüyor, durumu Location adresinden izleyin"}})
async def speech(body: SpeechIn, request: Request, c: Caller = Depends(caller("speech:create")),
                 idempotency_key: Annotated[Optional[str], Header(max_length=128)] = None,
                 response_format: Annotated[Optional[Literal["json", "binary"]], Query(alias="response")] = None):
    """Metni seslendirir ve ses hazır olduğunda **ses dosyasının URL'sini** döndürür.

    Ses `sync_wait_seconds` (varsayılan 60 sn) içinde hazır olmazsa **202** ile iş kimliği döner;
    durumu `GET /v1/jobs/{id}` ile izleyebilir ya da webhook bekleyebilirsiniz.

    `?response=binary` ya da `Accept: audio/*` ile JSON yerine doğrudan ses dosyası alınır.
    """
    g, _ = await run_in_threadpool(create_generation, c.project, body.model_dump(), key=c.key, source="api",
                                   mode="sync", ip=c.ip, idempotency_key=idempotency_key,
                                   explicit=set(body.model_fields_set))
    await wait_for(g["id"], settings.get("sync_wait_seconds"))
    g = await run_in_threadpool(db.row, "SELECT * FROM generations WHERE id=?", (g["id"],))
    out = await run_in_threadpool(public, g, c.project)
    if g["status"] in ACTIVE:
        return JSONResponse(out, status_code=202, headers={"Location": f"/v1/jobs/{g['id']}"})
    if g["status"] != "succeeded":
        code = g.get("error_code") or g["status"]
        raise APIError(_error_status(code), code, g.get("error_message") or "Ses üretilemedi.", generation_id=g["id"])
    binary = response_format == "binary" or (response_format is None and
                                             request.headers.get("accept", "").startswith("audio/"))
    if binary:
        path = audio_path(g)
        if not path:
            raise APIError(410, "audio_deleted", "Ses dosyası silinmiş.")
        return FileResponse(path, media_type=FORMAT_MIME[g["format"]], headers={
            "X-Generation-Id": g["id"], "X-Audio-Duration": str(g["duration"]),
            "X-Audio-Url": out["audio"]["url"], "Content-Disposition": f'inline; filename="{g["id"]}.{g["format"]}"'})
    return out


@router.post("/jobs", summary="Seslendirme işini kuyruğa al (beklemez)", status_code=202)
def create_job(body: SpeechIn, request: Request, c: Caller = Depends(caller("speech:create")),
               idempotency_key: Annotated[Optional[str], Header(max_length=128)] = None):
    """Uzun metinler için. Hemen **202** döner. Bitince projenin (ya da istekteki) webhook adresine
    `speech.succeeded` / `speech.failed` olayı gönderilir; `GET /v1/jobs/{id}` ile de izlenebilir."""
    g, created = create_generation(c.project, body.model_dump(), key=c.key, source="api", mode="async", ip=c.ip,
                                   idempotency_key=idempotency_key, explicit=set(body.model_fields_set))
    g = db.row("SELECT * FROM generations WHERE id=?", (g["id"],))
    return JSONResponse(public(g, c.project), status_code=202 if created else 200,
                        headers={"Location": f"/v1/jobs/{g['id']}"})


@router.get("/jobs/{gen_id}", summary="İşin durumu ve ses bağlantısı")
def get_job(gen_id: str, c: Caller = Depends(caller("speech:read")),
            include: Annotated[Optional[str], Query(description="virgülle: words,spoken_text")] = None):
    g = _gen_or_404(c, gen_id)
    inc = set((include or "").split(",")) - {""} if include is not None else None
    return public(g, c.project, inc)


@router.get("/jobs", summary="İşleri listele")
def list_jobs(c: Caller = Depends(caller("speech:read")), limit: int = Query(20, ge=1, le=100),
              starting_after: Optional[str] = None, status: Optional[str] = None,
              reference_id: Optional[str] = None):
    where, params = ["project_id=?"], [c.project["id"]]
    if status:
        where.append("status=?")
        params.append(status)
    if reference_id:
        where.append("reference_id=?")
        params.append(reference_id)
    if starting_after:
        ref = db.row("SELECT created_at, id FROM generations WHERE id=? AND project_id=?",
                     (starting_after, c.project["id"]))
        if ref:
            where.append("(created_at<? OR (created_at=? AND id<?))")
            params += [ref["created_at"], ref["created_at"], ref["id"]]
    items = db.rows(f"SELECT * FROM generations WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC "
                    f"LIMIT ?", (*params, limit + 1))
    has_more = len(items) > limit
    items = items[:limit]
    return {"object": "list", "data": [public(g, c.project, set()) for g in items], "has_more": has_more,
            "next_cursor": items[-1]["id"] if has_more and items else None}


@router.post("/jobs/{gen_id}/cancel", summary="Bekleyen işi iptal et")
def cancel_job(gen_id: str, c: Caller = Depends(caller("speech:create"))):
    g = _gen_or_404(c, gen_id)
    if g["status"] == "queued":
        db.execute("UPDATE generations SET status='canceled', finished_at=?, error_code='canceled', "
                   "error_message='İptal edildi.' WHERE id=? AND status='queued'", (now(), gen_id))
    elif g["status"] == "running":
        db.execute("UPDATE generations SET cancel_requested=1 WHERE id=?", (gen_id,))
    else:
        raise APIError(409, "not_cancelable", "İş zaten tamamlanmış.")
    return public(db.row("SELECT * FROM generations WHERE id=?", (gen_id,)), c.project, set())


@router.delete("/jobs/{gen_id}", summary="Ses dosyasını hemen sil")
def delete_job(gen_id: str, c: Caller = Depends(caller("speech:delete"))):
    g = _gen_or_404(c, gen_id)
    if g["status"] in ACTIVE:
        raise APIError(409, "job_active", "İş sürerken silinemez; önce iptal edin.")
    delete_audio(g)
    return {"id": gen_id, "object": "speech", "deleted": True}


@router.get("/voices", summary="Bu projenin kullanabileceği sesler")
def voices(c: Caller = Depends(caller("speech:read"))):
    return {"object": "list", "data": project_voices(c.project)}


@router.get("/presets", summary="Hazır ayarlar (haber, hikâye…)")
def list_presets(c: Caller = Depends(caller("speech:read"))):
    """İstekte `preset` alanıyla kullanılabilecek hazır ayarlar. `default` olan, istekte preset
    gönderilmediğinde bu projede otomatik uygulanır."""
    from . import presets

    default = c.project.get("default_preset")
    return {"object": "list", "data": [presets.public(p) | {"default": p["id"] == default}
                                       for p in presets.all_presets()]}


@router.get("/usage", summary="Kullanım ve sınırlar")
def get_usage(c: Caller = Depends(caller("speech:read"))):
    p = c.project
    u = usage(p["id"])
    return {
        "project": {"id": p["id"], "name": p["name"]},
        "limits": {"requests_per_minute": p["rate_limit_rpm"], "max_concurrency": p["max_concurrency"],
                   "max_chars_per_request": min(p["max_chars_per_request"], settings.get("global_max_chars")),
                   "daily_char_quota": p["daily_char_quota"] or None,
                   "monthly_char_quota": p["monthly_char_quota"] or None},
        "usage": u,
        "retention_days": (p["retention_days"] if p["retention_days"] is not None else settings.get("retention_days"))
        if settings.get("auto_delete_enabled") else None,
    }


@router.post("/normalize", summary="Metnin okunuş biçimini önizle")
def normalize(body: NormalizeIn, c: Caller = Depends(caller("speech:read"))):
    try:
        return {"spoken_text": engine.normalize(body.text)}
    except Exception:
        raise APIError(503, "engine_unavailable", "Ses motoruna ulaşılamadı.", {"Retry-After": "10"})
