"""Einstellungen des Audio8-TTS-Workers."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Audio8Settings:
    host: str = "127.0.0.1"
    port: int = 30006
    model_dir: Path = Path("/mnt/models/audio/audio8")
    reference_audio: Path = Path(
        "/mnt/models/audio/audio8/reference/de_male.wav")
    reference_text: Path = Path(
        "/mnt/models/audio/audio8/reference/de_male.txt")
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    default_language: str = "German"
    max_new_tokens: int = 1024
    retry_max_new_tokens: int = 2000
    temperature: float = 0.8
    top_p: float = 0.95
    top_k: int = 50
    seed: int = 42


def _wert(env: Mapping[str, str], name: str, default: str) -> str:
    wert = env.get(f"AUDIO8_{name}")
    if wert is None or wert == "":
        return default
    return wert


def load_settings(env: Mapping[str, str] | None = None) -> Audio8Settings:
    """Liest ``AUDIO8_*`` und weist ungueltige Werte mit ``ValueError`` ab."""
    quelle = os.environ if env is None else env
    settings = Audio8Settings(
        host=_wert(quelle, "HOST", "127.0.0.1"),
        port=int(_wert(quelle, "PORT", "30006")),
        model_dir=Path(_wert(quelle, "MODEL_DIR",
                             "/mnt/models/audio/audio8")),
        reference_audio=Path(_wert(
            quelle, "REFERENCE_AUDIO",
            "/mnt/models/audio/audio8/reference/de_male.wav")),
        reference_text=Path(_wert(
            quelle, "REFERENCE_TEXT",
            "/mnt/models/audio/audio8/reference/de_male.txt")),
        device=_wert(quelle, "DEVICE", "cuda:0"),
        dtype=_wert(quelle, "DTYPE", "bfloat16"),
        default_language=_wert(quelle, "DEFAULT_LANGUAGE", "German"),
        max_new_tokens=int(_wert(quelle, "MAX_NEW_TOKENS", "1024")),
        retry_max_new_tokens=int(
            _wert(quelle, "RETRY_MAX_NEW_TOKENS", "2000")),
        temperature=float(_wert(quelle, "TEMPERATURE", "0.8")),
        top_p=float(_wert(quelle, "TOP_P", "0.95")),
        top_k=int(_wert(quelle, "TOP_K", "50")),
        seed=int(_wert(quelle, "SEED", "42")),
    )
    for feld in ("host", "model_dir", "reference_audio", "reference_text",
                 "device", "dtype", "default_language"):
        if not str(getattr(settings, feld)).strip():
            raise ValueError(f"AUDIO8_{feld.upper()} darf nicht leer sein")
    if settings.port <= 0:
        raise ValueError(f"AUDIO8_PORT muss positiv sein: {settings.port}")
    if settings.max_new_tokens <= 0:
        raise ValueError(
            "AUDIO8_MAX_NEW_TOKENS muss positiv sein: "
            f"{settings.max_new_tokens}")
    if settings.retry_max_new_tokens < settings.max_new_tokens:
        raise ValueError(
            "AUDIO8_RETRY_MAX_NEW_TOKENS muss mindestens "
            f"AUDIO8_MAX_NEW_TOKENS sein: {settings.retry_max_new_tokens} < "
            f"{settings.max_new_tokens}")
    if settings.temperature <= 0:
        raise ValueError(
            f"AUDIO8_TEMPERATURE muss positiv sein: {settings.temperature}")
    if not 0 < settings.top_p <= 1:
        raise ValueError(
            f"AUDIO8_TOP_P muss in (0, 1] liegen: {settings.top_p}")
    if settings.top_k < 0:
        raise ValueError(
            f"AUDIO8_TOP_K muss nicht-negativ sein: {settings.top_k}")
    if settings.seed < 0:
        raise ValueError(f"AUDIO8_SEED muss nicht-negativ sein: {settings.seed}")
    return settings
