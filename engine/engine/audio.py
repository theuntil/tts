"""Ses son işlemleri ve dosya biçimleri."""
import io

import numpy as np
import soundfile as sf

FORMATS = {
    # ad: (soundfile biçimi, alt tür, mime, uzantı)
    "mp3": ("MP3", "MPEG_LAYER_III", "audio/mpeg", "mp3"),
    "wav": ("WAV", "PCM_16", "audio/wav", "wav"),
    "flac": ("FLAC", "PCM_16", "audio/flac", "flac"),
    "ogg": ("OGG", "VORBIS", "audio/ogg", "ogg"),
}


def postprocess(wav: np.ndarray, sr: int, *, pitch: float = 0.0, volume: float = 0.0,
                loudnorm: bool = True) -> np.ndarray:
    wav = np.asarray(wav, dtype=np.float32)
    if wav.size == 0:
        return wav
    if abs(pitch) >= 0.05:
        import librosa  # yalnız gerektiğinde yüklenir

        wav = librosa.effects.pitch_shift(wav, sr=sr, n_steps=float(pitch), res_type="soxr_hq")
    if loudnorm:
        # RMS'i yaklaşık -18 dBFS'e çek, tepeyi -1 dBFS'te tut (yayın/podcast için dengeli seviye)
        rms = float(np.sqrt(np.mean(wav ** 2))) or 1e-9
        wav = wav * (10 ** (-18 / 20) / rms)
        peak = float(np.max(np.abs(wav))) or 1e-9
        limit = 10 ** (-1 / 20)
        if peak > limit:
            wav = wav * (limit / peak)
    if volume:
        wav = wav * (10 ** (volume / 20))
    return np.clip(wav, -1.0, 1.0).astype(np.float32)


def encode(wav: np.ndarray, sr: int, fmt: str) -> bytes:
    kind, subtype, _, _ = FORMATS[fmt]
    buf = io.BytesIO()
    sf.write(buf, wav, sr, format=kind, subtype=subtype)
    return buf.getvalue()


def peaks(wav: np.ndarray, n: int = 960) -> list:
    """Dalga formu çizimi için n adet tepe değeri (0..1)."""
    if wav.size == 0:
        return []
    n = max(1, min(n, wav.size))
    edges = np.linspace(0, wav.size, n + 1, dtype=np.int64)
    out = np.maximum.reduceat(np.abs(wav), edges[:-1])
    top = float(out.max()) or 1.0
    return [round(float(v) / top, 3) for v in out]
