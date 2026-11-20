"""Tests fuer die Provider-API (/api) und das modellbasierte /v1-Routing.

Die App laeuft mit TestClient, einer temporaeren Registry und einer
ServerControl-Attrappe; der Upstream ist ein httpx-MockTransport. Es wird nie
ein Socket zu einem echten Modellserver geoeffnet.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from aiserver_monitor.app import create_app
from aiserver_monitor.autoload import ProviderRouter
from aiserver_monitor.config import Config
from aiserver_monitor.registry import Provider, load_registry, save_provider
from aiserver_monitor.serverctl import ServerControlError
from aiserver_monitor.webapi import SystemProbe


class FakeControl:
    """ServerControl-Attrappe mit aufzeichenbaren Start/Stop-Aufrufen."""

    def __init__(self, states: dict[str, str] | None = None):
        self.states = dict(states or {})
        self.start_calls: list[tuple[str, float | None]] = []
        self.stop_calls: list[tuple[str, float | None]] = []
        self.start_error: Exception | None = None
        self.logs: dict[str, list[str]] = {}
        self.transitioning = False

    @property
    def backends(self) -> dict[str, object]:
        return {name: object() for name in self.states}

    @property
    def backend_names(self) -> tuple[str, ...]:
        return tuple(self.states)

    async def snapshot(self) -> dict:
        backends = [
            {"name": name, "label": name, "state": state, "unit": None,
             "managed_by_systemd": False}
            for name, state in self.states.items()
        ]
        laufend = [name for name, state in self.states.items()
                   if state == "running"]
        return {
            "state": "running" if laufend else "stopped",
            "backend": laufend[0] if laufend else None,
            "backends": backends,
            "unit": None,
            "managed_by_systemd": False,
            "upstream": "http://127.0.0.1:30001",
            "error": None,
        }

    async def start_and_wait(self, backend, *, timeout_s=None):
        self.start_calls.append((backend, timeout_s))
        if self.start_error is not None:
            raise self.start_error
        self.states[backend] = "running"
        return await self.snapshot()

    async def stop_and_wait(self, backend, *, timeout_s=None):
        self.stop_calls.append((backend, timeout_s))
        self.states[backend] = "stopped"
        return await self.snapshot()

    def log_lines(self, backend, lines=200):
        return list(self.logs.get(backend, []))[-lines:]

    def refresh_from_registry(self, directory=None):
        return True


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gantry-api-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "providers"
        self.dir.mkdir()
        self.db = Path(self._tmp.name) / "monitor.db"

    def provider(self, provider_id: str, **kwargs) -> None:
        kwargs.setdefault("name", provider_id.title())
        kwargs.setdefault("start_cmd", ["true"])
        save_provider(Provider(id=provider_id, **kwargs), self.dir)

    def router(self, control: FakeControl, **kwargs) -> ProviderRouter:
        return ProviderRouter(
            Config(db_path=str(self.db)), control,
            registry_dir=self.dir, registry_ttl_s=0.0, **kwargs)

    def app(self, control: FakeControl, *, transport=None,
            system_probe=None, router=None):
        router = router or self.router(control)
        return create_app(
            Config(db_path=str(self.db)), transport=transport,
            server_control=control, provider_router=router,
            system_probe=system_probe, enable_background=False)


class TestProviderApi(ApiCase):
    def test_liste_liefert_felder_und_status(self) -> None:
        self.provider("alpha", name="Alpha", kind="custom",
                      host="127.0.0.1", port=8081, model="alpha-modell",
                      settings={"auto_load": False, "idle_unload_min": 5})
        self.provider("qwen", name="Qwen", kind="sglang",
                      host="127.0.0.1", port=30001, model="qwen-modell",
                      settings={"auto_load": True})
        control = FakeControl({"qwen": "running", "alpha": "stopped"})
        with TestClient(self.app(control)) as client:
            antwort = client.get("/api/providers")
        self.assertEqual(antwort.status_code, 200)
        daten = antwort.json()
        self.assertIsInstance(daten, list)
        self.assertEqual([p["id"] for p in daten], ["qwen", "alpha"])
        qwen = daten[0]
        for feld in ("id", "name", "kind", "model", "host", "port",
                     "status", "uptime", "settings", "auto_load"):
            self.assertIn(feld, qwen)
        self.assertEqual(qwen["status"], "running")
        self.assertTrue(qwen["auto_load"])
        self.assertEqual(daten[1]["status"], "stopped")
        self.assertFalse(daten[1]["auto_load"])
        # Keine Startbefehle in der oeffentlichen Darstellung.
        self.assertNotIn("start_cmd", qwen)
        self.assertNotIn("stop_cmd", qwen)

    def test_detail_und_404(self) -> None:
        self.provider("alpha", name="Alpha", model="m")
        control = FakeControl({"alpha": "stopped"})
        with TestClient(self.app(control)) as client:
            ok = client.get("/api/providers/alpha")
            fehlt = client.get("/api/providers/nope")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["id"], "alpha")
        self.assertEqual(fehlt.status_code, 404)

    def test_patch_persistiert_settings_und_name(self) -> None:
        self.provider("alpha", name="Alpha", model="m",
                      settings={"auto_load": True})
        control = FakeControl({"alpha": "stopped"})
        with TestClient(self.app(control)) as client:
            antwort = client.patch("/api/providers/alpha", json={
                "name": "Alpha Neu",
                "auto_load": False,
                "settings": {"idle_unload_min": 7},
            })
        self.assertEqual(antwort.status_code, 200, antwort.text)
        gespeichert = load_registry(self.dir)["alpha"]
        self.assertEqual(gespeichert.name, "Alpha Neu")
        self.assertIs(gespeichert.settings["auto_load"], False)
        self.assertEqual(gespeichert.settings["idle_unload_min"], 7)

    def test_patch_lehnt_unbekanntes_feld_und_id_ab(self) -> None:
        self.provider("alpha", name="Alpha", model="m")
        control = FakeControl({"alpha": "stopped"})
        with TestClient(self.app(control)) as client:
            unbekannt = client.patch("/api/providers/alpha",
                                     json={"zauber": 1})
            mit_id = client.patch("/api/providers/alpha",
                                  json={"id": "neu"})
        self.assertEqual(unbekannt.status_code, 400)
        self.assertEqual(mit_id.status_code, 400)

    def test_patch_ungueltiger_port_ist_400(self) -> None:
        self.provider("alpha", name="Alpha", model="m")
        control = FakeControl({"alpha": "stopped"})
        with TestClient(self.app(control)) as client:
            antwort = client.patch("/api/providers/alpha",
                                   json={"port": 99999})
        self.assertEqual(antwort.status_code, 400)

    def test_start_und_stop(self) -> None:
        self.provider("alpha", name="Alpha", model="m")
        control = FakeControl({"alpha": "stopped"})
        with TestClient(self.app(control)) as client:
            start = client.post("/api/providers/alpha/start")
            self.assertEqual(start.status_code, 200, start.text)
            self.assertEqual(start.json()["status"], "running")
            stop = client.post("/api/providers/alpha/stop")
            self.assertEqual(stop.status_code, 200, stop.text)
            self.assertEqual(stop.json()["status"], "stopped")
        self.assertEqual(control.start_calls, [("alpha", 600.0)])
        self.assertEqual(control.stop_calls, [("alpha", 60.0)])

    def test_startfehler_ist_502_mit_log_tail(self) -> None:
        self.provider("alpha", name="Alpha", model="m")
        control = FakeControl({"alpha": "stopped"})
        control.start_error = ServerControlError("start kaputt")
        control.logs["alpha"] = ["zeile 1", "zeile 2"]
        with TestClient(self.app(control)) as client:
            antwort = client.post("/api/providers/alpha/start")
        self.assertEqual(antwort.status_code, 502)
        self.assertIn("start kaputt", antwort.json()["error"])
        self.assertEqual(antwort.json()["log_tail"], ["zeile 1", "zeile 2"])

    def test_logs_liefert_tail(self) -> None:
        logfile = Path(self._tmp.name) / "alpha.log"
        logfile.write_text("\n".join(f"zeile {i}" for i in range(50)),
                           encoding="utf-8")
        self.provider("alpha", name="Alpha", model="m",
                      logfile=str(logfile))
        control = FakeControl({"alpha": "stopped"})
        with TestClient(self.app(control)) as client:
            antwort = client.get("/api/providers/alpha/logs?lines=3")
            fehlt = client.get("/api/providers/nope/logs")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json()["lines"],
                         ["zeile 47", "zeile 48", "zeile 49"])
        self.assertEqual(fehlt.status_code, 404)

    def test_system_liefert_gpu_ram_und_disk(self) -> None:
        async def runner(args):
            if args[0] == "nvidia-smi":
                return 0, "NVIDIA Test, 16384, 2048, 14336, 45, 12\n", ""
            if args[0] == "free":
                return 0, ("              total        used        free"
                           "      shared  buff/cache   available\n"
                           "Mem:   1000 400 600 0 0 800\n"
                           "Swap:   100  10  90\n"), ""
            return 127, "", "unbekannt"

        self.provider("alpha", name="Alpha", model="m")
        control = FakeControl({"alpha": "stopped"})
        with TestClient(self.app(
                control, system_probe=SystemProbe(runner=runner))) as client:
            antwort = client.get("/api/system")
        self.assertEqual(antwort.status_code, 200)
        daten = antwort.json()
        self.assertEqual(daten["gpus"][0]["name"], "NVIDIA Test")
        self.assertEqual(daten["gpus"][0]["vram_free_mib"], 14336)
        self.assertEqual(daten["ram"]["total_bytes"], 1000)
        self.assertEqual(daten["ram"]["available_bytes"], 800)
        self.assertEqual(daten["disk"]["mount"], "/")


class TestRoutingApi(ApiCase):
    def _app_mit_transport(self, control, seen, *, config=None,
                           router=None):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), request.content))
            if request.url.path == "/api/ps":
                return httpx.Response(404, json={"error": "nein"})
            return httpx.Response(200, json={
                "id": "x", "object": "text_completion", "choices": []})

        config = config or Config(db_path=str(self.db))
        router = router or ProviderRouter(
            config, control, registry_dir=self.dir, registry_ttl_s=0.0)
        return create_app(
            config,
            transport=httpx.MockTransport(handler),
            server_control=control, provider_router=router,
            enable_background=False)

    def test_request_auf_provider_modell_routet_auf_dessen_port(self) -> None:
        self.provider(
            "coder", name="Coder", model="coder-modell",
            host="127.0.0.1", port=8088,
            health_url="http://127.0.0.1:8088/health",
            settings={"auto_load": True})
        control = FakeControl({"coder": "stopped"})
        seen: list[tuple[str, bytes]] = []
        with TestClient(self._app_mit_transport(control, seen)) as client:
            antwort = client.post("/v1/completions", json={
                "model": "coder", "prompt": "hallo", "max_tokens": 1})
        self.assertEqual(antwort.status_code, 200, antwort.text)
        self.assertEqual(control.start_calls, [("coder", 600.0)])
        self.assertEqual(len(seen), 1)
        url, body = seen[0]
        self.assertTrue(url.startswith("http://127.0.0.1:8088/"), url)
        self.assertEqual(json.loads(body)["model"], "coder-modell")

    def test_modellname_selbst_routet_ohne_umbenennung(self) -> None:
        self.provider(
            "coder", name="Coder", model="coder-modell",
            host="127.0.0.1", port=8088,
            settings={"auto_load": True})
        control = FakeControl({"coder": "running"})
        seen: list[tuple[str, bytes]] = []
        with TestClient(self._app_mit_transport(control, seen)) as client:
            antwort = client.post("/v1/completions", json={
                "model": "coder-modell", "prompt": "x"})
        self.assertEqual(antwort.status_code, 200, antwort.text)
        self.assertEqual(control.start_calls, [])
        self.assertTrue(seen[0][0].startswith("http://127.0.0.1:8088/"))
        self.assertEqual(json.loads(seen[0][1])["model"], "coder-modell")

    def test_globaler_alias_bleibt_auf_dem_upstream(self) -> None:
        self.provider("qwen", name="Qwen", model="alias-modell",
                      host="127.0.0.1", port=30001,
                      settings={"auto_load": False})
        control = FakeControl({"qwen": "stopped"})
        seen: list[tuple[str, bytes]] = []
        config = Config(db_path=str(self.db), model_alias="alias-modell")
        with TestClient(self._app_mit_transport(
                control, seen, config=config)) as client:
            antwort = client.post("/v1/completions", json={
                "model": "alias-modell", "prompt": "x"})
        self.assertEqual(antwort.status_code, 200, antwort.text)
        self.assertEqual(control.start_calls, [])
        self.assertTrue(seen[0][0].startswith(
            "http://127.0.0.1:30001/"), seen[0][0])

    def test_autoload_fehler_ist_503_mit_log_tail(self) -> None:
        self.provider("coder", name="Coder", model="coder-modell",
                      host="127.0.0.1", port=8088,
                      settings={"auto_load": True})
        control = FakeControl({"coder": "stopped"})
        control.start_error = ServerControlError("start kaputt")
        control.logs["coder"] = ["lade...", "oom"]
        seen: list[tuple[str, bytes]] = []
        with TestClient(self._app_mit_transport(control, seen)) as client:
            antwort = client.post("/v1/completions", json={
                "model": "coder-modell", "prompt": "x"})
        self.assertEqual(antwort.status_code, 503)
        daten = antwort.json()
        self.assertEqual(daten["provider"], "coder")
        self.assertIn("konnte nicht gestartet werden", daten["error"]["message"])
        self.assertEqual(daten["log_tail"], ["lade...", "oom"])
        self.assertEqual(seen, [])

    def test_stream_gibt_lease_frei(self) -> None:
        self.provider("coder", name="Coder", model="coder-modell",
                      host="127.0.0.1", port=8088,
                      settings={"auto_load": True})

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                         b"data: [DONE]\n\n"),
                headers={"content-type": "text/event-stream"})

        control = FakeControl({"coder": "running"})
        router = self.router(control)
        app = create_app(Config(db_path=str(self.db)),
                         transport=httpx.MockTransport(handler),
                         server_control=control, provider_router=router,
                         enable_background=False)
        with TestClient(app) as client:
            with client.stream("POST", "/v1/chat/completions",
                               json={"model": "coder-modell",
                                     "stream": True}) as antwort:
                self.assertEqual(antwort.status_code, 200)
                self.assertTrue(b"".join(antwort.iter_bytes()))
        self.assertEqual(router.active_requests("coder"), 0)


if __name__ == "__main__":
    unittest.main()
