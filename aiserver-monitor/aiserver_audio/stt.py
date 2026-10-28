"""faster-whisper auf der CPU, mit kleinem Modell-Cache."""

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .catalog import WhisperInfo, whisper_dir
from .config import AudioSettings


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcription:
    text: str
    language: str
    duration: float
    segments: tuple[Segment, ...]


class SttEngine(Protocol):
    def preload(self, info: WhisperInfo) -> None: ...

    def transcribe(self, path: Path, *, info: WhisperInfo,
                   language: str | None,
                   prompt: str | None,
                   temperature: float) -> Transcription: ...


def normalize_language(language: str | None) -> str | None:
    """Macht aus ``auto``/leer ``None`` und normalisiert Grossschreibung."""
    code = (language or "").strip().casefold()
    return None if code in ("", "auto") else code


class FasterWhisperEngine:
    """Ein Modell im RAM; das Lock schuetzt Cache und laufende Transkription."""

    def __init__(self, settings: AudioSettings) -> None:
        self._settings = settings
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.RLock()

    def _laden(self, info: WhisperInfo) -> Any:
        if info.id in self._cache:
            self._cache.move_to_end(info.id)
            return self._cache[info.id]
        import faster_whisper

        modell = faster_whisper.WhisperModel(
            str(whisper_dir(self._settings.models_dir, info)),
            device="cpu",
            compute_type="int8",
            cpu_threads=self._settings.threads,
            num_workers=1,
        )
        self._cache[info.id] = modell
        while len(self._cache) > self._settings.model_cache:
            self._cache.popitem(last=False)
        return modell

    def preload(self, info: WhisperInfo) -> None:
        with self._lock:
            self._laden(info)

    def transcribe(self, path: Path, *, info: WhisperInfo,
                   language: str | None,
                   prompt: str | None,
                   temperature: float) -> Transcription:
        with self._lock:
            modell = self._laden(info)
            roh_segmente, details = modell.transcribe(
                str(path),
                language=normalize_language(language),
                task="transcribe",
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=prompt,
                temperature=temperature,
            )
            segmente = tuple(
                Segment(float(seg.start), float(seg.end), seg.text.strip())
                for seg in roh_segmente
            )
        text = " ".join(seg.text for seg in segmente if seg.text)
        return Transcription(text=text,
                             language=str(details.language),
                             duration=float(details.duration),
                             segments=segmente)
