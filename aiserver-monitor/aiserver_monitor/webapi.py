"""Provider-API und modellbasiertes Routing fuer das Gateway.

Zwei Aufgaben, bewusst in einem neuen Modul statt in ``proxy.py``:

* :func:`handle_v1_routed` wickelt ``/v1/*``-Anfragen um den bestehenden
  Passthrough: passt ein Modellname exakt zu einem Provider, wird der
  Upstream dieses Providers benutzt (eigener Host/Port) und der Provider bei
  Bedarf automatisch gestartet. Alles andere laeuft unveraendert durch
  ``proxy.handle_v1``.
* :func:`register_provider_routes` haengt die Dashboard-API unter ``/api``
  ein: Providerliste/-detail, PATCH, Start/Stop, Systemwerte und Log-Tail.

Secrets werden nicht ausgegeben: Die Listen- und Detailantworten zeigen nur
die explizit gelisteten Felder und die Provider-Settings; Startbefehle und
Umgebungsvariablen bleiben aussen vor. Einzige Ausnahme ist die lokale
Editor-Route ``GET /api/providers/{id}/edit``, die dem Formular die
Prozessfelder zum Vorbefuellen liefert. Logs stammen ausschliesslich aus der
in der Registry eingetragenen Logdatei.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from . import __version__
from .autoload import ProviderRouter, ProviderUnavailable
from .config import Config
from .proxy import Deps, handle_v1
from .registry import (
    PROVIDER_ID_RE,
    SETTING_DEFAULTS,
    Provider,
    RegistryError,
    delete_provider,
    load_registry,
    save_provider,
)
from .serverctl import (
    ServerConflictError,
    ServerControl,
    ServerControlError,
    read_log_tail,
    standard_repo_root,
)

logger = logging.getLogger(__name__)

#: Felder, die per PATCH direkt am Provider geaendert werden duerfen.
_PATCH_PROVIDER_FELDER = frozenset((
    "name", "kind", "enabled", "host", "port", "model", "health_url",
    "pidfile", "logfile", "workdir", "start_cmd", "stop_cmd",
))

#: Settings, die auch als Top-Level-Feld im PATCH-Body erlaubt sind.
_PATCH_SETTING_FELDER = frozenset(SETTING_DEFAULTS)

#: Felder, die beim Anlegen direkt am Provider gesetzt werden duerfen.
_ADD_PROVIDER_FELDER = _PATCH_PROVIDER_FELDER | {"id"}


def _provider_aus_body(daten: dict[str, Any]) -> Provider:
    """Baut aus einem POST-Body einen validierten :class:`Provider`.

    ``name`` fehlt -> die ID wird zum Anzeigenamen (wie beim Laden einer
    TOML-Datei). Unbekannte Felder sind ein Fehler, damit Tippfehler in der
    Oberflaeche nicht stillschweigend verschwinden. Startbefehle bleiben
    erlaubt, werden aber nie in der oeffentlichen Antwort ausgegeben.
    """
    unbekannt = sorted(set(daten)
                       - _ADD_PROVIDER_FELDER
                       - _PATCH_SETTING_FELDER
                       - {"settings"})
    if unbekannt:
        raise RegistryError(
            f"unbekannte(s) Feld(er): {', '.join(unbekannt)}")
    roh_id = daten.get("id")
    if not isinstance(roh_id, str) or not roh_id.strip():
        raise RegistryError("id fehlt")
    provider_id = roh_id.strip().lower()
    if not PROVIDER_ID_RE.match(provider_id):
        raise RegistryError(
            f"ungueltige id {provider_id!r}: nur Kleinbuchstaben, Ziffern, "
            "'-' und '_' (max. 64 Zeichen)")

    name = daten.get("name")
    if name is None or (isinstance(name, str) and not name.strip()):
        name = provider_id
    if not isinstance(name, str):
        raise RegistryError("name muss ein Text sein")

    roh_settings = daten.get("settings")
    if roh_settings is not None and not isinstance(roh_settings, dict):
        raise RegistryError("settings muss ein Objekt sein")
    einstellungen = dict(SETTING_DEFAULTS)
    einstellungen.update(roh_settings or {})
    for key in _PATCH_SETTING_FELDER:
        if key in daten:
            einstellungen[key] = daten[key]

    felder = {key: daten[key] for key in _ADD_PROVIDER_FELDER
              if key in daten and key not in ("id", "name")}
    if "enabled" in felder and not isinstance(felder["enabled"], bool):
        raise RegistryError("enabled muss true oder false sein")
    return Provider(id=provider_id, name=name,
                    settings=einstellungen, **felder)


def _provider_dict(provider: Provider, *, status: str, uptime: int) -> dict:
    """Oeffentliche Provider-Darstellung (ohne Startbefehle/Secrets)."""
    return {
        "id": provider.id,
        "name": provider.name,
        "kind": provider.kind,
        "enabled": bool(provider.enabled),
        "model": provider.model,
        "host": provider.host,
        "port": int(provider.port or 0),
        "status": status,
        "uptime": int(uptime),
        "settings": dict(provider.settings),
        "auto_load": bool(provider.settings.get("auto_load", True)),
        "idle_unload_min": int(provider.settings.get("idle_unload_min") or 0),
        "exclusive_gpu": bool(provider.settings.get("exclusive_gpu", False)),
        "health_url": provider.health_url,
    }


def _geordnet(providers: dict[str, Provider],
              control: ServerControl) -> list[Provider]:
    """Provider in der Anzeigereihenfolge der Server-Steuerung."""
    order = {name: index for index, name in enumerate(control.backend_names)}
    return sorted(providers.values(),
                  key=lambda p: (order.get(p.id, len(order)), p.id))


async def _zustands_map(router: ProviderRouter) -> tuple[dict[str, str],
                                                        dict[str, dict]]:
    """Zustaende/Metadaten aller Backends (ein Health-Poll je Aufruf)."""
    try:
        return await router.status()
    except Exception as exc:
        logger.warning("Provider-Status nicht ermittelbar: %s: %s",
                       type(exc).__name__, exc)
        return {}, {}


# --- /v1-Routing -----------------------------------------------------------


async def _request_modell(request: Request) -> str | None:
    """Modellname einer Completion-Anfrage, sonst ``None``."""
    if request.method != "POST":
        return None
    if not request.url.path.endswith(("/chat/completions", "/completions")):
        return None
    roh = await request.body()
    try:
        daten = json.loads(roh)
    except (TypeError, ValueError):
        return None
    if not isinstance(daten, dict):
        return None
    model = daten.get("model")
    return model if isinstance(model, str) and model else None


async def _ersetze_modell(request: Request, model: str) -> None:
    """Schreibt ``model`` in den bereits gelesenen Request-Body.

    ``handle_v1`` liest den Body ueber ``request.body()``; Starlette cached ihn
    in ``request._body``. Das Ersetzen dieses Caches haelt Disconnect-Pruefung
    und Streaming-Pfad unveraendert (anders als ein neuer ``receive``-Kanal).
    """
    roh = await request.body()
    try:
        daten = json.loads(roh)
    except (TypeError, ValueError):
        return
    if not isinstance(daten, dict) or daten.get("model") == model:
        return
    daten["model"] = model
    request._body = json.dumps(daten, ensure_ascii=False).encode("utf-8")
    if hasattr(request, "_json"):
        del request._json


def _provider_fehler(exc: ProviderUnavailable) -> JSONResponse:
    """503/Fehlerkoerper fuer einen nicht bedienbaren Provider."""
    inhalt: dict[str, Any] = {
        "error": {
            "message": str(exc),
            "type": "provider_unavailable",
        },
        "provider": exc.provider,
    }
    if exc.log_tail:
        inhalt["log_tail"] = list(exc.log_tail)
    return JSONResponse(status_code=exc.status_code, content=inhalt,
                        headers={"Retry-After": "30"})


def _freigabe_nach_antwort(antwort: Response, router: ProviderRouter,
                           provider_id: str) -> None:
    """Haelt den Request bis zum Ende des Streams aktiv (Idle-Unload-Schutz)."""
    alt = antwort.background

    async def _freigeben() -> None:
        try:
            if alt is not None:
                await alt()
        finally:
            router.release(provider_id)

    if isinstance(antwort, StreamingResponse) \
            and hasattr(antwort.body_iterator, "__aiter__"):
        # Der Background-Task einer StreamingResponse laeuft bei einem
        # Client-Abbruch nicht zuverlaessig; das ``finally`` im Iterator
        # gibt die Lease auch dann frei.
        original = antwort.body_iterator

        async def _iterator():
            try:
                async for teil in original:
                    yield teil
            finally:
                await _freigeben()

        antwort.body_iterator = _iterator()
        antwort.background = None
        return
    antwort.background = BackgroundTask(_freigeben)


def _ist_modellliste_get(request: Request) -> bool:
    """True fuer ``GET /v1/models`` (mit oder ohne Schraegstrich)."""
    return (request.method == "GET"
            and request.url.path.rstrip("/").endswith("/v1/models"))


def _modelle_aus_registry(registry_dir: Path | None = None) -> JSONResponse:
    """OpenAI-Modellliste aus der Provider-Registry.

    Ein Eintrag je aktiviertem Provider: ``id`` ist der Provider-Modellname
    (leer -> Anzeigename), ``owned_by`` die Provider-ID. Ohne aktivierte
    Provider bleibt die Liste leer. Das ist der Rueckfall fuer
    ``GET /v1/models``, wenn kein Backend erreichbar ist.
    """
    try:
        providers = load_registry(registry_dir)
    except (RegistryError, OSError):
        providers = {}
    daten = [
        {
            "id": provider.model or provider.name,
            "object": "model",
            "owned_by": provider.id,
        }
        for provider in providers.values()
        if provider.enabled
    ]
    return JSONResponse({"object": "list", "data": daten})


async def handle_v1_routed(request: Request, deps: Deps,
                           router: ProviderRouter) -> Response:
    """``handle_v1`` mit provider-eigenem Upstream und Auto-Load."""
    model = await _request_modell(request)
    if model is None:
        if _ist_modellliste_get(request):
            # /v1/models darf nie mit 500 enden: ist kein Backend erreichbar,
            # liefert das Gateway die Modellliste aus der Provider-Registry.
            try:
                return await handle_v1(request, deps)
            except (httpx.ConnectError, httpx.ConnectTimeout,
                    httpx.HTTPStatusError):
                logger.warning("Upstream fuer /v1/models nicht erreichbar; "
                               "liefere Registry-Liste aus.")
                return _modelle_aus_registry(router.registry_dir)
        return await handle_v1(request, deps)
    try:
        route = await router.prepare(model)
    except ProviderUnavailable as exc:
        return _provider_fehler(exc)
    if route is None:
        return await handle_v1(request, deps)

    provider = route.provider
    if provider.model and model != provider.model:
        # Der Client hat ID oder Anzeigename geschickt; der Upstream erwartet
        # den echten Modellnamen.
        await _ersetze_modell(request, provider.model)
    route_deps = replace(
        deps, config=replace(deps.config, upstream_base=route.base_url))
    router.acquire(provider.id)
    try:
        antwort = await handle_v1(request, route_deps)
    except Exception:
        router.release(provider.id)
        raise
    _freigabe_nach_antwort(antwort, router, provider.id)
    return antwort


# --- Systemwerte -----------------------------------------------------------


def _int_wert(text: str) -> int | None:
    """Ganzzahl aus nvidia-smi/free, ``N/A`` und Muell ergeben ``None``."""
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def parse_nvidia_smi(ausgabe: str) -> list[dict]:
    """CSV-Ausgabe von nvidia-smi in GPU-Dicts uebersetzen."""
    gpus: list[dict] = []
    for zeile in ausgabe.splitlines():
        teile = [teil.strip() for teil in zeile.split(",")]
        if len(teile) < 6 or not teile[0]:
            continue
        gpus.append({
            "name": teile[0],
            "vram_total_mib": _int_wert(teile[1]),
            "vram_used_mib": _int_wert(teile[2]),
            "vram_free_mib": _int_wert(teile[3]),
            "temperature_c": _int_wert(teile[4]),
            "utilization_pct": _int_wert(teile[5]),
        })
    return gpus


def parse_free(ausgabe: str) -> dict:
    """``free -b`` in RAM-/Swap-Werte (Bytes) uebersetzen."""
    ergebnis: dict[str, Any] = {}
    for zeile in ausgabe.splitlines():
        teile = zeile.split()
        if not teile:
            continue
        art = teile[0].rstrip(":")
        if art not in ("Mem", "Swap") or len(teile) < 4:
            continue
        eintrag = {
            "total_bytes": _int_wert(teile[1]),
            "used_bytes": _int_wert(teile[2]),
            "free_bytes": _int_wert(teile[3]),
        }
        if len(teile) >= 7:
            eintrag["available_bytes"] = _int_wert(teile[6])
        ergebnis["ram" if art == "Mem" else "swap"] = eintrag
    return ergebnis


class SystemProbe:
    """VRAM/RAM/GPU-Temperatur/Disk ohne schwere Abhaengigkeiten.

    ``runner`` ist injizierbar (Tests); er bekommt eine Argumentliste und
    liefert ``(returncode, stdout, stderr)``. Fehlende Tools sind kein Fehler
    der API: das jeweilige Feld bleibt leer und ``errors`` nennt die Ursache.
    """

    def __init__(self, runner=None, *, timeout_s: float = 3.0):
        self._runner = runner or self._run
        self._timeout_s = timeout_s

    async def _run(self, args: list[str]) -> tuple[int, str, str]:
        try:
            prozess = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return 127, "", str(exc)
        try:
            stdout, stderr = await asyncio.wait_for(
                prozess.communicate(), timeout=self._timeout_s)
        except TimeoutError:
            prozess.kill()
            await prozess.wait()
            return 124, "", f"Timeout nach {self._timeout_s:.0f} s"
        return (prozess.returncode or 0,
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"))

    async def snapshot(self) -> dict:
        fehler: list[str] = []
        rc, out, err = await self._runner([
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,memory.free,"
            "temperature.gpu,utilization.gpu",
            "--format=csv,noheader,nounits",
        ])
        gpus = parse_nvidia_smi(out) if rc == 0 else []
        if rc != 0:
            fehler.append(f"nvidia-smi: {err.strip() or f'Exit {rc}'}")

        rc, out, err = await self._runner(["free", "-b"])
        ram = parse_free(out) if rc == 0 else {}
        if rc != 0:
            fehler.append(f"free: {err.strip() or f'Exit {rc}'}")

        try:
            nutzung = shutil.disk_usage("/")
            disk = {
                "mount": "/",
                "total_bytes": nutzung.total,
                "used_bytes": nutzung.used,
                "free_bytes": nutzung.free,
            }
        except OSError as exc:
            disk = {}
            fehler.append(f"disk: {exc}")

        return {
            "gpus": gpus,
            "ram": ram.get("ram", {}),
            "swap": ram.get("swap", {}),
            "disk": disk,
            "errors": fehler,
        }


# --- Routen ----------------------------------------------------------------


def register_provider_routes(
        app: FastAPI, *,
        config: Config,
        server_control: ServerControl,
        router: ProviderRouter,
        system_probe: SystemProbe | None = None,
) -> None:
    """Haengt ``/api/providers*`` und ``/api/system`` an ``app``."""
    probe = system_probe or SystemProbe()
    gestartet = time.monotonic()

    def _provider_oder_404(provider_id: str
                           ) -> tuple[Provider | None, JSONResponse | None]:
        provider = router.providers().get(provider_id)
        if provider is None:
            return None, JSONResponse(
                status_code=404,
                content={"error": f"Provider {provider_id!r} nicht gefunden"})
        return provider, None

    async def _detail(provider: Provider) -> dict:
        states, _meta = await _zustands_map(router)
        return _provider_dict(provider,
                              status=states.get(provider.id, "stopped"),
                              uptime=router.uptime(provider.id))

    def _registry_neu_laden() -> bool:
        """Router-Cache verwerfen und ServerControl neu einlesen."""
        router.reload()
        return bool(server_control.refresh_from_registry(router.registry_dir))

    @app.get("/api/info")
    async def api_info() -> Response:
        """Name, Version und Laufzeit des Gateways (fuer den Header)."""
        return JSONResponse(content={
            "name": "Gantry",
            "version": __version__,
            "uptime_seconds": max(0, int(time.monotonic() - gestartet)),
            "providers": len(router.providers()),
        })

    @app.get("/api/providers")
    async def api_providers() -> Response:
        providers = router.providers()
        states, _meta = await _zustands_map(router)
        daten = [
            _provider_dict(provider,
                           status=states.get(provider.id, "stopped"),
                           uptime=router.uptime(provider.id))
            for provider in _geordnet(providers, server_control)
        ]
        return JSONResponse(content=daten)

    @app.get("/api/providers/{provider_id}")
    async def api_provider(provider_id: str) -> Response:
        provider, fehler = _provider_oder_404(provider_id)
        if fehler is not None:
            return fehler
        return JSONResponse(content=await _detail(provider))

    @app.get("/api/providers/{provider_id}/edit")
    async def api_provider_edit(provider_id: str) -> Response:
        """Vollstaendige Editor-Sicht (inkl. Start-/Stopbefehl) fuer ein Detail.

        Die Listen- und Detailantworten bleiben bewusst schlank; nur diese
        lokale Editor-Route liefert die Prozessfelder, damit das Formular sie
        vorbefuellen kann. Zugangsdaten/Umgebungsvariablen gibt es im
        Datenmodell nicht.
        """
        provider, fehler = _provider_oder_404(provider_id)
        if fehler is not None:
            return fehler
        return JSONResponse(content=provider.to_dict())

    @app.post("/api/providers", status_code=201)
    async def api_provider_create(request: Request) -> Response:
        try:
            daten = await request.json()
        except ValueError:
            daten = None
        if not isinstance(daten, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "JSON-Objekt erwartet"})
        try:
            provider = _provider_aus_body(daten)
        except RegistryError as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})

        router.reload()
        if (provider.id in router.providers()
                or (router.registry_dir / f"{provider.id}.toml").exists()):
            return JSONResponse(
                status_code=409,
                content={"error": f"Provider {provider.id!r} existiert "
                                  f"bereits"})
        try:
            save_provider(provider, router.registry_dir)
        except RegistryError as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})
        except OSError as exc:
            logger.warning("Provider %s anlegen fehlgeschlagen: %s",
                           provider.id, exc)
            return JSONResponse(
                status_code=500,
                content={"error": f"Provider konnte nicht gespeichert "
                                  f"werden: {exc}"})
        neu_geladen = _registry_neu_laden()
        antwort = await _detail(provider)
        antwort["reload_pending"] = not neu_geladen
        return JSONResponse(status_code=201, content=antwort)

    @app.delete("/api/providers/{provider_id}")
    async def api_provider_delete(provider_id: str) -> Response:
        router.reload()
        provider, fehler = _provider_oder_404(provider_id)
        if fehler is not None:
            return fehler
        try:
            geloescht = delete_provider(provider.id, router.registry_dir)
        except OSError as exc:
            logger.warning("Provider %s loeschen fehlgeschlagen: %s",
                           provider_id, exc)
            return JSONResponse(
                status_code=500,
                content={"error": f"Provider konnte nicht geloescht "
                                  f"werden: {exc}"})
        if not geloescht:
            return JSONResponse(
                status_code=404,
                content={"error": f"Provider {provider_id!r} nicht gefunden"})
        neu_geladen = _registry_neu_laden()
        return JSONResponse(content={
            "deleted": provider.id,
            "reload_pending": not neu_geladen,
        })

    @app.post("/api/providers/{provider_id}/rename")
    async def api_provider_rename(provider_id: str,
                                  request: Request) -> Response:
        router.reload()
        provider, fehler = _provider_oder_404(provider_id)
        if fehler is not None:
            return fehler
        try:
            daten = await request.json()
        except ValueError:
            daten = None
        if not isinstance(daten, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "JSON-Objekt erwartet"})
        roh_id = daten.get("id")
        if not isinstance(roh_id, str) or not roh_id.strip():
            return JSONResponse(
                status_code=400,
                content={"error": "id fehlt"})
        neu_id = roh_id.strip().lower()
        if neu_id == provider.id:
            return JSONResponse(
                status_code=400,
                content={"error": "neue id muss sich von der alten "
                                  "unterscheiden"})
        if not PROVIDER_ID_RE.match(neu_id):
            return JSONResponse(
                status_code=400,
                content={"error": f"ungueltige id {neu_id!r}: nur "
                                  f"Kleinbuchstaben, Ziffern, '-' und '_' "
                                  f"(max. 64 Zeichen)"})
        if (neu_id in router.providers()
                or (router.registry_dir / f"{neu_id}.toml").exists()):
            return JSONResponse(
                status_code=409,
                content={"error": f"Provider {neu_id!r} existiert bereits"})

        neuer_name = provider.name
        if not neuer_name or neuer_name == provider.id:
            neuer_name = neu_id
        try:
            umbenannt = replace(provider, id=neu_id, name=neuer_name)
        except (RegistryError, TypeError, ValueError) as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})
        try:
            save_provider(umbenannt, router.registry_dir)
        except RegistryError as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})
        except OSError as exc:
            logger.warning("Provider %s umbenennen fehlgeschlagen: %s",
                           provider_id, exc)
            return JSONResponse(
                status_code=500,
                content={"error": f"Provider konnte nicht umbenannt "
                                  f"werden: {exc}"})
        alt = router.registry_dir / f"{provider.id}.toml"
        alt.unlink(missing_ok=True)
        neu_geladen = _registry_neu_laden()
        antwort = await _detail(umbenannt)
        antwort["reload_pending"] = not neu_geladen
        return JSONResponse(content=antwort)

    @app.patch("/api/providers/{provider_id}")
    async def api_provider_patch(provider_id: str,
                                 request: Request) -> Response:
        provider, fehler = _provider_oder_404(provider_id)
        if fehler is not None:
            return fehler
        try:
            daten = await request.json()
        except ValueError:
            daten = None
        if not isinstance(daten, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "JSON-Objekt erwartet"})
        if "id" in daten:
            return JSONResponse(
                status_code=400,
                content={"error": "id ist nicht aenderbar (provider rename)"})

        aenderungen: dict[str, Any] = {}
        einstellungen = dict(provider.settings)
        for key, wert in daten.items():
            if key == "settings":
                if not isinstance(wert, dict):
                    return JSONResponse(
                        status_code=400,
                        content={"error": "settings muss ein Objekt sein"})
                einstellungen.update(wert)
            elif key in _PATCH_SETTING_FELDER:
                einstellungen[key] = wert
            elif key in _PATCH_PROVIDER_FELDER:
                aenderungen[key] = wert
            else:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"unbekanntes Feld {key!r}"})

        try:
            neu = replace(provider, settings=einstellungen, **aenderungen)
        except (RegistryError, TypeError, ValueError) as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})
        try:
            save_provider(neu, router.registry_dir)
        except (RegistryError, OSError) as exc:
            logger.warning("Provider %s speichern fehlgeschlagen: %s",
                           provider_id, exc)
            return JSONResponse(
                status_code=500,
                content={"error": f"Provider konnte nicht gespeichert "
                                  f"werden: {exc}"})
        router.reload()
        neu_geladen = bool(server_control.refresh_from_registry(
            router.registry_dir))
        antwort = await _detail(neu)
        antwort["reload_pending"] = not neu_geladen
        return JSONResponse(content=antwort)

    @app.post("/api/providers/{provider_id}/start")
    async def api_provider_start(provider_id: str) -> Response:
        if not config.server_control_enabled:
            return JSONResponse(
                status_code=503,
                content={"error": "Server-Steuerung ist deaktiviert."})
        provider, fehler = _provider_oder_404(provider_id)
        if fehler is not None:
            return fehler
        try:
            snap = await server_control.start_and_wait(
                provider.id, timeout_s=router.start_timeout_s)
        except ServerConflictError as exc:
            return JSONResponse(status_code=409,
                                content={"error": str(exc)})
        except ServerControlError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": str(exc),
                         "log_tail": server_control.log_lines(provider.id, 60)})
        except ValueError as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})
        router.observe(snap)
        router.touch(provider.id)
        states = router.observe(snap)
        return JSONResponse(content=_provider_dict(
            provider,
            status=states.get(provider.id) or snap.get("state", "stopped"),
            uptime=router.uptime(provider.id)))

    @app.post("/api/providers/{provider_id}/stop")
    async def api_provider_stop(provider_id: str) -> Response:
        if not config.server_control_enabled:
            return JSONResponse(
                status_code=503,
                content={"error": "Server-Steuerung ist deaktiviert."})
        provider, fehler = _provider_oder_404(provider_id)
        if fehler is not None:
            return fehler
        try:
            snap = await server_control.stop_and_wait(
                provider.id, timeout_s=router.stop_timeout_s)
        except ServerConflictError as exc:
            return JSONResponse(status_code=409,
                                content={"error": str(exc)})
        except ServerControlError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": str(exc),
                         "log_tail": server_control.log_lines(provider.id, 60)})
        except ValueError as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})
        states = router.observe(snap)
        router.touch(provider.id)
        return JSONResponse(content=_provider_dict(
            provider,
            status=states.get(provider.id, "stopped"),
            uptime=router.uptime(provider.id)))

    @app.get("/api/providers/{provider_id}/logs")
    async def api_provider_logs(
            provider_id: str,
            lines: int = Query(200, ge=1, le=2000),
    ) -> Response:
        provider, fehler = _provider_oder_404(provider_id)
        if fehler is not None:
            return fehler
        if provider.id not in server_control.backends:
            server_control.refresh_from_registry(router.registry_dir)
        zeilen = server_control.log_lines(provider.id, lines)
        if not zeilen and provider.logfile:
            pfad = Path(provider.logfile).expanduser()
            if not pfad.is_absolute():
                pfad = standard_repo_root() / pfad
            zeilen = read_log_tail(pfad, lines)
        antwort: dict[str, Any] = {
            "provider": provider.id,
            "logfile": provider.logfile,
            "lines": zeilen,
        }
        if not provider.logfile:
            antwort["error"] = "kein logfile konfiguriert"
        return JSONResponse(content=antwort)

    @app.get("/api/system")
    async def api_system() -> Response:
        return JSONResponse(content=await probe.snapshot())
