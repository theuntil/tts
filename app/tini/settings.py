"""Panelden değiştirilebilen sistem ayarları (veritabanında saklanır, bellekte önbelleklenir)."""
from __future__ import annotations

import json
import threading
import time

from . import db

# anahtar: (varsayılan, tür, alt sınır, üst sınır)
SCHEMA = {
    "public_api_url": ("", str, None, None),
    "default_voice": ("ema", str, None, None),
    # saklama
    "auto_delete_enabled": (True, bool, None, None),
    "retention_days": (7, int, 1, 3650),
    "log_retention_days": (90, int, 0, 3650),  # 0 = istek kayıtları silinmez
    "file_url_ttl_hours": (0, int, 0, 8760),  # 0 = bağlantı ses silinene kadar geçerli
    # kuyruk
    "sync_wait_seconds": (60, int, 5, 300),
    "max_queue": (300, int, 1, 100000),
    "worker_concurrency": (8, int, 1, 16),
    "max_pending_per_project": (200, int, 1, 100000),
    "global_max_chars": (60000, int, 100, 200000),
    # yeni projeler için varsayılan sınırlar
    "default_rate_limit_rpm": (60, int, 1, 100000),
    "default_max_concurrency": (6, int, 1, 100),
    "default_max_chars": (5000, int, 10, 200000),
    # webhook
    "webhook_max_attempts": (6, int, 1, 20),
    "webhook_timeout_seconds": (10, int, 2, 60),
    "webhook_allow_private": (False, bool, None, None),
    # güvenlik
    "session_hours": (12, int, 1, 720),
    "login_max_attempts": (8, int, 3, 100),
    "docs_public": (True, bool, None, None),
    # bakım
    "maintenance_mode": (False, bool, None, None),
    "maintenance_message": ("Sistem bakımda, lütfen biraz sonra tekrar deneyin.", str, None, None),
}

_cache: dict | None = None
_lock = threading.Lock()


def all() -> dict:  # noqa: A001
    global _cache
    if _cache is None:
        with _lock:
            stored = {r["key"]: json.loads(r["value"]) for r in db.rows("SELECT key, value FROM settings")}
            _cache = {k: stored.get(k, d[0]) for k, d in SCHEMA.items()}
    return dict(_cache)


def get(key):
    return all()[key]


def validate(changes: dict) -> dict:
    clean = {}
    for k, v in changes.items():
        if k not in SCHEMA:
            raise ValueError(f"Bilinmeyen ayar: {k}")
        _, typ, lo, hi = SCHEMA[k]
        if typ is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} doğru/yanlış olmalı")
        elif typ is int:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
                raise ValueError(f"{k} tam sayı olmalı")
            v = int(v)
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                raise ValueError(f"{k} {lo} ile {hi} arasında olmalı")
        else:
            if not isinstance(v, str) or len(v) > 2000:
                raise ValueError(f"{k} metin olmalı")
            v = v.strip()
            if k == "public_api_url":
                v = v.rstrip("/")
                if v and not v.startswith(("https://", "http://")):
                    raise ValueError("API adresi http:// ya da https:// ile başlamalı")
        clean[k] = v
    return clean


def update(changes: dict) -> dict:
    global _cache
    clean = validate(changes)
    now = int(time.time())
    with db.tx() as conn:
        for k, v in clean.items():
            conn.execute("INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (k, json.dumps(v), now))
    with _lock:
        _cache = None
    return clean
