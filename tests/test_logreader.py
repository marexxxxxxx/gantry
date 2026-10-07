import os
import shutil
import tempfile
import time
import unittest

from slotmon.logreader import LogReader

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


class TestState(ReaderCase):
    def test_received_opens_rid(self):
        self.append(RECEIVED)
        self.reader.poll()
        self.assertIn("a93031d87de846e187ddc80afcfb73eb", self.reader.state["open"])

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
        self.append(FINISHED)
        self.append(ACCESS)
        event = self.reader.poll()[0]
        self.assertEqual(event.meta["ip_port"], "127.0.0.1:40076")

    def test_ip_is_dropped_when_too_far_away(self):
        self.append(FINISHED)
        old = (
            '[2026-10-07 17:09:20] INFO:     10.0.0.9:1111 - '
            '"POST /v1/chat/completions HTTP/1.1" 200 OK'
        )
        self.append(old)
        event = self.reader.poll()[0]
        self.assertIsNone(event.meta["ip_port"])

    def test_running_event_has_no_ip(self):
        self.append(RECEIVED)
        self.assertIsNone(self.reader.poll()[0].meta["ip_port"])

    def test_prompts_never_appear_in_meta(self):
        self.append(FINISHED)
        meta = self.reader.poll()[0].meta
        self.assertNotIn("prompt", meta)
        self.assertNotIn("text", meta)

    def test_monitoring_traffic_never_pairs(self):
        # /get_load und /metrics sind Eigenverkehr des Monitors, kein Modell-Request.
        noise = (
            '[2026-10-07 17:19:20] INFO:     127.0.0.1:40076 - '
            '"GET /get_load HTTP/1.1" 200 OK'
        )
        self.append(FINISHED, noise)
        self.assertIsNone(self.reader.poll()[0].meta["ip_port"])

    def test_abort_and_configure_logging_never_pair(self):
        for path in ("/abort_request", "/configure_logging"):
            with self.subTest(path=path):
                reader = LogReader(self.path, pair_window_s=30.0)
                self.append(FINISHED)
                self.append(
                    f'[2026-10-07 17:19:20] INFO:     127.0.0.1:40076 - '
                    f'"POST {path} HTTP/1.1" 200 OK'
                )
                self.assertIsNone(reader.poll()[0].meta["ip_port"])

    def test_pending_access_does_not_grow_without_bound(self):
        # Im echten Log stehen 92450 Access-Zeilen, davon nur ~2300 unter /v1/.
        # OhneAufräumen wächst die Liste unbegrenzt (3 pro Minute /metrics).
        for i in range(50):
            self.append(
                f'[2026-10-07 17:19:20] INFO:     127.0.0.1:{40000 + i} - '
                f'"GET /metrics HTTP/1.1" 200 OK'
            )
        self.reader.poll()
        self.assertEqual(len(self.reader.pending_access), 0)

    def test_old_access_lines_are_dropped(self):
        for i in range(5):
            self.append(
                f'[2026-10-07 17:0{i}:20] INFO:     10.0.0.9:1111 - '
                f'"POST /v1/chat/completions HTTP/1.1" 200 OK'
            )
        self.reader.poll()
        self.assertLessEqual(len(self.reader.pending_access), 1)


class TestTruncation(ReaderCase):
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
