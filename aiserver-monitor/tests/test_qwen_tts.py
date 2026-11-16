import unittest
from pathlib import Path

import numpy as np

from aiserver_qwen_tts.config import QwenTTSSettings, load_settings
from aiserver_qwen_tts.engine import QwenTTSEngine
from aiserver_qwen_tts.errors import QwenTTSError


class TestQwenTTSSettings(unittest.TestCase):
    def test_defaults(self):
        s = load_settings({})
        self.assertEqual(s.host, "127.0.0.1")
        self.assertEqual(s.port, 30005)
        self.assertEqual(s.model_dir, Path("/mnt/models/audio/experiments/qwen"))
        self.assertEqual(s.device, "cuda:0")
        self.assertEqual(s.dtype, "bfloat16")
        self.assertEqual(s.default_language, "German")

    def test_env_overrides(self):
        s = load_settings({
            "QWEN_TTS_HOST": "0.0.0.0",
            "QWEN_TTS_PORT": "39005",
            "QWEN_TTS_MODEL_DIR": "/tmp/qwen",
            "QWEN_TTS_DEVICE": "cuda:1",
        })
        self.assertEqual((s.host, s.port, str(s.model_dir), s.device),
                         ("0.0.0.0", 39005, "/tmp/qwen", "cuda:1"))

    def test_port_must_be_positive(self):
        with self.assertRaises(ValueError):
            load_settings({"QWEN_TTS_PORT": "0"})


class FakeModel:
    def __init__(self):
        self.calls = []

    def generate_custom_voice(self, *, text, language, speaker, instruct=None):
        self.calls.append((text, language, speaker, instruct))
        return ([np.array([0.0, 0.5, -0.5], dtype=np.float32)], 24000)


class TestQwenTTSEngine(unittest.TestCase):
    def _engine(self, loader=None):
        settings = QwenTTSSettings(model_dir=Path("/tmp/qwen"), device="cpu")
        return QwenTTSEngine(settings, model_loader=loader)

    def test_loads_lazily_and_only_once(self):
        model = FakeModel()
        calls = []

        def loader(_settings):
            calls.append("load")
            return model

        engine = self._engine(loader)
        self.assertFalse(engine.model_loaded)
        engine.synthesize("Hallo", speaker="Vivian", language="German")
        engine.synthesize("Nochmal", speaker="Ryan", language="German")
        self.assertTrue(engine.model_loaded)
        self.assertEqual(calls, ["load"])

    def test_synthesize_returns_int16_pcm(self):
        engine = self._engine(lambda _s: FakeModel())
        pcm, sr = engine.synthesize(
            "Guten Tag.", speaker="Vivian", language="German")
        self.assertEqual(sr, 24000)
        self.assertEqual(len(pcm), 6)
        self.assertEqual(np.frombuffer(pcm, dtype=np.int16).tolist(),
                         [0, 16383, -16383])

    def test_unload_drops_model(self):
        engine = self._engine(lambda _s: FakeModel())
        engine.load()
        engine.unload()
        self.assertFalse(engine.model_loaded)

    def test_oom_maps_to_503(self):
        def loader(_settings):
            raise RuntimeError("CUDA out of memory")

        engine = self._engine(loader)
        with self.assertRaises(QwenTTSError) as ctx:
            engine.load()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.code, "vram_exhausted")

    def test_modell_fehlt_maps_to_503(self):
        def loader(_settings):
            raise FileNotFoundError("config.json")

        engine = self._engine(loader)
        with self.assertRaises(QwenTTSError) as ctx:
            engine.load()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.code, "model_unavailable")
        self.assertIn("download-models.sh", ctx.exception.message)

    def test_oom_waehrend_synthese_maps_to_503(self):
        class OomModel:
            def generate_custom_voice(self, **_kwargs):
                raise RuntimeError("CUDA out of memory")

        engine = self._engine(lambda _s: OomModel())
        with self.assertRaises(QwenTTSError) as ctx:
            engine.synthesize("Hallo", speaker="Vivian", language="German")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.code, "vram_exhausted")


from fastapi.testclient import TestClient

from aiserver_qwen_tts.app import create_qwen_tts_app


class FakeEngine:
    def __init__(self):
        self.model_loaded = False
        self.loads = 0
        self.unloads = 0
        self.calls = []

    def load(self):
        self.loads += 1
        self.model_loaded = True

    def unload(self):
        self.unloads += 1
        self.model_loaded = False

    def synthesize(self, text, *, speaker, language, instruct=None):
        self.calls.append((text, speaker, language, instruct))
        return b"\x01\x00" * 8, 24000


class OomEngine(FakeEngine):
    def load(self):
        raise QwenTTSError(503, "vram_exhausted", "Nicht genug VRAM")


class TestQwenTTSApp(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.app = create_qwen_tts_app(
            QwenTTSSettings(model_dir=Path("/tmp/qwen")), engine=self.engine)

    def test_healthz_reports_model_state(self):
        with TestClient(self.app) as c:
            self.assertEqual(c.get("/healthz").json(),
                             {"status": "ok", "model_loaded": False})
            c.post("/load")
            self.assertEqual(c.get("/healthz").json(),
                             {"status": "ok", "model_loaded": True})

    def test_load_and_unload(self):
        with TestClient(self.app) as c:
            self.assertEqual(c.post("/load").status_code, 200)
            self.assertEqual(self.engine.loads, 1)
            self.assertEqual(c.post("/unload").status_code, 200)
            self.assertEqual(self.engine.unloads, 1)

    def test_synthesize_returns_pcm_and_sample_rate(self):
        with TestClient(self.app) as c:
            r = c.post("/synthesize", json={
                "text": "Guten Tag.", "voice": "Ryan",
                "language": "German", "instruct": "freundlich"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"\x01\x00" * 8)
        self.assertEqual(r.headers["x-sample-rate"], "24000")
        self.assertEqual(self.engine.calls,
                         [("Guten Tag.", "Ryan", "German", "freundlich")])

    def test_empty_text_is_400(self):
        with TestClient(self.app) as c:
            self.assertEqual(
                c.post("/synthesize", json={"text": "  "}).status_code, 400)

    def test_unknown_voice_is_400(self):
        with TestClient(self.app) as c:
            self.assertEqual(
                c.post("/synthesize", json={
                    "text": "Hallo", "voice": "Unbekannt"}).status_code, 400)

    def test_oom_on_load_is_503(self):
        app = create_qwen_tts_app(
            QwenTTSSettings(model_dir=Path("/tmp/qwen")), engine=OomEngine())
        with TestClient(app) as c:
            r = c.post("/load")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"]["code"], "vram_exhausted")
