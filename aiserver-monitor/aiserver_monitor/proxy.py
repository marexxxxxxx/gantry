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
Voller Prompt und Vorschauen bleiben im RAM. POSTs mit JSON-Body und
``stream != true`` laufen ueber den Nicht-Stream-Pfad: eigener Job, je
Heartbeat ein einzelnes Leerzeichen (WS-Keepalive, abschaltbar) und am
Ende der komplette JSON-Body; ist der Upstream weg, kommt 502. Faellt der
Upstream erst nach dem Antwortstart aus, folgt der 502-Koerper als JSON
hinter den Leerzeichen ("spaeter" Transportfehler). GETs und POSTs ohne
JSON-Objekt bleiben ein getreuer, gepufferter Passthrough. Anfragen mit
Punkt-Segmenten im Pfad (``.``/``..``) werden vor jedem Upstream-Kontakt
mit 404 abgewiesen: httpx wuerde sie normalisieren und damit SGLang-Admin-
Routen am Job-Tracking vorbei erreichbar machen.

Mit ``abort_on_disconnect`` (Default true) wacht je getracktem Request ein
``_DisconnectWatcher`` ueber die Client-Verbindung
(``request.is_disconnected``, Poll ``_DISCONNECT_CHECK_S``): bricht der
Client ab, wird der wartende httpx-Aufruf gecancelt, der Job als
``aborted`` abgeschlossen (ohne Fehler, mit Ereignis) und der Request best
effort per ``abort_request`` bei SGLang abgemeldet. Mit ``false`` laeuft
alles wie bisher zu Ende (kein Disconnect-Handling).
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from .cancel import abort_request
from .config import Config
from .events import EventHub, format_comment
from .history import History
from .jobs import ACTIVE_STATUSES, Job, JobRegistry, JobStatus, job_to_dict
from .sse_parse import SSEByteParser, summarize_chunk

logger = logging.getLogger(__name__)

#: Hoechstens ein Live-Ereignis je Job in diesem Abstand (max. 4/s).
PUBLISH_ABSTAND_S = 0.25

#: Abstand der Disconnect-Pruefung je Watcher-Task (Sekunden).
_DISCONNECT_CHECK_S = 1.0

#: Keepalive-Kommentar, wie er zwischen den Original-Bytes zum Client geht.
_PING = format_comment("ping").encode("utf-8")

#: Antwortkoerper, wenn der Upstream nicht erreichbar ist (SGLang aus).
_BODY_NICHT_ERREICHBAR = (
    b'{"error": {"message": "sglang nicht erreichbar", '
    b'"type": "upstream_unavailable"}}')

#: Antwortkoerper auf Pfade mit Punkt-Segmenten (Traversal-Versuch).
_BODY_UNGUELTIGER_PFAD = (
    b'{"error": {"message": "ungueltiger Pfad", '
    b'"type": "invalid_path"}}')

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


def _pfad_mit_punktsegment(pfad: str) -> bool:
    """True, wenn ein Pfadsegment genau ``.`` oder ``..`` ist.

    httpx normalisiert solche Segmente beim Bau der Upstream-URL:
    ``/v1/../get_server_info`` wuerde beim Upstream als
    ``/get_server_info`` ankommen und damit SGLang-Admin-Routen ungeschuetzt
    erreichbar machen. Solche Anfragen enden vor jedem Upstream-Kontakt.
    """
    return any(segment in (".", "..") for segment in pfad.split("/"))


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


def _tokenzahl(wert: object) -> int | None:
    """Ganzzahlige Tokenzahl oder None; bool und Fremdtypen zaehlen nicht."""
    if isinstance(wert, bool) or not isinstance(wert, int):
        return None
    return wert


def _usage_aus_json(inhalt: bytes) -> tuple[int | None, int | None]:
    """``(prompt_tokens, completion_tokens)`` aus dem JSON-Body.

    Nicht-streamende Antworten tragen die usage direkt; fehlende oder
    fremdartige Werte bleiben None ("nicht gemessen" heisst None, nie 0).
    """
    koerper = _json_koerper(inhalt)
    usage = koerper.get("usage") if koerper is not None else None
    if not isinstance(usage, dict):
        return None, None
    return (_tokenzahl(usage.get("prompt_tokens")),
            _tokenzahl(usage.get("completion_tokens")))


def _finish_job(deps: Deps, job: Job, status: JobStatus, *,
                error: str | None = None,
                prompt_tokens: int | None = None,
                completion_tokens: int | None = None) -> None:
    """Job abschliessen: Registry, Ereignis, dann (fehlertolerant) History.

    Das Abschlussereignis geht **vor** dem History-Schreiben raus, und ein
    History-Fehler wird nur geloggt: ein DB-Problem darf den Abschluss weder
    verschlucken noch als un-abgeholter Task-Fehler entkommen (Task-7-Minor).
    Relay und Nicht-Stream-Pfad teilen sich diese Routine.
    """
    deps.registry.finish(job.rid, status, prompt_tokens=prompt_tokens,
                         completion_tokens=completion_tokens, error=error,
                         now=time.time())
    daten = job_to_dict(job)
    deps.hub.publish("job", daten)
    if deps.history is not None:
        try:
            deps.history.record_job(daten)
        except Exception:
            logger.warning("History-Eintrag fehlgeschlagen bei rid=%s",
                           job.rid, exc_info=True)


def _beende_abgebrochen(deps: Deps, job: Job) -> None:
    """Schliesst einen aktiven Job als ``aborted`` ab (nie als error).

    Nur ein noch aktiver Job wird angefasst: ist er schon fertig, aendert
    ein Client-Abbruch nichts mehr und es geht kein doppeltes Ereignis raus
    (``finish`` ist first-wins, das Ereignis kommt hier trotzdem genau einmal).
    """
    if job.status not in ACTIVE_STATUSES:
        return
    deps.registry.finish(job.rid, JobStatus.ABORTED, now=time.time())
    deps.hub.publish("job", job_to_dict(job))


async def _abort_best_effort(deps: Deps, rid: str) -> None:
    """Meldet den Request bei SGLang ab; jeder Fehler wird nur geloggt.

    Best effort: der Job ist zu diesem Zeitpunkt schon abgeschlossen, ein
    kaputter Upstream darf den Aufraeumpfad nicht als Exception verlassen
    (Ruling R3d).
    """
    try:
        await abort_request(deps.client, deps.config.upstream_base,
                            rid=rid, abort_all=False)
    except Exception:
        logger.warning("Abort am Upstream fehlgeschlagen (rid=%s)", rid,
                       exc_info=True)


def _client_weg_antwort() -> Response:
    """Antwort fuer einen Request, dessen Client schon weg ist.

    Die Verbindung ist tot, die Antwort erreicht niemanden mehr (499 wie
    nginx); so bleibt der Rueckweg der Aufraeumpfade einheitlich.
    """
    return Response(status_code=499)


class _DisconnectWatcher:
    """Wacht je getracktem Request ueber die Client-Verbindung.

    Pollt ``request.is_disconnected()`` im Abstand ``_DISCONNECT_CHECK_S``
    und ruft beim ersten Treffer den gesetzten Handler auf; der Handler
    raeumt die aktuelle Phase auf (pending Task canceln, Job abschliessen,
    Abort anstossen). ``setze_handler`` wechselt ihn beim Phasenwechsel
    (Kopfzeile -> Relay). ``stop`` beendet den Task; nach einem Treffer wird
    nicht gecancelt, damit der laufende Handler zu Ende kommt (der Task endet
    dann von selbst).
    """

    def __init__(self, request: Request, rid: str):
        self._request = request
        self.rid = rid
        self._handler: Callable[[], Awaitable[None]] | None = None
        self._task: asyncio.Task | None = None
        self._getroffen = False
        self._gestoppt = False

    @property
    def getroffen(self) -> bool:
        """True, sobald der Disconnect erkannt wurde."""
        return self._getroffen

    def setze_handler(self, handler: Callable[[], Awaitable[None]]) -> None:
        """Handler der aktuellen Phase (vor ``start`` bzw. beim Wechsel)."""
        self._handler = handler

    def start(self) -> None:
        """Startet den Watcher-Task (einmal je getracktem Request)."""
        self._task = asyncio.create_task(
            self._poll(), name=f"disconnect-{self.rid}")

    async def _poll(self) -> None:
        """Poll-Schleife bis zum Treffer oder ``stop``.

        Der ``stop``-Cancel kann in Starlettes ``is_disconnected``
        (``anyio``-CancelScope) verschluckt werden; darum prueft die Schleife
        das Stopp-Flag zusaetzlich selbst.
        """
        while True:
            if await self._request.is_disconnected():
                self._getroffen = True
                if self._handler is not None:
                    await self._handler()
                return
            if self._gestoppt:
                return
            try:
                await asyncio.sleep(_DISCONNECT_CHECK_S)
            except asyncio.CancelledError:
                if self._gestoppt:
                    return               # regulaeres Ende ueber ``stop``
                raise

    def stop(self) -> None:
        """Beendet den Watcher-Task (idempotent).

        Setzt das Stopp-Flag und cancelt den Task. Starlettes
        ``is_disconnected`` laeuft in einem ``anyio``-CancelScope und kann
        einen fremden Cancel verschlucken; das Flag beendet die Schleife
        darum zusaetzlich. Nach einem Treffer wird nicht gecancelt: der
        Handler laeuft zu Ende, der Task endet gleich selbst.
        """
        self._gestoppt = True
        if self._task is not None and not self._getroffen:
            self._task.cancel()


def _neue_wache(request: Request, deps: Deps, rid: str
                ) -> _DisconnectWatcher | None:
    """Watcher je getracktem Request; ohne ``abort_on_disconnect`` None.

    ``None`` heisst Altverhalten: kein Disconnect-Handling (Ruling R1). Der
    Aufrufer setzt den Handler und startet die Wache selbst; die Phase, die
    die Wache beendet, raeumt sie in ihrem ``finally`` ab.
    """
    if not deps.config.abort_on_disconnect:
        return None
    return _DisconnectWatcher(request, rid)


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
        """Job abschliessen: Registry, Ereignis, dann die History."""
        _finish_job(self._deps, self.job, status, error=error,
                    prompt_tokens=self.prompt_tokens,
                    completion_tokens=self.completion_tokens)

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
    verworfen. Mit ``wache`` (Default ``abort_on_disconnect``) uebernimmt
    der Watcher stattdessen das Aufraeumen: er cancelt die Lesetask,
    schliesst den Job als ``aborted`` ab und meldet den Request ab; die
    Lesetask beendet die Wache in ihrem ``finally``, sofern der Disconnect
    sie nicht selbst ausgeloest hat (dann laeuft ihr Handler noch).
    """

    def __init__(self, antwort: httpx.Response, tracker: _JobTracker, *,
                 heartbeat_s: float,
                 wache: _DisconnectWatcher | None = None):
        self._antwort = antwort
        self._tracker = tracker
        self._heartbeat_s = heartbeat_s
        self._wache = wache
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._client_ab = False
        self._lesetask: asyncio.Task | None = None

    def start(self) -> None:
        """Startet die Lesetask (liest den Upstream bis zum Ende)."""
        self._lesetask = asyncio.create_task(
            self._lesen(), name=f"relay-lesen-{self._tracker.rid}")

    async def beenden(self) -> None:
        """Beendet die Lesetask und raeumt ihre Reste ab (Client weg).

        Cancelt die Lesetask, wartet ihr Ende ab und schliesst die
        Upstream-Antwort; der Client-Generator wird mit ``None`` geweckt.
        Das deckt auch eine Lesetask ab, die vor ihrem ersten Schritt
        gecancelt wurde (Disconnect genau im Uebergang): dann lief ihr
        ``finally`` nie. Ein doppelter ``None``-Wecker ist harmlos, der
        Generator endet beim ersten.
        """
        aufgabe = self._lesetask
        if aufgabe is None:
            return
        aufgabe.cancel()
        await asyncio.wait({aufgabe})
        await self._antwort.aclose()
        self._queue.put_nowait(None)

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
                if self._wache is not None:
                    self._wache.stop()

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


async def _hole_nichtstream(deps: Deps, job: Job, url: str,
                            kopf: dict[str, str], inhalt: bytes,
                            wache: _DisconnectWatcher | None = None
                            ) -> httpx.Response | Exception:
    """Upstream-POST ohne Stream; schliesst den Job immer selbst ab.

    Die Aufgabe laeuft auch nach einem Client-Abbruch zu Ende und
    hinterlaesst keinen Zombie (wie ``_Relay``); bei einem Disconnect
    cancelt die ``wache`` sie und schliesst den Job ab. Ein Fehler wird als
    Wert zurueckgegeben statt geworfen: findet ihn der Client-Generator nicht
    mehr (Client weg), entsteht kein un-abgeholter Task-Fehler. Der Job ist
    fertig, wenn die Aufgabe endet: ``done`` mit usage aus dem JSON-Body,
    sonst ``error`` mit Statustext bzw. Meldung. Ohne Chunks gibt es kein
    erstes Token: der Job springt von ``waiting`` auf ``done``/``error``
    (TTFT bleibt None, "nicht gemessen").
    """
    try:
        antwort = await deps.client.request(
            "POST", url, headers=kopf, content=inhalt,
            timeout=_timeout(deps.config))
    except Exception as exc:
        logger.warning("Upstream-Fehler bei rid=%s: %s: %s",
                       job.rid, type(exc).__name__, exc)
        _finish_job(deps, job, JobStatus.ERROR,
                    error=f"{type(exc).__name__}: {exc}")
        return exc
    finally:
        # Der wartende Upstream-Aufruf ist vorbei: die Wache hat hier nichts
        # mehr zu cancelln (nach einem Treffer laeuft ihr Handler noch).
        if wache is not None:
            wache.stop()
    if antwort.status_code >= 400:
        _finish_job(deps, job, JobStatus.ERROR,
                    error=f"upstream {antwort.status_code}")
    else:
        prompt_tokens, completion_tokens = _usage_aus_json(antwort.content)
        _finish_job(deps, job, JobStatus.DONE,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens)
    return antwort


async def _nichtstream_langsam(deps: Deps,
                               aufgabe: asyncio.Task,
                               wache: _DisconnectWatcher | None = None
                               ) -> AsyncIterator[bytes]:
    """Client-Seite des Nicht-Stream-Pfads: WS-Keepalives, dann der Body.

    Je Heartbeat ohne Upstream-Antwort geht ein einzelnes Leerzeichen raus
    (nur wenn ``config.nonstream_keepalive``); der komplette JSON-Body folgt,
    sobald die Aufgabe fertig ist. Den Job schliesst die Aufgabe ab, nicht
    dieser Generator: ein Client-Abbruch laesst sie unberuehrt weiterlaufen.
    Scheitert die Aufgabe erst nach dem Antwortstart (spaeter
    Transportfehler), folgt der 502-Koerper hinter den Leerzeichen: so
    bleibt der Gesamtbody parsebares JSON statt "Leerzeichen und nichts".
    Hat die ``wache`` einen Disconnect erkannt, ist der Job schon
    abgeschlossen und die Aufgabe gecancelt: der Generator endet ohne Body,
    ohne die CancelledError zu heben.
    """
    try:
        while True:
            fertig, _ = await asyncio.wait({aufgabe},
                                           timeout=deps.config.heartbeat_s)
            if fertig:
                break
            if deps.config.nonstream_keepalive:
                yield b" "
        if wache is not None and wache.getroffen:
            return                       # Client weg: die Wache hat aufgeraeumt
        ergebnis = aufgabe.result()
        if isinstance(ergebnis, httpx.TransportError):
            yield _BODY_NICHT_ERREICHBAR
            return
        if isinstance(ergebnis, Exception):
            return                       # Fehler: Log und Job macht die Aufgabe
        yield ergebnis.content
    finally:
        if wache is not None:
            wache.stop()


async def _nichtstream(request: Request, deps: Deps, koerper: dict) -> Response:
    """POST mit JSON-Body und ``stream != true``: Job plus WS-Keepalives.

    Der Job entsteht **vor** dem Upstream-Aufruf und wird sofort publiziert,
    damit das Dashboard wartende Anfragen sieht; fehlt die rid, kommt eine
    neue in den Body (wie im Relay, fuer Registry und /abort_request). Ist
    die Antwort innerhalb eines Heartbeats da, geht sie byte-identisch und
    ohne Leerzeichen durch (kein Streaming); sonst liefert der Generator
    Leerzeichen vor dem kompletten JSON-Body. Nur ein Verbindungsfehler vor
    dem Antwortstart ergibt 502; spaeter erkannte Fehler stehen nur noch im
    Log und im Job (der Status 200 ist dann schon beim Client). Mit
    ``abort_on_disconnect`` (Default) wacht eine Wache ueber die Verbindung:
    bricht der Client waehrend der Upstream-Wartezeit ab, wird die Aufgabe
    gecancelt, der Job als ``aborted`` abgeschlossen und der Request best
    effort bei SGLang abgemeldet (Ruling R3a).
    """
    rid = ensure_rid(koerper)
    model = koerper.get("model")
    job = deps.registry.register(
        rid,
        source=_client_source(request),
        model=model if isinstance(model, str) else "",
        full_prompt=_extract_full_prompt(koerper),
        now=time.time(),
    )
    deps.hub.publish("job", job_to_dict(job))

    kopf = _upstream_headers(request)
    kopf["content-type"] = "application/json"
    wache = _neue_wache(request, deps, rid)
    aufgabe = asyncio.create_task(
        _hole_nichtstream(
            deps, job, _upstream_url(request, deps.config), kopf,
            json.dumps(koerper, ensure_ascii=False).encode("utf-8"),
            wache=wache),
        name=f"nichtstream-{rid}")

    async def bei_disconnect() -> None:
        """R3a: wartende Aufgabe canceln, Job aborted, Abort an SGLang."""
        aufgabe.cancel()
        _beende_abgebrochen(deps, job)
        await _abort_best_effort(deps, rid)

    if wache is not None:
        wache.setze_handler(bei_disconnect)
        wache.start()

    fertig, _ = await asyncio.wait({aufgabe},
                                   timeout=deps.config.heartbeat_s)
    if not fertig:
        return StreamingResponse(_nichtstream_langsam(deps, aufgabe, wache),
                                 media_type="application/json")
    if wache is not None:
        wache.stop()
        if wache.getroffen:
            # Client weg: die Wache hat die Aufgabe gecancelt und den Job
            # abgeschlossen; die Antwort erreicht ihn nicht mehr.
            return _client_weg_antwort()
    ergebnis = aufgabe.result()
    if isinstance(ergebnis, httpx.TransportError):
        return Response(_BODY_NICHT_ERREICHBAR, status_code=502,
                        media_type="application/json")
    if isinstance(ergebnis, Exception):
        # Kein Netzproblem, sondern ein unerwarteter Fehler: der Job ist
        # bereits error, der Bug soll aber sichtbar bleiben (500) statt als
        # irrefuehrendes "sglang nicht erreichbar" zu verschwinden.
        raise ergebnis
    return Response(ergebnis.content, status_code=ergebnis.status_code,
                    headers=_response_headers(ergebnis))


async def _passthrough(request: Request, deps: Deps, roh: bytes) -> Response:
    """Getreuer, gepufferter Passthrough fuer GET und Nicht-JSON-POSTs.

    Status und Body kommen unveraendert zurueck, ohne Job und ohne
    Keepalives; die getrackten Pfade (SSE-Relay, Nicht-Stream mit Jobs)
    liegen daneben.
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

    GET ohne Body und POSTs ohne JSON-Objekt sind ein gepufferter
    Passthrough ohne Job. JSON-POSTs ohne ``stream=true`` laufen ueber den
    Nicht-Stream-Pfad (Job, WS-Keepalives, 502 bei Verbindungsfehler).
    ``stream=true`` laeuft ueber das Relay: erst die Kopfzeile des Upstreams
    pruefen (Status >= 400 kommt unveraendert zurueck, ohne SSE und ohne
    Job), dann Job anlegen und streamen. Der Job wird erst nach der
    Kopfzeile angelegt; so hinterlaesst keine abgelehnte Anfrage eine
    Zeile in der Registry. Pfade mit Punkt-Segmenten enden sofort mit 404.
    Mit ``abort_on_disconnect`` (Default) wacht je getracktem Request eine
    ``_DisconnectWatcher`` ueber die Client-Verbindung und bricht bei einem
    Disconnect den wartenden Aufruf ab, schliesst den Job als ``aborted``
    ab und meldet den Request bei SGLang ab.
    """
    if _pfad_mit_punktsegment(request.url.path):
        return Response(_BODY_UNGUELTIGER_PFAD, status_code=404,
                        media_type="application/json")
    roh = await request.body()
    koerper = _json_koerper(roh) if request.method == "POST" else None
    if koerper is None:
        return await _passthrough(request, deps, roh)
    if koerper.get("stream") is not True:
        return await _nichtstream(request, deps, koerper)

    rid = ensure_rid(koerper)
    kopf = _upstream_headers(request)
    kopf["content-type"] = "application/json"
    # Als Task, damit die Wache den wartenden Aufruf canceln kann (R3b).
    kopf_aufgabe = asyncio.create_task(
        deps.client.send(
            deps.client.build_request(
                "POST", _upstream_url(request, deps.config), headers=kopf,
                content=json.dumps(koerper, ensure_ascii=False).encode("utf-8"),
                timeout=_timeout(deps.config)),
            stream=True),
        name=f"stream-kopf-{rid}")

    async def vor_kopf_abort() -> None:
        """R3b: den wartenden Kopfzeilen-Aufruf canceln, Abort an SGLang."""
        kopf_aufgabe.cancel()
        await _abort_best_effort(deps, rid)

    wache = _neue_wache(request, deps, rid)
    if wache is not None:
        wache.setze_handler(vor_kopf_abort)
        wache.start()
    uebergeben = False                   # das Relay uebernimmt die Wache
    try:
        try:
            antwort = await kopf_aufgabe
        except asyncio.CancelledError:
            kopf_aufgabe.cancel()
            if wache is None or not wache.getroffen:
                raise                    # kein Disconnect: Cancellation bleibt
            return _client_weg_antwort()

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
        deps.hub.publish("job", job_to_dict(job))
        relay = _Relay(antwort, _JobTracker(deps, job),
                       heartbeat_s=deps.config.heartbeat_s, wache=wache)

        erledigt = False

        async def nach_kopf_abort() -> None:
            """R3c: Relay beenden, Job aborted, Abort an SGLang (einmal)."""
            nonlocal erledigt
            if erledigt:
                return
            erledigt = True
            await relay.beenden()
            _beende_abgebrochen(deps, job)
            await _abort_best_effort(deps, rid)

        relay.start()
        uebergeben = True
        if wache is not None:
            wache.setze_handler(nach_kopf_abort)
            if wache.getroffen:
                # Disconnect genau zwischen Kopfzeile und Relay-Start: die
                # Wache lief noch mit dem Vor-Kopfzeilen-Handler.
                await nach_kopf_abort()
        return StreamingResponse(relay.stream(), status_code=antwort.status_code,
                                 headers=_response_headers(antwort),
                                 media_type="text/event-stream")
    finally:
        if wache is not None and not uebergeben:
            wache.stop()
