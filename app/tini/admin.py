"""Yönetim paneli API'si (/admin/api). Oturum çerezi + CSRF başlığı ile korunur."""
from __future__ import annotations

import collections
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from . import config, db, security, settings
from .api_v1 import SCOPES, SpeechIn, clear_key_cache, client_ip
from .core import APIError, audit, day_start, engine, iso, now, styles_catalog, voice_catalog
from .runner import cleanup, deliver, enqueue_webhook, runner
from .service import (ACTIVE, audio_path, create_generation, delete_audio, expires_at, file_url, public, usage,
                      wait_for)

router = APIRouter(prefix="/admin/api", tags=["admin"], include_in_schema=False)
COOKIE = "tini_session"
_fails: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_fails_lock = threading.Lock()


# ---------------------------------------------------------------------------- oturum

def _audit_user(u: dict) -> dict:
    return {"id": u["id"], "username": u.get("email") or u["username"]}


class User:
    def __init__(self, row: dict, session: dict):
        self.row, self.session = row, session
        self.id, self.role = row["id"], row["role"]
        self.username = row.get("email") or row["username"]

    def as_audit(self):
        return {"id": self.id, "username": self.username}


def current_user(request: Request) -> User:
    token = request.cookies.get(COOKIE)
    if not token:
        raise APIError(401, "unauthenticated", "Giriş yapmanız gerekiyor.")
    s = db.row("SELECT * FROM sessions WHERE id=?", (security.token_hash(token),))
    if not s or s["expires_at"] < now():
        raise APIError(401, "session_expired", "Oturumun süresi doldu, yeniden giriş yapın.")
    u = db.row("SELECT * FROM users WHERE id=?", (s["user_id"],))
    if not u or u["disabled"]:
        raise APIError(401, "unauthenticated", "Hesap kullanılamıyor.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not security.hmac.compare_digest(request.headers.get("x-csrf-token", ""), s["csrf"]):
            raise APIError(403, "csrf", "Güvenlik belirteci geçersiz. Sayfayı yenileyin.")
    if (s["last_seen_at"] or 0) < now() - 60:
        db.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now(), s["id"]))
    return User(u, s)


def admin_only(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise APIError(403, "forbidden", "Bu işlem için yönetici yetkisi gerekir.")
    return user


def _cookie_secure(request: Request) -> bool:
    if config.COOKIE_SECURE in ("true", "false"):
        return config.COOKIE_SECURE == "true"
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LoginIn(BaseModel):
    email: Optional[str] = Field(None, max_length=254)
    username: Optional[str] = Field(None, max_length=254)  # eski istemciler için
    password: str = Field(..., max_length=256)


def _find_user(login_id: str):
    login_id = (login_id or "").strip()
    if not login_id:
        return None
    return db.row("SELECT * FROM users WHERE email=? COLLATE NOCASE", (login_id,)) or \
        db.row("SELECT * FROM users WHERE email IS NULL AND username=?", (login_id,))


def _me(u):
    return {"id": u["id"], "email": u.get("email"), "name": u.get("name") or "", "role": u["role"],
            "username": u.get("email") or u["username"]}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = client_ip(request)
    limit = settings.get("login_max_attempts")
    t = time.time()
    with _fails_lock:
        q = _fails[ip]
        while q and q[0] < t - 900:
            q.popleft()
        if len(q) >= limit:
            raise APIError(429, "too_many_attempts", "Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin.")
    login_id = (body.email or body.username or "").strip()
    u = _find_user(login_id)
    if not u or u["disabled"] or not security.verify_password(body.password, u["password_hash"]):
        with _fails_lock:
            _fails[ip].append(t)
        time.sleep(0.4)
        audit(None, "login_failed", login_id[:64], ip=ip)
        raise APIError(401, "bad_credentials", "E-posta ya da şifre yanlış.")
    with _fails_lock:
        _fails.pop(ip, None)
    token = security.rand(48)
    csrf = security.rand(32)
    hours = settings.get("session_hours")
    db.insert("sessions", {"id": security.token_hash(token), "user_id": u["id"], "csrf": csrf, "created_at": now(),
                           "expires_at": now() + hours * 3600, "last_seen_at": now(), "ip": ip,
                           "user_agent": request.headers.get("user-agent", "")[:300]})
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), u["id"]))
    response.set_cookie(COOKIE, token, max_age=hours * 3600, httponly=True, samesite="strict",
                        secure=_cookie_secure(request), path="/")
    audit(_audit_user(u), "login", ip=ip)
    return {"user": _me(u), "csrf": csrf}


@router.post("/auth/logout")
def logout(response: Response, user: User = Depends(current_user)):
    db.execute("DELETE FROM sessions WHERE id=?", (user.session["id"],))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return {"user": _me(user.row), "csrf": user.session["csrf"], "version": config.VERSION}


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(..., max_length=256)


@router.post("/auth/password")
def change_password(body: PasswordIn, request: Request, user: User = Depends(current_user)):
    if not security.verify_password(body.current_password, user.row["password_hash"]):
        raise APIError(400, "bad_password", "Mevcut şifre yanlış.")
    if problem := security.password_problems(body.new_password):
        raise APIError(400, "weak_password", problem)
    db.update("users", user.id, {"password_hash": security.hash_password(body.new_password),
                                 "password_changed_at": now()})
    db.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (user.id, user.session["id"]))
    audit(user.as_audit(), "password_changed", ip=client_ip(request))
    return {"ok": True}


class ProfileIn(BaseModel):
    name: Optional[str] = Field(None, max_length=80)
    email: Optional[str] = Field(None, max_length=254)
    current_password: Optional[str] = Field(None, max_length=256)


@router.patch("/auth/profile")
def update_profile(body: ProfileIn, request: Request, user: User = Depends(current_user)):
    d = {}
    if body.name is not None:
        d["name"] = body.name.strip()
    if body.email is not None and body.email.strip().lower() != (user.row.get("email") or ""):
        email = body.email.strip().lower()
        if not EMAIL_RE.match(email):
            raise APIError(400, "bad_email", "Geçerli bir e-posta adresi yazın.")
        if not body.current_password or not security.verify_password(body.current_password, user.row["password_hash"]):
            raise APIError(400, "bad_password", "E-postayı değiştirmek için mevcut şifrenizi doğru yazın.")
        if db.row("SELECT id FROM users WHERE email=? COLLATE NOCASE AND id<>?", (email, user.id)):
            raise APIError(409, "exists", "Bu e-posta başka bir kullanıcıda kayıtlı.")
        d["email"] = email
        d["username"] = email
    db.update("users", user.id, d)
    audit(user.as_audit(), "profile_updated", details={k: v for k, v in d.items() if k != "username"},
          ip=client_ip(request))
    return {"user": _me(db.row("SELECT * FROM users WHERE id=?", (user.id,)))}


@router.get("/auth/sessions")
def sessions(user: User = Depends(current_user)):
    rows = db.rows("SELECT id, created_at, last_seen_at, ip, user_agent, expires_at FROM sessions WHERE user_id=? "
                   "ORDER BY last_seen_at DESC", (user.id,))
    for r in rows:
        r["current"] = r["id"] == user.session["id"]
        r["id"] = r["id"][:16]
    return {"sessions": rows}


@router.post("/auth/sessions/revoke-others")
def revoke_others(user: User = Depends(current_user)):
    db.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (user.id, user.session["id"]))
    return {"ok": True}


# ---------------------------------------------------------------------------- genel bakış

@router.get("/overview")
def overview(days: int = 14, project_id: Optional[str] = None, user: User = Depends(current_user)):
    days = max(1, min(days, 90))
    since = day_start() - (days - 1) * 86400
    pf, pp = ("AND project_id=?", (project_id,)) if project_id else ("", ())
    series = db.rows(
        "SELECT (created_at - ?) / 86400 AS d, COUNT(*) n, COALESCE(SUM(chars),0) c, "
        "COALESCE(SUM(CASE WHEN status='succeeded' THEN duration END),0) s, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) f "
        f"FROM generations WHERE created_at>=? {pf} GROUP BY d ORDER BY d", (since, since, *pp))
    by_day = {int(r["d"]): r for r in series}
    out_series = []
    for i in range(days):
        r = by_day.get(i, {})
        out_series.append({"date": iso(since + i * 86400 + 43200)[:10], "requests": r.get("n", 0),
                           "chars": r.get("c", 0), "audio_seconds": round(r.get("s", 0) or 0, 1),
                           "failed": r.get("f", 0) or 0})
    today = day_start()
    t = db.row("SELECT COUNT(*) n, COALESCE(SUM(chars),0) c, COALESCE(SUM(CASE WHEN status='succeeded' "
               "THEN duration END),0) s, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) f, "
               f"AVG(CASE WHEN status='succeeded' THEN processing_ms END) p FROM generations WHERE created_at>=? {pf}",
               (today, *pp))
    period = db.row("SELECT COUNT(*) n, COALESCE(SUM(chars),0) c, COALESCE(SUM(CASE WHEN status='succeeded' "
                    "THEN duration END),0) s, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) f "
                    f"FROM generations WHERE created_at>=? {pf}", (since, *pp))
    queue = db.row("SELECT SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) q, "
                   "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) r FROM generations")
    storage = db.row("SELECT COUNT(*) n, COALESCE(SUM(audio_size),0) b FROM generations "
                     "WHERE audio_path IS NOT NULL AND audio_deleted_at IS NULL")
    top = db.rows("SELECT p.id, p.name, COUNT(g.id) n, COALESCE(SUM(g.chars),0) c FROM projects p "
                  "LEFT JOIN generations g ON g.project_id=p.id AND g.created_at>=? "
                  "WHERE p.is_system=0 GROUP BY p.id ORDER BY n DESC LIMIT 6", (since,))
    recent = db.rows(f"SELECT * FROM generations WHERE 1=1 {pf} ORDER BY created_at DESC LIMIT 8", pp)
    return {
        "today": {"requests": t["n"], "chars": t["c"], "audio_seconds": round(t["s"] or 0, 1),
                  "failed": t["f"] or 0, "avg_processing_ms": int(t["p"] or 0)},
        "period": {"days": days, "requests": period["n"], "chars": period["c"],
                   "audio_seconds": round(period["s"] or 0, 1), "failed": period["f"] or 0},
        "series": out_series,
        "queue": {"queued": queue["q"] or 0, "running": queue["r"] or 0},
        "storage": {"files": storage["n"], "bytes": storage["b"], "disk": _disk()},
        "engine": engine.health(),
        "top_projects": top,
        "recent": [_gen_row(g) for g in recent],
        "maintenance": settings.get("maintenance_mode"),
    }


def _disk():
    try:
        u = shutil.disk_usage(config.DATA_DIR)
        return {"total": u.total, "free": u.free}
    except Exception:
        return None


# ---------------------------------------------------------------------------- projeler

class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    status: Optional[Literal["active", "paused"]] = None
    default_voice: Optional[str] = Field(None, max_length=64)
    default_preset: Optional[str] = Field(None, max_length=40)
    allowed_voices: Optional[list[str]] = None
    rate_limit_rpm: Optional[int] = Field(None, ge=1, le=100000)
    max_concurrency: Optional[int] = Field(None, ge=1, le=100)
    max_chars_per_request: Optional[int] = Field(None, ge=10, le=200000)
    daily_char_quota: Optional[int] = Field(None, ge=0, le=10**10)
    monthly_char_quota: Optional[int] = Field(None, ge=0, le=10**11)
    retention_days: Optional[int] = Field(None, ge=0, le=3650)
    use_global_retention: Optional[bool] = None
    store_text: Optional[bool] = None
    webhook_url: Optional[str] = Field(None, max_length=2000)
    ip_allowlist: Optional[str] = Field(None, max_length=4000)
    allowed_origins: Optional[str] = Field(None, max_length=4000)


def _project_out(p: dict, with_usage=True) -> dict:
    out = {k: v for k, v in p.items() if k != "webhook_secret"}
    out["allowed_voices"] = db.jload(p["allowed_voices"], [])
    out["webhook_secret_set"] = bool(p["webhook_secret"])
    out["created_at"] = iso(p["created_at"])
    out["updated_at"] = iso(p["updated_at"])
    out["keys_active"] = db.val("SELECT COUNT(*) FROM api_keys WHERE project_id=? AND revoked_at IS NULL "
                                "AND (expires_at IS NULL OR expires_at>?)", (p["id"], now()), 0)
    if with_usage:
        out["usage"] = usage(p["id"])
    return out


def _get_project(pid: str) -> dict:
    p = db.row("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise APIError(404, "not_found", "Proje bulunamadı.")
    return p


def _apply_project(data: dict, changes: ProjectIn):
    d = changes.model_dump(exclude_unset=True)
    if d.pop("use_global_retention", False):
        data["retention_days"] = None
        d.pop("retention_days", None)
    if "allowed_voices" in d:
        known = {v["id"] for v in voice_catalog()}
        bad = [v for v in d["allowed_voices"] if v not in known]
        if bad:
            raise APIError(400, "bad_voice", f"Bilinmeyen ses: {', '.join(bad)}")
        d["allowed_voices"] = db.jdump(d["allowed_voices"])
    if "default_preset" in d:
        if d["default_preset"]:
            from . import presets

            if not presets.get(d["default_preset"]):
                raise APIError(400, "bad_preset", "Bilinmeyen hazır ayar.")
        else:
            d["default_preset"] = None
    if d.get("ip_allowlist"):
        try:
            security.parse_networks(d["ip_allowlist"])
        except ValueError as e:
            raise APIError(400, "bad_ip", f"IP listesi geçersiz: {e}")
    if d.get("webhook_url") and not d["webhook_url"].startswith(("https://", "http://")):
        raise APIError(400, "bad_url", "Webhook adresi http(s):// ile başlamalı.")
    if "store_text" in d:
        d["store_text"] = int(d["store_text"])
    data.update(d)


@router.get("/projects")
def projects(user: User = Depends(current_user)):
    rows = db.rows("SELECT * FROM projects ORDER BY is_system ASC, created_at DESC")
    return {"projects": [_project_out(p) for p in rows]}


@router.post("/projects")
def create_project(body: ProjectIn, request: Request, user: User = Depends(admin_only)):
    if not body.name:
        raise APIError(400, "name_required", "Proje adı gerekli.")
    s = settings.all()
    p = {"id": security.new_id("prj", 14), "name": body.name, "description": "", "status": "active",
         "is_system": 0, "default_voice": None, "default_preset": None, "allowed_voices": "[]",
         "rate_limit_rpm": s["default_rate_limit_rpm"], "max_concurrency": s["default_max_concurrency"],
         "max_chars_per_request": s["default_max_chars"], "daily_char_quota": 0, "monthly_char_quota": 0,
         "retention_days": None, "store_text": 1, "webhook_url": "", "webhook_secret": "whsec_" + security.rand(32),
         "ip_allowlist": "", "allowed_origins": "", "created_at": now(), "updated_at": now()}
    _apply_project(p, body)
    db.insert("projects", p)
    audit(user.as_audit(), "project_created", p["id"], {"name": p["name"]}, client_ip(request))
    return _project_out(p)


@router.get("/projects/{pid}")
def get_project(pid: str, user: User = Depends(current_user)):
    return _project_out(_get_project(pid))


@router.patch("/projects/{pid}")
def update_project(pid: str, body: ProjectIn, request: Request, user: User = Depends(admin_only)):
    p = _get_project(pid)
    data = {}
    _apply_project(data, body)
    if p["is_system"] and data.get("status") == "paused":
        raise APIError(400, "system_project", "Panel projesi durdurulamaz.")
    data["updated_at"] = now()
    db.update("projects", pid, data)
    clear_key_cache()
    audit(user.as_audit(), "project_updated", pid, {k: v for k, v in data.items() if k != "updated_at"},
          client_ip(request))
    return _project_out(_get_project(pid))


@router.delete("/projects/{pid}")
def delete_project(pid: str, request: Request, user: User = Depends(admin_only)):
    p = _get_project(pid)
    if p["is_system"]:
        raise APIError(400, "system_project", "Panel projesi silinemez.")
    if db.val("SELECT COUNT(*) FROM generations WHERE project_id=? AND status IN ('queued','running')", (pid,), 0):
        raise APIError(409, "project_busy", "Projenin süren işleri var. Bitmesini bekleyin ya da projeyi durdurun.")
    for g in db.rows("SELECT * FROM generations WHERE project_id=?", (pid,)):
        delete_audio(g)
    db.execute("DELETE FROM generations WHERE project_id=?", (pid,))
    db.execute("DELETE FROM webhook_deliveries WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    clear_key_cache()
    audit(user.as_audit(), "project_deleted", pid, {"name": p["name"]}, client_ip(request))
    return {"ok": True}


@router.get("/projects/{pid}/usage")
def project_usage(pid: str, days: int = 30, user: User = Depends(current_user)):
    _get_project(pid)
    return overview(days=days, project_id=pid, user=user)


# ---------------------------------------------------------------------------- API anahtarları

class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=80)
    scopes: list[Literal["speech:create", "speech:read", "speech:delete"]] = Field(default_factory=lambda: list(SCOPES))
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)


def _key_out(k: dict) -> dict:
    state = "active"
    if k["revoked_at"]:
        state = "revoked"
    elif k["expires_at"] and k["expires_at"] < now():
        state = "expired"
    return {"id": k["id"], "project_id": k["project_id"], "name": k["name"],
            "display": f"{k['prefix']}…{k['last4']}", "scopes": k["scopes"].split(), "state": state,
            "created_at": iso(k["created_at"]), "created_by": k["created_by"], "expires_at": iso(k["expires_at"]),
            "revoked_at": iso(k["revoked_at"]), "last_used_at": iso(k["last_used_at"]),
            "last_used_ip": k["last_used_ip"], "use_count": k["use_count"]}


@router.get("/projects/{pid}/keys")
def keys(pid: str, user: User = Depends(current_user)):
    _get_project(pid)
    rows = db.rows("SELECT * FROM api_keys WHERE project_id=? ORDER BY revoked_at IS NOT NULL, created_at DESC",
                   (pid,))
    return {"keys": [_key_out(k) for k in rows]}


@router.post("/projects/{pid}/keys")
def create_key(pid: str, body: KeyIn, request: Request, user: User = Depends(admin_only)):
    _get_project(pid)
    if not body.scopes:
        raise APIError(400, "scopes_required", "En az bir yetki seçin.")
    raw = security.new_api_key()
    k = {"id": security.new_id("key", 14), "project_id": pid, "name": body.name, "prefix": raw[:9],
         "last4": raw[-4:], "key_hash": security.key_hash(raw), "scopes": " ".join(sorted(set(body.scopes))),
         "created_at": now(), "created_by": user.username,
         "expires_at": now() + body.expires_in_days * 86400 if body.expires_in_days else None}
    db.insert("api_keys", k)
    audit(user.as_audit(), "key_created", k["id"], {"project": pid, "name": body.name}, client_ip(request))
    return {"key": raw, **_key_out(k | {"revoked_at": None, "last_used_at": None, "last_used_ip": None,
                                        "use_count": 0})}


@router.post("/keys/{kid}/revoke")
def revoke_key(kid: str, request: Request, user: User = Depends(admin_only)):
    k = db.row("SELECT * FROM api_keys WHERE id=?", (kid,))
    if not k:
        raise APIError(404, "not_found", "Anahtar bulunamadı.")
    if not k["revoked_at"]:
        db.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (now(), kid))
        clear_key_cache()
        audit(user.as_audit(), "key_revoked", kid, {"project": k["project_id"], "name": k["name"]},
              client_ip(request))
    return _key_out(db.row("SELECT * FROM api_keys WHERE id=?", (kid,)))


@router.delete("/keys/{kid}")
def delete_key(kid: str, request: Request, user: User = Depends(admin_only)):
    k = db.row("SELECT * FROM api_keys WHERE id=?", (kid,))
    if not k:
        raise APIError(404, "not_found", "Anahtar bulunamadı.")
    if not k["revoked_at"] and not (k["expires_at"] and k["expires_at"] < now()):
        raise APIError(409, "key_active", "Etkin anahtar silinemez; önce iptal edin.")
    db.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    audit(user.as_audit(), "key_deleted", kid, {"project": k["project_id"]}, client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------- webhook

@router.post("/projects/{pid}/webhook/rotate")
def rotate_secret(pid: str, request: Request, user: User = Depends(admin_only)):
    _get_project(pid)
    secret = "whsec_" + security.rand(32)
    db.update("projects", pid, {"webhook_secret": secret, "updated_at": now()})
    audit(user.as_audit(), "webhook_secret_rotated", pid, ip=client_ip(request))
    return {"webhook_secret": secret}


@router.get("/projects/{pid}/webhook/secret")
def reveal_secret(pid: str, request: Request, user: User = Depends(admin_only)):
    p = _get_project(pid)
    audit(user.as_audit(), "webhook_secret_viewed", pid, ip=client_ip(request))
    return {"webhook_secret": p["webhook_secret"]}


@router.post("/projects/{pid}/webhook/test")
def test_webhook(pid: str, user: User = Depends(admin_only)):
    p = _get_project(pid)
    if not p["webhook_url"]:
        raise APIError(400, "no_webhook", "Önce webhook adresini kaydedin.")
    fake = {"id": "gen_test", "project_id": pid, "status": "succeeded", "mode": "async", "voice": "ema",
            "model": "ema-lightning", "params": "{}", "format": "mp3", "chars": 42, "reference_id": "test",
            "metadata": None, "created_at": now(), "started_at": now(), "finished_at": now(), "processing_ms": 900,
            "audio_path": None, "priority": 0}
    did = security.new_id("whd", 20)
    payload = {"id": did, "event": "webhook.test", "created_at": iso(now()), "data": public(fake, p, set())}
    db.insert("webhook_deliveries", {"id": did, "generation_id": None, "project_id": pid, "url": p["webhook_url"],
                                     "event": "webhook.test", "payload": db.jdump(payload), "status": "pending",
                                     "attempts": 0, "next_attempt_at": now() + 3600, "created_at": now()})
    d = db.row("SELECT * FROM webhook_deliveries WHERE id=?", (did,))
    res = deliver(d)
    if res.get("status") != "delivered":
        db.update("webhook_deliveries", did, {"status": "failed"})
    return {"delivery_id": did, **res}


@router.get("/projects/{pid}/webhook/deliveries")
def deliveries(pid: str, user: User = Depends(current_user)):
    rows = db.rows("SELECT id, generation_id, url, event, status, attempts, next_attempt_at, last_status_code, "
                   "last_error, created_at, delivered_at FROM webhook_deliveries WHERE project_id=? "
                   "ORDER BY created_at DESC LIMIT 50", (pid,))
    for r in rows:
        for k in ("next_attempt_at", "created_at", "delivered_at"):
            r[k] = iso(r[k])
    return {"deliveries": rows}


@router.post("/webhook/deliveries/{did}/retry")
def retry_delivery(did: str, user: User = Depends(admin_only)):
    d = db.row("SELECT * FROM webhook_deliveries WHERE id=?", (did,))
    if not d:
        raise APIError(404, "not_found", "Gönderim bulunamadı.")
    db.update("webhook_deliveries", did, {"status": "pending", "next_attempt_at": now(), "attempts": 0})
    return {"ok": True}


# ---------------------------------------------------------------------------- istek kayıtları

def _gen_row(g: dict) -> dict:
    params = db.jload(g.get("params"), {}) or {}
    project = db.row("SELECT name, retention_days FROM projects WHERE id=?", (g["project_id"],)) or {}
    text = g.get("text") or ""
    return {"id": g["id"], "project_id": g["project_id"], "project_name": project.get("name"),
            "source": g["source"], "mode": g["mode"], "status": g["status"], "voice": g["voice"],
            "style": params.get("style"), "format": g["format"], "chars": g["chars"],
            "excerpt": " ".join(text.split())[:160] if text else None, "reference_id": g.get("reference_id"),
            "duration": g.get("duration"), "processing_ms": g.get("processing_ms"),
            "created_at": iso(g["created_at"]), "error_code": g.get("error_code"),
            "audio_available": bool(g.get("audio_path")) and not g.get("audio_deleted_at"),
            "keep": bool(g.get("keep"))}


@router.get("/generations")
def generations(user: User = Depends(current_user), project_id: Optional[str] = None,
                status: Optional[str] = None, voice: Optional[str] = None, q: Optional[str] = None,
                source: Optional[str] = None, before: Optional[str] = None, limit: int = 30):
    limit = max(1, min(limit, 100))
    where, params = ["1=1"], []
    for col, v in (("project_id", project_id), ("voice", voice), ("source", source)):
        if v:
            where.append(f"{col}=?")
            params.append(v)
    if status:
        if status == "expired":
            where.append("status='succeeded' AND audio_deleted_at IS NOT NULL")
        else:
            where.append("status=?")
            params.append(status)
    if q:
        where.append("(id=? OR reference_id=? OR text LIKE ?)")
        params += [q.strip(), q.strip(), f"%{q.strip()}%"]
    if before:
        ref = db.row("SELECT created_at, id FROM generations WHERE id=?", (before,))
        if ref:
            where.append("(created_at<? OR (created_at=? AND id<?))")
            params += [ref["created_at"], ref["created_at"], ref["id"]]
    rows = db.rows(f"SELECT * FROM generations WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC "
                   f"LIMIT ?", (*params, limit + 1))
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [_gen_row(g) for g in rows], "next": rows[-1]["id"] if more and rows else None}


@router.get("/generations/{gid}")
def generation(gid: str, user: User = Depends(current_user)):
    g = db.row("SELECT * FROM generations WHERE id=?", (gid,))
    if not g:
        raise APIError(404, "not_found", "Kayıt bulunamadı.")
    project = db.row("SELECT * FROM projects WHERE id=?", (g["project_id"],))
    key = db.row("SELECT name, prefix, last4 FROM api_keys WHERE id=?", (g["key_id"],)) if g["key_id"] else None
    out = _gen_row(g)
    out.update({
        "text": g.get("text"), "spoken_text": g.get("spoken_text"), "words": db.jload(g.get("words"), []),
        "peaks": db.jload(g.get("peaks"), []), "params": db.jload(g.get("params"), {}),
        "metadata": db.jload(g.get("metadata")), "error_message": g.get("error_message"),
        "sample_rate": g.get("sample_rate"), "seed": g.get("seed"), "model": g.get("model"),
        "audio_size": g.get("audio_size"), "started_at": iso(g.get("started_at")),
        "finished_at": iso(g.get("finished_at")), "attempts": g.get("attempts"), "client_ip": g.get("client_ip"),
        "key": f"{key['name']} ({key['prefix']}…{key['last4']})" if key else None,
        "expires_at": iso(expires_at(g, project)), "audio_deleted_at": iso(g.get("audio_deleted_at")),
        "public": public(g, project, set()),
        "deliveries": db.rows("SELECT id, event, status, attempts, last_status_code, last_error, url "
                              "FROM webhook_deliveries WHERE generation_id=? ORDER BY created_at DESC", (gid,)),
    })
    return out


@router.get("/generations/{gid}/audio")
def generation_audio(gid: str, download: bool = False, user: User = Depends(current_user)):
    g = db.row("SELECT * FROM generations WHERE id=?", (gid,))
    path = audio_path(g) if g else None
    if not path:
        raise APIError(404, "not_found", "Ses dosyası yok.")
    from .service import FORMAT_MIME

    return FileResponse(path, media_type=FORMAT_MIME[g["format"]], filename=f"{g['reference_id'] or g['id']}.{g['format']}",
                        content_disposition_type="attachment" if download else "inline")


class KeepIn(BaseModel):
    keep: bool


@router.post("/generations/{gid}/keep")
def keep(gid: str, body: KeepIn, user: User = Depends(admin_only)):
    db.execute("UPDATE generations SET keep=? WHERE id=?", (int(body.keep), gid))
    return {"ok": True, "keep": body.keep}


@router.post("/generations/{gid}/cancel")
def cancel_generation(gid: str, user: User = Depends(admin_only)):
    g = db.row("SELECT * FROM generations WHERE id=?", (gid,))
    if not g or g["status"] not in ACTIVE:
        raise APIError(409, "not_cancelable", "İş zaten bitmiş.")
    if g["status"] == "queued":
        db.execute("UPDATE generations SET status='canceled', finished_at=?, error_code='canceled', "
                   "error_message='İptal edildi.' WHERE id=?", (now(), gid))
    else:
        db.execute("UPDATE generations SET cancel_requested=1 WHERE id=?", (gid,))
    return {"ok": True}


@router.post("/generations/{gid}/retry")
def retry_generation(gid: str, request: Request, user: User = Depends(admin_only)):
    g = db.row("SELECT * FROM generations WHERE id=?", (gid,))
    if not g or g["status"] not in ("failed", "canceled"):
        raise APIError(409, "not_retryable", "Yalnız başarısız ya da iptal edilmiş işler yeniden denenebilir.")
    if not g.get("text"):
        raise APIError(409, "no_text", "Bu projede metin saklanmadığı için yeniden denenemez.")
    db.update("generations", gid, {"status": "queued", "attempts": 0, "not_before": 0, "error_code": None,
                                   "error_message": None, "finished_at": None, "started_at": None,
                                   "cancel_requested": 0})
    runner.wake()
    audit(user.as_audit(), "generation_retried", gid, ip=client_ip(request))
    return {"ok": True}


@router.post("/generations/{gid}/resend-webhook")
def resend_webhook(gid: str, user: User = Depends(admin_only)):
    g = db.row("SELECT * FROM generations WHERE id=?", (gid,))
    if not g or g["status"] in ACTIVE:
        raise APIError(409, "not_ready", "İş henüz bitmedi.")
    did = enqueue_webhook(g)
    if not did:
        raise APIError(400, "no_webhook", "Bu proje için webhook tanımlı değil.")
    return {"delivery_id": did}


@router.delete("/generations/{gid}")
def delete_generation(gid: str, request: Request, audio_only: bool = False, user: User = Depends(admin_only)):
    g = db.row("SELECT * FROM generations WHERE id=?", (gid,))
    if not g:
        raise APIError(404, "not_found", "Kayıt bulunamadı.")
    if g["status"] in ACTIVE:
        raise APIError(409, "job_active", "İş sürerken silinemez; önce iptal edin.")
    delete_audio(g)
    if not audio_only:
        db.execute("DELETE FROM generations WHERE id=?", (gid,))
    audit(user.as_audit(), "generation_deleted", gid, {"audio_only": audio_only}, client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------- deneme alanı

class PlaygroundIn(SpeechIn):
    model_config = ConfigDict(extra="forbid")
    project_id: Optional[str] = None


@router.post("/playground")
async def playground(body: PlaygroundIn, request: Request, user: User = Depends(current_user)):
    if body.project_id:
        project = _get_project(body.project_id)
    else:
        project = db.row("SELECT * FROM projects WHERE is_system=1 LIMIT 1")
    data = body.model_dump(exclude={"project_id"})
    data["include"] = ["words", "spoken_text"]
    g, _ = await run_in_threadpool(create_generation, project, data, key=None, source="panel", mode="sync",
                                   ip=client_ip(request))
    await wait_for(g["id"], 600)
    return await run_in_threadpool(generation, g["id"], user)


class NormIn(BaseModel):
    text: str = Field(..., max_length=60000)


@router.post("/normalize")
def normalize(body: NormIn, user: User = Depends(current_user)):
    try:
        return {"spoken_text": engine.normalize(body.text)}
    except Exception:
        raise APIError(503, "engine_unavailable", "Ses motoruna ulaşılamadı.")


# ---------------------------------------------------------------------------- sesler

@router.get("/voices")
def voices(user: User = Depends(current_user)):
    return {"voices": voice_catalog(), "styles": styles_catalog(), "engine": engine.health()}


class VoiceIn(BaseModel):
    enabled: Optional[bool] = None
    display_name: Optional[str] = Field(None, max_length=60)
    sort: Optional[int] = None


@router.patch("/voices/{vid}")
def update_voice(vid: str, body: VoiceIn, request: Request, user: User = Depends(admin_only)):
    if vid not in {v["id"] for v in voice_catalog()}:
        raise APIError(404, "not_found", "Ses bulunamadı.")
    if body.enabled is False and settings.get("default_voice") == vid:
        raise APIError(400, "default_voice", "Varsayılan ses kapatılamaz; önce başka bir sesi varsayılan yapın.")
    cur = db.row("SELECT * FROM voice_config WHERE voice_id=?", (vid,)) or {"voice_id": vid, "enabled": 1,
                                                                             "display_name": None, "sort": 0}
    d = body.model_dump(exclude_unset=True)
    if "enabled" in d:
        d["enabled"] = int(d["enabled"])
    if d.get("display_name") == "":
        d["display_name"] = None
    cur.update(d)
    db.execute("INSERT INTO voice_config(voice_id, enabled, display_name, sort) VALUES(?,?,?,?) "
               "ON CONFLICT(voice_id) DO UPDATE SET enabled=excluded.enabled, display_name=excluded.display_name, "
               "sort=excluded.sort", (vid, cur["enabled"], cur["display_name"], cur["sort"]))
    audit(user.as_audit(), "voice_updated", vid, d, client_ip(request))
    return {"voices": voice_catalog()}


# ---------------------------------------------------------------------------- ayarlar

@router.get("/settings")
def get_settings(user: User = Depends(current_user)):
    return {"settings": settings.all(), "env": {"public_api_url": config.PUBLIC_API_URL,
                                                "panel_hosts": sorted(config.PANEL_HOSTS),
                                                "api_hosts": sorted(config.API_HOSTS), "timezone": config.TIMEZONE}}


@router.patch("/settings")
def patch_settings(body: dict, request: Request, user: User = Depends(admin_only)):
    try:
        if "default_voice" in body:
            v = {x["id"]: x for x in voice_catalog()}.get(body["default_voice"])
            if not v or not v["enabled"]:
                raise ValueError("Varsayılan ses etkin bir ses olmalı")
        changed = settings.update(body)
    except ValueError as e:
        raise APIError(400, "invalid_setting", str(e))
    audit(user.as_audit(), "settings_updated", "", changed, client_ip(request))
    return {"settings": settings.all()}


@router.post("/maintenance/cleanup")
def run_cleanup(user: User = Depends(admin_only)):
    return cleanup()


@router.get("/backup")
def backup(request: Request, user: User = Depends(admin_only)):
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dst = sqlite3.connect(tmp)
    db.connect().backup(dst)
    dst.close()
    audit(user.as_audit(), "backup_downloaded", ip=client_ip(request))
    name = f"tini-yedek-{time.strftime('%Y%m%d-%H%M')}.db"
    return FileResponse(tmp, filename=name, media_type="application/octet-stream",
                        background=BackgroundTask(os.unlink, tmp))


@router.get("/system")
def system(user: User = Depends(current_user)):
    db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    return {"version": config.VERSION, "engine": engine.health(max_age=0), "db_bytes": db_size,
            "disk": _disk(), "workers": settings.get("worker_concurrency"), "running": sorted(runner.running),
            "timezone": config.TIMEZONE}


# ---------------------------------------------------------------------------- kullanıcılar

class UserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: Optional[str] = Field(None, max_length=254)
    name: Optional[str] = Field(None, max_length=80)
    password: Optional[str] = Field(None, max_length=256)
    role: Optional[Literal["admin", "viewer"]] = None
    disabled: Optional[bool] = None


def _user_out(u):
    return {"id": u["id"], "email": u.get("email") or u["username"], "name": u.get("name") or "", "role": u["role"],
            "disabled": bool(u["disabled"]), "created_at": iso(u["created_at"]),
            "last_login_at": iso(u.get("last_login_at"))}


def _clean_email(email: str, exclude_id: str | None = None) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise APIError(400, "bad_email", "Geçerli bir e-posta adresi yazın.")
    if db.row("SELECT id FROM users WHERE (email=? COLLATE NOCASE OR username=?) AND id<>?",
              (email, email, exclude_id or "")):
        raise APIError(409, "exists", "Bu e-posta başka bir kullanıcıda kayıtlı.")
    return email


@router.get("/users")
def users(user: User = Depends(current_user)):
    return {"users": [_user_out(u) for u in db.rows("SELECT * FROM users ORDER BY created_at")]}


@router.post("/users")
def create_user(body: UserIn, request: Request, user: User = Depends(admin_only)):
    if not body.email or not body.password:
        raise APIError(400, "missing", "E-posta ve şifre gerekli.")
    email = _clean_email(body.email)
    if problem := security.password_problems(body.password):
        raise APIError(400, "weak_password", problem)
    u = {"id": security.new_id("usr", 12), "username": email, "email": email, "name": (body.name or "").strip(),
         "password_hash": security.hash_password(body.password), "role": body.role or "admin", "disabled": 0,
         "created_at": now(), "password_changed_at": now()}
    db.insert("users", u)
    audit(user.as_audit(), "user_created", u["id"], {"email": email, "role": u["role"]}, client_ip(request))
    return _user_out(u)


@router.patch("/users/{uid}")
def update_user(uid: str, body: UserIn, request: Request, user: User = Depends(admin_only)):
    u = db.row("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise APIError(404, "not_found", "Kullanıcı bulunamadı.")
    d = {}
    if body.email is not None and body.email.strip().lower() != (u.get("email") or ""):
        d["email"] = d["username"] = _clean_email(body.email, uid)
    if body.name is not None:
        d["name"] = body.name.strip()
    if body.password:
        if problem := security.password_problems(body.password):
            raise APIError(400, "weak_password", problem)
        d["password_hash"] = security.hash_password(body.password)
        d["password_changed_at"] = now()
    if body.role:
        d["role"] = body.role
    if body.disabled is not None:
        d["disabled"] = int(body.disabled)
    if uid == user.id and (d.get("disabled") or d.get("role") == "viewer"):
        raise APIError(400, "self", "Kendi hesabınızı kapatamaz ya da yetkisini düşüremezsiniz.")
    if (d.get("role") == "viewer" or d.get("disabled")) and u["role"] == "admin":
        if db.val("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND id<>?", (uid,), 0) == 0:
            raise APIError(400, "last_admin", "En az bir etkin yönetici kalmalı.")
    db.update("users", uid, d)
    if "password_hash" in d or d.get("disabled") or "email" in d:
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    audit(user.as_audit(), "user_updated", uid,
          {k: v for k, v in d.items() if k not in ("password_hash", "username")}, client_ip(request))
    return _user_out(db.row("SELECT * FROM users WHERE id=?", (uid,)))


@router.delete("/users/{uid}")
def delete_user(uid: str, request: Request, user: User = Depends(admin_only)):
    if uid == user.id:
        raise APIError(400, "self", "Kendi hesabınızı silemezsiniz.")
    u = db.row("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise APIError(404, "not_found", "Kullanıcı bulunamadı.")
    if u["role"] == "admin" and db.val("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND id<>?",
                                       (uid,), 0) == 0:
        raise APIError(400, "last_admin", "En az bir etkin yönetici kalmalı.")
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    audit(user.as_audit(), "user_deleted", uid, {"email": u.get("email") or u["username"]}, client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------- hazır ayarlar

class PresetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Optional[str] = Field(None, max_length=40)
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    description: Optional[str] = Field(None, max_length=300)
    params: Optional[dict] = None
    sort: Optional[int] = None


@router.get("/presets")
def list_presets(user: User = Depends(current_user)):
    from . import presets

    used = {}
    for r in db.rows("SELECT default_preset p, COUNT(*) n FROM projects WHERE default_preset IS NOT NULL "
                     "GROUP BY default_preset"):
        used[r["p"]] = r["n"]
    return {"presets": [p | {"projects_using": used.get(p["id"], 0)} for p in presets.all_presets()]}


@router.post("/presets")
def create_preset(body: PresetIn, request: Request, user: User = Depends(admin_only)):
    from . import presets

    pid = (body.id or "").strip().lower()
    if not presets.ID_RE.match(pid):
        raise APIError(400, "bad_id", "Kimlik 2-40 karakter olmalı; yalnız küçük harf, rakam, - ve _ (ör. haber-kisa).")
    if presets.get(pid):
        raise APIError(409, "exists", "Bu kimlikte bir hazır ayar zaten var.")
    if not body.name:
        raise APIError(400, "name_required", "Ad gerekli.")
    params = presets.validate_params(body.params or {}, {v["id"] for v in voice_catalog()})
    sort = db.val("SELECT COALESCE(MAX(sort),0)+1 FROM presets", (), 0)
    db.insert("presets", {"id": pid, "name": body.name.strip(), "description": (body.description or "").strip(),
                          "params": db.jdump(params), "sort": sort, "builtin": 0, "created_at": now(),
                          "updated_at": now()})
    audit(user.as_audit(), "preset_created", pid, params, client_ip(request))
    return presets.get(pid)


@router.patch("/presets/{pid}")
def update_preset(pid: str, body: PresetIn, request: Request, user: User = Depends(admin_only)):
    from . import presets

    if not presets.get(pid):
        raise APIError(404, "not_found", "Hazır ayar bulunamadı.")
    d = {}
    if body.name is not None:
        d["name"] = body.name.strip()
    if body.description is not None:
        d["description"] = body.description.strip()
    if body.params is not None:
        d["params"] = db.jdump(presets.validate_params(body.params, {v["id"] for v in voice_catalog()}))
    if body.sort is not None:
        d["sort"] = body.sort
    d["updated_at"] = now()
    db.update("presets", pid, d)
    clear_key_cache()
    audit(user.as_audit(), "preset_updated", pid, {k: v for k, v in d.items() if k != "updated_at"},
          client_ip(request))
    return presets.get(pid)


@router.delete("/presets/{pid}")
def delete_preset(pid: str, request: Request, user: User = Depends(admin_only)):
    from . import presets

    if not presets.get(pid):
        raise APIError(404, "not_found", "Hazır ayar bulunamadı.")
    db.execute("UPDATE projects SET default_preset=NULL WHERE default_preset=?", (pid,))
    db.execute("DELETE FROM presets WHERE id=?", (pid,))
    clear_key_cache()
    audit(user.as_audit(), "preset_deleted", pid, ip=client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------- denetim kaydı

@router.get("/audit")
def audit_log(user: User = Depends(current_user), before: Optional[int] = None, limit: int = 50):
    limit = max(1, min(limit, 200))
    rows = db.rows("SELECT * FROM audit_log WHERE (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                   (before, before, limit))
    for r in rows:
        r["ts"] = iso(r["ts"])
        r["details"] = db.jload(r["details"], {})
    return {"items": rows, "next": rows[-1]["id"] if len(rows) == limit else None}
