"""Provider-Routing, Auto-Load und Idle-Unload fuer das Gateway.

Die Registry (``providers/*.toml``) sagt, welcher Modellname zu welchem
Provider gehoert und wie er gestartet wird. Dieses Modul verbindet beides mit
:class:`~aiserver_monitor.serverctl.ServerControl`:

* :meth:`ProviderRouter.prepare` bildet einen Request-Modellnamen auf einen
  Provider ab und stellt ihn bei Bedarf bereit (``auto_load``).
* Vor dem Start werden laufende Provider mit ``exclusive_gpu=true`` gestoppt
  (VRAM-Guard); schlaegt der Start fehl, nennt die Fehlermeldung das Ende der
  Provider-Logdatei.
* :meth:`ProviderRouter.run_idle_loop` stoppt Provider mit
  ``idle_unload_min > 0`` nach ihrer Ruhezeit - nie waehrend eines aktiven
  Requests.

Das Modul enthaelt keine HTTP-Routen; die Provider-API liegt in
``webapi.py``. So bleibt ``proxy.py`` unveraendert und die bestehende
OpenAI-Passthrough-Logik intakt.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .registry import (
    Provider,
    RegistryError,
    default_registry_dir,
    load_registry,
)
from .serverctl import ServerControl, ServerControlError

logger = logging.getLogger(__name__)

#: Wie lange ein Auto-Load hoechstens auf Health wartet (Sekunden).
DEFAULT_START_TIMEOUT_S = 600.0

#: Zeitlimit fuer den Stopp eines Providers im VRAM-Guard/Idle-Unload.
DEFAULT_STOP_TIMEOUT_S = 60.0

#: Abstand zwischen zwei Idle-Pruefungen (Sekunden).
DEFAULT_IDLE_POLL_S = 30.0

#: Abstand zwischen zwei Health-/Zustandspruefungen (Sekunden).
DEFAULT_HEALTH_POLL_S = 1.0

#: Registry hoechstens so oft neu von der Platte lesen (Sekunden).
DEFAULT_REGISTRY_TTL_S = 2.0


class ProviderUnavailable(RuntimeError):
    """Ein Provider ist gestoppt/disabled und kann nicht bedient werden.

    ``log_tail`` traegt die letzten Zeilen der Provider-Logdatei, damit die
    Gateway-Antwort (503) die Ursache ohne zweite Abfrage zeigt.
    """

    def __init__(self, message: str, *, provider: str | None = None,
                 log_tail: list[str] | None = None,
                 status_code: int = 503):
        super().__init__(message)
        self.provider = provider
        self.log_tail = list(log_tail or [])
        self.status_code = status_code


@dataclass(frozen=True)
class Route:
    """Ziel eines Requests: Provider plus dessen HTTP-Wurzel."""

    provider: Provider
    base_url: str


class ProviderRouter:
    """Modellname -> Provider, inklusive Lebenszyklus und Ruhezeiten.

    ``clock``/``sleeper`` sind injizierbar, damit Tests ohne echte Wartezeit
    laufen. ``registry_dir`` zeigt auf die Registry des Gateways; ohne Angabe
    gilt :func:`default_registry_dir` (bzw. ``GANTRY_PROVIDERS_DIR``).
    """

    def __init__(
            self,
            config: Config,
            server_control: ServerControl,
            *,
            registry_dir: Path | str | None = None,
            clock: Callable[[], float] = time.monotonic,
            sleeper: Callable[[float], Awaitable[None]] | None = None,
            start_timeout_s: float = DEFAULT_START_TIMEOUT_S,
            stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
            idle_poll_s: float = DEFAULT_IDLE_POLL_S,
            health_poll_s: float = DEFAULT_HEALTH_POLL_S,
            registry_ttl_s: float = DEFAULT_REGISTRY_TTL_S,
    ):
        self._config = config
        self._control = server_control
        self._registry_dir = (Path(registry_dir).expanduser()
                              if registry_dir is not None
                              else default_registry_dir())
        self._clock = clock
        self._sleeper = sleeper or asyncio.sleep
        self._start_timeout_s = float(start_timeout_s)
        self._stop_timeout_s = float(stop_timeout_s)
        self._idle_poll_s = float(idle_poll_s)
        self._health_poll_s = float(health_poll_s)
        self._registry_ttl_s = float(registry_ttl_s)
        self._cache: dict[str, Provider] | None = None
        self._cache_at: float | None = None
        self._last_used: dict[str, float] = {}
        self._active: dict[str, int] = {}
        self._running_since: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # --- Registry -----------------------------------------------------------

    @property
    def registry_dir(self) -> Path:
        return self._registry_dir

    @property
    def start_timeout_s(self) -> float:
        return self._start_timeout_s

    @property
    def stop_timeout_s(self) -> float:
        return self._stop_timeout_s

    def providers(self) -> dict[str, Provider]:
        """Registry-Provider, kurz gecacht (TTL), nie eine Exception nach aussen."""
        jetzt = self._clock()
        if (self._cache is not None and self._cache_at is not None
                and jetzt - self._cache_at < self._registry_ttl_s):
            return dict(self._cache)
        try:
            providers = load_registry(self._registry_dir)
        except (RegistryError, OSError) as exc:
            logger.warning("Provider-Registry nicht lesbar (%s); "
                           "Routing bleibt beim alten Stand.", exc)
            providers = dict(self._cache or {})
        self._cache = providers
        self._cache_at = jetzt
        return dict(providers)

    def reload(self) -> None:
        """Registry-Cache verwerfen (nach PATCH/Anlegen eines Providers)."""
        self._cache = None
        self._cache_at = None

    # --- Modell -> Provider -------------------------------------------------

    def match(self, model: str | None) -> tuple[Provider, str] | None:
        """Exakter Treffer auf ``model``, ``id`` oder ``name``.

        Rueckgabe ist ``(Provider, art)`` mit ``art`` in ``model``/``id``/
        ``name``; ``None`` heisst "kein Provider, bestehender Pfad". Die
        Modellnamen haben Vorrang, damit ein Modell nie versehentlich ueber
        eine gleichnamige ID eines anderen Providers laeuft.
        """
        if not isinstance(model, str) or not model:
            return None
        providers = self.providers()
        for provider in providers.values():
            if provider.model and model == provider.model:
                return provider, "model"
        for provider in providers.values():
            if model == provider.id:
                return provider, "id"
        for provider in providers.values():
            if model == provider.name:
                return provider, "name"
        return None

    def base_url(self, provider: Provider) -> str:
        """HTTP-Wurzel des Providers; Port 0 faellt auf den Upstream zurueck."""
        if provider.port:
            host = provider.host or "127.0.0.1"
            return f"http://{host}:{provider.port}"
        return self._config.upstream_base.rstrip("/")

    async def prepare(self, model: str | None) -> Route | None:
        """Route fuer ``model`` oder ``None`` (bestehender Passthrough).

        Der globale Alias ``config.model_alias`` behaelt seine bisherige
        Bedeutung ("aktuell geladenes Backend") und laeuft nicht ueber die
        Provider-Zuordnung. Alle anderen exakten Treffer auf ``model``,
        ``id`` oder ``name`` gehen an den Provider; ist er gestoppt und
        ``auto_load=false``, endet der Request mit 503.
        """
        treffer = self.match(model)
        if treffer is None:
            return None
        provider, _art = treffer
        if (provider.model and model == self._config.model_alias
                and self._config.model_alias):
            # Der globale Alias bedeutet historisch "aktuell geladenes
            # Backend"; diese Semantik bleibt erhalten.
            return None

        snap = await self._control.snapshot()
        states = self.observe(snap)
        state = states.get(provider.id, "stopped")
        if state == "running":
            return Route(provider, self.base_url(provider))

        auto_load = bool(provider.settings.get("auto_load", True))
        if not auto_load:
            raise ProviderUnavailable(
                f"Provider {provider.id!r} ist gestoppt und "
                "auto_load ist deaktiviert.",
                provider=provider.id,
                log_tail=self._control.log_lines(provider.id, 40))

        await self.ensure_ready(provider, states=states, state=state)
        return Route(provider, self.base_url(provider))

    async def ensure_ready(self, provider: Provider, *,
                           states: Mapping[str, str] | None = None,
                           state: str | None = None,
                           timeout_s: float | None = None) -> None:
        """Startet ``provider`` bei Bedarf und wartet auf Health.

        Der VRAM-Guard stoppt vorher alle laufenden Provider mit
        ``exclusive_gpu=true``. Jeder Fehler wird zu
        :class:`ProviderUnavailable` mit Log-Tail.
        """
        lock = self._locks.setdefault(provider.id, asyncio.Lock())
        async with lock:
            if states is None or state is None:
                snap = await self._control.snapshot()
                states = self.observe(snap)
                state = states.get(provider.id, "stopped")

            if state == "running":
                self.touch(provider.id)
                return
            if not provider.enabled:
                raise ProviderUnavailable(
                    f"Provider {provider.id!r} ist deaktiviert.",
                    provider=provider.id,
                    log_tail=self._control.log_lines(provider.id, 40))
            if not bool(provider.settings.get("auto_load", True)):
                raise ProviderUnavailable(
                    f"Provider {provider.id!r} ist gestoppt und "
                    "auto_load ist deaktiviert.",
                    provider=provider.id,
                    log_tail=self._control.log_lines(provider.id, 40))

            # Ein laufender Stopp/Start eines anderen Providers: kurz warten,
            # damit Auto-Load nicht in einen 409 laeuft.
            await self._warte_auf_freie_steuerung(provider.id)

            # Der ServerControl kennt neue/geaenderte Provider erst nach
            # einem Refresh (Registry-PATCH, neuer Provider).
            if provider.id not in self._control.backends:
                self._control.refresh_from_registry(self._registry_dir)
            if provider.id not in self._control.backends:
                raise ProviderUnavailable(
                    f"Provider {provider.id!r} ist der Server-Steuerung "
                    "unbekannt (Registry nicht ladbar?).",
                    provider=provider.id)

            await self._vram_guard(provider)

            try:
                await self._control.start_and_wait(
                    provider.id,
                    timeout_s=(self._start_timeout_s if timeout_s is None
                               else timeout_s))
            except ServerControlError as exc:
                raise ProviderUnavailable(
                    f"Provider {provider.id!r} konnte nicht gestartet "
                    f"werden: {exc}",
                    provider=provider.id,
                    log_tail=self._control.log_lines(provider.id, 60)) from exc
            self.touch(provider.id)

    async def _warte_auf_freie_steuerung(self, provider_id: str) -> None:
        """Wartet, bis kein anderer Start/Stopp mehr laeuft (Zeitlimit)."""
        deadline = self._clock() + self._stop_timeout_s
        while getattr(self._control, "transitioning", False):
            snap = await self._control.snapshot()
            states = self.observe(snap)
            if states.get(provider_id) == "running":
                return
            if self._clock() >= deadline:
                raise ProviderUnavailable(
                    f"Provider {provider_id!r}: Server-Steuerung ist nach "
                    f"{int(self._stop_timeout_s)} s noch belegt.",
                    provider=provider_id,
                    log_tail=self._control.log_lines(provider_id, 40))
            await self._sleeper(self._health_poll_s)

    async def _vram_guard(self, provider: Provider) -> None:
        """Stoppt laufende Provider, die dieselbe GPU exklusiv brauchen."""
        if not bool(provider.settings.get("exclusive_gpu", False)):
            return
        eigene = self.providers()
        snap = await self._control.snapshot()
        states = self.observe(snap)
        for name, other in eigene.items():
            if name == provider.id:
                continue
            if not bool(other.settings.get("exclusive_gpu", False)):
                continue
            if states.get(name) != "running":
                continue
            logger.info("VRAM-Guard: stoppe %s vor Start von %s",
                        name, provider.id)
            try:
                await self._control.stop_and_wait(
                    name, timeout_s=self._stop_timeout_s)
            except ServerControlError as exc:
                raise ProviderUnavailable(
                    f"VRAM-Guard: Provider {name!r} liess sich nicht "
                    f"stoppen: {exc}",
                    provider=provider.id,
                    log_tail=self._control.log_lines(name, 40)) from exc

    # --- Aktivitaet / Idle-Unload ------------------------------------------

    def touch(self, provider_id: str) -> None:
        """Markiert einen Provider als gerade benutzt (Idle-Uhr zuruecksetzen)."""
        self._last_used[provider_id] = self._clock()

    def acquire(self, provider_id: str) -> None:
        """Beginnt einen aktiven Request (Idle-Unload sperrt den Provider)."""
        self._active[provider_id] = self._active.get(provider_id, 0) + 1
        self.touch(provider_id)

    def release(self, provider_id: str) -> None:
        """Beendet einen aktiven Request."""
        rest = self._active.get(provider_id, 0) - 1
        if rest > 0:
            self._active[provider_id] = rest
        else:
            self._active.pop(provider_id, None)
        self.touch(provider_id)

    def active_requests(self, provider_id: str) -> int:
        return self._active.get(provider_id, 0)

    def last_used(self, provider_id: str) -> float | None:
        return self._last_used.get(provider_id)

    def observe(self, snap: Mapping) -> dict[str, str]:
        """Zustaende aus einem ServerControl-Snapshot uebernehmen.

        Aktualisiert zugleich den Startzeitpunkt fuer ``uptime``.
        """
        jetzt = self._clock()
        states: dict[str, str] = {}
        for eintrag in snap.get("backends", []) if isinstance(snap, Mapping) else []:
            if not isinstance(eintrag, Mapping):
                continue
            name = eintrag.get("name")
            state = eintrag.get("state")
            if isinstance(name, str) and isinstance(state, str):
                states[name] = state
                if state == "running":
                    self._running_since.setdefault(name, jetzt)
                else:
                    self._running_since.pop(name, None)
        return states

    def uptime(self, provider_id: str) -> int:
        """Sekunden seit dem ersten "running"-Beobachten (0, wenn gestoppt)."""
        seit = self._running_since.get(provider_id)
        if seit is None:
            return 0
        return max(0, int(self._clock() - seit))

    async def status(self) -> tuple[dict[str, str], dict[str, str]]:
        """Aktuelle Zustaende plus Backend-Metadaten (fuer die Provider-API)."""
        snap = await self._control.snapshot()
        states = self.observe(snap)
        meta = {eintrag.get("name"): eintrag
                for eintrag in snap.get("backends", [])
                if isinstance(eintrag, Mapping) and eintrag.get("name")}
        return states, meta

    async def idle_check_once(self) -> list[str]:
        """Stoppt faellige Provider; Rueckgabe der gestoppten IDs."""
        providers = self.providers()
        snap = await self._control.snapshot()
        states = self.observe(snap)
        jetzt = self._clock()
        gestoppt: list[str] = []
        for provider in providers.values():
            minuten = int(provider.settings.get("idle_unload_min") or 0)
            if minuten <= 0:
                continue
            if self._active.get(provider.id, 0) > 0:
                continue
            if states.get(provider.id) != "running":
                continue
            letzte = self._last_used.get(provider.id)
            if letzte is None:
                # Gerade erst gesehen: die Ruhezeit beginnt jetzt.
                self._last_used[provider.id] = jetzt
                continue
            if jetzt - letzte < minuten * 60:
                continue
            try:
                await self._control.stop_and_wait(
                    provider.id, timeout_s=self._stop_timeout_s)
            except ServerControlError as exc:
                logger.warning("Idle-Unload von %s fehlgeschlagen: %s",
                               provider.id, exc)
                continue
            self._last_used.pop(provider.id, None)
            gestoppt.append(provider.id)
            logger.info("Idle-Unload: %s nach %d min gestoppt",
                        provider.id, minuten)
        return gestoppt

    async def run_idle_loop(self) -> None:
        """Hintergrund-Task: prueft regelmaessig auf faellige Idle-Provider."""
        while True:
            await self._sleeper(self._idle_poll_s)
            try:
                await self.idle_check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Idle-Unload-Pruefung fehlgeschlagen",
                               exc_info=True)
