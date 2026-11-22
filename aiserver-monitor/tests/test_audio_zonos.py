"""Tests fuer ZONOS3-Mini: Settings, Client, Fallback und App-Dispatch."""

import io
import json
import tempfile
import unittest
import wave
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from aiserver_audio.app import create_audio_app
from aiserver_audio.catalog import (ZONOS_MODEL, models_payload,
                                    resolve_voice, resolve_zonos_voice,
                                    zonos_installed)
from aiserver_audio.config import AudioSettings, load_settings
from aiserver_audio.errors import AudioError
from aiserver_audio.tts import AudioChunk
from aiserver_audio.zonos import ZonosClient, ZonosVoice


def _wav(pcm: bytes = b"\x01\x00" * 32, rate: int = 44100) -> bytes:
    ziel = io.BytesIO()
    with wave.open(ziel, "wb") as datei:
        datei.setnchannels(1)
        datei.setsampwidth(2)
        datei.setframerate(rate)
        datei.writeframes(pcm)
    return ziel.getvalue()


def _zonos_dateien(ziel: Path):
    ziel.mkdir(parents=True, exist_ok=True)
    for name in ZONOS_MODEL.required_files:
        (ziel / name).write_bytes(b"x")


class TestZonosSettings(unittest.TestCase):
    def test_defaults_und_env(self):
        s = load_settings({})
        self.assertEqual(s.zonos_base, "http://127.0.0.1:1919")
        self.assertEqual(s.zonos_timeout_s, 600)
        self.assertEqual(s.zonos_model_dir,
                         Path("/mnt/models/zonos3/gguf"))
        s = load_settings({
            "AUDIO_ZONOS_BASE": "http://127.0.0.1:3919",
            "AUDIO_ZONOS_TIMEOUT": "120",
            "AUDIO_ZONOS_MODEL_DIR": "/tmp/zonos",
        })
        self.assertEqual(s.zonos_base, "http://127.0.0.1:3919")
        self.assertEqual(s.zonos_timeout_s, 120)
        self.assertEqual(s.zonos_model_dir, Path("/tmp/zonos"))

    def test_timeout_muss_positiv_sein(self):
        with self.assertRaises(ValueError):
            load_settings({"AUDIO_ZONOS_TIMEOUT": "0"})


class TestZonosClient(unittest.TestCase):
    def _client(self, handler, *, fallback=None):
        s = AudioSettings(models_dir=Path("/tmp"), preload="",
                          zonos_base="http://127.0.0.1:3919")
        return ZonosClient(
            s, client=httpx.Client(transport=httpx.MockTransport(handler)),
            fallback_engine=fallback,
            fallback_info=resolve_voice("kerstin") if fallback else None)

    def test_sendet_controls_und_liefert_pcm(self):
        pcm = b"\x02\x00" * 8

        def handler(request):
            self.assertEqual(request.url.path, "/v2/generate")
            daten = json.loads(request.content)
            self.assertEqual(daten["text"], "Hallo")
            self.assertEqual(daten["language"], "de")
            self.assertEqual(daten["speaker_mode"], "embedding")
            self.assertEqual(daten["speaker_embedding_id"],
                             "default_1e94715774dd144e")
            self.assertEqual(daten["controls"]["emotions.happy"], 0.8)
            self.assertFalse(daten["stream"])
            return httpx.Response(200, content=_wav(pcm))

        info = ZonosVoice(ZONOS_MODEL.id)
        teile = list(self._client(handler).synthesize(
            "Hallo", info=info, speed=1.0))
        self.assertEqual(teile, [AudioChunk(pcm, 44100)])

    def test_server_nicht_erreichbar_502(self):
        def handler(request):
            raise httpx.ConnectError("down", request=request)

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=ZonosVoice(ZONOS_MODEL.id), speed=1.0))
        self.assertEqual(ctx.exception.status_code, 502)

    def test_timeout_504(self):
        def handler(request):
            raise httpx.ReadTimeout("langsam", request=request)

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=ZonosVoice(ZONOS_MODEL.id), speed=1.0))
        self.assertEqual(ctx.exception.status_code, 504)

    def test_fallback_auf_piper_bei_ausfall(self):
        class Fallback:
            def __init__(self):
                self.aufrufe = []

            def synthesize(self, text, *, info, speed):
                self.aufrufe.append((text, info.id, speed))
                yield AudioChunk(b"\x03\x00" * 8, 22050)

        fallback = Fallback()

        def handler(request):
            raise httpx.ConnectError("down", request=request)

        teile = list(self._client(handler, fallback=fallback).synthesize(
            "Hallo", info=ZonosVoice(ZONOS_MODEL.id), speed=1.0))
        self.assertEqual(teile, [AudioChunk(b"\x03\x00" * 8, 22050)])
        self.assertEqual(fallback.aufrufe[0][0], "Hallo")

    def test_kaputtes_wav_ist_upstream_error(self):
        def handler(request):
            return httpx.Response(200, content=b"kein wav")

        with self.assertRaises(AudioError) as ctx:
            list(self._client(handler).synthesize(
                "Hallo", info=ZonosVoice(ZONOS_MODEL.id), speed=1.0))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.code, "upstream_error")


class TestZonosCatalog(unittest.TestCase):
    def test_installed_prueft_gguf_dateien(self):
        with tempfile.TemporaryDirectory() as tmp:
            ziel = Path(tmp) / "gguf"
            self.assertFalse(zonos_installed(ziel))
            _zonos_dateien(ziel)
            self.assertTrue(zonos_installed(ziel))

    def test_models_payload_nutzt_zonos_model_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            ziel = Path(tmp) / "zonos-gguf"
            _zonos_dateien(ziel)
            payload = models_payload(Path(tmp), zonos_model_dir=ziel)
        modelle = {m["id"]: m for m in payload["tts"]["models"]}
        self.assertTrue(modelle[ZONOS_MODEL.id]["installed"])
        self.assertEqual(payload["tts"]["zonos3"]["voices"][0]["id"],
                         "liesel")

    def test_voice_defaults_und_unbekannt(self):
        for name in (None, "", "default", "zonos", "liesel", "liesel22",
                     "liesel-22", "happy22"):
            self.assertEqual(resolve_zonos_voice(name), "liesel")
        with self.assertRaises(AudioError) as ctx:
            resolve_zonos_voice("Unbekannt")
        self.assertEqual(ctx.exception.status_code, 400)


class FakeZonos:
    def __init__(self):
        self.aufrufe = []

    def synthesize(self, text, *, info, speed):
        self.aufrufe.append((text, info.voice, info.language, speed))
        yield AudioChunk(b"\x04\x00" * 32, 44100)


class FakePiper:
    def synthesize(self, text, *, info, speed):
        yield AudioChunk(b"\x05\x00" * 32, 22050)


class TestZonosAppDispatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.models = Path(self._tmp.name)
        self.zonos_dir = self.models / "zonos-gguf"
        _zonos_dateien(self.zonos_dir)
        self.engine = FakeZonos()
        settings = AudioSettings(
            models_dir=self.models,
            preload="",
            zonos_base="http://127.0.0.1:3919",
            zonos_model_dir=self.zonos_dir,
        )
        self.app = create_audio_app(
            settings, tts_engine=FakePiper(), zonos_engine=self.engine,
            qwen_tts_engine=FakeZonos(), audio8_engine=FakeZonos())
        self.client = TestClient(self.app)

    def tearDown(self):
        self._tmp.cleanup()

    def test_zonos_modell_wird_an_engine_geroutet(self):
        antwort = self.client.post("/v1/audio/speech", json={
            "model": "zonos3-mini",
            "voice": "liesel",
            "input": "Hallo Welt.",
            "response_format": "wav",
            "language": "German",
        })
        self.assertEqual(antwort.status_code, 200, antwort.text)
        self.assertEqual(antwort.headers["content-type"], "audio/wav")
        self.assertTrue(antwort.content.startswith(b"RIFF"))
        self.assertEqual(self.engine.aufrufe,
                         [("Hallo Welt.", "liesel", "de", 1.0)])

    def test_models_endpoint_listet_zonos(self):
        antwort = self.client.get("/v1/audio/models")
        self.assertEqual(antwort.status_code, 200)
        modelle = {m["id"]: m for m in antwort.json()["tts"]["models"]}
        self.assertTrue(modelle[ZONOS_MODEL.id]["installed"])
