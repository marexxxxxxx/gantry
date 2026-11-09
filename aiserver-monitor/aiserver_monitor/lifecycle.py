"""Modell-Lebenszyklus fuer Pipeline-Schritte: load, unload, status.

Der Aufrufer arbeitet mit exakt denselben Modellnamen, die er auch als
``model`` an die Inferenz-Endpunkte schickt. Ein Profil ist deshalb der
Modellname selbst; das Backend (Qwen, Flint, GPT-OSS, Abliterated oder
Embedding) kommt aus der Konfiguration, nie aus dem Request.

``load`` ist additiv: ein Embedding-Modell kann neben einem Chat-Modell
laufen, aber hoechstens ein Chat- oder TTS-Backend gleichzeitig. ``unload``
gibt Modellgewichte frei (Ollama ``keep_alive: 0``, Server-Stopp bei Qwen,
Worker-Stopp bei Qwen3-TTS); die Ollama-/Embedding-Prozesse bleiben am
Leben. Beide Operationen sind idempotent und laufen ueber ein Lock
nacheinander.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from .config import Config
from .serverctl import (
    ServerConflictError,
    ServerControlError,
    standard_repo_root,
)

#: Timeouts der kurzen Statusabfragen (Sekunden).
_PS_TIMEOUT_S = 2.0
_VERSION_TIMEOUT_S = 2.0

#: Timeouts fuer das Vorladen bzw. Entladen von Gewichten (Sekunden).
_PRELOAD_TIMEOUT_S = 300.0
_UNLOAD_TIMEOUT_S = 30.0

#: Hosts, fuer die der Embedding-Sidecar lokal gestartet werden darf.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LifecycleError(RuntimeError):
    """Basisfehler des Lebenszyklus (Antwort 502)."""


class UnknownProfileError(LifecycleError):
    """Mindestens ein Modellname ist nicht in der Konfiguration."""

    def __init__(self, unknown: list[str], profiles: list[str]):
        self.unknown = list(unknown)
        self.profiles = list(profiles)
        super().__init__(
            "Unbekannte Modellnamen: " + ", ".join(self.unknown))


class LifecycleConflictError(LifecycleError):
    """Chat und TTS gleichzeitig oder zwei Backends (Antwort 409)."""


class LifecycleDisabledError(LifecycleError):
    """Chat-Modelle brauchen die Server-Steuerung (Antwort 503)."""


class LifecycleTimeoutError(LifecycleError):
    """Laden/Entladen wurde nicht rechtzeitig fertig (Antwort 504)."""


class LifecycleUnavailableError(LifecycleError):
    """Das Modell kann auf dieser Maschine nicht geladen werden (503)."""


@dataclass(frozen=True)
class ModelProfile:
    """Ein ladbarer Modellname und das Backend, das ihn bedient."""

    name: str
    backend: str
    kind: str  # "chat", "tts" oder "embedding"


@dataclass(frozen=True)
class TTSBackend:
    """Ein GPU-TTS-Worker mit eigenem Prozess, Port und Skripten."""

    backend: str
    label: str
    base: str
    model_dir: str
    start_script: str
    stop_script: str
    pidfile: str
    health_path: str
    load_timeout_s: float
    unload_timeout_s: float
    env_prefix: str
    extra_env: tuple[tuple[str, str], ...] = ()


class ProfileCatalog:
    """Profile aus der Config, in fester Reihenfolge und ohne Duplikate.

    Namen werden exakt uebernommen: keine Trim-, Case- oder Pfad-
    Normalisierung, damit Load, Inferenz, Unload und Status denselben
    String treffen.
    """

    def __init__(self, config: Config):
        paare = [
            (config.model_alias, "qwen", "chat"),
            (config.flint_model, "flint", "chat"),
            (config.gptoss_model, "gptoss", "chat"),
            (config.abl_model, "abl", "chat"),
            (config.qwen_tts_model, "qwen-tts", "tts"),
            (config.embed_model, "embed", "embedding"),
        ]
        self._profiles: list[ModelProfile] = []
        gesehen: set[str] = set()
        for name, backend, kind in paare:
            if not name or name in gesehen:
                continue
            gesehen.add(name)
            self._profiles.append(ModelProfile(name, backend, kind))

    @property
    def profiles(self) -> list[ModelProfile]:
        return list(self._profiles)

    @property
    def names(self) -> list[str]:
        return [profil.name for profil in self._profiles]

    def get(self, name: str) -> ModelProfile | None:
        for profil in self._profiles:
            if profil.name == name:
                return profil
        return None

    def resolve(self, names: list[str]) -> list[ModelProfile]:
        """Validiert alle Namen, bevor irgendetwas geladen/entladen wird."""
        profile: list[ModelProfile] = []
        gesehen: set[str] = set()
        unbekannt: list[str] = []
        for name in names:
            treffer = self.get(name)
            if treffer is None:
                unbekannt.append(name)
            elif name not in gesehen:
                gesehen.add(name)
                profile.append(treffer)
        if unbekannt:
            raise UnknownProfileError(unbekannt, self.names)
        return profile


def _profil_dict(profil: ModelProfile) -> dict:
    return {
        "profile": profil.name,
        "backend": profil.backend,
        "kind": profil.kind,
    }


def _standard_spawner(repo_root: Path):
    """Startet Sidecar-Skripte im Hintergrund (Test-Injektion moeglich)."""

    def spawn(args: list[str], env: dict) -> object:
        return subprocess.Popen(
            args,
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

    return spawn


class ModelLifecycle:
    """Load/Unload/Status ueber ServerControl und die Ollama-Sidecars."""

    def __init__(
        self,
        config: Config,
        server_control,
        *,
        client: httpx.AsyncClient,
        repo_root: Path | str | None = None,
        spawner: Callable[[list[str], dict], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable] = asyncio.sleep,
        poll_s: float = 0.5,
        load_timeout_s: float = 900.0,
        unload_timeout_s: float = 60.0,
        embed_start_script: str | None = None,
    ):
        self.config = config
        self.server_control = server_control
        self.client = client
        self.repo_root = (Path(repo_root) if repo_root is not None
                          else standard_repo_root())
        self.catalog = ProfileCatalog(config)
        self._spawner = spawner or _standard_spawner(self.repo_root)
        self._clock = clock
        self._sleeper = sleeper
        self.poll_s = poll_s
        self.load_timeout_s = load_timeout_s
        self.unload_timeout_s = unload_timeout_s
        self.embed_start_script = (
            embed_start_script
            if embed_start_script is not None
            else config.embed_start_script)
        self._lock = asyncio.Lock()

    async def status(self) -> dict:
        """Aktuell geladene Profile plus alle ladbaren Namen."""
        snap = await self.server_control.snapshot()
        state = snap.get("state")
        backend = snap.get("backend")

        chat_modelle: set[str] = set()
        if state == "running" and backend in ("flint", "gptoss", "abl"):
            chat_modelle = await self._ollama_modelle(self.config.upstream_base)
        embed_modelle = await self._ollama_modelle(self.config.embed_base)

        tts_backends = self._tts_backends()
        geladen = []
        for profil in self.catalog.profiles:
            if profil.kind == "chat":
                if state != "running" or backend != profil.backend:
                    continue
                if profil.backend == "qwen" or profil.name in chat_modelle:
                    geladen.append(_profil_dict(profil))
            elif profil.kind == "tts":
                tts_backend = tts_backends.get(profil.backend)
                if (tts_backend is not None
                        and await self._tts_bereit(tts_backend)):
                    geladen.append(_profil_dict(profil))
            elif profil.name in embed_modelle:
                geladen.append(_profil_dict(profil))
        return {"profiles": self.catalog.names, "loaded": geladen}

    async def load(self, names: list[str]) -> dict:
        """Laedt die genannten Profile; gibt den Zustand danach zurueck."""
        profile = self.catalog.resolve(names)
        chat = [p for p in profile if p.kind == "chat"]
        tts = [p for p in profile if p.kind == "tts"]
        backends = {p.backend for p in chat + tts}
        if len(backends) > 1:
            raise LifecycleConflictError(
                "Es kann nur ein Chat- oder TTS-Backend gleichzeitig laufen: "
                + ", ".join(sorted(backends)))
        if chat and not self.config.server_control_enabled:
            raise LifecycleDisabledError(
                "Server-Steuerung ist deaktiviert.")

        async with self._lock:
            vorher = await self.status()
            vorher_namen = {eintrag["profile"] for eintrag in vorher["loaded"]}
            tts_offen = [p for p in tts if p.name not in vorher_namen]
            if tts_offen:
                snap = await self.server_control.snapshot()
                if snap.get("state") in ("running", "starting"):
                    raise LifecycleConflictError(
                        "Chat-Backend laeuft noch; zuerst /v1/unload "
                        "ausfuehren.")
            if chat:
                for tts_backend in self._tts_backends().values():
                    if await self._tts_bereit(tts_backend):
                        raise LifecycleConflictError(
                            f"{tts_backend.label} laeuft noch; "
                            "zuerst entladen.")

            chat_offen = [p for p in chat if p.name not in vorher_namen]
            embed_offen = any(
                p.kind == "embedding" and p.name not in vorher_namen
                for p in profile)

            aufgaben = []
            if chat_offen:
                aufgaben.append(self._ensure_chat(chat_offen[0]))
            if tts_offen:
                aufgaben.append(self._ensure_tts(tts_offen[0]))
            if embed_offen:
                aufgaben.append(self._ensure_embed())
            if aufgaben:
                await asyncio.gather(*aufgaben)

            nachher = await self.status()
            nachher_namen = {eintrag["profile"]
                             for eintrag in nachher["loaded"]}
            entladen = [eintrag for eintrag in vorher["loaded"]
                        if eintrag["profile"] not in nachher_namen]
            return {
                "requested": [p.name for p in profile],
                "profiles": self.catalog.names,
                "loaded": nachher["loaded"],
                "unloaded": entladen,
            }

    async def unload(self, names: list[str] | None = None) -> dict:
        """Entlaedt gezielte Profile oder alle; Gewichte werden freigegeben."""
        profile = (self.catalog.profiles if names is None
                   else self.catalog.resolve(names))
        chat = [p for p in profile if p.kind == "chat"]
        if chat and not self.config.server_control_enabled:
            snap = await self.server_control.snapshot()
            aktive_backends = {p.backend for p in chat}
            if (snap.get("state") in ("running", "starting")
                    and snap.get("backend") in aktive_backends):
                raise LifecycleDisabledError(
                    "Server-Steuerung ist deaktiviert.")

        async with self._lock:
            vorher = await self.status()
            vorher_namen = {eintrag["profile"] for eintrag in vorher["loaded"]}
            for treffer in profile:
                if treffer.name not in vorher_namen:
                    continue
                if treffer.kind == "chat":
                    await self._unload_chat(treffer)
                elif treffer.kind == "tts":
                    tts_backend = self._tts_backends().get(treffer.backend)
                    if tts_backend is not None:
                        await self._unload_tts(tts_backend)
                else:
                    await self._unload_embed()

            nachher = await self.status()
            nachher_namen = {eintrag["profile"]
                             for eintrag in nachher["loaded"]}
            entladen = [eintrag for eintrag in vorher["loaded"]
                        if eintrag["profile"] not in nachher_namen]
            return {
                "requested": ("all" if names is None
                              else [p.name for p in profile]),
                "unloaded": entladen,
                "loaded": nachher["loaded"],
                "profiles": self.catalog.names,
            }

    async def _ensure_chat(self, profil: ModelProfile) -> None:
        snap = await self.server_control.snapshot()
        if (snap.get("state") == "running"
                and snap.get("backend") == profil.backend):
            if profil.backend == "qwen":
                return
            if profil.name in await self._ollama_modelle(
                    self.config.upstream_base):
                return

        try:
            await self._start_backend(profil.backend)
        except ServerControlError as exc:
            raise LifecycleError(str(exc)) from exc
        except ValueError as exc:
            raise LifecycleError(str(exc)) from exc

        async def backend_bereit() -> bool:
            snap = await self.server_control.snapshot()
            if snap.get("error"):
                raise LifecycleError(str(snap["error"]))
            if (snap.get("state") != "running"
                    or snap.get("backend") != profil.backend):
                return False
            if profil.backend == "qwen":
                return True
            return await self._version_ok(self.config.upstream_base)

        await self._wait(backend_bereit, self.load_timeout_s,
                         f"Start von {profil.backend}")

        if profil.backend == "qwen":
            return

        modelle = await self._ollama_modelle(self.config.upstream_base)
        if profil.name not in modelle:
            await self._preload_generate(profil.name)

        async def modell_bereit() -> bool:
            return profil.name in await self._ollama_modelle(
                self.config.upstream_base)

        await self._wait(modell_bereit, self.load_timeout_s,
                         f"Laden von {profil.name}")

    async def _ensure_embed(self) -> None:
        if self.config.embed_model in await self._ollama_modelle(
                self.config.embed_base):
            return
        if not await self._version_ok(self.config.embed_base):
            self._start_embed_sidecar()
            await self._wait(
                lambda: self._version_ok(self.config.embed_base),
                self.load_timeout_s,
                "Start des Embedding-Sidecars")

        try:
            antwort = await self.client.post(
                f"{self.config.embed_base}/api/embed",
                json={"model": self.config.embed_model,
                      "input": "ping", "keep_alive": -1},
                timeout=_PRELOAD_TIMEOUT_S)
            antwort.raise_for_status()
        except httpx.HTTPError as exc:
            raise LifecycleError(
                f"Embedding-Vorladen fehlgeschlagen: {exc}") from exc

        await self._wait(
            lambda: self._modell_in_ps(
                self.config.embed_base, self.config.embed_model),
            self.load_timeout_s,
            f"Laden von {self.config.embed_model}")

    def _tts_backends(self) -> dict[str, TTSBackend]:
        """Alle konfigurierten GPU-TTS-Worker, nach Backend-Schluessel."""
        return {
            "qwen-tts": TTSBackend(
                backend="qwen-tts",
                label="Qwen3-TTS",
                base=self.config.qwen_tts_base,
                model_dir=self.config.qwen_tts_model_dir,
                start_script=self.config.qwen_tts_start_script,
                stop_script=self.config.qwen_tts_stop_script,
                pidfile=self.config.qwen_tts_pidfile,
                health_path=self.config.qwen_tts_health_path,
                load_timeout_s=self.config.qwen_tts_load_timeout_s,
                unload_timeout_s=self.config.qwen_tts_unload_timeout_s,
                env_prefix="QWEN_TTS",
            ),
        }

    async def _ensure_tts(self, profil: ModelProfile) -> None:
        backend = self._tts_backends().get(profil.backend)
        if backend is None:
            raise LifecycleError(
                f"Unbekanntes TTS-Backend: {profil.backend}")
        if await self._tts_bereit(backend):
            return
        self._spawn_tts(backend, backend.start_script)

        async def worker_erreichbar() -> bool:
            return await self._tts_health(backend) is not None

        await self._wait(
            worker_erreichbar,
            backend.load_timeout_s,
            f"Start des {backend.label}-Workers")
        try:
            antwort = await self.client.post(
                f"{backend.base}/load",
                timeout=backend.load_timeout_s)
            antwort.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LifecycleTimeoutError(
                f"Laden von {backend.label} hat das Zeitlimit "
                "ueberschritten") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 503:
                raise LifecycleUnavailableError(
                    f"{backend.label} konnte nicht geladen werden: "
                    f"{exc.response.text[:200]}") from exc
            raise LifecycleError(
                f"{backend.label}-Laden fehlgeschlagen: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LifecycleError(
                f"{backend.label}-Laden fehlgeschlagen: {exc}") from exc

        await self._wait(
            lambda: self._tts_bereit(backend),
            backend.load_timeout_s,
            f"Laden von {backend.label}")

    async def _unload_tts(self, backend: TTSBackend) -> None:
        if await self._tts_health(backend) is None:
            return
        try:
            antwort = await self.client.post(
                f"{backend.base}/unload",
                timeout=_UNLOAD_TIMEOUT_S)
            antwort.raise_for_status()
        except httpx.HTTPError:
            pass
        self._spawn_tts(backend, backend.stop_script)

        async def worker_weg() -> bool:
            return await self._tts_health(backend) is None

        await self._wait(
            worker_weg,
            backend.unload_timeout_s,
            f"Stoppen von {backend.label}")

    def _tts_env(self, backend: TTSBackend) -> dict:
        teile = urlsplit(backend.base)
        if teile.hostname not in _LOOPBACK_HOSTS:
            raise LifecycleError(
                f"{backend.label}-Worker kann nur lokal gestartet werden: "
                f"{backend.base}")
        if teile.port is None:
            raise LifecycleError(
                f"{backend.label}-Basis braucht einen Port: {backend.base}")
        env = dict(os.environ)
        env[f"{backend.env_prefix}_HOST"] = teile.hostname or "127.0.0.1"
        env[f"{backend.env_prefix}_PORT"] = str(teile.port)
        env[f"{backend.env_prefix}_MODEL_DIR"] = backend.model_dir
        pidfile = Path(backend.pidfile)
        if not pidfile.is_absolute():
            pidfile = self.repo_root / pidfile
        env[f"{backend.env_prefix}_PIDFILE"] = str(pidfile)
        for name, wert in backend.extra_env:
            env[name] = wert
        return env

    def _spawn_tts(self, backend: TTSBackend, script: str) -> None:
        pfad = Path(script)
        if not pfad.is_absolute():
            pfad = self.repo_root / pfad
        try:
            self._spawner(["bash", str(pfad)], self._tts_env(backend))
        except OSError as exc:
            raise LifecycleError(
                f"{backend.label}-Worker konnte nicht gestartet werden: "
                f"{exc}") from exc

    async def _tts_health(self, backend: TTSBackend) -> dict | None:
        try:
            antwort = await self.client.get(
                f"{backend.base}{backend.health_path}",
                timeout=_VERSION_TIMEOUT_S)
        except httpx.HTTPError:
            return None
        if antwort.status_code != 200:
            return None
        try:
            daten = antwort.json()
        except ValueError:
            return None
        return daten if isinstance(daten, dict) else None

    async def _tts_bereit(self, backend: TTSBackend) -> bool:
        health = await self._tts_health(backend)
        return bool(health and health.get("model_loaded") is True)

    async def _unload_chat(self, profil: ModelProfile) -> None:
        snap = await self.server_control.snapshot()
        if (snap.get("state") not in ("running", "starting")
                or snap.get("backend") != profil.backend):
            return

        if profil.backend == "qwen":
            await self._stop_backend("qwen")

            async def gestoppt() -> bool:
                aktuell = await self.server_control.snapshot()
                return (aktuell.get("state") == "stopped"
                        and aktuell.get("backend") != "qwen")

            await self._wait(gestoppt, self.unload_timeout_s,
                             "Stoppen von qwen")
            return

        if profil.name not in await self._ollama_modelle(
                self.config.upstream_base):
            return
        try:
            antwort = await self.client.post(
                f"{self.config.upstream_base}/api/generate",
                json={"model": profil.name, "prompt": "", "stream": False,
                      "keep_alive": 0},
                timeout=_UNLOAD_TIMEOUT_S)
            antwort.raise_for_status()
        except httpx.HTTPError:
            # Sidecar nicht erreichbar: dann ist auch nichts geladen.
            return

        async def weg() -> bool:
            return profil.name not in await self._ollama_modelle(
                self.config.upstream_base)

        await self._wait(weg, self.unload_timeout_s,
                         f"Entladen von {profil.name}")

    async def _unload_embed(self) -> None:
        if self.config.embed_model not in await self._ollama_modelle(
                self.config.embed_base):
            return
        try:
            antwort = await self.client.post(
                f"{self.config.embed_base}/api/embed",
                json={"model": self.config.embed_model,
                      "input": "ping", "keep_alive": 0},
                timeout=_UNLOAD_TIMEOUT_S)
            antwort.raise_for_status()
        except httpx.HTTPError:
            return

        async def weg() -> bool:
            return self.config.embed_model not in await self._ollama_modelle(
                self.config.embed_base)

        await self._wait(weg, self.unload_timeout_s,
                         f"Entladen von {self.config.embed_model}")

    async def _start_backend(self, backend: str) -> None:
        ende = self._clock() + self.load_timeout_s
        while True:
            try:
                await self.server_control.start(backend)
                return
            except ServerConflictError:
                if self._clock() >= ende:
                    raise LifecycleTimeoutError(
                        f"Backend {backend} ist noch im Uebergang")
                await self._sleeper(self.poll_s)

    async def _stop_backend(self, backend: str) -> None:
        ende = self._clock() + self.unload_timeout_s
        while True:
            try:
                await self.server_control.stop(backend)
                return
            except ServerConflictError:
                if self._clock() >= ende:
                    raise LifecycleTimeoutError(
                        f"Backend {backend} ist noch im Uebergang")
                await self._sleeper(self.poll_s)

    async def _preload_generate(self, name: str) -> None:
        try:
            antwort = await self.client.post(
                f"{self.config.upstream_base}/api/generate",
                json={"model": name, "prompt": "", "stream": False,
                      "keep_alive": -1},
                timeout=_PRELOAD_TIMEOUT_S)
            antwort.raise_for_status()
        except httpx.HTTPError as exc:
            raise LifecycleError(
                f"Vorladen von {name} fehlgeschlagen: {exc}") from exc

    def _start_embed_sidecar(self) -> None:
        teile = urlsplit(self.config.embed_base)
        if teile.hostname not in _LOOPBACK_HOSTS:
            raise LifecycleError(
                "Embedding-Sidecar kann nur lokal gestartet werden: "
                f"{self.config.embed_base}")
        env = dict(os.environ)
        env["EMBED_HOST"] = teile.netloc
        env["EMBED_MODEL"] = self.config.embed_model
        pfad = Path(self.embed_start_script)
        if not pfad.is_absolute():
            pfad = self.repo_root / pfad
        try:
            self._spawner(["bash", str(pfad)], env)
        except OSError as exc:
            raise LifecycleError(
                f"Embedding-Sidecar konnte nicht starten: {exc}") from exc

    async def _version_ok(self, base: str) -> bool:
        try:
            antwort = await self.client.get(
                f"{base}/api/version", timeout=_VERSION_TIMEOUT_S)
            return antwort.status_code == 200
        except httpx.HTTPError:
            return False

    async def _ollama_modelle(self, base: str) -> set[str]:
        try:
            antwort = await self.client.get(
                f"{base}/api/ps", timeout=_PS_TIMEOUT_S)
            antwort.raise_for_status()
            daten = antwort.json()
        except (httpx.HTTPError, ValueError):
            return set()
        if not isinstance(daten, dict):
            return set()
        modelle = daten.get("models")
        if not isinstance(modelle, list):
            return set()
        namen = set()
        for eintrag in modelle:
            if not isinstance(eintrag, dict):
                continue
            name = eintrag.get("name") or eintrag.get("model")
            if isinstance(name, str):
                namen.add(name)
        return namen

    async def _modell_in_ps(self, base: str, name: str) -> bool:
        return name in await self._ollama_modelle(base)

    async def _wait(self, predicate: Callable[[], Awaitable[bool]],
                    timeout_s: float, was: str) -> None:
        ende = self._clock() + timeout_s
        while True:
            if await predicate():
                return
            if self._clock() >= ende:
                raise LifecycleTimeoutError(
                    f"Zeitlimit fuer {was} ueberschritten")
            await self._sleeper(self.poll_s)
