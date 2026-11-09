"""Tests fuer Audio8-Settings, Fehlerklassifikation und den Sidecar-Client."""

import json
import unittest
from pathlib import Path

import httpx

from aiserver_audio.audio8 import Audio8Client, Audio8Voice
from aiserver_audio.config import AudioSettings, load_settings
from aiserver_audio.errors import AudioError, audio_error_body
from aiserver_audio.tts import AudioChunk


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
