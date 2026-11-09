"""Gemeinsames HTTP-Geruest der TTS-GPU-Worker.

Die Worker unterscheiden sich in Engine und Synthese-Request, teilen aber
den Lebenszyklus: ``/healthz`` meldet den Ladezustand, ``/load`` laedt das
Modell und ``/unload`` gibt es wieder frei. Dieses Modul haelt genau diese
drei Routen an einer Stelle, damit Qwen3-TTS und Audio8 denselben Vertrag
erfuellen.
"""

import asyncio
from typing import Protocol

from fastapi import FastAPI


class LifecycleEngine(Protocol):
    """Minimaler Engine-Vertrag fuer das gemeinsame Worker-Geruest."""

    @property
    def model_loaded(self) -> bool:
        ...

    def load(self) -> None:
        ...

    def unload(self) -> None:
        ...


def install_lifecycle_routes(app: FastAPI,
                             engine: LifecycleEngine) -> None:
    """Registriert ``/healthz``, ``/load`` und ``/unload`` an ``app``."""

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "model_loaded": engine.model_loaded}

    @app.post("/load")
    async def laden():
        await asyncio.to_thread(engine.load)
        return {"status": "ok", "model_loaded": True}

    @app.post("/unload")
    async def entladen():
        await asyncio.to_thread(engine.unload)
        return {"status": "ok", "model_loaded": False}
