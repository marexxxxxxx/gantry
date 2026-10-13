"""Upstream-SSE-Parser fuer den aiserver-monitor.

``SSEByteParser`` arbeitet auf rohen Upstream-Bytes (auch mitten in
Mehrbyte-UTF-8-Zeichen) und liefert die JSON-Objekte kompletter
``data:``-Zeilen. ``summarize_chunk`` verdichtet einen solchen Chunk auf
die fuer Messung und Vorschau relevanten Felder. Kein I/O.
"""

import json
from dataclasses import dataclass

_DATA_PREFIX = b"data:"
_DONE = b"[DONE]"


class SSEByteParser:
    """Puffert Bytes und parst nur komplette Zeilen.

    Der Puffer sammelt solange, bis ein ``\n`` ankommt; erst dann wird die
    Zeile UTF-8-dekodiert. Damit bleiben Mehrbyte-Zeichen ueber Chunk-Grenzen
    intakt. Verworfen werden Kommentarzeilen (``:``), Leerzeilen, andere
    SSE-Felder (z. B. ``event:``) sowie ``[DONE]``; nicht dekodierbares
    UTF-8, kaputtes JSON und Nicht-Objekte werden still uebersprungen,
    damit die Messung den Passthrough nie stoert.
    """

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[dict]:
        """Nimmt Upstream-Bytes entgegen und gibt neue JSON-Objekte zurueck."""
        self._buffer += chunk
        zeilen = self._buffer.split(b"\n")
        self._buffer = zeilen.pop()             # Restzeile bleibt liegen
        objs: list[dict] = []
        for zeile in zeilen:
            obj = _parse_line(zeile)
            if obj is not None:
                objs.append(obj)
        return objs


def _parse_line(zeile: bytes) -> dict | None:
    """Eine komplette Zeile als JSON-Objekt oder None (verwerfen)."""
    if zeile.endswith(b"\r"):
        zeile = zeile[:-1]
    if not zeile or zeile.startswith(b":"):
        return None
    if not zeile.startswith(_DATA_PREFIX):
        return None
    payload = zeile[len(_DATA_PREFIX):].strip()
    if not payload or payload == _DONE:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


@dataclass
class ChunkSummary:
    """Fuer Messung/Vorschau relevante Felder eines Upstream-Chunks.

    Jedes Feld ist ``None``, wenn der Chunk es nicht enthaelt ("nicht
    gemessen").
    """

    content: str | None = None
    reasoning: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


def _delta_text(value: object) -> str | None:
    """Nur Strings sind Vorschautexte; alles andere gilt als nicht gemessen."""
    return value if isinstance(value, str) else None


def summarize_chunk(obj: dict) -> ChunkSummary:
    """Liest content/reasoning/finish_reason und usage aus einem Chunk.

    Erwartet das OpenAI-Delta-Format: ``choices[0].delta.content``,
    ``choices[0].delta.reasoning_content``, ``choices[0].finish_reason``
    sowie Top-Level ``usage.prompt_tokens``/``completion_tokens``. Fehlende
    oder unerwartet geformte Teile ergeben None statt Fehler (z. B. liefert
    SGLang usage im letzten Chunk mit leeren ``choices``).
    """
    content = None
    reasoning = None
    finish_reason = None
    prompt_tokens = None
    completion_tokens = None

    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            finish_reason = choice.get("finish_reason")
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = _delta_text(delta.get("content"))
                reasoning = _delta_text(delta.get("reasoning_content"))

    usage = obj.get("usage")
    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")

    return ChunkSummary(
        content=content,
        reasoning=reasoning,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
    )
