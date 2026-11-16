"""TTS: Türkçe metinden konuşma platformu. Herkese açık API + yönetim paneli + kuyruk."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import admin, api_v1, config, db, security, settings
from .core import APIError, engine, now
from .runner import runner
from .service import FORMAT_MIME, audio_path

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tini")
STATIC = Path(__file__).resolve().parent.parent / "static"


def bootstrap():
    if not config.SECRET_KEY or len(config.SECRET_KEY) < 32:
        raise RuntimeError("SECRET_KEY en az 32 karakter olmalı (ör. `openssl rand -hex 32`).")
    db.migrate()
    config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    if not db.val("SELECT COUNT(*) FROM users", (), 0):
        if not config.ADMIN_EMAIL or "@" not in config.ADMIN_EMAIL or not config.ADMIN_PASSWORD:
            raise RuntimeError("İlk kurulum için ADMIN_EMAIL ve ADMIN_PASSWORD gerekli.")
        db.insert("users", {"id": security.new_id("usr", 12), "username": config.ADMIN_EMAIL,
                            "email": config.ADMIN_EMAIL, "name": config.ADMIN_NAME,
                            "password_hash": security.hash_password(config.ADMIN_PASSWORD), "role": "admin",
                            "disabled": 0, "created_at": now(), "password_changed_at": now()})
        log.info("İlk yönetici oluşturuldu: %s", config.ADMIN_EMAIL)
    elif config.ADMIN_EMAIL and not db.row("SELECT id FROM users WHERE email=? COLLATE NOCASE", (config.ADMIN_EMAIL,)):
        # önceki sürümden (kullanıcı adıyla giriş) geçiş: e-postası olmayan ilk yöneticiye ADMIN_EMAIL atanır
        legacy = db.row("SELECT id FROM users WHERE email IS NULL AND role='admin' ORDER BY created_at LIMIT 1")
        if legacy:
            db.update("users", legacy["id"], {"email": config.ADMIN_EMAIL, "username": config.ADMIN_EMAIL,
                                              "name": config.ADMIN_NAME})
            log.info("Mevcut yöneticiye e-posta atandı: %s", config.ADMIN_EMAIL)
    from . import presets

    presets.seed()
    if not db.val("SELECT COUNT(*) FROM projects WHERE is_system=1", (), 0):
        db.insert("projects", {"id": "prj_panel", "name": "Panel", "description": "Panelin deneme alanından gelen "
                               "istekler.", "status": "active", "is_system": 1, "allowed_voices": "[]",
                               "rate_limit_rpm": 1000, "max_concurrency": 2, "max_chars_per_request": 200000,
                               "daily_char_quota": 0, "monthly_char_quota": 0, "retention_days": None,
                               "store_text": 1, "webhook_url": "", "webhook_secret": "", "ip_allowlist": "",
                               "allowed_origins": "", "created_at": now(), "updated_at": now()})


@asynccontextmanager
async def lifespan(app: FastAPI):
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = 200
    bootstrap()
    runner.start()
    yield


app = FastAPI(title="TTS API", version=config.VERSION, lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url="/openapi.json",
              description=(
                  "Türkçe metinden konuşma API'si.\n\n"
                  "**Kimlik doğrulama:** `Authorization: Bearer <API_ANAHTARI>`\n\n"
                  "**Akış:**\n"
                  "1. `POST /v1/speech` → ses hazır olunca `audio.url` döner (kısa metinler için).\n"
                  "2. `POST /v1/jobs` → hemen `202`; bitince webhook gelir ya da `GET /v1/jobs/{id}` ile izlenir.\n\n"
                  "**Hata biçimi:** `{\"error\": {\"code\": \"...\", \"message\": \"...\"}}` ve `X-Request-ID` başlığı.\n\n"
                  "**Tekrar koruması:** `Idempotency-Key` başlığı ile aynı istek iki kez işlenmez.\n\n"
                  "**Webhook imzası:** `X-Tini-Signature: sha256=HMAC(secret, \"{X-Tini-Timestamp}.{gövde}\")`"))


# ---------------------------------------------------------------------------- hata biçimi

def _err(status, code, message, request: Request, headers=None, **extra):
    body = {"error": {"code": code, "message": message, **extra,
                      "request_id": getattr(request.state, "request_id", None)}}
    return JSONResponse(body, status_code=status, headers=headers)


@app.exception_handler(APIError)
async def api_error(request: Request, exc: APIError):
    return _err(exc.status, exc.code, exc.message, request, exc.headers, **exc.extra)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
        msg = e.get("msg", "").replace("Value error, ", "")
        if e.get("type") == "extra_forbidden":
            msg = "Tanınmayan alan"
        details.append({"field": loc, "message": msg})
    first = details[0] if details else {"field": "", "message": "Geçersiz istek"}
    return _err(422, "invalid_request", f"{first['field']}: {first['message']}".strip(": "), request,
                details=details)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    msg = {404: "Bulunamadı.", 405: "Bu yöntem desteklenmiyor."}.get(exc.status_code, str(exc.detail))
    return _err(exc.status_code, code, msg, request)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Beklenmeyen hata: %s %s", request.method, request.url.path)
    return _err(500, "internal_error", "Beklenmeyen bir hata oluştu.", request)


# ---------------------------------------------------------------------------- ara katman

PANEL_PATHS = ("/admin/", "/static/", "/login")


def _host(request: Request) -> str:
    return (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(":")[0].lower()


@app.middleware("http")
async def middleware(request: Request, call_next):
    rid = request.headers.get("x-request-id") or security.new_id("req", 16)
    if len(rid) > 64:
        rid = security.new_id("req", 16)
    request.state.request_id = rid
    path = request.url.path
    host = _host(request)
    is_panel = path == "/" or path.startswith(PANEL_PATHS)

    # domain ayrımı
    if config.PANEL_HOSTS and is_panel and host not in config.PANEL_HOSTS:
        if path == "/" and config.API_HOSTS and host in config.API_HOSTS:
            return RedirectResponse("/docs")
        return _err(404, "not_found", "Bulunamadı.", request)
    if config.API_HOSTS and (path.startswith("/v1/") or path.startswith("/files/")) and host not in config.API_HOSTS:
        return _err(404, "not_found", "Bulunamadı.", request)

    # gövde boyutu
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > config.MAX_BODY_BYTES:
        return _err(413, "payload_too_large", "İstek gövdesi çok büyük.", request)

    # tarayıcı ön kontrolü (CORS) — asıl izin, anahtar doğrulanınca projeye göre verilir
    origin = request.headers.get("origin")
    if request.method == "OPTIONS" and path.startswith("/v1/") and origin:
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": origin, "Vary": "Origin",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, X-API-Key, Content-Type, Idempotency-Key, X-Request-ID",
            "Access-Control-Max-Age": "600"})

    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000

    response.headers["X-Request-ID"] = rid
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if path.startswith("/v1/"):
        cors = getattr(request.state, "cors_origin", None)
        if cors:
            response.headers["Access-Control-Allow-Origin"] = cors
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Expose-Headers"] = (
                "X-Request-ID, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, Location, Retry-After")
        rl = getattr(request.state, "ratelimit", None)
        if rl:
            response.headers["X-RateLimit-Limit"] = str(rl[0])
            response.headers["X-RateLimit-Remaining"] = str(rl[1])
            response.headers["X-RateLimit-Reset"] = str(rl[2])
        response.headers.setdefault("Cache-Control", "no-store")
    if is_panel and not path.startswith("/static/"):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'")
    if request.headers.get("x-forwarded-proto") == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if path.startswith("/v1/") or path.startswith("/files/"):
        log.info("%s %s %s %d %.0fms", rid, request.method, path, response.status_code, ms)
    return response


# ---------------------------------------------------------------------------- yönlendiriciler

app.include_router(api_v1.router)
app.include_router(admin.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "version": config.VERSION}


@app.get("/readyz", include_in_schema=False)
def readyz():
    h = engine.health()
    ok = h.get("status") == "ok"
    return JSONResponse({"status": "ok" if ok else "degraded", "engine": h.get("status"),
                         "maintenance": settings.get("maintenance_mode")}, status_code=200 if ok else 503)


@app.get("/files/{name}", include_in_schema=False)
def files(name: str, request: Request, s: str = "", e: int = 0, download: bool = False):
    gen_id, _, ext = name.rpartition(".")
    if not gen_id or ext not in FORMAT_MIME or not security.check_file_sig(gen_id, s, e):
        return _err(404, "not_found", "Dosya bulunamadı ya da bağlantının süresi dolmuş.", request)
    g = db.row("SELECT * FROM generations WHERE id=?", (gen_id,))
    if not g or g["format"] != ext:
        return _err(404, "not_found", "Dosya bulunamadı ya da bağlantının süresi dolmuş.", request)
    path = audio_path(g)
    if not path:
        return _err(410, "audio_deleted", "Ses dosyası silinmiş.", request)
    filename = f"{(g.get('reference_id') or g['id'])[:80]}.{ext}"
    return FileResponse(path, media_type=FORMAT_MIME[ext], filename=filename,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "private, max-age=3600",
                                 "X-Robots-Tag": "noindex"})


@app.get("/docs", include_in_schema=False)
def docs():
    if not settings.get("docs_public"):
        return JSONResponse({"error": {"code": "not_found", "message": "Bulunamadı."}}, status_code=404)
    return get_swagger_ui_html(openapi_url="/openapi.json", title="TTS API belgeleri")


@app.get("/redoc", include_in_schema=False)
def redoc():
    if not settings.get("docs_public"):
        return JSONResponse({"error": {"code": "not_found", "message": "Bulunamadı."}}, status_code=404)
    return get_redoc_html(openapi_url="/openapi.json", title="TTS API belgeleri")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(STATIC / "index.html")
