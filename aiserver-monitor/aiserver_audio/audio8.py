"""HTTP-Client fuer den Audio8-TTS-GPU-Worker."""

from dataclasses import dataclass
from typing import Iterator

import httpx

from .config import AudioSettings
from .errors import AudioError
from .tts import AudioChunk

_UPSTREAM_CODES = frozenset((
    "vram_exhausted", "reference_missing", "reference_invalid"))


@dataclass(frozen=True)
class Audio8Voice:
    id: str
    voice: str
    language: str = "German"


class Audio8Client:
    """Fragt den separaten Audio8-Prozess und liefert PCM-Chunks."""

    def __init__(self, settings: AudioSettings, *,
                 client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client()

    def synthesize(self, text: str, *, info: Audio8Voice,
                   speed: float) -> Iterator[AudioChunk]:
        url = self._settings.audio8_base.rstrip("/") + "/synthesize"
        daten = {
            "text": text,
            "voice": info.voice,
            "language": info.language,
        }
        try:
            antwort = self._client.post(
                url,
                json=daten,
                timeout=self._settings.audio8_timeout_s,
            )
        except httpx.TimeoutException:
            raise AudioError(
                504, "timeout",
                "Audio8-TTS hat das Zeitlimit ueberschritten") from None
        except httpx.RequestError:
            raise AudioError(
                502, "upstream_unavailable",
                "Audio8-TTS-Worker ist nicht erreichbar") from None

        if antwort.status_code >= 400:
            code, message = self._fehler(antwort)
            if code in _UPSTREAM_CODES:
                raise AudioError(antwort.status_code, code, message)
            raise AudioError(
                antwort.status_code, "upstream_error",
                f"Audio8-TTS-Fehler: {antwort.text[:200]}")
        rate = int(antwort.headers.get("x-sample-rate", "44100"))
        yield AudioChunk(antwort.content, rate)

    @staticmethod
    def _fehler(antwort: httpx.Response) -> tuple[str | None, str]:
        try:
            daten = antwort.json()
            fehler = daten["error"]
            code = fehler.get("code")
            message = fehler.get("message")
        except (ValueError, KeyError, TypeError, AttributeError):
            return None, antwort.text[:200]
        if not isinstance(code, str) or not isinstance(message, str):
            return None, antwort.text[:200]
        return code, message
