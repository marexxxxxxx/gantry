"""Tests fuer die Auslieferung des neuen Gantry-Dashboards.

Der Fokus liegt auf der Verzahnung von Static-Mount, Dashboard-Route und den
unveraenderten Alt-Routen; die Provider-API selbst testet ``test_webapi``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from aiserver_monitor.app import create_app
from aiserver_monitor.autoload import ProviderRouter
from aiserver_monitor.config import Config


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


class DashboardCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gantry-dashboard-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "providers"
        self.dir.mkdir()
        self.db = Path(self._tmp.name) / "monitor.db"

    def app(self):
        control = _Control()
        router = ProviderRouter(
            Config(db_path=str(self.db)), control,
            registry_dir=self.dir, registry_ttl_s=0.0)
        return create_app(
            Config(db_path=str(self.db)), server_control=control,
            provider_router=router, enable_background=False)


class TestDashboardRoute(DashboardCase):
    def test_wurzel_liefert_das_neue_dashboard(self) -> None:
        with TestClient(self.app()) as client:
            antwort = client.get("/")
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("text/html", antwort.headers["content-type"])
        self.assertIn("Gantry", antwort.text)
        self.assertIn("Local AI Runtime", antwort.text)
        self.assertIn("/dashboard.css", antwort.text)
        self.assertIn("/dashboard.js", antwort.text)
        # Das alte Vite-Bundle wird nicht mehr referenziert.
        self.assertNotIn("/assets/index-", antwort.text)

    def test_tab_leiste_hat_drei_ansichten(self) -> None:
        with TestClient(self.app()) as client:
            antwort = client.get("/")
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("role=\"tablist\"", antwort.text)
        for ansicht in ("Runtime", "Priorität", "Queue"):
            self.assertIn(ansicht, antwort.text)
        for panel in ("view-runtime", "view-priority", "view-queue"):
            self.assertIn(f'id="{panel}"', antwort.text)
        # Die Deep-Link-Panels sind im Markup vorhanden und an die Tabs gebunden.
        self.assertEqual(antwort.text.count('role="tab"'), 3)

    def test_dashboard_js_referenziert_steuer_und_queue_endpunkte(self) -> None:
        with TestClient(self.app()) as client:
            js = client.get("/dashboard.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("/monitor/priority", js.text)
        self.assertIn("/monitor/hindsight", js.text)
        self.assertIn("/monitor/jobs", js.text)
        # Der Deep-Link wird ueber location.hash aufgeloest.
        self.assertIn("hashchange", js.text)
        # full_prompt wird bewusst nie in die UI uebernommen.
        self.assertNotIn("full_prompt", js.text)

    def test_dashboard_assets_werden_ausgeliefert(self) -> None:
        with TestClient(self.app()) as client:
            css = client.get("/dashboard.css")
            js = client.get("/dashboard.js")
        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.headers["content-type"])
        self.assertIn("--accent", css.text)
        self.assertEqual(js.status_code, 200)
        self.assertIn("javascript", js.headers["content-type"])
        self.assertIn("loadProviders", js.text)

    def test_alte_routen_bleiben_erreichbar(self) -> None:
        with TestClient(self.app()) as client:
            health = client.get("/healthz")
            jobs = client.get("/monitor/jobs")
            provider = client.get("/api/providers")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertEqual(jobs.status_code, 200)
        self.assertEqual(jobs.json(), {"jobs": []})
        self.assertEqual(provider.status_code, 200)
        self.assertEqual(provider.json(), [])

    def test_info_liefert_name_version_und_uptime(self) -> None:
        with TestClient(self.app()) as client:
            antwort = client.get("/api/info")
        self.assertEqual(antwort.status_code, 200)
        daten = antwort.json()
        self.assertEqual(daten["name"], "Gantry")
        self.assertIsInstance(daten["version"], str)
        self.assertTrue(daten["version"])
        self.assertGreaterEqual(daten["uptime_seconds"], 0)
        self.assertEqual(daten["providers"], 0)


if __name__ == "__main__":
    unittest.main()
