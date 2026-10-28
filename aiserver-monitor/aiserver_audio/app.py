"""FastAPI-App des CPU-Audio-Sidecars."""

import asyncio
import json
import logging
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .catalog import (models_payload, resolve_voice, resolve_whisper,
                      voice_installed, whisper_installed)
from .config import AudioSettings, load_settings
from .errors import AudioError, audio_error_body
from .ffmpeg import probe_duration
from .queue import Admission, QueueFull
from .stt import FasterWhisperEngine, Transcription
from .tts import (PiperEngine, content_type, split_sentences,
                  start_encoder, stream_audio)

logger = logging.getLogger(__name__)

_MEDIA_TYPES = {
    "json": "application/json",
    "text": "text/plain",
    "verbose_json": "application/json",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
}
_FORMATE = frozenset(_MEDIA_TYPES)
_MAX_AUDIO_S = 3600
_BLOCKGROESSE = 64 * 1024
_TTS_MODELS = frozenset({"piper", "tts-1", "tts-1-hd"})
_TTS_FORMATE = frozenset({"pcm", "wav", "mp3", "opus"})


class SpeechRequest(BaseModel):
    """OpenAI-kompatibler Text-fuer-Sprache-Request."""

    input: str = Field(..., max_length=5000)
    model: str = "piper"
    voice: str = "de_DE-thorsten-medium"
    response_format: str = "wav"
    speed: float = Field(1.0, ge=0.5, le=2.0)


def _zeitstempel(sekunden: float, trenner: str) -> str:
    ms = max(0, round(float(sekunden) * 1000))
    stunden, rest = divmod(ms, 3_600_000)
    minuten, rest = divmod(rest, 60_000)
    sek, ms = divmod(rest, 1000)
    return f"{stunden:02d}:{minuten:02d}:{sek:02d}{trenner}{ms:03d}"


def format_transcript(transcription: Transcription, fmt: str,
                      task: str = "transcribe") -> str:
    """Formatiert ein Ergebnis als JSON, Text, SRT oder VTT."""
    if fmt == "json":
        return json.dumps({"text": transcription.text}, ensure_ascii=False)
    if fmt == "text":
        return transcription.text
    if fmt == "verbose_json":
        return json.dumps({
            "task": task,
            "language": transcription.language,
            "duration": transcription.duration,
            "text": transcription.text,
            "segments": [
                {"id": i, "start": s.start, "end": s.end, "text": s.text}
                for i, s in enumerate(transcription.segments)
            ],
        }, ensure_ascii=False)
    if fmt == "srt":
        bloecke = []
        for i, s in enumerate(transcription.segments, 1):
            bloecke.append(
                f"{i}\n{_zeitstempel(s.start, ',')} --> "
                f"{_zeitstempel(s.end, ',')}\n{s.text}")
        return "\n\n".join(bloecke) + ("\n" if bloecke else "")
    if fmt == "vtt":
        bloecke = ["WEBVTT"]
        for s in transcription.segments:
            bloecke.append(
                f"{_zeitstempel(s.start, '.')} --> "
                f"{_zeitstempel(s.end, '.')}\n{s.text}")
        return "\n\n".join(bloecke) + "\n"
    raise ValueError(f"unbekanntes Format: {fmt}")


def _download_befehl(info) -> str:
    name = info.id.removeprefix("whisper-")
    return f"runtime/audio/download-models.sh --whisper {name}"


def _voice_download_befehl(info) -> str:
    name = info.aliases[0] if info.aliases else info.id
    return f"runtime/audio/download-models.sh --voices {name}"


async def _synthese(tts, satz, info, speed):
    return await asyncio.to_thread(
        lambda: list(tts.synthesize(satz, info=info, speed=speed)))


async def stream_speech(tts, text: str, *, info, fmt: str, speed: float,
                        timeout_s: float,
                        encoder_factory=start_encoder):
    """Synthetisiert Satz fuer Satz und liefert die Audiodaten als Stream.

    Der erste Satz wird vor dem ersten Byte vollstaendig synthetisiert; bei
    einem Timeout vor dem Antwortstart kann der Aufrufer noch 504 liefern.
    Nach dem Start beendet ein Timeout den Stream und wird nur geloggt.
    """
    saetze = split_sentences(text)
    if not saetze:
        raise AudioError(400, "invalid_request", "input darf nicht leer sein")
    loop = asyncio.get_running_loop()
    ende = loop.time() + timeout_s

    async def chunks():
        for satz in saetze:
            rest = ende - loop.time()
            if rest <= 0:
                raise AudioError(504, "timeout",
                                 "Sprachausgabe hat das Zeitlimit "
                                 "ueberschritten")
            try:
                teile = await asyncio.wait_for(
                    _synthese(tts, satz, info, speed), rest)
            except asyncio.TimeoutError:
                raise AudioError(504, "timeout",
                                 "Sprachausgabe hat das Zeitlimit "
                                 "ueberschritten") from None
            for teil in teile:
                yield teil

    audio = stream_audio(chunks(), fmt, encoder_factory=encoder_factory)
    try:
        async for block in audio:
            yield block
    finally:
        await audio.aclose()

def create_audio_app(settings: AudioSettings | None = None, *,
                     stt_engine=None, tts_engine=None,
                     duration_probe=probe_duration,
                     encoder_factory=start_encoder) -> FastAPI:
    """Baut die Audio-App; Engines und Zeitmessung sind austauschbar."""
    settings = settings or load_settings()
    stt = stt_engine if stt_engine is not None else FasterWhisperEngine(settings)
    tts = tts_engine if tts_engine is not None else PiperEngine(settings)
    admission = Admission(1, settings.max_queue)
    tts_admission = Admission(2, settings.max_queue)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.preload:
            try:
                info = resolve_whisper(settings.preload)
                await asyncio.to_thread(stt.preload, info)
            except Exception as exc:
                logger.warning("audio preload fehlgeschlagen: %s: %s",
                               type(exc).__name__, exc)
        yield

    app = FastAPI(title="aiserver-audio", lifespan=lifespan)
    app.state.settings = settings
    app.state.stt = stt
    app.state.tts = tts
    app.state.stt_admission = admission
    app.state.tts_admission = tts_admission

    @app.exception_handler(AudioError)
    async def _audio_fehler(request, exc: AudioError):
        return JSONResponse(
            audio_error_body(exc.message, exc.code),
            status_code=exc.status_code)

    @app.exception_handler(QueueFull)
    async def _queue_voll(request, exc: QueueFull):
        return JSONResponse(
            audio_error_body("Warteschlange voll", "queue_full"),
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)})

    @app.exception_handler(RequestValidationError)
    async def _ungueltig(request, exc: RequestValidationError):
        return JSONResponse(
            audio_error_body("Ungueltige Anfrage", "invalid_request"),
            status_code=400)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/v1/audio/models")
    async def modelle():
        return models_payload(settings.models_dir)

    @app.post("/v1/audio/transcriptions")
    async def transkription(
            file: UploadFile = File(...),
            model: str = Form("whisper-small"),
            language: str = Form("de"),
            response_format: str = Form("json"),
            prompt: str | None = Form(None),
            temperature: float = Form(0.0),
            task: str = Form("transcribe")):
        info = resolve_whisper(model)
        if task != "transcribe":
            raise AudioError(400, "invalid_request",
                             "task=translate wird nicht unterstuetzt")
        if response_format not in _FORMATE:
            raise AudioError(400, "invalid_request",
                             f"Unbekanntes Format: {response_format!r}")
        if not whisper_installed(settings.models_dir, info):
            raise AudioError(
                503, "model_not_installed",
                f"Modell {info.id} ist nicht installiert. Bitte "
                f"{_download_befehl(info)} ausfuehren.")

        await app.state.stt_admission.acquire()
        tmp_pfad: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix=Path(file.filename or "").suffix or ".audio") as tmp:
                tmp_pfad = Path(tmp.name)
                groesse = 0
                grenze = settings.max_upload_mb * 1024 * 1024
                while block := await file.read(_BLOCKGROESSE):
                    groesse += len(block)
                    if groesse > grenze:
                        raise AudioError(
                            413, "audio_too_large",
                            f"Datei groesser als {settings.max_upload_mb} MB")
                    tmp.write(block)

            dauer = await asyncio.to_thread(duration_probe, tmp_pfad)
            if dauer is None:
                raise AudioError(400, "invalid_audio",
                                 "Audiodatei konnte nicht gelesen werden")
            if dauer > _MAX_AUDIO_S:
                raise AudioError(413, "audio_too_long",
                                 "Audio laenger als 60 Minuten")

            status = "ok"
            start = time.perf_counter()
            try:
                ergebnis = await asyncio.wait_for(
                    asyncio.to_thread(
                        stt.transcribe, tmp_pfad, info=info,
                        language=language, prompt=prompt,
                        temperature=temperature),
                    timeout=settings.stt_timeout_s)
            except asyncio.TimeoutError:
                status = "timeout"
                raise AudioError(504, "timeout",
                                 "Transkription hat das Zeitlimit ueberschritten")
            except AudioError:
                status = "error"
                raise
            except Exception as exc:
                status = "error"
                logger.warning("audio stt fehlgeschlagen: %s: %s",
                               type(exc).__name__, exc)
                raise AudioError(500, "internal_error",
                                 "Transkription fehlgeschlagen")
            finally:
                verstrichen = time.perf_counter() - start
                rtf = verstrichen / dauer if dauer else 0.0
                logger.info(
                    "audio stt model=%s duration=%.1f rtf=%.2f status=%s",
                    info.id, dauer, rtf, status)

            return Response(
                format_transcript(ergebnis, response_format, task),
                media_type=_MEDIA_TYPES[response_format])
        finally:
            await file.close()
            if tmp_pfad is not None:
                tmp_pfad.unlink(missing_ok=True)
            app.state.stt_admission.release()

    @app.post("/v1/audio/speech")
    async def sprache(anfrage: SpeechRequest):
        info = resolve_voice(anfrage.voice)
        if anfrage.model.strip().casefold() not in _TTS_MODELS:
            raise AudioError(404, "model_not_found",
                             f"Unbekanntes TTS-Modell: {anfrage.model!r}")
        if not voice_installed(settings.models_dir, info):
            raise AudioError(
                503, "model_not_installed",
                f"Stimme {info.id} ist nicht installiert. Bitte "
                f"{_voice_download_befehl(info)} ausfuehren.")
        if anfrage.response_format not in _TTS_FORMATE:
            raise AudioError(400, "invalid_request",
                             f"Unbekanntes Format: "
                             f"{anfrage.response_format!r}")
        text = anfrage.input.strip()
        if not text:
            raise AudioError(400, "invalid_request",
                             "input darf nicht leer sein")

        await app.state.tts_admission.acquire()
        agen = stream_speech(
            app.state.tts, text, info=info,
            fmt=anfrage.response_format, speed=anfrage.speed,
            timeout_s=settings.tts_timeout_s,
            encoder_factory=encoder_factory)
        try:
            erster = await agen.__anext__()
        except BaseException:
            try:
                await agen.aclose()
            except Exception:
                pass
            app.state.tts_admission.release()
            raise

        freigegeben = False

        def freigeben():
            nonlocal freigegeben
            if not freigegeben:
                freigegeben = True
                app.state.tts_admission.release()

        start = time.perf_counter()

        async def koerper():
            status = "ok"
            try:
                yield erster
                async for block in agen:
                    yield block
            except AudioError as exc:
                status = "timeout" if exc.code == "timeout" else "error"
                logger.warning("audio tts abgebrochen: %s: %s",
                               exc.code, exc.message)
            except Exception as exc:
                status = "error"
                logger.warning("audio tts fehlgeschlagen: %s: %s",
                               type(exc).__name__, exc)
            finally:
                try:
                    await agen.aclose()
                except Exception:
                    pass
                freigeben()
                logger.info(
                    "audio tts voice=%s status=%s seconds=%.2f",
                    info.id, status, time.perf_counter() - start)

        return StreamingResponse(
            koerper(),
            media_type=content_type(anfrage.response_format),
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(freigeben))

    return app
