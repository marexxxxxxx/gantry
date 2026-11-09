"""Tests fuer die Audio8-Worker-Einstellungen und den Fehlertyp."""

import unittest
from pathlib import Path

from aiserver_audio8_tts.config import Audio8Settings, load_settings
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
