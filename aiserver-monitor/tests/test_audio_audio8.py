"""Tests fuer Audio8-Settings, Fehlerklassifikation und den Sidecar-Client."""

import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from aiserver_audio.app import create_audio_app
from aiserver_audio.audio8 import Audio8Client, Audio8Voice
from aiserver_audio.catalog import (audio8_installed, models_payload,
                                    resolve_audio8_voice)
from aiserver_audio.config import AudioSettings, load_settings
from aiserver_audio.errors import AudioError, audio_error_body
from aiserver_audio.tts import AudioChunk


def _audio8_dateien(ziel: Path):
    ziel.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "model.safetensors",
                 "generation_config.json", "codec.pth", "tokenizer.json",
                 "preprocessor_config.json", "configuration_arktts.py",
                 "modeling_arktts.py", "modeling_arktts_codec.py",
                 "processing_arktts.py"):
        (ziel / name).write_bytes(b"x")


class TestAudio8SidecarSettings(unittest.TestCase):
    def test_defaults_und_env(self):
        s = load_settings({})
        self.assertEqual(s.audio8_base, "http://127.0.0.1:30006")
        self.assertEqual(s.audio8_timeout_s, 600)
        self.assertEqual(s.audio8_model_dir,
                         Path("/mnt/models/audio/audio8"))
        s = load_settings({
            "AUDIO_AUDIO8_BASE": "http://127.0.0.1:39006",
            "AUDIO_AUDIO8_TIMEOUT": "120",
            "AUDIO_AUDIO8_MODEL_DIR": "/tmp/audio8",
        })
        self.assertEqual(s.audio8_base, "http://127.0.0.1:39006")
        self.assertEqual(s.audio8_timeout_s, 120)
        self.assertEqual(s.audio8_model_dir, Path("/tmp/audio8"))

    def test_timeout_muss_positiv_sein(self):
        with self.assertRaises(ValueError):
            load_settings({"AUDIO_AUDIO8_TIMEOUT": "0"})


class TestAudio8Client(unittest.TestCase):
    def _client(self, handler):
        s = AudioSettings(models_dir=Path("/tmp"), preload="",
                          audio8_base="http://127.0.0.1:39906")
        return Audio8Client(
            s, client=httpx.Client(transport=httpx.MockTransport(handler)))

    def test_sendet_text_voice_language_und_liefert_44100(self):
        def handler(request):
            self.assertEqual(request.url.path, "/synthesize")
            self.assertEqual(json.loads(request.content), {
                "text": "Hallo", "voice": "de_male",
                "language": "German"})
            return httpx.Response(
                200, content=b"\x01\x00" * 8,
                headers={"X-Sample-Rate": "44100"})

        info = Audio8Voice("Audio8-Realistic-Air", "de_male", "German")
        teile = list(self._client(handler).synthesize(
            "Hallo", info=info, speed=1.0))
        self.assertEqual(teile, [AudioChunk(b"\x01\x00" * 8, 44100)])

    def test_worker_nicht_erreichbar_502(self):
        def handler(request):
            raise httpx.ConnectError("down", request=request)

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=Audio8Voice("Audio8-Realistic-Air", "de_male"),
                speed=1.0))
        self.assertEqual(ctx.exception.status_code, 502)

    def test_worker_timeout_504(self):
        def handler(request):
            raise httpx.ReadTimeout("langsam", request=request)

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=Audio8Voice("Audio8-Realistic-Air", "de_male"),
                speed=1.0))
        self.assertEqual(ctx.exception.status_code, 504)

    def test_upstream_codes_werden_weitergereicht(self):
        for code in ("vram_exhausted", "reference_missing",
                     "reference_invalid"):
            with self.subTest(code=code):
                def handler(request, _code=code):
                    return httpx.Response(503, json={"error": {
                        "message": "Fehler", "code": _code}})

                with self.assertRaises(AudioError) as ctx:
                    list(self._client(handler).synthesize(
                        "Hallo", info=Audio8Voice(
                            "Audio8-Realistic-Air", "de_male"), speed=1.0))
                self.assertEqual((ctx.exception.status_code,
                                  ctx.exception.code,
                                  ctx.exception.message),
                                 (503, code, "Fehler"))

    def test_unbekannter_upstream_code_wird_upstream_error(self):
        def handler(request):
            return httpx.Response(500, json={"error": {
                "message": "kaputt", "code": "boom"}})

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=Audio8Voice("Audio8-Realistic-Air", "de_male"),
                speed=1.0))
        self.assertEqual(ctx.exception.code, "upstream_error")

    def test_nicht_json_fehler_wird_upstream_error(self):
        def handler(request):
            return httpx.Response(500, text="kaputt")

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=Audio8Voice("Audio8-Realistic-Air", "de_male"),
                speed=1.0))
        self.assertEqual(ctx.exception.code, "upstream_error")


class TestAudio8ErrorKlassifikation(unittest.TestCase):
    def test_server_codes_sind_server_error(self):
        for code in ("upstream_error", "vram_exhausted", "reference_missing",
                     "reference_invalid"):
            with self.subTest(code=code):
                self.assertEqual(
                    audio_error_body("x", code)["error"]["type"],
                    "server_error")


class TestAudio8Catalog(unittest.TestCase):
    def test_installed_prueft_konfiguriertes_verzeichnis(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            self.assertFalse(audio8_installed(models / "audio8"))
            _audio8_dateien(models / "audio8")
            self.assertTrue(audio8_installed(models / "audio8"))

    def test_models_payload_nutzt_audio8_model_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            ziel = models / "custom-audio8"
            _audio8_dateien(ziel)
            payload = models_payload(models, audio8_model_dir=ziel)
        modelle = {m["id"]: m for m in payload["tts"]["models"]}
        self.assertTrue(modelle["Audio8-Realistic-Air"]["installed"])
        self.assertEqual(payload["tts"]["audio8"]["voices"][0]["id"],
                         "de_male")

    def test_voice_defaults_und_unbekannt(self):
        for name in (None, "", "default", "audio8", "de_male",
                     "de_DE-thorsten-medium"):
            self.assertEqual(resolve_audio8_voice(name), "de_male")
        with self.assertRaises(AudioError) as ctx:
            resolve_audio8_voice("Unbekannt")
        self.assertEqual(ctx.exception.status_code, 400)


class FakeAudio8:
    def __init__(self, rate=44100):
        self.rate = rate
        self.aufrufe = []

    def synthesize(self, text, *, info, speed):
        self.aufrufe.append((text, info.voice, info.language, speed))
        yield AudioChunk(b"\x01\x00" * 32, self.rate)


class FakeProzess:
    class _Stdin:
        def write(self, _daten):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class _Stdout:
        def __init__(self):
            self._done = False

        async def read(self, _n):
            if self._done:
                return b""
            self._done = True
            return b"resampled"

    def __init__(self):
        self.stdin = self._Stdin()
        self.stdout = self._Stdout()
        self.returncode = None

    async def wait(self):
        self.returncode = 0

    def terminate(self):
        pass

    def kill(self):
        pass


class TestAudio8AppDispatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.models = Path(self._tmp.name)
        self.audio8_dir = self.models / "audio8-custom"
        _audio8_dateien(self.audio8_dir)
        self.settings = AudioSettings(
            models_dir=self.models, preload="",
            audio8_model_dir=self.audio8_dir,
            audio8_base="http://127.0.0.1:39906")
        self.audio8 = FakeAudio8()

    def tearDown(self):
        self._tmp.cleanup()

    def _app(self, *, encoder_factory=object()):
        return create_audio_app(
            self.settings, stt_engine=object(), tts_engine=object(),
            qwen_tts_engine=object(), chatterbox_engine=object(),
            audio8_engine=self.audio8, duration_probe=lambda _: 1.0,
            encoder_factory=encoder_factory)

    def test_speech_routet_zu_audio8_und_mappt_default_voice(self):
        with TestClient(self._app()) as c:
            r = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "Audio8-Realistic-Air",
                "response_format": "wav"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content[:4], b"RIFF")
        self.assertEqual(self.audio8.aufrufe,
                         [("Guten Tag.", "de_male", "German", 1.0)])

    def test_konfiguriertes_audio8_model_dir_wird_geprueft(self):
        with TestClient(self._app()) as c:
            r = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "audio8",
                "voice": "de_male", "response_format": "wav"})
        self.assertEqual(r.status_code, 200)

    def test_nicht_installiert_ist_503_mit_hinweis(self):
        self.settings = AudioSettings(
            models_dir=self.models, preload="",
            audio8_model_dir=self.models / "fehlt")
        with TestClient(self._app()) as c:
            r = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "audio8"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("runtime/audio8/download-models.sh",
                      r.json()["error"]["message"])

    def test_unbekannte_stimme_ist_400(self):
        with TestClient(self._app()) as c:
            r = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "audio8",
                "voice": "Unbekannt"})
        self.assertEqual(r.status_code, 400)

    def test_rohes_pcm_wird_auf_24k_resampelt(self):
        calls = []

        async def encoder(fmt, rate):
            calls.append((fmt, rate))
            return FakeProzess()

        with TestClient(self._app(encoder_factory=encoder)) as c:
            r = c.post("/v1/audio/speech", json={
                "input": "Guten Tag.", "model": "audio8",
                "response_format": "pcm"})
        self.assertEqual(r.content, b"resampled")
        self.assertEqual(calls, [("pcm", 44100)])
