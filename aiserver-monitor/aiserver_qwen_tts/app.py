"""FastAPI-Worker fuer Qwen3-TTS CustomVoice."""

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from aiserver_tts_worker import install_lifecycle_routes

from .config import QwenTTSSettings, load_settings
from .engine import QwenTTSEngine
from .errors import QwenTTSError

SPEAKERS = (
    "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
    "Ryan", "Aiden", "Ono_Anna", "Sohee",
)
LANGUAGES = (
    "German", "English", "Chinese", "Japanese", "Korean", "French",
    "Russian", "Portuguese", "Spanish", "Italian", "Auto",
)
_SPRECHER = {name.lower(): name for name in SPEAKERS}
_SPRACHEN = {name.lower(): name for name in LANGUAGES}


class SynthesizeRequest(BaseModel):
    text: str = Field(..., max_length=5000)
    voice: str = "Vivian"
    language: str = "German"
    instruct: str | None = None
    speed: float = Field(1.0, ge=0.5, le=2.0)


def _sprecher(name: str) -> str:
    treffer = _SPRECHER.get(name.strip().lower())
    if treffer is None:
        raise HTTPException(400, f"Unbekannte Stimme: {name!r}")
    return treffer


def _sprache(name: str, default: str) -> str:
    if not name.strip():
        return default
    treffer = _SPRACHEN.get(name.strip().lower())
    if treffer is None:
        raise HTTPException(400, f"Unbekannte Sprache: {name!r}")
    return treffer


def create_qwen_tts_app(
    settings: QwenTTSSettings | None = None,
    *,
    engine: QwenTTSEngine | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="aiserver-qwen-tts")
    app.state.settings = settings
    app.state.engine = engine or QwenTTSEngine(settings)
    install_lifecycle_routes(app, app.state.engine)

    @app.exception_handler(QwenTTSError)
    async def qwen_fehler(_request: Request, exc: QwenTTSError):
        return JSONResponse(
            {"error": {"message": exc.message, "code": exc.code}},
            status_code=exc.status_code,
        )

    @app.post("/synthesize")
    async def synthesize(anfrage: SynthesizeRequest):
        text = anfrage.text.strip()
        if not text:
            raise HTTPException(400, "text darf nicht leer sein")
        stimme = _sprecher(anfrage.voice)
        sprache = _sprache(anfrage.language, settings.default_language)
        pcm, sr = await asyncio.to_thread(
            app.state.engine.synthesize,
            text,
            speaker=stimme,
            language=sprache,
            instruct=anfrage.instruct,
        )
        return Response(pcm, media_type="audio/pcm",
                        headers={"X-Sample-Rate": str(sr)})

    return app
