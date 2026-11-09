"""Lade- und Lebenszykluslogik des Audio8-TTS-Workers."""

import gc
import logging
import threading
from pathlib import Path

from .config import Audio8Settings
from .errors import Audio8TTSError

logger = logging.getLogger(__name__)

_DOWNLOAD_HINWEIS = "runtime/audio8/download-models.sh"


def _ist_oom(exc: BaseException) -> bool:
    return "out of memory" in str(exc).lower()


def _datei_ok(pfad: Path) -> bool:
    try:
        return pfad.is_file() and pfad.stat().st_size > 0
    except OSError:
        return False


def _standard_processor_loader(settings: Audio8Settings):
    from transformers import AutoProcessor

    return AutoProcessor.from_pretrained(
        str(settings.model_dir), trust_remote_code=True)


def _standard_model_loader(settings: Audio8Settings):
    import torch
    from transformers import AutoModel

    modell = AutoModel.from_pretrained(
        str(settings.model_dir),
        trust_remote_code=True,
        dtype=getattr(torch, settings.dtype),
    )
    return modell.eval().to(settings.device)


def _standard_audio_probe(pfad: Path) -> None:
    import soundfile as sf

    sf.info(str(pfad))


class Audio8Engine:
    """Laedt Prozessor, Modell und Codec lazy und haelt die Referenzstimme."""

    def __init__(self, settings: Audio8Settings, *, processor_loader=None,
                 model_loader=None, audio_probe=None):
        self._settings = settings
        self._processor_loader = processor_loader or _standard_processor_loader
        self._model_loader = model_loader or _standard_model_loader
        self._audio_probe = audio_probe or _standard_audio_probe
        self._lock = threading.Lock()
        self._processor = None
        self._model = None
        self._reference_audio: Path | None = None
        self._reference_text: str | None = None

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def _referenz(self) -> tuple[Path, str]:
        audio = Path(self._settings.reference_audio)
        text = Path(self._settings.reference_text)
        if not _datei_ok(audio) or not _datei_ok(text):
            raise Audio8TTSError(
                503, "reference_missing",
                "Referenzaudio oder -text fehlt. Bitte "
                f"{_DOWNLOAD_HINWEIS} oder runtime/audio8/make-reference.sh "
                "ausfuehren.")
        try:
            transkript = text.read_text(encoding="utf-8").strip()
        except OSError:
            transkript = ""
        if not transkript:
            raise Audio8TTSError(
                503, "reference_missing",
                "Referenztext ist leer. Bitte runtime/audio8/make-reference.sh "
                "ausfuehren.")
        try:
            self._audio_probe(audio)
        except Exception as exc:
            raise Audio8TTSError(
                503, "reference_invalid",
                f"Referenzaudio ist nicht lesbar: {type(exc).__name__}") from None
        return audio, transkript

    def _laden(self) -> None:
        audio, text = self._referenz()
        if self._processor is None:
            self._processor = self._processor_loader(self._settings)
        if self._model is None:
            modell = self._model_loader(self._settings)
            lade_codec = getattr(modell, "load_codec", None)
            if not callable(lade_codec):
                raise Audio8TTSError(
                    503, "model_unavailable",
                    "Audio8-TTS: Modell bietet load_codec() nicht. Bitte "
                    "Modellstand pruefen und runtime/audio8/download-models.sh "
                    "erneut ausfuehren.")
            lade_codec()
            self._model = modell
            self._reference_audio = audio
            self._reference_text = text

    def load(self) -> None:
        with self._lock:
            try:
                self._laden()
            except Audio8TTSError:
                raise
            except FileNotFoundError:
                raise Audio8TTSError(
                    503, "model_unavailable",
                    "Audio8-Modell fehlt. Bitte "
                    f"{_DOWNLOAD_HINWEIS} ausfuehren.") from None
            except Exception as exc:
                if _ist_oom(exc):
                    raise Audio8TTSError(
                        503, "vram_exhausted",
                        "Audio8-TTS: nicht genug VRAM") from None
                raise

    def unload(self) -> None:
        with self._lock:
            self._processor = None
            self._model = None
            self._reference_audio = None
            self._reference_text = None
        gc.collect()
        try:
            import torch
        except ImportError:
            return
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _generieren(self, inputs: dict, max_new_tokens: int, generator):
        return self._model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=self._settings.temperature,
            top_p=self._settings.top_p,
            top_k=self._settings.top_k,
            do_sample=True,
            generator=generator,
            return_dict_in_generate=True,
        )

    def synthesize(self, text: str, *, language: str = "German"
                   ) -> tuple[bytes, int]:
        """Synthetisiert einen Satz mit der festen Referenzstimme."""
        import numpy as np
        import torch

        logger.info("audio8 synth language=%s chars=%d", language, len(text))
        with self._lock:
            try:
                self._laden()
                inputs = self._processor(
                    text=[text],
                    reference_audio=[self._reference_audio],
                    reference_text=[self._reference_text],
                    return_tensors="pt",
                )
                inputs = {
                    name: value.to(self._settings.device)
                    for name, value in inputs.items()
                }
                generator = torch.Generator(
                    device=self._settings.device).manual_seed(
                        self._settings.seed)
                output = self._generieren(
                    inputs, self._settings.max_new_tokens, generator)
                if (not bool(output.finished[0])
                        and self._settings.retry_max_new_tokens
                        > self._settings.max_new_tokens):
                    output = self._generieren(
                        inputs, self._settings.retry_max_new_tokens,
                        generator)
                if not bool(output.finished[0]):
                    logger.warning(
                        "audio8 synthese ohne EOS; liefere vorhandenes Audio")
                waveforms, lengths = self._model.decode_audio(output.codes)
                waveform = waveforms[0, :int(lengths[0])]
                pcm = (waveform.float().cpu().numpy().clip(-1.0, 1.0)
                       * 32767).astype(np.int16).tobytes()
                return pcm, int(self._model.config.codec_sample_rate)
            except Audio8TTSError:
                raise
            except Exception as exc:
                if _ist_oom(exc):
                    raise Audio8TTSError(
                        503, "vram_exhausted",
                        "Audio8-TTS: nicht genug VRAM") from None
                raise
