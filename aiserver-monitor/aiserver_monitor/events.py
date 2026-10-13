"""SSE-Bausteine fuer den aiserver-monitor.

Formatierung (``format_sse``/``format_comment``), ein Fan-out-Hub
(``EventHub``) fuer Live-Ereignisse an alle Dashboard-Abonnenten und
``sse_stream``: erst eine Liste vorformatierter Frames (z. B. Snapshot),
danach Live-Ereignisse; bei Leerlauf laeuft ein Keepalive-Kommentar.
Kein I/O, kein Netzwerk; Queues sind unbegrenzt (Abonnenten konsumieren
in der Regel sofort).
"""

import asyncio
import json
from collections.abc import AsyncIterator


def format_sse(event: str, data: dict) -> str:
    """Formatiert ein SSE-Ereignis als ``event:``-/``data:``-Frame.

    ``json.dumps`` mit ``ensure_ascii=False`` laesst Umlaute lesbar und
    escaped Zeilenumbrueche, sodass der Frame einzeilig bleibt; der Frame
    endet mit einer Leerzeile.
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def format_comment(text: str) -> str:
    """Formatiert einen SSE-Kommentar (Keepalive), z. B. ``: ping``."""
    return f": {text}\n\n"


class EventHub:
    """Verteilt SSE-Frames an alle Abonnenten (je eine asyncio.Queue).

    ``publish`` ist synchron und wirft nie: ohne Abonnenten passiert nichts.
    Abonnenten sind anonyme Queues; ``unsubscribe`` ist idempotent.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        """Meldet einen neuen Abonnenten an und gibt seine Queue zurueck."""
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Meldet einen Abonnenten ab; unbekannte Queues sind kein Fehler."""
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: dict) -> None:
        """Reiht einen formatierten Frame bei allen Abonnenten ein."""
        frame = format_sse(event, data)
        for queue in self._subscribers:
            queue.put_nowait(frame)


async def sse_stream(hub: EventHub, initial: list[str],
                     heartbeat_s: float) -> AsyncIterator[str]:
    """Liefert erst ``initial``, dann Live-Frames; Leerlauf -> ``: ping``.

    Die Anmeldung am Hub passiert beim ersten ``anext`` (async generator);
    beim Schliessen des Generators (Client weg) wird automatisch abgemeldet.
    """
    queue = hub.subscribe()
    try:
        for frame in initial:
            yield frame
        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
            except TimeoutError:
                yield format_comment("ping")
    finally:
        hub.unsubscribe(queue)
