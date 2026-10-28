"""Start/Stop/Status der Modell-Backends (Qwen/SGLang und Flint/Ollama).

Eine Quelle fuer Dashboard und ``server.sh``: Das Gateway ruft dieselbe
:class:`ServerControl` auf wie die CLI. Beide Backends lauschen auf demselben
Upstream-Port; ein Wechsel stoppt das laufende Backend, wartet auf die
Portfreigabe und startet das andere.

Qwen laeuft ueber die systemd-User-Unit ``escha-server.service`` (falls
installiert, sonst ueber ``runtime/sglang/start-penalty.sh`` /
``stop-server.sh``). Flint laeuft als eigener ``ollama serve``-Prozess
(``runtime/flint/start-flint.sh`` / ``stop-flint.sh``, PID-File + ``/api/version``).

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

import httpx

from .config import Config, load_config

logger = logging.getLogger(__name__)

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
BACKEND_NAMES = ("qwen", "flint", "gptoss")

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


@dataclass(frozen=True)
class Backend:
    """Ein Modellserver hinter demselben Upstream-Port.

    Genau eines der Backends laeuft: SGLang (Qwen) ueber die systemd-Unit,
    Flint und GPT-OSS ueber einen eigenen Ollama-Prozess mit PID-File.
    ``health_path`` unterscheidet sich, weil Ollama kein ``/health`` kennt.
    """

    name: str
    label: str
    health_path: str
    unit: str | None = None
    start_script: str | None = None
    stop_script: str | None = None
    process_pattern: str | None = None
    pidfile: str | None = None


class ServerControlError(RuntimeError):
    """Start/Stop ist fehlgeschlagen; ``str(exc)`` ist die Meldung fuer die UI."""


class ServerConflictError(ServerControlError):
    """Es laeuft bereits ein Start- oder Stop-Uebergang."""


def standard_repo_root() -> Path:
    """Repo-Wurzel dieses Checkouts (``.../aiserver-monitor/aiserver_monitor``)."""
    return Path(__file__).resolve().parents[2]


def _backend_zustand(aktiv: bool, health: bool) -> str:
    """Zustand eines Backends: Prozess/Unit plus Health."""
    if aktiv and health:
        return "running"
    return "starting" if aktiv else "stopped"


def _aktives_backend(zustaende: dict[str, str]) -> str | None:
    """Backend, das die UI als aktiv zeigen soll (running vor starting)."""
    for gesucht in ("running", "starting"):
        for name in BACKEND_NAMES:
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
            runner: Callable[[Sequence[str]], Awaitable[tuple[int, str, str]]]
            | None = None,
            spawner: Callable[[Sequence[str]], None] | None = None,
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
        self._backends: dict[str, Backend] = {
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
        }
        #: Kompatibilitaet: ``unit`` ist die Unit des Qwen-Backends.
        self._unit = unit
        self._runner = runner or self._run_subprocess
        self._spawner = spawner or self._spawn_subprocess
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

    # --- Konstruktion aus der Gateway-Config --------------------------------

    @classmethod
    def from_config(cls, config: Config, *,
                    client: httpx.AsyncClient | None = None) -> "ServerControl":
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
            client=client,
        )

    # --- Beobachtung --------------------------------------------------------

    async def snapshot(self) -> dict:
        """Aktueller Zustand als JSON-faehiges Dict (ohne ``enabled``)."""
        jetzt = self._clock()
        unit_installiert = await self._unit_installiert()
        qwen_aktiv = (
            await self._unit_aktiv(unit_installiert)
            or await self._prozess_laeuft()
        )

        zustaende: dict[str, str] = {}
        for name, backend_info in self._backends.items():
            if backend_info.unit is not None:
                aktiv = qwen_aktiv
                health = await self._health_ok(backend_info) if aktiv else False
            else:
                # Ollama-Backends teilen sich /api/version; nur das PID-File
                # sagt, welcher Prozess laeuft. Health entscheidet danach
                # zwischen "starting" und "running".
                aktiv = self._pid_laeuft(backend_info.pidfile)
                health = await self._health_ok(backend_info) if aktiv else False
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

        backend = self._transition_backend or _aktives_backend(zustaende)
        gewaehlt = self._backends.get(backend or DEFAULT_BACKEND)
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
            ziel = self._transition_backend or DEFAULT_BACKEND
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
                    f"{int(self._start_timeout_s)} s - Log pruefen.")
            elif (not wechsel_laeuft
                  and all(z == "stopped" for z in zustaende.values())
                  and vergangen >= self._start_grace_s):
                self._transition = None
                self._transition_backend = None
                self._error = "Der Server wurde beim Start beendet."
        else:  # stopping
            if all(z == "stopped" for z in zustaende.values()):
                self._transition = None
                self._transition_backend = None
                self._error = None
            elif vergangen >= self._stop_timeout_s:
                self._transition = None
                self._transition_backend = None
                self._error = (
                    "Der Stopp dauert laenger als "
                    f"{int(self._stop_timeout_s)} s - Log pruefen.")

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
            ziel = backend or vorher["backend"] or DEFAULT_BACKEND
            if vorher["state"] in ("running", "starting") \
                    and vorher["backend"] == ziel:
                return vorher

            andere = [name for name in self._backends if name != ziel]
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
            if vorher["state"] == "stopped":
                return vorher

            ziel = backend or vorher["backend"] or DEFAULT_BACKEND
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
                        f"(nach {int(self._stop_timeout_s)} s).")
                await self._sleeper(self._switch_poll_s)
            self._transition_start = self._clock()
            await self._start_backend(ziel)
        except Exception as exc:
            self._transition = None
            self._transition_backend = None
            self._error = f"Wechsel auf {ziel} fehlgeschlagen: {exc}"

    async def _start_backend(self, name: str) -> None:
        backend = self._backends[name]
        if backend.unit and await self._unit_installiert():
            rc, out, err = await self._runner(
                ["systemctl", "--user", "start", "--no-block", backend.unit])
            if rc != 0:
                self._transition = None
                self._error = self._fehlertext(
                    "systemctl start", rc, out, err)
                raise ServerControlError(self._error)
            return
        self._spawner(["bash", str(self._skript(backend.start_script))])

    async def _stop_backend(self, name: str) -> None:
        backend = self._backends[name]
        if backend.unit:
            unit_installiert = await self._unit_installiert()
            if unit_installiert and await self._unit_aktiv(unit_installiert):
                rc, out, err = await self._runner(
                    ["systemctl", "--user", "stop", "--no-block",
                     backend.unit])
                if rc != 0:
                    self._transition = None
                    self._error = self._fehlertext(
                        "systemctl stop", rc, out, err)
                    raise ServerControlError(self._error)
                return
        self._spawner(["bash", str(self._skript(backend.stop_script))])

    # --- Zustandsquellen ----------------------------------------------------

    async def _unit_installiert(self) -> bool:
        rc, _out, _err = await self._runner(
            ["systemctl", "--user", "cat", self._unit])
        return rc == 0

    async def _unit_aktiv(self, unit_installiert: bool) -> bool:
        if not unit_installiert:
            return False
        rc, _out, _err = await self._runner(
            ["systemctl", "--user", "is-active", "--quiet", self._unit])
        return rc == 0

    async def _prozess_laeuft(self) -> bool:
        muster = self._backends["qwen"].process_pattern
        if not muster:
            return False
        rc, _out, _err = await self._runner(["pgrep", "-f", muster])
        return rc == 0

    async def _backend_aktiv(self, name: str) -> bool:
        """Laeuft das Backend (Prozess/Unit bzw. PID-File)? Ohne Health."""
        backend = self._backends[name]
        if backend.pidfile is not None:
            return self._pid_laeuft(backend.pidfile)
        if backend.unit is not None:
            unit_installiert = await self._unit_installiert()
            return (await self._unit_aktiv(unit_installiert)
                    or await self._prozess_laeuft())
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
        url = f"{self._upstream_base}{backend.health_path}"
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

    def _skript(self, name: str) -> Path:
        pfad = Path(name)
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
                "braucht 2-4 Minuten, Flint und GPT-OSS wenige Sekunden.")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="server.sh",
        description="Modellserver (Qwen/SGLang, Flint/Ollama oder GPT-OSS/Ollama) "
                    "starten, stoppen, wechseln oder Status zeigen "
                    "(Dashboard bleibt davon unberuehrt).")
    parser.add_argument(
        "action",
        choices=("start", "stop", "status", "restart", "switch"))
    parser.add_argument(
        "backend", nargs="?", choices=BACKEND_NAMES,
        help=f"{', '.join(BACKEND_NAMES)} "
             f"(Default: laufendes Backend bzw. {DEFAULT_BACKEND})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    if args.action == "switch" and args.backend is None:
        parser.error("switch braucht ein Backend: "
                     f"{', '.join(BACKEND_NAMES)}")

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
