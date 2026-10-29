"""Chatterbox-V3-Inferenz auf der CPU, ohne Streaming."""

import threading

from .config import ChatterboxSettings


class ChatterboxEngine:
    """Laedt das lokale V3-Checkpoint und liefert 16-bit-PCM."""

    def __init__(self, settings: ChatterboxSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._model = None
        self._sr = 24000

    def _laden(self):
        if self._model is not None:
            return self._model
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        torch.set_num_threads(self._settings.threads)
        self._model = ChatterboxMultilingualTTS.from_local(
            str(self._settings.models_dir), device="cpu", t3_model="v3")
        self._sr = int(self._model.sr)
        return self._model

    def synthesize(self, text: str, speed: float = 1.0) -> tuple[bytes, int]:
        import numpy as np
        import torch

        with self._lock:
            modell = self._laden()
            with torch.inference_mode():
                wav = modell.generate(text, language_id="de")
            daten = np.asarray(wav.detach().cpu(), dtype=np.float32).reshape(-1)
            pcm = (np.clip(daten, -1.0, 1.0) * 32767.0).astype(np.int16)
            return pcm.tobytes(), self._sr
