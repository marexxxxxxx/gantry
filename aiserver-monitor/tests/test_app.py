"""Tests fuer die App-Verdrahtung (app.py): Routen, Lifespan, Client-IP.

Die meisten Faelle laufen ueber den TestClient (damit laeuft die Lifespan).
Der Upstream ist immer ein injizierter Transport; es geht nie ein Socket nach
127.0.0.1:30001. Nur der SSE-Integrationstest braucht echte Sockets: httpx
puffert ueber den ASGITransport unendliche Streams, deshalb laeuft dort ein
echter uvicorn auf 127.0.0.1:39127 in einem Hintergrund-Thread.
"""

import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
import uvicorn
from fastapi.testclient import TestClient

import fakes
from aiserver_monitor.app import _CfClientMiddleware, client_source, create_app
from aiserver_monitor.config import Config
from aiserver_monitor.hindsight import DEFAULT_HINDSIGHT_MARKERS
from aiserver_monitor.history import History
from aiserver_monitor.jobs import JobStatus
from aiserver_monitor.lifecycle import (
    LifecycleConflictError,
    LifecycleDisabledError,
    LifecycleError,
    LifecycleTimeoutError,
    LifecycleUnavailableError,
    UnknownProfileError,
)
from aiserver_monitor.metrics_parse import GpuSample
from aiserver_monitor.priority import DEFAULT_MARKER
from aiserver_monitor.proxy import Deps
from aiserver_monitor.serverctl import ServerControl

#: Port des SSE-Integrationstests (39xxx ist fuer Tests freigegeben).
SSE_PORT = 39127

#: Port des Socket-Tests fuer den Nicht-Stream-Disconnect (39xxx).
DC_PORT = 39217

#: Static-Verzeichnis des Pakets (existiert bis Task 12 nicht).
STATIC_DIR = Path(__file__).resolve().parent.parent / "aiserver_monitor" / "static"


def _job_dict(rid: str, status: str, created_at_ms: int, **felder) -> dict:
    """Job-Zeile fuer History.record_job, gleiche Form wie in test_history."""
    basis = {
        "rid": rid, "source": "10.0.0.1", "model": "m",
        "prompt_start": "p", "status": status,
        "created_at_ms": created_at_ms, "ttft_ms": None,
        "duration_ms": None, "prompt_tokens": None,
        "completion_tokens": None, "error": None,
    }
    basis.update(felder)
    return basis


def _chat_skript(tokens: list[str]) -> list[fakes.Schritt]:
    """Kleines SGLang-SSE-Skript fuer App-Integrationstests."""
    schritte = [(0.0, fakes.chat_chunk(
        {"role": "assistant", "content": ""}))]
    schritte += [(0.0, fakes.chat_chunk({"content": token}))
                 for token in tokens]
    schritte.append((0.0, fakes.chat_chunk({}, finish_reason="stop")))
    schritte.append((0.0, fakes.DONE))
    return schritte


def _sample_dict(ts: float, **felder) -> dict:
    """Sample-Zeile fuer History.record_sample."""
    basis = {"ts": ts, "available": True}
    basis.update(felder)
    return basis


async def _leerer_gpu_reader() -> GpuSample:
    """GPU-Reader fuer Tests: kein nvidia-smi, alles "nicht gemessen"."""
    return GpuSample(None, None, None)


def _metrik_transport() -> httpx.MockTransport:
    """Transport, der ``/metrics`` und ``/health`` wie SGLang beantwortet."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            return httpx.Response(
                200,
                text='sglang:num_running_reqs{model_name="m"} 3.0\n'
                     'sglang:num_queue_reqs{model_name="m"} 1.0\n')
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class AppTestCase(unittest.TestCase):
    """Temp-DB je Test plus Helfer fuer Config, DB-Zugriff und App-Bau."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aiserver-monitor-app-")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "monitor.db")

    def config(self, **felder) -> Config:
        basis = {"db_path": self.db_path}
        basis.update(felder)
        return Config(**basis)

    def app(self, *, transport=None, steuer_transport=None,
            enable_background: bool = False,
            gpu_reader=None, server_control=None, model_lifecycle=None,
            **felder):
        return create_app(self.config(**felder), transport=transport,
                          steuer_transport=steuer_transport,
                          gpu_reader=gpu_reader,
                          server_control=server_control,
                          model_lifecycle=model_lifecycle,
                          enable_background=enable_background)

    def db_zeilen(self, sql: str, params=()) -> list[tuple]:
        """Liest direkt aus der DB (eigene Verbindung), unabhaengig von History."""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def db_vorbereiten(self, *, jobs=(), samples=()) -> None:
        """Legt die DB vor dem App-Start an (wie test_history: History, dann zu)."""
        history = History(self.db_path)
        history.init()
        try:
            for job in jobs:
                history.record_job(job)
            for sample in samples:
                history.record_sample(sample)
        finally:
            history.close()


class TestClientSource(unittest.TestCase):
    """R-A: gueltige CF-Connecting-IP gewinnt, sonst Peer, sonst "unbekannt"."""

    def test_gueltige_cf_ip_gewinnt(self):
        self.assertEqual(
            client_source({"cf-connecting-ip": "203.0.113.7"}, "10.0.0.1"),
            "203.0.113.7")

    def test_header_schreibweise_und_leerzeichen(self):
        self.assertEqual(
            client_source({"CF-Connecting-IP": " 203.0.113.7 "}, "10.0.0.1"),
            "203.0.113.7")

    def test_gueltige_ipv6_ip_gewinnt(self):
        self.assertEqual(
            client_source({"cf-connecting-ip": "2001:db8::1"}, None),
            "2001:db8::1")

    def test_ohne_header_gewinnt_der_peer(self):
        self.assertEqual(client_source({}, "10.0.0.1"), "10.0.0.1")

    def test_ungueltiger_header_faellt_auf_den_peer_zurueck(self):
        for wert in ("keine-ip", "999.1.1.1", "1.2.3", "", "  "):
            with self.subTest(wert=repr(wert)):
                self.assertEqual(
                    client_source({"cf-connecting-ip": wert}, "10.0.0.1"),
                    "10.0.0.1")

    def test_ohne_peer_unbekannt(self):
        self.assertEqual(client_source({}, None), "unbekannt")
        self.assertEqual(client_source({"cf-connecting-ip": "kaputt"}, ""),
                         "unbekannt")


class TestCfMiddleware(AppTestCase):
    """R-A: die Middleware ersetzt die Peer-IP im Scope; proxy.py bleibt unberuehrt."""

    def _app(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        return self.app(transport=httpx.MockTransport(handler))

    def _job_quelle(self, app, headers: dict) -> str:
        with TestClient(app) as c:
            antwort = c.post(
                "/v1/chat/completions",
                json={"model": "m",
                      "messages": [{"role": "user", "content": "Hi"}]},
                headers=headers)
            self.assertEqual(antwort.status_code, 200)
            jobs = c.get("/monitor/jobs").json()["jobs"]
        self.assertEqual(len(jobs), 1)
        return jobs[0]["source"]

    def test_gueltige_cf_ip_wird_job_quelle(self):
        self.assertEqual(
            self._job_quelle(self._app(),
                             {"CF-Connecting-IP": "203.0.113.7"}),
            "203.0.113.7")

    def test_ohne_header_bleibt_peer_ip(self):
        self.assertEqual(self._job_quelle(self._app(), {}), "testclient")

    def test_ungueltiger_header_bleibt_peer_ip(self):
        self.assertEqual(
            self._job_quelle(self._app(), {"CF-Connecting-IP": "kaputt"}),
            "testclient")


class TestCfMiddlewareScope(unittest.IsolatedAsyncioTestCase):
    """R-A-Detail: nur gueltige IPs aendern den Scope, der Port bleibt."""

    async def _scope_nach_middleware(self, headers, client):
        gesehen: dict = {}

        async def inner(scope, receive, send):
            gesehen.update(scope)

        scope = {"type": "http", "method": "GET", "path": "/",
                 "headers": headers, "client": client}
        await _CfClientMiddleware(inner)(scope, None, None)
        return gesehen

    async def test_gueltige_ip_ersetzt_host_und_behaelt_den_port(self):
        scope = await self._scope_nach_middleware(
            [(b"cf-connecting-ip", b"203.0.113.7")], ("10.0.0.1", 4711))
        self.assertEqual(scope["client"], ("203.0.113.7", 4711))

    async def test_ohne_client_ist_der_port_null(self):
        scope = await self._scope_nach_middleware(
            [(b"cf-connecting-ip", b"203.0.113.7")], None)
        self.assertEqual(scope["client"], ("203.0.113.7", 0))

    async def test_ungueltiger_header_laesst_den_scope_unveraendert(self):
        scope = await self._scope_nach_middleware(
            [(b"cf-connecting-ip", b"kaputt")], ("10.0.0.1", 4711))
        self.assertEqual(scope["client"], ("10.0.0.1", 4711))


class TestBasisRouten(AppTestCase):
    def test_healthz(self):
        with TestClient(self.app()) as c:
            antwort = c.get("/healthz")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {"status": "ok"})

    def test_monitor_jobs_leer(self):
        with TestClient(self.app()) as c:
            antwort = c.get("/monitor/jobs")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {"jobs": []})

    def test_monitor_jobs_gefuellt(self):
        app = self.app()
        with TestClient(app) as c:
            c.app.state.deps.registry.register(
                "r1", source="10.0.0.1", model="m", full_prompt="Hallo",
                now=1000.0)
            antwort = c.get("/monitor/jobs")
        self.assertEqual([job["rid"] for job in antwort.json()["jobs"]], ["r1"])

    def test_app_state_deps_und_collector_task(self):
        # R-J: beides liegt fuer Tests bereit; ohne Hintergrund kein Task.
        app = self.app(enable_background=False)
        self.assertIsInstance(app.state.deps, Deps)
        self.assertIsNone(app.state.collector_task)
        with TestClient(app) as c:
            self.assertIsNone(c.app.state.collector_task)

    def test_wurzel_liefert_html(self):
        with TestClient(self.app()) as c:
            antwort = c.get("/")
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("text/html", antwort.headers["content-type"])
        if not STATIC_DIR.is_dir():
            # Bis Task 12 gibt es kein Static-Verzeichnis: Platzhalterseite.
            self.assertIn("aiserver-monitor", antwort.text)
            self.assertIn("Dashboard", antwort.text)


class TestPrioritaetsRouten(AppTestCase):
    """GET/PUT /monitor/priority: Dashboard liest und schreibt die Marker."""

    def test_get_liefert_defaults(self):
        with TestClient(self.app()) as c:
            antwort = c.get("/monitor/priority")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(),
                         {"enabled": True, "markers": [DEFAULT_MARKER]})

    def test_put_speichert_und_überlebt_neustart(self):
        with TestClient(self.app()) as c:
            antwort = c.put("/monitor/priority", json={
                "enabled": False,
                "markers": ["  Neuer Marker  ", "Noch einer"],
            })
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json(), {
                "enabled": False,
                "markers": ["Neuer Marker", "Noch einer"],
            })

        with TestClient(self.app()) as c:
            antwort = c.get("/monitor/priority")
        self.assertEqual(antwort.json(), {
            "enabled": False,
            "markers": ["Neuer Marker", "Noch einer"],
        })

    def test_put_lehnt_ungueltige_marker_ab(self):
        with TestClient(self.app()) as c:
            antwort = c.put("/monitor/priority", json={
                "enabled": True,
                "markers": [42],
            })
            gespeichert = c.get("/monitor/priority")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(gespeichert.json(),
                         {"enabled": True, "markers": [DEFAULT_MARKER]})

    def test_put_lehnt_nicht_bool_ab(self):
        with TestClient(self.app()) as c:
            antwort = c.put("/monitor/priority", json={
                "enabled": "ja",
                "markers": [DEFAULT_MARKER],
            })
        self.assertEqual(antwort.status_code, 400)


class TestHindsightRouten(AppTestCase):
    """GET/PUT /monitor/hindsight: blockieren und gezielt Jobs abbrechen."""

    def test_get_liefert_defaults(self):
        with TestClient(self.app()) as c:
            antwort = c.get("/monitor/hindsight")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {
            "enabled": False,
            "markers": list(DEFAULT_HINDSIGHT_MARKERS),
        })

    def test_put_speichert_und_überlebt_neustart(self):
        with TestClient(self.app()) as c:
            antwort = c.put("/monitor/hindsight", json={
                "enabled": True,
                "markers": ["  Neuer Marker  ", "Noch einer"],
            })
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json(), {
                "enabled": True,
                "markers": ["Neuer Marker", "Noch einer"],
                "aborted": 0,
            })

        with TestClient(self.app()) as c:
            antwort = c.get("/monitor/hindsight")
        self.assertEqual(antwort.json(), {
            "enabled": True,
            "markers": ["Neuer Marker", "Noch einer"],
        })

    def test_put_lehnt_ungueltige_marker_ab(self):
        with TestClient(self.app()) as c:
            antwort = c.put("/monitor/hindsight", json={
                "enabled": True,
                "markers": [42],
            })
            gespeichert = c.get("/monitor/hindsight")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(gespeichert.json(), {
            "enabled": False,
            "markers": list(DEFAULT_HINDSIGHT_MARKERS),
        })

    def test_put_lehnt_nicht_bool_ab(self):
        with TestClient(self.app()) as c:
            antwort = c.put("/monitor/hindsight", json={
                "enabled": "ja",
                "markers": list(DEFAULT_HINDSIGHT_MARKERS),
            })
        self.assertEqual(antwort.status_code, 400)

    def test_einschalten_bricht_nur_aktive_hindsight_jobs_ab(self):
        fake = _AbortFake()
        app = self.app(steuer_transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            reg = c.app.state.deps.registry
            reg.register("h-waiting", source="s", model="m",
                         full_prompt="p", now=1000.0, hindsight=True)
            reg.register("h-running", source="s", model="m",
                         full_prompt="p", now=1001.0, hindsight=True)
            reg.note_running("h-running", now=1002.0)
            reg.register("normal", source="s", model="m",
                         full_prompt="p", now=1003.0)
            reg.note_running("normal", now=1004.0)
            reg.register("h-done", source="s", model="m",
                         full_prompt="p", now=1005.0, hindsight=True)
            reg.finish("h-done", JobStatus.DONE, now=1006.0)
            queue = c.app.state.deps.hub.subscribe()

            antwort = c.put("/monitor/hindsight", json={
                "enabled": True,
                "markers": list(DEFAULT_HINDSIGHT_MARKERS),
            })

            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json()["aborted"], 2)
            self.assertEqual(
                sorted(eintrag["rid"] for eintrag in fake.koerper),
                ["h-running", "h-waiting"])
            self.assertTrue(all(
                eintrag["abort_all"] is False for eintrag in fake.koerper))
            self.assertIs(reg.get("h-waiting").status, JobStatus.ABORTED)
            self.assertIs(reg.get("h-running").status, JobStatus.ABORTED)
            self.assertIs(reg.get("normal").status, JobStatus.RUNNING)
            self.assertIs(reg.get("h-done").status, JobStatus.DONE)
            self.assertTrue(
                reg.abort_signal("h-waiting").is_set())
            self.assertTrue(
                reg.abort_signal("h-running").is_set())
            ereignisse = []
            while not queue.empty():
                rahmen = queue.get_nowait()
                ereignisse.append(json.loads(rahmen.split("data: ", 1)[1]))
            self.assertEqual(
                sorted(eintrag["rid"] for eintrag in ereignisse
                       if eintrag["status"] == "aborted"),
                ["h-running", "h-waiting"])

    def test_ausschalten_bricht_keine_jobs_ab(self):
        fake = _AbortFake()
        app = self.app(steuer_transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            c.app.state.deps.registry.register(
                "h1", source="s", model="m", full_prompt="p", now=1000.0,
                hindsight=True)
            antwort = c.put("/monitor/hindsight", json={
                "enabled": False,
                "markers": list(DEFAULT_HINDSIGHT_MARKERS),
            })
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json()["aborted"], 0)
            self.assertIs(c.app.state.deps.registry.get("h1").status,
                          JobStatus.WAITING)
        self.assertEqual(fake.requests, [])

    def test_aktivierter_blocker_weist_hindsight_ab_und_laesst_normal_durch(self):
        fake = fakes.FakeSglang(_chat_skript(["Hallo"]))
        app = self.app(transport=httpx.ASGITransport(app=fake.app))
        with TestClient(app) as c:
            aktiviert = c.put("/monitor/hindsight", json={
                "enabled": True,
                "markers": list(DEFAULT_HINDSIGHT_MARKERS),
            })
            self.assertEqual(aktiviert.status_code, 200)
            blocked = c.post("/v1/chat/completions", json={
                "model": "m",
                "messages": [{"role": "system", "content": (
                    "Extract SIGNIFICANT facts from text")}],
            })
            normal = c.post("/v1/chat/completions", json={
                "model": "m",
                "messages": [{"role": "system", "content": DEFAULT_MARKER}],
            })
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(blocked.json()["error"]["type"],
                         "hindsight_blocked")
        self.assertEqual(normal.status_code, 200)
        self.assertEqual(len(fake.anfragen), 1)


class _AbortFake:
    """MockTransport-Handler: schreibt Abbruch-Anfragen mit, optional Fehler."""

    def __init__(self, *, status: int = 200,
                 fehler: Exception | None = None):
        self.status = status
        self.fehler = fehler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fehler is not None:
            raise self.fehler
        return httpx.Response(self.status, json={"status": "ok"})

    @property
    def koerper(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


class _AusgeschoepfterTransport(httpx.AsyncBaseTransport):
    """Transport wie ein gesaettigter Pool: wirft sofort ``PoolTimeout``.

    Simuliert den Live-Befund: der Proxy-Client bekommt keinen Slot mehr; der
    dedizierte Steuer-Client muss davon unabhaengig antworten koennen.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(
            self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise httpx.PoolTimeout("Pool ist ausgeschoepft")


class TestCancelRouten(AppTestCase):
    def _registriere(self, c, rid: str, *, aktiv: bool = True):
        job = c.app.state.deps.registry.register(
            rid, source="10.0.0.1", model="m", full_prompt="Hallo", now=1000.0)
        if aktiv:
            c.app.state.deps.registry.note_running(rid, now=1001.0)
        return job

    def test_einzelabbruch_leitet_weiter_und_markiert_aborted(self):
        fake = _AbortFake()
        app = self.app(transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            self._registriere(c, "rid-1")
            queue = c.app.state.deps.hub.subscribe()
            antwort = c.post("/monitor/jobs/rid-1/cancel")
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json(), {"ok": True})
            # Weiterleitung: POST /abort_request mit der rid, kein abort_all.
            self.assertEqual(len(fake.requests), 1)
            self.assertEqual(fake.requests[0].method, "POST")
            self.assertIn("/abort_request", str(fake.requests[0].url))
            self.assertEqual(fake.koerper[0],
                             {"rid": "rid-1", "abort_all": False})
            # Job abgebrochen und als Ereignis publiziert.
            self.assertIs(c.app.state.deps.registry.get("rid-1").status,
                          JobStatus.ABORTED)
            rahmen = queue.get_nowait()
            self.assertTrue(rahmen.startswith("event: job\ndata: "))
            self.assertEqual(json.loads(rahmen.split("data: ", 1)[1])["status"],
                             "aborted")

    def test_einzelabbruch_unbekannte_rid_crasht_nicht(self):
        fake = _AbortFake()
        app = self.app(transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            antwort = c.post("/monitor/jobs/gibt-es-nicht/cancel")
            self.assertEqual(c.get("/monitor/jobs").json(), {"jobs": []})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {"ok": True})
        self.assertEqual(fake.koerper,
                         [{"rid": "gibt-es-nicht", "abort_all": False}])

    def test_einzelabbruch_setzt_das_lokale_signal(self):
        # R5: die Route setzt zusaetzlich das lokale Abbruch-Signal des Jobs;
        # nur damit beendet der Non-Stream-Pfad seinen laufenden Lauf.
        fake = _AbortFake()
        app = self.app(transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            self._registriere(c, "sig-1")
            signal = c.app.state.deps.registry.abort_signal("sig-1")
            self.assertFalse(signal.is_set())
            antwort = c.post("/monitor/jobs/sig-1/cancel")
            self.assertEqual(antwort.json(), {"ok": True})
            self.assertTrue(signal.is_set())

    def test_einzelabbruch_nach_done_ist_ein_noop(self):
        # R7.7 (first-wins): ein Abbruch nach dem Abschluss aendert den Job
        # nicht und erzeugt kein aborted-Ereignis.
        fake = _AbortFake()
        app = self.app(transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            reg = c.app.state.deps.registry
            self._registriere(c, "fertig-1")
            reg.finish("fertig-1", JobStatus.DONE, completion_tokens=9,
                       now=1002.0)
            queue = c.app.state.deps.hub.subscribe()
            antwort = c.post("/monitor/jobs/fertig-1/cancel")
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json(), {"ok": True})
            self.assertIs(reg.get("fertig-1").status, JobStatus.DONE)
            self.assertEqual(reg.get("fertig-1").completion_tokens, 9)
            self.assertTrue(queue.empty())              # kein zweites Ereignis

    def test_einzelabbruch_ohne_upstream_ok_job_bleibt(self):
        # Upstream antwortet mit Status != 200: ok False, Job unangetastet.
        fake = _AbortFake(status=500)
        app = self.app(transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            self._registriere(c, "rid-2")
            with self.assertLogs("aiserver_monitor.cancel", level="WARNING"):
                antwort = c.post("/monitor/jobs/rid-2/cancel")
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json(), {"ok": False})
            self.assertIs(c.app.state.deps.registry.get("rid-2").status,
                          JobStatus.RUNNING)

    def test_einzelabbruch_netzfehler(self):
        fake = _AbortFake(fehler=httpx.ConnectError("upstream weg"))
        app = self.app(transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            self._registriere(c, "rid-3")
            with self.assertLogs("aiserver_monitor.app",
                                 level="WARNING") as logs:
                antwort = c.post("/monitor/jobs/rid-3/cancel")
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json(), {"ok": False})
            self.assertIs(c.app.state.deps.registry.get("rid-3").status,
                          JobStatus.RUNNING)
        self.assertTrue(any("fehlgeschlagen" in zeile
                            for zeile in logs.output))

    def test_cancel_all_bricht_nur_aktive_jobs_ab(self):
        fake = _AbortFake()
        app = self.app(transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            reg = c.app.state.deps.registry
            self._registriere(c, "w1", aktiv=False)       # waiting
            self._registriere(c, "r1")                    # running
            reg.register("d1", source="s", model="m", full_prompt="p",
                         now=1002.0)
            reg.finish("d1", JobStatus.DONE, now=1003.0)
            antwort = c.post("/monitor/cancel_all")
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json(), {"ok": True})
            self.assertEqual(fake.koerper[0],
                             {"rid": "", "abort_all": True})
            self.assertIs(reg.get("w1").status, JobStatus.ABORTED)
            self.assertIs(reg.get("r1").status, JobStatus.ABORTED)
            self.assertIs(reg.get("d1").status, JobStatus.DONE)

    def test_cancel_all_netzfehler_laesst_jobs_in_ruhe(self):
        fake = _AbortFake(fehler=httpx.ConnectError("upstream weg"))
        app = self.app(transport=httpx.MockTransport(fake))
        with TestClient(app) as c:
            self._registriere(c, "rid-4")
            with self.assertLogs("aiserver_monitor.app", level="WARNING"):
                antwort = c.post("/monitor/cancel_all")
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json(), {"ok": False})
            self.assertIs(c.app.state.deps.registry.get("rid-4").status,
                          JobStatus.RUNNING)


class TestDedizierterSteuerClient(AppTestCase):
    """R1/R2/R4/R5: Cancel laeuft ueber einen eigenen Client (eigener Pool)."""

    def test_steuer_client_ist_eigenstaendig_mit_modul_timeout(self):
        # R1: eigener Client, explizites Timeout aus der Modulkonstanten.
        from aiserver_monitor import app as app_modul
        app = self.app(enable_background=False)
        steuer_client = app.state.steuer_client
        self.assertIsNot(steuer_client, app.state.deps.client)
        self.assertEqual(app_modul._STEUER_TIMEOUT_S, 5.0)
        self.assertEqual(steuer_client.timeout,
                         httpx.Timeout(app_modul._STEUER_TIMEOUT_S))

    def test_cancel_und_cancel_all_nutzen_den_steuer_client(self):
        # R5a: getrennte Recorder; /abort_request sieht nur der Steuer-Client.
        proxy_fake = _AbortFake()
        steuer_fake = _AbortFake()
        app = self.app(transport=httpx.MockTransport(proxy_fake),
                       steuer_transport=httpx.MockTransport(steuer_fake))
        with TestClient(app) as c:
            reg = c.app.state.deps.registry
            reg.register("w1", source="10.0.0.1", model="m",
                         full_prompt="Hallo", now=1000.0)
            reg.note_running("w1", now=1001.0)
            einzel = c.post("/monitor/jobs/w1/cancel")
            alle = c.post("/monitor/cancel_all")
        self.assertEqual(einzel.status_code, 200)
        self.assertEqual(einzel.json(), {"ok": True})
        self.assertEqual(alle.status_code, 200)
        self.assertEqual(alle.json(), {"ok": True})
        self.assertEqual(steuer_fake.koerper,
                         [{"rid": "w1", "abort_all": False},
                          {"rid": "", "abort_all": True}])
        # Der Proxy-Client bleibt unberuehrt (kein /abort_request, gar nichts).
        self.assertEqual(proxy_fake.requests, [])

    def test_cancel_bleibt_ok_bei_ausgeschoepftem_proxy_pool(self):
        # R5b: der Proxy-Client laeuft in den PoolTimeout (Live-Bug); die
        # Cancel-Routen antworten trotzdem ok=true ueber den Steuer-Client.
        proxy_transport = _AusgeschoepfterTransport()
        steuer_fake = _AbortFake()
        app = self.app(transport=proxy_transport,
                       steuer_transport=httpx.MockTransport(steuer_fake))
        with TestClient(app) as c:
            reg = c.app.state.deps.registry
            reg.register("p1", source="10.0.0.1", model="m",
                         full_prompt="Hallo", now=1000.0)
            reg.note_running("p1", now=1001.0)
            antwort = c.post("/monitor/jobs/p1/cancel")
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json(), {"ok": True})
            self.assertIs(reg.get("p1").status, JobStatus.ABORTED)
            alle = c.post("/monitor/cancel_all")
            self.assertEqual(alle.json(), {"ok": True})
        self.assertEqual(steuer_fake.koerper,
                         [{"rid": "p1", "abort_all": False},
                          {"rid": "", "abort_all": True}])
        # Der gesaettigte Proxy-Client wurde nie kontaktiert.
        self.assertEqual(proxy_transport.requests, [])

    def test_shutdown_schliesst_auch_den_steuer_client(self):
        # R4: die Lifespan schliesst den Steuer-Client beim Shutdown mit.
        app = self.app(enable_background=False)
        steuer_client = app.state.steuer_client
        self.assertIsNot(steuer_client, app.state.deps.client)
        self.assertFalse(steuer_client.is_closed)
        with TestClient(app):
            self.assertFalse(steuer_client.is_closed)
        self.assertTrue(steuer_client.is_closed)


class TestNonstreamLiveIntegration(AppTestCase):
    """R5 komplett: die Cancel-Route beendet einen laufenden Chat wirklich.

    Der Upstream streamt langsam; der HTTP-Client haengt, waehrend der Job als
    ``running`` sichtbar ist. Der Cancel-Aufruf setzt das Signal, die Route
    markiert aborted, der wartende Client bekommt 499 mit ``aborted``-JSON
    und der Thread endet (kein Zombie).
    """

    def test_cancel_route_beendet_laufenden_chat_und_antwortet_499(self):
        schritte = [
            (0.0, fakes.chat_chunk({"role": "assistant", "content": ""})),
            (0.0, fakes.chat_chunk({"content": "A"})),
            (5.0, fakes.chat_chunk({"content": "B"})),
            (0.0, fakes.chat_chunk({}, finish_reason="stop")),
            (0.0, fakes.DONE),
        ]
        abbrueche: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/abort_request":
                abbrueche.append(json.loads(request.content))
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=fakes.SkriptStream(schritte))

        app = self.app(transport=httpx.MockTransport(handler),
                       heartbeat_s=15.0)
        with TestClient(app) as c:
            ergebnis: dict = {}

            def chat() -> None:
                try:
                    antwort = c.post("/v1/chat/completions", json={
                        "model": "fake-modell", "rid": "live-int-1",
                        "stream": False,
                        "messages": [{"role": "user", "content": "Hi"}]})
                except Exception as exc:     # z. B. Gateway-Shutdown im Test
                    ergebnis["fehler"] = f"{type(exc).__name__}: {exc}"
                    return
                ergebnis["status"] = antwort.status_code
                ergebnis["body"] = antwort.json()

            thread = threading.Thread(target=chat, daemon=True)
            thread.start()
            try:
                self.assertTrue(
                    self._warte_auf_status(c, "live-int-1", "running"),
                    "Job wurde nicht als running sichtbar")

                antwort = c.post("/monitor/jobs/live-int-1/cancel")
                self.assertEqual(antwort.status_code, 200)
                self.assertEqual(antwort.json(), {"ok": True})

                thread.join(timeout=3.0)
                self.assertFalse(thread.is_alive(), "der Client haengt noch")
                self.assertNotIn("fehler", ergebnis)
                self.assertEqual(ergebnis["status"], 499)
                self.assertEqual(ergebnis["body"], {
                    "error": {"message": "Job wurde abgebrochen",
                              "type": "aborted"}})
                job = c.app.state.deps.registry.get("live-int-1")
                self.assertIs(job.status, JobStatus.ABORTED)
                # abort_request ging raus (ggf. doppelt: Route und Pfad sind
                # beide best effort, SGLang vertraegt das).
                self.assertGreaterEqual(len(abbrueche), 1)
                for eintrag in abbrueche:
                    self.assertEqual(eintrag, {"rid": "live-int-1",
                                               "abort_all": False})
            finally:
                thread.join(timeout=3.0)

    def _warte_auf_status(self, c, rid: str, status: str) -> bool:
        """Pollt den Job-Snapshot bis zum Status (kurze Deadline)."""
        ende = time.monotonic() + 3.0
        while time.monotonic() < ende:
            jobs = {eintrag["rid"]: eintrag
                    for eintrag in c.get("/monitor/jobs").json()["jobs"]}
            if jobs.get(rid, {}).get("status") == status:
                return True
            time.sleep(0.02)
        return False


class TestStatsRoute(AppTestCase):
    def test_defaults_und_ms_umrechnung(self):
        jetzt = time.time()
        job_ms = int((jetzt - 7200) * 1000)
        self.db_vorbereiten(jobs=[_job_dict("alt", "done", job_ms,
                                            duration_ms=1000.0)])
        with TestClient(self.app()) as c:
            # Explizite ms-Grenzen um den Job: genau der eine Job zaehlt.
            antwort = c.get("/monitor/stats", params={
                "from": job_ms - 100, "to": job_ms + 100, "bucket": 60})
            self.assertEqual(antwort.status_code, 200)
            self.assertEqual(antwort.json()["overview"]["count"], 1)
            # Fenster direkt daneben (ms, nicht s): leer.
            antwort = c.get("/monitor/stats", params={
                "from": job_ms + 101, "to": job_ms + 60000, "bucket": 60})
            self.assertEqual(antwort.json()["overview"]["count"], 0)
            # Defaults to=now/from=to-24h: der Job vor 2 h ist drin.
            antwort = c.get("/monitor/stats", params={"bucket": 60})
            self.assertEqual(antwort.json()["overview"]["count"], 1)

    def test_antwort_ist_query_stats_ergebnis(self):
        jetzt = time.time()
        self.db_vorbereiten(
            jobs=[_job_dict("j1", "done", int((jetzt - 7200) * 1000),
                            duration_ms=1000.0, prompt_tokens=3,
                            completion_tokens=2)],
            samples=[_sample_dict(jetzt - 7200, running=2, waiting=1)])
        with TestClient(self.app()) as c:
            daten = c.get("/monitor/stats", params={"bucket": 60}).json()
        self.assertEqual(
            sorted(daten), ["buckets", "errors", "models", "overview", "sources"])
        self.assertEqual(daten["overview"]["count"], 1)
        self.assertEqual(daten["overview"]["done"], 1)
        self.assertEqual(daten["overview"]["tokens_in"], 3)
        self.assertEqual(daten["overview"]["tokens_out"], 2)
        self.assertIn("p99", daten["overview"]["ttft"])
        self.assertIn("tok_per_s", daten["overview"])
        self.assertEqual(daten["models"][0]["model"], "m")
        self.assertEqual(daten["sources"][0]["source"], "10.0.0.1")
        self.assertEqual(daten["errors"], [])
        self.assertEqual(len(daten["buckets"]), 1)
        self.assertEqual(daten["buckets"][0]["running"], 2.0)
        self.assertEqual(daten["buckets"][0]["jobs"], 1)

    def test_fenster_wird_auf_30_tage_geklemmt(self):
        jetzt = time.time()
        self.db_vorbereiten(jobs=[
            _job_dict("sehr-alt", "done", int((jetzt - 40 * 86400) * 1000),
                      duration_ms=1000.0),
            _job_dict("gerade-noch", "done", int((jetzt - 29 * 86400) * 1000),
                      duration_ms=1000.0),
        ])
        with TestClient(self.app()) as c:
            daten = c.get("/monitor/stats", params={
                "from": 0, "to": int(jetzt * 1000), "bucket": 86400}).json()
        # Ohne Klemmung waeren beide Jobs drin (from=0); mit Klemmung nur der
        # 29-Tage-Job ausserhalb von 30 Tagen.
        self.assertEqual(daten["overview"]["count"], 1)

    def test_from_nach_to_ist_400(self):
        with TestClient(self.app()) as c:
            antwort = c.get("/monitor/stats", params={
                "from": 2000, "to": 1000, "bucket": 60})
        self.assertEqual(antwort.status_code, 400)
        self.assertIn("error", antwort.json())
        self.assertIn("from", antwort.json()["error"])

    def test_bucket_kleiner_gleich_null_ist_400(self):
        with TestClient(self.app()) as c:
            for bucket in (0, -5):
                with self.subTest(bucket=bucket):
                    antwort = c.get("/monitor/stats",
                                    params={"bucket": bucket})
                    self.assertEqual(antwort.status_code, 400)
                    self.assertIn("error", antwort.json())


class TestV1Passthrough(AppTestCase):
    def test_v1_models_passthrough(self):
        antwort_daten = {"object": "list",
                         "data": [{"id": "fake-modell", "object": "model",
                                   "owned_by": "fake"}]}
        gesehen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            gesehen.append(request)
            return httpx.Response(200, json=antwort_daten)

        app = self.app(transport=httpx.MockTransport(handler))
        with TestClient(app) as c:
            antwort = c.get("/v1/models")
        self.assertEqual(antwort.status_code, 200)
        daten = antwort.json()
        ids = [eintrag["id"] for eintrag in daten["data"]]
        # Der Upstream-Eintrag bleibt, die stabilen Gateway-Namen kommen dazu.
        self.assertEqual(ids[0], "fake-modell")
        self.assertIn("escha-qwen38-27b-w2", ids)
        self.assertIn("edge0-120b", ids)
        self.assertEqual(daten["object"], "list")
        self.assertEqual(len(gesehen), 1)
        self.assertEqual(gesehen[0].method, "GET")
        self.assertEqual(str(gesehen[0].url),
                         "http://127.0.0.1:30001/v1/models")

    def test_v1_models_alias_meldet_edge0_kontext(self):
        antwort_daten = {"object": "list", "data": [
            {"id": "edge0-120b", "object": "model", "owned_by": "local",
             "context_length": 131072}]}
        gesehen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            gesehen.append(request)
            return httpx.Response(200, json=antwort_daten)

        app = self.app(transport=httpx.MockTransport(handler))
        with TestClient(app) as c:
            antwort = c.get("/v1/models")
        self.assertEqual(antwort.status_code, 200)
        eintraege = {e["id"]: e for e in antwort.json()["data"]}
        alias = eintraege["escha-qwen38-27b-w2"]
        self.assertEqual(alias["context_length"], 131072)
        self.assertEqual(alias["max_model_len"], 131072)
        # Es bleibt beim einen Upstream-Aufruf: Edge0 meldet den Kontext
        # bereits selbst, /api/ps wird nicht gebraucht.
        self.assertEqual([str(r.url) for r in gesehen],
                         ["http://127.0.0.1:30001/v1/models"])

    def test_v1_models_alias_meldet_qwen_kontext(self):
        antwort_daten = {"object": "list", "data": [
            {"id": "escha-qwen38-27b-w2", "object": "model",
             "owned_by": "sglang", "max_model_len": 86016}]}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=antwort_daten)

        app = self.app(transport=httpx.MockTransport(handler))
        with TestClient(app) as c:
            antwort = c.get("/v1/models")
        eintraege = {e["id"]: e for e in antwort.json()["data"]}
        alias = eintraege["escha-qwen38-27b-w2"]
        self.assertEqual(alias["context_length"], 86016)
        self.assertEqual(alias["max_model_len"], 86016)
        # edge0-120b ist nicht geladen und darf kein Fenster versprechen.
        edge0 = eintraege["edge0-120b"]
        self.assertNotIn("context_length", edge0)
        self.assertNotIn("max_model_len", edge0)

    def test_v1_models_alias_meldet_ollama_kontext_aus_api_ps(self):
        modelle = ["hf.co/AtomicChat/Flint-GGUF:IQ4_XS", "gpt-oss:20b"]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={
                    "object": "list",
                    "data": [{"id": name, "object": "model",
                              "owned_by": "ollama"} for name in modelle],
                })
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [
                    {"name": modelle[0], "context_length": 32768}]})
            return httpx.Response(404)

        app = self.app(transport=httpx.MockTransport(handler))
        with TestClient(app) as c:
            antwort = c.get("/v1/models")
        eintraege = {e["id"]: e for e in antwort.json()["data"]}
        alias = eintraege["escha-qwen38-27b-w2"]
        self.assertEqual(alias["context_length"], 32768)
        # Auch der tatsaechlich geladene Ollama-Name traegt den Live-Wert.
        self.assertEqual(eintraege[modelle[0]]["context_length"], 32768)
        self.assertNotIn("context_length", eintraege["edge0-120b"])
        self.assertNotIn("context_length", eintraege[modelle[1]])


class TestCollectorImHintergrund(AppTestCase):
    def test_collector_startet_und_schreibt_sample(self):
        app = self.app(transport=_metrik_transport(),
                       enable_background=True, gpu_reader=_leerer_gpu_reader,
                       poll_interval_s=0.05)
        with TestClient(app) as c:
            task = c.app.state.collector_task
            self.assertIsNotNone(task)
            ende = time.monotonic() + 2.0
            zeilen: list[tuple] = []
            while time.monotonic() < ende:
                zeilen = self.db_zeilen(
                    "SELECT available, running, waiting FROM samples"
                    " ORDER BY ts LIMIT 1")
                if zeilen:
                    break
                time.sleep(0.02)
            self.assertTrue(zeilen, "Collector hat kein Sample geschrieben")
            self.assertEqual(zeilen[0], (1, 3, 1))
        # Shutdown raeumt den Task ab (kein Zombie-Task).
        self.assertTrue(task.done())

    def test_eigener_collector_client_und_shutdown_schliesst_beide(self):
        # R5/R7-6: der Sammler hat einen EIGENEN httpx-Client (eigener Pool);
        # die Lifespan schliesst beim Shutdown beide Clients.
        app = self.app(transport=_metrik_transport(), enable_background=False)
        client = app.state.deps.client
        collector_client = app.state.collector_client
        self.assertIsNot(collector_client, client)
        self.assertFalse(client.is_closed)
        self.assertFalse(collector_client.is_closed)

        with TestClient(app):
            self.assertFalse(client.is_closed)

        self.assertTrue(client.is_closed)
        self.assertTrue(collector_client.is_closed)


class TestLifespanRecoveryUndCleanup(AppTestCase):
    def test_running_job_wird_beim_start_error(self):
        self.db_vorbereiten(jobs=[_job_dict(
            "laeuft", "running", int((time.time() - 60) * 1000), ttft_ms=10.0)])
        with TestClient(self.app()) as c:
            pass
        self.assertEqual(
            self.db_zeilen(
                "SELECT status, error FROM jobs WHERE rid = 'laeuft'"),
            [("error", "gateway restart")])

    def test_recovery_loggt_mit_info(self):
        self.db_vorbereiten(jobs=[_job_dict(
            "laeuft", "running", int((time.time() - 60) * 1000))])
        with self.assertLogs("aiserver_monitor.app", level="INFO") as logs:
            with TestClient(self.app()) as c:
                pass
        self.assertTrue(any("Recovery" in zeile for zeile in logs.output))

    def test_start_cleanup_loescht_alte_zeilen(self):
        jetzt = time.time()
        self.db_vorbereiten(
            jobs=[_job_dict("alt", "done", int((jetzt - 31 * 86400) * 1000),
                            duration_ms=1.0),
                  _job_dict("jung", "done", int((jetzt - 100) * 1000),
                            duration_ms=1.0)],
            samples=[_sample_dict(jetzt - 8 * 86400),
                     _sample_dict(jetzt - 100)])
        with TestClient(self.app()) as c:
            pass
        self.assertEqual(self.db_zeilen("SELECT rid FROM jobs"), [("jung",)])
        zeilen = self.db_zeilen("SELECT ts FROM samples")
        self.assertEqual(len(zeilen), 1)
        self.assertGreater(zeilen[0][0], jetzt - 86400)

    def test_create_app_legt_db_verzeichnis_an(self):
        pfad = os.path.join(self.tmp.name, "neu", "tief", "monitor.db")
        app = create_app(Config(db_path=pfad), enable_background=False)
        with TestClient(app) as c:
            pass
        self.assertTrue(os.path.exists(pfad))


class TestSseIntegration(AppTestCase):
    """R-K: erstes SSE-Event ist der Snapshot (echter uvicorn, echter Socket)."""

    def test_erstes_event_ist_snapshot(self):
        app = self.app(enable_background=False, heartbeat_s=0.05)
        app.state.deps.registry.register(
            "sse-1", source="10.0.0.1", model="m", full_prompt="Hallo Welt",
            now=time.time())
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=SSE_PORT, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            ende = time.monotonic() + 5.0
            while not server.started and time.monotonic() < ende:
                time.sleep(0.02)
            self.assertTrue(server.started, "uvicorn startet nicht")
            zeilen = self._sse_zeilen(
                f"http://127.0.0.1:{SSE_PORT}/monitor/events")
            self.assertEqual(zeilen[0], "event: snapshot")
            daten = json.loads(zeilen[1].removeprefix("data: "))
            self.assertEqual([job["rid"] for job in daten["jobs"]], ["sse-1"])
            self.assertIsNone(daten["stats"])
            self.assertEqual(zeilen[2], "")
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "uvicorn-Thread laeuft noch")

    def _sse_zeilen(self, url: str) -> list[str]:
        """Die ersten SSE-Zeilen mit 2-s-Leselimit; schliesst den Stream."""
        zeilen: list[str] = []
        with httpx.Client(timeout=httpx.Timeout(2.0),
                          trust_env=False) as client:
            with client.stream("GET", url) as antwort:
                self.assertEqual(antwort.status_code, 200)
                self.assertEqual(antwort.headers["content-type"],
                                 "text/event-stream; charset=utf-8")
                self.assertEqual(antwort.headers["cache-control"], "no-cache")
                for zeile in antwort.iter_lines():
                    zeilen.append(zeile)
                    if len(zeilen) >= 3:
                        break
        return zeilen


class _StillerSseTransport(httpx.AsyncBaseTransport):
    """Skripteter Upstream: stille SSE-Antwort, Aborts werden protokolliert.

    ``/v1/chat/completions`` liefert eine SSE-Antwort, deren erstes Byte
    ``PAUSE_S`` Sekunden auf sich warten laesst: solange bleibt der
    Non-Stream-Job im Pool und der Antwortkoerper liefert nur
    Leerzeichen-Keepalives. ``/abort_request`` antwortet 200 und sammelt die
    Bodies (Beweis, dass der Abort wirklich am Upstream ankam).
    """

    PAUSE_S = 30.0

    def __init__(self) -> None:
        self.aborts: list[dict] = []

    async def handle_async_request(
            self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/abort_request":
            self.aborts.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"success": True})
        if request.url.path in ("/health", "/metrics"):
            return httpx.Response(200, text="")
        sse = (
            'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"Hallo"}}]}\n\n'
            'data: [DONE]\n\n'
        ).encode("utf-8")
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=fakes.SkriptStream([(self.PAUSE_S, sse)]))


class TestNichtstreamDisconnectSocket(AppTestCase):
    """Client-Close im Non-Stream-Pfad, echter uvicorn und roher Socket.

    Fall A ist der Zombie-Bug: der Close kommt erst, nachdem die Antwort
    bereits streamt (erster Leerzeichen-Keepalive ist beim Client). Dann
    schliesst Starlette den Antwort-Generator; ohne Aufraeumen bleibt der
    Job ``waiting``. Fall B (Kontrolle) schliesst VOR dem Antwortstart und
    laeuft weiter ueber den unveraenderten Wacher-Pfad.
    """

    def _server(self, transport) -> tuple[uvicorn.Server, threading.Thread]:
        """App mit skriptetem Transport als echter uvicorn im Hintergrund.

        ``timeout_graceful_shutdown`` begrenzt das Herunterfahren: sonst
        wartet uvicorn beim Stoppen auf den stummen 30-s-Upstream-Read.
        """
        app = self.app(transport=transport, enable_background=False,
                       heartbeat_s=0.2)
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=DC_PORT,
            log_level="warning", timeout_graceful_shutdown=0.5))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        ende = time.monotonic() + 5.0
        while not server.started and time.monotonic() < ende:
            time.sleep(0.02)
        self.assertTrue(server.started, "uvicorn startet nicht")
        return server, thread

    def _stop(self, server: uvicorn.Server,
              thread: threading.Thread) -> None:
        server.should_exit = True
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "uvicorn-Thread laeuft noch")

    def _raw_post(self, rid: str) -> socket.socket:
        """Roher HTTP-POST (stream=false); Socket bleibt offen."""
        sock = socket.create_connection(("127.0.0.1", DC_PORT), timeout=3.0)
        self.addCleanup(sock.close)
        body = json.dumps({
            "model": "probe", "rid": rid, "stream": False,
            "messages": [{"role": "user", "content": "Test"}],
        }).encode("utf-8")
        sock.sendall(
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{DC_PORT}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        return sock

    def _erste_keepalive_lesen(self, sock: socket.socket) -> None:
        """Liest, bis der erste Body-Chunk (Keepalive-Leerzeichen) da ist."""
        sock.settimeout(5.0)
        puffer = b""
        while b"\r\n\r\n" not in puffer:
            puffer += sock.recv(4096)
        kopf, _, puffer = puffer.partition(b"\r\n\r\n")
        self.assertTrue(kopf.startswith(b"HTTP/1.1 200"), kopf[:60])
        # Die Stream-Antwort kommt im Chunked-Format: "<hex>\r\n<bytes>\r\n".
        while b"\r\n" not in puffer:
            puffer += sock.recv(4096)
        zeile, _, puffer = puffer.partition(b"\r\n")
        groesse = int(zeile.split(b";")[0], 16)
        while len(puffer) < groesse:
            puffer += sock.recv(4096)
        self.assertGreaterEqual(groesse, 1)
        self.assertEqual(puffer[:groesse], b" " * groesse,
                         "erster Body-Chunk ist kein Keepalive")

    def _status(self, rid: str) -> str | None:
        """Job-Status ueber die HTTP-Route (laeuft im uvicorn-Thread)."""
        antwort = httpx.get(f"http://127.0.0.1:{DC_PORT}/monitor/jobs",
                            timeout=2.0, trust_env=False)
        for job in antwort.json()["jobs"]:
            if job["rid"] == rid:
                return job["status"]
        return None

    def _warte_auf(self, bedingung, *, timeout_s: float = 5.0) -> bool:
        ende = time.monotonic() + timeout_s
        while time.monotonic() < ende:
            if bedingung():
                return True
            time.sleep(0.05)
        return bedingung()

    def test_disconnect_nach_antwort_start_raeumt_job_ab(self):
        # Der Close kommt nach dem ersten Keepalive: der Generator wird von
        # Starlette geschlossen, der Job muss trotzdem aborted werden und
        # der Abort am Upstream ankommen.
        transport = _StillerSseTransport()
        server, thread = self._server(transport)
        try:
            sock = self._raw_post("dc-nach")
            self._erste_keepalive_lesen(sock)       # Antwort streamt jetzt
            sock.close()

            aborted = self._warte_auf(
                lambda: self._status("dc-nach") == "aborted")
            self.assertTrue(aborted, "Job blieb waiting (Zombie)")
            abgesagt = self._warte_auf(lambda: any(
                eintrag.get("rid") == "dc-nach"
                for eintrag in transport.aborts))
            self.assertTrue(abgesagt, "kein abort_request am Upstream")
        finally:
            self._stop(server, thread)

    def test_disconnect_vor_antwort_start_raeumt_job_ab(self):
        # Kontrolle: der Close kommt vor dem ersten Keepalive und wird vom
        # Wacher erkannt. Dessen Poll-Intervall ist hier verkuerzt: bei
        # 0,2-s-Heartbeat und 1-s-Intervall wuerde sonst der Generator-Close
        # das Rennen gewinnen (Fall A) und der Pre-Response-Pfad ungetestet
        # bleiben.
        transport = _StillerSseTransport()
        server, thread = self._server(transport)
        try:
            with mock.patch("aiserver_monitor.proxy._DISCONNECT_CHECK_S", 0.05):
                sock = self._raw_post("dc-vor")
                time.sleep(0.05)
                sock.close()
                aborted = self._warte_auf(
                    lambda: self._status("dc-vor") == "aborted")
            self.assertTrue(aborted, "Job blieb waiting (Zombie)")
        finally:
            self._stop(server, thread)


class _ServerRunner:
    """systemctl/pgrep-Ersatz fuer die Routen-Tests."""

    def __init__(self, *, unit_installiert=True, unit_aktiv=False,
                 prozess_laeuft=False, start_rc=0):
        self.unit_installiert = unit_installiert
        self.unit_aktiv = unit_aktiv
        self.prozess_laeuft = prozess_laeuft
        self.start_rc = start_rc
        self.aufrufe: list[list[str]] = []

    async def __call__(self, args):
        self.aufrufe.append(list(args))
        prefix = args[:3]
        if prefix == ["systemctl", "--user", "cat"]:
            return (0, "", "") if self.unit_installiert else (1, "", "no unit")
        if prefix == ["systemctl", "--user", "is-active"]:
            return (0, "active\n", "") if self.unit_aktiv else (3, "", "")
        if prefix == ["systemctl", "--user", "start"]:
            return (self.start_rc, "", "" if self.start_rc == 0 else "kaputt")
        if prefix == ["systemctl", "--user", "stop"]:
            return (0, "", "")
        if args[0] == "pgrep":
            return (0, "123\n", "") if self.prozess_laeuft else (1, "", "")
        return (0, "", "")


class _ServerHealth:
    def __init__(self, ok=False):
        self.ok = ok
        self.pfade: list[str] = []

    async def __call__(self, pfad: str = ""):
        self.pfade.append(pfad)
        return self.ok


class _ServerSpawner:
    def __init__(self):
        self.aufrufe: list[list[str]] = []

    def __call__(self, args):
        self.aufrufe.append(list(args))


class TestServerRoutes(AppTestCase):
    """GET/POST /monitor/server mappen auf die ServerControl-Zustaende."""

    def _control(self, *, runner=None, health=None, spawner=None):
        return ServerControl(
            upstream_base="http://127.0.0.1:30001",
            repo_root=Path("/repo"),
            runner=runner or _ServerRunner(),
            spawner=spawner or _ServerSpawner(),
            health_reader=health or _ServerHealth(ok=False),
        )

    def test_status_liefert_zustand_und_enabled(self):
        runner = _ServerRunner(unit_installiert=False)
        control = self._control(runner=runner)
        with TestClient(self.app(server_control=control)) as client:
            antwort = client.get("/monitor/server")
        self.assertEqual(antwort.status_code, 200)
        daten = antwort.json()
        self.assertEqual(daten["state"], "stopped")
        self.assertIs(daten["enabled"], True)
        self.assertEqual(daten["unit"], "escha-server.service")

    def test_start_ruft_systemctl_und_meldet_starting(self):
        runner = _ServerRunner(unit_installiert=True, unit_aktiv=False)
        control = self._control(runner=runner)
        with TestClient(self.app(server_control=control)) as client:
            antwort = client.post("/monitor/server/start")
        self.assertEqual(antwort.status_code, 202)
        self.assertEqual(antwort.json()["state"], "starting")
        self.assertIn(
            ["systemctl", "--user", "start", "--no-block",
             "escha-server.service"],
            runner.aufrufe)

    def test_stop_meldet_stopping(self):
        runner = _ServerRunner(unit_installiert=True, unit_aktiv=True,
                               prozess_laeuft=True)
        control = self._control(runner=runner, health=_ServerHealth(ok=True))
        with TestClient(self.app(server_control=control)) as client:
            antwort = client.post("/monitor/server/stop")
        self.assertEqual(antwort.status_code, 202)
        self.assertEqual(antwort.json()["state"], "stopping")

    def test_zweiter_start_ist_409(self):
        runner = _ServerRunner(unit_installiert=True, unit_aktiv=False)
        control = self._control(runner=runner)
        with TestClient(self.app(server_control=control)) as client:
            self.assertEqual(client.post("/monitor/server/start").status_code,
                             202)
            antwort = client.post("/monitor/server/start")
        self.assertEqual(antwort.status_code, 409)
        self.assertIn("error", antwort.json())

    def test_deaktivierte_steuerung_verweigert_start(self):
        control = self._control()
        with TestClient(self.app(server_control=control,
                                 server_control_enabled=False)) as client:
            status = client.get("/monitor/server")
            antwort = client.post("/monitor/server/start")
        self.assertEqual(status.status_code, 200)
        self.assertIs(status.json()["enabled"], False)
        self.assertEqual(antwort.status_code, 503)

    def test_start_fehler_ist_502_mit_meldung(self):
        runner = _ServerRunner(unit_installiert=True, unit_aktiv=False,
                               start_rc=1)
        control = self._control(runner=runner)
        with TestClient(self.app(server_control=control)) as client:
            antwort = client.post("/monitor/server/start")
        self.assertEqual(antwort.status_code, 502)
        self.assertIn("kaputt", antwort.json()["error"])

    def test_status_liefert_backend_liste(self):
        control = self._control()
        with TestClient(self.app(server_control=control)) as client:
            antwort = client.get("/monitor/server")
        daten = antwort.json()
        self.assertIsNone(daten["backend"])
        self.assertEqual([b["name"] for b in daten["backends"]],
                         ["qwen", "flint", "gptoss", "abl", "edge0"])

    def test_start_mit_backend_gptoss_startet_gptoss(self):
        runner = _ServerRunner(unit_installiert=False, unit_aktiv=False,
                               prozess_laeuft=False)
        spawner = _ServerSpawner()
        control = self._control(runner=runner, spawner=spawner)
        with TestClient(self.app(server_control=control)) as client:
            antwort = client.post("/monitor/server/start",
                                  json={"backend": "gptoss"})
        self.assertEqual(antwort.status_code, 202)
        self.assertEqual(antwort.json()["backend"], "gptoss")
        self.assertEqual(antwort.json()["state"], "starting")
        self.assertEqual(
            spawner.aufrufe,
            [["bash",
              str(Path("/repo") / "runtime/gptoss/start-gptoss.sh")]])

    def test_start_mit_backend_abl_startet_abl(self):
        runner = _ServerRunner(unit_installiert=False, unit_aktiv=False,
                               prozess_laeuft=False)
        spawner = _ServerSpawner()
        control = self._control(runner=runner, spawner=spawner)
        with TestClient(self.app(server_control=control)) as client:
            antwort = client.post("/monitor/server/start",
                                  json={"backend": "abl"})
        self.assertEqual(antwort.status_code, 202)
        self.assertEqual(antwort.json()["backend"], "abl")
        self.assertEqual(antwort.json()["state"], "starting")
        self.assertEqual(
            spawner.aufrufe,
            [["bash", str(Path("/repo") / "runtime/abl/start-abl.sh")]])

    def test_start_mit_backend_flint_startet_flint(self):
        runner = _ServerRunner(unit_installiert=False, unit_aktiv=False,
                               prozess_laeuft=False)
        spawner = _ServerSpawner()
        control = self._control(runner=runner, spawner=spawner)
        with TestClient(self.app(server_control=control)) as client:
            antwort = client.post("/monitor/server/start",
                                  json={"backend": "flint"})
        self.assertEqual(antwort.status_code, 202)
        self.assertEqual(antwort.json()["backend"], "flint")
        self.assertEqual(antwort.json()["state"], "starting")
        self.assertEqual(
            spawner.aufrufe,
            [["bash", str(Path("/repo") / "runtime/flint/start-flint.sh")]])

    def test_unbekanntes_backend_ist_400(self):
        control = self._control()
        with TestClient(self.app(server_control=control)) as client:
            antwort = client.post("/monitor/server/start",
                                  json={"backend": "llama"})
        self.assertEqual(antwort.status_code, 400)
        self.assertIn("error", antwort.json())


class TestLebenszyklusRouten(AppTestCase):
    """POST /v1/load, POST /v1/unload und GET /v1/status mappen aufs Lifecycle."""

    def _lifecycle(self):
        return _FakeLifecycle()

    def test_status_liefert_profiles_und_geladene(self):
        with TestClient(self.app(model_lifecycle=self._lifecycle())) as client:
            antwort = client.get("/v1/status")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json()["profiles"], ["a:b", "x/y"])
        self.assertEqual(antwort.json()["loaded"][0]["profile"], "a:b")

    def test_load_reicht_namen_exakt_durch(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load",
                                  json={"models": ["a:b", "x/y"]})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.load_calls, [["a:b", "x/y"]])
        self.assertEqual(antwort.json()["requested"], ["a:b", "x/y"])

    def test_load_akzeptiert_einzelnen_namen(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load", json={"model": "a:b"})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.load_calls, [["a:b"]])

    def test_load_akzeptiert_liste_als_body(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load", json=["a:b", "x/y"])
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.load_calls, [["a:b", "x/y"]])

    def test_load_pfad_mit_modellnamen(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load/Qwen3-TTS")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.load_calls, [["Qwen3-TTS"]])

    def test_load_pfad_mit_schraegstrichen_im_namen(self):
        lifecycle = self._lifecycle()
        name = "hf.co/AtomicChat/Flint-GGUF:IQ4_XS"
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post(f"/v1/load/{name}")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.load_calls, [[name]])

    def test_load_ohne_body_ist_400(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(lifecycle.load_calls, [])

    def test_load_unbekannt_ist_404_mit_profiles(self):
        lifecycle = self._lifecycle()
        lifecycle.load_exc = UnknownProfileError(["boese"], ["a:b", "x/y"])
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load", json={"models": ["boese"]})
        self.assertEqual(antwort.status_code, 404)
        self.assertEqual(antwort.json()["profiles"], ["a:b", "x/y"])

    def test_load_konflikt_ist_409(self):
        lifecycle = self._lifecycle()
        lifecycle.load_exc = LifecycleConflictError("nur ein Chat-Backend")
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load", json={"models": ["a:b", "x/y"]})
        self.assertEqual(antwort.status_code, 409)
        self.assertIn("error", antwort.json())

    def test_load_deaktiviert_ist_503(self):
        lifecycle = self._lifecycle()
        lifecycle.load_exc = LifecycleDisabledError("deaktiviert")
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load", json={"models": ["a:b"]})
        self.assertEqual(antwort.status_code, 503)

    def test_load_timeout_ist_504(self):
        lifecycle = self._lifecycle()
        lifecycle.load_exc = LifecycleTimeoutError("zu langsam")
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load", json={"models": ["a:b"]})
        self.assertEqual(antwort.status_code, 504)

    def test_load_qwen3_tts_vram_503(self):
        lifecycle = self._lifecycle()
        lifecycle.load_exc = LifecycleUnavailableError("Nicht genug VRAM")
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load", json={"model": "Qwen3-TTS"})
        self.assertEqual(antwort.status_code, 503)
        self.assertIn("VRAM", antwort.json()["error"])

    def test_load_fehler_ist_502(self):
        lifecycle = self._lifecycle()
        lifecycle.load_exc = LifecycleError("kaputt")
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/load", json={"models": ["a:b"]})
        self.assertEqual(antwort.status_code, 502)
        self.assertIn("kaputt", antwort.json()["error"])

    def test_unload_ohne_body_entlaedt_alles(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/unload")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.unload_calls, [None])
        self.assertEqual(antwort.json()["requested"], "all")

    def test_unload_leeres_dict_entlaedt_alles(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/unload", json={})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.unload_calls, [None])

    def test_unload_all_true_entlaedt_alles(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/unload", json={"all": True})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.unload_calls, [None])

    def test_unload_pfad_entlaedt_einzelnes_profil(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/unload/Qwen3-TTS")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.unload_calls, [["Qwen3-TTS"]])

    def test_unload_gezielt_reicht_namen_durch(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/unload", json={"models": ["a:b"]})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(lifecycle.unload_calls, [["a:b"]])

    def test_unload_unbekannt_ist_404(self):
        lifecycle = self._lifecycle()
        lifecycle.unload_exc = UnknownProfileError(["boese"], ["a:b"])
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/unload", json={"models": ["boese"]})
        self.assertEqual(antwort.status_code, 404)

    def test_unload_ohne_body_meldet_leere_liste(self):
        lifecycle = self._lifecycle()
        with TestClient(self.app(model_lifecycle=lifecycle)) as client:
            antwort = client.post("/v1/unload")
        self.assertEqual(antwort.json()["unloaded"], [])


class _FakeLifecycle:
    """Minimaler Lifecycle-Ersatz fuer die Routen-/Fehlermapping-Tests."""

    def __init__(self):
        self.load_calls = []
        self.unload_calls = []
        self.load_exc = None
        self.unload_exc = None

    async def status(self):
        return {
            "profiles": ["a:b", "x/y"],
            "loaded": [{"profile": "a:b", "backend": "qwen", "kind": "chat"}],
        }

    async def load(self, names):
        if self.load_exc is not None:
            raise self.load_exc
        self.load_calls.append(list(names))
        return {"requested": list(names), "profiles": ["a:b", "x/y"],
                "loaded": [], "unloaded": []}

    async def unload(self, names=None):
        if self.unload_exc is not None:
            raise self.unload_exc
        self.unload_calls.append(names)
        return {"requested": "all" if names is None else list(names),
                "unloaded": [], "loaded": [], "profiles": ["a:b", "x/y"]}


if __name__ == "__main__":
    unittest.main()
