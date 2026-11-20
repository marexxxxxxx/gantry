"""Tests fuer den Rueckfall von ``GET /v1/models``.

``/v1/models`` darf nie mit 500 enden: ist kein Backend erreichbar, liefert
das Gateway eine OpenAI-Modellliste aus der Provider-Registry statt den
httpx-Verbindungsfehler weiterzureichen. Laeuft ein Backend, bleibt der
gewohnte Passthrough erhalten.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from aiserver_monitor.app import create_app
from aiserver_monitor.autoload import ProviderRouter
from aiserver_monitor.config import Config
from aiserver_monitor.registry import Provider, save_provider


class FakeControl:
    """Minimale ServerControl-Attrappe (alle Provider gestoppt)."""

    @property
    def backends(self) -> dict[str, object]:
        return {}

    @property
    def backend_names(self) -> tuple[str, ...]:
        return ()

    async def snapshot(self) -> dict:
        return {
            "state": "stopped", "backend": None, "backends": [],
            "unit": None, "managed_by_systemd": False,
            "upstream": "http://127.0.0.1:30001", "error": None,
        }

    async def start_and_wait(self, backend, *, timeout_s=None):
        raise AssertionError("kein Autoload in den /v1/models-Tests")

    async def stop_and_wait(self, backend, *, timeout_s=None):
        raise AssertionError("kein Stop in den /v1/models-Tests")

    def log_lines(self, backend, lines=200):
        return []

    def refresh_from_registry(self, directory=None):
        return True


class ModelsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gantry-models-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "providers"
        self.dir.mkdir()
        self.db = Path(self._tmp.name) / "monitor.db"

    def provider(self, provider_id: str, **kwargs) -> None:
        kwargs.setdefault("name", provider_id.title())
        kwargs.setdefault("start_cmd", ["true"])
        save_provider(Provider(id=provider_id, **kwargs), self.dir)

    def app(self, transport: httpx.AsyncBaseTransport):
        control = FakeControl()
        router = ProviderRouter(
            Config(db_path=str(self.db)), control,
            registry_dir=self.dir, registry_ttl_s=0.0)
        return create_app(
            Config(db_path=str(self.db)), transport=transport,
            server_control=control, provider_router=router,
            enable_background=False)


def _unreachbar(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("kein Backend", request=request)


class TestV1ModelsFallback(ModelsCase):
    def test_upstream_unerreichbar_liefert_registry_liste(self) -> None:
        self.provider("alpha", name="Alpha", model="alpha-modell",
                      enabled=True)
        self.provider("beta", name="Beta", model="",
                      enabled=True)
        self.provider("aus", name="Aus", model="aus-modell",
                      enabled=False)
        with TestClient(self.app(httpx.MockTransport(_unreachbar))) as client:
            antwort = client.get("/v1/models")
        self.assertEqual(antwort.status_code, 200, antwort.text)
        daten = antwort.json()
        self.assertEqual(daten["object"], "list")
        self.assertEqual(daten["data"], [
            {"id": "alpha-modell", "object": "model", "owned_by": "alpha"},
            {"id": "Beta", "object": "model", "owned_by": "beta"},
        ])

    def test_leere_registry_liefert_leere_liste(self) -> None:
        with TestClient(self.app(httpx.MockTransport(_unreachbar))) as client:
            antwort = client.get("/v1/models")
        self.assertEqual(antwort.status_code, 200, antwort.text)
        self.assertEqual(antwort.json(), {"object": "list", "data": []})

    def test_upstream_laeuft_bleibt_passthrough(self) -> None:
        self.provider("alpha", name="Alpha", model="alpha-modell")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"object": "list",
                           "data": [{"id": "vom-upstream",
                                     "object": "model",
                                     "owned_by": "sglang"}]})

        with TestClient(self.app(httpx.MockTransport(handler))) as client:
            antwort = client.get("/v1/models")
        self.assertEqual(antwort.status_code, 200)
        daten = antwort.json()
        ids = {eintrag["id"] for eintrag in daten["data"]}
        # Die Upstream-Liste kommt durch (der Gateway-Alias darf sie nur
        # ergaenzen, nicht ersetzen).
        self.assertIn("vom-upstream", ids)

    def test_mit_anfuehrendem_schraegstrich(self) -> None:
        self.provider("alpha", name="Alpha", model="alpha-modell")
        with TestClient(self.app(httpx.MockTransport(_unreachbar))) as client:
            antwort = client.get("/v1/models/")
        self.assertEqual(antwort.status_code, 200, antwort.text)
        self.assertEqual(antwort.json()["data"],
                         [{"id": "alpha-modell", "object": "model",
                           "owned_by": "alpha"}])


if __name__ == "__main__":
    unittest.main()
