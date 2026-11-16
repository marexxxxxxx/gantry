"""Qwen3-TTS-Inferenz auf der GPU, lazy geladen."""

import gc
import threading

from .config import QwenTTSSettings
from .errors import QwenTTSError


def _standard_loader(settings: QwenTTSSettings):
    import torch
    from qwen_tts import Qwen3TTSModel

    return Qwen3TTSModel.from_pretrained(
        str(settings.model_dir),
        device_map=settings.device,
        dtype=getattr(torch, settings.dtype),
    )


def _ist_oom(exc: BaseException) -> bool:
    return "out of memory" in str(exc).lower()


class QwenTTSEngine:
    """Laedt das lokale CustomVoice-Checkpoint und liefert 16-bit-PCM."""

    def __init__(self, settings: QwenTTSSettings, *, model_loader=None) -> None:
        self._settings = settings
        self._loader = model_loader or _standard_loader
        self._lock = threading.Lock()
        self._model = None

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def _laden(self):
        if self._model is not None:
            return self._model
        try:
            self._model = self._loader(self._settings)
        except FileNotFoundError as exc:
            raise QwenTTSError(
                503,
                "model_unavailable",
                f"Qwen3-TTS-Modell fehlt unter {self._settings.model_dir}: "
                f"{exc}. Zuerst runtime/qwen-tts/download-models.sh "
                "ausfuehren.",
            ) from exc
        except Exception as exc:
            if _ist_oom(exc):
                raise QwenTTSError(
                    503,
                    "vram_exhausted",
                    "Nicht genug VRAM fuer Qwen3-TTS. "
                    "Zuerst /v1/unload ausfuehren.",
                ) from exc
            raise
        return self._model

    def load(self) -> None:
        with self._lock:
            self._laden()

    def unload(self) -> None:
        with self._lock:
            self._model = None
        gc.collect()
        try:
            import torch
        except ImportError:
            return
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def synthesize(
        self,
        text: str,
        *,
        speaker: str,
        language: str,
        instruct: str | None = None,
    ) -> tuple[bytes, int]:
        import numpy as np
        import torch

        with self._lock:
            modell = self._laden()
            kwargs = {
                "text": text,
                "language": language,
                "speaker": speaker,
            }
            if instruct:
                kwargs["instruct"] = instruct
            try:
                with torch.inference_mode():
                    wavs, sr = modell.generate_custom_voice(**kwargs)
            except Exception as exc:
                if _ist_oom(exc):
                    raise QwenTTSError(
                        503,
                        "vram_exhausted",
                        "Nicht genug VRAM fuer Qwen3-TTS. "
                        "Zuerst /v1/unload ausfuehren.",
                    ) from exc
                raise

        daten = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
        pcm = (np.clip(daten, -1.0, 1.0) * 32767.0).astype(np.int16)
        return pcm.tobytes(), int(sr)
