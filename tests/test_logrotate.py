"""Tests fuer die Log-Aufraeumung.

Kein Test hier fasst das echte Log an. Alle Fixtures liegen in
``tempfile.mkdtemp()``, also unter /tmp mit einem zufaelligen Namen; der
Pfadwaechter in ``logrotate.is_allowed`` verlangt zusaetzlich einen der beiden
erlaubten Basenamen, sodass ein Fehler in diesem Modul nicht auf
``/tmp/escha_server_lan.log`` durchschlagen kann. ``TestPfadwaechter`` haelt
genau das fest.
"""

import os
import shutil
import tempfile
import unittest

from slotmon.logrotate import LogRotator, RotateResult, is_allowed, should_check

#: Wo die beiden echten Pfade aus der Konfiguration liegen. Nur fuer den
#: Wächter befragt - ``check()`` wird nie darauf aufgerufen.
SERVER_LOG = "/tmp/escha_server_lan.log"
MONITOR_LOG = "/tmp/slotmon.log"


class RotatorCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotmon-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "escha_server_lan.log")
        self.addCleanup(self._close_if_open)
        self.handle = None

    def _close_if_open(self):
        if self.handle is not None and not self.handle.closed:
            self.handle.close()

    def write_mb(self, size_mb, pattern="x"):
        # int(...): 4.5 * 1024 * 1024 ist ein float, und "x" * float wirft
        # TypeError. Der Bruchteil geht als Nachkommanul lost - weg.
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(pattern * int(size_mb * 1024 * 1024))


class TestThreshold(RotatorCase):
    def test_below_threshold_does_not_truncate(self):
        self.write_mb(3)
        result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertFalse(result.truncated)
        self.assertAlmostEqual(result.size_mb, 3.0, delta=0.5)
        self.assertGreater(os.path.getsize(self.path), 0)

    def test_above_threshold_truncates(self):
        self.write_mb(8)
        result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertTrue(result.truncated)
        self.assertEqual(os.path.getsize(self.path), 0)

    def test_warn_band_is_flagged(self):
        self.write_mb(4.5)
        result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertTrue(result.warn)
        self.assertFalse(result.truncated)

    def test_below_warn_band_is_not_flagged(self):
        """Gegenprobe zu test_warn_band_is_flagged: sonst koennte `warn`
        unabhaengig von der Groesse immer True sein."""
        self.write_mb(3)
        result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertFalse(result.warn)
        self.assertFalse(result.truncated)

    def test_missing_file_is_not_an_error(self):
        result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertIsNone(result.size_mb)
        self.assertIsNone(result.error)
        self.assertFalse(result.truncated)


class TestOpenHandle(RotatorCase):
    def test_open_file_survives_truncate_and_keeps_writing(self):
        self.write_mb(8)
        self.handle = open(self.path, "a", encoding="utf-8")
        self.handle.write("vorher\n")
        self.handle.flush()

        result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertTrue(result.truncated)
        self.assertEqual(os.path.getsize(self.path), 0)

        self.handle.write("nachher\n")
        self.handle.flush()
        self.assertEqual(os.path.getsize(self.path), len("nachher\n"))
        self.handle.close()
        self.handle = None

        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "nachher\n")

    def test_never_removes_the_file(self):
        self.write_mb(8)
        LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertTrue(os.path.exists(self.path))


class TestPathGuard(RotatorCase):
    def test_foreign_path_is_never_touched(self):
        # Gross genug, dass ein gebrochener Waechter wirklich kuerzen wuerde:
        # eine kleine Datei bleibt auch dann unangetastet und der Test waere
        # aus dem falschen Grund gruen.
        self.write_mb(8)
        foreign = os.path.join(self.dir, "wichtige-datei.log")
        with open(foreign, "w", encoding="utf-8") as handle:
            handle.write("y" * (8 * 1024 * 1024))
        result = LogRotator(max_mb=5, warn_mb=4).check(foreign)
        self.assertFalse(result.truncated)
        self.assertIsNotNone(result.error)
        self.assertEqual(os.path.getsize(foreign), 8 * 1024 * 1024)

    def test_path_outside_tmp_is_rejected(self):
        outside = "/var/log/sonstwas.log"
        self.assertFalse(is_allowed(outside))
        self.assertFalse(LogRotator(max_mb=1, warn_mb=1).check(outside).truncated)

    def test_allowed_name_outside_tmp_is_still_rejected(self):
        """Gegenprobe zu test_path_outside_tmp_is_rejected.

        "sonstwas.log" faellt schon am Namen durch - dieser Test benutzt einen
        erlaubten Namen und schliesst damit die /tmp-Regel wirklich aus.
        """
        outside = "/var/log/escha_server_lan.log"
        self.assertTrue(is_allowed(SERVER_LOG))
        self.assertFalse(is_allowed(outside))

    def test_real_file_with_allowed_name_outside_tmp_survives(self):
        """Der harte Fall: erlaubter Name, echte Datei, ausserhalb von /tmp.

        Fassage im Home-Verzeichnis, nicht unter /tmp - sonst waere der Test
        gruen, ohne dass die /tmp-Regel geprueft wurde.
        """
        home = os.path.expanduser("~")
        outside_dir = tempfile.mkdtemp(prefix="slotmon-guard-", dir=home)
        self.addCleanup(shutil.rmtree, outside_dir)
        outside = os.path.join(outside_dir, "escha_server_lan.log")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("y" * (8 * 1024 * 1024))

        # Faellt $HOME unter /tmp, greift hier die self.assertFalse-Zeile und
        # der Test schlaegt fehl, statt aus dem falschen Grund gruen zu sein.
        self.assertFalse(is_allowed(outside))
        result = LogRotator(max_mb=5, warn_mb=4).check(outside)
        self.assertFalse(result.truncated)
        self.assertIsNotNone(result.error)
        self.assertEqual(os.path.getsize(outside), 8 * 1024 * 1024)

    def test_nested_tmp_path_with_known_name_is_allowed(self):
        nested = os.path.join(tempfile.mkdtemp(prefix="slotmon-nest-"),
                              "escha_server_lan.log")
        self.addCleanup(shutil.rmtree, os.path.dirname(nested))
        self.assertTrue(is_allowed(nested))

    def test_monitor_own_log_is_allowed(self):
        own = os.path.join(self.dir, "slotmon.log")
        with open(own, "w", encoding="utf-8") as handle:
            handle.write("z" * (6 * 1024 * 1024))
        self.assertTrue(LogRotator(max_mb=5, warn_mb=4).check(own).truncated)
        # Nicht nur das Flag: die Datei muss auch wirklich leer sein.
        self.assertEqual(os.path.getsize(own), 0)

    def test_symlink_pointing_out_of_tmp_is_rejected(self):
        """realpath loest den Symlink auf - ein Link nach aussen ist zu."""
        home = os.path.expanduser("~")
        outside_dir = tempfile.mkdtemp(prefix="slotmon-guard-", dir=home)
        self.addCleanup(shutil.rmtree, outside_dir)
        target = os.path.join(outside_dir, "escha_server_lan.log")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("y" * (8 * 1024 * 1024))

        link = os.path.join(self.dir, "slotmon.log")
        os.symlink(target, link)
        self.assertFalse(is_allowed(link))
        self.assertFalse(LogRotator(max_mb=5, warn_mb=4).check(link).truncated)
        self.assertEqual(os.path.getsize(target), 8 * 1024 * 1024)

    def test_real_configured_paths_are_allowed(self):
        """Nur der Wächter wird befragt, nie check() - sonst rührte der Test
        das echte Log an."""
        self.assertTrue(is_allowed(SERVER_LOG))
        self.assertTrue(is_allowed(MONITOR_LOG))


class TestCadence(RotatorCase):
    def test_only_every_nth_call_stats(self):
        self.write_mb(8)
        rotator = LogRotator(max_mb=5, warn_mb=4)
        counter = 0
        results = []
        for _ in range(10):
            results.append(rotator.check(self.path))
        # Erster Aufruf prueft sofort, danach nur jeder every-te
        self.assertTrue(results[0].truncated)
        self.assertFalse(any(r.truncated for r in results[1:]))
        # Und wirklich gekuerzt, nicht nur gemeldet.
        self.assertEqual(os.path.getsize(self.path), 0)
        self.assertIsNone(results[5].size_mb)

    def test_force_checks_on_a_skipped_tick(self):
        self.write_mb(8)
        rotator = LogRotator(max_mb=5, warn_mb=4, every=60)
        self.assertTrue(rotator.check(self.path).truncated)
        # Die Datei ist jetzt leer, ein weiterer Takt wuerde "0 MB" messen.
        # size_mb=None beweist: der Takt wurde uebersprungen, nicht gemessen.
        skipped = rotator.check(self.path)
        self.assertFalse(skipped.truncated)
        self.assertIsNone(skipped.size_mb)

        self.write_mb(8)
        self.assertTrue(rotator.check(self.path, force=True).truncated)
        self.assertEqual(os.path.getsize(self.path), 0)

    def test_skipped_call_reports_nothing(self):
        """Ein uebersprungener Tick meldet gar nichts.

        Kein 'unbekannt' und kein warn, sonst schwemmt der Collector im
        Sekunden-Takt Leermeldungen mit.
        """
        self.write_mb(8)
        rotator = LogRotator(max_mb=5, warn_mb=4, every=60)
        rotator.check(self.path)
        self.assertEqual(rotator.check(self.path), RotateResult())
        # Die Datei ist nach dem ersten Tick gekuerzt und bleibt es.
        self.assertEqual(os.path.getsize(self.path), 0)


class TestShouldCheck(unittest.TestCase):
    """Der Collector entscheidet mit derselben Funktion wie der Rotator -
    sonst laufen beide auseinander und der Collector ruft check() genau dann
    auf, wenn der Rotator ueberspringt."""

    def test_first_call_is_due(self):
        self.assertTrue(should_check(SERVER_LOG, 60, 0))

    def test_only_every_nth_counter_is_due(self):
        due = [c for c in range(180) if should_check(SERVER_LOG, 60, c)]
        self.assertEqual(due, [0, 60, 120])

    def test_foreign_path_is_never_due(self):
        self.assertFalse(should_check("/tmp/slotmon-history.jsonl", 60, 0))
        self.assertFalse(should_check("/var/log/escha_server_lan.log", 60, 0))

    def test_zero_every_does_not_explode(self):
        self.assertTrue(should_check(SERVER_LOG, 0, 0))
        self.assertTrue(should_check(SERVER_LOG, 0, 7))


if __name__ == "__main__":
    unittest.main()