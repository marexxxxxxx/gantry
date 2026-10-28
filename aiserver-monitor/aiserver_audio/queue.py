"""Begrenzte Warteschlange fuer STT und TTS."""

import asyncio


class QueueFull(Exception):
    def __init__(self, retry_after: int = 5) -> None:
        super().__init__("Warteschlange voll")
        self.retry_after = retry_after


class Admission:
    """Semaphore plus Zaehler der Wartenden (429 statt unbegrenzter Stau)."""

    def __init__(self, parallel: int, max_queue: int,
                 retry_after: int = 5) -> None:
        if parallel <= 0:
            raise ValueError("parallel muss positiv sein")
        if max_queue <= 0:
            raise ValueError("max_queue muss positiv sein")
        self._sem = asyncio.Semaphore(parallel)
        self._max_queue = max_queue
        self.retry_after = retry_after
        self._waiting = 0

    @property
    def waiting(self) -> int:
        return self._waiting

    async def acquire(self) -> None:
        if self._waiting >= self._max_queue:
            raise QueueFull(self.retry_after)
        self._waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self._waiting -= 1

    def release(self) -> None:
        self._sem.release()
