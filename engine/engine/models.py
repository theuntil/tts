"""Model katmanı: EMA Lightning (birincil) ve Ahenk Cep, trnorm metin ön ucuyla.

Yeni bir model eklemek için VOICES'a bir kayıt ve load() içine yükleme kodu eklemek yeterlidir;
dışarıdaki API sesleri yalnızca kimlikleriyle (ör. "ema") tanır.
"""
from __future__ import annotations

import contextlib
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import torch

log = logging.getLogger("engine.models")

RATES = (48000, 24000, 16000, 8000)
LETTERS_PER_SECOND = 18.0
PARAGRAPH = re.compile(r"\n\s*\n+")
GROUP_PIECES = max(1, int(os.environ.get("ENGINE_GROUP_PIECES", "3") or 3))  # ~30 sn ses

STYLES = {
    "neutral": "Nötr", "calm": "Sakin", "cheerful": "Neşeli", "empathetic": "Empatik",
    "sad": "Üzgün", "angry": "Kızgın", "surprised": "Şaşkın",
}

VOICES = {
    "ema": {
        "id": "ema",
        "name": "EMA Lightning",
        "model": "ema-lightning",
        "model_version": "1.1.2",
        "description": "Tek ses, çok hızlı ve doğru Türkçe okuma. Ağırlıklar Apache-2.0.",
        "language": "tr",
        "styles": ["neutral"],
        "license": "Apache-2.0",
        "attribution": "EMA Lightning, Canberk Aslan (Apache-2.0)",
    },
    "ahenk-cep": {
        "id": "ahenk-cep",
        "name": "Ahenk Cep",
        "model": "ahenk-cep",
        "model_version": "1.0",
        "description": "Tasarlanmış kadın sesi (f022), 7 üslup seçeneği (etkisi hafif). Ağırlıklar CC BY 4.0.",
        "language": "tr",
        "styles": list(STYLES),
        "license": "CC-BY-4.0",
        "attribution": "Ahenk (https://github.com/ozguradmin/ahenk-turkish-tts), CC BY 4.0; "
                       "Alania Turkish Synthetic Speech by PatientDesk AI (CC BY 4.0)",
    },
}


class Cancelled(Exception):
    pass


@dataclass
class Result:
    audio: np.ndarray
    sample_rate: int
    seed: int
    spoken: str
    words: list = field(default_factory=list)
    gen_seconds: float = 0.0


def _env_bool(name, default):
    v = os.environ.get(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on", "evet")


def _weight(local_dir: str, repo: str, name: str) -> str:
    if local_dir:
        p = Path(local_dir) / name
        if not p.is_file():
            raise FileNotFoundError(f"{local_dir} içinde {name} yok")
        return str(p)
    from huggingface_hub import hf_hub_download

    return hf_hub_download(repo, name, token=os.environ.get("HF_TOKEN") or None)


class Engine:
    def __init__(self):
        self.cep_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self.active = 0
        self.voices: dict = {}
        self.cep_model = None
        self.status = "loading"
        self.error: Optional[str] = None
        self.started = time.time()
        self.busy_since: Optional[float] = None
        self.served = 0

    def load(self):
        try:
            threads = int(os.environ.get("TORCH_THREADS", "0") or 0)
            if threads > 0:
                torch.set_num_threads(threads)
            from ema_lightning import EMA
            from ema_lightning.decoder import load_decoder
            from ema_lightning.frontend import Frontend
            from ema_lightning.model import load_acoustic

            ema_dir = os.environ.get("EMA_WEIGHTS_DIR", "")
            ema_repo = os.environ.get("EMA_REPO", "canberkkkkkk/ema-lightning")
            log.info("EMA Lightning yükleniyor (%s)", ema_dir or ema_repo)
            model = load_acoustic(_weight(ema_dir, ema_repo, "ema.pt"), "cpu")
            dec = load_decoder(_weight(ema_dir, ema_repo, "decoder.pt"), "cpu")
            self.voices["ema"] = EMA._from_parts(model, dec, self._frontend(Frontend, model), "cpu")

            if _env_bool("ENABLE_AHENK_CEP", True):
                try:
                    from ahenk_cep import load_student

                    cep_dir = os.environ.get("CEP_WEIGHTS_DIR", "")
                    cep_repo = os.environ.get("CEP_REPO", "ozgurguler/ahenk-cep-tr")
                    log.info("Ahenk Cep yükleniyor (%s)", cep_dir or cep_repo)
                    student = load_student(_weight(cep_dir, cep_repo, "student.pt"))
                    decoder = load_decoder(_weight(cep_dir, cep_repo, "decoder.pt"), "cpu")
                    self.cep_model = student
                    self.voices["ahenk-cep"] = EMA._from_parts(student, decoder, self._frontend(Frontend, student),
                                                               "cpu")
                except Exception as e:  # ikinci ses zorunlu değil
                    log.warning("Ahenk Cep yüklenemedi, yalnız EMA kullanılacak: %s", e)

            self.synthesize("Merhaba, sistem hazır.", voice="ema")  # ısınma + CPU toplu iş ölçümü
            self.status = "ok"
            log.info("Hazır. Sesler: %s, iş parçacığı: %d", ", ".join(self.voices), torch.get_num_threads())
        except Exception as e:
            self.status, self.error = "error", f"{type(e).__name__}: {e}"
            log.exception("Model yüklenemedi")

    @staticmethod
    def _frontend(Frontend, model):
        # trnorm okunuş biçimini zaten üretiyor; EMA'nın ön ucundan yalnız alfabe adımı kalsın
        fe = Frontend.__new__(Frontend)
        fe.vocab = frozenset(model.vocab)
        fe.spoken = lambda t: t
        return fe

    @property
    def ready(self):
        return self.status == "ok"

    @staticmethod
    def spoken(text: str) -> str:
        from trnorm import normalize

        parts = [p.strip() for p in PARAGRAPH.split(text.replace("\r\n", "\n")) if p.strip()]
        return "\n\n".join(normalize(" ".join(p.split())) for p in parts)

    def synthesize(self, text: str, *, voice: str = "ema", style: str = "neutral", speed: float = 1.0,
                   steps: int = 16, cfg: float = 2.0, seed: Optional[int] = 0, pause_scale: float = 1.0,
                   paragraph_pause: float = 0.7, sample_rate: int = 48000,
                   should_cancel: Optional[Callable[[], bool]] = None) -> Result:
        import ema_lightning.api as ema_api
        from ema_lightning.chunker import chunk

        if voice not in self.voices:
            raise ValueError(f"Ses yüklü değil: {voice}")
        if sample_rate not in RATES:
            raise ValueError(f"sample_rate şunlardan biri olmalı: {RATES}")
        engine = self.voices[voice]
        seed = random.SystemRandom().randrange(2**31) if seed is None else int(seed)
        spoken = self.spoken(text)
        paragraphs = [p for p in spoken.split("\n\n") if p.strip()]

        # EMA motoru (Playhead) aynı anda gelen istekleri kendi içinde toplu işler: kilit gerekmez.
        # Ahenk Cep'in üslup/adım ayarı modelin üzerinde tutulduğu için yalnız onun istekleri sıraya girer.
        lock = self.cep_lock if voice == "ahenk-cep" else contextlib.nullcontext()
        with lock:
            with self._count_lock:
                self.active += 1
                self.busy_since = self.busy_since or time.time()
            t0 = time.perf_counter()
            try:
                if voice == "ahenk-cep":
                    from ahenk_cep import STYLES as CEP_STYLES

                    self.cep_model.style = CEP_STYLES.index(style if style in STYLES else "neutral")
                    self.cep_model.steps, self.cep_model.cfg_scale = int(steps), float(cfg)

                # EMA._pieces ile aynı parçalama; duraklamalar burada ölçeklenir (genel durum değiştirilmez)
                pieces = []
                for k, para in enumerate(paragraphs):
                    s0 = (seed + 7919 * k) % 2**31
                    ps = [engine._engine.piece(piece, pause * pause_scale, (s0 * 1_000_003 + i) % 2**63)
                          for i, (piece, pause) in enumerate(chunk(engine._frontend(para), speed))]
                    if ps:
                        ps[-1].pause = paragraph_pause if k < len(paragraphs) - 1 else 0.0
                        pieces.extend(ps)

                # Uzun metin tek seferde sıraya girerse arkasından gelen kısa istekler hepsinin bitmesini
                # bekler. Parçaları küçük gruplar hâlinde gönderiyoruz; gruplar arasında başka isteklerin
                # cümleleri araya girer (kısa isteklerin gecikmesi uzun işlerden bağımsız kalır).
                chunks, words, produced, words_offset = [], [], 0, 0.0
                for g0 in range(0, len(pieces), GROUP_PIECES):
                    group = pieces[g0:g0 + GROUP_PIECES]
                    request = engine._playhead.submit(group, speed)
                    gen = engine._receive(request, sample_rate)
                    try:
                        for out in gen:
                            if should_cancel and should_cancel():
                                raise Cancelled()
                            chunks.append(out)
                            produced += len(out)
                    finally:
                        gen.close()
                    base = words_offset
                    words += [{"w": w.text, "s": round(w.start + base, 3), "e": round(w.end + base, 3)}
                              for w in ema_api._words(request.pieces)]
                    words_offset = produced / sample_rate
                audio = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)
                self.served += 1
                return Result(audio, sample_rate, seed, spoken, words, time.perf_counter() - t0)
            finally:
                with self._count_lock:
                    self.active -= 1
                    if not self.active:
                        self.busy_since = None


engine = Engine()
