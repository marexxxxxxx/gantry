"""Tests fuer Provider-Host/Port, Auto-Load, VRAM-Guard und Idle-Unload.

Die Server-Control-Tests benutzen eine echte, winzige Health-HTTP-Quelle auf
127.0.0.1 (kein Modell, kein GPU). Die Router-Tests ersetzen die Steuerung
durch ein Fake, damit Start/Stop-Reihenfolge und Ruhezeiten ohne echte
Prozesse pruefbar sind.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aiserver_monitor.autoload import (
    ProviderRouter,
    ProviderUnavailable,
)
from aiserver_monitor.config import Config
from aiserver_monitor.registry import Provider, load_registry, save_provider
from aiserver_monitor.serverctl import (
    ServerControl,
    ServerControlError,
    read_log_tail,
)

REPO = Path("/repo")


class _HealthHandler(BaseHTTPRequestHandler):
    """Antwortet auf ``/health`` mit dem Status des Testservers."""

    def do_GET(self):  # noqa: N802 (http.server-API)
        status = getattr(self.server, "status", 200)
        if self.path.split("?")[0] != "/health":
            status = 404
        if status == 200:
            inhalt = b'{"status":"ok"}'
        else:
            inhalt = b'{"status":"loading"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(inhalt)))
        self.end_headers()
        self.wfile.write(inhalt)

    def log_message(self, *_args):  # Testausgabe bleibt still.
        pass


class HealthServer:
    """Mini-HTTP-Server auf einem freien Port; Status ist umschaltbar."""

    def __init__(self, status: int = 200):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        self._httpd.status = status
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def set_status(self, status: int) -> None:
        self._httpd.status = status

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)


class FakeRunner:
    """systemctl/pgrep-Ersatz ohne Unit und ohne Prozess."""

    async def __call__(self, args):
        if args[:3] == ["systemctl", "--user", "cat"]:
            return 1, "", "no unit"
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return 3, "", ""
        if args[0] == "pgrep":
            return 1, "", ""
        return 0, "", ""


class FakeCommandSpawner:
    def __init__(self, on_start=None):
        self.aufrufe: list[tuple[list[str], str]] = []
        self._on_start = on_start

    def __call__(self, args, cwd):
        self.aufrufe.append((list(args), str(cwd)))
        if self._on_start is not None:
            self._on_start()


class FakeCommandRunner:
    def __init__(self, on_stop=None, rc=0, err=""):
        self.aufrufe: list[tuple[list[str], str]] = []
        self._on_stop = on_stop
        self.rc = rc
        self.err = err

    async def __call__(self, args, cwd):
        self.aufrufe.append((list(args), str(cwd)))
        if self._on_stop is not None:
            self._on_stop()
        return self.rc, "", self.err


class _RegistryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gantry-provider-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def provider(self, provider_id: str, **kwargs) -> None:
        kwargs.setdefault("name", provider_id.title())
        save_provider(Provider(id=provider_id, **kwargs), self.dir)

    def control(self, **kwargs) -> ServerControl:
        kwargs.setdefault("runner", FakeRunner())
        kwargs.setdefault("command_spawner", FakeCommandSpawner())
        kwargs.setdefault("command_runner", FakeCommandRunner())
        kwargs.setdefault("switch_poll_s", 0.01)
        return ServerControl.from_registry(
            self.dir,
            upstream_base="http://127.0.0.1:30001",
            repo_root=REPO,
            **kwargs,
        )


class TestProviderBackend(_RegistryCase):
    def test_host_port_health_und_settings_werden_uebernommen(self) -> None:
        self.provider(
            "coder", name="Coder", kind="custom", host="127.0.0.1",
            port=8080, model="coder-model",
            health_url="http://127.0.0.1:8080/health",
            workdir="runtime/coder",
            start_cmd=["serve.sh"], stop_cmd=["stop.sh"],
            settings={"auto_load": True, "idle_unload_min": 30,
                      "exclusive_gpu": True})
        backend = self.control().backends["coder"]
        self.assertEqual(backend.host, "127.0.0.1")
        self.assertEqual(backend.port, 8080)
        self.assertEqual(backend.base_url, "http://127.0.0.1:8080")
        self.assertEqual(backend.health_url, "http://127.0.0.1:8080/health")
        self.assertEqual(backend.health_path, "/health")
        self.assertEqual(backend.model, "coder-model")
        self.assertTrue(backend.auto_load)
        self.assertEqual(backend.idle_unload_min, 30)
        self.assertTrue(backend.exclusive_gpu)


class TestProviderHealth(_RegistryCase):
    async def test_health_auf_eigenem_port_ist_running_ohne_pidfile(self) -> None:
        server = HealthServer(200)
        self.addCleanup(server.stop)
        self.provider(
            "coder", host="127.0.0.1", port=server.port,
            health_url=f"http://127.0.0.1:{server.port}/health")
        control = self.control()
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["backend"], "coder")
        self.assertEqual(snap["backends"][0]["state"], "running")

        server.set_status(503)
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "stopped")
        self.assertIsNone(snap["backend"])
        self.assertEqual(snap["backends"][0]["state"], "stopped")

    async def test_health_auf_anderem_port_wird_nicht_verwechselt(self) -> None:
        server = HealthServer(200)
        self.addCleanup(server.stop)
        self.provider(
            "coder", host="127.0.0.1", port=server.port,
            health_url=f"http://127.0.0.1:{server.port}/health")
        andere = HealthServer(503)
        self.addCleanup(andere.stop)
        self.provider(
            "andere", host="127.0.0.1", port=andere.port,
            health_url=f"http://127.0.0.1:{andere.port}/health")
        snap = await self.control().snapshot()
        states = {b["name"]: b["state"] for b in snap["backends"]}
        self.assertEqual(states["coder"], "running")
        self.assertEqual(states["andere"], "stopped")


class TestProviderStartStop(_RegistryCase):
    async def test_start_wartet_auf_health_und_nutzt_workdir(self) -> None:
        server = HealthServer(503)
        self.addCleanup(server.stop)
        spawner = FakeCommandSpawner(
            on_start=lambda: server.set_status(200))
        self.provider(
            "coder", host="127.0.0.1", port=server.port,
            health_url=f"http://127.0.0.1:{server.port}/health",
            workdir="runtime/coder",
            start_cmd=["serve.sh", "--port", "8080"])
        control = self.control(command_spawner=spawner)
        snap = await control.start_and_wait("coder", timeout_s=1.0)
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["backend"], "coder")
        self.assertEqual(
            spawner.aufrufe,
            [(["serve.sh", "--port", "8080"],
              str(REPO / "runtime/coder"))])

    async def test_start_fehler_nennt_log_tail(self) -> None:
        server = HealthServer(503)
        self.addCleanup(server.stop)
        logfile = self.dir / "coder.log"
        logfile.write_text(
            "lade modell\nboom: keine gpu\n", encoding="utf-8")
        self.provider(
            "coder", host="127.0.0.1", port=server.port,
            health_url=f"http://127.0.0.1:{server.port}/health",
            logfile=str(logfile), start_cmd=["serve.sh"])
        control = self.control(start_timeout_s=0.05)
        with self.assertRaises(ServerControlError) as ctx:
            await control.start_and_wait("coder", timeout_s=0.05)
        self.assertIn("Log (letzte", str(ctx.exception))
        self.assertIn("boom: keine gpu", str(ctx.exception))

    async def test_stop_wartet_auf_health_und_nutzt_stop_cmd(self) -> None:
        server = HealthServer(200)
        self.addCleanup(server.stop)
        runner = FakeCommandRunner(
            on_stop=lambda: server.set_status(503))
        self.provider(
            "coder", host="127.0.0.1", port=server.port,
            health_url=f"http://127.0.0.1:{server.port}/health",
            workdir="/opt/coder",
            stop_cmd=["stop-coder"])
        control = self.control(command_runner=runner)
        self.assertEqual((await control.snapshot())["state"], "running")
        snap = await control.stop_and_wait("coder", timeout_s=1.0)
        self.assertEqual(snap["state"], "stopped")
        self.assertEqual(runner.aufrufe,
                         [(["stop-coder"], "/opt/coder")])

    async def test_refresh_liest_neue_provider_nach(self) -> None:
        self.provider("alt", start_cmd=["true"])
        control = self.control()
        self.assertEqual(control.backend_names, ("alt",))
        self.provider("neu", start_cmd=["neu-start"])
        self.assertTrue(control.refresh_from_registry())
        self.assertEqual(control.backend_names, ("alt", "neu"))
        self.assertEqual(control.backends["neu"].start_cmd, ["neu-start"])


class TestLogTail(unittest.TestCase):
    def test_tail_liest_nur_die_letzten_zeilen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.log"
            path.write_text("\n".join(f"zeile {i}" for i in range(100)),
                            encoding="utf-8")
            self.assertEqual(read_log_tail(path, 3),
                             ["zeile 97", "zeile 98", "zeile 99"])

    def test_fehlende_datei_ist_leer(self) -> None:
        self.assertEqual(read_log_tail("/gibt/es/nicht", 5), [])


class FakeClock:
    def __init__(self, jetzt: float = 1000.0):
        self.jetzt = jetzt

    def __call__(self) -> float:
        return self.jetzt


class FakeControl:
    """Minimale ServerControl-Attrappe fuer Router-Tests."""

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


class _RouterCase(_RegistryCase):
    def router(self, control, *, clock=None, config=None,
               **kwargs) -> ProviderRouter:
        return ProviderRouter(
            config or Config(), control,
            registry_dir=self.dir,
            clock=clock or FakeClock(),
            registry_ttl_s=0.0,
            **kwargs,
        )


class TestRouting(_RouterCase):
    def test_match_ueber_model_id_und_name(self) -> None:
        self.provider("coder", name="Coder Gross", model="coder-model",
                      port=8080)
        control = FakeControl({"coder": "stopped"})
        router = self.router(control)
        self.assertEqual(router.match("coder-model")[0].id, "coder")
        self.assertEqual(router.match("coder")[0].id, "coder")
        self.assertEqual(router.match("Coder Gross")[0].id, "coder")
        self.assertIsNone(router.match("unbekannt"))
        self.assertIsNone(router.match(""))

    async def test_prepare_startet_autoload_und_liefert_provider_port(
            self) -> None:
        self.provider("coder", name="Coder", model="coder-model",
                      host="127.0.0.1", port=8080, start_cmd=["true"],
                      settings={"auto_load": True})
        control = FakeControl({"coder": "stopped"})
        router = self.router(control)
        route = await router.prepare("coder")
        self.assertIsNotNone(route)
        self.assertEqual(route.base_url, "http://127.0.0.1:8080")
        self.assertEqual(control.start_calls, [("coder", 600.0)])

    async def test_autoload_aus_und_modellname_ist_503(self) -> None:
        self.provider("flint", name="Flint", model="flint-modell",
                      port=30001, settings={"auto_load": False})
        control = FakeControl({"flint": "stopped"})
        router = self.router(control)
        with self.assertRaises(ProviderUnavailable) as ctx:
            await router.prepare("flint-modell")
        self.assertEqual(ctx.exception.provider, "flint")
        self.assertEqual(control.start_calls, [])

    async def test_autoload_aus_und_id_ist_503_mit_log(self) -> None:
        self.provider("flint", name="Flint", model="flint-modell",
                      port=30001, settings={"auto_load": False})
        control = FakeControl({"flint": "stopped"})
        control.logs["flint"] = ["letzte zeile"]
        router = self.router(control)
        with self.assertRaises(ProviderUnavailable) as ctx:
            await router.prepare("flint")
        self.assertEqual(ctx.exception.provider, "flint")
        self.assertIn("auto_load", str(ctx.exception))
        self.assertEqual(ctx.exception.log_tail, ["letzte zeile"])

    async def test_globaler_alias_bleibt_beim_alten_pfad(self) -> None:
        self.provider("qwen", name="Qwen", model="alias-modell",
                      port=30001, settings={"auto_load": True})
        control = FakeControl({"qwen": "stopped"})
        router = self.router(control,
                             config=Config(model_alias="alias-modell"))
        self.assertIsNone(await router.prepare("alias-modell"))

    async def test_vram_guard_stoppt_exklusive_provider(self) -> None:
        self.provider("coder", name="Coder", model="coder-modell",
                      host="127.0.0.1", port=8080, start_cmd=["true"],
                      settings={"auto_load": True, "exclusive_gpu": True})
        self.provider("edge0", name="Edge0", model="edge0-120b",
                      host="127.0.0.1", port=30001,
                      settings={"auto_load": False, "exclusive_gpu": True})
        control = FakeControl({"coder": "stopped", "edge0": "running"})
        router = self.router(control)
        route = await router.prepare("coder")
        self.assertIsNotNone(route)
        self.assertEqual(control.stop_calls, [("edge0", 60.0)])
        self.assertEqual(control.start_calls, [("coder", 600.0)])

    async def test_startfehler_wird_zu_provider_unavailable(self) -> None:
        self.provider("coder", name="Coder", model="coder-modell",
                      host="127.0.0.1", port=8080,
                      settings={"auto_load": True})
        control = FakeControl({"coder": "stopped"})
        control.start_error = ServerControlError("kaputt")
        control.logs["coder"] = ["zeile"]
        router = self.router(control)
        with self.assertRaises(ProviderUnavailable) as ctx:
            await router.prepare("coder")
        self.assertIn("konnte nicht gestartet werden", str(ctx.exception))
        self.assertEqual(ctx.exception.log_tail, ["zeile"])


class TestIdleUnload(_RouterCase):
    async def test_idle_stoppt_nach_ruhezeit(self) -> None:
        self.provider("coder", name="Coder", model="coder-modell",
                      port=8080,
                      settings={"auto_load": True, "idle_unload_min": 1})
        clock = FakeClock()
        control = FakeControl({"coder": "running"})
        router = self.router(control, clock=clock)
        self.assertEqual(await router.idle_check_once(), [])
        clock.jetzt += 61
        self.assertEqual(await router.idle_check_once(), ["coder"])
        self.assertEqual(control.stop_calls, [("coder", 60.0)])

    async def test_idle_stoppt_nicht_waehrend_aktiver_requests(self) -> None:
        self.provider("coder", name="Coder", model="coder-modell",
                      port=8080,
                      settings={"auto_load": True, "idle_unload_min": 1})
        clock = FakeClock()
        control = FakeControl({"coder": "running"})
        router = self.router(control, clock=clock)
        await router.idle_check_once()          # startet die Ruheuhr
        router.acquire("coder")
        clock.jetzt += 61
        self.assertEqual(await router.idle_check_once(), [])
        self.assertEqual(control.stop_calls, [])
        router.release("coder")
        clock.jetzt += 61
        self.assertEqual(await router.idle_check_once(), ["coder"])

    async def test_idle_null_stoppt_nie(self) -> None:
        self.provider("coder", name="Coder", model="coder-modell",
                      port=8080,
                      settings={"auto_load": True, "idle_unload_min": 0})
        clock = FakeClock()
        control = FakeControl({"coder": "running"})
        router = self.router(control, clock=clock)
        await router.idle_check_once()
        clock.jetzt += 10_000
        self.assertEqual(await router.idle_check_once(), [])
        self.assertEqual(control.stop_calls, [])

    async def test_uptime_waechst_nur_im_running_zustand(self) -> None:
        self.provider("coder", port=8080)
        clock = FakeClock(1000.0)
        control = FakeControl({"coder": "stopped"})
        router = self.router(control, clock=clock)
        await router.status()
        self.assertEqual(router.uptime("coder"), 0)
        control.states["coder"] = "running"
        clock.jetzt = 1005.0
        await router.status()
        self.assertEqual(router.uptime("coder"), 0)  # Startzeit ist 1005
        clock.jetzt = 1010.0
        await router.status()
        self.assertEqual(router.uptime("coder"), 5)


if __name__ == "__main__":
    unittest.main()
