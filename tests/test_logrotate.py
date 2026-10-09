"""Tests fuer die Log-Aufraeumung.

Kein Test hier fasst das echte Log an. Alle Fixtures liegen in
``tempfile.mkdtemp()``, also unter /tmp mit einem zufaelligen Namen; der
Pfadwaechter in ``logrotate.is_allowed`` verlangt zusaetzlich einen der beiden
erlaubten Basenamen, sodass ein Fehler in diesem Modul nicht auf
``/tmp/escha_server_lan.log`` durchschlagen kann. ``TestPathGuard`` haelt
genau das fest.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import slotmon.logrotate as logrotate
from slotmon.logrotate import LogRotator, RotateResult, is_allowed

#: Wo die beiden echten Pfade aus der Konfiguration liegen. Nur fuer den
#: Wächter befragt - ``check()`` wird nie darauf aufgerufen.
SERVER_LOG = "/tmp/escha_server_lan.log"
MONITOR_LOG = "/tmp/slotmon.log"

MB = 1024 * 1024


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

    def write_bytes(self, count, pattern=b"x"):
        with open(self.path, "wb") as handle:
            handle.write(pattern * count)

    def write_mb(self, size_mb, pattern="x"):
        # int(...): 4.5 * 1024 * 1024 ist ein float, und "x" * float wirft
        # TypeError. Der Bruchteil geht als Nachkommanul lost - weg.
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(pattern * int(size_mb * MB))


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


class TestWarnGrenze(RotatorCase):
    """Die Grenze zwischen 'warn' und 'truncate' sitzt auf den Byte genau.

    `<` oder `<=` an dieser einen Stelle ist der ganze Unterschied zwischen
    'Warnung genau auf der Schwelle' und 'stille Schwelle'.
    """

    def test_exactly_at_warn_threshold_warns(self):
        self.write_bytes(4 * MB)
        result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertEqual(result.size_mb, 4.0)
        self.assertTrue(result.warn)
        self.assertFalse(result.truncated)
        # Nicht gekuerzt - die Warnung ersetzt kein Truncate.
        self.assertEqual(os.path.getsize(self.path), 4 * MB)

    def test_one_byte_below_warn_threshold_is_quiet(self):
        self.write_bytes(4 * MB - 1)
        result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertFalse(result.warn)
        self.assertEqual(os.path.getsize(self.path), 4 * MB - 1)

    def test_exactly_at_max_threshold_truncates(self):
        self.write_bytes(5 * MB)
        result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertTrue(result.truncated)
        self.assertEqual(os.path.getsize(self.path), 0)


class TestSchwellenKonsistenz(RotatorCase):
    """MAX_LOG_MB kleiner als WARN_LOG_MB darf kein totes Band erzeugen.

    Sonst gilt fuer alles zwischen beiden Werten: kein Truncate, keine
    Warnung, kein Fehler - das Log waechst unbegrenzt und meldet sich nie.
    """

    def test_warn_band_is_pulled_below_max(self):
        self.assertEqual(LogRotator(max_mb=500, warn_mb=900).warn_mb, 450.0)

    def test_sane_warn_band_is_left_alone(self):
        self.assertEqual(LogRotator().warn_mb, 900)
        self.assertEqual(LogRotator(max_mb=5, warn_mb=4).warn_mb, 4)

    def test_no_dead_band_between_max_and_warn(self):
        # MAX_LOG_MB=5, WARN_LOG_MB=9 - dieselbe Lage wie 500/900, nur klein.
        rotator = LogRotator(max_mb=5, warn_mb=9)
        self.assertEqual(rotator.warn_mb, 4.5)

        # Oberhalb von max_mb: wird gekuerzt.
        self.write_mb(6)
        self.assertTrue(rotator.check(self.path).truncated)

        # Zwischen der aufgezogenen Warnschwelle und max_mb: wird gewarnt.
        # Vorher war das hier still: 6 MB gegen ein 5-MB-Limit ergab
        # truncated=False warn=False error=None.
        self.write_mb(4.75)
        result = rotator.check(self.path, force=True)
        self.assertTrue(result.warn)
        self.assertFalse(result.truncated)
        self.assertIsNone(result.error)
        self.assertEqual(os.path.getsize(self.path), int(4.75 * MB))


class TestTruncateFehler(RotatorCase):
    """Der Zweig, der bei EACCES, EROFS und ENOSPC feuert."""

    def test_truncate_failure_is_reported(self):
        self.write_mb(8)
        rotator = LogRotator(max_mb=5, warn_mb=4)
        with mock.patch("slotmon.logrotate.os.truncate",
                        side_effect=OSError(13, "Permission denied")):
            result = rotator.check(self.path)
        self.assertFalse(result.truncated)
        self.assertIsNotNone(result.error)
        self.assertIn("truncate", result.error)
        self.assertIn("Permission denied", result.error)
        # Der Wert vor dem Kuerzen bleibt brauchbar - der Aufrufer soll die
        # Groesse sehen, die nicht abzubekommen war.
        self.assertEqual(result.size_mb, 8.0)
        self.assertEqual(os.path.getsize(self.path), 8 * MB)

    def test_truncate_failure_does_not_propagate(self):
        """Ein Fehler darf den Monitor nicht killen."""
        self.write_mb(8)
        with mock.patch("slotmon.logrotate.os.truncate",
                        side_effect=OSError(28, "No space left on device")):
            result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertIn("No space left", result.error)

    def test_readonly_filesystem_is_reported(self):
        self.write_mb(8)
        with mock.patch("slotmon.logrotate.os.truncate",
                        side_effect=OSError(30, "Read-only file system")):
            result = LogRotator(max_mb=5, warn_mb=4).check(self.path)
        self.assertIsNotNone(result.error)
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
            handle.write("y" * (8 * MB))
        result = LogRotator(max_mb=5, warn_mb=4).check(foreign)
        self.assertFalse(result.truncated)
        self.assertIsNotNone(result.error)
        self.assertEqual(os.path.getsize(foreign), 8 * MB)

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

    def test_neighbour_directory_of_tmp_is_rejected(self):
        """/tmpfoo beginnt mit "/tmp" - ohne abschliessendes os.sep waere es
        erlaubt. Reine Pfadpruefung, es wird nichts angelegt."""
        self.assertFalse(is_allowed("/tmpfoo/escha_server_lan.log"))
        self.assertFalse(is_allowed("/tmplog/slotmon.log"))
        self.assertTrue(is_allowed(SERVER_LOG))

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
            handle.write("y" * (8 * MB))

        # Faellt $HOME unter /tmp, greift hier die self.assertFalse-Zeile und
        # der Test schlaegt fehl, statt aus dem falschen Grund gruen zu sein.
        self.assertFalse(is_allowed(outside))
        result = LogRotator(max_mb=5, warn_mb=4).check(outside)
        self.assertFalse(result.truncated)
        self.assertIsNotNone(result.error)
        self.assertEqual(os.path.getsize(outside), 8 * MB)

    def test_nested_tmp_path_with_known_name_is_allowed(self):
        nested = os.path.join(tempfile.mkdtemp(prefix="slotmon-nest-"),
                              "escha_server_lan.log")
        self.addCleanup(shutil.rmtree, os.path.dirname(nested))
        self.assertTrue(is_allowed(nested))

    def test_monitor_own_log_is_allowed(self):
        own = os.path.join(self.dir, "slotmon.log")
        with open(own, "w", encoding="utf-8") as handle:
            handle.write("z" * (6 * MB))
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
            handle.write("y" * (8 * MB))

        link = os.path.join(self.dir, "slotmon.log")
        os.symlink(target, link)
        self.assertFalse(is_allowed(link))
        self.assertFalse(LogRotator(max_mb=5, warn_mb=4).check(link).truncated)
        self.assertEqual(os.path.getsize(target), 8 * MB)

    def test_symlink_to_foreign_name_inside_tmp_is_rejected(self):
        """Auch der Zielname muss erlaubt sein.

        Ein Link "slotmon.log" auf die eigene Historie liegt unter /tmp und
        heisst erlaubt - ist es nicht, sonst raemt der Monitor
        /tmp/slotmon/history.jsonl weg.
        """
        target = os.path.join(self.dir, "history.jsonl")
        with open(target, "wb") as handle:
            handle.write(b"y" * (8 * MB))
        link = os.path.join(self.dir, "slotmon.log")
        os.symlink(target, link)
        self.assertFalse(is_allowed(link))
        self.assertFalse(LogRotator(max_mb=5, warn_mb=4).check(link).truncated)
        self.assertEqual(os.path.getsize(target), 8 * MB)

    def test_measurement_and_truncate_use_the_resolved_path(self):
        """Zwischen Wache und Zugriff liegt kein ungepruefter Pfad mehr.

        Der Reviewer hat vorgefuehrt, dass ein umgelegter Symlink im Fenster
        zwischen ``is_allowed`` und ``os.truncate`` eine Datei ausserhalb von
        /tmp trifft. Geprueft wird deshalb, welcher Pfad bei getsize und
        truncate tatsaechlich ankommt - und dass es ueberhaupt ein anderer ist.
        """
        real = os.path.join(self.dir, "escha_server_lan.log")
        with open(real, "wb") as handle:
            handle.write(b"x" * (8 * MB))
        link = os.path.join(self.dir, "slotmon.log")
        os.symlink(real, link)
        resolved = os.path.realpath(link)
        self.assertNotEqual(resolved, link, "Vorbbedingung: der Pfad muss sich unterscheiden")

        measured, truncated = [], []
        echte_getsize = os.path.getsize
        echtes_truncate = os.truncate

        def spion_getsize(pfad, *args, **kwargs):
            measured.append(pfad)
            return echte_getsize(pfad, *args, **kwargs)

        def spion_truncate(pfad, *laenge, **kwargs):
            truncated.append(pfad)
            return echtes_truncate(pfad, *laenge, **kwargs)

        with mock.patch("slotmon.logrotate.os.path.getsize", spion_getsize), \
                mock.patch("slotmon.logrotate.os.truncate", spion_truncate):
            result = LogRotator(max_mb=5, warn_mb=4).check(link)

        self.assertTrue(result.truncated)
        self.assertEqual(measured, [resolved])
        self.assertEqual(truncated, [resolved])

    def test_real_configured_paths_are_allowed(self):
        """Nur der Wächter wird befragt, nie check() - sonst rührte der Test
        das echte Log an."""
        self.assertTrue(is_allowed(SERVER_LOG))
        self.assertTrue(is_allowed(MONITOR_LOG))


class TestCadence(RotatorCase):
    def test_only_every_nth_call_stats(self):
        self.write_mb(8)
        rotator = LogRotator(max_mb=5, warn_mb=4)
        results = []
        for _ in range(10):
            results.append(rotator.check(self.path))
        # Erster Aufruf prueft sofort, danach nur jeder every-te
        self.assertTrue(results[0].truncated)
        self.assertFalse(any(r.truncated for r in results[1:]))
        # Und wirklich gekuerzt, nicht nur gemeldet.
        self.assertEqual(os.path.getsize(self.path), 0)
        self.assertIsNone(results[5].size_mb)

    def test_default_every_is_two_minutes(self):
        """60 Ticks mal POLL_INTERVAL 2.0 s.

        Task 8 ruft check() jeden Pol und rechnet ``every`` *nicht* um:
        int(2.0 * 60) = 120 waere das doppelte Intervall.
        """
        self.assertEqual(LogRotator().every, 60)

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


class TestCollectorNutzung(RotatorCase):
    """So benutzt Task 8 den Rotator: check() in JEDEM Pol, ohne Gate davor.

    Genau diese Nutzung ist in Fix-Runde 1 ausgefallen. Der Collector hatte
    ``should_check`` als eigene Taktung vorgesetzt und ``check()`` dadurch nur
    noertig angerufen; zwei unabhaengige Zaehler kamen nie gleichzeitig auf 0.
    Gemessen (8 MB je Pol, every=10, 31 Polls): 0 Kuerzungen, das Log wuchs auf
    62 MB, und die Rueckgabe war nicht von einem gesunden Leer-Tick zu
    unterscheiden - kein Fehler, kein Signal, nur Stille.
    """

    #: Kleiner als der Review, gleiches Verhaeltnis: Schwelle alle 10 Polls,
    #: 2 MB Schreibzugriff je Pol, 31 Polls.
    MAX_MB = 20
    WARN_MB = 10
    EVERY = 10
    POLLS = 31
    WRITE_MB = 2

    def simulate(self, rotieren):
        """31 Polls mit einem 'Server', der 2 MB je Pol anhaengt."""
        write = self.WRITE_MB * MB
        for _ in range(self.POLLS):
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write("x" * write)
            rotieren()
        return os.path.getsize(self.path)

    def test_rotation_holds_when_called_every_poll(self):
        rotator = LogRotator(max_mb=self.MAX_MB, warn_mb=self.WARN_MB,
                             every=self.EVERY)
        kuerzungen = 0

        def rotieren():
            nonlocal kuerzungen
            kuerzungen += rotator.check(self.path).truncated

        ende = self.simulate(rotieren)
        # Ohne Takt waeren es 0. Genau 3 kommen bei faerigem due-Takt
        # (Poll 1, 11, 21, 31 - Poll 1 ist noch unter der Schwelle).
        self.assertEqual(kuerzungen, 3)
        self.assertEqual(ende, 0)

    def test_skipped_tick_is_identifiable(self):
        """Ein uebersprungener Tick muss sich von einer Messung unterscheiden.

        Sonst kann Task 8 nicht entscheiden, ob es "0 MB" oder "nicht
        gemessen" anzeigt - und zeigt im Zweifel 0 MB an.
        """
        rotator = LogRotator(max_mb=self.MAX_MB, warn_mb=self.WARN_MB,
                             every=self.EVERY)
        rotator.check(self.path)          # Poll 1, faellig
        uebersprungen = rotator.check(self.path)   # Poll 2, nicht faellig
        self.assertIsNone(uebersprungen.size_mb)
        self.assertIsNone(uebersprungen.error)
        self.assertFalse(uebersprungen.truncated)
        self.assertFalse(uebersprungen.warn)

    def test_there_is_no_second_cadence_tool(self):
        """Die Taktung hat genau eine Autoritaet: check().

        ``should_check`` ist abschafft, weil sein einziger Zweck der falsche
        Aufruf war - als Gate vor check(). Ohne den Rueckweg dorthin kann
        Task 8 den Fehler nicht wieder einbauen.
        """
        self.assertFalse(hasattr(logrotate, "should_check"))

    def test_public_surface_is_the_documented_one(self):
        for name in ("is_allowed", "resolve_allowed", "RotateResult", "LogRotator"):
            self.assertTrue(hasattr(logrotate, name), name)


if __name__ == "__main__":
    unittest.main()
