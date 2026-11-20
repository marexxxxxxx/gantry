"""Tests fuer das Aufraeumen der Queue (Entfernen terminaler Jobs).

Deckt die ``JobRegistry.clear_terminal``-Methode und die Route
``POST /monitor/jobs/clear`` ab: nur terminale Jobs (done/aborted/error)
verschwinden, aktive Jobs (waiting/running) bleiben unangetastet.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from aiserver_monitor.app import create_app
from aiserver_monitor.config import Config
from aiserver_monitor.jobs import JobRegistry, JobStatus


class _Control:
    """Minimale ServerControl-Attrappe ohne Start-/Stop-Aufrufe."""

    backends: dict[str, object] = {}
    backend_names: tuple[str, ...] = ()

    async def snapshot(self) -> dict:
        return {
            "state": "stopped",
            "backend": None,
            "backends": [],
            "unit": None,
            "managed_by_systemd": False,
            "upstream": "http://127.0.0.1:30001",
            "error": None,
        }

    def refresh_from_registry(self, directory=None) -> bool:
        return True

    def log_lines(self, backend, lines=200) -> list[str]:
        return []


class ClearCase(unittest.TestCase):
    """Temp-DB je Test plus App-Bau (Background und Server-Steuerung aus)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gantry-clear-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "monitor.db"

    def app(self):
        control = _Control()
        return create_app(
            Config(db_path=str(self.db)), server_control=control,
            enable_background=False)


class TestClearTerminalUnit(unittest.TestCase):
    """``clear_terminal`` auf der Registry: nur terminale Jobs fallen weg."""

    def test_entfernt_nur_terminale_jobs(self):
        reg = JobRegistry(preview_max_chars=5)
        reg.register("done", source="s", model="m", full_prompt="p", now=1.0)
        reg.finish("done", JobStatus.DONE, now=2.0)
        reg.register("error", source="s", model="m", full_prompt="p", now=3.0)
        reg.finish("error", JobStatus.ERROR, error="kaputt", now=4.0)
        reg.register("aborted", source="s", model="m", full_prompt="p", now=5.0)
        reg.finish("aborted", JobStatus.ABORTED, now=6.0)
        reg.register("waiting", source="s", model="m", full_prompt="p", now=7.0)
        reg.register("running", source="s", model="m", full_prompt="p", now=8.0)
        reg.note_running("running", now=9.0)

        entfernt = reg.clear_terminal()

        self.assertEqual(entfernt, 3)
        self.assertIsNone(reg.get("done"))
        self.assertIsNone(reg.get("error"))
        self.assertIsNone(reg.get("aborted"))
        self.assertIsNotNone(reg.get("waiting"))
        self.assertIsNotNone(reg.get("running"))
        self.assertIs(reg.get("waiting").status, JobStatus.WAITING)
        self.assertIs(reg.get("running").status, JobStatus.RUNNING)

    def test_ohne_terminale_jobs_bleibt_alles(self):
        reg = JobRegistry(preview_max_chars=5)
        reg.register("w1", source="s", model="m", full_prompt="p", now=1.0)
        reg.register("w2", source="s", model="m", full_prompt="p", now=2.0)
        reg.note_running("w2", now=3.0)

        self.assertEqual(reg.clear_terminal(), 0)
        self.assertEqual(len(reg.snapshot()), 2)

    def test_leere_registry_liefert_null(self):
        reg = JobRegistry(preview_max_chars=5)
        self.assertEqual(reg.clear_terminal(), 0)


class TestClearRoute(ClearCase):
    """``POST /monitor/jobs/clear`` entfernt terminale Jobs, behaelt aktive."""

    def test_clear_entfernt_terminale_und_behaelt_aktive(self):
        app = self.app()
        reg = app.state.deps.registry
        reg.register("done", source="s", model="m", full_prompt="p", now=1.0)
        reg.finish("done", JobStatus.DONE, now=2.0)
        reg.register("error", source="s", model="m", full_prompt="p", now=3.0)
        reg.finish("error", JobStatus.ERROR, error="x", now=4.0)
        reg.register("waiting", source="s", model="m", full_prompt="p", now=5.0)
        reg.register("running", source="s", model="m", full_prompt="p", now=6.0)
        reg.note_running("running", now=7.0)

        with TestClient(app) as client:
            antwort = client.post("/monitor/jobs/clear")

        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {"removed": 2})
        self.assertIsNone(reg.get("done"))
        self.assertIsNone(reg.get("error"))
        self.assertIsNotNone(reg.get("waiting"))
        self.assertIsNotNone(reg.get("running"))

    def test_clear_ohne_terminale_jobs_ist_noop(self):
        app = self.app()
        reg = app.state.deps.registry
        reg.register("waiting", source="s", model="m", full_prompt="p", now=1.0)
        reg.register("running", source="s", model="m", full_prompt="p", now=2.0)
        reg.note_running("running", now=3.0)

        with TestClient(app) as client:
            antwort = client.post("/monitor/jobs/clear")

        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {"removed": 0})
        self.assertEqual(len(reg.snapshot()), 2)

    def test_snapshot_nach_clear_zeigt_nur_aktive(self):
        app = self.app()
        reg = app.state.deps.registry
        reg.register("done", source="s", model="m", full_prompt="p", now=1.0)
        reg.finish("done", JobStatus.DONE, now=2.0)
        reg.register("waiting", source="s", model="m", full_prompt="p", now=3.0)

        with TestClient(app) as client:
            client.post("/monitor/jobs/clear")
            jobs = client.get("/monitor/jobs")

        self.assertEqual(jobs.status_code, 200)
        rids = [job["rid"] for job in jobs.json()["jobs"]]
        self.assertEqual(rids, ["waiting"])


if __name__ == "__main__":
    unittest.main()
