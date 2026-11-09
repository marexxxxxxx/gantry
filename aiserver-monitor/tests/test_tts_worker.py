"""Tests fuer das gemeinsame Lifecycle-Geruest der TTS-Worker."""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiserver_tts_worker import install_lifecycle_routes


class FakeEngine:
    def __init__(self):
        self.model_loaded = False
        self.loads = 0
        self.unloads = 0

    def load(self):
        self.loads += 1
        self.model_loaded = True

    def unload(self):
        self.unloads += 1
        self.model_loaded = False


class TestLifecycleRoutes(unittest.TestCase):
    def _app(self):
        engine = FakeEngine()
        app = FastAPI()
        install_lifecycle_routes(app, engine)
        return app, engine

    def test_healthz_meldet_ladezustand(self):
        app, _engine = self._app()
        with TestClient(app) as c:
            self.assertEqual(c.get("/healthz").json(),
                             {"status": "ok", "model_loaded": False})
            c.post("/load")
            self.assertEqual(c.get("/healthz").json(),
                             {"status": "ok", "model_loaded": True})

    def test_load_und_unload_rufen_die_engine(self):
        app, engine = self._app()
        with TestClient(app) as c:
            self.assertEqual(c.post("/load").json(),
                             {"status": "ok", "model_loaded": True})
            self.assertEqual(c.post("/unload").json(),
                             {"status": "ok", "model_loaded": False})
        self.assertEqual((engine.loads, engine.unloads), (1, 1))
