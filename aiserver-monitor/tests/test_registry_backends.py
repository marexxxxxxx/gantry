"""Tests fuer registry-getriebene Backends in serverctl.py.

Geprueft wird, dass ``ServerControl.from_registry`` die ``providers/*.toml``
in Backends uebersetzt und ``start_cmd``/``stop_cmd`` ueber die injizierten
Prozess-Helfer ausfuehrt - ohne echte Prozesse, systemd oder GPU.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiserver_monitor.config import Config
from aiserver_monitor.registry import Provider, save_provider
from aiserver_monitor.serverctl import (
    BACKEND_NAMES,
    ServerControl,
    ServerControlError,
)

REPO = Path("/repo")


class FakeRunner:
    """Antwortet wie systemctl/pgrep und zeichnet alle Aufrufe auf."""

    def __init__(self, *, unit_installiert: bool = False,
                 unit_aktiv: bool = False, prozess_laeuft: bool = False):
        self.unit_installiert = unit_installiert
        self.unit_aktiv = unit_aktiv
        self.prozess_laeuft = prozess_laeuft
        self.aufrufe: list[list[str]] = []

    async def __call__(self, args: list[str]):
        self.aufrufe.append(list(args))
        if args[:3] == ["systemctl", "--user", "cat"]:
            return (0, "", "") if self.unit_installiert else (1, "", "no unit")
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return ((0, "active\n", "") if self.unit_aktiv
                    else (3, "inactive\n", ""))
        if args[0] == "pgrep":
            return (0, "123\n", "") if self.prozess_laeuft else (1, "", "")
        return (0, "", "")


class FakeHealth:
    def __init__(self, ok: bool = False):
        self.ok = ok
        self.pfade: list[str] = []

    async def __call__(self, pfad: str) -> bool:
        self.pfade.append(pfad)
        return self.ok


class FakeSpawner:
    """Ersatz fuer den Skript-Spawner (start_script/stop_script)."""

    def __init__(self):
        self.aufrufe: list[list[str]] = []

    def __call__(self, args: list[str]) -> None:
        self.aufrufe.append(list(args))


class FakeCommandSpawner:
    """Ersatz fuer den detached Registry-Startbefehl."""

    def __init__(self):
        self.aufrufe: list[tuple[list[str], str]] = []

    def __call__(self, args: list[str], cwd: str) -> None:
        self.aufrufe.append((list(args), str(cwd)))


class FakeCommandRunner:
    """Ersatz fuer den blockierenden Registry-Stopbefehl."""

    def __init__(self, rc: int = 0, out: str = "", err: str = ""):
        self.rc = rc
        self.out = out
        self.err = err
        self.aufrufe: list[tuple[list[str], str]] = []

    async def __call__(self, args: list[str], cwd: str):
        self.aufrufe.append((list(args), str(cwd)))
        return self.rc, self.out, self.err


class _RegistryTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gantry-backends-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.runner = FakeRunner()
        self.health = FakeHealth(ok=False)
        self.spawner = FakeSpawner()
        self.command_spawner = FakeCommandSpawner()
        self.command_runner = FakeCommandRunner()

    def provider(self, provider_id: str, **kwargs) -> None:
        kwargs.setdefault("name", provider_id.title())
        save_provider(Provider(id=provider_id, **kwargs), self.dir)

    def control(self, **kwargs) -> ServerControl:
        return ServerControl.from_registry(
            self.dir,
            upstream_base="http://127.0.0.1:30001",
            repo_root=REPO,
            runner=self.runner,
            spawner=self.spawner,
            command_spawner=self.command_spawner,
            command_runner=self.command_runner,
            health_reader=self.health,
            **kwargs,
        )

    def pidfile(self, provider_id: str = "alpha") -> str:
        """Legt ein PID-File mit der eigenen PID an (Prozess gilt als aktiv)."""
        path = self.dir / "runtime" / provider_id / f"{provider_id}.pid"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))
        return str(path)


class TestBuildFromRegistry(_RegistryTestCase):
    def test_alle_felder_werden_uebernommen(self) -> None:
        self.provider(
            "alpha", name="Alpha", kind="custom",
            health_url="http://127.0.0.1:30001/status?voll=1",
            pidfile="runtime/alpha/alpha.pid",
            workdir="runtime/alpha",
            start_cmd=["runtime/alpha/start.sh", "--fast"],
            stop_cmd=["runtime/alpha/stop.sh"],
            settings={"scope_unit": "alpha.scope", "memory_max": "8G"},
        )
        control = self.control()
        alpha = control.backends["alpha"]
        self.assertEqual(alpha.name, "alpha")
        self.assertEqual(alpha.label, "Alpha")
        self.assertEqual(alpha.health_path, "/status")
        self.assertEqual(alpha.start_cmd,
                         ["runtime/alpha/start.sh", "--fast"])
        self.assertEqual(alpha.stop_cmd, ["runtime/alpha/stop.sh"])
        self.assertEqual(alpha.pidfile, "runtime/alpha/alpha.pid")
        self.assertEqual(alpha.workdir, "runtime/alpha")
        self.assertEqual(alpha.scope_unit, "alpha.scope")
        self.assertEqual(alpha.memory_max, "8G")

    def test_bekannte_namen_stehen_vor_neuen(self) -> None:
        # Alphabetisch waere abl/zeta; die UI soll aber qwen zuerst zeigen.
        self.provider("zeta", start_cmd=["true"])
        self.provider("abl", start_cmd=["true"])
        self.provider("qwen", kind="sglang", start_cmd=["true"])
        control = self.control()
        self.assertEqual(control.backend_names, ("qwen", "abl", "zeta"))

    def test_health_pfad_aus_url_oder_blossem_pfad(self) -> None:
        self.provider("olli", kind="ollama", health_url="")
        self.provider("kurz", health_url="healthz")
        self.provider("url", health_url="http://127.0.0.1:30001/health")
        control = self.control()
        self.assertEqual(control.backends["olli"].health_path, "/api/version")
        self.assertEqual(control.backends["kurz"].health_path, "/healthz")
        self.assertEqual(control.backends["url"].health_path, "/health")

    def test_sglang_unit_und_prozessmuster_werden_abgeleitet(self) -> None:
        self.provider(
            "qwen", kind="sglang", health_url="http://127.0.0.1:30001/health",
            start_cmd=["systemctl", "--user", "start", "escha-server.service"],
            stop_cmd=["systemctl", "--user", "stop", "escha-server.service"],
        )
        qwen = self.control().backends["qwen"]
        self.assertEqual(qwen.unit, "escha-server.service")
        self.assertEqual(qwen.process_pattern, "[s]glang.launch_server")

    def test_leere_registry_ergibt_keine_backends(self) -> None:
        control = self.control()
        self.assertEqual(control.backend_names, ())

    async def test_status_nutzt_registry_pidfile_und_health(self) -> None:
        self.provider("alpha", pidfile=self.pidfile(),
                      health_url="http://127.0.0.1:30001/status")
        self.health.ok = True
        control = self.control()
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["backend"], "alpha")
        self.assertIn("/status", self.health.pfade)


class TestRegistryStartStop(_RegistryTestCase):
    async def test_start_cmd_laeuft_detached_im_workdir(self) -> None:
        self.provider(
            "alpha", start_cmd=["runtime/alpha/start.sh", "--fast"],
            workdir="runtime/alpha")
        control = self.control()
        snap = await control.start("alpha")
        self.assertEqual(snap["state"], "starting")
        self.assertEqual(
            self.command_spawner.aufrufe,
            [(["runtime/alpha/start.sh", "--fast"],
              str(REPO / "runtime/alpha"))])
        self.assertEqual(self.spawner.aufrufe, [])
        self.assertNotIn(
            ["systemctl", "--user", "start", "--no-block",
             "escha-server.service"],
            self.runner.aufrufe)

    async def test_start_cmd_gewinnt_gegen_installierte_unit(self) -> None:
        self.provider(
            "qwen", kind="sglang", start_cmd=["/opt/qwen/start"],
            stop_cmd=["/opt/qwen/stop"],
            health_url="http://127.0.0.1:30001/health")
        self.runner.unit_installiert = True
        control = self.control()
        await control.start("qwen")
        self.assertEqual(self.command_spawner.aufrufe,
                         [(["/opt/qwen/start"], str(REPO))])
        self.assertNotIn(
            ["systemctl", "--user", "start", "--no-block",
             "escha-server.service"],
            self.runner.aufrufe)

    async def test_scope_umhuellt_registry_start_cmd(self) -> None:
        self.provider(
            "alpha", start_cmd=["runtime/alpha/start.sh"],
            settings={"scope_unit": "alpha.scope", "memory_max": "8G"})
        control = self.control()
        await control.start("alpha")
        self.assertEqual(
            self.command_spawner.aufrufe[0][0],
            ["systemd-run", "--user", "--scope", "--collect",
             "--unit", "alpha.scope",
             "--setenv", "EDGE0_FOREGROUND=1",
             "--property", "MemoryMax=8G",
             "runtime/alpha/start.sh"])

    async def test_stop_cmd_laeuft_blockierend_im_workdir(self) -> None:
        self.provider(
            "alpha", pidfile=self.pidfile(),
            workdir="runtime/alpha",
            stop_cmd=["runtime/alpha/stop.sh"])
        self.health.ok = True
        control = self.control()
        snap = await control.stop("alpha")
        self.assertEqual(snap["state"], "stopping")
        self.assertEqual(
            self.command_runner.aufrufe,
            [(["runtime/alpha/stop.sh"], str(REPO / "runtime/alpha"))])
        self.assertEqual(self.spawner.aufrufe, [])

    async def test_stop_cmd_stoppt_vorher_den_scope(self) -> None:
        self.provider(
            "alpha", pidfile=self.pidfile(),
            stop_cmd=["runtime/alpha/stop.sh"],
            settings={"scope_unit": "alpha.scope"})
        self.health.ok = True
        control = self.control()
        await control.stop("alpha")
        self.assertIn(["systemctl", "--user", "stop", "alpha.scope"],
                      self.runner.aufrufe)
        self.assertEqual(len(self.command_runner.aufrufe), 1)

    async def test_stop_cmd_fehler_setzt_fehlertext(self) -> None:
        self.provider(
            "alpha", pidfile=self.pidfile(),
            stop_cmd=["runtime/alpha/stop.sh"])
        self.health.ok = True
        self.command_runner.rc = 1
        self.command_runner.err = "stop kaputt"
        control = self.control()
        with self.assertRaises(ServerControlError):
            await control.stop("alpha")
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertIn("stop kaputt", snap["error"])

    async def test_start_ohne_befehl_ist_fehler(self) -> None:
        self.provider("alpha")
        control = self.control()
        with self.assertRaises(ServerControlError):
            await control.start("alpha")
        snap = await control.snapshot()
        self.assertIn("kein Startbefehl", snap["error"])

    async def test_stop_ohne_befehl_ist_fehler(self) -> None:
        self.provider("alpha", pidfile=self.pidfile())
        self.health.ok = True
        control = self.control()
        with self.assertRaises(ServerControlError):
            await control.stop("alpha")
        snap = await control.snapshot()
        self.assertIn("kein Stopbefehl", snap["error"])

    async def test_unbekanntes_backend_ist_fehler(self) -> None:
        self.provider("alpha", start_cmd=["true"])
        control = self.control()
        with self.assertRaises(ValueError):
            await control.start("nope")
        self.assertEqual(self.command_spawner.aufrufe, [])


class TestFromConfigFallback(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gantry-config-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_ohne_providers_bleiben_die_eingebauten_backends(self) -> None:
        with mock.patch.dict(os.environ,
                             {"GANTRY_PROVIDERS_DIR": str(self.dir)}):
            control = ServerControl.from_config(Config())
        self.assertEqual(control.backend_names, BACKEND_NAMES)
        self.assertEqual(control.backends["flint"].pidfile,
                         "runtime/flint/flint.pid")
        self.assertEqual(control.backends["edge0"].scope_unit,
                         "edge0-backend.scope")

    def test_registry_hat_vorrang_vor_den_eingebauten(self) -> None:
        save_provider(Provider(id="alpha", name="Alpha",
                               start_cmd=["true"]), self.dir)
        with mock.patch.dict(os.environ,
                             {"GANTRY_PROVIDERS_DIR": str(self.dir)}):
            control = ServerControl.from_config(Config())
        self.assertEqual(control.backend_names, ("alpha",))
        self.assertEqual(control.backends["alpha"].start_cmd, ["true"])

    def test_kaputte_registry_faellt_auf_die_eingebauten_zurueck(self) -> None:
        (self.dir / "kaputt.toml").write_text("id = [\n", encoding="utf-8")
        with mock.patch.dict(os.environ,
                             {"GANTRY_PROVIDERS_DIR": str(self.dir)}):
            control = ServerControl.from_config(Config())
        self.assertEqual(control.backend_names, BACKEND_NAMES)


if __name__ == "__main__":
    unittest.main()
