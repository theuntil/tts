"""Hazır ayarlar: "haber", "hikaye" gibi adlandırılmış okuma ayarı kümeleri.

İstekte `preset` gönderilirse (ya da projenin varsayılan hazır ayarı varsa) değerleri uygulanır; istekte
açıkça yazılan alanlar hazır ayardaki değeri her zaman ezer. Hiçbiri zorunlu değildir.
"""
from __future__ import annotations

import re

from . import db
from .core import APIError, now

# alan: (tür, alt, üst)  — SpeechIn ile aynı sınırlar
FIELDS = {
    "voice": (str, None, None),
    "style": (str, None, None),
    "speed": (float, 0.5, 2.0),
    "pitch": (float, -4.0, 4.0),
    "pause_scale": (float, 0.0, 3.0),
    "paragraph_pause": (float, 0.0, 3.0),
    "volume": (float, -20.0, 10.0),
    "normalize_loudness": (bool, None, None),
    "format": (str, None, None),
    "sample_rate": (int, None, None),
}
STYLES = {"neutral", "calm", "cheerful", "empathetic", "sad", "angry", "surprised"}
FORMATS = {"mp3", "wav", "ogg", "flac"}
RATES = {48000, 24000, 16000, 8000}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")

DEFAULTS = [
    ("haber", "Haber", "Net ve akıcı haber okuması.",
     {"style": "neutral", "speed": 1.05, "pause_scale": 1.0, "paragraph_pause": 0.8, "pitch": 0.0}),
    ("hikaye", "Hikâye", "Sakin, daha uzun duraklamalı anlatım.",
     {"style": "calm", "speed": 0.92, "pause_scale": 1.4, "paragraph_pause": 1.1, "pitch": 0.0}),
    ("masal", "Masal", "Çocuklar için yavaş ve sıcak anlatım.",
     {"style": "cheerful", "speed": 0.88, "pause_scale": 1.5, "paragraph_pause": 1.2, "pitch": 0.5}),
    ("duyuru", "Duyuru", "Resmî ve ölçülü anons.",
     {"style": "neutral", "speed": 0.95, "pause_scale": 1.2, "paragraph_pause": 0.9, "pitch": -0.5}),
    ("sosyal-medya", "Sosyal medya", "Kısa, canlı ve tempolu.",
     {"style": "cheerful", "speed": 1.1, "pause_scale": 0.8, "paragraph_pause": 0.5, "pitch": 0.0}),
]


def seed():
    if db.val("SELECT COUNT(*) FROM presets", (), 0):
        return
    for i, (pid, name, desc, params) in enumerate(DEFAULTS):
        db.insert("presets", {"id": pid, "name": name, "description": desc, "params": db.jdump(params),
                              "sort": i, "builtin": 1, "created_at": now(), "updated_at": now()})


def validate_params(params: dict, known_voices: set | None = None) -> dict:
    if not isinstance(params, dict):
        raise APIError(400, "invalid_preset", "Hazır ayar değerleri bir nesne olmalı.")
    clean = {}
    for k, v in params.items():
        if v is None or v == "":
            continue
        if k not in FIELDS:
            raise APIError(400, "invalid_preset", f"Hazır ayarda kullanılamayan alan: {k}")
        typ, lo, hi = FIELDS[k]
        try:
            if typ is bool:
                if not isinstance(v, bool):
                    raise ValueError
            elif typ is int:
                v = int(v)
            elif typ is float:
                v = round(float(v), 3)
            else:
                v = str(v)
        except (TypeError, ValueError):
            raise APIError(400, "invalid_preset", f"{k} değeri geçersiz.")
        if lo is not None and not (lo <= v <= hi):
            raise APIError(400, "invalid_preset", f"{k} {lo} ile {hi} arasında olmalı.")
        if k == "style" and v not in STYLES:
            raise APIError(400, "invalid_preset", f"Bilinmeyen üslup: {v}")
        if k == "format" and v not in FORMATS:
            raise APIError(400, "invalid_preset", f"Bilinmeyen biçim: {v}")
        if k == "sample_rate" and v not in RATES:
            raise APIError(400, "invalid_preset", f"Geçersiz örnekleme hızı: {v}")
        if k == "voice" and known_voices is not None and v not in known_voices:
            raise APIError(400, "invalid_preset", f"Bilinmeyen ses: {v}")
        clean[k] = v
    return clean


def get(pid: str) -> dict | None:
    r = db.row("SELECT * FROM presets WHERE id=?", (pid,))
    if r:
        r["params"] = db.jload(r["params"], {}) or {}
    return r


def all_presets() -> list[dict]:
    rows = db.rows("SELECT * FROM presets ORDER BY sort, name")
    for r in rows:
        r["params"] = db.jload(r["params"], {}) or {}
    return rows


def public(p: dict) -> dict:
    return {"id": p["id"], "name": p["name"], "description": p["description"], "params": p["params"]}


def apply(project: dict, data: dict, explicit: set) -> dict:
    """İstek verisine hazır ayarı uygular. `explicit`: istekte açıkça gönderilen alanlar."""
    requested = data.get("preset")
    pid = requested or project.get("default_preset")
    if not pid:
        return data
    p = get(pid)
    if not p:
        if requested:
            raise APIError(400, "preset_not_found", f"'{pid}' adlı hazır ayar yok. GET /v1/presets ile listeleyin.")
        return data  # projenin varsayılanı silinmiş: sessizce yok say
    out = dict(data)
    for k, v in p["params"].items():
        if k not in explicit:
            out[k] = v
    out["preset"] = pid
    return out
