"""FastAPI-Worker fuer Chatterbox Multilingual V3."""

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .config import ChatterboxSettings, load_settings
from .engine import ChatterboxEngine


class SynthesizeRequest(BaseModel):
    text: str = Field(..., max_length=5000)
    speed: float = Field(1.0, ge=0.5, le=2.0)


def create_chatterbox_app(settings: ChatterboxSettings | None = None, *,
                          engine: ChatterboxEngine | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="aiserver-chatterbox")
    app.state.settings = settings
    app.state.engine = engine or ChatterboxEngine(settings)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/synthesize")
    async def synthesize(anfrage: SynthesizeRequest):
        text = anfrage.text.strip()
        if not text:
            raise HTTPException(400, "text darf nicht leer sein")
        pcm, sr = await asyncio.to_thread(
            app.state.engine.synthesize, text, anfrage.speed)
        return Response(pcm, media_type="audio/pcm",
                        headers={"X-Sample-Rate": str(sr)})

    return app
