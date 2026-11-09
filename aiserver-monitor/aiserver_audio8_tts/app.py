"""FastAPI-App des Audio8-GPU-Workers."""

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from aiserver_tts_worker import install_lifecycle_routes

from .config import Audio8Settings, load_settings
from .engine import Audio8Engine
from .errors import Audio8TTSError

_VOICES = frozenset({
    "", "default", "audio8", "de_male", "de_de-thorsten-medium",
})


class SynthesizeRequest(BaseModel):
    """Text-fuer-Sprache-Anfrage an den Audio8-Worker."""

    text: str = Field(..., max_length=5000)
    voice: str = "de_male"
    language: str = ""


def _voice(name: str) -> str:
    gesucht = (name or "").strip().casefold()
    if gesucht in _VOICES:
        return "de_male"
    raise HTTPException(400, f"Unbekannte Audio8-Stimme: {name!r}")


def create_audio8_tts_app(settings: Audio8Settings | None = None, *,
                          engine=None) -> FastAPI:
    """Baut die Worker-App; die Engine ist fuer Tests austauschbar."""
    settings = settings or load_settings()
    engine = engine or Audio8Engine(settings)
    app = FastAPI(title="aiserver-audio8-tts")
    app.state.settings = settings
    app.state.engine = engine
    install_lifecycle_routes(app, engine)

    @app.exception_handler(Audio8TTSError)
    async def _audio8_fehler(request, exc: Audio8TTSError):
        return JSONResponse(
            {"error": {"message": exc.message, "code": exc.code}},
            status_code=exc.status_code)

    @app.post("/synthesize")
    async def synthese(anfrage: SynthesizeRequest):
        text = anfrage.text.strip()
        if not text:
            raise HTTPException(400, "text darf nicht leer sein")
        _voice(anfrage.voice)
        language = anfrage.language.strip() or settings.default_language
        pcm, rate = await asyncio.to_thread(
            engine.synthesize, text, language=language)
        return Response(
            pcm,
            media_type="audio/pcm",
            headers={"X-Sample-Rate": str(rate)})

    return app
