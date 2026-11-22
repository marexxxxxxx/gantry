"""Tests fuer die GPU-Arbiter-API (/api/arbiter) und den HOLD-Hook im /v1-Routing.

Die App laeuft mit ASGITransport in einem einzigen Event-Loop
(unittest.IsolatedAsyncioTestCase), einer temporaeren (leeren) Registry und
einer ServerControl-Attrappe; der Upstream ist ein httpx-MockTransport. Es wird
nie ein Socket zu einem echten Modellserver geoeffnet.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from aiserver_monitor.app import create_app
from aiserver_monitor.autoload import ProviderRouter
from aiserver_monitor.config import Config
from aiserver_monitor.gpu_arbiter import GpuArbiter


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


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "x", "object": "text_completion",
                                     "choices": []})


class _RecordingRouter(ProviderRouter):
    """ProviderRouter mit leerer Registry, der prepare()-Aufrufe mitschneidet."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prepared: list[str] = []

    async def prepare(self, model):
        self.prepared.append(model)
        return await super().prepare(model)


class TestArbiterWebapi(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gantry-arbiter-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "providers"
        self.dir.mkdir()
        self.db = Path(self._tmp.name) / "monitor.db"

    def _router(self, control: FakeControl) -> _RecordingRouter:
        return _RecordingRouter(
            Config(db_path=str(self.db)), control,
            registry_dir=self.dir, registry_ttl_s=0.0)

    def _app(self, control: FakeControl, token: str = "test-token"):
        router = self._router(control)
        app = create_app(
            Config(db_path=str(self.db), arbiter_token=token),
            transport=httpx.MockTransport(_ok_handler),
            server_control=control,
            provider_router=router,
            enable_background=False,
        )
        return app, router

    async def _client(self, app, token: str = "test-token") -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test",
                                 headers={"X-Arbiter-Token": token})

    async def test_hold_haelt_chat_request_waehrend_pause(self) -> None:
        control = FakeControl({"edge0": "running"})
        app, router = self._app(control)
        async with app.router.lifespan_context(app):
            arbiter = app.state.arbiter
            lease = await arbiter.request_lease()
            self.assertEqual(arbiter.state.name, "PAUSED")
            async with await self._client(app) as client:
                task = asyncio.create_task(client.post(
                    "/v1/completions",
                    json={"model": "egal", "prompt": "hi"}))
                await asyncio.sleep(0.1)
                self.assertFalse(task.done())
                self.assertEqual(router.prepared, [])
                await arbiter.complete(lease.lease_id)
                antwort = await asyncio.wait_for(task, timeout=5)
                self.assertEqual(antwort.status_code, 200)
                self.assertEqual(router.prepared, ["egal"])

    async def test_embeddings_nicht_gehalten(self) -> None:
        control = FakeControl({"edge0": "running"})
        app, _router = self._app(control)
        async with app.router.lifespan_context(app):
            arbiter = app.state.arbiter
            await arbiter.request_lease()
            self.assertEqual(arbiter.state.name, "PAUSED")
            async with await self._client(app) as client:
                antwort = await asyncio.wait_for(
                    client.post("/v1/embeddings",
                                json={"model": "e", "input": "hi"}),
                    timeout=5)
            self.assertEqual(antwort.status_code, 200)

    async def test_arbiter_lease_endpoint(self) -> None:
        control = FakeControl({"edge0": "running"})
        app, _router = self._app(control)
        async with app.router.lifespan_context(app):
            async with await self._client(app) as client:
                antwort = await client.post("/api/arbiter/lease", json={})
            self.assertEqual(antwort.status_code, 200)
            daten = antwort.json()
            self.assertIn("lease_id", daten)
            self.assertEqual(daten["state"], "granted")
            self.assertEqual(daten["position"], 0)

    async def test_arbiter_endpoint_ohne_token_403(self) -> None:
        control = FakeControl({"edge0": "running"})
        app, _router = self._app(control, token="")
        async with app.router.lifespan_context(app):
            async with await self._client(app, token="") as client:
                antwort = await client.post("/api/arbiter/lease", json={})
            self.assertEqual(antwort.status_code, 403)

    async def test_arbiter_endpoint_falscher_token_403(self) -> None:
        control = FakeControl({"edge0": "running"})
        app, _router = self._app(control, token="richtig")
        async with app.router.lifespan_context(app):
            async with await self._client(app, token="falsch") as client:
                antwort = await client.post("/api/arbiter/lease", json={})
            self.assertEqual(antwort.status_code, 403)

    async def test_ende_zu_ende_lease_run_und_resume(self) -> None:
        control = FakeControl({"edge0": "running"})
        freigabe = asyncio.Event()

        class _BlockingProcess:
            returncode = 0

            async def communicate(self):
                await freigabe.wait()
                return (b"out", b"err")

        async def factory(*args, **kwargs):
            return _BlockingProcess()

        config = Config(db_path=str(self.db), arbiter_token="test-token")
        arbiter = GpuArbiter(
            config, control, inflight=lambda: 0, subprocess_factory=factory)
        router = self._router(control)
        app = create_app(
            config,
            transport=httpx.MockTransport(_ok_handler),
            server_control=control,
            provider_router=router,
            arbiter=arbiter,
            enable_background=False,
        )
        async with app.router.lifespan_context(app):
            async with await self._client(app) as client:
                run_task = asyncio.create_task(client.post(
                    "/api/arbiter/run",
                    json={"command": ["blender", "-b", "x.blend"]}))
                for _ in range(200):
                    if arbiter.state.name == "PAUSED":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(arbiter.state.name, "PAUSED")
                self.assertEqual(len(control.stop_calls), 1)

                chat_task = asyncio.create_task(client.post(
                    "/v1/completions",
                    json={"model": "egal", "prompt": "hi"}))
                await asyncio.sleep(0.1)
                self.assertFalse(chat_task.done())
                self.assertEqual(router.prepared, [])

                freigabe.set()
                run_antwort = await asyncio.wait_for(run_task, timeout=5)
                self.assertEqual(run_antwort.status_code, 200)
                antwort = await asyncio.wait_for(chat_task, timeout=5)
                self.assertEqual(antwort.status_code, 200)
                self.assertEqual(router.prepared, ["egal"])
                self.assertEqual(len(control.start_calls), 1)
