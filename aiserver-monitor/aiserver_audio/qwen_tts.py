"""HTTP-Client fuer den Qwen3-TTS-GPU-Worker."""

from dataclasses import dataclass

import httpx

from .config import AudioSettings
from .errors import AudioError
from .tts import AudioChunk


@dataclass(frozen=True)
class QwenVoice:
    id: str
    voice: str
    language: str
    instruct: str | None = None


class QwenTTSClient:
    """Fragt den separaten Qwen3-TTS-Prozess und liefert PCM-Chunks."""

    def __init__(self, settings: AudioSettings, *,
                 client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client()

    def synthesize(self, text: str, *, info: QwenVoice, speed: float):
        url = self._settings.qwen_tts_base.rstrip("/") + "/synthesize"
        daten = {
            "text": text,
            "voice": info.voice,
            "language": info.language,
            "speed": speed,
        }
        if info.instruct:
            daten["instruct"] = info.instruct
        try:
            antwort = self._client.post(
                url,
                json=daten,
                timeout=self._settings.qwen_tts_timeout_s,
            )
        except httpx.TimeoutException:
            raise AudioError(
                504, "timeout",
                "Qwen3-TTS hat das Zeitlimit ueberschritten") from None
        except httpx.RequestError:
            raise AudioError(
                502, "upstream_unavailable",
                "Qwen3-TTS-Worker ist nicht erreichbar") from None

        if antwort.status_code >= 400:
            raise AudioError(
                antwort.status_code, "upstream_error",
                f"Qwen3-TTS-Fehler: {antwort.text[:200]}")
        rate = int(antwort.headers.get("x-sample-rate", "24000"))
        yield AudioChunk(antwort.content, rate)
