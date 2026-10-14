"""Tests fuer den Metrik-Sammler (collector.py).

Der Upstream ist der Fake-SGLang aus ``fakes.py``: ueber
``httpx.ASGITransport`` liefert er ``/metrics`` und ``/health``;
``json_transport`` mit ``httpx.ConnectError`` steht fuer "SGLang aus". Es
laeuft kein Socket. Die GPU kommt als injizierter ``gpu_reader``, damit kein
nvidia-smi laeuft. Nur der Test des Standard-Readers startet einen
Ersatzbefehl (kleines Shell-Skript), ebenfalls kein nvidia-smi.
"""

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

import fakes
from aiserver_monitor.collector import Collector
from aiserver_monitor.config import Config
from aiserver_monitor.events import EventHub
from aiserver_monitor.history import History
from aiserver_monitor.jobs import JobRegistry
from aiserver_monitor.metrics_parse import GpuSample
from aiserver_monitor.proxy import Deps


class _SampleRecorder:
    """Minimaler History-Ersatz: sammelt die Samples aus record_sample."""

    def __init__(self):
        self.samples: list[dict] = []

    def record_sample(self, sample: dict) -> None:
        self.samples.append(sample)


class _FakeGpuReader:
    """Injizierter GPU-Reader: liefert feste Werte und zaehlt seine Aufrufe."""

    def __init__(self, util_pct: int | None = None,
                 vram_used_mb: int | None = None,
                 vram_total_mb: int | None = None):
        self.sample = GpuSample(util_pct, vram_used_mb, vram_total_mb)
        self.aufrufe = 0

    async def __call__(self) -> GpuSample:
        self.aufrufe += 1
        return self.sample


class _FehlerhafterGpuReader:
    """Scheitert genau beim ersten Aufruf; danach liefert er feste Werte."""

    def __init__(self):
        self.aufrufe = 0

    async def __call__(self) -> GpuSample:
        self.aufrufe += 1
        if self.aufrufe == 1:
            raise RuntimeError("GPU nicht lesbar")
        return GpuSample(5, 100, 200)


def _frames(queue: asyncio.Queue[str]) -> list[tuple[str, dict]]:
    """Alle bereitliegenden Hub-Frames als (event-Kopf, Payload)."""
    frames = []
    while not queue.empty():
        rahmen = queue.get_nowait()
        kopf, daten = rahmen.split("data: ", 1)
        frames.append((kopf.strip(), json.loads(daten)))
    return frames


async def _warte_bis(predicate, *, timeout_s: float = 2.0) -> bool:
    """Wartet bis zur Zeitgrenze darauf, dass ``predicate()`` wahr wird."""
    ende = time.monotonic() + timeout_s
    while time.monotonic() < ende:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class CollectorTestBasis(unittest.IsolatedAsyncioTestCase):
    """Gemeinsamer Aufbau: Hub, Sample-Recorder und Deps je Test."""

    async def asyncSetUp(self):
        self.hub = EventHub()
        self.queue = self.hub.subscribe()
        self.history = _SampleRecorder()

    def deps(self, transport: httpx.AsyncBaseTransport, *,
             config: Config | None = None) -> Deps:
        """Deps mit dem Transport; die Basis-URL endet auf einem Schraegstrich.

        Der Schraegstrich prueft den ``rstrip`` des Sammlers mit; der Client
        wird am Testende geschlossen.
        """
        client = httpx.AsyncClient(transport=transport)
        self.addAsyncCleanup(client.aclose)
        return Deps(
            config=config if config is not None else Config(
                upstream_base="http://upstream.test/"),
            registry=JobRegistry(preview_max_chars=4096),
            hub=self.hub,
            history=self.history,
            client=client,
        )


class TestSampleOnce(CollectorTestBasis):
    async def test_upstream_erreichbar_liefert_alle_messwerte(self):
        """Beide Endpunkte 200: Sample mit allen Werten, ein Stats-Frame."""
        fake = fakes.FakeSglang()
        collector = Collector(self.deps(httpx.ASGITransport(app=fake.app)),
                              gpu_reader=_FakeGpuReader(12, 3456, 16384))

        sample = await collector.sample_once()

        # Ohne Pfad-Doppelung trotz Basis-URL mit Schraegstrich.
        self.assertEqual(fake.pfade, ["/metrics", "/health"])
        self.assertIsInstance(sample["ts"], float)
        self.assertAlmostEqual(sample["ts"], time.time(), delta=5.0)
        self.assertIs(sample["available"], True)
        self.assertEqual(sample["running"], 3)
        self.assertIsInstance(sample["running"], int)
        self.assertEqual(sample["waiting"], 1)
        self.assertEqual(sample["throughput"], 42.5)
        self.assertEqual(sample["token_usage"], 0.25)
        self.assertEqual(sample["max_tokens"], 32768.0)
        self.assertEqual(sample["gpu_util"], 12)
        self.assertEqual(sample["vram_used_mb"], 3456)
        self.assertEqual(sample["vram_total_mb"], 16384)
        self.assertEqual(_frames(self.queue), [("event: stats", sample)])
        self.assertEqual(self.history.samples, [sample])

    async def test_upstream_aus_ergibt_nicht_verfuegbar_ohne_exception(self):
        """Upstream weg: available=False, GPU-Werte bleiben, kein Fehler."""
        collector = Collector(
            self.deps(fakes.json_transport(
                fehler=httpx.ConnectError("Verbindung abgelehnt"))),
            gpu_reader=_FakeGpuReader(12, 3456, 16384))

        with self.assertLogs("aiserver_monitor.collector", level="WARNING"):
            sample = await collector.sample_once()

        self.assertIs(sample["available"], False)
        self.assertIsInstance(sample["ts"], float)
        for key in ("running", "waiting", "throughput", "token_usage",
                    "max_tokens"):
            self.assertNotIn(key, sample)
        self.assertEqual(sample["gpu_util"], 12)
        self.assertEqual(sample["vram_used_mb"], 3456)
        self.assertEqual(sample["vram_total_mb"], 16384)
        # Trotz "Upstream aus": Sample geht an Hub und History.
        self.assertEqual(_frames(self.queue), [("event: stats", sample)])
        self.assertEqual(self.history.samples, [sample])

    async def test_nicht_200_von_einem_endpunkt_ergibt_nicht_verfuegbar(self):
        """available=True verlangt HTTP 200 von /metrics UND /health."""
        for kaputter_pfad in ("/metrics", "/health"):
            with self.subTest(pfad=kaputter_pfad):
                def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.path == kaputter_pfad:
                        return httpx.Response(500)
                    return httpx.Response(
                        200, content=fakes.meter_text().encode("utf-8"))

                collector = Collector(self.deps(httpx.MockTransport(handler)),
                                      gpu_reader=_FakeGpuReader())
                with self.assertLogs("aiserver_monitor.collector",
                                     level="WARNING"):
                    sample = await collector.sample_once()

                self.assertIs(sample["available"], False)
                self.assertEqual(set(sample), {"ts", "available"})
                self.assertEqual(_frames(self.queue),
                                 [("event: stats", sample)])

    async def test_injizierter_gpu_reader_ersetzt_nvidia_smi(self):
        """Der Reader wird genau einmal genutzt; sein gpu_cmd laeuft nie."""
        fake = fakes.FakeSglang()
        deps = self.deps(httpx.ASGITransport(app=fake.app))
        # Ein Kommando, das es nicht gibt: Liefe der Standard-Reader, fehlten
        # die GPU-Keys statt der Werte des injizierten Readers.
        deps.config.gpu_cmd = "gibt-es-definitiv-nicht-xyz"
        gpu = _FakeGpuReader(7, 1024, 8192)

        sample = await Collector(deps, gpu_reader=gpu).sample_once()

        self.assertEqual(gpu.aufrufe, 1)
        self.assertEqual(sample["gpu_util"], 7)
        self.assertEqual(sample["vram_used_mb"], 1024)
        self.assertEqual(sample["vram_total_mb"], 8192)

    async def test_ohne_history_wird_nur_publiziert(self):
        """deps.history=None: kein record_sample, aber ein Stats-Frame."""
        fake = fakes.FakeSglang()
        deps = self.deps(httpx.ASGITransport(app=fake.app))
        deps.history = None

        sample = await Collector(deps,
                                 gpu_reader=_FakeGpuReader()).sample_once()

        self.assertIs(sample["available"], True)
        self.assertEqual(self.history.samples, [])
        self.assertEqual(_frames(self.queue), [("event: stats", sample)])


class TestEigenerClient(CollectorTestBasis):
    """R5: der Sammler kann einen eigenen httpx-Client (eigener Pool) nutzen."""

    async def test_injizierter_client_nutzt_dessen_transport(self):
        # Der Sammler geht ueber seinen eigenen Client; der Transport von
        # deps.client bleibt komplett unberuehrt (kein geteilter Pool).
        fake = fakes.FakeSglang()
        eigener = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app))
        self.addAsyncCleanup(eigener.aclose)
        aufrufe: list[str] = []

        def fremder_handler(request: httpx.Request) -> httpx.Response:
            aufrufe.append(str(request.url))
            return httpx.Response(404)          # nur der Fake misst richtig

        collector = Collector(self.deps(httpx.MockTransport(fremder_handler)),
                              gpu_reader=_FakeGpuReader(1, 2, 3),
                              client=eigener)

        sample = await collector.sample_once()

        self.assertEqual(fake.pfade, ["/metrics", "/health"])
        self.assertIs(sample["available"], True)
        self.assertEqual(sample["running"], 3)
        self.assertEqual(aufrufe, [])           # deps.client blieb unberuehrt


class TestStandardGpuReader(CollectorTestBasis):
    async def test_gpu_cmd_liefert_auslastung_und_vram(self):
        """Standard-Reader: CSV-Ausgabe von config.gpu_cmd wird zum GpuSample.

        Statt nvidia-smi laeuft ein kleines Skript mit derselben Ausgabe.
        """
        with tempfile.TemporaryDirectory() as tmp:
            skript = Path(tmp) / "fake-nvidia-smi"
            skript.write_text("#!/bin/sh\necho '12, 3456, 16384'\n")
            skript.chmod(0o755)
            config = Config(upstream_base="http://upstream.test/",
                            gpu_cmd=str(skript))

            sample = await Collector(self.deps(
                httpx.ASGITransport(app=fakes.FakeSglang().app),
                config=config)).sample_once()

        self.assertEqual(sample["gpu_util"], 12)
        self.assertEqual(sample["vram_used_mb"], 3456)
        self.assertEqual(sample["vram_total_mb"], 16384)

    async def test_fehler_des_readers_ergeben_none_werte(self):
        """Fehlendes Kommando, Status ungleich 0, unlesbare Ausgabe: None.

        ``sys.executable`` mit den unbekannten nvidia-smi-Optionen endet
        garantiert ungleich 0, ohne dass ein externes Kommando noetig ist;
        das Skript mit dem rohen 0xFF-Byte liefert kein UTF-8.
        """
        with tempfile.TemporaryDirectory() as tmp:
            kaputt = Path(tmp) / "kaputt"
            # printf '\377' schreibt das rohe Byte 0xFF: kein gueltiges UTF-8.
            kaputt.write_text("#!/bin/sh\nprintf '\\377'\n")
            kaputt.chmod(0o755)

            for gpu_cmd in ("gibt-es-definitiv-nicht-xyz", sys.executable,
                            str(kaputt)):
                with self.subTest(gpu_cmd=gpu_cmd):
                    config = Config(upstream_base="http://upstream.test/",
                                    gpu_cmd=gpu_cmd)
                    collector = Collector(self.deps(
                        httpx.ASGITransport(app=fakes.FakeSglang().app),
                        config=config))

                    with self.assertLogs("aiserver_monitor.collector",
                                         level="WARNING"):
                        sample = await collector.sample_once()

                    # Der Upstream selbst war erreichbar; nur die GPU fehlt.
                    self.assertIs(sample["available"], True)
                    self.assertEqual(sample["running"], 3)
                    for key in ("gpu_util", "vram_used_mb", "vram_total_mb"):
                        self.assertNotIn(key, sample)

    async def test_leere_ausgabe_ergibt_none_werte(self):
        """Leere Ausgabe: None-Werte, nie 0 und keine Exception."""
        with tempfile.TemporaryDirectory() as tmp:
            skript = Path(tmp) / "leer"
            skript.write_text("#!/bin/sh\nexit 0\n")
            skript.chmod(0o755)
            config = Config(upstream_base="http://upstream.test/",
                            gpu_cmd=str(skript))

            sample = await Collector(self.deps(
                httpx.ASGITransport(app=fakes.FakeSglang().app),
                config=config)).sample_once()

        for key in ("gpu_util", "vram_used_mb", "vram_total_mb"):
            self.assertNotIn(key, sample)

    async def test_haengendes_kommando_ergibt_timeout_und_none_werte(self):
        """Ein haengendes gpu_cmd wird nach _GPU_TIMEOUT_S beendet.

        Das Wrapper-Skript macht per ``exec`` direkt ``sleep`` (wie
        ``nvidia-smi`` selbst, kein Enkelprozess); das Limit wird klein
        gepatcht. Der Messdurchlauf wartet die 5 s nicht ab, die GPU-Werte
        bleiben None und der Upstream-Teil ist heil.
        """
        with tempfile.TemporaryDirectory() as tmp:
            skript = Path(tmp) / "haengt"
            skript.write_text("#!/bin/sh\nexec sleep 5\n")
            skript.chmod(0o755)
            config = Config(upstream_base="http://upstream.test/",
                            gpu_cmd=str(skript))
            collector = Collector(self.deps(
                httpx.ASGITransport(app=fakes.FakeSglang().app),
                config=config))

            with mock.patch("aiserver_monitor.collector._GPU_TIMEOUT_S", 0.2):
                start = time.monotonic()
                with self.assertLogs("aiserver_monitor.collector",
                                     level="WARNING") as logs:
                    sample = await collector.sample_once()
                dauer = time.monotonic() - start

        self.assertLess(dauer, 3.0)                     # nicht die 5 s
        self.assertTrue(any("Timeout" in zeile for zeile in logs.output))
        self.assertIs(sample["available"], True)
        self.assertEqual(sample["running"], 3)
        for key in ("gpu_util", "vram_used_mb", "vram_total_mb"):
            self.assertNotIn(key, sample)


class TestRunLoop(CollectorTestBasis):
    async def test_erster_durchlauf_sofort_und_cancel_kommt_heraus(self):
        """run() misst sofort (lange poll_interval_s) und laesst Cancel
        durch."""
        deps = self.deps(
            fakes.json_transport(
                fehler=httpx.ConnectError("Verbindung abgelehnt")),
            config=Config(upstream_base="http://upstream.test/",
                          poll_interval_s=10.0))
        collector = Collector(deps, gpu_reader=_FakeGpuReader())

        with self.assertLogs("aiserver_monitor.collector", level="WARNING"):
            aufgabe = asyncio.create_task(collector.run())
            try:
                erreicht = await _warte_bis(
                    lambda: len(self.history.samples) >= 1, timeout_s=2.0)
                self.assertTrue(erreicht, "run() misst nicht sofort")
                self.assertFalse(aufgabe.done())
            finally:
                aufgabe.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await aufgabe

        self.assertTrue(aufgabe.cancelled())
        # Minimales Sample: nur ts und available, keine erfundenen Werte.
        self.assertEqual(set(self.history.samples[0]), {"ts", "available"})
        self.assertIs(self.history.samples[0]["available"], False)

    async def test_fehler_eines_durchlaufs_beendet_die_schleife_nicht(self):
        """Ein Fehler in sample_once wird geloggt; der naechste Durchlauf
        misst."""
        deps = self.deps(httpx.ASGITransport(app=fakes.FakeSglang().app),
                         config=Config(upstream_base="http://upstream.test/",
                                       poll_interval_s=0.01))
        gpu = _FehlerhafterGpuReader()
        collector = Collector(deps, gpu_reader=gpu)

        with self.assertLogs("aiserver_monitor.collector",
                             level="WARNING") as logs:
            aufgabe = asyncio.create_task(collector.run())
            try:
                erreicht = await _warte_bis(
                    lambda: len(self.history.samples) >= 1, timeout_s=2.0)
                self.assertTrue(erreicht, "run() erholt sich nicht vom Fehler")
                self.assertFalse(aufgabe.done())
            finally:
                aufgabe.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await aufgabe

        self.assertGreaterEqual(gpu.aufrufe, 2)
        self.assertIn("Messdurchlauf fehlgeschlagen", logs.output[0])
        self.assertIn("RuntimeError", logs.output[0])
        # Nach dem Fehler laeuft die Schleife weiter: History und Hub passen.
        self.assertEqual(self.history.samples[0]["running"], 3)
        self.assertEqual(self.history.samples[0]["gpu_util"], 5)
        self.assertEqual([payload for _, payload in _frames(self.queue)],
                         self.history.samples)

    async def test_periodischer_cleanup_loescht_alte_zeilen(self):
        """run() raeumt die Retention im Betrieb auf, nicht nur beim Start.

        Echte temp-SQLite mit altem Job und altem Sample; das Intervall wird
        klein gepatcht. Danach: alte Zeilen weg, Schleife laeuft und laesst
        sich abbrechen.
        """
        with tempfile.TemporaryDirectory() as tmp:
            pfad = Path(tmp) / "monitor.db"
            hist = History(pfad, retention_jobs_days=1,
                           retention_series_days=1)
            hist.init()
            self.addCleanup(hist.close)
            alt = time.time() - 10 * 86400
            hist.record_job({
                "rid": "alt", "source": "s", "model": "m", "prompt_start": "p",
                "status": "done", "created_at_ms": int(alt * 1000)})
            hist.record_sample({"ts": alt, "available": True})

            def alte_zeilen_weg() -> bool:
                conn = sqlite3.connect(pfad)
                try:
                    jobs = conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE rid = 'alt'"
                    ).fetchone()[0]
                    samples = conn.execute(
                        "SELECT COUNT(*) FROM samples WHERE ts = ?",
                        (alt,)).fetchone()[0]
                finally:
                    conn.close()
                return jobs == 0 and samples == 0

            deps = self.deps(
                httpx.ASGITransport(app=fakes.FakeSglang().app),
                config=Config(upstream_base="http://upstream.test/",
                              poll_interval_s=0.01))
            deps.history = hist
            collector = Collector(deps, gpu_reader=_FakeGpuReader())

            with mock.patch(
                    "aiserver_monitor.collector._CLEANUP_ABSTAND_S", 0.05):
                aufgabe = asyncio.create_task(collector.run())
                try:
                    erreicht = await _warte_bis(alte_zeilen_weg, timeout_s=2.0)
                    self.assertTrue(
                        erreicht, "Cleanup loeschte die alten Zeilen nicht")
                    self.assertFalse(aufgabe.done())
                finally:
                    aufgabe.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await aufgabe

            self.assertTrue(aufgabe.cancelled())
            # Die neuen Samples des laufenden Betriebs bleiben in der DB.
            self.assertIsNotNone(hist.last_sample())


if __name__ == "__main__":
    unittest.main()
