"""Tests fuer die Qwen3-TTS-Anbindung im Audio-Sidecar."""

import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from aiserver_audio.app import create_audio_app
from aiserver_audio.catalog import (models_payload, qwen_tts_installed,
                                    resolve_qwen_voice)
from aiserver_audio.config import AudioSettings
from aiserver_audio.errors import AudioError
from aiserver_audio.qwen_tts import QwenTTSClient, QwenVoice
from aiserver_audio.tts import AudioChunk


def _qwen_dateien(models_dir: Path) -> None:
    ziel = models_dir / "experiments" / "qwen"
    (ziel / "speech_tokenizer").mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "model.safetensors",
                 "generation_config.json",
                 "speech_tokenizer/config.json",
                 "speech_tokenizer/model.safetensors"):
        (ziel / name).write_bytes(b"x")


class TestQwenTTSClient(unittest.TestCase):
    def _client(self, handler):
        s = AudioSettings(models_dir=Path("/tmp"), preload="",
                          qwen_tts_base="http://127.0.0.1:39905")
        return QwenTTSClient(
            s, client=httpx.Client(transport=httpx.MockTransport(handler)))

    def test_sends_voice_language_and_returns_pcm(self):
        def handler(request):
            self.assertEqual(request.url.path, "/synthesize")
            daten = json.loads(request.content)
            self.assertEqual(daten["voice"], "Ryan")
            self.assertEqual(daten["language"], "German")
            return httpx.Response(
                200, content=b"\x01\x00" * 8,
                headers={"X-Sample-Rate": "24000"})

        info = QwenVoice("Qwen3-TTS", "Ryan", "German", None)
        teile = list(self._client(handler).synthesize(
            "Hallo", info=info, speed=1.0))
        self.assertEqual(teile, [AudioChunk(b"\x01\x00" * 8, 24000)])

    def test_worker_nicht_erreichbar_502(self):
        def handler(request):
            raise httpx.ConnectError("down", request=request)

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=QwenVoice("Qwen3-TTS", "Vivian", "German", None),
                speed=1.0))
        self.assertEqual(ctx.exception.status_code, 502)

    def test_worker_timeout_504(self):
        def handler(request):
            raise httpx.ReadTimeout("zu langsam", request=request)

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=QwenVoice("Qwen3-TTS", "Vivian", "German", None),
                speed=1.0))
        self.assertEqual(ctx.exception.status_code, 504)


class TestQwenTTSCatalog(unittest.TestCase):
    def test_installed_checks_all_required_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            self.assertFalse(qwen_tts_installed(models))
            _qwen_dateien(models)
            self.assertTrue(qwen_tts_installed(models))

    def test_models_payload_lists_qwen3_tts_and_voices(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = models_payload(Path(tmp))
        ids = {m["id"] for m in payload["tts"]["models"]}
        self.assertIn("Qwen3-TTS", ids)
        voices = {v["id"] for v in payload["tts"]["qwen3_tts"]["voices"]}
        self.assertIn("Vivian", voices)
        self.assertIn("Ryan", voices)

    def test_default_voice_and_piper_default_map_to_vivian(self):
        self.assertEqual(resolve_qwen_voice(None), "Vivian")
        self.assertEqual(resolve_qwen_voice("default"), "Vivian")
        self.assertEqual(resolve_qwen_voice("de_DE-thorsten-medium"), "Vivian")
        self.assertEqual(resolve_qwen_voice("ryan"), "Ryan")

    def test_unknown_voice_is_400(self):
        with self.assertRaises(AudioError) as ctx:
            resolve_qwen_voice("Unbekannt")
        self.assertEqual(ctx.exception.status_code, 400)


class FakeQwenTTS:
    def __init__(self):
        self.aufrufe = []

    def synthesize(self, text, *, info, speed):
        self.aufrufe.append((text, info.voice, info.language, info.instruct,
                             speed))
        yield AudioChunk(b"\x01\x00" * 32, 24000)


class TestQwenTTSApp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.models_dir = Path(self._tmp.name)
        self.settings = AudioSettings(models_dir=self.models_dir, preload="")
        self.qwen = FakeQwenTTS()

    def tearDown(self):
        self._tmp.cleanup()

    def _app(self):
        return create_audio_app(
            self.settings, stt_engine=object(), tts_engine=object(),
            qwen_tts_engine=self.qwen, chatterbox_engine=object(),
            duration_probe=lambda _: 1.0, encoder_factory=object())

    def test_speech_routes_to_qwen_worker(self):
        _qwen_dateien(self.models_dir)
        with TestClient(self._app()) as c:
            r = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "Qwen3-TTS",
                "voice": "Ryan", "language": "German",
                "instruct": "freundlich", "response_format": "wav"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content[:4], b"RIFF")
        self.assertEqual(self.qwen.aufrufe,
                         [("Guten Tag.", "Ryan", "German", "freundlich", 1.0)])

    def test_default_voice_is_vivian(self):
        _qwen_dateien(self.models_dir)
        with TestClient(self._app()) as c:
            r = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "qwen3-tts",
                "response_format": "wav"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.qwen.aufrufe[0][1], "Vivian")

    def test_not_installed_503_with_hint(self):
        with TestClient(self._app()) as c:
            r = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "Qwen3-TTS"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("runtime/qwen-tts/download-models.sh",
                      r.json()["error"]["message"])
