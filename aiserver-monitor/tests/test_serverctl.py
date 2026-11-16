"""Tests fuer die SGLang-Server-Steuerung (serverctl.py).

Der echte Server wird nie angefasst: ``runner`` ersetzt systemctl/pgrep,
``spawner`` die Start-/Stop-Skripte, ``health_reader`` den /health-Aufruf.
Geprueft wird, welchen Weg die Steuerung waehlt und welchen Zustand sie
daraus ableitet.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from aiserver_monitor.serverctl import (
    ServerConflictError,
    ServerControl,
    ServerControlError,
)

UNIT = "escha-server.service"
REPO = Path("/repo")


class FakeRunner:
    """Antwortet wie systemctl/pgrep; merkt sich alle Aufrufe."""

    def __init__(self, *, unit_installiert=True, unit_aktiv=False,
                 prozess_laeuft=False, start_rc=0, stop_rc=0):
        self.unit_installiert = unit_installiert
        self.unit_aktiv = unit_aktiv
        self.prozess_laeuft = prozess_laeuft
        self.start_rc = start_rc
        self.stop_rc = stop_rc
        self.aufrufe: list[list[str]] = []

    async def __call__(self, args: list[str]):
        self.aufrufe.append(list(args))
        prefix = args[:3]
        if prefix == ["systemctl", "--user", "cat"]:
            return (0, "", "") if self.unit_installiert else (1, "", "no unit")
        if prefix == ["systemctl", "--user", "is-active"]:
            return (0, "active\n", "") if self.unit_aktiv else (3, "inactive\n", "")
        if prefix == ["systemctl", "--user", "start"]:
            return (self.start_rc, "",
                    "" if self.start_rc == 0 else "start kaputt")
        if prefix == ["systemctl", "--user", "stop"]:
            return (self.stop_rc, "",
                    "" if self.stop_rc == 0 else "stop kaputt")
        if args[0] == "pgrep":
            return (0, "123\n", "") if self.prozess_laeuft else (1, "", "")
        return (0, "", "")

    def kommandos(self) -> list[list[str]]:
        return self.aufrufe


class FakeHealth:
    def __init__(self, ok: bool = False, fehler: Exception | None = None):
        self.ok = ok
        self.fehler = fehler
        self.aufrufe = 0
        self.pfade: list[str] = []

    async def __call__(self, pfad: str = "") -> bool:
        self.aufrufe += 1
        self.pfade.append(pfad)
        if self.fehler is not None:
            raise self.fehler
        return self.ok


class FakeHealthProPfad:
    """Health-Antwort je Backend-Pfad (``/health`` vs. ``/api/version``)."""

    def __init__(self, **pfade: bool):
        self.ok = pfade
        self.pfade: list[str] = []

    async def __call__(self, pfad: str) -> bool:
        self.pfade.append(pfad)
        return self.ok.get(pfad, False)


class FakeSpawner:
    def __init__(self):
        self.aufrufe: list[list[str]] = []

    def __call__(self, args: list[str]) -> None:
        self.aufrufe.append(list(args))


class FakeClock:
    def __init__(self):
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


def _control(*, runner=None, health=None, spawner=None, clock=None,
             unit=UNIT, start_timeout_s=600.0, stop_timeout_s=60.0,
             start_grace_s=10.0, flint_pidfile=None, gptoss_pidfile=None,
             abl_pidfile=None, sleeper=None):
    runner = FakeRunner() if runner is None else runner
    health = FakeHealth() if health is None else health
    spawner = FakeSpawner() if spawner is None else spawner
    clock = FakeClock() if clock is None else clock
    control = ServerControl(
        upstream_base="http://127.0.0.1:30001",
        repo_root=REPO,
        unit=unit,
        start_script="runtime/sglang/start-penalty.sh",
        stop_script="runtime/sglang/stop-server.sh",
        process_pattern="[s]glang.launch_server",
        flint_start_script="runtime/flint/start-flint.sh",
        flint_stop_script="runtime/flint/stop-flint.sh",
        flint_pidfile=flint_pidfile or "runtime/flint/flint.pid",
        flint_health_path="/api/version",
        gptoss_start_script="runtime/gptoss/start-gptoss.sh",
        gptoss_stop_script="runtime/gptoss/stop-gptoss.sh",
        gptoss_pidfile=gptoss_pidfile or "runtime/gptoss/gptoss.pid",
        abl_start_script="runtime/abl/start-abl.sh",
        abl_stop_script="runtime/abl/stop-abl.sh",
        abl_pidfile=abl_pidfile or "runtime/abl/abl.pid",
        runner=runner,
        spawner=spawner,
        health_reader=health,
        clock=clock,
        sleeper=sleeper,
        start_timeout_s=start_timeout_s,
        stop_timeout_s=stop_timeout_s,
        start_grace_s=start_grace_s,
    )
    return control, runner, health, spawner, clock


class TestStatus(unittest.IsolatedAsyncioTestCase):
    async def test_ohne_unit_und_ohne_prozess_ist_gestoppt(self):
        runner = FakeRunner(unit_installiert=False, prozess_laeuft=False)
        control, _, _, _, _ = _control(runner=runner)
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "stopped")
        self.assertFalse(snap["managed_by_systemd"])
        self.assertEqual(snap["unit"], UNIT)
        self.assertIsNone(snap["error"])

    async def test_laufender_prozess_mit_health_ist_running(self):
        runner = FakeRunner(unit_installiert=False, prozess_laeuft=True)
        health = FakeHealth(ok=True)
        control, _, _, _, _ = _control(runner=runner, health=health)
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertFalse(snap["managed_by_systemd"])

    async def test_laufender_prozess_ohne_health_ist_starting(self):
        runner = FakeRunner(unit_installiert=False, prozess_laeuft=True)
        control, _, _, _, _ = _control(runner=runner, health=FakeHealth(ok=False))
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "starting")

    async def test_aktive_unit_mit_health_ist_running(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=True,
                            prozess_laeuft=True)
        control, _, _, _, _ = _control(runner=runner, health=FakeHealth(ok=True))
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertTrue(snap["managed_by_systemd"])

    async def test_manueller_prozess_trotz_installierter_unit_zaehlt(self):
        # Unit installiert, aber nicht aktiv; der Server wurde von Hand
        # gestartet. Er laeuft trotzdem und darf nicht als gestoppt gelten.
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=True)
        control, _, _, _, _ = _control(runner=runner, health=FakeHealth(ok=True))
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")

    async def test_health_fehler_macht_status_nicht_kaputt(self):
        runner = FakeRunner(unit_installiert=False, prozess_laeuft=True)
        health = FakeHealth(fehler=RuntimeError("health kaputt"))
        control, _, _, _, _ = _control(runner=runner, health=health)
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "starting")


class TestStartStop(unittest.IsolatedAsyncioTestCase):
    async def test_start_mit_unit_nutzt_systemctl_no_block(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False)
        health = FakeHealth(ok=False)
        control, _, _, spawner, _ = _control(runner=runner, health=health)
        snap = await control.start()
        self.assertEqual(snap["state"], "starting")
        self.assertIn(
            ["systemctl", "--user", "start", "--no-block", UNIT],
            runner.kommandos())
        self.assertEqual(spawner.aufrufe, [])

        runner.unit_aktiv = True
        runner.prozess_laeuft = True
        health.ok = True
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")

    async def test_stop_mit_aktiver_unit_nutzt_systemctl_no_block(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=True,
                            prozess_laeuft=True)
        control, _, health, _, _ = _control(runner=runner,
                                            health=FakeHealth(ok=True))
        snap = await control.stop()
        self.assertEqual(snap["state"], "stopping")
        self.assertIn(
            ["systemctl", "--user", "stop", "--no-block", UNIT],
            runner.kommandos())

        runner.unit_aktiv = False
        runner.prozess_laeuft = False
        health.ok = False
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "stopped")

    async def test_start_ohne_unit_nutzt_start_skript(self):
        runner = FakeRunner(unit_installiert=False, prozess_laeuft=False)
        control, _, _, spawner, _ = _control(runner=runner)
        snap = await control.start()
        self.assertEqual(snap["state"], "starting")
        self.assertEqual(
            spawner.aufrufe,
            [["bash", str(REPO / "runtime/sglang/start-penalty.sh")]])

    async def test_stop_ohne_unit_nutzt_stop_skript(self):
        runner = FakeRunner(unit_installiert=False, prozess_laeuft=True)
        control, _, _, spawner, _ = _control(runner=runner,
                                             health=FakeHealth(ok=True))
        snap = await control.stop()
        self.assertEqual(snap["state"], "stopping")
        self.assertEqual(
            spawner.aufrufe,
            [["bash", str(REPO / "runtime/sglang/stop-server.sh")]])

    async def test_stop_bei_manuellen_prozess_und_inaktiver_unit_nutzt_skript(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=True)
        control, _, _, spawner, _ = _control(runner=runner,
                                             health=FakeHealth(ok=True))
        await control.stop()
        self.assertEqual(
            spawner.aufrufe,
            [["bash", str(REPO / "runtime/sglang/stop-server.sh")]])

    async def test_start_bei_laufendem_server_ist_no_op(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=True,
                            prozess_laeuft=True)
        control, _, _, _, _ = _control(runner=runner,
                                       health=FakeHealth(ok=True))
        snap = await control.start()
        self.assertEqual(snap["state"], "running")
        self.assertNotIn(
            ["systemctl", "--user", "start", "--no-block", UNIT],
            runner.kommandos())

    async def test_stop_bei_gestopptem_server_ist_no_op(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False)
        control, _, _, spawner, _ = _control(runner=runner)
        snap = await control.stop()
        self.assertEqual(snap["state"], "stopped")
        self.assertEqual(spawner.aufrufe, [])

    async def test_zweiter_start_waehrend_start_ist_konflikt(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False)
        control, _, _, _, _ = _control(runner=runner)
        await control.start()
        with self.assertRaises(ServerConflictError):
            await control.start()
        with self.assertRaises(ServerConflictError):
            await control.stop()

    async def test_start_fehler_setzt_fehlertext_und_hebt_uebergang_auf(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False, start_rc=1)
        control, _, _, _, _ = _control(runner=runner)
        with self.assertRaises(ServerControlError):
            await control.start()
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "stopped")
        self.assertIn("start kaputt", snap["error"])

    async def test_start_ohne_prozess_bleibt_kurz_starting(self):
        # systemctl --no-block kehrt zurueck, bevor der Prozess da ist.
        # Innerhalb der Grace-Zeit darf das kein Fehler sein.
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False)
        clock = FakeClock()
        control, _, _, _, _ = _control(runner=runner, clock=clock,
                                       start_grace_s=10.0)
        await control.start()
        clock.jetzt += 5.0
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "starting")
        self.assertIsNone(snap["error"])

        clock.jetzt += 6.0
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "stopped")
        self.assertIsNotNone(snap["error"])

    async def test_stopp_bleibt_nicht_ewig_haengen(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=True,
                            prozess_laeuft=True)
        clock = FakeClock()
        control, _, health, _, _ = _control(runner=runner, clock=clock,
                                            health=FakeHealth(ok=True),
                                            stop_timeout_s=30.0)
        await control.stop()
        clock.jetzt += 31.0
        snap = await control.snapshot()
        self.assertNotEqual(snap["state"], "stopping")
        self.assertEqual(snap["state"], "running")
        self.assertIsNotNone(snap["error"])


class TestBackends(unittest.IsolatedAsyncioTestCase):
    """Qwen (SGLang), Flint, GPT-OSS und Abliterated (Ollama) teilen einen Port."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="serverctl-flint-")
        self.addCleanup(self.tmp.cleanup)
        self.pidfile = Path(self.tmp.name) / "flint.pid"

    def _flint_an(self) -> None:
        self.pidfile.write_text(str(os.getpid()))

    def _flint_aus(self) -> None:
        self.pidfile.unlink(missing_ok=True)

    async def test_gestoppt_listet_beide_backends(self):
        control, _, _, _, _ = _control(flint_pidfile=self.pidfile)
        snap = await control.snapshot()
        self.assertIsNone(snap["backend"])
        self.assertEqual([b["name"] for b in snap["backends"]],
                         ["qwen", "flint", "gptoss", "abl", "edge0"])
        self.assertEqual([b["label"] for b in snap["backends"]],
                         ["Qwen (SGLang)", "Flint (Ollama)",
                          "GPT-OSS 20B (Ollama)",
                          "Qwen3-14B Abliterated (Ollama)",
                          "Edge0 120B (NVFP4)"])
        self.assertEqual([b["state"] for b in snap["backends"]],
                         ["stopped", "stopped", "stopped", "stopped",
                          "stopped"])

    async def test_laufender_flint_wird_erkannt(self):
        self._flint_an()
        health = FakeHealthProPfad(**{"/api/version": True})
        control, _, _, _, _ = _control(flint_pidfile=self.pidfile,
                                       health=health)
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["backend"], "flint")
        self.assertEqual(snap["backends"][1]["state"], "running")
        self.assertIn("/api/version", health.pfade)

    async def test_health_pfad_je_backend(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=True,
                            prozess_laeuft=True)
        health = FakeHealthProPfad(**{"/health": True, "/api/version": False})
        control, _, _, _, _ = _control(runner=runner, health=health,
                                       flint_pidfile=self.pidfile)
        snap = await control.snapshot()
        self.assertEqual(snap["backend"], "qwen")
        self.assertIn("/health", health.pfade)

    async def test_start_flint_nutzt_start_skript(self):
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False)
        health = FakeHealthProPfad(**{"/api/version": False})
        control, _, _, spawner, _ = _control(runner=runner, health=health,
                                             flint_pidfile=self.pidfile)
        snap = await control.start("flint")
        self.assertEqual(snap["state"], "starting")
        self.assertEqual(snap["backend"], "flint")
        self.assertEqual(
            spawner.aufrufe,
            [["bash", str(REPO / "runtime/flint/start-flint.sh")]])

        self._flint_an()
        health.ok["/api/version"] = True
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["backend"], "flint")

    async def test_stop_flint_nutzt_stop_skript(self):
        self._flint_an()
        health = FakeHealthProPfad(**{"/api/version": True})
        control, _, _, spawner, _ = _control(flint_pidfile=self.pidfile,
                                             health=health)
        snap = await control.stop()
        self.assertEqual(snap["state"], "stopping")
        self.assertEqual(
            spawner.aufrufe,
            [["bash", str(REPO / "runtime/flint/stop-flint.sh")]])

        self._flint_aus()
        health.ok["/api/version"] = False
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "stopped")
        self.assertIsNone(snap["backend"])

    async def test_wechsel_stoppt_flint_vor_dem_qwen_start(self):
        self._flint_an()
        health = FakeHealthProPfad(**{"/api/version": True})
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False)

        async def sleeper(_sekunden: float) -> None:
            # Beim ersten Poll ist der Stop durch: Flint ist verschwunden.
            self._flint_aus()
            health.ok["/api/version"] = False

        control, _, _, spawner, _ = _control(
            runner=runner, health=health, flint_pidfile=self.pidfile,
            sleeper=sleeper)
        snap = await control.start("qwen")
        self.assertEqual(snap["state"], "starting")
        self.assertEqual(snap["backend"], "qwen")

        for _ in range(100):
            await asyncio.sleep(0)
            if ["systemctl", "--user", "start", "--no-block", UNIT] \
                    in runner.kommandos():
                break
        self.assertIn(["bash", str(REPO / "runtime/flint/stop-flint.sh")],
                      spawner.aufrufe)
        self.assertIn(["systemctl", "--user", "start", "--no-block", UNIT],
                      runner.kommandos())

        runner.unit_aktiv = True
        runner.prozess_laeuft = True
        health.ok["/health"] = True
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["backend"], "qwen")

    async def test_start_gptoss_nutzt_eigenes_skript(self):
        gptoss = Path(self.tmp.name) / "gptoss.pid"
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False)
        health = FakeHealthProPfad(**{"/api/version": False})
        control, _, _, spawner, _ = _control(
            runner=runner, health=health, gptoss_pidfile=gptoss)
        snap = await control.start("gptoss")
        self.assertEqual(snap["state"], "starting")
        self.assertEqual(snap["backend"], "gptoss")
        self.assertEqual(
            spawner.aufrufe,
            [["bash", str(REPO / "runtime/gptoss/start-gptoss.sh")]])

        gptoss.write_text(str(os.getpid()))
        health.ok["/api/version"] = True
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["backend"], "gptoss")

    async def test_start_abl_nutzt_eigenes_skript(self):
        abl = Path(self.tmp.name) / "abl.pid"
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False)
        health = FakeHealthProPfad(**{"/api/version": False})
        control, _, _, spawner, _ = _control(
            runner=runner, health=health, abl_pidfile=abl)
        snap = await control.start("abl")
        self.assertEqual(snap["state"], "starting")
        self.assertEqual(snap["backend"], "abl")
        self.assertEqual(
            spawner.aufrufe,
            [["bash", str(REPO / "runtime/abl/start-abl.sh")]])

        abl.write_text(str(os.getpid()))
        health.ok["/api/version"] = True
        snap = await control.snapshot()
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["backend"], "abl")

    async def test_wechsel_flint_zu_gptoss(self):
        gptoss = Path(self.tmp.name) / "gptoss.pid"
        self._flint_an()
        health = FakeHealthProPfad(**{"/api/version": True})
        runner = FakeRunner(unit_installiert=True, unit_aktiv=False,
                            prozess_laeuft=False)

        async def sleeper(_sekunden: float) -> None:
            self._flint_aus()
            health.ok["/api/version"] = False

        control, _, _, spawner, _ = _control(
            runner=runner, health=health, flint_pidfile=self.pidfile,
            gptoss_pidfile=gptoss, sleeper=sleeper)
        snap = await control.start("gptoss")
        self.assertEqual(snap["state"], "starting")
        self.assertEqual(snap["backend"], "gptoss")

        for _ in range(100):
            await asyncio.sleep(0)
            if ["bash", str(REPO / "runtime/gptoss/start-gptoss.sh")] \
                    in spawner.aufrufe:
                break
        self.assertIn(["bash", str(REPO / "runtime/flint/stop-flint.sh")],
                      spawner.aufrufe)
        self.assertIn(
            ["bash", str(REPO / "runtime/gptoss/start-gptoss.sh")],
            spawner.aufrufe)

    async def test_unbekanntes_backend_ist_fehler(self):
        control, _, _, spawner, _ = _control(flint_pidfile=self.pidfile)
        with self.assertRaises(ValueError):
            await control.start("llama")
        self.assertEqual(spawner.aufrufe, [])


if __name__ == "__main__":
    unittest.main()
