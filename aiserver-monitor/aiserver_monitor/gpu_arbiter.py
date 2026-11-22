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
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable


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


@dataclass
class _Lease:
    """Interner Zustand eines Leases (mutable Deadline etc.)."""

    lease_id: str
    timeout_s: float | None
    deadline: float | None
    completion: str


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
        timeout = (
            timeout_s if timeout_s is not None
            else self._config.arbiter_lease_timeout_s
        )
        async with self._lock:
            lease = _Lease(
                lease_id=f"lease-{next(self._lease_ids)}",
                timeout_s=timeout,
                deadline=self._clock() + timeout if timeout else None,
                completion=completion,
            )
            if self._state is ArbiterState.IDLE:
                self._activate(lease)
                return LeaseInfo(lease.lease_id, "granted", 0)
            position = (1 if self._active is not None else 0) + len(self._queue)
            self._queue.append(lease)
            return LeaseInfo(lease.lease_id, "queued", position)

    async def release(self, lease_id: str) -> None:
        async with self._lock:
            self._finish(lease_id)

    async def complete(self, lease_id: str, *, exit_code=None) -> None:
        async with self._lock:
            self._finish(lease_id)

    # -- intern ------------------------------------------------------------

    def _activate(self, lease: _Lease) -> None:
        self._active = lease
        self._set_state(ArbiterState.PAUSED)

    def _finish(self, lease_id: str) -> None:
        if self._active is not None and self._active.lease_id == lease_id:
            self._active = None
            self._set_state(ArbiterState.IDLE)
            return
        for i, lease in enumerate(self._queue):
            if lease.lease_id == lease_id:
                del self._queue[i]
                return
        # Unbekannte lease_id: idempotent ignorieren.

    def _set_state(self, state: ArbiterState) -> None:
        self._state = state
        if state is ArbiterState.IDLE:
            self._serving.set()
        else:
            self._serving.clear()
