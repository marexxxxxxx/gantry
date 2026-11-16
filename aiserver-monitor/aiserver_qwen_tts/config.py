"""Einstellungen des Qwen3-TTS-Workers."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class QwenTTSSettings:
    host: str = "127.0.0.1"
    port: int = 30005
    model_dir: Path = Path("/mnt/models/audio/experiments/qwen")
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    default_language: str = "German"


def _wert(env: Mapping[str, str], name: str, default: str) -> str:
    return env.get("QWEN_TTS_" + name) or default


def load_settings(env: Mapping[str, str] | None = None) -> QwenTTSSettings:
    env = os.environ if env is None else env
    settings = QwenTTSSettings(
        host=_wert(env, "HOST", "127.0.0.1").strip(),
        port=int(_wert(env, "PORT", "30005")),
        model_dir=Path(_wert(env, "MODEL_DIR", "/mnt/models/audio/experiments/qwen")),
        device=_wert(env, "DEVICE", "cuda:0").strip(),
        dtype=_wert(env, "DTYPE", "bfloat16").strip(),
        default_language=_wert(env, "DEFAULT_LANGUAGE", "German").strip(),
    )
    if not settings.host:
        raise ValueError("QWEN_TTS_HOST darf nicht leer sein")
    if settings.port <= 0:
        raise ValueError("QWEN_TTS_PORT muss positiv sein")
    if not str(settings.model_dir):
        raise ValueError("QWEN_TTS_MODEL_DIR darf nicht leer sein")
    if not settings.device:
        raise ValueError("QWEN_TTS_DEVICE darf nicht leer sein")
    if not settings.dtype:
        raise ValueError("QWEN_TTS_DTYPE darf nicht leer sein")
    if not settings.default_language:
        raise ValueError("QWEN_TTS_DEFAULT_LANGUAGE darf nicht leer sein")
    return settings
