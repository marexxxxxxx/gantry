"""GPU-Arbiter: exklusive Lease/Pause fuer externe VRAM-Jobs.

Der Arbiter regelt den exklusiven Zugriff externer Jobs (3D-Render, A/V-Gen)
auf die GPU, die sich das Gantry-Gateway mit dem LLM-Backend teilt. Ablauf
eines Leases: ``request_lease`` versetzt das Gateway in PAUSING (laufende
Generierungen auslaufen lassen), entlaedt das Chat-Backend (PAUSED), gibt dem
externen Job die GPU frei und laedt nach Job-Ende (``complete``/``release``)
das vorherige Backend wieder (RESUMING -> IDLE).

Waehrend der Pause haelt ``wait_serving`` eingehende Chat-Requests in einer
HOLD-Queue (kein 503); sie laufen weiter, sobald der Zustand wieder IDLE ist.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Poll-Takt der Drain-Schleife (Sekunden). Drain wartet auf ``inflight()==0``;
#: ein kleiner Takt haelt die Pause kurz, ein zu kleiner dreht die Schleife heiss.
_DRAIN_POLL_S = 0.1


class ArbiterState(StrEnum):
    IDLE = "idle"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    ERROR = "error"


@dataclass(frozen=True)
class LeaseInfo:
    """Oeffentliche Antwort auf ``request_lease``."""

    lease_id: str
    state: str  # "granted" | "queued"
    position: int


@dataclass(frozen=True)
class JobResult:
    """Ergebnis eines ueber den Arbiter gelaufenen externen Jobs."""

    lease_id: str
    exit_code: int | None
    stdout: str
    stderr: str


@dataclass
class _Lease:
    """Interner Zustand eines Leases (mutable Deadline etc.)."""

    lease_id: str
    timeout_s: float | None
    deadline: float | None
    completion: str
    #: Wird gesetzt, sobald dieser Lease der aktive (GPU haltende) ist.
    granted: asyncio.Event = field(default_factory=asyncio.Event)


class GpuArbiter:
    """Lease-State-Machine und HOLD-Queue fuer externe GPU-Jobs."""

    def __init__(
        self,
        config,
        server_control,
        *,
        inflight: Callable[[], int],
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        subprocess_factory=asyncio.create_subprocess_exec,
    ):
        self._config = config
        self._server_control = server_control
        self._inflight = inflight
        self._clock = clock
        self._sleeper = sleeper
        self._subprocess_factory = subprocess_factory

        self._state = ArbiterState.IDLE
        self._serving = asyncio.Event()
        self._serving.set()  # IDLE = serving
        self._lock = asyncio.Lock()
        self._queue: deque[_Lease] = deque()
        self._active: _Lease | None = None
        self._lease_ids = itertools.count(1)
        self._previous_backend: str | None = None

    # -- Beobachtung ------------------------------------------------------

    @property
    def state(self) -> ArbiterState:
        return self._state

    def snapshot(self) -> dict:
        return {
            "state": self._state.value,
            "active": self._active.lease_id if self._active else None,
            "queue": [lease.lease_id for lease in self._queue],
        }

    # -- Request-HOLD ------------------------------------------------------

    async def wait_serving(self) -> None:
        """Haelt, bis der Arbiter wieder IDLE (serving) ist."""
        await self._serving.wait()

    # -- Lease-Verwaltung ---------------------------------------------------

    async def request_lease(
        self,
        *,
        vram_budget_mib=None,
        timeout_s: float | None = None,
        completion: str = "exit_code",
    ) -> LeaseInfo:
        lease = self._new_lease(timeout_s=timeout_s, completion=completion)
        return await self._submit(lease)

    def _new_lease(self, *, timeout_s, completion) -> _Lease:
        timeout = (
            timeout_s if timeout_s is not None
            else self._config.arbiter_lease_timeout_s
        )
        return _Lease(
            lease_id=f"lease-{next(self._lease_ids)}",
            timeout_s=timeout,
            deadline=None,
            completion=completion,
        )

    async def _submit(self, lease: _Lease) -> LeaseInfo:
        """Reiht einen Lease ein und vergibt ihn, sobald die GPU frei ist."""
        async with self._lock:
            if self._state is ArbiterState.IDLE and self._active is None:
                self._active = lease
                self._set_state(ArbiterState.PAUSING)
            else:
                position = (
                    (1 if self._active is not None else 0) + len(self._queue)
                )
                self._queue.append(lease)
                return LeaseInfo(lease.lease_id, "queued", position)
        await self._drain_and_stop()
        self._mark_granted(lease)
        return LeaseInfo(lease.lease_id, "granted", 0)

    def _mark_granted(self, lease: _Lease) -> None:
        """Vermerkt den Lease als aktiv: Deadline ab jetzt, Waiter wecken."""
        lease.deadline = (
            self._clock() + lease.timeout_s if lease.timeout_s else None
        )
        lease.granted.set()

    async def release(self, lease_id: str) -> None:
        await self._finish(lease_id)

    async def complete(self, lease_id: str, *, exit_code=None) -> None:
        await self._finish(lease_id)

    async def run_job(
        self,
        command: list[str],
        *,
        cwd=None,
        env=None,
        vram_budget_mib=None,
        timeout_s=None,
    ) -> JobResult:
        """Lease holen, Job ausfuehren, Lease freigeben — ein Agent-Aufruf."""
        lease = self._new_lease(timeout_s=timeout_s, completion="exit_code")
        info = await self._submit(lease)
        if info.state == "queued":
            await lease.granted.wait()
        proc = await self._subprocess_factory(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        exit_code = proc.returncode
        await self.complete(lease.lease_id, exit_code=exit_code)
        return JobResult(
            lease_id=lease.lease_id,
            exit_code=exit_code,
            stdout=stdout.decode() if isinstance(stdout, bytes) else stdout,
            stderr=stderr.decode() if isinstance(stderr, bytes) else stderr,
        )

    async def watchdog_once(self) -> None:
        """Erzwingt Completion eines Leases, dessen Deadline abgelaufen ist."""
        active = self._active
        if (
            self._state is ArbiterState.PAUSED
            and active is not None
            and active.deadline is not None
            and self._clock() > active.deadline
        ):
            logger.warning(
                "GPU-Arbiter: Lease %s hat die Deadline ueberschritten — "
                "erzwinge Completion.",
                active.lease_id,
            )
            await self.complete(active.lease_id)

    async def run_watchdog_loop(self) -> None:
        """Periodischer Watchdog (Hintergrund-Task)."""
        while True:
            await self._sleeper(self._config.arbiter_watchdog_interval_s)
            await self.watchdog_once()

    # -- intern ------------------------------------------------------------

    async def _drain_and_stop(self) -> None:
        previous = self._server_control.snapshot().get("backend")
        self._previous_backend = previous
        await self._wait_drained()
        if previous:
            await self._server_control.stop_and_wait(previous)
        self._set_state(ArbiterState.PAUSED)

    async def _wait_drained(self) -> None:
        deadline = self._clock() + self._config.arbiter_drain_timeout_s
        warned = False
        while self._inflight() > 0:
            if not warned and self._clock() >= deadline:
                logger.warning(
                    "GPU-Arbiter: Drain wartet laenger als %.0fs auf "
                    "laufende Generierungen (kein Force-Abort).",
                    self._config.arbiter_drain_timeout_s,
                )
                warned = True
            await self._sleeper(_DRAIN_POLL_S)

    async def _finish(self, lease_id: str) -> None:
        async with self._lock:
            if self._active is not None and self._active.lease_id == lease_id:
                if self._queue:
                    self._active = self._queue.popleft()
                    self._mark_granted(self._active)
                    return  # Ketten-Lease: bleibt PAUSED, kein Reload
                self._active = None
                self._set_state(ArbiterState.RESUMING)
            else:
                for i, lease in enumerate(self._queue):
                    if lease.lease_id == lease_id:
                        del self._queue[i]
                        return
                return  # unbekannte lease_id: idempotent ignorieren
        await self._resume()

    async def _resume(self) -> None:
        try:
            if self._previous_backend:
                await self._server_control.start_and_wait(
                    self._previous_backend)
        except Exception:
            logger.exception("GPU-Arbiter: Resume fehlgeschlagen")
            self._set_state(ArbiterState.ERROR)
            return
        self._set_state(ArbiterState.IDLE)
        await self._grant_next_queued()

    async def _grant_next_queued(self) -> None:
        """Vergibt einen waehrend des Resumes eingereihten Lease."""
        while True:
            async with self._lock:
                if (self._queue and self._active is None
                        and self._state is ArbiterState.IDLE):
                    lease = self._queue.popleft()
                    self._active = lease
                    self._set_state(ArbiterState.PAUSING)
                else:
                    return
            await self._drain_and_stop()
            self._mark_granted(lease)

    def _set_state(self, state: ArbiterState) -> None:
        self._state = state
        if state is ArbiterState.IDLE:
            self._serving.set()
        else:
            self._serving.clear()
