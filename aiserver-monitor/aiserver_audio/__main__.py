"""Startet den Audio-Sidecar mit uvicorn.

Aufruf: ``python -m aiserver_audio``. Host und Port kommen aus
``AUDIO_HOST``/``AUDIO_PORT`` (siehe :mod:`aiserver_audio.config`).
"""

from __future__ import annotations

import uvicorn

from .app import create_audio_app
from .config import load_settings


def main() -> None:
    settings = load_settings()
    app = create_audio_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
