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
Voller Prompt und Vorschauen bleiben im RAM. POSTs auf ``chat/completions``
mit JSON-Body und ``stream != true`` werden intern als Stream abgewickelt:
der Upstream bekommt eine Body-Kopie mit ``stream=true`` und
``stream_options.include_usage``, die SSE-Antwort wird live verfolgt und am
Ende zu einem OpenAI-ChatCompletion-JSON reassembliert (bewusst ohne das
SGLang-``meta_info``). Ist das Ergebnis innerhalb von ``heartbeat_s`` da,
geht es als einzelne Response ohne Leerzeichen raus; sonst folgen je
Heartbeat Leerzeichen (WS-Keepalive, abschaltbar) vor dem JSON. Ein
Abbruch-Signal je Job (Cancel-Route) beendet den lokalen Lauf wirklich: 499
mit ``aborted``-JSON vor dem Antwortstart, sonst dasselbe JSON hinter den
Leerzeichen. Andere JSON-POSTs, GETs und POSTs ohne JSON-Objekt bleiben der
getreue, gepufferte Byte-Passthrough ohne Job. ``/v1/embeddings`` ist die
Ausnahme: Es geht immer an ``embed_base`` (eigener CPU-Ollama) statt an
SGLang; bekannte Chat-Modellnamen werden auf ``embed_model`` abgebildet.
Faellt der Upstream vor dem Antwortstart aus, kommt 502; ein spaeter
erkannter Transportfehler steht als 502-Koerper hinter den Leerzeichen.
Anfragen mit Punkt-Segmenten im Pfad (``.``/``..``) werden vor jedem
Upstream-Kontakt mit 404 abgewiesen: httpx wuerde sie normalisieren und
damit SGLang-Admin-Routen am Job-Tracking vorbei erreichbar machen.

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
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from .cancel import abort_request
from .config import Config
from .events import EventHub, format_comment
from .hindsight import HindsightBlocker
from .history import History
from .jobs import ACTIVE_STATUSES, Job, JobRegistry, JobStatus, job_to_dict
from .priority import PriorityManager
from .sse_parse import SSEByteParser, summarize_chunk

logger = logging.getLogger(__name__)

#: Hoechstens ein Live-Ereignis je Job in diesem Abstand (max. 4/s).
PUBLISH_ABSTAND_S = 0.25

#: Abstand der Disconnect-Pruefung je Watcher-Task (Sekunden).
_DISCONNECT_CHECK_S = 1.0

#: Keepalive-Kommentar, wie er zwischen den Original-Bytes zum Client geht.
_PING = format_comment("ping").encode("utf-8")

#: Pfad-Suffix, dessen Non-Stream-Anfragen intern gestreamt werden (R1).
_CHAT_COMPLETIONS = "chat/completions"

#: Antwortkoerper, wenn der Upstream nicht erreichbar ist (SGLang aus).
_BODY_NICHT_ERREICHBAR = (
    b'{"error": {"message": "sglang nicht erreichbar", '
    b'"type": "upstream_unavailable"}}')

#: Antwortkoerper eines lokal abgebrochenen Jobs (Cancel-Route).
_BODY_ABGEBROCHEN = (
    b'{"error": {"message": "Job wurde abgebrochen", "type": "aborted"}}')

#: Antwortkoerper, solange Hindsight im Dashboard blockiert ist.
_BODY_HINDSIGHT_BLOCKIERT = (
    b'{"error": {"message": "Hindsight ist voruebergehend blockiert", '
    b'"type": "hindsight_blocked"}}')

#: Antwortkoerper auf Pfade mit Punkt-Segmenten (Traversal-Versuch).
_BODY_UNGUELTIGER_PFAD = (
    b'{"error": {"message": "ungueltiger Pfad", '
    b'"type": "invalid_path"}}')

#: Zeitlimit der Backend-Probe (``/api/version``): lokal und billig, aber
#: ein haengender Upstream darf den Request nicht aufhalten.
_PROBE_TIMEOUT_S = 1.0

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
    priority: PriorityManager | None = None
    hindsight: HindsightBlocker | None = None
    backend: "BackendProbe | None" = None


class BackendProbe:
    """Erkennt, welches Ollama-Backend (Flint/GPT-OSS) gerade laeuft.

    SGLang hat ``/api/ps`` nicht (404). Ollama listet dort das geladene
    Modell; die Startskripte laden ihr Modell mit ``keep_alive=-1`` vor,
    damit die Zuordnung sofort stimmt. Zusaetzlich zaehlt das PID-File des
    Backends, falls das Modell (noch) nicht geladen ist. Das Ergebnis wird
    ``ttl_s`` lang gecacht: die Abfrage laeuft je Request, ein Modellwechsel
    (Sekunden) soll aber nicht bei jedem Token erneut messen. Jeder Fehler
    und "kein Treffer" bedeuten None - dann bleibt der Modelname unveraendert
    (Qwen).
    """

    def __init__(self, config: Config, client: httpx.AsyncClient, *,
                 repo_root: Path | None = None,
                 ttl_s: float = 2.0,
                 clock: Callable[[], float] = time.monotonic):
        self._config = config
        self._client = client
        self._repo_root = repo_root
        self._ttl_s = ttl_s
        self._clock = clock
        self._stand: str | None = None
        self._gemessen: float | None = None

    def _pidfile_modell(self) -> str | None:
        """Backend aus dem PID-File, wenn der Prozess noch lebt."""
        if self._repo_root is None:
            return None
        for pfad, modell in (
                (self._config.flint_pidfile, self._config.flint_model),
                (self._config.gptoss_pidfile, self._config.gptoss_model)):
            try:
                pid = int((self._repo_root / pfad).read_text().strip())
                os.kill(pid, 0)
            except (OSError, ValueError):
                continue
            return modell
        return None

    async def aktives_modell(self) -> str | None:
        jetzt = self._clock()
        if self._gemessen is not None \
                and jetzt - self._gemessen < self._ttl_s:
            return self._stand
        modell = self._pidfile_modell()
        if modell is None:
            basis = self._config.upstream_base.rstrip("/")
            try:
                antwort = await self._client.get(
                    f"{basis}/api/ps", timeout=_PROBE_TIMEOUT_S)
                if antwort.status_code == 200:
                    geladen = {
                        eintrag.get("name")
                        for eintrag in antwort.json().get("models", [])
                        if isinstance(eintrag, dict)
                    }
                    for bekannt in (self._config.flint_model,
                                    self._config.gptoss_model):
                        if bekannt in geladen:
                            modell = bekannt
                            break
            except Exception as exc:
                logger.debug("Backend-Probe fehlgeschlagen: %s: %s",
                             type(exc).__name__, exc)
        self._stand = modell
        self._gemessen = jetzt
        return modell


def _ist_completion_pfad(pfad: str) -> bool:
    """Chat- und Text-Completion: nur dort wird der Modelname gemappt."""
    return pfad.endswith(("/chat/completions", "/completions"))


def _ist_embedding_pfad(pfad: str) -> bool:
    """Embeddings laufen im CPU-Sidecar, nicht im Chat-Backend."""
    return pfad.endswith("/embeddings")


def _embedding_modell_anpassen(deps: "Deps", koerper: dict) -> bool:
    """Bildet bekannte Modellnamen auf ``embed_model`` ab.

    Es gibt genau ein Embedding-Modell. Clients duerfen deshalb den
    Chat-Alias, den Flint-/GPT-OSS-Namen oder gar keinen Namen schicken.
    Unbekannte Namen bleiben unveraendert: das Sidecar antwortet dann mit
    einem klaren "model not found", statt still ein anderes Modell zu
    benutzen. Rueckgabe: True, wenn der Body geaendert wurde.
    """
    bekannt = {
        name for name in (deps.config.model_alias,
                          deps.config.flint_model,
                          deps.config.gptoss_model,
                          deps.config.embed_model)
        if name
    }
    modell = koerper.get("model")
    if modell and modell not in bekannt:
        return False
    if modell == deps.config.embed_model:
        return False
    koerper["model"] = deps.config.embed_model
    return True


async def _modell_anpassen(deps: "Deps", koerper: dict, *,
                           chat: bool = False) -> bool:
    """Bildet bekannte Modellnamen auf das aktive Backend ab.

    Angenommen werden Alias, Flint- und GPT-OSS-Modell: Der Schalter im
    Dashboard ist autoritativ, weil ohnehin nur ein Ollama-Modell ins VRAM
    passt. Clients duerfen also weiterhin ihren gewohnten Namen schicken
    (Hindsight schickt je nach Lauf Alias, Flint- oder GPT-OSS-Namen).
    Unbekannte Modelle gehen unveraendert weiter; laeuft Qwen, bleibt alles
    unveraendert. Fuer GPT-OSS setzt das Gateway ausserdem die
    Hindsight-Defaults ``temperature: 0`` und ``reasoning_effort: "low"`` -
    nur wenn der Client sie nicht selbst mitschickt. Rueckgabe: True, wenn
    der Body geaendert wurde.
    """
    if deps.backend is None:
        return False
    bekannt = {
        name for name in (deps.config.model_alias,
                          deps.config.flint_model,
                          deps.config.gptoss_model)
        if name
    }
    if koerper.get("model") not in bekannt:
        return False
    ziel = await deps.backend.aktives_modell()
    if ziel is None:
        return False
    geaendert = koerper["model"] != ziel
    koerper["model"] = ziel
    if ziel == deps.config.gptoss_model:
        if "temperature" not in koerper:
            koerper["temperature"] = 0
            geaendert = True
        if chat and "reasoning_effort" not in koerper:
            koerper["reasoning_effort"] = "low"
            geaendert = True
    return geaendert


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


def _upstream_url(request: Request, config: Config, *,
                  base: str | None = None) -> str:
    """Upstream-URL aus Basis, Originalpfad und Query."""
    url = (base or config.upstream_base).rstrip("/") + request.url.path
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


def _stream_body(koerper: dict) -> bytes:
    """Body-Kopie fuer den internen Chat-Stream (Ruling R2).

    ``stream`` wird auf true gesetzt und ``stream_options.include_usage``
    gemergt (vorhandene Optionen bleiben), damit SGLang die usage im letzten
    SSE-Chunk mitschickt. Sonst bleiben die Felder unveraendert; die rid hat
    ``ensure_rid`` vorher in den Originalbody geschrieben.
    """
    kopie = dict(koerper)
    kopie["stream"] = True
    optionen = kopie.get("stream_options")
    if isinstance(optionen, dict):
        kopie["stream_options"] = {**optionen, "include_usage": True}
    else:
        kopie["stream_options"] = {"include_usage": True}
    return json.dumps(kopie, ensure_ascii=False).encode("utf-8")


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


def _hindsight_antwort() -> Response:
    """503 fuer einen blockierten Hindsight-Request (kein Job, kein Upstream)."""
    return Response(_BODY_HINDSIGHT_BLOCKIERT, status_code=503,
                    headers={"Retry-After": "60"},
                    media_type="application/json")


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
    Client-Abbruch sauber. Mit ``sammeln`` legt der Tracker zusaetzlich den
    vollen Text und die rohe usage ab: die Ringe sind gekuerzt, der
    Non-Stream-Pfad braucht aber den kompletten Inhalt fuer die
    reassemblierte ChatCompletion-Antwort.
    """

    def __init__(self, deps: Deps, job: Job, *, sammeln: bool = False):
        self.job = job
        self._deps = deps
        self._sammeln = sammeln
        self._parser = SSEByteParser()
        self._last_publish: float | None = None
        self._finish_reason: str | None = None
        self._inhalt_gesehen = False
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        #: Voller Text bzw. letzte rohe usage (nur mit ``sammeln``).
        self.content_teile: list[str] = []
        self.reasoning_teile: list[str] = []
        self.usage: dict | None = None

    @property
    def rid(self) -> str:
        return self.job.rid

    @property
    def finish_reason(self) -> str | None:
        """Letztes gesehenes ``finish_reason`` (None: keins gesehen)."""
        return self._finish_reason

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
                if self._sammeln:
                    if summary.content:
                        self.content_teile.append(summary.content)
                    if summary.reasoning:
                        self.reasoning_teile.append(summary.reasoning)
            if self._sammeln:
                usage = obj.get("usage")
                if isinstance(usage, dict) and usage:
                    self.usage = usage
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


class _InternerStream:
    """Interner Upstream-Stream einer Non-Stream-Chat-Anfrage.

    Die Aufgabe stellt den Upstream-Aufruf mit ``stream=true`` und liest die
    SSE-Antwort bis zum Ende; der ``_JobTracker`` (``sammeln=True``) misst
    mit und sammelt den vollen Text. Daraus baut die Aufgabe am Ende die
    reassemblierte ChatCompletion-Antwort (Ruling R4). ``beenden`` ist das
    Analogon zu ``_Relay.beenden``: Cancelt die Lesetask, wartet ihr Ende ab
    und schliesst die Upstream-Antwort. Den Job schliesst in diesem Fall der
    Aufrufer ab (``_beende_abgebrochen``); nach einem sauberen Lauf
    schliesst ihn die Lesetask selbst (``close_clean``, genau ein
    History-Eintrag).
    """

    def __init__(self, deps: Deps, job: Job, *, url: str, kopf: dict[str, str],
                 inhalt: bytes, model: str):
        self.job = job
        self._deps = deps
        self._url = url
        self._kopf = kopf
        self._inhalt = inhalt
        self._model = model
        self._tracker = _JobTracker(deps, job, sammeln=True)
        self._antwort: httpx.Response | None = None
        self._aufgabe: asyncio.Task | None = None
        #: Fehler des Upstream-Aufrufs/Reads (None: sauber durchgelaufen).
        self.fehler: BaseException | None = None
        #: Antwortstatus >= 400 samt unveraendertem Body und Kopfzeilen.
        self.kopf_status: int | None = None
        self.kopf_body: bytes = b""
        self.kopf_headers: dict[str, str] = {}
        #: Fertige Client-Antwort, sobald der Lauf durch ist (Ruling R4).
        self.body: bytes | None = None

    def start(self) -> asyncio.Task:
        """Startet die Lesetask (einmal je Anfrage) und gibt sie zurueck."""
        self._aufgabe = asyncio.create_task(
            self._lauf(), name=f"nichtstream-live-{self.job.rid}")
        return self._aufgabe

    async def beenden(self) -> None:
        """Cancelt die Lesetask, wartet ihr Ende ab, schliesst die Antwort.

        Deckt auch eine Lesetask ab, die vor ihrem ersten Schritt gecancelt
        wurde: dann gibt es keine Upstream-Antwort zu schliessen. Der Job
        wird hier nicht angefasst (das macht ``_beende_abgebrochen``); ein
        doppelter Aufruf ist harmlos.
        """
        aufgabe = self._aufgabe
        if aufgabe is None:
            return
        aufgabe.cancel()
        await asyncio.wait({aufgabe})
        if self._antwort is not None:
            await self._antwort.aclose()

    async def _lauf(self) -> None:
        """Upstream-Aufruf mit ``stream=true``; SSE bis zum Ende lesen."""
        try:
            antwort = await self._deps.client.send(
                self._deps.client.build_request(
                    "POST", self._url, headers=self._kopf,
                    content=self._inhalt, timeout=_timeout(self._deps.config)),
                stream=True)
        except Exception as exc:              # Verbindungsfehler vor Phase 2
            logger.warning("Upstream-Fehler bei rid=%s: %s: %s",
                           self.job.rid, type(exc).__name__, exc)
            self.fehler = exc
            _finish_job(self._deps, self.job, JobStatus.ERROR,
                        error=f"{type(exc).__name__}: {exc}")
            return
        self._antwort = antwort
        if antwort.status_code >= 400:
            # Ablehnung vor jedem SSE-Delta: Status und Body unveraendert.
            await antwort.aread()
            self.kopf_status = antwort.status_code
            self.kopf_body = antwort.content
            self.kopf_headers = _response_headers(antwort)
            await antwort.aclose()
            _finish_job(self._deps, self.job, JobStatus.ERROR,
                        error=f"upstream {self.kopf_status}")
            return
        try:
            async for chunk in antwort.aiter_bytes():
                self._tracker.absorb(chunk)
        except Exception as exc:              # Upstream weg/mittendrin kaputt
            logger.warning("Upstream-Abbruch bei rid=%s: %s: %s",
                           self.job.rid, type(exc).__name__, exc)
            self.fehler = exc
            self._tracker.close(JobStatus.ERROR,
                                error=f"{type(exc).__name__}: {exc}")
        else:
            self.body = self._body_bauen()
            self._tracker.close_clean()
        finally:
            try:
                await antwort.aclose()
            except Exception:                 # Schliessen darf die Aufgabe
                logger.warning("Antwort-Close fehlgeschlagen bei rid=%s",
                               self.job.rid, exc_info=True)

    def _body_bauen(self) -> bytes:
        """Reassemblierte ChatCompletion-Antwort (Ruling R4).

        ``content``/``reasoning_content`` sind der volle gesammelte Text,
        ``finish_reason`` das gemerkte oder ``"stop"``. ``usage`` kommt roh
        vom Upstream und fehlt ganz, wenn keine usage ankam (nie 0
        erfinden); das SGLang-``meta_info`` wird bewusst nicht uebernommen.
        """
        antwort = {
            "id": self.job.rid,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self._model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(self._tracker.content_teile),
                    "reasoning_content": "".join(self._tracker.reasoning_teile),
                },
                "finish_reason": self._tracker.finish_reason or "stop",
            }],
        }
        if self._tracker.usage is not None:
            antwort["usage"] = self._tracker.usage
        return json.dumps(antwort, ensure_ascii=False).encode("utf-8")


async def _nichtstream(request: Request, deps: Deps, koerper: dict, *,
                       hindsight: bool = False) -> Response:
    """POST ``chat/completions`` mit ``stream != true``: interner Stream.

    Der Job entsteht **vor** dem Upstream-Aufruf und wird sofort als
    ``waiting`` publiziert; fehlt die rid, kommt eine neue in den Body (wie
    im Relay, fuer Registry und ``/abort_request``). Der Upstream bekommt
    eine Body-Kopie mit ``stream=true`` und ``stream_options.include_usage``;
    die SSE-Antwort wird live verfolgt und zu einem ChatCompletion-JSON
    reassembliert (Ruling R4, bewusst ohne SGLang-``meta_info``). Innerhalb
    von ``heartbeat_s`` fertig heisst einzelne Response ohne Leerzeichen;
    sonst kommen je Heartbeat Leerzeichen vor das JSON
    (``nonstream_keepalive``). Ein Abbruch-Signal der Cancel-Route beendet
    den lokalen Lauf wirklich: 499 mit ``aborted``-JSON vor dem
    Antwortstart, sonst dasselbe JSON hinter den Leerzeichen. Nur ein
    Verbindungsfehler vor dem Antwortstart ergibt 502. Mit
    ``abort_on_disconnect`` (Default) wacht eine Wache ueber die Verbindung
    wie bisher: bricht der Client ab, wird der laufende Task gecancelt, der
    Job als ``aborted`` abgeschlossen und der Request best effort bei SGLang
    abgemeldet.
    """
    rid = ensure_rid(koerper)
    model = koerper.get("model")
    if not isinstance(model, str):
        model = ""
    job = deps.registry.register(
        rid,
        source=_client_source(request),
        model=model,
        full_prompt=_extract_full_prompt(koerper),
        now=time.time(),
        hindsight=hindsight,
    )
    deps.hub.publish("job", job_to_dict(job))

    abbruch = deps.registry.abort_signal(rid)
    if abbruch is None:                       # nach ``register`` unerreichbar
        abbruch = asyncio.Event()
    kopf = _upstream_headers(request)
    kopf["content-type"] = "application/json"
    verlauf = _InternerStream(
        deps, job, url=_upstream_url(request, deps.config), kopf=kopf,
        inhalt=_stream_body(koerper), model=model)
    aufgabe = verlauf.start()

    async def bei_abbruch() -> None:
        """Lauf beenden, Job ``aborted``, Abort an SGLang (best effort).

        Wird vom Abbruch-Signal und vom Disconnect-Handler aufgerufen;
        mehrfacher Aufruf ist unschaedlich (first-wins in der Registry,
        ``abort_request`` ist idempotent).
        """
        await verlauf.beenden()
        _beende_abgebrochen(deps, job)
        await _abort_best_effort(deps, rid)

    wache = _neue_wache(request, deps, rid)
    if wache is not None:
        wache.setze_handler(bei_abbruch)
        wache.start()

    def wache_zu() -> bool:
        """Wache beenden; True, wenn der Client weg ist (Disconnect)."""
        if wache is None:
            return False
        wache.stop()
        return wache.getroffen

    def abgebrochen_antwort() -> Response:
        """499 mit ``aborted``-JSON (Antwort war noch nicht gestartet)."""
        return Response(_BODY_ABGEBROCHEN, status_code=499,
                        media_type="application/json")

    signal = asyncio.create_task(abbruch.wait(), name=f"abbruch-warten-{rid}")
    uebergeben = False
    try:
        bereit, _ = await asyncio.wait(
            {aufgabe, signal}, timeout=deps.config.heartbeat_s,
            return_when=asyncio.FIRST_COMPLETED)
        if aufgabe in bereit:
            if wache_zu():
                return _client_weg_antwort()
            if signal in bereit:
                await bei_abbruch()
                return abgebrochen_antwort()
            if verlauf.kopf_status is not None:
                return Response(verlauf.kopf_body,
                                status_code=verlauf.kopf_status,
                                headers=verlauf.kopf_headers)
            if isinstance(verlauf.fehler, httpx.TransportError):
                return Response(_BODY_NICHT_ERREICHBAR, status_code=502,
                                media_type="application/json")
            if verlauf.fehler is not None:
                # Kein Netzproblem, sondern ein unerwarteter Fehler: der Job
                # ist bereits error, der Bug soll aber sichtbar bleiben (500)
                # statt als "sglang nicht erreichbar" zu verschwinden.
                raise verlauf.fehler
            return Response(verlauf.body, media_type="application/json")
        if signal in bereit:
            await bei_abbruch()
            if wache_zu():
                return _client_weg_antwort()
            return abgebrochen_antwort()

        async def langsam() -> AsyncIterator[bytes]:
            """Client-Seite: Leerzeichen je Heartbeat, dann das fertige JSON.

            Je Heartbeat ohne Ergebnis geht ein einzelnes Leerzeichen raus
            (nur wenn ``config.nonstream_keepalive``); den Job schliesst die
            Lesetask ab, nicht dieser Generator. Ein Transportfehler nach dem
            Antwortstart kommt als 502-Koerper hinter den Leerzeichen, damit
            der Gesamtbody parsebares JSON bleibt. Ein Abbruch-Signal liefert
            das ``aborted``-JSON hinter den Leerzeichen. Hat die ``wache``
            einen Disconnect erkannt, endet der Generator ohne Body (der Job
            ist schon abgeschlossen und die Lesetask gecancelt). Schliesst
            Starlette den Generator stattdessen selbst (Client weg nach dem
            Antwortstart), raeumt das ``finally`` best effort ab, falls die
            Wache den Disconnect nicht mehr gesehen hat.
            """
            beendet_normal = False
            try:
                while True:
                    bereit, _ = await asyncio.wait(
                        {aufgabe, signal}, timeout=deps.config.heartbeat_s,
                        return_when=asyncio.FIRST_COMPLETED)
                    if signal in bereit:
                        await bei_abbruch()
                        beendet_normal = True
                        yield _BODY_ABGEBROCHEN
                        return
                    if aufgabe in bereit:
                        beendet_normal = True
                        break
                    if deps.config.nonstream_keepalive:
                        yield b" "
                if wache_zu():
                    return           # Client weg: die Wache hat aufgeraeumt
                if verlauf.kopf_status is not None:
                    yield verlauf.kopf_body
                    return
                if isinstance(verlauf.fehler, httpx.TransportError):
                    yield _BODY_NICHT_ERREICHBAR
                    return
                if verlauf.fehler is not None:
                    return           # Log und Job macht die Lesetask
                yield verlauf.body
            finally:
                treffer = wache_zu()
                if (wache is not None and not beendet_normal and not treffer
                        and job.status in ACTIVE_STATUSES):
                    # Der Generator wurde von aussen geschlossen (Client
                    # weg), bevor die Wache den Disconnect gesehen hat:
                    # best effort aufraeumen. Nie awaiten
                    # (GeneratorExit-/Cancel-Kontext); bei_abbruch ist
                    # idempotent und first-wins.
                    asyncio.create_task(bei_abbruch(),
                                        name=f"dc-cleanup-{rid}")
                signal.cancel()

        uebergeben = True
        return StreamingResponse(langsam(), media_type="application/json")
    finally:
        if not uebergeben:
            signal.cancel()
            if wache is not None:
                # Auch bei einem Abbruch von aussen (Server-Shutdown): die
                # Wache darf nicht als Zombie weiterpollend uebrig bleiben.
                wache.stop()


async def _passthrough(request: Request, deps: Deps, roh: bytes, *,
                       base: str | None = None) -> Response:
    """Getreuer, gepufferter Passthrough fuer GET und Nicht-JSON-POSTs.

    Status und Body kommen unveraendert zurueck, ohne Job und ohne
    Keepalives; die getrackten Pfade (SSE-Relay, Nicht-Stream mit Jobs)
    liegen daneben. ``base`` ueberschreibt den Upstream (Embeddings).
    """
    antwort = await deps.client.request(
        request.method,
        _upstream_url(request, deps.config, base=base),
        headers=_upstream_headers(request),
        content=roh if roh else None,
        timeout=_timeout(deps.config),
    )
    return Response(antwort.content, status_code=antwort.status_code,
                    headers=_response_headers(antwort))


async def handle_v1(request: Request, deps: Deps) -> Response:
    """Bedient jede ``/v1/*``-Anfrage des Gateways.

    GET ohne Body und POSTs ohne JSON-Objekt sind ein gepufferter
    Passthrough ohne Job. Nur JSON-POSTs auf ``chat/completions`` mit
    ``stream != true`` laufen ueber den internen Stream (Job, Live-Tracking,
    Keepalives, 502 bei Verbindungsfehler); andere JSON-POSTs bleiben der
    Byte-Passthrough ohne Job. ``stream=true`` laeuft ueber das Relay: erst
    die Kopfzeile des Upstreams pruefen (Status >= 400 kommt unveraendert
    zurueck, ohne SSE und ohne Job), dann Job anlegen und streamen. Der Job
    wird erst nach der Kopfzeile angelegt; so hinterlaesst keine abgelehnte
    Anfrage eine Zeile in der Registry. Pfade mit Punkt-Segmenten enden
    sofort mit 404. Mit ``abort_on_disconnect`` (Default) wacht je
    getracktem Request eine ``_DisconnectWatcher`` ueber die
    Client-Verbindung und bricht bei einem Disconnect den wartenden Aufruf
    ab, schliesst den Job als ``aborted`` ab und meldet den Request bei
    SGLang ab.
    """
    if _pfad_mit_punktsegment(request.url.path):
        return Response(_BODY_UNGUELTIGER_PFAD, status_code=404,
                        media_type="application/json")
    roh = await request.body()
    koerper = _json_koerper(roh) if request.method == "POST" else None
    if _ist_embedding_pfad(request.url.path):
        # Embeddings gehoeren dem CPU-Sidecar, nie dem Chat-Backend: kein
        # Job, keine Prioritaet, kein Hindsight, keine Keepalives.
        if koerper is not None and _embedding_modell_anpassen(deps, koerper):
            roh = json.dumps(koerper, ensure_ascii=False).encode("utf-8")
        return await _passthrough(request, deps, roh,
                                  base=deps.config.embed_base)
    if koerper is None:
        return await _passthrough(request, deps, roh)
    if _ist_completion_pfad(request.url.path):
        # Ollama kennt den Qwen-Alias nicht; bei aktivem Flint/GPT-OSS
        # umschreiben, bevor Prioritaet/Hindsight/rid den Body sehen.
        geaendert = await _modell_anpassen(
            deps, koerper,
            chat=request.url.path.endswith(_CHAT_COMPLETIONS))
        if geaendert:
            # Der Passthrough (z. B. /v1/completions ohne stream) schickt
            # sonst die alten Bytes weiter.
            roh = json.dumps(koerper, ensure_ascii=False).encode("utf-8")
    ist_hindsight = False
    if request.url.path.endswith(_CHAT_COMPLETIONS):
        # Marker nur auf Chat-Bodies anwenden; andere JSON-POSTs
        # (z. B. Embeddings) bleiben unveraendert.
        if deps.priority is not None:
            deps.priority.mark(koerper)
        if deps.hindsight is not None:
            ist_hindsight = deps.hindsight.matches(koerper)
            if deps.hindsight.blocked(koerper):
                # Blockiert: sofort 503, ohne SGLang-Kontakt und ohne Job.
                return _hindsight_antwort()
    if koerper.get("stream") is not True:
        if request.url.path.endswith(_CHAT_COMPLETIONS):
            return await _nichtstream(request, deps, koerper,
                                      hindsight=ist_hindsight)
        # Ruling R1: nur Chat-Non-Stream wird konvertiert; alle anderen
        # JSON-POSTs bleiben der getreue Byte-Passthrough ohne Job.
        return await _passthrough(request, deps, roh)

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

        if (ist_hindsight and deps.hindsight is not None
                and deps.hindsight.settings().enabled):
            # Der Blocker wurde waehrend des Kopfzeilen-Aufrufs aktiviert:
            # Request bei SGLang abmelden, bevor ein Job entsteht.
            await antwort.aread()
            await antwort.aclose()
            await _abort_best_effort(deps, rid)
            return _hindsight_antwort()

        model = koerper.get("model")
        job = deps.registry.register(
            rid,
            source=_client_source(request),
            model=model if isinstance(model, str) else "",
            full_prompt=_extract_full_prompt(koerper),
            now=time.time(),
            hindsight=ist_hindsight,
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
