"""HTTP-Client fuer ZONOS3-Mini auf der CPU."""

import io
import logging
import wave
from dataclasses import dataclass, field
from typing import Iterator, Mapping

import httpx

from .catalog import PiperInfo
from .config import AudioSettings
from .errors import AudioError
from .tts import AudioChunk, PiperEngine

logger = logging.getLogger(__name__)

#: Fest eingestellte Stimme. Die Werte stammen aus den Hoerproben
#: "Liesel 22 froehlich" (12_zonos_liesel_happy22_lang).
ZONOS_HAPPY22_CONTROLS: dict[str, float] = {
    "pitch.pitch": -0.8,
    "texture.soft": 2.0,
    "delivery.conversational": 1.5,
    "delivery.tempo": 1.05,
    "identity.gender": 1.2,
    "identity.age": -2.3,
    "emotions.happy": 0.8,
    "mood.valence": 0.8,
    "mood.arousal": 0.3,
}

ZONOS_SPEAKER_ID = "default_1e94715774dd144e"

_FALLBACK_CODES = frozenset((
    "timeout", "upstream_unavailable", "upstream_error"))


@dataclass(frozen=True)
class ZonosVoice:
    """Feste ZONOS-Stimme inklusive Steering-Controls."""

    id: str
    voice: str = "liesel"
    language: str = "de"
    speaker_embedding_id: str = ZONOS_SPEAKER_ID
    controls: Mapping[str, float] = field(
        default_factory=lambda: dict(ZONOS_HAPPY22_CONTROLS))
    seed: int = 43
    max_tokens: int = 8192


class ZonosClient:
    """Fragt den lokalen ZONOS3-Server und liefert PCM16-Chunks.

    Bei einem Ausfall des ZONOS-Servers wird optional eine Piper-Stimme
    als Fallback verwendet, damit Hermes nicht stumm bleibt.
    """

    def __init__(self, settings: AudioSettings, *,
                 client: httpx.Client | None = None,
                 fallback_engine: PiperEngine | None = None,
                 fallback_info: PiperInfo | None = None):
        self._settings = settings
        self._client = client or httpx.Client()
        self._fallback_engine = fallback_engine
        self._fallback_info = fallback_info

    def synthesize(self, text: str, *, info: ZonosVoice,
                   speed: float) -> Iterator[AudioChunk]:
        try:
            yield from self._synthesize_zonos(text, info=info)
        except AudioError as exc:
            if (self._fallback_engine is None or self._fallback_info is None
                    or exc.code not in _FALLBACK_CODES):
                raise
            logger.warning(
                "zonos nicht verfuegbar (%s), fallback auf %s",
                exc.code, self._fallback_info.id)
            yield from self._fallback_engine.synthesize(
                text, info=self._fallback_info, speed=speed)

    def _synthesize_zonos(self, text: str, *,
                          info: ZonosVoice) -> Iterator[AudioChunk]:
        url = self._settings.zonos_base.rstrip("/") + "/v2/generate"
        daten = {
            "text": text,
            "language": info.language,
            "speaker_embedding_id": info.speaker_embedding_id,
            "speaker_mode": "embedding",
            "controls": dict(info.controls),
            "seed": info.seed,
            "max_tokens": info.max_tokens,
            "stream": False,
            "format": "wav",
        }
        try:
            antwort = self._client.post(
                url,
                json=daten,
                timeout=self._settings.zonos_timeout_s,
            )
        except httpx.TimeoutException:
            raise AudioError(
                504, "timeout",
                "ZONOS3-TTS hat das Zeitlimit ueberschritten") from None
        except httpx.RequestError:
            raise AudioError(
                502, "upstream_unavailable",
                "ZONOS3-TTS-Server ist nicht erreichbar") from None

        if antwort.status_code >= 400:
            code, message = self._fehler(antwort)
            raise AudioError(
                antwort.status_code,
                code or "upstream_error",
                f"ZONOS3-TTS-Fehler: {message}")

        try:
            with wave.open(io.BytesIO(antwort.content), "rb") as datei:
                rate = datei.getframerate()
                kanaele = datei.getnchannels()
                breite = datei.getsampwidth()
                pcm = datei.readframes(datei.getnframes())
        except (wave.Error, EOFError, ValueError):
            raise AudioError(
                502, "upstream_error",
                "ZONOS3-TTS lieferte keine gueltige WAV-Antwort") from None

        if not pcm or breite != 2 or kanaele != 1:
            raise AudioError(
                502, "upstream_error",
                "ZONOS3-TTS lieferte kein PCM16-mono-Audio")
        yield AudioChunk(pcm=pcm, sample_rate=rate)

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
