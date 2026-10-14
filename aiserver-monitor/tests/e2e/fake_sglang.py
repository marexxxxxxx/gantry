"""Fake-SGLang fuer die Playwright-E2E-Pruefung des Dashboards.

run_e2e.sh startet dieses Skript unter uvicorn auf 127.0.0.1:E2E_FAKE_PORT
(Default 39001). Es antwortet wie SGLang an den vier Stellen, die das
Gateway braucht:

* ``GET /metrics`` - Prometheus-Text mit gueltigen Werten, damit der
  Sammler ``available=True`` meldet (Auslastung, Queue, Durchsatz, Token).
* ``GET /health`` - ``{"status": "ok"}``.
* ``POST /v1/chat/completions`` - Streaming (SSE) und Nicht-Streaming
  (JSON mit ``usage``). Das "Skript" je Anfrage haengt an ``rid`` oder
  ``model``:
    - enthaelt "langsam": erstes Token nach ~0,4 s, danach eines je ~0,5 s
      (40 Token) - laufender Job, wachsende Vorschau und Abbruch werden
      sichtbar;
    - enthaelt "queue": ~2,5 s Wartezeit vor dem ersten Token (sichtbar
      "wartend"), danach ein kurzer Strom;
    - sonst: kurzer, schneller Strom (5 Token je ~50 ms).
* ``POST /abort_request`` - Abbruch wie bei SGLang: einzelne rids oder alle
  laufenden Stroeme enden dann ohne ``finish_reason``. Den Job-Status setzt
  das Gateway selbst.

Jeder Streaming-Frame ist gueltiges OpenAI-SSE (``data: {...}`` mit
``finish_reason`` im letzten Frame, danach ``data: [DONE]``); die ``usage``
steht im letzten Frame bzw. in der JSON-Antwort, damit die Token gemessen
werden. Die Formathelfer (``chat_chunk``, ``meter_text``, ``DONE``) kommen
aus tests/fakes.py, damit der Draht-Vertrag der Unit-Tests unveraendert gilt.
"""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

#: Projektwurzel (tests/e2e/fake_sglang.py -> tests -> Projekt), damit
#: ``tests.fakes`` unabhaengig vom Startverzeichnis importierbar ist.
WURZEL = Path(__file__).resolve().parents[2]
if str(WURZEL) not in sys.path:
    sys.path.insert(0, str(WURZEL))

import uvicorn  # noqa: E402 (sys.path-Fix muss zuerst laufen)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response, StreamingResponse  # noqa: E402

from tests.fakes import DONE, chat_chunk, meter_text  # noqa: E402

#: Port des Fakes; run_e2e.sh setzt E2E_FAKE_PORT.
PORT = int(os.environ.get("E2E_FAKE_PORT", "39001"))

#: Prompt-Token in jeder ``usage`` (fester, deterministischer Wert).
PROMPT_TOKENS = 7

#: Ein Skript je Fall: (Pause vor Token 1, Pause je Token, Anzahl Token).
SKRIPT_LANGSAM = (0.4, 0.5, 40)
SKRIPT_QUEUE = (2.5, 0.1, 4)
SKRIPT_SCHNELL = (0.05, 0.05, 5)


def skript_fuer(schluessel: str) -> tuple[float, float, int]:
    """Fall anhand von rid/model; alles Unbekannte ist der schnelle Fall."""
    if "langsam" in schluessel:
        return SKRIPT_LANGSAM
    if "queue" in schluessel:
        return SKRIPT_QUEUE
    return SKRIPT_SCHNELL


def baue_app() -> FastAPI:
    """FastAPI-App des Fakes mit Abbruch-Zustand (rids und Notaus)."""
    app = FastAPI(title="Fake-SGLang E2E")
    abgebrochen: set[str] = set()
    notaus = {"generation": 0}

    def ist_abgebrochen(rid: str, generation: int) -> bool:
        """Einzelabbruch (rid) oder Notaus (Generation gewechselt)?"""
        return rid in abgebrochen or generation != notaus["generation"]

    async def sse_strom(rid: str, skript: tuple[float, float, int],
                        generation: int) -> AsyncIterator[bytes]:
        """Skripteter SSE-Strom; ein Abbruch beendet ihn ohne Abschluss.

        Der Rollen-Chunk (leeres Delta) kommt wie bei SGLang sofort: das
        Gateway legt den Job damit schon vor dem ersten Token an und meldet
        ihn als "wartend"; erst der erste Inhalts-Chunk zaehlt als TTFT.
        """
        pause_erst, pause_token, anzahl = skript
        try:
            yield chat_chunk({"role": "assistant"})
            if pause_erst > 0:
                await asyncio.sleep(pause_erst)
            for index in range(anzahl):
                if ist_abgebrochen(rid, generation):
                    return
                yield chat_chunk({"content": f"token-{index} "})
                if index < anzahl - 1 and pause_token > 0:
                    await asyncio.sleep(pause_token)
            if ist_abgebrochen(rid, generation):
                return
            yield chat_chunk({}, finish_reason="stop", usage={
                "prompt_tokens": PROMPT_TOKENS,
                "completion_tokens": anzahl,
                "total_tokens": PROMPT_TOKENS + anzahl,
            })
            yield DONE
        finally:
            abgebrochen.discard(rid)

    def json_antwort(model: str, skript: tuple[float, float, int]) -> dict:
        """Kleine, gueltige OpenAI-Antwort fuer den Nicht-Stream-Pfad."""
        _, _, anzahl = skript
        return {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant",
                            "content": "Fake-Antwort ohne Streaming"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": PROMPT_TOKENS,
                      "completion_tokens": anzahl,
                      "total_tokens": PROMPT_TOKENS + anzahl},
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        koerper = await request.json()
        rid = koerper.get("rid")
        rid = rid if isinstance(rid, str) else ""
        model = koerper.get("model")
        model = model if isinstance(model, str) else ""
        skript = skript_fuer(f"{rid} {model}")
        if koerper.get("stream") is True:
            return StreamingResponse(
                sse_strom(rid, skript, notaus["generation"]),
                media_type="text/event-stream")
        return JSONResponse(json_antwort(model, skript))

    @app.post("/abort_request")
    async def abort(request: Request):
        koerper = await request.json()
        if koerper.get("abort_all"):
            notaus["generation"] += 1
        else:
            rid = koerper.get("rid")
            if isinstance(rid, str) and rid:
                abgebrochen.add(rid)
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return Response(meter_text(), media_type="text/plain")

    return app


def main() -> None:
    """Startet den Fake unter uvicorn; Logs gehen nach stdout/stderr."""
    print(f"[fake-sglang] lauscht auf 127.0.0.1:{PORT}", flush=True)
    uvicorn.run(baue_app(), host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
