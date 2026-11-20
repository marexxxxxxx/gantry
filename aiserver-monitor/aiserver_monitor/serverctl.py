"""Start/Stop/Status der Modell-Backends (Qwen/SGLang und Ollama).

Eine Quelle fuer Dashboard und ``server.sh``: Das Gateway ruft dieselbe
:class:`ServerControl` auf wie die CLI. Alle Backends lauschen auf demselben
Upstream-Port; ein Wechsel stoppt das laufende Backend, wartet auf die
Portfreigabe und startet das andere.

Qwen laeuft ueber die systemd-User-Unit ``escha-server.service`` (falls
installiert, sonst ueber ``runtime/sglang/start-penalty.sh`` /
``stop-server.sh``). Flint, GPT-OSS und Abliterated laufen als eigener
``ollama serve``-Prozess (``runtime/<backend>/start-*.sh`` /
``stop-*.sh``, PID-File + ``/api/version``).

``--no-block`` kehrt zurueck, bevor systemd fertig ist. Der Zustand wird
deshalb aus Unit-/Prozesszustand plus Health-Pfad abgeleitet; die UI pollt
:meth:`ServerControl.snapshot`. Uebergaenge haben Zeitlimits, damit ein
haengender Start/Stop die Steuerung nicht dauerhaft blockiert.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Config, load_config
from .registry import (
    Provider,
    RegistryError,
    default_registry_dir,
    load_registry,
)

logger = logging.getLogger(__name__)

#: Upstream, gegen den Health-Polls laufen, wenn keine Config vorliegt.
DEFAULT_UPSTREAM_BASE = "http://127.0.0.1:30001"

#: Unit, die den Modellserver betreibt (Default; per Config ueberschreibbar).
DEFAULT_UNIT = "escha-server.service"

#: Skripte fuer Installationen ohne systemd-Unit (relativ zur Repo-Wurzel).
DEFAULT_START_SCRIPT = "runtime/sglang/start-penalty.sh"
DEFAULT_STOP_SCRIPT = "runtime/sglang/stop-server.sh"

#: Prozessmuster fuer ``pgrep -f``. Die Klammer verhindert, dass pgrep sich
#: selbst oder eine Shell mit dem Muster findet.
DEFAULT_PROCESS_PATTERN = "[s]glang.launch_server"

#: Zeitlimit einzelner systemctl-/pgrep-Aufrufe.
_RUN_TIMEOUT_S = 10.0

#: Zeitlimit des Health-Aufrufs; ein haengender Upstream blockiert die UI nicht.
_HEALTH_TIMEOUT_S = 2.0

#: Reihenfolge und Namen der Backends, die sich den Upstream-Port teilen.
BACKEND_NAMES = ("qwen", "flint", "gptoss", "abl", "edge0")

#: Backend, das ohne explizite Wahl gestartet wird.
DEFAULT_BACKEND = "qwen"

#: Ollama-Backends laufen als eigener Prozess auf demselben Port wie SGLang.
DEFAULT_FLINT_START_SCRIPT = "runtime/flint/start-flint.sh"
DEFAULT_FLINT_STOP_SCRIPT = "runtime/flint/stop-flint.sh"
DEFAULT_FLINT_PIDFILE = "runtime/flint/flint.pid"
DEFAULT_FLINT_HEALTH_PATH = "/api/version"

DEFAULT_GPTOSS_START_SCRIPT = "runtime/gptoss/start-gptoss.sh"
DEFAULT_GPTOSS_STOP_SCRIPT = "runtime/gptoss/stop-gptoss.sh"
DEFAULT_GPTOSS_PIDFILE = "runtime/gptoss/gptoss.pid"

DEFAULT_ABL_START_SCRIPT = "runtime/abl/start-abl.sh"
DEFAULT_ABL_STOP_SCRIPT = "runtime/abl/stop-abl.sh"
DEFAULT_ABL_PIDFILE = "runtime/abl/abl.pid"

#: Edge0 laeuft nicht ueber Ollama, sondern als eigener edge0-infer-Prozess
#: (edge0-strata-Engine) mit eigenem PID-File und SGLang-aehnlichem /health.
DEFAULT_EDGE0_START_SCRIPT = "runtime/edge0/start-edge0.sh"
DEFAULT_EDGE0_STOP_SCRIPT = "runtime/edge0/stop-edge0.sh"
DEFAULT_EDGE0_PIDFILE = "runtime/edge0/edge0.pid"
DEFAULT_EDGE0_HEALTH_PATH = "/health"

#: Edge0 laeuft in einem eigenen systemd-Scope mit MemoryMax. Ohne Scope
#: teilt die Engine den cgroup mit dem Gateway, und der Kernel-OOM-Killer
#: reisst beim RAM-Druck das Gateway samt Sidecars mit (beobachtet 19.11.
#: 08:08 und 08:20, siehe /tmp/edge0_server.log). 44G liegt ueber dem
#: gemessenen Bedarf (~38G) und unter den 60G des Hosts, damit noch Platz
#: fuer Gateway, Ollama und Renderings bleibt. Leerer Scope-Name schaltet
#: die Isolation ab.
DEFAULT_EDGE0_SCOPE_UNIT = "edge0-backend.scope"
DEFAULT_EDGE0_MEMORY_MAX = "44G"


@dataclass(frozen=True)
class Backend:
    """Ein Modellserver hinter demselben Upstream-Port.

    Genau eines der Backends laeuft: SGLang (Qwen) ueber die systemd-Unit,
    Flint, GPT-OSS und Abliterated ueber einen eigenen Ollama-Prozess mit
    PID-File. ``health_path`` unterscheidet sich, weil Ollama kein ``/health``
    kennt.
    """

    name: str
    label: str
    health_path: str
    unit: str | None = None
    start_script: str | None = None
    stop_script: str | None = None
    process_pattern: str | None = None
    pidfile: str | None = None
    #: Direkter Startbefehl aus der Provider-Registry; gewinnt gegen unit/
    #: start_script. Wird detached im ``workdir`` ausgefuehrt.
    start_cmd: list[str] | None = None
    #: Direkter Stopbefehl aus der Provider-Registry; gewinnt gegen unit/
    #: stop_script. Wird blockierend (mit Timeout) im ``workdir`` ausgefuehrt.
    stop_cmd: list[str] | None = None
    #: Arbeitsverzeichnis fuer start_cmd/stop_cmd (relativ zur Repo-Wurzel).
    workdir: str | None = None
    #: Eigener systemd-Scope fuer den Start (cgroup-Isolation, optional).
    scope_unit: str | None = None
    #: MemoryMax des Scopes; leer/None laesst ihn unbeschraenkt.
    memory_max: str | None = None
    #: Host/Port des Providers. ``base_url`` ist die daraus gebaute Wurzel;
    #: ein leerer Wert bedeutet "gemeinsamer Upstream" (Legacy-Backends).
    host: str = "127.0.0.1"
    port: int = 0
    base_url: str = ""
    #: Vollstaendige Health-URL (falls der Provider eine absolute URL nennt).
    health_url: str = ""
    #: Modellname des Providers (fuer Routing und Anzeige).
    model: str = ""
    #: Logdatei des Providers; Fehlermeldungen zitieren daraus das Ende.
    logfile: str | None = None
    #: Provider-Einstellungen, die die Steuerung/Veraumnahme beeinflussen.
    auto_load: bool = True
    idle_unload_min: int = 0
    exclusive_gpu: bool = False
    enabled: bool = True


def _health_path_from_url(url: str, *, default: str = "/health") -> str:
    """Pfad aus ``health_url`` extrahieren (absolute URL oder blosser Pfad)."""
    text = str(url or "").strip()
    if not text:
        return default
    parsed = urlparse(text)
    path = parsed.path if (parsed.scheme or parsed.netloc) else text
    if not path:
        return default
    if not path.startswith("/"):
        path = "/" + path
    return path


def _unit_from_command(command: Sequence[str] | None) -> str | None:
    """Unit-Namen aus ``systemctl ... start|stop <unit>`` ableiten."""
    if not command or Path(command[0]).name != "systemctl":
        return None
    for aktion in ("start", "restart", "stop"):
        if aktion not in command:
            continue
        rest = [teil for teil in command[command.index(aktion) + 1:]
                if not teil.startswith("-")]
        if rest and rest[0].endswith(".service"):
            return rest[0]
    return None


def _absolute_url(url: str) -> str:
    """``url`` als absolute HTTP(S)-URL, sonst leerer String."""
    text = str(url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return text
    return ""


def _backend_from_provider(provider: Provider) -> Backend:
    """Registry-Provider in ein :class:`Backend` uebersetzen.

    ``scope_unit``/``memory_max`` (und optional ``unit``/``process_pattern``)
    stehen in ``[settings]``; alle anderen Felder kommen aus den Top-Level-
    Keys. Der Health-Pfad wird aus ``health_url`` extrahiert; eine absolute
    URL bleibt zusaetzlich vollstaendig erhalten, damit auch Provider auf
    einem eigenen Port (z. B. qwen-coder auf 8080) korrekt gepollt werden.
    """
    settings = provider.settings or {}
    default_health = ("/api/version" if provider.kind == "ollama"
                      else "/health")
    unit = str(settings.get("unit") or "").strip() or None
    process_pattern = str(settings.get("process_pattern") or "").strip() or None
    if provider.kind == "sglang":
        unit = unit or _unit_from_command(provider.start_cmd)
        unit = unit or _unit_from_command(provider.stop_cmd)
        process_pattern = process_pattern or DEFAULT_PROCESS_PATTERN
    base_url = (f"http://{provider.host}:{provider.port}"
                if provider.port else "")
    return Backend(
        name=provider.id,
        label=provider.name,
        health_path=_health_path_from_url(provider.health_url,
                                          default=default_health),
        unit=unit,
        start_cmd=list(provider.start_cmd) or None,
        stop_cmd=list(provider.stop_cmd) or None,
        process_pattern=process_pattern,
        pidfile=provider.pidfile or None,
        workdir=provider.workdir or ".",
        scope_unit=str(settings.get("scope_unit") or "").strip() or None,
        memory_max=str(settings.get("memory_max") or "").strip() or None,
        host=provider.host or "127.0.0.1",
        port=int(provider.port or 0),
        base_url=base_url,
        health_url=_absolute_url(provider.health_url),
        model=provider.model,
        logfile=provider.logfile or None,
        auto_load=bool(settings.get("auto_load", True)),
        idle_unload_min=int(settings.get("idle_unload_min") or 0),
        exclusive_gpu=bool(settings.get("exclusive_gpu", False)),
        enabled=bool(provider.enabled),
    )


def _backends_from_providers(
        providers: dict[str, Provider]) -> dict[str, Backend]:
    """Backends in stabiler Reihenfolge: bekannte Namen zuerst, dann neue."""
    bekannt = {name: index for index, name in enumerate(BACKEND_NAMES)}
    namen = sorted(providers,
                   key=lambda name: (bekannt.get(name, len(bekannt)), name))
    return {name: _backend_from_provider(providers[name]) for name in namen}


class ServerControlError(RuntimeError):
    """Start/Stop ist fehlgeschlagen; ``str(exc)`` ist die Meldung fuer die UI."""


class ServerConflictError(ServerControlError):
    """Es laeuft bereits ein Start- oder Stop-Uebergang."""


def standard_repo_root() -> Path:
    """Repo-Wurzel dieses Checkouts (``.../aiserver-monitor/aiserver_monitor``)."""
    return Path(__file__).resolve().parents[2]


def read_log_tail(path: Path | str, lines: int = 200) -> list[str]:
    """Letzte ``lines`` Zeilen einer Datei, ohne sie komplett zu laden.

    Fehlende oder unlesbare Dateien ergeben eine leere Liste: ein Log-Tail
    ist Beiwerk einer Fehlermeldung und darf sie nie ersetzen.
    """
    try:
        anzahl = max(1, min(int(lines), 2000))
    except (TypeError, ValueError):
        anzahl = 200
    pfad = Path(path)
    try:
        with open(pfad, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            block = 8192
            daten = b""
            while position > 0 and daten.count(b"\n") <= anzahl:
                schritt = min(block, position)
                position -= schritt
                handle.seek(position)
                daten = handle.read(schritt) + daten
                block *= 2
    except OSError:
        return []
    return daten.decode(errors="replace").splitlines()[-anzahl:]


def _backend_zustand(aktiv: bool, health: bool) -> str:
    """Zustand eines Backends: Prozess/Unit plus Health."""
    if aktiv and health:
        return "running"
    return "starting" if aktiv else "stopped"


def _aktives_backend(zustaende: dict[str, str],
                     namen: Sequence[str] = BACKEND_NAMES) -> str | None:
    """Backend, das die UI als aktiv zeigen soll (running vor starting)."""
    for gesucht in ("running", "starting"):
        for name in namen:
            if zustaende.get(name) == gesucht:
                return name
    return None


class ServerControl:
    """Zustandsmaschine fuer den Modellserver.

    ``runner``, ``spawner`` und ``health_reader`` sind injizierbar, damit
    Tests weder systemd noch GPU anfassen. ``clock`` liefert monotone
    Sekunden (Zeitlimits); ``client`` teilt sich in der App den dedizierten
    Steuer-Client.
    """

    def __init__(
            self,
            *,
            upstream_base: str,
            repo_root: Path,
            unit: str = DEFAULT_UNIT,
            start_script: str = DEFAULT_START_SCRIPT,
            stop_script: str = DEFAULT_STOP_SCRIPT,
            process_pattern: str = DEFAULT_PROCESS_PATTERN,
            flint_start_script: str = DEFAULT_FLINT_START_SCRIPT,
            flint_stop_script: str = DEFAULT_FLINT_STOP_SCRIPT,
            flint_pidfile: str = DEFAULT_FLINT_PIDFILE,
            flint_health_path: str = DEFAULT_FLINT_HEALTH_PATH,
            gptoss_start_script: str = DEFAULT_GPTOSS_START_SCRIPT,
            gptoss_stop_script: str = DEFAULT_GPTOSS_STOP_SCRIPT,
            gptoss_pidfile: str = DEFAULT_GPTOSS_PIDFILE,
            abl_start_script: str = DEFAULT_ABL_START_SCRIPT,
            abl_stop_script: str = DEFAULT_ABL_STOP_SCRIPT,
            abl_pidfile: str = DEFAULT_ABL_PIDFILE,
            edge0_start_script: str = DEFAULT_EDGE0_START_SCRIPT,
            edge0_stop_script: str = DEFAULT_EDGE0_STOP_SCRIPT,
            edge0_pidfile: str = DEFAULT_EDGE0_PIDFILE,
            edge0_health_path: str = DEFAULT_EDGE0_HEALTH_PATH,
            edge0_scope_unit: str | None = DEFAULT_EDGE0_SCOPE_UNIT,
            edge0_memory_max: str | None = DEFAULT_EDGE0_MEMORY_MAX,
            backends: dict[str, Backend] | None = None,
            registry_dir: Path | str | None = None,
            runner: Callable[[Sequence[str]], Awaitable[tuple[int, str, str]]]
            | None = None,
            spawner: Callable[[Sequence[str]], None] | None = None,
            command_spawner: Callable[[Sequence[str], str], object | None]
            | None = None,
            command_runner: Callable[[Sequence[str], str],
                                     Awaitable[tuple[int, str, str]]]
            | None = None,
            health_reader: Callable[[str], Awaitable[bool]] | None = None,
            clock: Callable[[], float] = time.monotonic,
            sleeper: Callable[[float], Awaitable[None]] | None = None,
            client: httpx.AsyncClient | None = None,
            start_timeout_s: float = 600.0,
            stop_timeout_s: float = 60.0,
            start_grace_s: float = 10.0,
            switch_poll_s: float = 0.5,
    ):
        self._upstream_base = upstream_base.rstrip("/")
        self._repo_root = Path(repo_root)
        legacy_backends: dict[str, Backend] = {
            "qwen": Backend(
                name="qwen",
                label="Qwen (SGLang)",
                health_path="/health",
                unit=unit,
                start_script=start_script,
                stop_script=stop_script,
                process_pattern=process_pattern,
            ),
            "flint": Backend(
                name="flint",
                label="Flint (Ollama)",
                health_path=flint_health_path,
                start_script=flint_start_script,
                stop_script=flint_stop_script,
                pidfile=flint_pidfile,
            ),
            "gptoss": Backend(
                name="gptoss",
                label="GPT-OSS 20B (Ollama)",
                health_path=flint_health_path,
                start_script=gptoss_start_script,
                stop_script=gptoss_stop_script,
                pidfile=gptoss_pidfile,
            ),
            "abl": Backend(
                name="abl",
                label="Qwen3-14B Abliterated (Ollama)",
                health_path=flint_health_path,
                start_script=abl_start_script,
                stop_script=abl_stop_script,
                pidfile=abl_pidfile,
            ),
            "edge0": Backend(
                name="edge0",
                label="Edge0 120B (NVFP4)",
                health_path=edge0_health_path,
                start_script=edge0_start_script,
                stop_script=edge0_stop_script,
                pidfile=edge0_pidfile,
                scope_unit=edge0_scope_unit or None,
                memory_max=edge0_memory_max or None,
            ),
        }
        # Eine explizit uebergebene Registry ersetzt die eingebauten Backends;
        # ein leeres Dict bleibt leer (der Aufrufer entscheidet ueber den
        # Rueckfall, siehe :meth:`from_config`).
        self._backends: dict[str, Backend] = (
            dict(backends) if backends is not None else legacy_backends)
        self._registry_dir = (Path(registry_dir) if registry_dir is not None
                              else None)
        self._backend_names = tuple(self._backends)
        self._default_backend = (
            DEFAULT_BACKEND if DEFAULT_BACKEND in self._backends
            else (self._backend_names[0] if self._backend_names
                  else DEFAULT_BACKEND))
        #: Kompatibilitaet: ``unit`` ist die Unit des Qwen-Backends.
        self._unit = unit
        self._runner = runner or self._run_subprocess
        self._spawner = spawner or self._spawn_subprocess
        self._command_spawner = command_spawner or self._spawn_command
        self._command_runner = command_runner or self._run_command
        self._health_reader = health_reader
        self._clock = clock
        self._sleeper = sleeper or asyncio.sleep
        self._client = client
        self._start_timeout_s = start_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._start_grace_s = start_grace_s
        self._switch_poll_s = switch_poll_s

        self._transition: str | None = None
        self._transition_backend: str | None = None
        self._transition_start = 0.0
        self._wechsel_task: asyncio.Task | None = None
        self._error: str | None = None
        self._lock = asyncio.Lock()
        #: Handles der per start_cmd gestarteten Prozesse (Best effort). Sie
        #: ueberbruecken das Fenster zwischen Prozessstart und erstem
        #: erfolgreichen Health-Poll, in dem ein Provider ohne PID-File sonst
        #: als "gestoppt" gaelte und ein Stopp no-op waere.
        self._spawned: dict[str, object] = {}

    # --- Konstruktion aus der Provider-Registry -----------------------------

    @property
    def backends(self) -> dict[str, Backend]:
        """Kopie der bekannten Backends (Name -> Backend)."""
        return dict(self._backends)

    @property
    def backend_names(self) -> tuple[str, ...]:
        """Backend-Namen in Anzeigereihenfolge (Registry bzw. eingebaut)."""
        return self._backend_names

    @property
    def transitioning(self) -> bool:
        """Laeuft gerade ein Start-/Stop-Uebergang?"""
        return self._transition is not None

    @property
    def transition_backend(self) -> str | None:
        """Backend des laufenden Uebergangs (None ohne Uebergang)."""
        return self._transition_backend

    @classmethod
    def from_registry(
            cls,
            registry_dir: Path | str | None = None,
            *,
            upstream_base: str = DEFAULT_UPSTREAM_BASE,
            repo_root: Path | str | None = None,
            **kwargs,
    ) -> "ServerControl":
        """Backends aus ``providers/*.toml`` bauen.

        ``registry_dir=None`` nimmt :func:`default_registry_dir` (bzw.
        ``GANTRY_PROVIDERS_DIR``). Eine leere Registry ergibt eine leere
        Backend-Liste; den Rueckfall auf die eingebauten Backends entscheidet
        :meth:`from_config`.
        """
        providers = load_registry(registry_dir)
        backends = _backends_from_providers(providers)
        unit = kwargs.pop("unit", None)
        if unit is None:
            unit = next((backend.unit for backend in backends.values()
                         if backend.unit), DEFAULT_UNIT)
        return cls(
            upstream_base=upstream_base,
            repo_root=(Path(repo_root) if repo_root is not None
                       else standard_repo_root()),
            unit=unit,
            backends=backends,
            registry_dir=(Path(registry_dir) if registry_dir is not None
                          else default_registry_dir()),
            **kwargs,
        )

    @classmethod
    def from_config(cls, config: Config, *,
                    client: httpx.AsyncClient | None = None) -> "ServerControl":
        """Registry-Backends, sonst die eingebauten (Rueckfall).

        Eine fehlende oder unlesbare ``providers/``-Registry darf das Gateway
        nicht am Start hindern; sie faellt dann auf die hartkodierten Backends
        zurueck. Gueltige Provider-Dateien sind die einzige Quelle.
        """
        try:
            providers = load_registry()
        except (RegistryError, OSError) as exc:
            logger.warning(
                "Provider-Registry nicht lesbar (%s) - nutze eingebaute "
                "Backends.", exc)
            providers = {}
        if providers:
            backends = _backends_from_providers(providers)
            unit = next((backend.unit for backend in backends.values()
                         if backend.unit), config.server_unit)
            return cls(
                upstream_base=config.upstream_base,
                repo_root=standard_repo_root(),
                unit=unit,
                backends=backends,
                registry_dir=default_registry_dir(),
                client=client,
            )
        return cls(
            upstream_base=config.upstream_base,
            repo_root=standard_repo_root(),
            unit=config.server_unit,
            start_script=config.server_start_script,
            stop_script=config.server_stop_script,
            process_pattern=config.server_process_pattern,
            flint_start_script=config.flint_start_script,
            flint_stop_script=config.flint_stop_script,
            flint_pidfile=config.flint_pidfile,
            flint_health_path=config.flint_health_path,
            gptoss_start_script=config.gptoss_start_script,
            gptoss_stop_script=config.gptoss_stop_script,
            gptoss_pidfile=config.gptoss_pidfile,
            abl_start_script=config.abl_start_script,
            abl_stop_script=config.abl_stop_script,
            abl_pidfile=config.abl_pidfile,
            edge0_start_script=config.edge0_start_script,
            edge0_stop_script=config.edge0_stop_script,
            edge0_pidfile=config.edge0_pidfile,
            edge0_health_path=config.edge0_health_path,
            edge0_scope_unit=config.edge0_scope_unit,
            edge0_memory_max=config.edge0_memory_max,
            client=client,
        )

    # --- Beobachtung --------------------------------------------------------

    async def snapshot(self) -> dict:
        """Aktueller Zustand als JSON-faehiges Dict (ohne ``enabled``)."""
        jetzt = self._clock()
        unit_installiert = await self._unit_installiert()

        zustaende: dict[str, str] = {}
        for name, backend_info in self._backends.items():
            if backend_info.pidfile is not None:
                # Ollama-Backends teilen sich /api/version; nur das PID-File
                # sagt, welcher Prozess laeuft. Health entscheidet danach
                # zwischen "starting" und "running".
                aktiv = self._pid_laeuft(backend_info.pidfile)
                health = await self._health_ok(backend_info) if aktiv else False
            elif backend_info.unit is not None:
                unit_installiert_backend = (
                    unit_installiert if backend_info.unit == self._unit
                    else await self._unit_installiert(backend_info.unit))
                aktiv = (
                    await self._unit_aktiv(unit_installiert_backend,
                                           backend_info.unit)
                    or await self._prozess_laeuft(backend_info)
                )
                health = await self._health_ok(backend_info) if aktiv else False
            else:
                # Provider ohne PID-File und ohne Unit (z. B. qwen-coder auf
                # Port 8080): allein der provider-eigene Health-Endpunkt sagt,
                # ob er laeuft. Waehrend eines Starts bleibt der Zustand
                # "starting", damit die Grace-Pruefung den Ladevorgang nicht
                # nach wenigen Sekunden als Fehler abbricht.
                health = await self._health_ok(backend_info)
                aktiv = health or self._spawn_lebt(name)
                if (not aktiv and self._transition == "starting"
                        and self._transition_backend == name):
                    zustaende[name] = "starting"
                    continue
            zustaende[name] = _backend_zustand(aktiv, health)
        self._uebergang_pruefen(jetzt, zustaende)

        if self._transition == "starting":
            state = "starting"
        elif self._transition == "stopping":
            state = "stopping"
        elif "running" in zustaende.values():
            state = "running"
        elif "starting" in zustaende.values():
            state = "starting"
        else:
            state = "stopped"

        backend = self._transition_backend or _aktives_backend(
            zustaende, self._backend_names)
        gewaehlt = self._backends.get(backend or self._default_backend)
        unit = gewaehlt.unit if gewaehlt and gewaehlt.unit else self._unit

        # Ein laufender Server raeumt keinen alten Fehler weg: die UI zeigt
        # ihn als "letzter Fehler", bis eine Aktion erfolgreich war.

        return {
            "state": state,
            "backend": backend,
            "backends": [
                {
                    "name": name,
                    "label": backend_info.label,
                    "state": zustaende[name],
                    "unit": backend_info.unit,
                    "managed_by_systemd": (
                        unit_installiert if backend_info.unit else False),
                }
                for name, backend_info in self._backends.items()
            ],
            "unit": unit,
            "managed_by_systemd": (
                unit_installiert if backend == "qwen" else False),
            "upstream": self._upstream_base,
            "error": self._error,
        }

    def _uebergang_pruefen(self, jetzt: float, zustaende: dict[str, str]) -> None:
        """Beendet einen Uebergang, wenn Ziel erreicht, Fehler oder Zeitlimit."""
        if self._transition is None:
            return
        vergangen = jetzt - self._transition_start
        if self._transition == "starting":
            ziel = self._transition_backend or self._default_backend
            wechsel_laeuft = (
                self._wechsel_task is not None
                and not self._wechsel_task.done()
            )
            if zustaende.get(ziel) == "running":
                self._transition = None
                self._transition_backend = None
                self._error = None
            elif vergangen >= self._start_timeout_s:
                self._transition = None
                self._transition_backend = None
                self._error = (
                    "Der Start dauert laenger als "
                    f"{self._start_timeout_s:g} s - Log pruefen.")
            elif (not wechsel_laeuft
                  and all(z == "stopped" for z in zustaende.values())
                  and vergangen >= self._start_grace_s):
                self._transition = None
                self._transition_backend = None
                self._error = "Der Server wurde beim Start beendet."
        else:  # stopping
            ziel = self._transition_backend
            ziel_gestoppt = (ziel is not None
                             and zustaende.get(ziel) == "stopped")
            if ziel_gestoppt or all(z == "stopped" for z in zustaende.values()):
                self._transition = None
                self._transition_backend = None
                self._error = None
            elif vergangen >= self._stop_timeout_s:
                self._transition = None
                self._transition_backend = None
                self._error = (
                    "Der Stopp dauert laenger als "
                    f"{self._stop_timeout_s:g} s - Log pruefen.")

    # --- Aktionen -----------------------------------------------------------

    async def start(self, backend: str | None = None) -> dict:
        """Startet ``backend``; laeuft ein anderes, wird es zuerst gestoppt.

        Idempotent fuer das bereits laufende Backend, 409 bei laufendem
        Uebergang. Der Wechsel kehrt sofort zurueck (Zustand "starting");
        ein Hintergrund-Task wartet, bis der alte Server den Port freigibt.
        """
        if backend is not None and backend not in self._backends:
            raise ValueError(
                f"Unbekanntes Backend {backend!r}; erlaubt: "
                f"{', '.join(self._backends)}")
        async with self._lock:
            if self._transition is not None:
                raise ServerConflictError(
                    "Es laeuft bereits ein Start- oder Stoppvorgang.")
            vorher = await self.snapshot()
            ziel = backend or vorher["backend"] or self._default_backend
            if vorher["state"] in ("running", "starting") \
                    and vorher["backend"] == ziel:
                return vorher

            andere = self._konflikt_backends(ziel)
            laufend = [name for name in andere
                       if await self._backend_aktiv(name)]
            self._transition = "starting"
            self._transition_backend = ziel
            self._transition_start = self._clock()
            self._error = None
            try:
                if laufend:
                    self._wechsel_task = asyncio.create_task(
                        self._wechsel(ziel, laufend),
                        name=f"server-wechsel-{ziel}")
                else:
                    await self._start_backend(ziel)
            except ServerControlError:
                raise
            except Exception as exc:
                self._transition = None
                self._error = f"Start fehlgeschlagen: {type(exc).__name__}: {exc}"
                raise ServerControlError(self._error) from exc
            return await self.snapshot()

    async def stop(self, backend: str | None = None) -> dict:
        """Stoppt ``backend`` (Default: das laufende); idempotent."""
        if backend is not None and backend not in self._backends:
            raise ValueError(
                f"Unbekanntes Backend {backend!r}; erlaubt: "
                f"{', '.join(self._backends)}")
        async with self._lock:
            if self._transition is not None:
                raise ServerConflictError(
                    "Es laeuft bereits ein Start- oder Stoppvorgang.")
            vorher = await self.snapshot()
            ziel = backend or vorher["backend"] or self._default_backend
            if backend is not None:
                # Gezielter Stopp: nur dieser Provider zaehlt. Laeuft ein
                # anderer Provider weiter, darf das kein No-op verhindern.
                if not await self._backend_aktiv(ziel):
                    return vorher
            elif vorher["state"] == "stopped":
                return vorher
            self._transition = "stopping"
            self._transition_backend = ziel
            self._transition_start = self._clock()
            self._error = None
            try:
                await self._stop_backend(ziel)
            except ServerControlError:
                raise
            except Exception as exc:
                self._transition = None
                self._error = f"Stopp fehlgeschlagen: {type(exc).__name__}: {exc}"
                raise ServerControlError(self._error) from exc
            return await self.snapshot()

    async def start_and_wait(self, backend: str | None = None, *,
                             timeout_s: float | None = None) -> dict:
        """Startet ``backend`` und wartet, bis dessen Health-Endpunkt ok ist.

        Anders als :meth:`start` (das sofort mit ``starting`` zurueckkehrt)
        blockiert dieser Pfad bis zum Health-Erfolg. Ein Fehlschlag nennt das
        Ende der Provider-Logdatei, damit die Ursache ohne zweite Abfrage
        sichtbar ist.
        """
        timeout = self._start_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise ValueError("timeout_s muss positiv sein")
        snap = await self.start(backend)
        ziel = snap.get("backend") or backend or self._default_backend
        if ziel not in self._backends:
            raise ServerControlError(f"Unbekanntes Backend {ziel!r}")
        return await self.wait_for_health(ziel, timeout_s=timeout)

    async def wait_for_health(self, backend: str, *,
                              timeout_s: float | None = None) -> dict:
        """Wartet auf ``/health`` des Backends (oder bricht mit Log-Tail ab)."""
        if backend not in self._backends:
            raise ValueError(
                f"Unbekanntes Backend {backend!r}; erlaubt: "
                f"{', '.join(self._backends)}")
        timeout = self._start_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise ValueError("timeout_s muss positiv sein")
        info = self._backends[backend]
        deadline = self._clock() + timeout
        while True:
            if await self._health_ok(info):
                return await self.snapshot()
            snap = await self.snapshot()
            if snap.get("error"):
                raise ServerControlError(
                    self._mit_log(backend, str(snap["error"])))
            if self._clock() >= deadline:
                raise ServerControlError(self._mit_log(
                    backend,
                    f"{info.label} wurde nach {timeout:g} s nicht "
                    "gesund (Health nicht ok)."))
            await self._sleeper(self._switch_poll_s)

    async def stop_and_wait(self, backend: str, *,
                            timeout_s: float | None = None) -> dict:
        """Stoppt ``backend`` und wartet, bis Prozess/Health weg sind."""
        if backend not in self._backends:
            raise ValueError(
                f"Unbekanntes Backend {backend!r}; erlaubt: "
                f"{', '.join(self._backends)}")
        timeout = self._stop_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise ValueError("timeout_s muss positiv sein")
        await self.stop(backend)
        deadline = self._clock() + timeout
        while await self._backend_aktiv(backend):
            if self._clock() >= deadline:
                raise ServerControlError(self._mit_log(
                    backend,
                    f"{self._backends[backend].label} liess sich nach "
                    f"{timeout:g} s nicht stoppen."))
            await self._sleeper(self._switch_poll_s)
        return await self.snapshot()

    def log_lines(self, backend: str, lines: int = 200) -> list[str]:
        """Letzte Zeilen der Provider-Logdatei ( leer, wenn keine/kein File)."""
        info = self._backends.get(backend)
        if info is None or not info.logfile:
            return []
        return read_log_tail(self._skript(info.logfile), lines)

    def _mit_log(self, backend: str, meldung: str) -> str:
        """Meldung plus Log-Ende (fuer Start-/Stop-Fehler in der API)."""
        zeilen = self.log_lines(backend, 20)
        if not zeilen:
            return meldung
        return f"{meldung}\nLog (letzte {len(zeilen)} Zeilen):\n" + \
            "\n".join(zeilen)

    def _backend_endpoint(self, backend: Backend) -> str:
        """Effektiver HTTP-Endpunkt des Backends (Port oder gemeinsamer Upstream)."""
        return (backend.base_url or self._upstream_base).rstrip("/")

    def _konflikt_backends(self, ziel: str) -> list[str]:
        """Backends, die fuer einen Start von ``ziel`` weichen muessen.

        Zwei Provider auf demselben Host/Port koennen nicht gleichzeitig
        laufen. Zusaetzlich verdrängen sich Provider mit
        ``exclusive_gpu=true`` gegenseitig: die GPU traegt nur ein grosses
        Modell zur Zeit (VRAM-Guard).
        """
        backend = self._backends[ziel]
        endpoint = self._backend_endpoint(backend)
        konflikte: list[str] = []
        for name, other in self._backends.items():
            if name == ziel:
                continue
            if self._backend_endpoint(other) == endpoint:
                konflikte.append(name)
            elif backend.exclusive_gpu and other.exclusive_gpu:
                konflikte.append(name)
        return konflikte

    def refresh_from_registry(
            self, directory: Path | str | None = None) -> bool:
        """Backends nach einer Registry-Aenderung neu einlesen.

        Waehrend eines Start-/Stop-Uebergangs wird nicht getauscht: der
        laufende Hintergrund-Task haelt Backend-Namen fest. Ein leerer oder
        kaputter Registry-Stand laesst die bisherigen Backends stehen.
        """
        if self._transition is not None:
            return False
        ziel_dir = (Path(directory) if directory is not None
                    else self._registry_dir)
        try:
            providers = load_registry(ziel_dir)
        except (RegistryError, OSError) as exc:
            logger.warning("Provider-Registry nicht neu lesbar: %s", exc)
            return False
        if not providers:
            return False
        backends = _backends_from_providers(providers)
        self._backends = backends
        self._backend_names = tuple(backends)
        self._default_backend = (
            DEFAULT_BACKEND if DEFAULT_BACKEND in backends
            else (self._backend_names[0] if self._backend_names
                  else DEFAULT_BACKEND))
        self._unit = next((backend.unit for backend in backends.values()
                           if backend.unit), self._unit)
        if ziel_dir is not None:
            self._registry_dir = Path(ziel_dir)
        return True

    async def _wechsel(self, ziel: str, laufend: Sequence[str]) -> None:
        """Stoppt die laufenden Backends, wartet auf den Port, startet ``ziel``."""
        try:
            for name in laufend:
                await self._stop_backend(name)
            ende = self._clock() + self._stop_timeout_s
            while any([await self._backend_aktiv(name) for name in laufend]):
                if self._clock() >= ende:
                    raise ServerControlError(
                        "Der laufende Server gibt den Port nicht frei "
                        f"(nach {self._stop_timeout_s:g} s).")
                await self._sleeper(self._switch_poll_s)
            self._transition_start = self._clock()
            await self._start_backend(ziel)
        except Exception as exc:
            self._transition = None
            self._transition_backend = None
            self._error = f"Wechsel auf {ziel} fehlgeschlagen: {exc}"

    async def _start_backend(self, name: str) -> None:
        backend = self._backends[name]
        # Registry-Befehl gewinnt: er laeuft detached im Provider-workdir.
        # Ein systemd-Scope (cgroup-Isolation) bleibt auch fuer ihn aktiv.
        if backend.start_cmd:
            befehl = list(backend.start_cmd)
            if backend.scope_unit:
                befehl = self._scope_befehl(backend, befehl)
            handle = self._command_spawner(
                befehl, str(self._backend_workdir(backend)))
            if handle is not None:
                self._spawned[name] = handle
            return
        if backend.unit and await self._unit_installiert(backend.unit):
            rc, out, err = await self._runner(
                ["systemctl", "--user", "start", "--no-block", backend.unit])
            if rc != 0:
                self._transition = None
                self._error = self._fehlertext(
                    "systemctl start", rc, out, err)
                raise ServerControlError(self._error)
            return
        if not backend.start_script:
            self._transition = None
            self._error = (f"Fuer Backend {name!r} ist kein Startbefehl "
                           "hinterlegt (start_cmd, Unit oder start_script).")
            raise ServerControlError(self._error)
        befehl = ["bash", str(self._skript(backend.start_script))]
        if backend.scope_unit:
            befehl = self._scope_befehl(backend, befehl)
        self._spawner(befehl)

    def _backend_workdir(self, backend: Backend) -> Path:
        """``workdir`` des Providers, relativ zur Repo-Wurzel aufgeloest."""
        return self._skript(backend.workdir or ".")

    def _scope_befehl(self, backend: Backend, befehl: list[str]) -> list[str]:
        """Legt den Start in einen eigenen systemd-Scope (eigener cgroup).

        Mit ``MemoryMax`` trifft ein RAM-OOM nur das Backend, nicht das
        Gateway samt Ollama-, Embed- und Audio-Sidecars. ``EDGE0_FOREGROUND``
        haelt die Engine im Vordergrund; daemonisiert sie sich (setsid), kann
        sie den Scope verlassen und die Isolation waere wirkungslos.
        """
        args = [
            "systemd-run", "--user", "--scope", "--collect",
            "--unit", backend.scope_unit,
            "--setenv", "EDGE0_FOREGROUND=1",
        ]
        if backend.memory_max:
            args += ["--property", f"MemoryMax={backend.memory_max}"]
        return args + befehl

    async def _stop_backend(self, name: str) -> None:
        backend = self._backends[name]
        # Registry-Befehl gewinnt: blockierend mit Timeout, damit PID-Files
        # und Engine-Reste zuverlaessig aufgeraeumt sind, bevor der naechste
        # Start laeuft. Ein Scope wird vorher beendet (wie beim Skript-Pfad).
        if backend.stop_cmd:
            if backend.scope_unit:
                await self._runner(
                    ["systemctl", "--user", "stop", backend.scope_unit])
            rc, out, err = await self._command_runner(
                list(backend.stop_cmd), str(self._backend_workdir(backend)))
            if rc != 0:
                self._transition = None
                self._error = self._fehlertext(
                    "Stop-Kommando", rc, out, err)
                raise ServerControlError(self._error)
            return
        if backend.unit:
            unit_installiert = await self._unit_installiert(backend.unit)
            if unit_installiert and await self._unit_aktiv(
                    unit_installiert, backend.unit):
                rc, out, err = await self._runner(
                    ["systemctl", "--user", "stop", "--no-block",
                     backend.unit])
                if rc != 0:
                    self._transition = None
                    self._error = self._fehlertext(
                        "systemctl stop", rc, out, err)
                    raise ServerControlError(self._error)
                return
        # Scope zuerst beenden: systemd schickt SIGTERM an den ganzen cgroup
        # und die Engine faehrt sauber herunter. Das Stop-Skript raeumt
        # danach PID-File und Reste auf.
        if backend.scope_unit:
            await self._runner(
                ["systemctl", "--user", "stop", backend.scope_unit])
        if not backend.stop_script:
            self._transition = None
            self._error = (f"Fuer Backend {name!r} ist kein Stopbefehl "
                           "hinterlegt (stop_cmd, Unit oder stop_script).")
            raise ServerControlError(self._error)
        self._spawner(["bash", str(self._skript(backend.stop_script))])

    # --- Zustandsquellen ----------------------------------------------------

    async def _unit_installiert(self, unit: str | None = None) -> bool:
        rc, _out, _err = await self._runner(
            ["systemctl", "--user", "cat", unit or self._unit])
        return rc == 0

    async def _unit_aktiv(self, unit_installiert: bool,
                          unit: str | None = None) -> bool:
        if not unit_installiert:
            return False
        rc, _out, _err = await self._runner(
            ["systemctl", "--user", "is-active", "--quiet",
             unit or self._unit])
        return rc == 0

    async def _prozess_laeuft(self, backend: Backend | None = None) -> bool:
        if backend is not None:
            muster = backend.process_pattern
        else:
            qwen = self._backends.get("qwen")
            muster = qwen.process_pattern if qwen is not None else None
        if not muster:
            return False
        rc, _out, _err = await self._runner(["pgrep", "-f", muster])
        return rc == 0

    async def _backend_aktiv(self, name: str) -> bool:
        """Laeuft das Backend (Prozess/Unit bzw. Health)? Ohne Health-Details."""
        backend = self._backends[name]
        if backend.pidfile is not None:
            return self._pid_laeuft(backend.pidfile)
        if backend.unit is not None:
            unit_installiert = await self._unit_installiert(backend.unit)
            return (await self._unit_aktiv(unit_installiert, backend.unit)
                    or await self._prozess_laeuft(backend))
        # Provider ohne PID-File/Unit: der Health-Endpunkt ist die einzige
        # verlaessliche Quelle (qwen-coder auf Port 8080). Ein noch laufender
        # start_cmd-Prozess ueberbrueckt die Ladezeit bis zum ersten Health-OK.
        if await self._health_ok(backend):
            return True
        return self._spawn_lebt(name)

    def _spawn_lebt(self, name: str) -> bool:
        """Lebt das per start_cmd gestartete Popen-Handle noch?"""
        handle = self._spawned.get(name)
        if handle is None:
            return False
        poll = getattr(handle, "poll", None)
        if poll is None:
            return True
        try:
            if poll() is None:
                return True
        except Exception:
            pass
        self._spawned.pop(name, None)
        return False

    def _pid_laeuft(self, pidfile: str | None) -> bool:
        if not pidfile:
            return False
        try:
            pid = int(self._skript(pidfile).read_text().strip())
        except (OSError, ValueError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _health_ok(self, backend: Backend) -> bool:
        if self._health_reader is not None:
            try:
                return await self._health_reader(backend.health_path)
            except Exception as exc:  # Health darf den Status nie sprengen.
                logger.debug("Health-Pruefung fehlgeschlagen: %s", exc)
                return False
        if backend.health_url:
            # Provider-eigene absolute URL: qwen-coder auf 8080, nicht der
            # gemeinsame Upstream auf 30001.
            url = backend.health_url
        else:
            basis = (backend.base_url or self._upstream_base).rstrip("/")
            url = f"{basis}{backend.health_path}"
        try:
            if self._client is None:
                async with httpx.AsyncClient(
                        timeout=_HEALTH_TIMEOUT_S) as client:
                    antwort = await client.get(url)
            else:
                antwort = await self._client.get(
                    url, timeout=_HEALTH_TIMEOUT_S)
            return antwort.is_success
        except Exception as exc:
            logger.debug("Health-Pruefung fehlgeschlagen: %s", exc)
            return False

    # --- Prozesse -----------------------------------------------------------

    async def _run_subprocess(
            self, args: Sequence[str]) -> tuple[int, str, str]:
        """Fuehrt systemctl/pgrep aus; fehlende Tools werden zu Exit 127."""
        try:
            prozess = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as exc:
            return 127, "", str(exc)
        try:
            stdout, stderr = await asyncio.wait_for(
                prozess.communicate(), timeout=_RUN_TIMEOUT_S)
        except TimeoutError:
            prozess.kill()
            await prozess.wait()
            return 124, "", f"Timeout nach {_RUN_TIMEOUT_S:.0f} s"
        return (
            prozess.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    def _spawn_subprocess(self, args: Sequence[str]) -> None:
        """Startet ein Skript im Hintergrund, eigenstaendig und ohne Ausgabe.

        ``start_new_session`` loest den Prozess von der Gateway-Sitzung, damit
        ein Gateway-Neustart den laufenden Serverstart nicht abbricht.
        """
        subprocess.Popen(
            list(args),
            cwd=str(self._repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _spawn_command(self, args: Sequence[str], cwd: str):
        """Startet ein ``start_cmd`` aus der Registry detached im workdir.

        Wie ``_spawn_subprocess``: eigener Prozess-Session, keine Ausgabe -
        die Startskripte/Engines loggen selbst. Das Popen-Handle erlaubt der
        Steuerung, den Prozess bis zum ersten Health-Erfolg zu erkennen.
        """
        return subprocess.Popen(
            list(args),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    async def _run_command(
            self, args: Sequence[str], cwd: str) -> tuple[int, str, str]:
        """Fuehrt ein ``stop_cmd`` blockierend im workdir aus (mit Timeout)."""
        try:
            prozess = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError,
                NotADirectoryError) as exc:
            return 127, "", str(exc)
        try:
            stdout, stderr = await asyncio.wait_for(
                prozess.communicate(), timeout=self._stop_timeout_s)
        except TimeoutError:
            prozess.kill()
            await prozess.wait()
            return 124, "", f"Timeout nach {self._stop_timeout_s:g} s"
        return (
            prozess.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    def _skript(self, name: str) -> Path:
        pfad = Path(name).expanduser()
        return pfad if pfad.is_absolute() else self._repo_root / pfad

    @staticmethod
    def _fehlertext(aktion: str, rc: int, out: str, err: str) -> str:
        detail = (err.strip() or out.strip() or f"Exit {rc}")
        return f"{aktion} fehlgeschlagen: {detail}"


# --- CLI (server.sh) --------------------------------------------------------


_ZUSTAND_TEXT = {
    "running": "laeuft",
    "starting": "startet",
    "stopping": "stoppt",
    "stopped": "gestoppt",
}


def _backend_label(snap: dict, backend: str | None) -> str:
    for eintrag in snap.get("backends", []):
        if eintrag["name"] == backend:
            return eintrag["label"]
    return backend or "Modellserver"


def _status_text(snap: dict) -> str:
    zustand = snap["state"]
    label = _backend_label(snap, snap.get("backend"))
    if zustand == "running":
        text = (f"{label} laeuft ({snap['upstream']}, Health ok).")
    elif zustand == "starting":
        text = (f"{label} startet noch (Health noch nicht ok) - Qwen "
                "braucht 2-4 Minuten, Flint, GPT-OSS und Abliterated wenige "
                "Sekunden.")
    elif zustand == "stopping":
        text = f"{label} stoppt ..."
    else:
        text = "Modellserver ist gestoppt."
    if snap.get("error"):
        text += f" Letzter Fehler: {snap['error']}"
    return text


def _backends_text(snap: dict) -> str:
    zeilen = []
    for eintrag in snap.get("backends", []):
        zeilen.append(f"  {eintrag['name']:<5} {eintrag['label']:<16} "
                      f"{_ZUSTAND_TEXT[eintrag['state']]}")
    return "\n".join(zeilen)


async def _mit_client(action: str, *, timeout_s: float,
                      intervall_s: float = 3.0,
                      backend: str | None = None) -> int:
    config = load_config(os.environ.get("AISERVER_MONITOR_CONFIG") or None)
    async with httpx.AsyncClient() as client:
        control = ServerControl.from_config(config, client=client)
        if action == "status":
            snap = await control.snapshot()
            print(_status_text(snap))
            print(_backends_text(snap))
            return 0 if snap["state"] == "running" else 1

        try:
            vorher = (await control.start(backend) if action == "start"
                      else await control.stop(backend))
        except (ServerControlError, ValueError) as exc:
            print(f"[server] {exc}", file=sys.stderr)
            return 1
        print(_status_text(vorher))

        ziel_backend = backend or vorher.get("backend")
        ziel = "running" if action == "start" else "stopped"
        deadline = time.monotonic() + timeout_s
        letzte_meldung = time.monotonic()
        while True:
            snap = await control.snapshot()
            erreicht = (snap["state"] == "running"
                        and (ziel_backend is None
                             or snap["backend"] == ziel_backend))
            if ziel == "running" and erreicht:
                print(_status_text(snap))
                return 0
            if ziel == "stopped" and snap["state"] == "stopped":
                print(_status_text(snap))
                return 0
            if snap["state"] == "stopped" and ziel == "running":
                print(_status_text(snap), file=sys.stderr)
                return 1
            if time.monotonic() >= deadline:
                print(f"[server] Zeitlimit ({int(timeout_s)} s) erreicht: "
                      f"{_status_text(snap)}", file=sys.stderr)
                return 1
            if time.monotonic() - letzte_meldung >= 15.0:
                print(_status_text(snap))
                letzte_meldung = time.monotonic()
            await asyncio.sleep(intervall_s)


def _backend_auswahl() -> tuple[str, ...]:
    """Backend-Namen fuer ``server.sh``: Registry, sonst die eingebauten."""
    try:
        providers = load_registry()
    except (RegistryError, OSError) as exc:
        logger.warning("Provider-Registry nicht lesbar: %s", exc)
        return BACKEND_NAMES
    return tuple(_backends_from_providers(providers)) or BACKEND_NAMES


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "provider":
        from .provider_cli import main as provider_main
        return provider_main(args_list[1:])
    backend_auswahl = _backend_auswahl()
    parser = argparse.ArgumentParser(
        prog="server.sh",
        description="Modellserver (Qwen/SGLang oder Ollama: Flint, GPT-OSS, "
                    "Abliterated) starten, stoppen, wechseln oder Status "
                    "zeigen (Dashboard bleibt davon unberuehrt).")
    parser.add_argument(
        "action",
        choices=("start", "stop", "status", "restart", "switch"))
    parser.add_argument(
        "backend", nargs="?", choices=backend_auswahl,
        help=f"{', '.join(backend_auswahl)} "
             f"(Default: laufendes Backend bzw. {DEFAULT_BACKEND})")
    args = parser.parse_args(args_list)
    logging.basicConfig(level=logging.WARNING)

    if args.action == "switch" and args.backend is None:
        parser.error("switch braucht ein Backend: "
                     f"{', '.join(backend_auswahl)}")

    if args.action == "restart":
        rc = asyncio.run(_mit_client("stop", timeout_s=90.0,
                                     backend=args.backend))
        if rc != 0:
            return rc
        return asyncio.run(_mit_client("start", timeout_s=900.0,
                                       backend=args.backend))
    if args.action == "switch":
        return asyncio.run(_mit_client("start", timeout_s=900.0,
                                       backend=args.backend))
    timeout = 900.0 if args.action == "start" else 90.0
    return asyncio.run(_mit_client(args.action, timeout_s=timeout,
                                   backend=args.backend))


if __name__ == "__main__":
    raise SystemExit(main())
