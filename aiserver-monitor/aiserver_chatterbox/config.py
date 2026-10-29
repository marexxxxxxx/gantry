"""Einstellungen des Chatterbox-Workers."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ChatterboxSettings:
    host: str = "127.0.0.1"
    port: int = 30004
    models_dir: Path = Path("/mnt/models/audio/chatterbox")
    threads: int = 8


def _wert(env: Mapping[str, str], name: str, default: str) -> str:
    return env.get("CHATTERBOX_" + name) or default


def load_settings(env: Mapping[str, str] | None = None) -> ChatterboxSettings:
    env = os.environ if env is None else env
    settings = ChatterboxSettings(
        host=_wert(env, "HOST", "127.0.0.1").strip(),
        port=int(_wert(env, "PORT", "30004")),
        models_dir=Path(_wert(env, "MODELS_DIR", "/mnt/models/audio/chatterbox")),
        threads=int(_wert(env, "THREADS", "8")),
    )
    if not settings.host:
        raise ValueError("CHATTERBOX_HOST darf nicht leer sein")
    for feld in ("port", "threads"):
        if getattr(settings, feld) <= 0:
            raise ValueError(f"CHATTERBOX_{feld.upper()} muss positiv sein")
    return settings
