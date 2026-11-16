"""SQLite (WAL) veri katmanı: iş parçacığı başına bağlantı, sıralı göçler, küçük yardımcılar."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_local = threading.local()

MIGRATIONS = [
    # 1: ilk şema
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER
    );
    CREATE TABLE users (
        id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'admin', disabled INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, last_login_at INTEGER, password_changed_at INTEGER
    );
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, last_seen_at INTEGER, ip TEXT, user_agent TEXT
    );
    CREATE TABLE projects (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active', is_system INTEGER NOT NULL DEFAULT 0,
        default_voice TEXT, allowed_voices TEXT NOT NULL DEFAULT '[]',
        rate_limit_rpm INTEGER NOT NULL DEFAULT 60, max_concurrency INTEGER NOT NULL DEFAULT 2,
        max_chars_per_request INTEGER NOT NULL DEFAULT 5000,
        daily_char_quota INTEGER NOT NULL DEFAULT 0, monthly_char_quota INTEGER NOT NULL DEFAULT 0,
        retention_days INTEGER, store_text INTEGER NOT NULL DEFAULT 1,
        webhook_url TEXT NOT NULL DEFAULT '', webhook_secret TEXT NOT NULL DEFAULT '',
        ip_allowlist TEXT NOT NULL DEFAULT '', allowed_origins TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    );
    CREATE TABLE api_keys (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL, prefix TEXT NOT NULL, last4 TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
        scopes TEXT NOT NULL, created_at INTEGER NOT NULL, created_by TEXT, expires_at INTEGER,
        revoked_at INTEGER, last_used_at INTEGER, last_used_ip TEXT, use_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX ix_keys_project ON api_keys(project_id);
    CREATE TABLE generations (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, key_id TEXT, source TEXT NOT NULL, mode TEXT NOT NULL,
        status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        not_before INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
        text TEXT, text_hash TEXT, chars INTEGER NOT NULL, voice TEXT NOT NULL, model TEXT, params TEXT NOT NULL,
        format TEXT NOT NULL, reference_id TEXT, metadata TEXT, webhook_url TEXT, idempotency_key TEXT,
        audio_path TEXT, audio_size INTEGER, duration REAL, sample_rate INTEGER, seed INTEGER,
        spoken_text TEXT, words TEXT, peaks TEXT, error_code TEXT, error_message TEXT,
        created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER, processing_ms INTEGER,
        keep INTEGER NOT NULL DEFAULT 0, audio_deleted_at INTEGER, client_ip TEXT
    );
    CREATE INDEX ix_gen_project_created ON generations(project_id, created_at);
    CREATE INDEX ix_gen_queue ON generations(status, priority, created_at);
    CREATE INDEX ix_gen_created ON generations(created_at);
    CREATE INDEX ix_gen_ref ON generations(reference_id);
    CREATE UNIQUE INDEX ux_gen_idem ON generations(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
    CREATE TABLE webhook_deliveries (
        id TEXT PRIMARY KEY, generation_id TEXT, project_id TEXT NOT NULL, url TEXT NOT NULL, event TEXT NOT NULL,
        payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL, last_status_code INTEGER, last_error TEXT,
        created_at INTEGER NOT NULL, delivered_at INTEGER
    );
    CREATE INDEX ix_wh_due ON webhook_deliveries(status, next_attempt_at);
    CREATE INDEX ix_wh_gen ON webhook_deliveries(generation_id);
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, user_id TEXT, username TEXT,
        action TEXT NOT NULL, target TEXT, details TEXT, ip TEXT
    );
    CREATE INDEX ix_audit_ts ON audit_log(ts);
    CREATE TABLE voice_config (
        voice_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, display_name TEXT, sort INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 2: e-posta ile giriş, hazır ayarlar, proje varsayılan hazır ayarı
    """
    ALTER TABLE users ADD COLUMN email TEXT;
    ALTER TABLE users ADD COLUMN name TEXT;
    CREATE UNIQUE INDEX ux_users_email ON users(email COLLATE NOCASE) WHERE email IS NOT NULL;
    CREATE TABLE presets (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', params TEXT NOT NULL,
        sort INTEGER NOT NULL DEFAULT 0, builtin INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    ALTER TABLE projects ADD COLUMN default_preset TEXT
    """,
]


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA temp_store=MEMORY")
        _local.conn = conn
    return conn


def migrate():
    conn = connect()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={i}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _split(sql):
    return [s.strip() for s in sql.split(";") if s.strip()]


def rows(sql, params=()):
    return [dict(r) for r in connect().execute(sql, params).fetchall()]


def row(sql, params=()):
    r = connect().execute(sql, params).fetchone()
    return dict(r) if r else None


def val(sql, params=(), default=None):
    r = connect().execute(sql, params).fetchone()
    return default if r is None or r[0] is None else r[0]


def execute(sql, params=()):
    return connect().execute(sql, params)


@contextmanager
def tx():
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def insert(table, data: dict):
    cols = ",".join(data)
    marks = ",".join("?" for _ in data)
    connect().execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))


def update(table, key, data: dict, key_col="id"):
    if not data:
        return
    sets = ",".join(f"{k}=?" for k in data)
    connect().execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?", (*data.values(), key))


def jdump(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def jload(v, default=None):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except Exception:
        return default
