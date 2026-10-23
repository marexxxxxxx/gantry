"""Tests fuer die SQLite-History: Schema, Mapping, Stats, Retention, Recovery."""

import os
import sqlite3
import tempfile
import unittest

from aiserver_monitor.history import History, percentile
from aiserver_monitor.jobs import JobRegistry, JobStatus, job_to_dict
from aiserver_monitor.metrics_parse import GpuSample, extract_sample

#: Spalten exakt gemaess Schema v1 (Brief/Plan).
JOBS_SPALTEN = ["rid", "source", "model", "prompt_start", "status",
                "created_at", "first_token_at", "finished_at",
                "prompt_tokens", "completion_tokens", "error"]
SAMPLES_SPALTEN = ["ts", "available", "running", "waiting", "throughput",
                   "token_usage", "gpu_util", "vram_used_mb", "vram_total_mb"]

TAG = 86400.0


class HistoryTestCase(unittest.TestCase):
    """Frische tempfile-DB je Test; Helfer fuer Job-/Sample-dicts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aiserver-monitor-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "monitor.db")
        self.history = History(self.path)
        self.history.init()
        self.addCleanup(self.history.close)

    def job(self, rid, status, created_at_ms, **felder):
        basis = {
            "rid": rid, "source": "10.0.0.1", "model": "m",
            "prompt_start": "p", "status": status,
            "created_at_ms": created_at_ms, "ttft_ms": None,
            "duration_ms": None, "prompt_tokens": None,
            "completion_tokens": None, "error": None,
        }
        basis.update(felder)
        return basis

    def sample(self, ts, **felder):
        basis = {"ts": ts, "available": True}
        basis.update(felder)
        return basis

    def db_zeilen(self, sql, params=()):
        """Liest direkt aus der DB (zweite Verbindung), unabhaengig von History."""
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class TestPercentile(unittest.TestCase):
    def test_brief_werte_p50_und_p95(self):
        werte = [10, 20, 30, 40, 100]
        self.assertEqual(percentile(werte, 50), 30)
        self.assertEqual(percentile(werte, 95), 100)

    def test_leere_liste_ist_none(self):
        self.assertIsNone(percentile([], 50))
        self.assertIsNone(percentile([], 95))

    def test_unsortiert_einzelwert_und_grenzen(self):
        self.assertEqual(percentile([40.0, 10.0, 30.0, 20.0], 50), 20.0)
        self.assertEqual(percentile([7.5], 95), 7.5)
        self.assertEqual(percentile([3.0, 1.0, 2.0], 0), 1.0)
        self.assertEqual(percentile([3.0, 1.0, 2.0], 100), 3.0)


class TestSchema(HistoryTestCase):
    def test_spalten_exakt_gemaess_schema_v1(self):
        jobs = [zeile[1] for zeile in self.db_zeilen("PRAGMA table_info(jobs)")]
        samples = [zeile[1] for zeile in self.db_zeilen("PRAGMA table_info(samples)")]
        self.assertEqual(jobs, JOBS_SPALTEN)
        self.assertEqual(samples, SAMPLES_SPALTEN)

    def test_init_ist_idempotent_und_aktiviert_wal(self):
        self.history.init()                     # zweiter Aufruf ist ein No-op
        self.assertEqual(self.db_zeilen("PRAGMA journal_mode")[0][0], "wal")
        namen = [zeile[0] for zeile in self.db_zeilen(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        self.assertEqual(namen, ["jobs", "samples", "settings"])

    def test_init_legt_fehlende_verzeichnisse_an(self):
        # Default-Pfad "data/monitor.db": das Verzeichnis existiert frisch nicht.
        pfad = os.path.join(self.tmp.name, "neu", "tief", "monitor.db")
        history = History(pfad)
        history.init()
        self.addCleanup(history.close)
        self.assertTrue(os.path.exists(pfad))


class TestSettings(HistoryTestCase):
    """Allgemeine Einstellungen (Key/Value) ueberleben Close/Init."""

    def test_unbekannter_schluessel_ist_none(self):
        self.assertIsNone(self.history.get_setting("fehlt"))

    def test_setting_wird_gespeichert_und_ueberschrieben(self):
        self.history.set_setting("priority", "eins")
        self.assertEqual(self.history.get_setting("priority"), "eins")
        self.history.set_setting("priority", "zwei")
        self.assertEqual(self.history.get_setting("priority"), "zwei")

    def test_setting_ueberlebt_close_und_neues_init(self):
        self.history.set_setting("priority", "gespeichert")
        self.history.close()
        neu = History(self.path)
        neu.init()
        self.addCleanup(neu.close)
        self.assertEqual(neu.get_setting("priority"), "gespeichert")


class TestRecordJob(HistoryTestCase):
    def test_speichert_spalten_und_leitet_zeiten_ab(self):
        self.history.record_job(self.job(
            "r1", "done", 1_000_000, ttft_ms=2000.0, duration_ms=9000.0,
            prompt_tokens=11, completion_tokens=7))
        zeile = self.db_zeilen("SELECT * FROM jobs")[0]
        self.assertEqual(zeile, ("r1", "10.0.0.1", "m", "p", "done",
                                 1000.0, 1002.0, 1009.0, 11, 7, None))

    def test_ohne_ttft_und_dauer_bleiben_die_zeiten_null(self):
        self.history.record_job(self.job("r2", "waiting", 2_000_000))
        zeile = self.db_zeilen("SELECT created_at, first_token_at, finished_at FROM jobs")[0]
        self.assertEqual(zeile, (2000.0, None, None))

    def test_full_prompt_und_vorschauen_werden_nicht_persistiert(self):
        self.history.record_job(self.job(
            "r3", "running", 3_000_000, prompt_start="x" * 100,
            full_prompt="x" * 100 + "GEHEIMER-PROMPT",
            content_preview="GEHEIME-ANTWORT",
            reasoning_preview="GEHEIMES-DENKEN", tok_per_s=1.5))
        self.assertEqual(self.db_zeilen("SELECT prompt_start FROM jobs")[0][0], "x" * 100)
        # Kein Volltext in der Datei: vorher schliessen (WAL-Checkpoint).
        self.history.close()
        with open(self.path, "rb") as fh:
            rohdaten = fh.read()
        self.assertNotIn(b"GEHEIMER-PROMPT", rohdaten)
        self.assertNotIn(b"GEHEIME-ANTWORT", rohdaten)
        self.assertNotIn(b"GEHEIMES-DENKEN", rohdaten)

    def test_insert_or_replace_auf_rid(self):
        self.history.record_job(self.job("r4", "waiting", 4_000_000))
        self.history.record_job(self.job(
            "r4", "done", 4_000_000, ttft_ms=1000.0, duration_ms=5000.0,
            completion_tokens=3))
        zeilen = self.db_zeilen("SELECT rid, status, finished_at, completion_tokens FROM jobs")
        self.assertEqual(zeilen, [("r4", "done", 4005.0, 3)])

    def test_unbekannte_keys_werden_ignoriert(self):
        self.history.record_job(self.job("r5", "aborted", 5_000_000,
                                         extra="unbekannt"))
        self.assertEqual(self.db_zeilen("SELECT COUNT(*) FROM jobs")[0][0], 1)


class TestRecordSample(HistoryTestCase):
    def test_speichert_spalten_und_ignoriert_extras(self):
        self.history.record_sample(self.sample(
            100.0, running=2, waiting=5, throughput=41.5, token_usage=4096.0,
            gpu_util=12, vram_used_mb=3456, vram_total_mb=16384,
            max_tokens=32768.0))
        zeile = self.db_zeilen("SELECT * FROM samples")[0]
        self.assertEqual(zeile, (100.0, 1, 2, 5, 41.5, 4096.0, 12, 3456, 16384))

    def test_fehlende_keys_werden_null_nie_null_wert(self):
        self.history.record_sample(self.sample(101.0))
        zeile = self.db_zeilen("SELECT * FROM samples")[0]
        self.assertEqual(zeile, (101.0, 1, None, None, None, None, None, None, None))

    def test_nicht_verfuegbares_sample(self):
        self.history.record_sample(self.sample(102.0, available=False))
        zeile = self.db_zeilen("SELECT available, running FROM samples")[0]
        self.assertEqual(zeile, (0, None))

    def test_insert_or_replace_auf_ts(self):
        self.history.record_sample(self.sample(103.0, running=1))
        self.history.record_sample(self.sample(103.0, running=9))
        self.assertEqual(self.db_zeilen("SELECT ts, running FROM samples"), [(103.0, 9)])


class TestMarkStaleRunning(HistoryTestCase):
    def test_markiert_waiting_und_running_und_liefert_anzahl(self):
        self.history.record_job(self.job("w", "waiting", 100_000))
        self.history.record_job(self.job("r", "running", 200_000, ttft_ms=10.0))
        self.history.record_job(self.job("d", "done", 300_000, duration_ms=1000.0))
        self.history.record_job(self.job("a", "aborted", 400_000, duration_ms=2000.0))
        anzahl = self.history.mark_stale_running(5000.0)
        self.assertEqual(anzahl, 2)
        zeilen = {zeile[0]: zeile for zeile in self.db_zeilen(
            "SELECT rid, status, error, finished_at FROM jobs")}
        self.assertEqual(zeilen["w"], ("w", "error", "gateway restart", 5000.0))
        self.assertEqual(zeilen["r"], ("r", "error", "gateway restart", 5000.0))
        self.assertEqual(zeilen["d"], ("d", "done", None, 301.0))
        self.assertEqual(zeilen["a"], ("a", "aborted", None, 402.0))
        # TTFT des wartenden Jobs bleibt ungemessen (nie 0).
        ttft = self.db_zeilen("SELECT first_token_at FROM jobs WHERE rid = 'w'")[0][0]
        self.assertIsNone(ttft)

    def test_ohne_kandidaten_null(self):
        self.history.record_job(self.job("d", "done", 300_000, duration_ms=1000.0))
        self.assertEqual(self.history.mark_stale_running(5000.0), 0)

    def test_vorhandenes_finished_at_bleibt_erhalten(self):
        # Defensiv: eine inkonsistente Zeile (Status laeuft, Ende schon gesetzt)
        # behaelt ihr Ende statt den Neustart-Zeitpunkt zu bekommen.
        self.history.record_job(self.job("r", "running", 200_000, duration_ms=1000.0))
        self.assertEqual(self.history.mark_stale_running(5000.0), 1)
        zeile = self.db_zeilen("SELECT status, finished_at FROM jobs")[0]
        self.assertEqual(zeile, ("error", 201.0))


class TestCleanup(HistoryTestCase):
    def test_loescht_alte_jobs_und_samples(self):
        now = 10_000_000.0
        self.history.record_job(self.job("alt", "done", int((now - 31 * TAG) * 1000)))
        self.history.record_job(self.job("jung", "done", int((now - 29 * TAG) * 1000)))
        self.history.record_sample(self.sample(now - 8 * TAG))
        self.history.record_sample(self.sample(now - 6 * TAG))
        self.assertEqual(self.history.cleanup(now), (1, 1))
        self.assertEqual(self.db_zeilen("SELECT rid FROM jobs"), [("jung",)])
        self.assertEqual(self.db_zeilen("SELECT ts FROM samples"), [(now - 6 * TAG,)])

    def test_cutoff_selbst_bleibt_erhalten(self):
        now = 20_000_000.0
        self.history.record_job(self.job("grenze", "done", int((now - 30 * TAG) * 1000)))
        self.history.record_sample(self.sample(now - 7 * TAG))
        self.assertEqual(self.history.cleanup(now), (0, 0))

    def test_retention_ist_konfigurierbar(self):
        now = 30_000_000.0
        kurz = History(os.path.join(self.tmp.name, "kurz.db"),
                       retention_jobs_days=1, retention_series_days=1)
        kurz.init()
        self.addCleanup(kurz.close)
        kurz.record_job(self.job("alt", "done", int((now - 2 * TAG) * 1000)))
        kurz.record_job(self.job("jung", "done", int((now - 0.5 * TAG) * 1000)))
        kurz.record_sample(self.sample(now - 2 * TAG))
        kurz.record_sample(self.sample(now - 0.5 * TAG))
        self.assertEqual(kurz.cleanup(now), (1, 1))


class TestQueryStats(HistoryTestCase):
    def setUp(self):
        super().setUp()
        # Drei Jobs im Fenster [1000, 1100] ...
        self.history.record_job(self.job(
            "j1", "done", 1_000_000, ttft_ms=2000.0, duration_ms=9000.0,
            prompt_tokens=11, completion_tokens=7))
        self.history.record_job(self.job(
            "j2", "done", 1_010_000, ttft_ms=4000.0, duration_ms=20000.0,
            prompt_tokens=3, completion_tokens=5))
        self.history.record_job(self.job(
            "j3", "aborted", 1_020_000, duration_ms=1000.0))
        # ... und ein Fehler-Job ausserhalb (ohne Token-Messwerte).
        self.history.record_job(self.job(
            "j4", "error", 2_000_000, duration_ms=1000.0, error="upstream weg"))

    def test_overview_im_fenster(self):
        stats = self.history.query_stats(1000.0, 1100.0, 60)
        self.assertEqual(stats["overview"], {
            "count": 3, "done": 2, "aborted": 1, "error": 0,
            "tokens_in": 14, "tokens_out": 12,
            "ttft": {"p50": 2.0, "p95": 4.0},
            "duration": {"p50": 9.0, "p95": 20.0},
        })
        self.assertEqual(stats["buckets"], [])

    def test_grenzen_inklusiv_und_fehler_job_zaehlt(self):
        stats = self.history.query_stats(1000.0, 2000.0, 60)
        self.assertEqual(stats["overview"]["count"], 4)
        self.assertEqual(stats["overview"]["error"], 1)
        self.assertEqual(stats["overview"]["tokens_in"], 14)
        self.assertEqual(stats["overview"]["tokens_out"], 12)
        # Nur der Fehler-Job: keine TTFT, Dauer 1 s, Token-Summen ungemessen.
        nur_j4 = self.history.query_stats(1900.0, 2100.0, 60)
        self.assertEqual(nur_j4["overview"]["count"], 1)
        self.assertIsNone(nur_j4["overview"]["tokens_in"])
        self.assertIsNone(nur_j4["overview"]["tokens_out"])
        self.assertIsNone(nur_j4["overview"]["ttft"]["p50"])
        self.assertIsNone(nur_j4["overview"]["ttft"]["p95"])
        self.assertEqual(nur_j4["overview"]["duration"], {"p50": 1.0, "p95": 1.0})

    def test_leeres_fenster(self):
        stats = self.history.query_stats(2100.0, 2200.0, 60)
        self.assertEqual(stats["overview"], {
            "count": 0, "done": 0, "aborted": 0, "error": 0,
            "tokens_in": None, "tokens_out": None,
            "ttft": {"p50": None, "p95": None},
            "duration": {"p50": None, "p95": None},
        })
        self.assertEqual(stats["buckets"], [])

    def test_buckets_mitteln_je_fenster(self):
        self.history.record_sample(self.sample(
            100.0, running=2, waiting=4, throughput=10.0,
            gpu_util=50, vram_used_mb=1000, vram_total_mb=16000))
        self.history.record_sample(self.sample(
            130.0, running=4, throughput=20.0, token_usage=100.0,
            gpu_util=70, vram_used_mb=1200))
        self.history.record_sample(self.sample(160.0, available=False))
        stats = self.history.query_stats(100.0, 200.0, 60)
        self.assertEqual(stats["buckets"], [
            {"ts": 60, "available": 1.0, "running": 2.0, "waiting": 4.0,
             "throughput": 10.0, "token_usage": None, "gpu_util": 50.0,
             "vram_used_mb": 1000.0, "vram_total_mb": 16000.0},
            {"ts": 120, "available": 0.5, "running": 4.0, "waiting": None,
             "throughput": 20.0, "token_usage": 100.0, "gpu_util": 70.0,
             "vram_used_mb": 1200.0, "vram_total_mb": None},
        ])

    def test_bucket_muss_positiv_sein(self):
        with self.assertRaises(ValueError):
            self.history.query_stats(0.0, 100.0, 0)


class TestLastSample(HistoryTestCase):
    """R-F: letztes Sample fuer den SSE-Snapshot (gleiche Keys wie Buckets)."""

    def test_leere_tabelle_ist_none(self):
        self.assertIsNone(self.history.last_sample())

    def test_liefert_die_letzte_zeile(self):
        self.history.record_sample(self.sample(
            100.0, running=2, waiting=1, throughput=41.5, token_usage=0.25,
            gpu_util=12, vram_used_mb=3456, vram_total_mb=16384))
        self.history.record_sample(self.sample(
            200.0, available=False, running=5, waiting=3, throughput=10.0,
            token_usage=0.5, gpu_util=80, vram_used_mb=4096,
            vram_total_mb=16384))
        self.assertEqual(self.history.last_sample(), {
            "ts": 200.0, "available": False, "running": 5, "waiting": 3,
            "throughput": 10.0, "token_usage": 0.5, "gpu_util": 80,
            "vram_used_mb": 4096, "vram_total_mb": 16384})

    def test_fehlende_werte_sind_none_und_available_ist_bool(self):
        self.history.record_sample(self.sample(300.0))
        letzte = self.history.last_sample()
        self.assertEqual(letzte["running"], None)
        self.assertEqual(letzte["token_usage"], None)
        self.assertIs(letzte["available"], True)

    def test_keys_deckungsgleich_mit_bucket_eintraegen(self):
        self.history.record_sample(self.sample(100.0, running=2))
        letzte = self.history.last_sample()
        bucket = self.history.query_stats(0.0, 200.0, 60)["buckets"][0]
        self.assertEqual(set(letzte), set(bucket))

    def test_buckets_nur_im_fenster(self):
        self.history.record_sample(self.sample(99.0, running=1))
        self.history.record_sample(self.sample(100.0, running=2))
        self.history.record_sample(self.sample(200.0, running=3))
        stats = self.history.query_stats(100.0, 200.0, 60)
        self.assertEqual([bucket["ts"] for bucket in stats["buckets"]], [60, 180])
        # 99.0 liegt vor dem Fenster und darf das Fenster 60 nicht verwaessern.
        self.assertEqual(stats["buckets"][0]["running"], 2.0)


class TestProducerMapping(HistoryTestCase):
    """Vertrag mit Task 2/Task 3: echte job_to_dict-/extract_sample-Dicts."""

    def test_job_to_dict_roundtrip(self):
        reg = JobRegistry(preview_max_chars=5)
        reg.register("t2", source="10.0.0.9", model="m",
                     full_prompt="p" * 150, now=1000.0)
        reg.note_running("t2", now=1002.0)
        reg.finish("t2", JobStatus.DONE, prompt_tokens=11,
                   completion_tokens=7, now=1009.0)
        self.history.record_job(job_to_dict(reg.get("t2")))
        zeile = self.db_zeilen("SELECT * FROM jobs")[0]
        self.assertEqual(zeile, ("t2", "10.0.0.9", "m", "p" * 100, "done",
                                 1000.0, 1002.0, 1009.0, 11, 7, None))

    def test_extract_sample_roundtrip(self):
        sample = extract_sample(
            {"sglang:num_running_reqs": 2.0, "sglang:gen_throughput": 41.5,
             "sglang:max_total_num_tokens": 32768.0},
            GpuSample(12, 3456, 16384), ts=100.5, available=True)
        self.history.record_sample(sample)
        zeile = self.db_zeilen("SELECT * FROM samples")[0]
        self.assertEqual(zeile, (100.5, 1, 2, None, 41.5, None, 12, 3456, 16384))


if __name__ == "__main__":
    unittest.main()
