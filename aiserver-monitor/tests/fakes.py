"""Fake-SGLang fuer die Tests des Monitors.

FastAPI-App mit den Routen, die der Monitor braucht: skriptete SSE-Ausgabe
unter ``/v1/chat/completions`` und ``/v1/completions``, ``/metrics`` im
Prometheus-Textformat und ``/health``. Die App laeuft in Tests ueber
``httpx.ASGITransport`` (ohne Sockets) und spaeter im E2E-Lauf ueber uvicorn.

Fuer echte Zeitabstaende (Keepalive, Client-Abbruch) reicht der ASGITransport
nicht: er puffert die Antwort und gibt sie erst nach dem Ende der App als
einen Block heraus. Dort kommt :class:`SkriptStream` per
``httpx.MockTransport`` zum Einsatz: dieselben Skript-Schritte, aber als
zeitgetreuer Bytestrom.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

#: Standardmodellname des Fakes.
MODELL = "fake-modell"

#: Ende-Marker eines SSE-Stroms, byteweise wie am Draht.
DONE = b"data: [DONE]\n\n"

#: Ein Skript-Schritt: Pause vor dem Chunk (Sekunden) und dessen Bytes.
Schritt = tuple[float, bytes]


def chat_chunk(delta: dict, *, finish_reason: str | None = None,
               usage: dict | None = None) -> bytes:
    """Ein ``data:``-Frame im OpenAI-Chat-Format (Bytes wie am Draht).

    ``usage`` landet im selben Frame; SGLang schickt es im letzten Chunk mit
    leerem ``delta``.
    """
    daten = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": MODELL,
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": finish_reason}],
    }
    if usage is not None:
        daten["usage"] = usage
    return b"data: " + json.dumps(daten, ensure_ascii=False).encode("utf-8") + b"\n\n"


def skript_bytes(schritte: Iterable[Schritt]) -> bytes:
    """Alle Skript-Bytes aneinander (die erwartete Draht-Antwort)."""
    return b"".join(chunk for _, chunk in schritte)


def meter_text() -> str:
    """Prometheus-Antwort mit den Metriken, die der Monitor liest."""
    return (
        "# HELP sglang:num_running_reqs Laufende Anfragen\n"
        "# TYPE sglang:num_running_reqs gauge\n"
        'sglang:num_running_reqs{model_name="fake-modell"} 3.0\n'
        "# TYPE sglang:num_queue_reqs gauge\n"
        'sglang:num_queue_reqs{model_name="fake-modell"} 1.0\n'
        "# TYPE sglang:gen_throughput gauge\n"
        'sglang:gen_throughput{model_name="fake-modell"} 42.5\n'
        "# TYPE sglang:token_usage gauge\n"
        'sglang:token_usage{model_name="fake-modell"} 0.25\n'
        "# TYPE sglang:max_total_num_tokens gauge\n"
        'sglang:max_total_num_tokens{model_name="fake-modell"} 32768.0\n'
    )


class FakeSglang:
    """Skriptbarer Fake-Upstream als FastAPI-App.

    ``schritte`` ist die SSE-Ausgabe fuer ``stream=true`` (mit optionalen
    Pausen); ``anfragen`` sammelt die geparsten JSON-Bodies der
    POST-Anfragen, ``pfade`` die angefragten Pfade inklusive Query. Damit
    pruefen Tests Weiterleitung, Query und rid-Durchreichung. ``fehler``
    (Status, Body) laesst die POST-Routen fehlschlagen.
    ``pause_nichtstream_s`` verzoegert die nicht-streamende JSON-Antwort
    (Keepalive-Test des Nicht-Stream-Pfads).
    """

    def __init__(self, schritte: Iterable[Schritt] = (), *,
                 pause_nichtstream_s: float = 0.0):
        self.schritte = list(schritte)
        self.pause_nichtstream_s = pause_nichtstream_s
        self.anfragen: list[dict] = []
        self.pfade: list[str] = []
        self.fehler: tuple[int, bytes] | None = None
        self.app = FastAPI(title="Fake-SGLang")
        self._routen()

    def skript_bytes(self) -> bytes:
        """Die SSE-Antwort des Skripts als Bytes."""
        return skript_bytes(self.schritte)

    def _pfad(self, request: Request) -> None:
        pfad = request.url.path
        if request.url.query:
            pfad += "?" + request.url.query
        self.pfade.append(pfad)

    async def _sse(self) -> AsyncIterator[bytes]:
        for pause, chunk in self.schritte:
            if pause > 0:
                await asyncio.sleep(pause)
            if chunk:
                yield chunk

    def _antwort(self, koerper: dict) -> dict:
        """Kleine, gueltige OpenAI-Antwort fuer den nicht-streamenden Pfad."""
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": koerper.get("model") or MODELL,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Fake-Antwort"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                      "total_tokens": 5},
        }

    def _routen(self) -> None:
        async def post(request: Request):
            self._pfad(request)
            koerper = await request.json()
            self.anfragen.append(koerper)
            if self.fehler is not None:
                status, body = self.fehler
                return Response(body, status_code=status,
                                media_type="application/json")
            if koerper.get("stream"):
                return StreamingResponse(self._sse(),
                                         media_type="text/event-stream")
            if self.pause_nichtstream_s > 0:
                await asyncio.sleep(self.pause_nichtstream_s)
            return JSONResponse(self._antwort(koerper))

        @self.app.get("/health")
        async def health(request: Request):
            self._pfad(request)
            return {"status": "ok"}

        @self.app.get("/metrics")
        async def metrics(request: Request):
            self._pfad(request)
            return Response(meter_text(), media_type="text/plain")

        @self.app.get("/v1/models")
        async def models(request: Request):
            self._pfad(request)
            return {"object": "list",
                    "data": [{"id": MODELL, "object": "model",
                              "owned_by": "fake"}]}

        self.app.post("/v1/chat/completions")(post)
        self.app.post("/v1/completions")(post)


class SkriptStream(httpx.AsyncByteStream):
    """Test-Bytestrom, der die Skript-Schritte mit echten Pausen liefert.

    Mit ``fehler`` bricht der Strom nach den Schritten ab (Upstream weg).
    """

    def __init__(self, schritte: Iterable[Schritt],
                 fehler: BaseException | None = None):
        self._schritte = list(schritte)
        self._fehler = fehler

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for pause, chunk in self._schritte:
            if pause > 0:
                await asyncio.sleep(pause)
            if chunk:
                yield chunk
        if self._fehler is not None:
            raise self._fehler


def skript_transport(schritte: Iterable[Schritt] = (), *,
                     status: int = 200,
                     content_type: str = "text/event-stream",
                     fehler: BaseException | None = None) -> httpx.MockTransport:
    """Transport mit zeitgetreuem Skript-Strom (echte Leerlaufzeiten, keine Sockets).

    Anders als ``httpx.ASGITransport`` puffert dieser Weg nicht: die Pausen
    sind fuer den Relay als Leerlauf sichtbar (Keepalives) und der Client
    kann mitten im Strom abbrechen. ``fehler`` laesst den Strom nach den
    Schritten mit einem Netzfehler enden.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, headers={"content-type": content_type},
            stream=SkriptStream(schritte, fehler))

    return httpx.MockTransport(handler)


def json_transport(inhalt: bytes = b"", *,
                   content_type: str = "application/json",
                   pause: float = 0.0,
                   fehler: BaseException | None = None,
                   fehler_nach_pause: bool = False) -> httpx.MockTransport:
    """Transport mit exakt vorgegebenem, gepuffertem JSON-Body.

    Fuer den Nicht-Stream-Pfad: die Antwort kommt komplett und byte-genau
    (``inhalt``); ``pause`` verzoegert sie (fuer die Keepalive-Schleife
    sichtbar), ``fehler`` wirft stattdessen (z. B. ``httpx.ConnectError``,
    wenn SGLang aus ist). Mit ``fehler_nach_pause=True`` kommt zuerst die
    Pause, dann der Fehler: der "spaete" Transportfehler nach Antwortstart.
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        if fehler is not None and not fehler_nach_pause:
            raise fehler
        if pause > 0:
            await asyncio.sleep(pause)
        if fehler is not None:
            raise fehler
        return httpx.Response(200, headers={"content-type": content_type},
                              content=inhalt)

    return httpx.MockTransport(handler)
