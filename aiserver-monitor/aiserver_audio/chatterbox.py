"""HTTP-Client fuer den Chatterbox-CPU-Worker."""

import httpx

from .config import AudioSettings
from .errors import AudioError
from .tts import AudioChunk


class ChatterboxClient:
    """Fragt den separaten Chatterbox-Prozess und liefert PCM-Chunks."""

    def __init__(self, settings: AudioSettings, *, client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client()

    def synthesize(self, text: str, *, info, speed: float):
        url = self._settings.chatterbox_base.rstrip("/") + "/synthesize"
        try:
            antwort = self._client.post(
                url,
                json={"text": text, "speed": speed},
                timeout=self._settings.chatterbox_timeout_s,
            )
        except httpx.TimeoutException:
            raise AudioError(
                504, "timeout",
                "Chatterbox hat das Zeitlimit ueberschritten") from None
        except httpx.RequestError:
            raise AudioError(
                502, "upstream_unavailable",
                "Chatterbox-Worker ist nicht erreichbar") from None

        if antwort.status_code >= 400:
            raise AudioError(
                antwort.status_code, "upstream_error",
                f"Chatterbox-Fehler: {antwort.text[:200]}")
        rate = int(antwort.headers.get("x-sample-rate", "24000"))
        yield AudioChunk(antwort.content, rate)
