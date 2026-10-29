"""Einstellungen des CPU-Audio-Sidecars."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class AudioSettings:
    host: str = "127.0.0.1"
    port: int = 30003
    models_dir: Path = Path("/mnt/models/audio")
    threads: int = 8
    preload: str = "whisper-small"
    model_cache: int = 1
    max_queue: int = 8
    stt_timeout_s: int = 1800
    tts_timeout_s: int = 120
    max_upload_mb: int = 100
    chatterbox_base: str = "http://127.0.0.1:30004"
    chatterbox_timeout_s: int = 120


def _wert(env: Mapping[str, str], name: str, default: str) -> str:
    wert = env.get(f"AUDIO_{name}")
    if wert is None or wert == "":
        return default
    return wert


def load_settings(env: Mapping[str, str] | None = None) -> AudioSettings:
    """Liest ``AUDIO_*``; ``AUDIO_PRELOAD=""`` schaltet das Vorladen ab."""
    quelle = os.environ if env is None else env
    preload = quelle.get("AUDIO_PRELOAD")
    settings = AudioSettings(
        host=_wert(quelle, "HOST", "127.0.0.1"),
        port=int(_wert(quelle, "PORT", "30003")),
        models_dir=Path(_wert(quelle, "MODELS_DIR", "/mnt/models/audio")),
        threads=int(_wert(quelle, "THREADS", "8")),
        preload="whisper-small" if preload is None else preload,
        model_cache=int(_wert(quelle, "MODEL_CACHE", "1")),
        max_queue=int(_wert(quelle, "MAX_QUEUE", "8")),
        stt_timeout_s=int(_wert(quelle, "STT_TIMEOUT", "1800")),
        tts_timeout_s=int(_wert(quelle, "TTS_TIMEOUT", "120")),
        max_upload_mb=int(_wert(quelle, "MAX_UPLOAD_MB", "100")),
        chatterbox_base=_wert(quelle, "CHATTERBOX_BASE",
                             "http://127.0.0.1:30004"),
        chatterbox_timeout_s=int(
            _wert(quelle, "CHATTERBOX_TIMEOUT", "120")),
    )
    for feld in ("port", "threads", "model_cache", "max_queue",
                 "stt_timeout_s", "tts_timeout_s", "max_upload_mb",
                 "chatterbox_timeout_s"):
        if getattr(settings, feld) <= 0:
            raise ValueError(f"AUDIO_{feld.upper()} muss positiv sein: "
                             f"{getattr(settings, feld)}")
    if not settings.host.strip():
        raise ValueError("AUDIO_HOST darf nicht leer sein")
    if not settings.chatterbox_base.strip():
        raise ValueError("AUDIO_CHATTERBOX_BASE darf nicht leer sein")
    return settings
