"""Startet den Audio8-TTS-Worker mit uvicorn."""

import uvicorn

from .app import create_audio8_tts_app
from .config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        create_audio8_tts_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
