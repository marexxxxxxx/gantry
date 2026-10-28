"""Piper-TTS auf der CPU: Stimmen-Pool und Satz-Splitting."""

import json
import re
import threading
from dataclasses import dataclass
from typing import Iterator, Protocol

from .catalog import PiperInfo, piper_files
from .config import AudioSettings

#: Satzgrenzen fuer die Streaming-Synthese. Ein Satz ohne Satzzeichen
#: bleibt ein Satz und wird dadurch nicht kuenstlich zerteilt.
_SATZGRENZE = re.compile(r"(?<=[.!?])\s+")

#: Zwei parallele Synthesen pro Stimme; jede bekommt eine eigene
#: ONNX-Session mit genau zwei Intra-Threads.
_POOLGROESSE = 2
_INTRA_THREADS = 2
_INTER_THREADS = 1


def split_sentences(text: str) -> list[str]:
    """Zerlegt Text an Satzzeichen; der Inhalt bleibt unveraendert."""
    return [teil for teil in _SATZGRENZE.split(text.strip()) if teil]


@dataclass(frozen=True)
class AudioChunk:
    """Ein Stueck PCM (16 Bit, mono, little endian) einer Synthese."""

    pcm: bytes
    sample_rate: int


class TtsEngine(Protocol):
    def synthesize(self, text: str, *, info: PiperInfo,
                   speed: float) -> Iterator[AudioChunk]:
        ...


class PiperEngine:
    """Laedt Piper-Stimmen lazy und verleiht sie aus einem kleinen Pool.

    Der Pool pro Stimme hat zwei Instanzen, damit zwei Saetze parallel
    synthetisiert werden koennen, ohne sich eine ONNX-Session zu teilen.
    Ein Lock schuetzt nur das Befuellen und die Ausleihe, nicht die
    eigentliche Synthese.
    """

    def __init__(self, settings: AudioSettings):
        self._settings = settings
        self._bedingung = threading.Condition()
        self._frei: dict[str, list] = {}

    def _pool_bauen(self, info: PiperInfo) -> list:
        import onnxruntime as ort
        from piper.config import PiperConfig
        from piper.voice import PiperVoice

        onnx, config_pfad = piper_files(self._settings.models_dir, info)
        konfiguration = PiperConfig.from_dict(
            json.loads(config_pfad.read_text(encoding="utf-8")))
        pool = []
        for _ in range(_POOLGROESSE):
            optionen = ort.SessionOptions()
            optionen.intra_op_num_threads = _INTRA_THREADS
            optionen.inter_op_num_threads = _INTER_THREADS
            session = ort.InferenceSession(
                str(onnx), sess_options=optionen,
                providers=["CPUExecutionProvider"])
            pool.append(PiperVoice(session, konfiguration))
        return pool

    def _leihen(self, info: PiperInfo) -> list:
        with self._bedingung:
            pool = self._frei.get(info.id)
            if pool is None:
                pool = self._pool_bauen(info)
                self._frei[info.id] = pool
            while not pool:
                self._bedingung.wait()
            return pool.pop(), pool

    def _zurueckgeben(self, info: PiperInfo, voice, pool: list) -> None:
        with self._bedingung:
            pool.append(voice)
            self._bedingung.notify()

    def synthesize(self, text: str, *, info: PiperInfo,
                   speed: float) -> Iterator[AudioChunk]:
        from piper.voice import SynthesisConfig

        if speed <= 0:
            raise ValueError("speed muss positiv sein")
        voice, pool = self._leihen(info)
        try:
            for chunk in voice.synthesize(
                    text, SynthesisConfig(length_scale=1.0 / speed)):
                yield AudioChunk(pcm=chunk.audio_int16_bytes,
                                 sample_rate=chunk.sample_rate)
        finally:
            self._zurueckgeben(info, voice, pool)
