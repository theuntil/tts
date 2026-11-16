"""Tını Engine: yalnız ses üreten dahili servis. Dışarıya açılmaz; yalnız app servisi konuşur.

Uç noktalar (hepsi X-Engine-Token ister, /health hariç):
  GET  /health       durum
  GET  /voices       yüklü sesler ve yetenekleri
  POST /normalize    trnorm okunuş biçimi
  POST /synthesize   ses üretir; JSON içinde base64 ses + süre + kelime zamanları
"""
from __future__ import annotations

import base64
import hmac
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import audio
from .models import RATES, STYLES, VOICES, engine

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
TOKEN = os.environ.get("ENGINE_TOKEN", "")
VERSION = "2.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=engine.load, name="model-load", daemon=True).start()
    yield


app = FastAPI(title="Tını Engine", version=VERSION, docs_url=None, redoc_url=None, lifespan=lifespan)


def auth(request: Request):
    if TOKEN and not hmac.compare_digest(request.headers.get("x-engine-token", ""), TOKEN):
        raise HTTPException(status_code=401, detail="engine token")


class SynthIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(..., min_length=1, max_length=200000)
    voice: str = "ema"
    style: Literal[tuple(STYLES)] = "neutral"  # type: ignore[valid-type]
    speed: float = Field(1.0, ge=0.25, le=4.0)
    pause_scale: float = Field(1.0, ge=0, le=5)
    paragraph_pause: float = Field(0.7, ge=0, le=5)
    pitch: float = Field(0.0, ge=-12, le=12)
    steps: int = Field(16, ge=2, le=64)
    cfg: float = Field(2.0, ge=1, le=6)
    seed: Optional[int] = Field(0, ge=0, le=2**31 - 1)
    volume: float = Field(0.0, ge=-30, le=20)
    loudnorm: bool = True
    sample_rate: int = 48000
    format: Literal["mp3", "wav", "ogg", "flac"] = "mp3"
    include_words: bool = True


class NormIn(BaseModel):
    text: str = Field(..., max_length=200000)


@app.get("/health")
def health():
    body = {"status": engine.status, "voices": list(engine.voices), "version": VERSION,
            "busy": engine.active > 0, "active": engine.active, "served": engine.served,
            "uptime": round(time.time() - engine.started)}
    if engine.error:
        body["error"] = engine.error
    return JSONResponse(body, status_code=200 if engine.ready else 503)


@app.get("/voices", dependencies=[Depends(auth)])
def voices():
    return {"voices": [v | {"available": v["id"] in engine.voices} for v in VOICES.values()],
            "styles": [{"id": k, "label": v} for k, v in STYLES.items()], "sample_rates": list(RATES),
            "formats": list(audio.FORMATS)}


@app.post("/normalize", dependencies=[Depends(auth)])
def normalize(body: NormIn):
    return {"spoken": engine.spoken(body.text)}


@app.post("/synthesize", dependencies=[Depends(auth)])
def synthesize(body: SynthIn):
    if not engine.ready:
        raise HTTPException(status_code=503, detail=engine.error or "loading")
    if body.voice not in engine.voices:
        raise HTTPException(status_code=400, detail=f"voice_unavailable:{body.voice}")
    if body.sample_rate not in RATES:
        raise HTTPException(status_code=400, detail="bad_sample_rate")
    r = engine.synthesize(body.text, voice=body.voice, style=body.style, speed=body.speed, steps=body.steps,
                          cfg=body.cfg, seed=body.seed, pause_scale=body.pause_scale,
                          paragraph_pause=body.paragraph_pause, sample_rate=body.sample_rate)
    if r.audio.size == 0:
        raise HTTPException(status_code=422, detail="nothing_to_read")
    wav = audio.postprocess(r.audio, r.sample_rate, pitch=body.pitch, volume=body.volume, loudnorm=body.loudnorm)
    data = audio.encode(wav, r.sample_rate, body.format)
    return {
        "audio_b64": base64.b64encode(data).decode(),
        "format": body.format,
        "mime": audio.FORMATS[body.format][2],
        "duration": round(len(wav) / r.sample_rate, 3),
        "sample_rate": r.sample_rate,
        "seed": r.seed,
        "gen_seconds": round(r.gen_seconds, 3),
        "spoken_text": r.spoken,
        "words": r.words if body.include_words else [],
        "peaks": audio.peaks(wav, 480),
        "voice": body.voice,
        "model": VOICES[body.voice]["model"],
        "model_version": VOICES[body.voice]["model_version"],
    }
