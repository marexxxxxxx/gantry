import tempfile
import unittest
from pathlib import Path

from aiserver_audio.catalog import (
    PIPER_VOICES,
    WHISPER_MODELS,
    models_payload,
    piper_files,
    resolve_voice,
    resolve_whisper,
    voice_installed,
    whisper_dir,
    whisper_installed,
)
from aiserver_audio.errors import AudioError, audio_error_body


class TestKatalog(unittest.TestCase):
    def test_whisper_aliase(self):
        for name in (None, "", "whisper-small", "small", "whisper-1", "SMALL"):
            with self.subTest(name=name):
                self.assertEqual(resolve_whisper(name).id, "whisper-small")
        self.assertEqual(resolve_whisper("medium").id, "whisper-medium")
        self.assertEqual(resolve_whisper("turbo").id,
                         "whisper-large-v3-turbo")
        self.assertEqual(resolve_whisper("large-v3-turbo").id,
                         "whisper-large-v3-turbo")

    def test_unbekanntes_whisper_modell(self):
        with self.assertRaises(AudioError) as ctx:
            resolve_whisper("whisper-99")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.code, "model_not_found")

    def test_piper_aliase(self):
        for name in (None, "", "thorsten", "de_DE-thorsten-medium"):
            with self.subTest(name=name):
                self.assertEqual(resolve_voice(name).id,
                                 "de_DE-thorsten-medium")
        self.assertEqual(resolve_voice("kerstin").id, "de_DE-kerstin-low")
        self.assertEqual(resolve_voice("EVA").id, "de_DE-eva_k-x_low")

    def test_unbekannte_stimme(self):
        with self.assertRaises(AudioError) as ctx:
            resolve_voice("martin")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.code, "model_not_found")

    def test_whisper_installation(self):
        info = resolve_whisper("small")
        with tempfile.TemporaryDirectory() as tmp:
            basis = Path(tmp)
            self.assertFalse(whisper_installed(basis, info))
            ziel = whisper_dir(basis, info)
            ziel.mkdir(parents=True)
            for name in info.required_files:
                (ziel / name).write_bytes(b"x")
            self.assertTrue(whisper_installed(basis, info))

    def test_piper_installation(self):
        info = resolve_voice("thorsten")
        with tempfile.TemporaryDirectory() as tmp:
            basis = Path(tmp)
            self.assertFalse(voice_installed(basis, info))
            onnx, cfg = piper_files(basis, info)
            onnx.parent.mkdir(parents=True)
            onnx.write_bytes(b"x")
            self.assertFalse(voice_installed(basis, info))
            cfg.write_bytes(b"{}")
            self.assertTrue(voice_installed(basis, info))

    def test_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = models_payload(Path(tmp))
        self.assertEqual(payload["whisper"]["default"], "whisper-small")
        self.assertEqual(len(payload["whisper"]["models"]), 3)
        self.assertEqual(
            [m["id"] for m in payload["whisper"]["models"]],
            ["whisper-small", "whisper-medium",
             "whisper-large-v3-turbo"])
        self.assertTrue(all(not m["installed"]
                            for m in payload["whisper"]["models"]))
        self.assertEqual(payload["whisper"]["models"][0]["size_mb"], 484)
        self.assertEqual(payload["tts"]["default_model"], "piper")
        self.assertEqual(payload["tts"]["default_voice"],
                         "de_DE-thorsten-medium")
        self.assertEqual(
            [v["id"] for v in payload["tts"]["voices"]],
            ["de_DE-thorsten-medium", "de_DE-kerstin-low",
             "de_DE-eva_k-x_low"])
        self.assertEqual(payload["tts"]["voices"][2]["size_mb"], 21)

    def test_katalog_groessen(self):
        self.assertEqual([m.size_mb for m in WHISPER_MODELS],
                         [484, 1528, 1618])
        self.assertEqual([v.size_mb for v in PIPER_VOICES], [63, 63, 21])
        self.assertEqual([v.sample_rate for v in PIPER_VOICES],
                         [22050, 22050, 16000])


class TestFehlerformat(unittest.TestCase):
    def test_invalid_request(self):
        self.assertEqual(
            audio_error_body("kaputt", "invalid_request"),
            {"error": {"message": "kaputt",
                       "type": "invalid_request_error",
                       "code": "invalid_request"}})

    def test_server_error(self):
        self.assertEqual(
            audio_error_body("weg", "upstream_unavailable")["error"]["type"],
            "server_error")

    def test_audio_error_traegt_attribute(self):
        err = AudioError(503, "model_not_installed", "fehlt")
        self.assertEqual(err.status_code, 503)
        self.assertEqual(err.code, "model_not_installed")
        self.assertEqual(err.message, "fehlt")
        self.assertIn("fehlt", str(err))
