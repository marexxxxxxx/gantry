"""FastAPI-App fuer aiserver-monitor: Routen, Static, Lifespan, Client-IP.

``create_app`` verdrahtet Config, Job-Registry, EventHub, History, die
httpx-Clients (gemeinsam fuer den Proxy, je eigene fuer Metriken und
Steuer/Cancel) und (optional) den Metrik-Sammler zu einer App.
Routen: Health, Job-Snapshot, SSE-Ereignisse, Cancel/Notaus, Statistik und der
``/v1``-Passthrough; zuletzt der Static-Mount (oder die Platzhalterseite).

Die Client-IP kommt bevorzugt aus ``CF-Connecting-IP`` (Cloudflare steht vor
dem Gateway): ist der Header eine gueltige IP, ersetzt die Middleware damit die
Peer-IP im Scope; ``proxy._client_source`` liest dann automatisch die echte
Quelle, proxy.py bleibt unberuehrt.
"""

import asyncio
import ipaddress
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.responses import Response, StreamingResponse

from .cancel import abort_request
from .collector import Collector
from .config import Config
from .events import EventHub, format_sse, sse_stream
from .history import History
from .jobs import ACTIVE_STATUSES, JobRegistry, JobStatus, job_to_dict
from .metrics_parse import GpuSample
from .proxy import Deps, handle_v1

logger = logging.getLogger(__name__)

#: Maximales Statistik-Fenster (30 Tage, in Millisekunden).
MAX_FENSTER_MS = 30 * 86400 * 1000

#: Default-Bucket der Statistik in Sekunden (bis die UI etwas anderes schickt).
DEFAULT_BUCKET_S = 60

#: Timeout des dedizierten Steuer-Clients fuer Cancel (Sekunden).
_STEUER_TIMEOUT_S = 5.0

#: Platzhalterseite, solange das Static-Verzeichnis (Webapp-Tasks) fehlt.
_PLATZHALTER_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aiserver-monitor</title>
<style>
  html, body { margin: 0; height: 100%; }
  body { display: flex; align-items: center; justify-content: center;
         background: #0f1115; color: #e6e8eb;
         font-family: system-ui, sans-serif; }
  main { max-width: 30rem; padding: 2rem; }
  h1 { font-size: 1.3rem; font-weight: 600; }
  p { line-height: 1.6; color: #9aa3af; }
  code { color: #e6e8eb; }
</style>
</head>
<body>
<main>
<h1>aiserver-monitor</h1>
<p>Das Dashboard wird mit den Webapp-Tasks gebaut und dann hier ausgeliefert.
Bis dahin l&auml;uft das Gateway schon: Jobs unter <code>/monitor/jobs</code>,
Live-Ereignisse unter <code>/monitor/events</code>.</p>
</main>
</body>
</html>
"""


def _gueltige_ip(wert: object) -> str | None:
    """Getrimmte, gueltige IPv4-/IPv6-Adresse als String, sonst None."""
    if not isinstance(wert, str):
        return None
    try:
        return str(ipaddress.ip_address(wert.strip()))
    except ValueError:
        return None


def _header_wert(headers, name: str) -> object:
    """Header-Wert unabhaengig von der Schreibweise (dict oder Headers)."""
    wert = headers.get(name)
    if wert is not None:
        return wert
    for schluessel, kandidat in headers.items():
        if str(schluessel).lower() == name:
            return kandidat
    return None


def client_source(headers, peer_host: str | None) -> str:
    """Quelle eines Requests: gueltige CF-Connecting-IP, sonst die Peer-IP.

    ``peer_host`` kann None oder leer sein; dann heisst die Quelle
    ``"unbekannt"``. Nur eine syntaktisch gueltige IP im Header gewinnt.
    """
    ip = _gueltige_ip(_header_wert(headers, "cf-connecting-ip"))
    if ip is not None:
        return ip
    if peer_host:
        return peer_host
    return "unbekannt"


class _CfClientMiddleware:
    """ASGI-Middleware: gueltige CF-Connecting-IP ersetzt die Peer-IP im Scope.

    Nur eine gueltige IP aendert den Scope (Port bleibt, sonst 0); ohne
    gueltigen Header bleibt alles unveraendert. Die Rohform als reine
    ASGI-Middleware haelt den Scope der Route identisch.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            ip = _gueltige_ip(
                _header_wert(Headers(scope=scope), "cf-connecting-ip"))
            if ip is not None:
                alt = scope.get("client")
                scope["client"] = (ip, alt[1] if alt else 0)
        await self.app(scope, receive, send)


def create_app(config: Config, *,
               transport: httpx.AsyncBaseTransport | None = None,
               steuer_transport: httpx.AsyncBaseTransport | None = None,
               gpu_reader: Callable[[], Awaitable[GpuSample]] | None = None,
               enable_background: bool = True) -> FastAPI:
    """Baut die Gateway-App aus Config, optionalem Transport und GPU-Reader.

    ``transport`` ersetzt den httpx-Transport der Clients (Tests),
    ``steuer_transport`` den des dedizierten Steuer-Clients; ohne Angabe
    faellt der Steuer-Client auf ``transport`` zurueck. ``gpu_reader``
    ersetzt den nvidia-smi-Aufruf des Sammlers. ``enable_background=False``
    startet keinen Collector (Tests). Die Lifespan initialisiert und raeumt
    die History auf, markiert haengengebliebene Jobs als error und
    startet/stoppt den Collector.
    """
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)

    registry = JobRegistry(preview_max_chars=config.preview_max_chars)
    hub = EventHub()
    history = History(config.db_path,
                      retention_jobs_days=config.retention_jobs_days,
                      retention_series_days=config.retention_series_days)
    client = httpx.AsyncClient(transport=transport)
    # Der Sammler bekommt einen EIGENEN Client (eigener Pool): sind die
    # Proxy-Verbindungen voll (wartende Jobs), laufen die Metrik-GETs nicht
    # mehr in den PoolTimeout und das Dashboard meldet SGLang nicht
    # faelschlich als nicht erreichbar.
    collector_client = httpx.AsyncClient(transport=transport)
    # Steuer-/Cancel-Aufrufe bekommen ebenfalls einen EIGENEN Client mit
    # eigenem Pool und explizitem Timeout: sind die Proxy-Verbindungen voll
    # (wartende Relay-Jobs), wuerde der Abbruch sonst im PoolTimeout enden
    # und das Dashboard faelschlich "Abbruch nicht moeglich" melden.
    steuer_client = httpx.AsyncClient(
        transport=transport if steuer_transport is None else steuer_transport,
        timeout=httpx.Timeout(_STEUER_TIMEOUT_S))
    deps = Deps(
        config=config,
        registry=registry,
        hub=hub,
        history=history,
        client=client,
    )
    collector = Collector(deps, gpu_reader=gpu_reader, client=collector_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        jetzt = time.time()
        history.init()
        jobs_weg, samples_weg = history.cleanup(jetzt)
        logger.info("Start-Cleanup: %s Jobs und %s Samples geloescht",
                    jobs_weg, samples_weg)
        stale = history.mark_stale_running(jetzt)
        logger.info("Start-Recovery: %s aktive Jobs als error markiert", stale)
        task = None
        if enable_background:
            task = asyncio.create_task(collector.run(), name="collector")
        app.state.collector_task = task
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await client.aclose()
            await collector_client.aclose()
            await steuer_client.aclose()
            history.close()

    app = FastAPI(title="aiserver-monitor", lifespan=lifespan)
    app.add_middleware(_CfClientMiddleware)
    app.state.deps = deps
    app.state.collector_task = None
    app.state.collector_client = collector_client
    app.state.steuer_client = steuer_client

    async def _abbrechen(*, rid: str | None, abort_all: bool) -> bool:
        """``abort_request`` ueber den Steuer-Client: Netzfehler -> False.

        Der dedizierte Steuer-Client hat einen eigenen Pool, damit ein
        gesaettigter Proxy-Pool den Abbruch nicht blockiert (PoolTimeout).
        """
        try:
            return await abort_request(steuer_client, config.upstream_base,
                                       rid=rid, abort_all=abort_all)
        except httpx.HTTPError as exc:
            logger.warning(
                "Abbruch am Upstream fehlgeschlagen (rid=%r, abort_all=%s):"
                " %s: %s", rid, abort_all, type(exc).__name__, exc)
            return False

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/monitor/jobs")
    async def monitor_jobs() -> dict:
        return {"jobs": registry.snapshot()}

    @app.get("/monitor/events")
    async def monitor_events() -> StreamingResponse:
        def snapshot_frames() -> list[str]:
            """Snapshot erst beim Abo bauen (kein verlorenes Live-Ereignis)."""
            return [format_sse("snapshot", {
                "jobs": registry.snapshot(),
                "stats": history.last_sample(),
            })]

        return StreamingResponse(
            sse_stream(hub, snapshot_frames, config.heartbeat_s),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/monitor/jobs/{rid}/cancel")
    async def monitor_cancel_job(rid: str) -> dict:
        # R5: erst das lokale Abbruch-Signal (der Non-Stream-Pfad beendet
        # damit seinen Lauf wirklich), dann best effort der Abort am
        # Upstream. Ein unbekannter rid hat kein Signal und bleibt No-op.
        signal = registry.abort_signal(rid)
        if signal is not None:
            signal.set()
        ok = await _abbrechen(rid=rid, abort_all=False)
        if ok:
            job = registry.get(rid)
            if job is not None and job.status in ACTIVE_STATUSES:
                registry.finish(rid, JobStatus.ABORTED, now=time.time())
                hub.publish("job", job_to_dict(job))
        return {"ok": ok}

    @app.post("/monitor/cancel_all")
    async def monitor_cancel_all() -> dict:
        ok = await _abbrechen(rid=None, abort_all=True)
        if ok:
            jetzt = time.time()
            for eintrag in registry.snapshot():
                if eintrag["status"] not in ACTIVE_STATUSES:
                    continue
                job = registry.get(eintrag["rid"])
                registry.finish(job.rid, JobStatus.ABORTED, now=jetzt)
                hub.publish("job", job_to_dict(job))
        return {"ok": ok}

    @app.get("/monitor/stats")
    async def monitor_stats(
            from_ms: int | None = Query(None, alias="from"),
            to_ms: int | None = Query(None, alias="to"),
            bucket: int = Query(DEFAULT_BUCKET_S)):
        """Statistik; ``from``/``to`` sind Millisekunden, ``bucket`` Sekunden."""
        if bucket <= 0:
            return JSONResponse(
                status_code=400,
                content={"error": "bucket muss positiv sein (Sekunden)"})
        jetzt_ms = int(time.time() * 1000)
        if to_ms is None:
            to_ms = jetzt_ms
        if from_ms is None:
            from_ms = to_ms - 24 * 60 * 60 * 1000
        if from_ms > to_ms:
            return JSONResponse(
                status_code=400,
                content={"error": "from liegt nach to"})
        if to_ms - from_ms > MAX_FENSTER_MS:
            from_ms = to_ms - MAX_FENSTER_MS
        return history.query_stats(from_ms / 1000.0, to_ms / 1000.0, bucket)

    @app.api_route("/v1/{pfad:path}", methods=["GET", "POST"])
    async def v1_passthrough(pfad: str, request: Request) -> Response:
        """Reicht die Anfrage weiter; ``pfad`` dient nur dem Routing."""
        return await handle_v1(request, deps)

    static_verzeichnis = Path(__file__).parent / "static"
    if static_verzeichnis.is_dir():
        # Zuletzt: der Mount faengt alles ab, was keine Route matcht.
        app.mount("/", StaticFiles(directory=str(static_verzeichnis),
                                   html=True), name="static")
    else:
        @app.get("/")
        async def index() -> HTMLResponse:
            return HTMLResponse(_PLATZHALTER_HTML)

    return app
