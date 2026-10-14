"""Passthrough fuer ``/v1/*``: Relay mit Keepalives und Job-Tracking.

Der Monitor sitzt zwischen Client und SGLang. ``handle_v1`` reicht Anfragen
weiter; nur ``stream=true`` laeuft ueber das Relay: eine Lesetask liest den
Upstream **bis zum Ende** und fuellt eine Queue, der Client-Generator wartet
mit ``heartbeat_s`` und sendet bei Leerlauf den Kommentar ``: ping``
(Cloudflare-sicher). Zwischen den Keepalives fliessen die Original-Bytes roh
zum Client, inklusive ``[DONE]``.

Nebenbei misst das Relay jeden Byte-Chunk (``SSEByteParser`` +
``summarize_chunk``): TTFT, Vorschau, Tokenzaehler und gedrosselte
Live-Ereignisse am Hub; beim Abschluss genau ein ``history.record_job``.
Voller Prompt und Vorschauen bleiben im RAM. Ohne ``stream=true`` (und fuer
GET) gibt es einen getreuen, gepufferten Passthrough ohne Job; Keepalives
und Fehlerbilder fuer diesen Pfad baut Task 8.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from .config import Config
from .events import EventHub, format_comment
from .history import History
from .jobs import Job, JobRegistry, JobStatus, job_to_dict
from .sse_parse import SSEByteParser, summarize_chunk

logger = logging.getLogger(__name__)

#: Hoechstens ein Live-Ereignis je Job in diesem Abstand (max. 4/s).
PUBLISH_ABSTAND_S = 0.25

#: Keepalive-Kommentar, wie er zwischen den Original-Bytes zum Client geht.
_PING = format_comment("ping").encode("utf-8")

#: Anfrage-Kopfzeilen, die nie zum Upstream gehen: ``host`` gehoert zur
#: Ziel-URL, Laenge und Verbindung setzen httpx und uvicorn.
_NIE_ZUM_UPSTREAM = frozenset((
    "host", "content-length", "connection", "keep-alive", "upgrade",
    "transfer-encoding",
))

#: Upstream-Kopfzeilen, die nie zum Client gehen: Laenge und Verbindung regelt
#: Starlette, ``content-encoding`` entfaellt, weil httpx schon dekodiert hat.
_NIE_ZUM_CLIENT = frozenset((
    "content-length", "content-encoding", "connection", "keep-alive",
    "upgrade", "transfer-encoding",
))


@dataclass
class Deps:
    """Was ``handle_v1`` von der App bekommt.

    ``client`` ist der httpx-Client zum Upstream; er darf einen eigenen
    ``transport`` tragen (Tests) und wird von der App verwaltet.
    """

    config: Config
    registry: JobRegistry
    hub: EventHub
    history: History | None
    client: httpx.AsyncClient


def ensure_rid(body: dict) -> str:
    """Reicht eine Client-rid durch, sonst ``uuid4().hex`` in den Body.

    Nur nicht-leere Strings gelten als Client-rid; alles andere (None, "",
    Zahlen) wird ersetzt, damit SGLang immer eine brauchbare rid sieht.
    """
    rid = body.get("rid")
    if not isinstance(rid, str) or not rid:
        rid = uuid.uuid4().hex
        body["rid"] = rid
    return rid


def _text_von(inhalt: object) -> str:
    """Nachrichtentext: Strings bleiben, None wird leer, Rest wird JSON.

    Multimodale Inhalte (Liste von Teilen) werden so lesbar aufbereitet,
    ohne zu raten.
    """
    if isinstance(inhalt, str):
        return inhalt
    if inhalt is None:
        return ""
    return json.dumps(inhalt, ensure_ascii=False, default=str)


def _extract_full_prompt(koerper: dict) -> str:
    """Setzt den vollen Prompt aus ``messages`` (chat) bzw. ``prompt`` zusammen.

    Landet nur im RAM: in die History geht ausschliesslich der Anfang
    (``prompt_start``, erste ``prompt_start_chars`` Zeichen, macht die
    Registry beim Anlegen).
    """
    messages = koerper.get("messages")
    if isinstance(messages, list):
        zeilen = []
        for nachricht in messages:
            if not isinstance(nachricht, dict):
                continue
            rolle = nachricht.get("role")
            if not isinstance(rolle, str):
                rolle = "?"
            zeilen.append(f"{rolle}: {_text_von(nachricht.get('content'))}")
        return "\n".join(zeilen)
    prompt = koerper.get("prompt")
    if isinstance(prompt, list):
        return "\n".join(_text_von(teil) for teil in prompt)
    return _text_von(prompt)


def _client_source(request: Request) -> str:
    """Quelle minimal: Peer-IP des Clients oder ``"unbekannt"``.

    Die Cloudflare-Vorranglogik (``CF-Connecting-IP``) baut Task 10 als
    ``client_source`` in der App.
    """
    client = request.client
    if client is not None and client.host:
        return client.host
    return "unbekannt"


def _upstream_url(request: Request, config: Config) -> str:
    """Upstream-URL aus Basis, Originalpfad und Query."""
    url = config.upstream_base.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return url


def _upstream_headers(request: Request) -> dict[str, str]:
    """Kopfzeilen zum Upstream, klein geschrieben und ohne Host/Laenge/Hop-by-Hop.

    Klein geschrieben, damit das Ueberschreiben einzelner Felder (z. B.
    ``content-type``) keinen doppelten Kopf erzeugt; HTTP-Namen sind
    schreibungsunabhaengig.
    """
    return {name.lower(): wert for name, wert in request.headers.items()
            if name.lower() not in _NIE_ZUM_UPSTREAM}


def _response_headers(antwort: httpx.Response) -> dict[str, str]:
    """Upstream-Kopfzeilen fuer die Antwort an den Client (gefiltert)."""
    return {name: wert for name, wert in antwort.headers.items()
            if name.lower() not in _NIE_ZUM_CLIENT}


def _timeout(config: Config) -> httpx.Timeout:
    """Nur der Verbindungsaufbau ist begrenzt; Streams lesen ohne Limit."""
    return httpx.Timeout(None, connect=config.connect_timeout_s)


def _json_koerper(roh: bytes) -> dict | None:
    """Geparster JSON-Body oder None (kein JSON-Objekt)."""
    try:
        koerper = json.loads(roh)
    except ValueError:
        return None
    return koerper if isinstance(koerper, dict) else None


class _JobTracker:
    """Verfolgt einen Streaming-Job: Parser, usage, Vorschau, Drossel.

    ``absorb`` verarbeitet jeden Upstream-Chunk (TTFT, Vorschau, Zaehler und
    gedrosselte Live-Ereignisse), ``close`` schliesst genau einmal ab
    (Registry, History, finales Ereignis). Beides ruft die Lesetask auf,
    nicht der Client-Generator; so endet der Job auch nach einem
    Client-Abbruch sauber.
    """

    def __init__(self, deps: Deps, job: Job):
        self.job = job
        self._deps = deps
        self._parser = SSEByteParser()
        self._last_publish: float | None = None
        self._finish_reason: str | None = None
        self._inhalt_gesehen = False
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None

    @property
    def rid(self) -> str:
        return self.job.rid

    def absorb(self, chunk: bytes) -> None:
        """Ein Upstream-Chunk: messen, Vorschau fuellen, gedrosselt melden."""
        for obj in self._parser.feed(chunk):
            summary = summarize_chunk(obj)
            if summary.content or summary.reasoning:
                # Erst echtes Delta zaehlt als erstes Token; der Rollen-Chunk
                # von SGLang (leerer Inhalt) soll die TTFT nicht verfaelschen.
                self._inhalt_gesehen = True
                self._deps.registry.note_running(self.rid, time.time())
                self._deps.registry.add_text(
                    self.rid, content=summary.content,
                    reasoning=summary.reasoning)
            if summary.prompt_tokens is not None:
                self.prompt_tokens = summary.prompt_tokens
            if summary.completion_tokens is not None:
                self.completion_tokens = summary.completion_tokens
            if summary.finish_reason is not None:
                self._finish_reason = summary.finish_reason
        self._publish_due()

    def close_clean(self) -> None:
        """Streamende ohne Fehler auswerten.

        Kam Inhalt, aber kein ``finish_reason``, endete der Strom vorzeitig
        (abgeschnitten); dann wird der Job als Fehler abgeschlossen statt als
        Zombie oder faelschlich als fertig zu bleiben. Antworten ohne
        SSE-Inhalt (kein Delta) gelten normal als fertig.
        """
        if self._inhalt_gesehen and self._finish_reason is None:
            logger.warning("Stream ohne finish_reason bei rid=%s", self.rid)
            self.close(JobStatus.ERROR,
                       error="Stream endete ohne finish_reason")
            return
        self.close(JobStatus.DONE)

    def close(self, status: JobStatus, *, error: str | None = None) -> None:
        """Job abschliessen: Registry, einmal History, dann das Ereignis."""
        self._deps.registry.finish(
            self.rid, status, prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens, error=error,
            now=time.time())
        daten = job_to_dict(self.job)
        if self._deps.history is not None:
            self._deps.history.record_job(daten)
        self._deps.hub.publish("job", daten)

    def _publish_due(self) -> None:
        """Live-Ereignis senden, aber hoechstens alle PUBLISH_ABSTAND_S."""
        jetzt = time.time()
        if (self._last_publish is not None
                and jetzt - self._last_publish < PUBLISH_ABSTAND_S):
            return
        self._last_publish = jetzt
        self._deps.hub.publish("job", job_to_dict(self.job))


class _Relay:
    """Lesetask plus Queue fuer einen Streaming-Job.

    Die Lesetask liest den Upstream bis zum Ende und meldet jeden Chunk an
    die Queue und den Tracker; der Client-Generator wartet mit
    ``heartbeat_s`` auf Bytes und sendet sonst ``: ping``. Bricht der Client
    ab, schaltet sein ``finally`` nur die Weitergabe ab (``_client_ab``);
    die Lesetask misst weiter, der Job endet sauber, das Ergebnis wird
    verworfen.
    """

    def __init__(self, antwort: httpx.Response, tracker: _JobTracker, *,
                 heartbeat_s: float):
        self._antwort = antwort
        self._tracker = tracker
        self._heartbeat_s = heartbeat_s
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._client_ab = False
        self._lesetask: asyncio.Task | None = None

    def start(self) -> None:
        """Startet die Lesetask (liest den Upstream bis zum Ende)."""
        self._lesetask = asyncio.create_task(
            self._lesen(), name=f"relay-lesen-{self._tracker.rid}")

    async def _lesen(self) -> None:
        try:
            async for chunk in self._antwort.aiter_bytes():
                self._tracker.absorb(chunk)
                if not self._client_ab:
                    self._queue.put_nowait(chunk)
        except Exception as exc:                       # Upstream weg/mittendrin kaputt
            logger.warning("Upstream-Abbruch bei rid=%s: %s: %s",
                           self._tracker.rid, type(exc).__name__, exc)
            self._tracker.close(JobStatus.ERROR,
                                error=f"{type(exc).__name__}: {exc}")
        else:
            self._tracker.close_clean()
        finally:
            try:
                await self._antwort.aclose()
            finally:
                self._queue.put_nowait(None)

    async def stream(self) -> AsyncIterator[bytes]:
        """Client-Seite: Original-Bytes roh, bei Leerlauf Keepalive-Pings."""
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(self._queue.get(),
                                                   timeout=self._heartbeat_s)
                except TimeoutError:
                    yield _PING
                    continue
                if chunk is None:
                    return
                yield chunk
        finally:
            # Nur die Client-Seite abraeumen: die Lesetask liest weiter.
            self._client_ab = True


async def _passthrough(request: Request, deps: Deps, roh: bytes) -> Response:
    """Getreuer, gepufferter Passthrough (GET ohne Body, ohne ``stream=true``).

    Status und Body kommen unveraendert zurueck, ohne Job und ohne
    Keepalives; Job-Tracking und Fehlerbilder fuer diesen Pfad baut Task 8.
    """
    antwort = await deps.client.request(
        request.method,
        _upstream_url(request, deps.config),
        headers=_upstream_headers(request),
        content=roh if roh else None,
        timeout=_timeout(deps.config),
    )
    return Response(antwort.content, status_code=antwort.status_code,
                    headers=_response_headers(antwort))


async def handle_v1(request: Request, deps: Deps) -> Response:
    """Bedient jede ``/v1/*``-Anfrage des Gateways.

    GET ohne Body und POSTs ohne ``stream=true`` sind ein gepufferter
    Passthrough. ``stream=true`` laeuft ueber das Relay: erst die Kopfzeile
    des Upstreams pruefen (Status >= 400 kommt unveraendert zurueck, ohne
    SSE und ohne Job), dann Job anlegen und streamen. Der Job wird erst nach
    der Kopfzeile angelegt; so hinterlaesst keine abgelehnte Anfrage eine
    Zeile in der Registry.
    """
    roh = await request.body()
    koerper = _json_koerper(roh) if request.method == "POST" else None
    if koerper is None or koerper.get("stream") is not True:
        return await _passthrough(request, deps, roh)

    rid = ensure_rid(koerper)
    kopf = _upstream_headers(request)
    kopf["content-type"] = "application/json"
    antwort = await deps.client.send(
        deps.client.build_request(
            "POST", _upstream_url(request, deps.config), headers=kopf,
            content=json.dumps(koerper, ensure_ascii=False).encode("utf-8"),
            timeout=_timeout(deps.config)),
        stream=True,
    )

    if antwort.status_code >= 400:
        await antwort.aread()
        inhalt = antwort.content
        kopfzeilen = _response_headers(antwort)
        await antwort.aclose()
        return Response(inhalt, status_code=antwort.status_code,
                        headers=kopfzeilen)

    model = koerper.get("model")
    job = deps.registry.register(
        rid,
        source=_client_source(request),
        model=model if isinstance(model, str) else "",
        full_prompt=_extract_full_prompt(koerper),
        now=time.time(),
    )
    relay = _Relay(antwort, _JobTracker(deps, job),
                   heartbeat_s=deps.config.heartbeat_s)
    relay.start()
    return StreamingResponse(relay.stream(), status_code=antwort.status_code,
                             headers=_response_headers(antwort),
                             media_type="text/event-stream")
