"""Tests fuer den Collector: Zustand, Uhren, Rotation, Historie, Quellenvertrag.

Zwei Uhren, nicht verwechseln
-----------------------------
`LogReader.state["open"]` und `LogEvent.at` sind **Unix-Zeit** (aus dem
`timestamp`-Feld des Logs). `meta["received_time"]` ist sglangs **monotoner**
Zaehler ab Serverstart, im echten Log ~4.7e5. Ein Collector, der die Startzeit
aus `received_time` nimmt, rechnet `1.79e9 - 4.7e5` und zeigt eine Laufzeit von
~-1.79 Mrd. Sekunden. Die Fixtures bauen deshalb beide Werte weit auseinander:
`MONOTONIC` ist 900.0, `RECEIVED_AT` ist Unix. Wer sie verwechselt, rechnet
200 statt 100 Sekunden und faellt in `test_duration_*` auf.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from slotmon.collector import Collector
from slotmon.config import load_config
from slotmon.logrotate import RotateResult
from slotmon.sources import Sample, ServerSources

#: Unix-Zeit des `request.received` der Fixtures.
RECEIVED_AT = 1_700_000_000.0
#: Unix-Zeit des `request.finished`. Differenz 17 s - die echte Laufzeit des
#: abgeschlossenen Slots, unabhaengig von `now`.
FINISHED_AT = RECEIVED_AT + 17.0
#: sglangs monotoner Zaehler. Bewusst weit von RECEIVED_AT entfernt.
MONOTONIC = 900.0

MAX_REQ_INPUT_LEN = 83994


def stamp(at, iso=True):
    """Log-Zeitstempel fuer eine Unix-Zeit. `localtime`/`mktime` sind symmetrisch:
    der Reader parst mit `time.strptime` + `time.mktime`, also geht der Wert
    verlustfrei hin und zurueck."""
    fmt = "%Y-%m-%dT%H:%M:%S" if iso else "%Y-%m-%d %H:%M:%S"
    return time.strftime(fmt, time.localtime(at))


def received(rid="aaa", at=RECEIVED_AT, monotonic=MONOTONIC):
    return json.dumps({
        "timestamp": stamp(at),
        "event": "request.received",
        "rid": rid,
        "obj": {"rid": rid, "received_time": monotonic},
    })


def finished(rid="aaa", at=FINISHED_AT, monotonic=MONOTONIC,
             prompt=100, completion=50, cached=0):
    return json.dumps({
        "timestamp": stamp(at),
        "event": "request.finished",
        "rid": rid,
        "obj": {"rid": rid, "received_time": monotonic},
        "out": {"meta_info": {"id": rid, "finish_reason": {"type": "stop"},
                              "prompt_tokens": prompt,
                              "completion_tokens": completion,
                              "cached_tokens": cached}},
    })


def access(ip_port="192.168.9.2:44444", at=FINISHED_AT, status=200,
           path="/v1/chat/completions"):
    return (f"[{stamp(at, iso=False)}] INFO:     {ip_port} - "
            f'"POST {path} HTTP/1.1" {status} OK')


def batch_line(ctx=41997, tok_per_s=40.0, queue=0):
    return (f"Decode batch, #running-req: 1, #full token: {ctx}, "
            f"full token usage: 0.50, mamba num: 1, mamba usage: 1.00, "
            f"cuda graph: True, gen throughput (token/s): {tok_per_s:.2f}, "
            f"#queue-req: {queue}")


class CollectorCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotmon-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        # Der Basename muss auf `escha_server*.log` passen, sonst verweigert
        # LogRotator den Pfad (Ruling 1) - der Rotationstest pruefe dann die
        # Ablehnung statt der Kuerzung.
        self.log = os.path.join(self.dir, "escha_server_test.log")
        with open(self.log, "w", encoding="utf-8"):
            pass
        # Auch das Monitor-Log wird rotiert. Ohne eigene Temp-Datei fasste ein
        # Testlauf das echte /tmp/slotmon.log an.
        self.monitor_log = os.path.join(self.dir, "slotmon.log")
        self.history_path = os.path.join(self.dir, "history.jsonl")
        self.now = RECEIVED_AT + 100.0
        self.info_calls = 0
        self.cfg = load_config({
            "SERVER_LOG": self.log,
            "MONITOR_LOG": self.monitor_log,
            "HISTORY": self.history_path,
            "MAX_LOG_MB": "1024",
        })

    def append(self, *lines):
        with open(self.log, "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def sample(self, value):
        """Sample mit `at` = jetzt. `at` ist die Abrufzeit; steht dort eine
        Konstante, waere `server.age_s` eine erfundene Zahl."""
        return Sample(value, self.now, None)

    def pinned(self, value, at):
        """Sample mit festem `at`, unabhaengig von der laufenden Uhr."""
        return Sample(value, at, None)

    def server_info(self, **kwargs):
        self.info_calls += 1
        return self.sample({"max_req_input_len": MAX_REQ_INPUT_LEN})

    def make(self, names=None, now_fn=None, cfg=None):
        collector = Collector(
            cfg if cfg is not None else self.cfg,
            {"192.168.9.2": "Laptop"} if names is None else names,
            now_fn=now_fn or (lambda: self.now),
        )
        self.addCleanup(collector.close)
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 1, "num_waiting_reqs": 0})
        collector.sources.read_metrics = lambda **kw: self.sample(
            {"gen_throughput": 49.0, "num_queue_reqs": 0, "num_running_reqs": 1})
        collector.sources.read_gpu = lambda **kw: self.sample(
            {"util": 95, "used_mb": 15424, "total_mb": 16303})
        collector.sources.read_server_info = self.server_info
        return collector

    def history_lines(self):
        if not os.path.exists(self.history_path):
            return []
        with open(self.history_path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def stderr_of(self, action):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            action()
        return buffer.getvalue()


class TestEmptyState(CollectorCase):
    def test_fresh_collector_has_no_holder(self):
        state = self.make().tick()
        self.assertIsNone(state["slot"]["rid"])
        self.assertEqual(state["slot"]["open_count"], 0)

    def test_state_shape_is_complete(self):
        state = self.make().tick()
        for key in ("at", "server", "slot", "queue", "speed", "gpu", "log", "history"):
            self.assertIn(key, state)
        for key in ("connected", "load_error", "age_s"):
            self.assertIn(key, state["server"])
        for key in ("state", "rid", "system", "ip_port", "started_at",
                    "duration_s", "legacy", "open_count", "complete"):
            self.assertIn(key, state["slot"])

    def test_state_does_no_io(self):
        """`state()` liefert den letzten Tick ohne I/O - der SSE-Pfad pollt
        darueber und darf den Server nicht mitfragen."""
        collector = self.make()
        first = collector.tick()

        def boom(**kwargs):
            raise AssertionError("state() hat I/O gemacht")

        collector.sources.read_load = boom
        collector.sources.read_metrics = boom
        collector.sources.read_gpu = boom
        collector.sources.read_server_info = boom
        collector.reader.poll = boom
        collector.rotator.check = boom
        self.assertEqual(collector.state(), first)

    def test_state_before_first_tick_is_shaped(self):
        state = self.make().state()
        self.assertIsNone(state["slot"]["rid"])
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertEqual(state["history"], [])

    def test_missing_log_file_does_not_crash(self):
        os.unlink(self.log)
        state = self.make().tick()
        self.assertIsNone(state["slot"]["rid"])


class TestHolder(CollectorCase):
    def test_open_request_becomes_holder(self):
        collector = self.make()
        self.append(received())
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "aaa")
        self.assertEqual(state["slot"]["state"], "busy")
        self.assertEqual(state["slot"]["open_count"], 1)
        self.assertTrue(state["slot"]["complete"])
        # Kein Systemname: die Access-Zeile entsteht erst beim Abschluss, die
        # Zuordnung ist abschluss-basiert (Spec, "Bekannte Grenzen"). Der Name
        # erscheint in der Historie, siehe TestCompletion.
        self.assertIsNone(state["slot"]["system"])

    def test_running_slot_has_no_ip_yet(self):
        collector = self.make()
        self.append(received())
        state = collector.tick()
        self.assertIsNone(state["slot"]["ip_port"])

    def test_duration_is_measured_from_received_time(self):
        # received_time der Fixture ist MONOTONIC=900.0, die Unix-Zeit ist
        # RECEIVED_AT, now startet 100 s danach.
        collector = self.make()
        self.append(received())
        state = collector.tick()
        self.assertAlmostEqual(state["slot"]["duration_s"], 100.0, delta=0.001)
        self.now = RECEIVED_AT + 130.0
        state = collector.tick()
        self.assertAlmostEqual(state["slot"]["duration_s"], 130.0, delta=0.001)

    def test_holder_started_at_is_unix_time(self):
        """`started_at` muss Unix-Zeit sein. Ein Mutant, der `received_time`
        (900.0) durchreicht, faellt hier auf."""
        collector = self.make()
        self.append(received())
        state = collector.tick()
        self.assertEqual(state["slot"]["started_at"], RECEIVED_AT)

    def test_duration_is_zero_not_none_at_a_zero_start_time(self):
        """`0.0` ist eine Startzeit, kein fehlender Wert.

        `if started_at:` statt `is not None` liefert hier `None` und das
        Dashboard zeigt fuer einen Slot, der gerade anfing, "keine Laufzeit".
        Unix 0 ist zwar 1970, aber die Bedingung muss trotzdem an `None`
        hängen, nicht an der Wahrheit der Zahl - das ist der Fehlertyp, der
        hier schon einmal Schaden angerichtet hat.
        """
        collector = self.make()
        self.append(received(at=0.0))
        state = collector.tick()
        self.assertEqual(state["slot"]["started_at"], 0.0)
        self.assertAlmostEqual(state["slot"]["duration_s"],
                               self.now - 0.0, delta=0.001)

    def test_legacy_queue_reports_unknown_holder(self):
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 2302, "num_waiting_reqs": 2301})
        self.append(received())
        state = collector.tick()
        self.assertEqual(state["slot"]["legacy"], 2300)
        self.assertIsNone(state["slot"]["rid"])
        self.assertFalse(state["slot"]["complete"])
        self.assertEqual(state["slot"]["state"], "unknown")

    def test_oldest_open_request_holds_the_slot(self):
        collector = self.make()
        self.append(received(rid="bbb", at=RECEIVED_AT + 50.0))
        collector.tick()
        self.append(received(rid="aaa", at=RECEIVED_AT))
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "aaa")
        self.assertEqual(state["slot"]["open_count"], 2)


class TestReservedRids(CollectorCase):
    """sglangs interner Health-Check nutzt `HEALTH_CHECK_<ts>.<us>` als rid."""

    def test_health_check_never_becomes_holder(self):
        collector = self.make()
        self.append(received(rid="HEALTH_CHECK_1791471536.2288644"))
        state = collector.tick()
        self.assertIsNone(state["slot"]["rid"])
        self.assertEqual(state["slot"]["open_count"], 0)

    def test_bare_health_check_never_becomes_holder(self):
        collector = self.make()
        self.append(received(rid="HEALTH_CHECK"))
        state = collector.tick()
        self.assertIsNone(state["slot"]["rid"])
        self.assertEqual(state["slot"]["open_count"], 0)

    def test_health_check_lands_in_no_history(self):
        collector = self.make()
        self.append(received(rid="HEALTH_CHECK_1791471536.2288644"))
        collector.tick()
        self.append(finished(rid="HEALTH_CHECK_1791471536.2288644"), access())
        state = collector.tick()
        self.assertEqual(state["history"], [])
        self.assertEqual(self.history_lines(), [])

    def test_a_real_request_next_to_a_health_check_still_holds(self):
        collector = self.make()
        self.append(received(rid="HEALTH_CHECK_1.1", at=RECEIVED_AT - 10.0))
        self.append(received(rid="aaa"))
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "aaa")
        self.assertEqual(state["slot"]["open_count"], 1)


class TestCompletion(CollectorCase):
    def test_finished_request_lands_in_history(self):
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        state = collector.tick()
        self.assertEqual(len(state["history"]), 1)
        entry = state["history"][0]
        self.assertEqual(entry["rid"], "aaa")
        self.assertEqual(entry["system"], "Laptop")
        self.assertEqual(entry["ip_port"], "192.168.9.2:44444")
        self.assertEqual(entry["completion_tokens"], 50)

    def test_history_survives_a_restart(self):
        first = self.make()
        self.append(received())
        first.tick()
        self.append(finished(), access())
        first.tick()
        first.close()

        second = self.make()
        state = second.tick()
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["history"][0]["rid"], "aaa")

    def test_slot_is_free_after_completion(self):
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        state = collector.tick()
        self.assertIsNone(state["slot"]["rid"])
        self.assertEqual(state["slot"]["state"], "free")

    def test_history_duration_uses_unix_time_not_received_time(self):
        """Der abgeschlossene Slot dauerte 17 s (Unix). Aus `received_time`
        (900.0) und `now` (1.7e9) wuerden ~1.7 Mrd. Sekunden."""
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        entry = collector.tick()["history"][0]
        self.assertEqual(entry["started_at"], RECEIVED_AT)
        self.assertEqual(entry["ended_at"], FINISHED_AT)
        self.assertAlmostEqual(entry["duration_s"], 17.0, delta=0.001)

    def test_history_without_access_line_keeps_rid_and_tokens(self):
        """Keine Access-Zeile: IP bleibt leer, der Eintrag existiert trotzdem.
        Kein geratener Zeitstempel, kein verworfener Request."""
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished())
        entry = collector.tick()["history"][0]
        self.assertEqual(entry["rid"], "aaa")
        self.assertIsNone(entry["ip_port"])
        self.assertIsNone(entry["system"])
        self.assertEqual(entry["prompt_tokens"], 100)

    def test_finished_twice_is_not_written_twice(self):
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        collector.tick()
        self.append(finished(), access())
        state = collector.tick()
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(len(self.history_lines()), 1)

    def test_unknown_ip_falls_back_to_the_raw_ip(self):
        collector = self.make(names={})
        self.append(received())
        collector.tick()
        self.append(finished(), access(ip_port="10.9.9.9:1111"))
        entry = collector.tick()["history"][0]
        self.assertEqual(entry["system"], "10.9.9.9")


class TestDegradedServer(CollectorCase):
    def test_load_failure_keeps_running_and_marks_disconnected(self):
        collector = self.make()
        collector.sources.read_load = lambda **kw: Sample(None, None, "ConnectError: down")
        state = collector.tick()
        self.assertFalse(state["server"]["connected"])
        self.assertEqual(state["server"]["load_error"], "ConnectError: down")
        # `num_waiting_reqs` ist die einzige belastbare Queue-Zahl. Fehlt sie,
        # steht `–` im Dashboard - eine Zahl aus /metrics unter der Bedingung
        # "Server down" waere eine geratene.
        self.assertIsNone(state["queue"]["waiting"])
        self.assertIsNone(state["slot"]["legacy"])

    def test_metrics_still_report_running_when_load_is_down(self):
        """Ist nur eine Quelle tot, zeigen die uebrigen Kacheln weiter."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: Sample(None, None, "down")
        state = collector.tick()
        self.assertEqual(state["queue"]["running"], 1)
        self.assertEqual(state["speed"]["tok_per_s"], 49.0)
        self.assertFalse(state["server"]["connected"])

    def test_unknown_waiting_hides_legacy(self):
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": None, "num_waiting_reqs": None})
        state = collector.tick()
        self.assertIsNone(state["slot"]["legacy"])
        self.assertEqual(state["slot"]["state"], "unknown")

    def test_all_sources_down_stay_null_and_never_zero(self):
        collector = self.make()
        for name in ("read_load", "read_metrics", "read_gpu"):
            setattr(collector.sources, name,
                    lambda **kw: Sample(None, None, "down"))
        state = collector.tick()
        self.assertFalse(state["server"]["connected"])
        self.assertIsNone(state["queue"]["waiting"])
        self.assertIsNone(state["queue"]["running"])
        self.assertIsNone(state["speed"]["tok_per_s"])
        self.assertIsNone(state["speed"]["output_pct"])
        self.assertIsNone(state["gpu"]["util"])
        self.assertIsNone(state["gpu"]["used_mb"])
        self.assertIsNone(state["slot"]["rid"])

    def test_age_is_measured_from_the_fetch_time(self):
        """`server.age_s` ist `now - Sample.at`. Ein Ausdruck, der stattdessen
        `time.time()` oder eine Konstante benutzt, ueberlebt das nicht."""
        collector = self.make()
        # Einmal gestempelt, nicht bei jedem Aufruf neu: der Zeitstempel steht
        # fuer einen Abruf, nicht fuer "jetzt".
        fetched = self.pinned({"num_reqs": 1, "num_waiting_reqs": 0}, self.now - 5.0)
        collector.sources.read_load = lambda **kw: fetched
        state = collector.tick()
        self.assertAlmostEqual(state["server"]["age_s"], 5.0, delta=0.001)
        self.now += 20.0
        state = collector.tick()
        self.assertAlmostEqual(state["server"]["age_s"], 25.0, delta=0.001)


class TestSpeed(CollectorCase):
    def test_max_req_input_len_gives_output_pct(self):
        collector = self.make()
        self.append(batch_line(ctx=41997))
        state = collector.tick()
        self.assertEqual(state["speed"]["ctx_tokens"], 41997)
        self.assertEqual(state["speed"]["max_req_input_len"], MAX_REQ_INPUT_LEN)
        self.assertAlmostEqual(state["speed"]["output_pct"], 50.0, delta=0.001)

    def test_throughput_prefers_metrics(self):
        collector = self.make()
        self.append(batch_line(tok_per_s=40.0))
        state = collector.tick()
        self.assertEqual(state["speed"]["tok_per_s"], 49.0)

    def test_throughput_falls_back_to_the_log_line(self):
        collector = self.make()
        collector.sources.read_metrics = lambda **kw: self.sample(
            {"gen_throughput": None, "num_queue_reqs": 0, "num_running_reqs": 1})
        self.append(batch_line(tok_per_s=40.0))
        state = collector.tick()
        self.assertAlmostEqual(state["speed"]["tok_per_s"], 40.0, delta=0.001)

    def test_last_batch_line_wins(self):
        collector = self.make()
        collector.sources.read_metrics = lambda **kw: self.sample(
            {"gen_throughput": None, "num_queue_reqs": 0, "num_running_reqs": 1})
        self.append(batch_line(ctx=100, tok_per_s=10.0))
        self.append(batch_line(ctx=200, tok_per_s=20.0))
        state = collector.tick()
        self.assertEqual(state["speed"]["ctx_tokens"], 200)
        self.assertAlmostEqual(state["speed"]["tok_per_s"], 20.0, delta=0.001)

    def test_no_batch_line_leaves_speed_null(self):
        state = self.make().tick()
        self.assertIsNone(state["speed"]["ctx_tokens"])
        self.assertIsNone(state["speed"]["output_pct"])

    def test_output_pct_null_without_max_req_input_len(self):
        collector = self.make()
        collector.sources.read_server_info = lambda **kw: Sample(None, None, "down")
        self.append(batch_line(ctx=41997))
        state = collector.tick()
        self.assertIsNone(state["speed"]["max_req_input_len"])
        self.assertIsNone(state["speed"]["output_pct"])


class TestServerInfoIsFetchedOnce(CollectorCase):
    def test_server_info_is_read_once_and_cached(self):
        collector = self.make()
        for _ in range(5):
            collector.tick()
        self.assertEqual(self.info_calls, 1)

    def test_server_info_is_retried_after_a_failure(self):
        collector = self.make()
        collector.sources.read_server_info = lambda **kw: Sample(None, None, "HTTP 503")
        state = collector.tick()
        self.assertIsNone(state["speed"]["max_req_input_len"])
        collector.sources.read_server_info = self.server_info
        state = collector.tick()
        self.assertEqual(state["speed"]["max_req_input_len"], MAX_REQ_INPUT_LEN)
        for _ in range(3):
            collector.tick()
        self.assertEqual(self.info_calls, 1)


class TestRotation(CollectorCase):
    def test_check_runs_every_poll(self):
        """`check()` ist self-gated; ein eigener Takt daneben ist die
        stillschweigend abgeschaltete Rotation (Ruling 12)."""
        collector = self.make()
        collector.rotator.check = mock.Mock(wraps=collector.rotator.check)
        collector.monitor_rotator.check = mock.Mock(wraps=collector.monitor_rotator.check)
        for _ in range(5):
            collector.tick()
        self.assertEqual(collector.rotator.check.call_count, 5)
        self.assertEqual(collector.monitor_rotator.check.call_count, 5)

    def test_rotate_every_stays_at_sixty(self):
        collector = self.make()
        # every ist "60 Polls", bei POLL_INTERVAL=2.0 also zwei Minuten.
        # Aus poll_interval abgeleitet waeren es 120.
        self.assertEqual(collector.rotator.every, 60)
        self.assertEqual(collector.monitor_rotator.every, 60)

    def test_one_rotator_per_log_file(self):
        """Ein Objekt pro Log: zwei Dateien auf einer Instanz teilen sich den
        Zaehler und eine wird nie gekuerzt."""
        collector = self.make()
        self.assertIsNot(collector.rotator, collector.monitor_rotator)
        seen = []

        def spy(target):
            def check(path, force=False):
                seen.append((id(target), path))
                return RotateResult()
            return check

        collector.rotator.check = spy(collector.rotator)
        collector.monitor_rotator.check = spy(collector.monitor_rotator)
        collector.tick()
        self.assertEqual(seen, [(id(collector.rotator), self.log),
                                (id(collector.monitor_rotator), self.monitor_log)])

    def test_measured_size_on_the_first_tick(self):
        collector = self.make()
        state = collector.tick()
        self.assertIsNotNone(state["log"]["size_mb"])
        self.assertFalse(state["log"]["truncated"])
        self.assertFalse(state["log"]["warn"])

    def test_skipped_tick_reports_no_size_not_zero(self):
        """`size_mb is None` heisst "nicht gemessen". Als 0 gelesen waere es
        eine Behauptung, die der Rotator nie gemacht hat."""
        collector = self.make()
        self.append(received())
        first = collector.tick()
        second = collector.tick()
        self.assertIsNotNone(first["log"]["size_mb"])
        self.assertIsNone(second["log"]["size_mb"])
        self.assertIsNot(second["log"]["size_mb"], 0.0)
        self.assertFalse(second["log"]["truncated"])

    def test_big_log_is_truncated_by_the_rotator(self):
        cfg = load_config({
            "SERVER_LOG": self.log,
            "MONITOR_LOG": self.monitor_log,
            "HISTORY": self.history_path,
            "MAX_LOG_MB": "1",
        })
        collector = self.make(cfg=cfg)
        filler = "noise " * 100 + "\n"
        with open(self.log, "a", encoding="utf-8") as handle:
            for _ in range(2100):
                handle.write(filler)
            handle.write(received() + "\n")
        self.assertGreater(os.path.getsize(self.log), 1024 * 1024)

        output = self.stderr_of(collector.tick)
        state = collector.state()
        self.assertTrue(state["log"]["truncated"])
        self.assertGreaterEqual(state["log"]["size_mb"], 1.0)
        self.assertIn("log-truncated", output)
        # Nur der Rotator kuerzt - und er hat es getan.
        self.assertEqual(os.path.getsize(self.log), 0)
        # Der Reader beginnt nach dem Kuerzen von vorn: keine offene rid aus
        # der verlorenen Segmenthaelfte.
        self.assertEqual(collector.reader.state["open"], {})

    def test_disallowed_path_is_reported_not_truncated(self):
        collector = self.make()
        collector.cfg.server_log = os.path.join(self.dir, "nope.log")
        output = self.stderr_of(collector.tick)
        state = collector.state()
        self.assertFalse(state["log"]["truncated"])
        self.assertIsNone(state["log"]["size_mb"])
        self.assertIn("log-rotate-error", output)

    def test_one_failing_log_does_not_silence_the_other(self):
        """Der zuletzt gemeldete Fehler haengt an der Datei. Ein gemeinsamer
        Zaehler liesse den Fehler der zweiten Datei unterdruecken."""
        collector = self.make()
        collector.cfg.server_log = os.path.join(self.dir, "nope.log")
        output = self.stderr_of(collector.tick)
        self.assertIn("server_log", output)
        self.assertNotIn("monitor_log", output)

    def test_rotation_error_is_logged_once_not_every_poll(self):
        """Ein Fehler, der in jedem Takt dieselbe Zeile schreibt, ist Rauschen
        und macht die eine Zeile, auf die es ankommt, unsichtbar.

        Der Rotator springt auf seinem eigenen Takt und liefert den Fehler nur an
        den faelligen Takten. Damit der Test die Drosselung des Collectors
        misst und nicht die des Rotators, antworten hier beide Rotatoren mit
        jedem Aufruf.
        """
        collector = self.make()
        collector.cfg.server_log = os.path.join(self.dir, "nope.log")
        for rotator in (collector.rotator, collector.monitor_rotator):
            rotator.check = lambda path, force=False: RotateResult(
                error=f"pfad nicht freigegeben: {path}")
        output = self.stderr_of(lambda: [collector.tick() for _ in range(5)])
        self.assertEqual(output.count("server_log"), 1)
        self.assertEqual(output.count("monitor_log"), 1)


class TestHistoryCompaction(CollectorCase):
    def test_history_is_compacted_once_per_interval(self):
        collector = self.make()
        collector.history.compact = mock.Mock(wraps=collector.history.compact)
        for _ in range(3):
            collector.tick()
        self.assertEqual(collector.history.compact.call_count, 1)

    def test_compaction_runs_when_the_interval_passed(self):
        collector = self.make()
        collector.compact_every_s = 0.0
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        collector.tick()
        self.assertEqual(len(self.history_lines()), 1)

    def test_compaction_drops_entries_beyond_retention(self):
        stale = {"rid": "old", "ended_at": self.now - 2 * 24 * 3600,
                 "system": "Laptop"}
        with open(self.history_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(stale) + "\n")
        collector = self.make()
        collector.compact_every_s = 0.0
        state = collector.tick()
        self.assertEqual([entry["rid"] for entry in state["history"]], [])
        self.assertEqual(self.history_lines(), [])

    def test_broken_history_line_is_skipped(self):
        with open(self.history_path, "a", encoding="utf-8") as handle:
            handle.write("{kein json\n")
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        state = collector.tick()
        self.assertEqual(len(state["history"]), 1)


class TestSampleCarriesTheFetchTime(unittest.TestCase):
    """`Sample.at` ist die Abrufzeit. Die Alterpruefung des Collectors
    (`server.age_s`) und das Caching von `max_req_input_len` stehen darauf.
    Eine Konstante oder ein Zeitstempel von vor dem Aufruf ueberlebt hier
    nicht."""

    def setUp(self):
        self.sources = ServerSources(load_config({}))

    def assertFetchedNow(self, sample, before, after):
        self.assertIsNotNone(sample.at, "Sample.at ist None")
        self.assertLessEqual(before, sample.at, "at liegt vor dem Aufruf")
        self.assertLessEqual(sample.at, after, "at liegt nach dem Aufruf")

    def _http_sample(self, method, text="{}"):
        import httpx
        before = time.time()
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text=text)) as get:
            sample = getattr(self.sources, method)()
        after = time.time()
        get.assert_called_once()
        self.assertFetchedNow(sample, before, after)

    def test_read_load_at_is_the_fetch_time(self):
        self._http_sample("read_load", json.dumps([{"num_reqs": 1,
                                                   "num_waiting_reqs": 0}]))

    def test_read_metrics_at_is_the_fetch_time(self):
        self._http_sample("read_metrics", "# empty\n")

    def test_read_server_info_at_is_the_fetch_time(self):
        self._http_sample("read_server_info",
                          json.dumps({"max_req_input_len": MAX_REQ_INPUT_LEN}))

    def test_read_gpu_at_is_the_fetch_time(self):
        before = time.time()
        with mock.patch("subprocess.run", return_value=mock.Mock(
                returncode=0, stdout="42, 1, 2\n", stderr="")) as run:
            sample = self.sources.read_gpu()
        after = time.time()
        run.assert_called_once()
        self.assertFetchedNow(sample, before, after)

    def test_error_sample_has_no_timestamp(self):
        """Ein Fehlschlag traegt keine Abrufzeit - sonst waere eine Quelle,
        die seit Stunden tot ist, juenger als der Monitor."""
        sample = Sample(None, None, "down")
        self.assertIsNone(sample.at)
        self.assertFalse(sample.fresh)


def read_source(module):
    with open(module.__file__, encoding="utf-8") as handle:
        return handle.read()


class TestGuard(CollectorCase):
    def test_no_socket_command_in_source(self):
        import slotmon.collector as module
        import slotmon.logreader as reader
        for source in (read_source(module), read_source(reader)):
            self.assertNotIn("ss -t", source)
            self.assertNotIn("/proc/net/tcp", source)

    def test_collector_never_removes_the_log(self):
        """`rm` loescht nur den Verzeichniseintrag, die Inode lebt weiter und der
        Platz kommt erst beim Neustart zurueck - den es nicht gibt. Nur
        `LogRotator` kuerzt, und nur oberhalb der Schwelle."""
        import slotmon.collector as module
        source = read_source(module)
        self.assertNotIn("os.remove", source)
        self.assertNotIn("os.unlink", source)
        self.assertNotIn("shutil", source)


if __name__ == "__main__":
    unittest.main()
