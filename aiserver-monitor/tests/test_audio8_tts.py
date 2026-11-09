"""Tests fuer die Audio8-Worker-Einstellungen, Engine und den Fehlertyp."""

import tempfile
import unittest
from pathlib import Path

from aiserver_audio8_tts.config import Audio8Settings, load_settings
from aiserver_audio8_tts.engine import Audio8Engine
from aiserver_audio8_tts.errors import Audio8TTSError


class TestAudio8Settings(unittest.TestCase):
    def test_defaults(self):
        s = load_settings({})
        self.assertEqual(s.host, "127.0.0.1")
        self.assertEqual(s.port, 30006)
        self.assertEqual(s.model_dir, Path("/mnt/models/audio/audio8"))
        self.assertEqual(
            s.reference_audio,
            Path("/mnt/models/audio/audio8/reference/de_male.wav"))
        self.assertEqual(s.reference_text,
                         Path("/mnt/models/audio/audio8/reference/de_male.txt"))
        self.assertEqual(s.device, "cuda:0")
        self.assertEqual(s.dtype, "bfloat16")
        self.assertEqual(s.default_language, "German")
        self.assertEqual(s.max_new_tokens, 1024)
        self.assertEqual(s.retry_max_new_tokens, 2000)
        self.assertEqual(s.temperature, 0.8)
        self.assertEqual(s.top_p, 0.95)
        self.assertEqual(s.top_k, 50)
        self.assertEqual(s.seed, 42)

    def test_env_overrides(self):
        s = load_settings({
            "AUDIO8_PORT": "39006",
            "AUDIO8_MODEL_DIR": "/tmp/audio8",
            "AUDIO8_REFERENCE_AUDIO": "/tmp/ref.wav",
            "AUDIO8_DEVICE": "cpu",
        })
        self.assertEqual((s.port, str(s.model_dir), str(s.reference_audio),
                          s.device), (39006, "/tmp/audio8", "/tmp/ref.wav",
                                      "cpu"))

    def test_ungueltige_werte(self):
        for env in (
            {"AUDIO8_PORT": "0"},
            {"AUDIO8_RETRY_MAX_NEW_TOKENS": "100"},
            {"AUDIO8_TOP_P": "1.5"},
            {"AUDIO8_TEMPERATURE": "0"},
        ):
            with self.subTest(env=env):
                with self.assertRaises(ValueError):
                    load_settings(env)


class TestAudio8TTSError(unittest.TestCase):
    def test_haelt_status_code_code_und_message(self):
        exc = Audio8TTSError(503, "vram_exhausted", "kein VRAM")
        self.assertEqual((exc.status_code, exc.code, exc.message),
                         (503, "vram_exhausted", "kein VRAM"))


class FakeCodecModel:
    def __init__(self):
        self.codec_loaded = 0

    def load_codec(self):
        self.codec_loaded += 1


def _referenz(tmp: Path):
    audio = tmp / "de_male.wav"
    text = tmp / "de_male.txt"
    audio.write_bytes(b"RIFF")
    text.write_text("Guten Tag.", encoding="utf-8")
    return audio, text


class TestAudio8EngineLaden(unittest.TestCase):
    def _engine(self, tmp, *, model_loader=None, audio_probe=None):
        audio, text = _referenz(tmp)
        settings = Audio8Settings(
            model_dir=tmp, reference_audio=audio, reference_text=text,
            device="cpu")
        return Audio8Engine(
            settings,
            processor_loader=lambda _s: object(),
            model_loader=model_loader or (lambda _s: FakeCodecModel()),
            audio_probe=audio_probe or (lambda _p: None))

    def test_load_laedt_prozessor_modell_und_codec(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FakeCodecModel()
            engine = self._engine(Path(tmp), model_loader=lambda _s: model)
            self.assertFalse(engine.model_loaded)
            engine.load()
            self.assertTrue(engine.model_loaded)
            self.assertEqual(model.codec_loaded, 1)

    def test_fehlende_referenz_ist_503_reference_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio, text = _referenz(Path(tmp))
            text.unlink()
            settings = Audio8Settings(
                model_dir=Path(tmp), reference_audio=audio,
                reference_text=text, device="cpu")
            engine = Audio8Engine(
                settings, processor_loader=lambda _s: object(),
                model_loader=lambda _s: FakeCodecModel(),
                audio_probe=lambda _p: None)
            with self.assertRaises(Audio8TTSError) as ctx:
                engine.load()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.code, "reference_missing")

    def test_unlesbare_referenz_ist_503_reference_invalid(self):
        def probe(_pfad):
            raise OSError("kein wav")

        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp), audio_probe=probe)
            with self.assertRaises(Audio8TTSError) as ctx:
                engine.load()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.code, "reference_invalid")

    def test_oom_beim_laden_ist_503(self):
        def loader(_settings):
            raise RuntimeError("CUDA out of memory")

        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp), model_loader=loader)
            with self.assertRaises(Audio8TTSError) as ctx:
                engine.load()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.code, "vram_exhausted")

    def test_fehlendes_modell_ist_503_mit_download_hinweis(self):
        def loader(_settings):
            raise FileNotFoundError("config.json")

        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp), model_loader=loader)
            with self.assertRaises(Audio8TTSError) as ctx:
                engine.load()
        self.assertEqual(ctx.exception.code, "model_unavailable")
        self.assertIn("runtime/audio8/download-models.sh",
                      ctx.exception.message)
