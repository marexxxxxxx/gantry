import tempfile
import unittest
from pathlib import Path

from aiserver_audio.config import AudioSettings, load_settings


class TestAudioSettings(unittest.TestCase):
    def test_defaults(self):
        s = load_settings(env={})
        self.assertEqual(s.host, "127.0.0.1")
        self.assertEqual(s.port, 30003)
        self.assertEqual(s.models_dir, Path("/mnt/models/audio"))
        self.assertEqual(s.threads, 8)
        self.assertEqual(s.preload, "whisper-small")
        self.assertEqual(s.model_cache, 1)
        self.assertEqual(s.max_queue, 8)
        self.assertEqual(s.stt_timeout_s, 1800)
        self.assertEqual(s.tts_timeout_s, 120)
        self.assertEqual(s.max_upload_mb, 100)

    def test_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(env={
                "AUDIO_HOST": "127.0.0.2",
                "AUDIO_PORT": "39003",
                "AUDIO_MODELS_DIR": tmp,
                "AUDIO_THREADS": "4",
                "AUDIO_PRELOAD": "whisper-medium",
                "AUDIO_MODEL_CACHE": "2",
                "AUDIO_MAX_QUEUE": "3",
                "AUDIO_STT_TIMEOUT": "60",
                "AUDIO_TTS_TIMEOUT": "30",
                "AUDIO_MAX_UPLOAD_MB": "10",
            })
        self.assertEqual(s.host, "127.0.0.2")
        self.assertEqual(s.port, 39003)
        self.assertEqual(s.models_dir, Path(tmp))
        self.assertEqual(s.threads, 4)
        self.assertEqual(s.preload, "whisper-medium")
        self.assertEqual(s.model_cache, 2)
        self.assertEqual(s.max_queue, 3)
        self.assertEqual(s.stt_timeout_s, 60)
        self.assertEqual(s.tts_timeout_s, 30)
        self.assertEqual(s.max_upload_mb, 10)

    def test_leere_werte_gelten_als_nicht_gesetzt(self):
        s = load_settings(env={"AUDIO_PRELOAD": "", "AUDIO_PORT": ""})
        self.assertEqual(s.preload, "")
        self.assertEqual(s.port, 30003)

    def test_ungueltige_werte_werden_abgelehnt(self):
        for env in (
            {"AUDIO_PORT": "0"},
            {"AUDIO_THREADS": "0"},
            {"AUDIO_MAX_QUEUE": "-1"},
            {"AUDIO_STT_TIMEOUT": "0"},
            {"AUDIO_MAX_UPLOAD_MB": "0"},
        ):
            with self.subTest(env=env):
                with self.assertRaises(ValueError):
                    load_settings(env=env)

    def test_settings_sind_unveraenderlich(self):
        s = AudioSettings()
        with self.assertRaises(Exception):
            s.port = 1  # type: ignore[misc]
