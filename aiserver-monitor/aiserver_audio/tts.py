"""Piper-TTS auf der CPU: Stimmen-Pool und Satz-Splitting."""

import asyncio
import json
import re
import struct
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


_CONTENT_TYPES = {
    "pcm": "audio/pcm",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
}

_BLOCKGROESSE = 64 * 1024
_ENCODER_TIMEOUT_S = 5.0


def content_type(fmt: str) -> str:
    """Content-Type der Ausgabe; unbekannte Formate werden abgelehnt."""
    try:
        return _CONTENT_TYPES[fmt]
    except KeyError:
        raise ValueError(f"unbekanntes Audioformat: {fmt}") from None


def wav_header(sample_rate: int) -> bytes:
    """Streaming-WAV-Kopf mit unbekannter Gesamtlaenge."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 0xFFFFFFFF, b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
        b"data", 0xFFFFFFFF)


async def start_encoder(fmt: str, sample_rate: int):
    """Startet ffmpeg als Roh-PCM-Encoder fuer MP3/Opus/Resampling."""
    basis = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
    ]
    if fmt == "pcm":
        ziel = ["-f", "s16le", "-ar", "24000", "-ac", "1", "pipe:1"]
    elif fmt == "mp3":
        ziel = ["-f", "mp3", "-b:a", "64k", "pipe:1"]
    elif fmt == "opus":
        ziel = ["-f", "opus", "-c:a", "libopus", "-b:a", "32k", "pipe:1"]
    else:
        raise ValueError(f"unbekanntes Audioformat: {fmt}")
    return await asyncio.create_subprocess_exec(
        *basis, *ziel,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)


async def _schreiben(prozess, chunks) -> None:
    try:
        async for chunk in chunks:
            prozess.stdin.write(chunk.pcm)
            await prozess.stdin.drain()
    finally:
        try:
            prozess.stdin.close()
        except Exception:
            pass


async def _aufraeumen(prozess) -> None:
    try:
        prozess.stdin.close()
    except Exception:
        pass
    if getattr(prozess, "returncode", None) is not None:
        return
    try:
        prozess.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(prozess.wait(), _ENCODER_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            prozess.kill()
        except ProcessLookupError:
            pass
        await prozess.wait()


async def stream_audio(chunks, fmt: str, *,
                       encoder_factory=start_encoder):
    """Liefert PCM/WAV direkt oder laesst ffmpeg einmal fuer alle Chunks laufen."""
    content_type(fmt)
    quelle = chunks.__aiter__()
    try:
        erster = await quelle.__anext__()
    except StopAsyncIteration:
        return

    if fmt == "wav":
        yield wav_header(erster.sample_rate)
        yield erster.pcm
        async for chunk in quelle:
            yield chunk.pcm
        return

    if fmt == "pcm" and erster.sample_rate == 24000:
        yield erster.pcm
        async for chunk in quelle:
            yield chunk.pcm
        return

    async def alle():
        yield erster
        async for chunk in quelle:
            yield chunk

    prozess = await encoder_factory(fmt, erster.sample_rate)
    schreiber = asyncio.create_task(_schreiben(prozess, alle()))
    try:
        while True:
            block = await prozess.stdout.read(_BLOCKGROESSE)
            if not block:
                break
            yield block
        await schreiber
    finally:
        if not schreiber.done():
            schreiber.cancel()
            try:
                await schreiber
            except (asyncio.CancelledError, Exception):
                pass
        await _aufraeumen(prozess)
