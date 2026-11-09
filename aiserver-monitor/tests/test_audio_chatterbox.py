"""Tests fuer die optionale Chatterbox-Anbindung."""
import tempfile
import unittest
from pathlib import Path

import httpx

from aiserver_audio.app import create_audio_app
from aiserver_audio.catalog import chatterbox_installed, models_payload
from aiserver_audio.chatterbox import ChatterboxClient
from aiserver_audio.config import AudioSettings
from aiserver_audio.errors import AudioError
from aiserver_audio.tts import AudioChunk


def _chatterbox_dateien(models_dir: Path) -> None:
    ziel = models_dir / "chatterbox"
    ziel.mkdir(parents=True, exist_ok=True)
    for name in ("ve.pt", "s3gen.pt", "t3_mtl23ls_v3.safetensors",
                 "grapheme_mtl_merged_expanded_v1.json",
                 "Cangjie5_TC.json"):
        (ziel / name).write_bytes(b"x")


class FakeChatterbox:
    def __init__(self):
        self.aufrufe = []

    def synthesize(self, text, *, info, speed):
        self.aufrufe.append((text, info.id, speed))
        yield AudioChunk(b"\x01\x00" * 32, 24000)


class TestChatterboxClient(unittest.TestCase):
    def _client(self, handler, **kwargs):
        einstellungen = AudioSettings(
            models_dir=Path("/tmp"), preload="",
            chatterbox_base="http://127.0.0.1:39904", **kwargs)
        return ChatterboxClient(
            einstellungen,
            client=httpx.Client(transport=httpx.MockTransport(handler)))

    def test_liefert_pcm_und_sample_rate(self):
        def handler(request):
            self.assertEqual(request.url.path, "/synthesize")
            return httpx.Response(
                200, content=b"\x01\x00" * 8,
                headers={"X-Sample-Rate": "24000"})

        teile = list(self._client(handler).synthesize(
            "Hallo", info=object(), speed=1.0))
        self.assertEqual(teile, [AudioChunk(b"\x01\x00" * 8, 24000)])

    def test_worker_nicht_erreichbar_502(self):
        def handler(request):
            raise httpx.ConnectError("down", request=request)

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=object(), speed=1.0))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.code, "upstream_unavailable")

    def test_worker_timeout_504(self):
        def handler(request):
            raise httpx.ReadTimeout("zu langsam", request=request)

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=object(), speed=1.0))
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertEqual(ctx.exception.code, "timeout")


class TestChatterboxApp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.models_dir = Path(self._tmp.name)
        self.settings = AudioSettings(models_dir=self.models_dir, preload="")
        self.chatterbox = FakeChatterbox()

    def tearDown(self):
        self._tmp.cleanup()

    def _app(self):
        return create_audio_app(
            self.settings, stt_engine=object(), tts_engine=object(),
            chatterbox_engine=self.chatterbox,
            duration_probe=lambda _: 1.0,
            encoder_factory=object())

    def test_chatterbox_modell_und_default_stimme(self):
        _chatterbox_dateien(self.models_dir)
        app = self._app()
        with __import__("fastapi.testclient").testclient.TestClient(app) as c:
            antwort = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "chatterbox-v3",
                "voice": "default", "response_format": "wav"})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.content[:4], b"RIFF")
        self.assertEqual(self.chatterbox.aufrufe,
                         [("Guten Tag.", "chatterbox", 1.0)])

    def test_chatterbox_nicht_installiert_503(self):
        app = self._app()
        with __import__("fastapi.testclient").testclient.TestClient(app) as c:
            antwort = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "chatterbox",
                "voice": "default"})
        self.assertEqual(antwort.status_code, 503)
        fehler = antwort.json()["error"]
        self.assertEqual(fehler["code"], "model_not_installed")
        self.assertIn("runtime/chatterbox/download-models.sh",
                      fehler["message"])

    def test_modelle_enthalten_chatterbox(self):
        daten = models_payload(self.models_dir)
        ids = {m["id"]: m["installed"] for m in daten["tts"]["models"]}
        self.assertEqual(ids, {"piper": True, "chatterbox": False,
                               "Qwen3-TTS": False,
                               "Audio8-Realistic-Air": False})
        self.assertTrue(chatterbox_installed(self.models_dir) is False)
