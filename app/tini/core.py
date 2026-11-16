"""Ortak parçalar: hata biçimi, zaman yardımcıları, engine istemcisi, ses katalogu, kayıt (audit)."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import config, db, settings

log = logging.getLogger("tini")
TZ = ZoneInfo(config.TIMEZONE)


# ------------------------------------------------------------------ hatalar

class APIError(Exception):
    """Tüm API hataları tek biçimde döner: {"error": {"code", "message", ...}}"""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.headers, self.extra = status, code, message, headers or {}, extra


# ------------------------------------------------------------------ zaman

def now() -> int:
    return int(time.time())


def day_start(ts: float | None = None) -> int:
    d = datetime.fromtimestamp(ts or time.time(), TZ)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def month_start(ts: float | None = None) -> int:
    d = datetime.fromtimestamp(ts or time.time(), TZ)
    return int(d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def next_day(ts: float | None = None) -> int:
    return int((datetime.fromtimestamp(day_start(ts), TZ) + timedelta(days=1)).timestamp())


def next_month(ts: float | None = None) -> int:
    d = datetime.fromtimestamp(month_start(ts), TZ) + timedelta(days=32)
    return int(d.replace(day=1).timestamp())


def iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ engine istemcisi

class EngineClient:
    def __init__(self):
        self.http = httpx.Client(base_url=config.ENGINE_URL, timeout=httpx.Timeout(900.0, connect=5.0),
                                 headers={"x-engine-token": config.ENGINE_TOKEN})
        self._health = {"status": "unknown"}
        self._health_at = 0.0
        self._voices = None
        self._voices_at = 0.0
        self._lock = threading.Lock()

    def health(self, max_age: float = 3.0) -> dict:
        if time.time() - self._health_at < max_age:
            return self._health
        try:
            r = self.http.get("/health", timeout=4)
            self._health = r.json()
        except Exception as e:
            self._health = {"status": "unreachable", "error": type(e).__name__}
        self._health_at = time.time()
        return self._health

    def voices(self, max_age: float = 30.0) -> dict:
        with self._lock:
            if self._voices is not None and time.time() - self._voices_at < max_age:
                return self._voices
            try:
                r = self.http.get("/voices", timeout=5)
                r.raise_for_status()
                self._voices, self._voices_at = r.json(), time.time()
            except Exception:
                if self._voices is None:
                    return {"voices": [], "styles": [], "sample_rates": [48000, 24000, 16000, 8000],
                            "formats": ["mp3", "wav", "ogg", "flac"]}
            return self._voices

    def normalize(self, text: str) -> str:
        r = self.http.post("/normalize", json={"text": text}, timeout=30)
        r.raise_for_status()
        return r.json()["spoken"]

    def synthesize(self, payload: dict) -> httpx.Response:
        chars = len(payload.get("text", ""))
        return self.http.post("/synthesize", json=payload, timeout=httpx.Timeout(120 + chars * 0.25, connect=5))


engine = EngineClient()


# ------------------------------------------------------------------ ses katalogu (engine + panel ayarları)

def voice_catalog() -> list[dict]:
    data = engine.voices()
    cfg = {r["voice_id"]: r for r in db.rows("SELECT * FROM voice_config")}
    default = settings.get("default_voice")
    out = []
    for v in data.get("voices", []):
        c = cfg.get(v["id"], {})
        out.append({
            "id": v["id"],
            "name": c.get("display_name") or v["name"],
            "model": v.get("model"),
            "model_version": v.get("model_version"),
            "description": v.get("description", ""),
            "language": v.get("language", "tr"),
            "styles": v.get("styles", ["neutral"]),
            "license": v.get("license"),
            "attribution": v.get("attribution"),
            "loaded": bool(v.get("available")),
            "enabled": bool(c.get("enabled", 1)),
            "default": v["id"] == default,
            "sort": c.get("sort", 0),
        })
    out.sort(key=lambda v: (not v["default"], v["sort"], v["name"]))
    return out


def styles_catalog() -> list[dict]:
    return engine.voices().get("styles", [])


# ------------------------------------------------------------------ denetim kaydı

def audit(user: dict | None, action: str, target: str = "", details: dict | None = None, ip: str = ""):
    try:
        db.insert("audit_log", {
            "ts": now(), "user_id": user["id"] if user else None, "username": user["username"] if user else None,
            "action": action, "target": target, "details": db.jdump(details or {}), "ip": ip,
        })
    except Exception:
        log.exception("audit yazılamadı")
