import os
import shutil
import tempfile
import time
import unittest

from slotmon.logreader import CHUNK_BYTES, LogReader

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "log_sample.txt")

RECEIVED = (
    '{"timestamp": "2026-10-07T17:19:07.123456", "event": "request.received", '
    '"rid": "a93031d87de846e187ddc80afcfb73eb", '
    '"obj": {"rid": "a93031d87de846e187ddc80afcfb73eb", "received_time": 437300.5}}'
)
FINISHED = (
    '{"timestamp": "2026-10-07T17:19:24.654321", "event": "request.finished", '
    '"rid": "d7d50a43cae04ef3a11f625fc1939662", '
    '"obj": {"rid": "d7d50a43cae04ef3a11f625fc1939662", "received_time": 437290.25}, '
    '"out": {"meta_info": {"id": "d7d50a43cae04ef3a11f625fc1939662", '
    '"finish_reason": {"type": "stop", "matched": 248046}, "prompt_tokens": 4270, '
    '"completion_tokens": 1687, "cached_tokens": 0}}}'
)
ACCESS = (
    '[2026-10-07 17:19:20] INFO:     127.0.0.1:40076 - '
    '"POST /v1/chat/completions HTTP/1.1" 200 OK'
)


class ReaderCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotmon-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "server.log")
        self.reader = LogReader(self.path, pair_window_s=30.0)

    def append(self, *lines):
        with open(self.path, "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def seed_fixture(self):
        shutil.copyfile(FIXTURE, self.path)


class TestParsing(ReaderCase):
    def test_received_event(self):
        self.append(RECEIVED)
        events = self.reader.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "received")
        self.assertEqual(events[0].rid, "a93031d87de846e187ddc80afcfb73eb")
        # `at` ist Unix-Zeit aus dem timestamp-Feld, damit _attach_ip die
        # Access-Zeile (ebenfalls Unix-Zeit) dagegen vergleichen kann.
        expected = time.mktime(
            time.strptime("2026-10-07T17:19:07", "%Y-%m-%dT%H:%M:%S")
        )
        self.assertAlmostEqual(events[0].at, expected, delta=1.0)
        # Die sglang-eigene received_time liegt in meta und ist KEINE Unix-Zeit.
        self.assertEqual(events[0].meta["received_time"], 437300.5)

    def test_finished_event_carries_meta_info(self):
        self.append(FINISHED)
        event = self.reader.poll()[0]
        self.assertEqual(event.kind, "finished")
        self.assertEqual(event.rid, "d7d50a43cae04ef3a11f625fc1939662")
        self.assertEqual(event.meta["prompt_tokens"], 4270)
        self.assertEqual(event.meta["completion_tokens"], 1687)
        self.assertEqual(event.meta["cached_tokens"], 0)
        self.assertEqual(event.meta["finish_reason"], "stop")
        self.assertEqual(event.meta["received_time"], 437290.25)

    def test_unrelated_lines_are_ignored(self):
        self.append(ACCESS, "[2026-10-07 17:19:25] some other noise")
        self.assertEqual(self.reader.poll(), [])

    def test_malformed_json_does_not_raise(self):
        self.append('{"event": "request.received", "rid": ', RECEIVED)
        events = self.reader.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].rid, "a93031d87de846e187ddc80afcfb73eb")

    def test_real_fixture_line_by_line(self):
        self.seed_fixture()
        events = self.reader.poll()
        kinds = [(e.kind, e.rid) for e in events]
        self.assertEqual(kinds, [
            ("received", "a93031d87de846e187ddc80afcfb73eb"),
            ("finished", "d7d50a43cae04ef3a11f625fc1939662"),
        ])
        # Die Fixture ist echte Logreihenfolge: Access 17:19:20, finished 17:19:24.
        # Bisher wurde hier nur kind/rid geprueft - ausgerechnet der einzige Test
        # mit realistischer Reihenfolge testete die Zuordnung nicht mit.
        finished = events[1]
        self.assertEqual(finished.meta["ip_port"], "127.0.0.1:40076")
        self.assertEqual(finished.meta["status"], 200)


class TestState(ReaderCase):
    def test_received_opens_rid(self):
        self.append(RECEIVED)
        self.reader.poll()
        self.assertIn("a93031d87de846e187ddc80afcfb73eb", self.reader.state["open"])

    def test_open_rid_value_is_unix_time_not_received_time(self):
        # received_time ist sglangs monotoner Zaehler (437300.5). Landet der in
        # state["open"], rechnet Task 8 now - open[rid] und bekommt ~1,79 Mrd. s.
        self.append(RECEIVED)
        self.reader.poll()
        stored = self.reader.state["open"]["a93031d87de846e187ddc80afcfb73eb"]
        expected = time.mktime(
            time.strptime("2026-10-07T17:19:07", "%Y-%m-%dT%H:%M:%S")
        )
        self.assertAlmostEqual(stored, expected, delta=1.0)
        # Und die daraus berechnete Dauer ist plausibel, nicht 56 Jahre.
        duration = time.time() - stored
        self.assertLess(abs(duration), 10 * 365 * 24 * 3600)

    def test_open_rid_value_stays_unix_when_received_time_absent(self):
        line = (
            '{"timestamp": "2026-10-07T17:19:07.123456", "event": "request.received", '
            '"rid": "nort", "obj": {"rid": "nort"}}'
        )
        self.append(line)
        self.reader.poll()
        stored = self.reader.state["open"]["nort"]
        expected = time.mktime(
            time.strptime("2026-10-07T17:19:07", "%Y-%m-%dT%H:%M:%S")
        )
        self.assertAlmostEqual(stored, expected, delta=1.0)

    def test_finished_closes_rid(self):
        # Der echte Server loggt fuer DENSELBEN Request dieselbe rid in
        # request.received und request.finished. Die Fixture-Zeilen im Plan
        # tragen zwei verschiedene rids, das kann den Slot nie schliessen.
        finished_same_rid = FINISHED.replace(
            "d7d50a43cae04ef3a11f625fc1939662", "a93031d87de846e187ddc80afcfb73eb"
        )
        self.append(RECEIVED)
        self.reader.poll()
        self.append(finished_same_rid)
        self.reader.poll()
        self.assertEqual(self.reader.state["open"], {})

    def test_finished_without_received_is_still_reported(self):
        self.append(FINISHED)
        event = self.reader.poll()[0]
        self.assertEqual(event.kind, "finished")
        self.assertNotIn(event.rid, self.reader.state["open"])

    def test_duplicate_received_does_not_count_twice(self):
        self.append(RECEIVED)
        self.reader.poll()
        self.append(RECEIVED)
        self.reader.poll()
        self.assertEqual(len(self.reader.state["open"]), 1)

    def test_no_new_bytes_returns_empty(self):
        self.append(RECEIVED)
        self.reader.poll()
        self.assertEqual(self.reader.poll(), [])

    def test_poll_reads_only_new_bytes(self):
        self.append(RECEIVED)
        first = self.reader.poll()
        self.append(FINISHED)
        second = self.reader.poll()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].kind, "finished")


class TestIpPairing(ReaderCase):
    def test_ip_is_attached_when_close_in_time(self):
        # Reale Reihenfolge: Access-Zeile kommt VOR dem finished-Event
        # (Log 17:19:20 access, 17:19:24 finished).
        self.append(ACCESS)
        self.append(FINISHED)
        event = self.reader.poll()[0]
        self.assertEqual(event.meta["ip_port"], "127.0.0.1:40076")

    def test_ip_is_attached_across_polls(self):
        # Access-Zeile und finished-Event landen in verschiedenen polls.
        self.append(ACCESS)
        self.reader.poll()
        self.assertEqual(self.reader.pending_access[0]["ip_port"], "127.0.0.1:40076")
        self.append(FINISHED)
        event = self.reader.poll()[0]
        self.assertEqual(event.meta["ip_port"], "127.0.0.1:40076")

    def test_ip_cannot_backfill_a_previously_returned_event(self):
        # Dokumentierte Grenze: ein Event, das schon zurueckgegeben wurde, kann
        # seine IP nicht mehr nachtraeglich bekommen. Die Access-Zeile kam erst
        # im zweiten poll. Siehe Modul-Docstring.
        self.append(FINISHED)
        first = self.reader.poll()[0]
        self.assertIsNone(first.meta["ip_port"])
        self.append(ACCESS)
        self.reader.poll()
        self.assertIsNone(first.meta["ip_port"])

    def test_ip_is_dropped_when_too_far_away(self):
        old = (
            '[2026-10-07 17:09:20] INFO:     10.0.0.9:1111 - '
            '"POST /v1/chat/completions HTTP/1.1" 200 OK'
        )
        self.append(old)
        self.append(FINISHED)
        event = self.reader.poll()[0]
        self.assertIsNone(event.meta["ip_port"])

    def test_running_event_has_no_ip(self):
        self.append(RECEIVED)
        self.assertIsNone(self.reader.poll()[0].meta["ip_port"])

    def test_prompt_text_never_appears_in_meta(self):
        # Die Fixture enthaelt keinen Prompt-Text. Damit das ueberhaupt geprueft
        # werden kann, wird eine Zeile mit echtem Prompt-Feld gebaut; nur
        # Metadaten duerfen in meta landen, der Text selbst nie.
        line = (
            '{"timestamp": "2026-10-07T17:19:24.654321", "event": "request.finished", '
            '"rid": "secret-rid", "obj": {"rid": "secret-rid", "received_time": 437290.25, '
            '"text": "GEHEIMER-PROMPT-TEXT", "input_ids": [1,2,3]}, '
            '"out": {"text": "GEHEIME-ANTWORT", "meta_info": {"id": "secret-rid", '
            '"finish_reason": {"type": "stop", "matched": 1}, "prompt_tokens": 4270, '
            '"completion_tokens": 1687, "cached_tokens": 0}}}'
        )
        self.append(line)
        meta = self.reader.poll()[0].meta
        blob = repr(meta)
        self.assertNotIn("GEHEIMER-PROMPT-TEXT", blob)
        self.assertNotIn("GEHEIME-ANTWORT", blob)
        self.assertNotIn("input_ids", blob)
        self.assertNotIn("text", meta)
        # Metadaten sind weiterhin da - es wird nicht weggefiltert, nur der Text.
        self.assertEqual(meta["prompt_tokens"], 4270)
        self.assertEqual(meta["completion_tokens"], 1687)

    def test_monitoring_traffic_never_pairs(self):
        # /get_load und /metrics sind Eigenverkehr des Monitors, kein Modell-Request.
        noise = (
            '[2026-10-07 17:19:20] INFO:     127.0.0.1:40076 - '
            '"GET /get_load HTTP/1.1" 200 OK'
        )
        self.append(noise, FINISHED)
        self.assertIsNone(self.reader.poll()[0].meta["ip_port"])

    def test_abort_and_configure_logging_never_pair(self):
        for path in ("/abort_request", "/configure_logging"):
            with self.subTest(path=path):
                reader = LogReader(self.path, pair_window_s=30.0)
                self.append(
                    f'[2026-10-07 17:19:20] INFO:     127.0.0.1:40076 - '
                    f'"POST {path} HTTP/1.1" 200 OK'
                )
                self.append(FINISHED)
                self.assertIsNone(reader.poll()[0].meta["ip_port"])

    def test_failed_request_never_becomes_slot_holder(self):
        # Eine 500 ist kein erfolgreicher Slot-Halter. Sie wird nicht gepaart,
        # der Status bleibt aber fuer Task 8 sichtbar.
        failed = (
            '[2026-10-07 17:19:20] INFO:     127.0.0.1:40076 - '
            '"POST /v1/chat/completions HTTP/1.1" 500 Internal Server Error'
        )
        self.append(failed, FINISHED)
        event = self.reader.poll()[0]
        self.assertIsNone(event.meta["ip_port"])
        self.assertEqual(event.meta["status"], 500)

    def test_successful_request_records_status_200(self):
        self.append(ACCESS, FINISHED)
        event = self.reader.poll()[0]
        self.assertEqual(event.meta["status"], 200)
        self.assertEqual(event.meta["ip_port"], "127.0.0.1:40076")

    def test_pending_access_does_not_grow_without_bound(self):
        # Im echten Log stehen 92450 Access-Zeilen, davon nur ~2300 unter /v1/.
        # Ohne Aufraeumen waechst die Liste unbegrenzt (3 pro Minute /metrics).
        for i in range(50):
            self.append(
                f'[2026-10-07 17:19:20] INFO:     127.0.0.1:{40000 + i} - '
                f'"GET /metrics HTTP/1.1" 200 OK'
            )
        self.reader.poll()
        self.assertEqual(len(self.reader.pending_access), 0)

    def test_old_access_lines_are_dropped_but_recent_one_survives(self):
        for i in range(5):
            self.append(
                f'[2026-10-07 17:0{i}:20] INFO:     10.0.0.9:1111 - '
                f'"POST /v1/chat/completions HTTP/1.1" 200 OK'
            )
        self.reader.poll()
        # Genau eine ueberlebt (die neueste, 17:04:20) - nicht "weniger als 1",
        # das waere auch bei vollstaendigem Verlust erfuellt.
        self.assertEqual(len(self.reader.pending_access), 1)
        self.assertEqual(self.reader.pending_access[0]["ip_port"], "10.0.0.9:1111")


class TestLargeLogs(ReaderCase):
    """Ein 49-MB-Log darf nicht am Stueck in den Speicher."""

    def _write_many(self, count):
        with open(self.path, "w", encoding="utf-8") as handle:
            for i in range(count):
                handle.write(
                    '{"timestamp": "2026-10-07T17:19:07.123456", '
                    '"event": "request.received", "rid": "rid-%06d", '
                    '"obj": {"rid": "rid-%06d", "received_time": %f}}\n' % (i, i, 437300.0 + i)
                )

    def test_events_are_found_across_chunk_boundaries(self):
        # Deutlich groesser als CHUNK_BYTES, damit die Zeilen wirklich ueber
        # Chunk-Grenzen laufen.
        self._write_many(60000)
        size = os.path.getsize(self.path)
        self.assertGreater(size, CHUNK_BYTES * 3)
        events = self.reader.poll()
        self.assertEqual(len(events), 60000)
        self.assertEqual(events[0].rid, "rid-000000")
        self.assertEqual(events[-1].rid, "rid-059999")
        self.assertEqual(len(self.reader.state["open"]), 60000)

    def test_reader_never_reads_more_than_a_chunk_at_once(self):
        # Ohne Chunking wuerde _read_bytes die ganze Datei lesen. Wir messen
        # den groessten tatsaechlich gelesenen Block ueber den Offset-Sprung.
        self._write_many(60000)
        jumps = []
        original = self.reader._read_raw

        def spy(limit):
            data = original(limit)
            jumps.append(len(data))
            return data

        self.reader._read_raw = spy
        self.reader.poll()
        self.assertTrue(jumps, "es wurde nichts gelesen")
        self.assertLessEqual(max(jumps), CHUNK_BYTES)

    def test_multibyte_character_split_across_chunks_survives(self):
        # Der Decoder muss zeichenweise ueber Chunk-Grenzen arbeiten, sonst
        # wird ein geteiltes UTF-8-Zeichen zu U+FFFD.
        head = "x" * (CHUNK_BYTES - 2)
        body = (
            '{"timestamp": "2026-10-07T17:19:07.123456", "event": "request.received", '
            '"rid": "mü-ü-ü", "obj": {"rid": "mü-ü-ü", "received_time": 1.0}}\n'
        )
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(head + "\n" + body)
        events = self.reader.poll()
        # Die Zeile beginnt hinter dem Padding; sie muss als GANZES erkannt werden.
        self.assertEqual([e.rid for e in events], ["mü-ü-ü"])

    def test_partial_line_carries_over_to_next_poll(self):
        self.append(RECEIVED)
        # Datei abschneiden, sodass die letzte Zeile unvollstaendig ist.
        with open(self.path, "r+b") as handle:
            handle.truncate(len(RECEIVED) + 1 - 10)
        self.reader.poll()
        self.assertEqual(self.reader.state["open"], {})
        self.append(FINISHED)
        self.assertEqual(self.reader.poll(), [])


class TestReadErrors(ReaderCase):
    def test_missing_file_returns_empty_and_reports_nothing_new(self):
        events = self.reader.poll()
        self.assertEqual(events, [])
        self.assertIsNone(self.reader.last_error)

    def test_unreadable_file_is_reported_distinctly(self):
        # Kein "leere Ansicht ohne Hinweis": der Fehler steht in last_error.
        self.append(RECEIVED)
        os.chmod(self.path, 0o000)
        self.addCleanup(os.chmod, self.path, 0o644)
        if os.geteuid() == 0:
            self.skipTest("root umgeht Berechtigungen")
        events = self.reader.poll()
        self.assertEqual(events, [])
        self.assertIsNotNone(self.reader.last_error)
        self.assertIn("PermissionError", self.reader.last_error)

    def test_last_error_clears_after_success(self):
        self.append(RECEIVED)
        self.reader.poll()
        self.assertIsNone(self.reader.last_error)


class TestPairingCost(ReaderCase):
    """Die Zuordnung darf nicht linear ueber den Monitor-Eigenverkehr laufen.

    Auf dem echten Log stehen 92k Access-Zeilen, davon ~2.3k unter /v1/. Ein
    linearer Scan pro Event kostete in Runde 1 die doppelte Poll-Zeit (2.3 s ->
    4.2 s). Der Zaehler hier ist deterministisch, im Gegensatz zu einer Zeitmessung.
    """

    def _monitor_traffic(self, count, with_v1=True):
        for i in range(count):
            self.append(
                f'[2026-10-07 17:19:20] INFO:     127.0.0.1:{40000 + i % 1000} - '
                f'"GET /metrics HTTP/1.1" 200 OK'
            )
            self.append(
                f'[2026-10-07 17:19:21] INFO:     127.0.0.1:{45000 + i % 1000} - '
                f'"GET /get_load HTTP/1.1" 200 OK'
            )
        if with_v1:
            self.append(ACCESS)
        self.append(FINISHED)

    def test_pairing_scan_does_not_grow_with_monitoring_traffic(self):
        scans = {}
        for count in (10, 5000):
            reader = LogReader(self.path, pair_window_s=30.0)
            open(self.path, "w", encoding="utf-8").close()
            self._monitor_traffic(count)
            reader.poll()
            scans[count] = reader.window_scans
        # 5000 Zeilen Eigenverkehr duerfen die Arbeit nicht vervielfaachen.
        self.assertLessEqual(
            scans[5000], scans[10] * 2 + 5,
            f"Scan wuchs mit dem Eigenverkehr: {scans}",
        )

    def test_pairing_still_finds_ip_amid_monitoring_traffic(self):
        self._monitor_traffic(5000)
        event = self.reader.poll()[0]
        self.assertEqual(event.meta["ip_port"], "127.0.0.1:40076")
        self.assertEqual(event.meta["status"], 200)

    def test_failed_lookup_does_not_walk_monitoring_traffic(self):
        failed = (
            '[2026-10-07 17:19:20] INFO:     1.2.3.4:1111 - '
            '"POST /v1/chat/completions HTTP/1.1" 500 Internal Server Error'
        )
        for count in (10, 5000):
            reader = LogReader(self.path, pair_window_s=30.0)
            open(self.path, "w", encoding="utf-8").close()
            for i in range(count):
                self.append(
                    f'[2026-10-07 17:19:20] INFO:     127.0.0.1:{40000 + i % 1000} - '
                    f'"GET /metrics HTTP/1.1" 200 OK'
                )
            self.append(failed, FINISHED)
            reader.poll()
            self.assertLessEqual(reader.window_scans, count + 10)
        # Der Zaehler muss sublinear bleiben: Eigenverkehr wird nie durchsucht.
        self.assertLess(reader.window_scans, 50)


class TestFailedAttribution(ReaderCase):
    def _finished(self, second, rid):
        return (
            '{"timestamp": "2026-10-07T17:19:%02d.654321", "event": "request.finished", '
            '"rid": "%s", "obj": {"rid": "%s", "received_time": 1.0}, '
            '"out": {"meta_info": {"id": "%s", "finish_reason": {"type": "stop", '
            '"matched": 1}, "prompt_tokens": 10, "completion_tokens": 5, '
            '"cached_tokens": 0}}}' % (second, rid, rid, rid)
        )

    def test_one_failed_line_is_claimed_by_only_one_event(self):
        failed = (
            '[2026-10-07 17:19:20] INFO:     1.2.3.4:1111 - '
            '"POST /v1/chat/completions HTTP/1.1" 500 Internal Server Error'
        )
        self.append(failed, self._finished(24, "r1"), self._finished(26, "r2"))
        events = self.reader.poll()
        self.assertEqual([e.meta["status"] for e in events], [500, None])
        self.assertEqual([e.meta["ip_port"] for e in events], [None, None])

    def test_failed_line_is_consumed_from_the_reserve(self):
        failed = (
            '[2026-10-07 17:19:20] INFO:     1.2.3.4:1111 - '
            '"POST /v1/chat/completions HTTP/1.1" 503 Service Unavailable'
        )
        self.append(failed, self._finished(24, "r1"))
        self.reader.poll()
        self.assertEqual(self.reader.pending_access, [])

    def test_successful_line_is_still_claimed_by_only_one_event(self):
        self.append(ACCESS, self._finished(24, "r1"), self._finished(26, "r2"))
        events = self.reader.poll()
        ips = [e.meta["ip_port"] for e in events]
        self.assertEqual(ips.count("127.0.0.1:40076"), 1)
        self.assertEqual(ips.count(None), 1)

    def test_two_failed_lines_are_matched_one_to_one(self):
        # 500@:20 und 503@:30, Events bei :24 und :32. So liegt der erste Event
        # 4 s von der 500 und 6 s von der 503, der zweite 2 s von der 503 und
        # 12 s von der 500: jeder nimmt seine naechste Zeile, keine wird doppelt
        # vergeben.
        first = (
            '[2026-10-07 17:19:20] INFO:     1.2.3.4:1111 - '
            '"POST /v1/chat/completions HTTP/1.1" 500 Internal Server Error'
        )
        second = (
            '[2026-10-07 17:19:30] INFO:     5.6.7.8:2222 - '
            '"POST /v1/chat/completions HTTP/1.1" 503 Service Unavailable'
        )
        self.append(first, second, self._finished(24, "r1"), self._finished(32, "r2"))
        events = self.reader.poll()
        self.assertEqual([e.meta["status"] for e in events], [500, 503])
        self.assertEqual([e.meta["ip_port"] for e in events], [None, None])


class TestMetaShape(ReaderCase):
    def test_every_event_kind_carries_the_same_status_key(self):
        # Task 8 iteriert alle Events und liest meta.get("status"). Der Schluessel
        # muss also bei received genauso vorhanden sein wie bei finished.
        self.append(RECEIVED)
        received = self.reader.poll()[0]
        self.assertIn("status", received.meta)
        self.assertIsNone(received.meta["status"])

    def test_finished_event_has_status_key_too(self):
        self.append(ACCESS, FINISHED)
        event = self.reader.poll()[0]
        self.assertIn("status", event.meta)
        self.assertEqual(event.meta["status"], 200)

    def test_meta_shape_is_identical_across_kinds(self):
        self.append(RECEIVED)
        received = self.reader.poll()[0]
        self.assertEqual(
            sorted(received.meta), sorted(["received_time", "ip_port", "status"])
        )


class TestTruncation(ReaderCase):
    def test_shrink_clears_pending_access(self):
        # Der size<offset-Zweig raeumte nur offset/partial auf, eine /v1/-Zeile
        # ueberlebte das truncate -s 0. Genau der Pfad, den Task 7 LogRotator
        # ausloest. Beide Zweige muessen identisch aufraeumen.
        access = (
            '[2026-10-07 17:19:20] INFO:     192.168.9.2:1111 - '
            '"POST /v1/chat/completions HTTP/1.1" 200 OK'
        )
        self.append(access)
        self.reader.poll()
        self.assertEqual(len(self.reader.pending_access), 1)
        open(self.path, "w", encoding="utf-8").close()
        self.reader.poll()
        self.assertEqual(self.reader.pending_access, [])

    def test_inode_change_clears_pending_access(self):
        access = (
            '[2026-10-07 17:19:20] INFO:     192.168.9.2:1111 - '
            '"POST /v1/chat/completions HTTP/1.1" 200 OK'
        )
        self.append(access)
        self.reader.poll()
        self.assertEqual(len(self.reader.pending_access), 1)
        replacement = os.path.join(self.dir, "rotated.log")
        with open(replacement, "w", encoding="utf-8") as handle:
            handle.write(RECEIVED + "\n")
        os.replace(replacement, self.path)
        self.reader.poll()
        self.assertEqual(self.reader.pending_access, [])

    def test_shrink_is_detected_and_reading_restarts(self):
        self.append(RECEIVED)
        self.reader.poll()
        self.assertEqual(len(self.reader.state["open"]), 1)
        # truncate -s 0: die Datei ist kleiner als die gemerkte Position.
        open(os.path.join(self.dir, "server.log"), "w", encoding="utf-8").close()
        self.assertEqual(self.reader.poll(), [])
        self.append(RECEIVED)
        events = self.reader.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(len(self.reader.state["open"]), 1)

    def test_new_file_at_same_path_is_detected(self):
        # logrotate per rename: neue inode, gleiche Groesse, neuer Inhalt.
        self.append(RECEIVED)
        self.reader.poll()
        replacement = os.path.join(self.dir, "rotated.log")
        with open(replacement, "w", encoding="utf-8") as handle:
            handle.write(RECEIVED + "\n")
        os.replace(replacement, self.path)
        events = self.reader.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].rid, "a93031d87de846e187ddc80afcfb73eb")

    def test_missing_file_is_not_fatal(self):
        events = self.reader.poll()
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
