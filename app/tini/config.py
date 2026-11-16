"""Ortam değişkenleri. Çalışırken değişebilen her şey veritabanındaki ayarlardadır (settings.py)."""
import os
from pathlib import Path


def env(name, default=""):
    return os.environ.get(name, default).strip()


VERSION = "2.1.0"
DATA_DIR = Path(env("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "tini.db"
AUDIO_DIR = DATA_DIR / "audio"
BACKUP_DIR = DATA_DIR / "backups"

ENGINE_URL = env("ENGINE_URL", "http://tini-engine:9000").rstrip("/")
ENGINE_TOKEN = env("ENGINE_TOKEN")

# Tüm imzaların (API anahtarı özetleri, dosya bağlantıları, webhook) kökü. Değiştirilirse mevcut
# API anahtarları ve dosya bağlantıları geçersiz olur.
SECRET_KEY = env("SECRET_KEY")

# İlk açılışta oluşturulacak yönetici (kullanıcı tablosu boşsa). Giriş e-posta ile yapılır.
ADMIN_EMAIL = env("ADMIN_EMAIL").lower()
ADMIN_NAME = env("ADMIN_NAME", "Yönetici")
ADMIN_PASSWORD = env("ADMIN_PASSWORD")

# Domain ayrımı (isteğe bağlı): boşsa her şey her domainde çalışır
PANEL_HOSTS = {h.strip().lower() for h in env("PANEL_HOSTS").split(",") if h.strip()}
API_HOSTS = {h.strip().lower() for h in env("API_HOSTS").split(",") if h.strip()}
PUBLIC_API_URL = env("PUBLIC_API_URL").rstrip("/")

TIMEZONE = env("TZ", "Europe/Istanbul")
LOG_LEVEL = env("LOG_LEVEL", "INFO")
COOKIE_SECURE = env("COOKIE_SECURE", "auto")  # auto | true | false
MAX_BODY_BYTES = int(env("MAX_BODY_BYTES", str(2 * 1024 * 1024)))
