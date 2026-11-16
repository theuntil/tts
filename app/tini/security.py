"""Parola özetleri, API anahtarları, oturum belirteçleri, imzalı dosya bağlantıları, IP denetimi."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import string
import time

from . import config

ALPHABET = string.ascii_letters + string.digits
KEY_PREFIX = "tini_"


def _secret() -> bytes:
    if not config.SECRET_KEY:
        raise RuntimeError("SECRET_KEY ayarlanmamış")
    return config.SECRET_KEY.encode()


def rand(n: int = 24) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def new_id(prefix: str, n: int = 20) -> str:
    return f"{prefix}_{rand(n)}"


# ------------------------------------------------------------------ parolalar (scrypt, ek bağımlılık yok)

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2**15, 8, 1
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def password_problems(password: str) -> str | None:
    if len(password) < 10:
        return "Şifre en az 10 karakter olmalı"
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        return "Şifre büyük harf, küçük harf ve rakam içermeli"
    return None


# ------------------------------------------------------------------ API anahtarları

def new_api_key() -> str:
    return KEY_PREFIX + rand(40)


def key_hash(key: str) -> str:
    return hmac.new(_secret(), f"apikey:{key}".encode(), hashlib.sha256).hexdigest()


def looks_like_key(key: str) -> bool:
    return key.startswith(KEY_PREFIX) and 20 <= len(key) <= 100 and all(c in ALPHABET or c == "_" for c in key)


# ------------------------------------------------------------------ oturumlar

def token_hash(token: str) -> str:
    return hashlib.sha256(f"session:{token}".encode()).hexdigest()


# ------------------------------------------------------------------ imzalı dosya bağlantıları

def file_sig(gen_id: str, exp: int = 0) -> str:
    raw = hmac.new(_secret(), f"file:{gen_id}:{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw[:18]).decode().rstrip("=")


def check_file_sig(gen_id: str, sig: str, exp: int = 0) -> bool:
    if exp and exp < time.time():
        return False
    return hmac.compare_digest(file_sig(gen_id, exp), sig or "")


# ------------------------------------------------------------------ webhook imzası

def webhook_signature(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


# ------------------------------------------------------------------ IP listeleri

def parse_networks(text: str):
    nets = []
    for part in (text or "").replace("\n", ",").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def ip_allowed(ip: str, allowlist: str) -> bool:
    nets = parse_networks(allowlist)
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def is_private_host_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified
