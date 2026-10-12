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
from slotmon.logrotate import LogRotator, RotateResult
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
    verlustfrei hin und zurueck.

    Mikrosekunden **nur**, wenn `at` keine ganze Sekunde ist. Das echte Log
    schreibt `2026-10-08T14:57:35.123456`, dieses Fixture vorher aber nur
    Sekunden - jeder Sub-Sekunden-Abstand wurde dadurch auf volle Sekunden
    gerundet, ohne dass es auffiel. Die Bedingung haelt die bisherigen Fixtures
    byte-identisch: alle benutzen ganze Sekunden.

    `time.strftime` kann `%f` nicht (es gibt die Mikrosekunden nicht aus,
    sondern schreibt die Zeichenfolge durch), der Bruchteil wird daher von Hand
    angehaengt.
    """
    base = time.strftime("%Y-%m-%dT%H:%M:%S" if iso else "%Y-%m-%d %H:%M:%S",
                         time.localtime(at))
    if iso and at % 1 != 0:
        micro = int(round((at % 1) * 1_000_000))
        base = f"{base}.{micro:06d}"
    return base


def received(rid="aaa", at=RECEIVED_AT, monotonic=MONOTONIC):
    return json.dumps({
        "timestamp": stamp(at),
        "event": "request.received",
        "rid": rid,
        "obj": {"rid": rid, "received_time": monotonic},
    })


def finished(rid="aaa", at=FINISHED_AT, monotonic=MONOTONIC,
             prompt=100, completion=50, cached=0, reason="stop"):
    return json.dumps({
        "timestamp": stamp(at),
        "event": "request.finished",
        "rid": rid,
        "obj": {"rid": rid, "received_time": monotonic},
        "out": {"meta_info": {"id": rid, "finish_reason": {"type": reason},
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

    def write_log_mb(self, path, size_mb):
        """Logdatei auf eine Groesse bringen.

        Byte-genau, damit `size_mb` im Rotator wirklich das ist, was der Test
        behauptet - 4.5 MB sind 4718592 Bytes, nicht 4.
        """
        with open(path, "wb") as handle:
            handle.write(b"x" * int(size_mb * 1024 * 1024))

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
        self.assert_slot_shape(state)

    def test_state_shape_is_complete_before_the_first_tick(self):
        """`/api/state` liefert `state()`. Ein Client, der vor dem ersten Poll
        liest, darf keinen `KeyError` bekommen - die Kacheln existieren dann
        zwar mit "unbekannt", aber sie existieren."""
        self.assert_slot_shape(self.make().state())

    def assert_slot_shape(self, state):
        for key in ("at", "server", "slot", "queue", "speed", "gpu", "log", "history"):
            self.assertIn(key, state)
        for key in ("connected", "load_error", "age_s"):
            self.assertIn(key, state["server"])
        for key in ("state", "rid", "system", "ip_port", "started_at",
                    "duration_s", "legacy", "open_count", "complete", "reason"):
            self.assertIn(key, state["slot"], f"slot[{key!r}] fehlt")
        for key in ("size_mb", "warn", "truncated", "history_error"):
            self.assertIn(key, state["log"], f"log[{key!r}] fehlt")

    def test_reason_names_the_state_before_any_tick(self):
        """`unknown` ohne `reason` ist verboten - auch vor dem ersten Poll.

        `Collector.tick()` verbietet genau das fuer jeden Weg, der zu einem
        `unknown` fuehrt. `_empty_state()` verstiess dagegen: dort stand
        `"reason": None`, und im Dashboard erschien daraufhin der schwache
        generische Satz, obwohl gar kein Altbestand belegt ist - `legacy` ist
        `null`, nicht positiv. Sichtbar bei jedem Seitenaufruf und nach jedem
        Serverstart fuer rund ein Poll-Intervall.
        """
        slot = self.make().state()["slot"]
        self.assertEqual(slot["state"], "unknown")
        self.assertIsInstance(slot["reason"], str)
        self.assertNotEqual(slot["reason"].strip(), "")

    def test_the_reason_before_the_first_tick_claims_nothing_about_the_queue(self):
        """Der erste Poll steht aus - das ist alles, was man sagen kann."""
        reason = self.make().state()["slot"]["reason"]
        self.assertIn("Poll", reason)
        self.assertNotIn("Altbestand", reason)
        self.assertNotIn("Halter", reason)

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
        haengen, nicht an der Wahrheit der Zahl - das ist der Fehlertyp, der
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

    def test_abbruch_wird_markiert_und_bleibt_in_der_historie(self):
        """Ein Abbruch ist ein Ereignis in der Historie, kein verworfener Abschluss."""
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(reason="abort"), access())
        state = collector.tick()
        self.assertEqual(len(state["history"]), 1)
        self.assertIs(state["history"][0]["aborted"], True)
        self.assertIs(self.history_lines()[0]["aborted"], True)

    def test_normaler_abschluss_ist_nicht_markiert(self):
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        entry = collector.tick()["history"][0]
        self.assertIs(entry["aborted"], False)

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
        # steht `-` im Dashboard - eine Zahl aus /metrics unter der Bedingung
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

    def test_age_is_positive_with_a_real_round_trip(self):
        """Der Test oben ist untauglich, und genau deshalb ist er blau.

        `pinned(..., self.now - 5.0)` setzt `Sample.at` von Hand fuenf
        Sekunden in die Vergangenheit. Das steht in keinem Produktionspfad:
        `sources.read_load` stempelt **nach** dem HTTP-Roundtrip. Im echten
        Betrieb ist `now` (am Anfang des Ticks genommen) also immer **kleiner**
        als `load.at`, und `max(0.0, now - load.at)` klemmt auf `0.0`.

        Das ist kein Rundungsfehler, sondern ein toter Zweig: die
        Veraltungs-Markierung im Dashboard kann strukturell nie greifen, und
        ein eingefrorenes Dashboard sieht dabei gesund aus.

        Hier wird der Zeitstempel von der Quelle **selbst** gesetzt, wie im
        Betrieb.
        """
        collector = self.make(now_fn=time.time)
        real = time.time

        def read_load(timeout=3.0):
            # Die Verzoegerung steht fuer den Roundtrip. Sie muss auch dann
            # auftreten, wenn die Quelle einen echten `time.time()`-Stempel
            # mitgibt - sonst prueft der Test nichts.
            time.sleep(0.05)
            return Sample({"num_reqs": 1, "num_waiting_reqs": 0}, real(), None)

        collector.sources.read_load = read_load
        state = collector.tick()
        self.assertIsNotNone(state["server"]["age_s"])
        self.assertGreater(
            state["server"]["age_s"], 0.0,
            "age_s ist strukturell 0.0 - die Quelle stempelt nach dem "
            "Roundtrip, `now` kommt von davor")


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

    def test_monitor_log_rotates_at_five_megabytes(self):
        """Das eigene Monitor-Log hat eine eigene, viel niedrigere Schwelle.

        Spec Z. 324: "Eigenes Monitor-Log rotiert bei 5 MB." Mit derselben
        Schwelle wie das Server-Log (Standard 1024 MB) waere dasMonitor-Log
        bei einem Fehler, der im Sekundentakt schreibt, ein GB gross, bevor
        jemand etwas sieht - und es ist genau das Log, in dem man danach
        sucht. Der Ausfall ist also genau dann am teuersten, wenn er
        sichtbar sein muesste.

        Live bemerkt, nicht durch die Tests: beide Rotatoren bekamen
        `max_log_mb`, die 5 MB der Spec gab es im Code nicht.
        """
        collector = self.make()
        self.assertEqual(collector.monitor_rotator.max_mb, 5)

    def test_the_two_thresholds_stay_independent(self):
        """Kuerzt das Monitor-Log, darf das Server-Log unberuehrt bleiben."""
        collector = self.make()
        self.assertNotEqual(collector.rotator.max_mb,
                            collector.monitor_rotator.max_mb)

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

    def test_a_warning_stays_visible_between_two_measurements(self):
        """Der Rotator misst nur jeden 60. Takt - die Warnung muss trotzdem stehen.

        Aus dem Whole-Branch-Review. `LogRotator.check()` liefert auf einem
        uebersprungenen Takt absichtlich nichts: ein Takt, der nicht gemessen
        hat, soll das auch sagen. Das ist eine Zusage aus Task 8 und sie
        bleibt - aber sie endete in der Anzeige, denn der Collector reichte
        den leeren Befund unveraendert durch.

        Gemessen waren 2 von 120 Taks mit `warn=True` (Indizes 0 und 60). Bei
        `POLL_INTERVAL=2.0` ist die Warnung damit 1,7 Prozent der Zeit
        sichtbar; 98 Sekunden lang stand im Dashboard gar nichts.

        Randbedingung 3 verbietet `0`, nicht "zuletzt gemessen".
        """
        collector = self.make()
        # Datei zwischen warn_mb und max_mb: gewarnt, aber nicht gekuerzt.
        self.write_log_mb(self.log, 4.5)
        collector.rotator = LogRotator(max_mb=5, warn_mb=4)

        zustaende = [collector.tick() for _ in range(61)]
        with_warn = [z for z in zustaende if z["log"]["warn"]]
        self.assertEqual(
            len(with_warn), 61,
            f"nur {len(with_warn)} von 61 Taks zeigten die Warnung")

    def test_the_stale_measurement_carries_its_timestamp(self):
        """Ein alter Befund wird als alter ausgewiesen, nicht als frischer.

        Sonst waere eine Groesse von vor zwei Minuten von einer frischen nicht
        zu unterscheiden - und genau das ist die Behauptung, die die Kachel
        tragen soll.
        """
        collector = self.make()
        self.write_log_mb(self.log, 4.5)
        collector.rotator = LogRotator(max_mb=5, warn_mb=4)

        collector.tick()
        self.now += 10.0
        spaeter = collector.tick()
        self.assertEqual(spaeter["log"]["measured_at"], self.now - 10.0,
                         "der Zeitstempel sagt nicht, dass der Befund alt ist")

    def test_a_fresh_measurement_is_not_marked_stale(self):
        """Nach einer echten Messung ist nichts veraltet.

        Sonst wuerde jede Kachel dauerhaft "gemessen vor N s" sagen und der
        Unterschied zwischen alt und frisch ginge verloren.
        """
        collector = self.make()
        self.write_log_mb(self.log, 0.5)
        collector.rotator = LogRotator(max_mb=5, warn_mb=4)
        state = collector.tick()
        self.assertEqual(state["log"]["measured_at"], self.now)
        self.assertLess(state["log"]["measured_at"] - self.now, 1.0)


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


# ======================================================================
# Fix-Runde 1
# ======================================================================
#
# Die globale Randbedingung: **keine Quelle darf werfen, und `tick()` gibt
# niemals eine Exception nach aussen.** Die Poll-Schleife stirbt sonst
# still, waehrend `/api/state` weiter den letzten guten Tick ausliefert.


def unreadable_history(self, kind="readonly"):
    """history.jsonl so herrichten, dass `append`/`compact` scheitern.

    `kind="dir"`: der Pfad ist ein Verzeichnis -> `IsADirectoryError`.
    `kind="readonly"`: die Datei existiert, ist aber nicht beschreibbar ->
    `PermissionError`. Ohne root (uid 1000) greift das chmod wirklich.
    """
    if os.path.exists(self.history_path):
        os.unlink(self.history_path)
    if kind == "dir":
        os.mkdir(self.history_path)
        return
    with open(self.history_path, "w", encoding="utf-8"):
        pass
    os.chmod(self.history_path, 0o444)
    self.addCleanup(os.chmod, self.history_path, 0o644)


class TestTickNeverRaises(CollectorCase):
    """C1 - kein Ausnahme-Pfad verlaesst `tick()`."""

    def test_history_append_permission_error_does_not_escape(self):
        unreadable_history(self, "readonly")
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        state = collector.tick()  # darf nicht werfen
        self.assertEqual(state["slot"]["state"], "free")

    def test_history_append_isdirerror_does_not_escape(self):
        unreadable_history(self, "dir")
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "free")

    def test_history_compact_permission_error_does_not_escape(self):
        unreadable_history(self, "readonly")
        collector = self.make()
        collector.compact_every_s = 0.0
        state = collector.tick()  # compact() schlaegt hier fehl
        self.assertEqual(state["slot"]["state"], "free")

    def test_a_raising_source_does_not_escape(self):
        """`sources` wirft per Konstruktion nicht - aber ein Bug in Task 5, ein
        unerwarteter Fehlertyp aus `httpx` oder ein `KeyboardInterrupt-artiger
        Ausreisser darf die Schleife nicht toeten. Der Fehler wird als Sample
        gefuehrt, nicht verschluckt."""
        collector = self.make()
        for name in ("read_load", "read_metrics", "read_gpu", "read_server_info"):
            def boom(**kwargs):
                raise RuntimeError("Quelle explodiert")
            setattr(collector.sources, name, boom)
        state = collector.tick()
        self.assertFalse(state["server"]["connected"])
        self.assertEqual(state["queue"]["waiting"], None)

    def test_history_error_is_visible_in_the_state(self):
        """`/api/state` liefert `collector.state()`. Ein Schreibfehler, der nur
        im Logfile landet, ist fuer den Leser des Dashboards unsichtbar -
        und `/api/state` friert auf dem letzten guten Tick ein, wenn `tick()`
        wirft. Also ein Kanal im Zustand."""
        unreadable_history(self, "readonly")
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        state = collector.tick()
        self.assertIsNotNone(state["log"]["history_error"])
        self.assertIn("PermissionError", state["log"]["history_error"])

    def test_history_error_is_none_when_writing_works(self):
        state = self.make().tick()
        self.assertIsNone(state["log"]["history_error"])

    def test_history_error_is_cleared_after_recovery(self):
        """Ein einmaliger Fehler darf nicht ewig im Zustand stehen."""
        unreadable_history(self, "readonly")
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        self.assertIsNotNone(collector.tick()["log"]["history_error"])
        os.chmod(self.history_path, 0o644)
        self.assertIsNone(collector.tick()["log"]["history_error"])

    def test_a_successful_write_clears_the_error_by_itself(self):
        """Der Fehler muss auch **ohne** mitlaufenden `compact` verschwinden.

        `compact` laeuft nur einmal pro Stunde. War es zum Zeitpunkt des
        Schreibfehlers bereits erfolgreich, ist der Kanal danach eine Stunde
        lang allein - und `/api/state` wuerde die reparierte Historie weiter
        als fehlerhaft ausweisen. Der Erfolg des naechsten `append` raeumt ihn
        selbst auf.

        Ohne dieses `compact` laeuft der Test auch gruen, wenn die Aufraeumung
        fehlt: der erste Fehlschlag setzt `_last_compact` nicht, also
        wiederholt `compact` im Folgetick und loescht den Kanal.
        """
        unreadable_history(self, "readonly")
        collector = self.make()
        # `compact` hat in diesem Durchlauf bereits stattgefunden.
        collector._last_compact = self.now - 10.0
        collector._history_error = None
        self.append(finished(rid="broken"))
        self.assertIsNotNone(collector.tick()["log"]["history_error"])
        os.chmod(self.history_path, 0o644)
        self.append(finished(rid="repaired"))
        state = collector.tick()
        self.assertIsNone(state["log"]["history_error"])
        self.assertIn("repaired", [e["rid"] for e in self.history_lines()])

    def test_compact_clears_the_error_on_its_own(self):
        """Umgekehrt: ein reparierter `compact`-Pfad raeumt den Kanal auf, auch
        wenn gar nicht geschrieben wird."""
        unreadable_history(self, "readonly")
        collector = self.make()
        self.assertIsNotNone(collector.tick()["log"]["history_error"])
        os.chmod(self.history_path, 0o644)
        self.assertIsNone(collector.tick()["log"]["history_error"])

    def test_history_error_does_not_stop_the_rest_of_the_state(self):
        """Der Schreibfehler darf nur den Schreibpfad betreffen. Die uebrigen
        Kacheln - Server, GPU, Durchsatz, Historie - muessen weiterlaufen,
        sonst sieht ein Schreibfehler aus wie ein Serverausfall."""
        unreadable_history(self, "readonly")
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        state = collector.tick()
        self.assertTrue(state["server"]["connected"])
        self.assertEqual(state["gpu"]["util"], 95)
        self.assertEqual(state["speed"]["tok_per_s"], 49.0)
        self.assertFalse(state["log"]["truncated"])
        # Die Historie laesst sich weiter lesen - nur schreiben geht nicht.
        self.assertIsInstance(state["history"], list)

    def test_a_failed_write_is_not_counted_as_written(self):
        """Wird der Eintrag als geschrieben gemerkt, obwohl `append` scheiterte,
        geht er bei einem spaeteren `compact` verloren - der rid waere aus
        `_recorded` gestrichen, aber nie in der Datei gelandet."""
        unreadable_history(self, "readonly")
        collector = self.make()
        self.append(received())
        collector.tick()
        self.append(finished(), access())
        collector.tick()
        self.assertNotIn("aaa", collector._recorded)
        os.chmod(self.history_path, 0o644)
        self.append(finished(), access())
        collector.tick()
        self.assertIn("aaa", [e["rid"] for e in self.history_lines()])


class TestGhostRids(CollectorCase):
    """C2 - Geister-rids nach einem Server-Neustart.

    Live reproduziert: `open_count=3`, `duration_s=142773` (39,7 h), waehrend
    `num_reqs=0`. Der Server kann keine Request kennen, also sind die rids einer
    frueheren Servergeneration.
    """

    def GHOST_LOAD(self, **kwargs):
        return self.sample({"num_reqs": 0, "num_waiting_reqs": 0})

    def test_num_reqs_zero_drops_ghost_rids(self):
        collector = self.make()
        collector.sources.read_load = self.GHOST_LOAD
        self.append(received(rid="aaa", at=RECEIVED_AT))
        state = collector.tick()
        self.assertEqual(state["slot"]["open_count"], 0)
        self.assertIsNone(state["slot"]["rid"])
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIsNotNone(state["slot"]["reason"])

    def test_num_reqs_zero_is_unknown_not_free(self):
        """`free` ist eine **positive** Behauptung. Hier ist der Zustand
        unbestimmt, nicht leer."""
        collector = self.make()
        collector.sources.read_load = self.GHOST_LOAD
        self.append(received(rid="aaa"))
        state = collector.tick()
        self.assertNotEqual(state["slot"]["state"], "free")

    def test_reason_names_the_contradiction(self):
        collector = self.make()
        collector.sources.read_load = self.GHOST_LOAD
        self.append(received(rid="aaa"))
        reason = collector.tick()["slot"]["reason"]
        self.assertIn("num_reqs", reason)

    def test_num_reqs_one_keeps_the_holder(self):
        """Der Fall, den der Brief ausdruecklich NICHT blind verwerfen will:
        `num_reqs` zaehlt laufende *und* wartende. Ein laufender Request
        erhaelt seinen Halter zurueck."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 1, "num_waiting_reqs": 0})
        self.append(received(rid="aaa"))
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "aaa")
        self.assertEqual(state["slot"]["state"], "busy")
        self.assertIsNone(state["slot"]["reason"])

    def test_ghosts_are_dropped_not_just_hidden(self):
        """Die Geister muessen aus `received_at` raus, sonst tauchen sie beim
        naechsten Abschluss wieder auf."""
        collector = self.make()
        collector.sources.read_load = self.GHOST_LOAD
        self.append(received(rid="aaa"))
        collector.tick()
        self.assertNotIn("aaa", collector.received_at)

    def test_unknown_load_does_not_drop_anything(self):
        """Faellt /get_load aus, ist `num_reqs` unbekannt. Dann wird nichts
        verworfen - eine kaputte Quelle ist kein Beweis fuer einen Geist.

        **Assertion geaendert (Runde 3):** hier stand `assertIsNone(reason)`.
        Das war ein Stellvertreter fuer "kein Geist-Urteil", und als solcher ist
        es jetzt falsch: die Regel "`unknown` ohne `reason` ist verboten"
        verlangt an dieser Stelle einen Grund. Geprueft wird jetzt, **worin**
        der Grund besteht - namely dass er den Serverausfall nennt und keinen
        Geist unterstellt.
        """
        collector = self.make()
        collector.sources.read_load = lambda **kw: Sample(None, None, "down")
        self.append(received(rid="aaa"))
        state = collector.tick()
        self.assertEqual(state["slot"]["open_count"], 1)
        reason = state["slot"]["reason"]
        self.assertIsNotNone(reason)
        self.assertNotIn("num_reqs", reason)

    def test_num_reqs_none_does_not_drop_anything(self):
        """**Assertion geaendert (Runde 3):** siehe
        `test_unknown_load_does_not_drop_anything` - `assertIsNone(reason)` war
        ein Stellvertreter, kein Test."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": None, "num_waiting_reqs": None})
        self.append(received(rid="aaa"))
        state = collector.tick()
        self.assertEqual(state["slot"]["open_count"], 1)
        reason = state["slot"]["reason"]
        self.assertIsNotNone(reason)
        self.assertNotIn("laenger als", reason)

    def test_reason_is_none_in_the_normal_case(self):
        state = self.make().tick()
        self.assertIsNone(state["slot"]["reason"])


# ======================================================================
# Fix-Runde 2
# ======================================================================
#
# N1 und N2 haben eine Wurzel: Geister-rids wurden in einem lokalen Dict
# versteckt statt aus `LogReader.state["open"]` entfernt. Sie kamen also
# unveraendert zurueck, sobald `num_reqs > 0` wurde.
#
# Das Log traegt pro Serverstart genau eine Zeile
# `[YYYY-MM-DD HH:MM:SS] The server is fired up and ready to roll!`
# Am echten Log gemessen: 4 Banner, davon sind die letzten drei
# 2026-10-08 14:53:27, 2026-10-08 14:58:55, 2026-10-09 09:36:21.


def banner(at, text="The server is fired up and ready to roll!"):
    return f"[{stamp(at, iso=False)}] {text}"


class TestGenerationBoundary(CollectorCase):
    """N1 - der Phantom-Halter kehrt zurueck, sobald der Server busy wird."""

    OLD_BANNER = RECEIVED_AT - 5000.0
    NEW_BANNER = RECEIVED_AT + 50.0

    def two_generations(self, num_reqs_seq):
        """Ein Log mit zwei Servergenerationen.

        `aaa` ist ein Geist aus der alten (received vor dem neuen Banner, kein
        finished). `fresh` ist ein Request der laufenden Generation.
        """
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": num_reqs_seq.pop(0), "num_waiting_reqs": 0})
        self.append(banner(self.OLD_BANNER))
        self.append(received(rid="aaa", at=self.OLD_BANNER + 1))
        self.append(finished(rid="closed", at=self.OLD_BANNER + 2))
        self.append(banner(self.NEW_BANNER))
        return collector

    def test_ghost_does_not_return_when_the_server_becomes_busy(self):
        """**Der** Regressionstest fuer N1.

        Im idle-Tick wird der Geist von `num_reqs == 0` unterdrueckt. Sobald
        ein echter Request laeuft, ist `num_reqs == 1` - und `evaluate()`
        waehlt den **aeltesten** offenen rid. Das ist der Geist, 10 Stunden
        alt. Genau das war die Live-Beobachtung:

            idle tick : unknown  rid: None
            busy tick : busy     rid: GHOST  duration_s: 36100.0
        """
        collector = self.two_generations([0, 1])
        idle = collector.tick()
        self.assertEqual(idle["slot"]["open_count"], 0)
        self.assertIsNone(idle["slot"]["rid"])

        self.append(received(rid="fresh", at=self.NEW_BANNER + 1))
        busy = collector.tick()
        self.assertEqual(busy["slot"]["rid"], "fresh")
        self.assertEqual(busy["slot"]["open_count"], 1)
        self.assertNotEqual(busy["slot"]["rid"], "aaa")

    def test_ghost_never_reaches_the_open_dict_at_all(self):
        """Die eigentliche Ursache: der Geist steht in `LogReader.state["open"]`.
        Der Collector filtert ihn nur weg, der Leser kennt ihn nicht."""
        collector = self.two_generations([0])
        collector.tick()
        self.assertNotIn("aaa", collector.reader.state["open"])

    def test_reader_keeps_only_the_current_generation(self):
        """Direkt am Leser, unabhaengig vom `num_reqs`-Waechter."""
        collector = self.two_generations([0])
        collector.tick()
        self.assertEqual(sorted(collector.reader.state["open"]), [])

    def test_a_fresh_rid_survives_the_num_reqs_transition(self):
        """**Der** Regressionstest fuer N2.

        Ein `request.received`, das **vor** dem Zaehlen des Schedulers einfliesst,
        wird bei `num_reqs == 0` nicht verworfen - und verliert dabei nicht seine
        Startzeit. Im Folgetick bei `num_reqs == 1` ist er der Halter, mit
        Startzeit.

        **Fixture korrigiert (Runde 3), zweimal aus gemessenen Gruenden:**

        1. Der rid lag bei `NEW_BANNER + 1`; `NEW_BANNER` ist `RECEIVED_AT + 50`
           und die Uhr steht bei `RECEIVED_AT + 100`. Der rid war **49 s** alt
           und haette unter jedem Alterskriterium als Phantom gegolten. Der Name
           sagte "frisch", das Fixture war es nie - und genau darum hat dieser
           Test den N2-Bug zwei Runden nicht gesehen: er pruefte eine
           Altersgrenze, die es damals gar nicht gab.
        2. Das Alter ist jetzt `1.0` und nicht `0.5`. `LogReader._collect`
           parst `stamp[:19]` und **schneidet Mikrosekunden ab**
           (`2023-11-14T22:14:59.500000` -> `1700000099.0`), gemessen. Ein
           Sub-Sekunden-Fixture koennte also nie den Wert liefern, den es
           behauptet - die Assertion waere falsch, nicht der Code.
        """
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(banner(self.OLD_BANNER))
        self.append(received(rid="fresh", at=self.now - 1.0))
        first = collector.tick()
        self.assertEqual(first["slot"]["open_count"], 1)
        self.assertEqual(first["slot"]["rid"], "fresh")
        self.assertIn("fresh", collector.received_at)

    def test_start_time_survives_into_the_history(self):
        """N2 in Zahlen: die Historienzeile bekommt ein `duration_s`, nicht
        `None`. Der Verlust der Startzeit war der eigentliche Datenschaden."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(banner(self.OLD_BANNER))
        self.append(received(rid="fresh", at=self.now - 1.0))
        collector.tick()
        # Jetzt zaehlt der Scheduler den Request, und er endet.
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 1, "num_waiting_reqs": 0})
        self.append(finished(rid="fresh", at=self.now + 16.0),
                    access(at=self.now + 16.0))
        state = collector.tick()
        self.assertEqual(len(state["history"]), 1)
        entry = state["history"][0]
        self.assertEqual(entry["started_at"], self.now - 1.0)
        self.assertAlmostEqual(entry["duration_s"], 17.0, delta=0.001)

    def test_num_reqs_guard_still_works_without_a_banner(self):
        """Ruling 15 bleibt: ohne Banner im Log ist der `num_reqs == 0`-Waechter
        der einzige Beleg, und er muss allein greifen."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(received(rid="aaa", at=RECEIVED_AT))
        state = collector.tick()
        self.assertEqual(state["slot"]["open_count"], 0)
        self.assertIsNone(state["slot"]["rid"])

    def test_both_guards_together(self):
        """Kein Banner **und** `num_reqs` unbekannt: es wird nichts verworfen.

        Eine kaputte Quelle ist kein Beweis, und ohne Generationsgrenze im Log
        gibt es auch keinen. Der rid bleibt offen - und wird trotzdem nicht zum
        Halter, weil `waiting` unbekannt ist und `evaluate()` dann keinen
        bestimmt. Genau das ist der Unterschied zwischen "nicht belegbar" und
        "weg".

        **Assertion geaendert (Runde 3):** `assertIsNone(reason)` war ein
        Stellvertreter fuer "kein Geist-Urteil". Jetzt wird geprueft, dass der
        Grund den Serverausfall nennt.
        """
        collector = self.make()
        collector.sources.read_load = lambda **kw: Sample(None, None, "down")
        self.append(received(rid="aaa", at=RECEIVED_AT))
        state = collector.tick()
        self.assertEqual(state["slot"]["open_count"], 1)
        self.assertIn("aaa", collector.reader.state["open"])
        self.assertIsNone(state["slot"]["rid"])
        reason = state["slot"]["reason"]
        self.assertIsNotNone(reason)
        self.assertIn("verbunden mit Server: nein", reason)

    def test_reason_never_contradicts_the_connected_flag(self):
        """**Gefunden bei der Durchsicht von Runde 3, nicht von einem roten Test.**

        `waiting is None` und `connected is False` sind nicht dasselbe.
        `sources.py:63` holt `num_waiting_reqs` mit `.get()`; eine frische
        `/get_load`-Antwort **ohne** dieses Feld ergibt `Sample(fresh)` mit
        `waiting=None` - bei `connected=True`. Der Runde-3-Zweig schrieb dort
        pauschal "(verbunden mit Server: nein)" und behauptete damit das
        Gegenteil von `state["server"]["connected"]`, zwei Zeilen weiter oben
        im selben Dict. Gemessen: `connected=True`, `reason` sagt "nein".

        Eine `reason`, die einem anderen Feld desselben Zustands widerspricht,
        ist schlimmer als keine: sie ist als Beleg lesbar und falsch.
        """
        collector = self.make()
        # Genau die Form, die `read_load` liefert, wenn der Schluessel fehlt.
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": None})
        state = collector.tick()
        self.assertTrue(state["server"]["connected"])
        self.assertIsNone(state["queue"]["waiting"])
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIn("verbunden mit Server: ja", state["slot"]["reason"])

    def test_reason_says_not_connected_only_when_it_is_not(self):
        """Die andere Seite derselben Behauptung."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: Sample(None, None, "down")
        state = collector.tick()
        self.assertFalse(state["server"]["connected"])
        self.assertIn("verbunden mit Server: nein", state["slot"]["reason"])


class TestBannerRecognition(CollectorCase):
    """Die Banner-Erkennung selbst, mit den echten Logzeilen."""

    BANNER_LINE = ("[2026-10-08 14:58:55] The server is fired up and ready to roll!\n")

    def test_a_real_banner_line_is_recognised(self):
        from slotmon.logreader import LogReader
        with open(self.log, "w", encoding="utf-8") as handle:
            handle.write(self.BANNER_LINE)
        reader = LogReader(self.log)
        reader.poll()
        self.assertIsNotNone(reader.generation_start)

    def test_a_lookalike_line_is_not_a_banner(self):
        """Der Ausdruck darf nicht auf every-match-Muster beruhen: eine Zeile,
        die das Wort `fired up` im Request-Text enthaelt, ist kein Serverstart."""
        from slotmon.logreader import LogReader
        lookalike = ('[2026-10-08 14:58:55] INFO: request says '
                     '"the server is fired up and ready to roll!" 200 OK\n')
        with open(self.log, "w", encoding="utf-8") as handle:
            handle.write(lookalike)
        reader = LogReader(self.log)
        reader.poll()
        self.assertIsNone(reader.generation_start)

    def test_a_banner_does_not_damage_the_json_events(self        ):
        """Der Banner steht mitten im Log. Die JSON-Zeilen danach muessen
        unveraendert gelesen werden."""
        collector = self.make()
        self.append(banner(RECEIVED_AT - 100.0))
        self.append(received(rid="aaa", at=RECEIVED_AT))
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "aaa")
        self.assertEqual(state["slot"]["open_count"], 1)

    def test_received_in_the_same_second_as_the_banner_is_kept(self):
        """**Grenzfall, real im Log vorhanden.**

        Gemessen: Zeile 329328 traegt das Banner `2026-10-08 14:58:55`, und
        20 Zeilen darunter steht ein `request.received` mit **derselben**
        Sekunde. Das gehoert noch zur laufenden Generation. Die Grenze ist
        deshalb der Zeitstempel des Banners mit `>=` - nicht die naechste
        Zeile danach, und nicht `>`.
        """
        collector = self.make()
        boundary = RECEIVED_AT
        self.append(received(rid="ghost", at=boundary - 1))
        self.append(banner(boundary))
        self.append(received(rid="same", at=boundary))
        state = collector.tick()
        self.assertEqual(sorted(collector.reader.state["open"]), ["same"])
        self.assertEqual(state["slot"]["rid"], "same")

    def test_received_one_second_before_the_banner_is_a_ghost(self):
        collector = self.make()
        boundary = RECEIVED_AT
        self.append(received(rid="ghost", at=boundary - 1))
        self.append(banner(boundary))
        self.append(received(rid="fresh", at=boundary + 5))
        collector.tick()
        self.assertEqual(sorted(collector.reader.state["open"]), ["fresh"])

    def test_purge_also_keeps_a_rid_from_the_banner_second(self):
        """**Luecke gefunden durch den Mutanten N1e.**

        `test_received_in_the_same_second_as_the_banner_is_kept` deckt nur den
        `_apply`-Weg ab: der rid kommt **nach** dem Banner im selben Poll. Der
        zweite Weg - der Purge in `_collect()`, `if since >= generation_start` -
        war ungetestet, und genau dort liegt die zweite Grenzentscheidung.

        Gemessen: `>=` laesst den rid stehen, `>` wirft ihn weg. Der Mutant
        blieb gruen, also unterschied ihn kein Test.

        Die beiden Wege muessen **dieselbe** Grenze ziehen: `_apply` verwirft
        bei `event.at < generation_start`, der Purge bei `since >=`. Ein rid in
        der Banner-Sekunde bleibt also nach beiden. Ein `>` im Purge wuerde ihn
        fallen lassen, waehrend `_apply` ihn behalten haette - die beiden Wege
        widerspruechen sich dann, und das faellt nur auf, wenn beide getestet
        werden.
        """
        collector = self.make()
        boundary = RECEIVED_AT
        self.append(received(rid="edge", at=boundary))
        collector.tick()
        self.assertEqual(sorted(collector.reader.state["open"]), ["edge"])
        # Der Banner kommt erst jetzt, in derselben Sekunde wie der rid.
        self.append(banner(boundary))
        collector.tick()
        self.assertEqual(sorted(collector.reader.state["open"]), ["edge"])

    def test_only_the_last_banner_counts(self):
        """Drei Generationen im Log: die Grenze ist die **letzte**, sonst
        kaeme ein Request der zweiten Generation als Geist durch."""
        collector = self.make()
        self.append(banner(RECEIVED_AT - 9000.0))
        self.append(received(rid="g1", at=RECEIVED_AT - 8000.0))
        self.append(banner(RECEIVED_AT - 5000.0))
        self.append(received(rid="g2", at=RECEIVED_AT - 4000.0))
        self.append(banner(RECEIVED_AT))
        self.append(received(rid="g3", at=RECEIVED_AT + 1))
        collector.tick()
        self.assertEqual(sorted(collector.reader.state["open"]), ["g3"])

    def test_no_banner_means_no_boundary(self):
        from slotmon.logreader import LogReader
        with open(self.log, "w", encoding="utf-8") as handle:
            handle.write(received(rid="aaa") + "\n")
        reader = LogReader(self.log)
        reader.poll()
        self.assertIsNone(reader.generation_start)
        self.assertIn("aaa", reader.state["open"])

    def test_banner_in_a_later_poll_purges_what_is_already_open(self):
        """Der Fall, den der `_apply`-Filter allein nicht erwischt.

        Ein Neustart passiert, **waehrend der Monitor laeuft**: Poll 1 liest ein
        `request.received` einer alten Generation, Poll 2 erst das Banner.
        `_apply` kann den rid dann nicht mehr ablehnen - er steht schon in
        `open`, und `seen` verhindert jede erneute Pruefung. Ohne das Leeren
        bei Bannerempfang bleibt der Geist liegen.

        Gemessen mit deaktiviertem Purge:
            after poll1 open: {'aaa': 1700000000.0} | generation_start: None
            after poll2 open: {'aaa': 1700000000.0}   <- Geist ueberlebt
        """
        collector = self.make()
        self.append(received(rid="ghost", at=RECEIVED_AT))
        collector.tick()
        self.assertIn("ghost", collector.reader.state["open"])

        # Der Neustart. Das Banner kommt erst jetzt im Log an.
        self.append(banner(RECEIVED_AT + 60.0))
        collector.tick()
        self.assertEqual(sorted(collector.reader.state["open"]), [])

    def test_purge_keeps_rids_from_the_new_generation(self):
        """Das Leeren darf nicht zu viel loeschen: ein rid, der nach dem Banner
        empfangen wurde, gehoert zur laufenden Generation."""
        collector = self.make()
        self.append(received(rid="fresh", at=RECEIVED_AT + 5.0))
        self.append(banner(RECEIVED_AT))
        collector.tick()
        self.assertEqual(sorted(collector.reader.state["open"]), ["fresh"])


class TestHistoryErrorChannel(CollectorCase):
    """N3 - der Fehlerkanal darf nicht im selben Tick uebertoent werden.

    `_compact_history` laeuft nach `_flush_finished` im selben Tick. Ein
    erfolgreiches Compact setzt `_history_error` auf `None` - aber es hat den
    fehlgeschlagenen Eintrag **nicht ersetzt**. Damit verstummt der Kanal
    genau dann, wenn es ihn braucht.
    """

    def fail_then_succeed(self, collector):
        """Ein `append`, der beim ersten Versuch scheitert."""
        calls = []
        real = collector.history.append

        def flaky(entry):
            calls.append(entry["rid"])
            if len(calls) == 1:
                raise OSError(28, "No space left on device")
            return real(entry)

        collector.history.append = flaky
        return calls

    def test_failed_append_is_not_silenced_by_a_successful_compact(self):
        collector = self.make()
        # compact() laeuft in diesem Tick und wird erfolgreich sein.
        collector.compact_every_s = 0.0
        with open(self.history_path, "w", encoding="utf-8") as handle:
            handle.write('{"rid": "old", "ended_at": %r}\n' % (self.now - 10.0))
        self.fail_then_succeed(collector)
        self.append(finished(rid="lost"))
        state = collector.tick()
        self.assertIsNotNone(state["log"]["history_error"])
        self.assertIn("No space left", state["log"]["history_error"])

    def test_the_lost_entry_is_actually_missing(self):
        """Der Fehler muss echt sein: der Eintrag darf nicht doch noch in der
        Datei sein."""
        collector = self.make()
        collector.compact_every_s = 0.0
        self.fail_then_succeed(collector)
        self.append(finished(rid="lost"))
        collector.tick()
        self.assertNotIn("lost", [e["rid"] for e in self.history_lines()])

    def test_mixed_tick_reports_the_error(self):
        """Append A scheitert, Append B gelingt. Der Kanal meldet weiterhin
        Fehler - im selben Tick erfolgreich geschrieben zu haben, heisst
        nicht, dass nichts fehlgeschlagen ist."""
        collector = self.make()
        real = collector.history.append
        seen = []

        def flaky(entry):
            seen.append(entry["rid"])
            if len(seen) == 1:
                raise OSError(28, "No space left on device")
            return real(entry)

        collector.history.append = flaky
        self.append(finished(rid="lost"), finished(rid="kept"))
        state = collector.tick()
        self.assertIsNotNone(state["log"]["history_error"])
        rids = [e["rid"] for e in self.history_lines()]
        self.assertNotIn("lost", rids)
        self.assertIn("kept", rids)

    def test_the_channel_clears_only_when_a_later_tick_is_fully_clean(self):
        collector = self.make()
        collector.compact_every_s = 0.0
        self.fail_then_succeed(collector)
        self.append(finished(rid="lost"))
        self.assertIsNotNone(collector.tick()["log"]["history_error"])
        # Der naechste Tick schreibt wieder sauber.
        state = collector.tick()
        self.assertIsNone(state["log"]["history_error"])

    def test_a_compact_failure_alone_still_reports(self):
        """Umgekehrt: scheitert nur `compact`, muss der Kanal ebenfalls
        antworten."""
        import unittest.mock as mock
        collector = self.make()
        with mock.patch.object(collector.history, "compact",
                               side_effect=OSError(13, "Permission denied")):
            state = collector.tick()
        self.assertIn("Permission denied", state["log"]["history_error"])


class TestAfterTruncate(CollectorCase):
    """I3 - nach einer Truncate ist der Slot `unknown`, nicht `free`.

    `reader.reset()` leert das `open`-Dict, der naechste Tick bewertet gegen
    ein leeres Dict und meldet `free` - waehrend `/metrics` direkt daneben
    `num_running_reqs=1` und die GPU 100 % sagt.
    """

    def truncating_cfg(self):
        from slotmon.config import load_config as lc
        return lc({
            "SERVER_LOG": self.log,
            "MONITOR_LOG": self.monitor_log,
            "HISTORY": self.history_path,
            "MAX_LOG_MB": "1",
        })

    def test_slot_is_unknown_after_a_truncate(self):
        collector = self.make(cfg=self.truncating_cfg())
        filler = "noise " * 100 + "\n"
        with open(self.log, "a", encoding="utf-8") as handle:
            for _ in range(2100):
                handle.write(filler)
        first = self.stderr_of(collector.tick)
        self.assertTrue(collector.state()["log"]["truncated"], first)
        second = collector.tick()
        self.assertEqual(second["slot"]["state"], "unknown")

    def test_reason_names_the_truncate(self):
        collector = self.make(cfg=self.truncating_cfg())
        filler = "noise " * 100 + "\n"
        with open(self.log, "a", encoding="utf-8") as handle:
            for _ in range(2100):
                handle.write(filler)
        self.stderr_of(collector.tick)
        reason = collector.tick()["slot"]["reason"]
        self.assertIsNotNone(reason)
        self.assertIn("truncat", reason.lower())

    def test_the_truncating_tick_itself_has_a_reason(self):
        """**Luecke gefunden durch den Mutanten I3d.**

        `test_reason_names_the_truncate` liest den **Folge**-Tick. Der
        kuerzende Tick selbst war ungetestet - und im Folge-Tick wird
        `BLIND_REASON` ueber `elif self._blind` neu gesetzt, wodurch eine
        fehlende Zuweisung im Truncate-Zweig nicht auffaellt. Genau das hat der
        Mutant ausgenutzt: `reason = None` statt `BLIND_REASON`, und kein Test
        wurde rot.

        Gemessen: im kuerzenden Tick ist `reason` mit der Mutation `None`, ohne
        Mutation der Truncate-Text. Der `_blind`-Zweig ist eine **Folge**
        reparieren, keine eigene Begruendung - die Behauptung muss im Tick
        stehen, der sie begruendet.
        """
        collector = self.make(cfg=self.truncating_cfg())
        filler = "noise " * 100 + "\n"
        with open(self.log, "a", encoding="utf-8") as handle:
            for _ in range(2100):
                handle.write(filler)
        self.stderr_of(collector.tick)
        # `state()` ist der Zustand **dieses** Ticks - der naechste `tick()`
        # kuerzt nicht mehr und wuerde den Zweig gar nicht mehr nehmen.
        truncating = collector.state()
        self.assertTrue(truncating["log"]["truncated"])
        self.assertIsNotNone(truncating["slot"]["reason"])
        self.assertIn("truncat", truncating["slot"]["reason"].lower())

    def test_running_metrics_do_not_make_it_free(self):
        """Der widerspruechliche Befund aus dem Brief: `queue.running=1` und
        `gpu.util=100`, trotzdem `free`."""
        collector = self.make(cfg=self.truncating_cfg())
        filler = "noise " * 100 + "\n"
        with open(self.log, "a", encoding="utf-8") as handle:
            for _ in range(2100):
                handle.write(filler)
        self.stderr_of(collector.tick)
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertEqual(state["queue"]["running"], 1)
        self.assertEqual(state["gpu"]["util"], 95)

    def test_unknown_clears_on_the_next_request(self):
        collector = self.make(cfg=self.truncating_cfg())
        filler = "noise " * 100 + "\n"
        with open(self.log, "a", encoding="utf-8") as handle:
            for _ in range(2100):
                handle.write(filler)
        self.stderr_of(collector.tick)
        collector.tick()
        self.append(received(rid="new"))
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "new")
        self.assertEqual(state["slot"]["state"], "busy")
        self.assertIsNone(state["slot"]["reason"])

    def test_a_tick_without_truncate_is_unaffected(self):
        state = self.make().tick()
        self.assertEqual(state["slot"]["state"], "free")
        self.assertIsNone(state["slot"]["reason"])

    def truncating_idle_collector(self):
        """Ein Log, das die Schwelle reisst, **waehrend nichts laeuft**.

        Das ist der Alltagsfall: der Log waechst allein durch die
        `/metrics`-Access-Zeilen, der Slot ist frei. Der Tick, der kuerzt,
        bewertet gegen ein leeres `open`-Dict und haette `free` gemeldet - eine
        positive Behauptung ueber einen Zustand, den der Monitor nicht mehr
        sehen kann.
        """
        cfg = self.truncating_cfg()
        collector = self.make(cfg=cfg)
        filler = "noise " * 100 + "\n"
        with open(self.log, "a", encoding="utf-8") as handle:
            for _ in range(2100):
                handle.write(filler)
        self.stderr_of(collector.tick)
        self.assertTrue(collector.state()["log"]["truncated"])
        return collector

    def test_truncating_tick_of_an_idle_slot_is_not_free(self):
        """Ohne diese Pruefung meldet der kuerzende Tick `free`, weil er gegen
        das leere `open`-Dict bewertet, das die Truncate gerade abgeraeumt hat.
        Genau das war der Befund im Brief."""
        collector = self.truncating_idle_collector()
        state = collector.state()
        self.assertEqual(state["slot"]["state"], "unknown")

    def test_blind_flag_is_cleared_after_the_request_ends(self):
        """`self._blind = False` muss observable sein.

        Bleibt das Flag dauerhaft gesetzt, ist **jeder** spaetere Tick mit
        leerem `open`-Dict `unknown` - der Slot meldet nie wieder `free`, auch
        nicht, nachdem der frische Request sauber beendet wurde. Das ist kein
        konservatives Verhalten, sondern ein Zustand, der sich nicht mehr
        erholt.
        """
        collector = self.truncating_idle_collector()
        self.append(received(rid="aaa"))
        busy = collector.tick()
        self.assertEqual(busy["slot"]["state"], "busy")
        self.append(finished(rid="aaa"), access())
        idle = collector.tick()
        self.assertEqual(idle["slot"]["open_count"], 0)
        self.assertEqual(idle["slot"]["state"], "free")
        self.assertIsNone(idle["slot"]["reason"])

    def test_flag_cleared_only_when_something_is_in_the_log(self):
        """Leerer Log ist kein Grund, den Blick zurueckzugeben: solange nichts
        im Log steht, bleibt die Aussage `unknown`."""
        collector = self.truncating_idle_collector()
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")


class TestRetentionBeforeAppend(CollectorCase):
    """I4 - `finished`-Events vor dem `append` gegen das Fenster filtern."""

    def old_finished(self, rid="old"):
        return finished(rid=rid, at=self.now - 2 * 24 * 3600)

    def counting(self, collector):
        """Zaehlt die `append`-Aufrufe.

        Der blosse Dateiinhalt beweist I4 nicht: `compact()` verwirft
        Altbestand sowieso, die Datei ist also auch ohne Vorfilter am Ende
        korrekt und leer. Der Unterschied ist die **Arbeit** - 1 849 Appends und
        6,9 s bei einem 74 000-Events-Log. Genau die zaehlt dieses Fixture.
        """
        calls = []
        real = collector.history.append
        collector.history.append = lambda entry: (calls.append(entry["rid"]),
                                                 real(entry))[1]
        return calls

    def test_stale_finished_is_not_appended(self):
        collector = self.make()
        calls = self.counting(collector)
        self.append(self.old_finished())
        state = collector.tick()
        self.assertEqual(calls, [])
        self.assertEqual(state["history"], [])
        self.assertEqual(self.history_lines(), [])

    def test_fresh_finished_is_still_appended(self):
        collector = self.make()
        calls = self.counting(collector)
        self.append(finished(rid="fresh"))
        collector.tick()
        self.assertEqual(calls, ["fresh"])
        self.assertEqual(len(self.history_lines()), 1)

    def test_the_window_edge_is_inclusive(self):
        """Genau am Fensterrand wird noch behalten - `History._fresh` benutzt
        `<=`, und der Vorfilter muss das gleiche tun, sonst verschwindet ein
        Eintrag genau in dem Tick, in dem er faellig wird."""
        collector = self.make()
        calls = self.counting(collector)
        self.append(finished(rid="edge", at=self.now - 24 * 3600))
        collector.tick()
        self.assertEqual(calls, ["edge"])

    def test_one_second_past_the_edge_is_dropped(self):
        collector = self.make()
        calls = self.counting(collector)
        self.append(finished(rid="edge", at=self.now - 24 * 3600 - 1))
        collector.tick()
        self.assertEqual(calls, [])

    def test_filter_survives_a_missing_ended_at(self):
        """`ended_at` ist `LogEvent.at` und damit immer eine Zahl - der Reader
        faellt auf `time.time()` zurueck, wenn der Timestamp unlesbar ist. Der
        Vorfilter darf darauf trotzdem nicht mit einem Vergleich auf `None`
        antworten, sondern haelt den Eintrag: wegwerfen ist die Entscheidung,
        die ein Fehler falsch trifft.
        """
        collector = self.make()
        calls = self.counting(collector)
        collector.finished["nostamp"] = {"rid": "nostamp", "ip_port": None,
                                         "started_at": None, "ended_at": None}
        collector.tick()
        self.assertEqual(calls, ["nostamp"])

    def test_the_write_happens_once_not_twice(self):
        """Ohne Vorfilter wird der Altbestand angehaengt und im selben Tick von
        `compact()` wieder verworfen - zwei Schreibvorgaenge fuer nichts."""
        collector = self.make()
        collector.compact_every_s = 0.0
        calls = self.counting(collector)
        for index in range(20):
            self.append(finished(rid=f"old{index}", at=self.now - 2 * 24 * 3600))
        state = collector.tick()
        self.assertEqual(calls, [])
        self.assertEqual(state["history"], [])
        self.assertEqual(self.history_lines(), [])


class TestNonStringRid(CollectorCase):
    """I5 - `is_reserved()` stuerzt auf einer Nicht-String-rid ab.

    `logreader._collect` akzeptiert jede truthy rid; `"rid": 12345` oder eine
    Liste fuehren dann zu `AttributeError` bzw. `TypeError: unhashable type`,
    und beides verlaesst `tick()`.
    """

    def test_is_reserved_rejects_non_strings(self):
        from slotmon.collector import is_reserved
        for bad in (12345, 1.5, ["a"], {"a": 1}, None, True, object()):
            with self.subTest(rid=bad):
                self.assertIs(is_reserved(bad), False)

    def test_is_reserved_still_matches_the_real_prefix(self):
        """Der reservierte rid traegt im echten Log einen Suffix:
        `HEALTH_CHECK_1791471536.2288644`. Der Praefix-Filter bleibt."""
        from slotmon.collector import is_reserved
        self.assertIs(is_reserved("HEALTH_CHECK_1791471536.2288644"), True)
        self.assertIs(is_reserved("HEALTH_CHECK"), True)
        self.assertIs(is_reserved("aaa"), False)

    def _log_with_rid(self, raw_rid):
        line = ('{"timestamp": "2026-10-07T17:19:07.000000", '
                '"event": "request.received", "rid": %s, "obj": {"rid": %s}}'
                % (raw_rid, raw_rid))
        self.append(line)
        return self.make()

    def test_numeric_rid_does_not_escape_tick(self):
        for raw in ("12345", "1.5"):
            with self.subTest(rid=raw):
                self.setUp()
                collector = self._log_with_rid(raw)
                state = collector.tick()
                self.assertIsInstance(state["slot"]["open_count"], int)

    def test_unhashable_rid_does_not_escape_tick(self):
        for raw in ('["a"]', '{"a": 1}'):
            with self.subTest(rid=raw):
                self.setUp()
                collector = self._log_with_rid(raw)
                state = collector.tick()
                self.assertIsInstance(state["slot"]["open_count"], int)

    def test_null_rid_does_not_escape_tick(self):
        collector = self._log_with_rid("null")
        state = collector.tick()
        self.assertIsInstance(state["slot"]["open_count"], int)

    def test_a_malformed_rid_never_becomes_the_holder(self):
        """Ein rid, der keine Zeichenkette ist, kann kein Slot-Halter sein."""
        collector = self._log_with_rid("12345")
        state = collector.tick()
        self.assertIsNone(state["slot"]["rid"])

    def test_a_malformed_rid_never_enters_received_at(self):
        """Der Waechter sitzt an **beiden** Schranken, und jede braucht ihren
        eigenen Beleg.

        Der `open_rids`-Filter allein laesst einen numerischen rid in
        `received_at` landen - als Integer-Schluessel. Kein Absturz, aber ein
        Zustand, in dem eine Nicht-Zeichenkette als rid gefuehrt wird und bei
        einem spaeteren `request.finished` als Schluessel wieder auftaucht
        (`finished[12345]`, JSON-Schluessel `"12345"`).
        """
        collector = self._log_with_rid("12345")
        collector.tick()
        self.assertEqual(collector.received_at, {})

    def test_a_malformed_rid_never_enters_finished(self):
        """Dieselbe Schranke fuer den Abschluss: ein Integer-rid im
        `finished`-Dict wuerde als JSON-Schluessel `"12345"` in die Historie
        geschrieben, und `json.dumps` sortiert gemischte Schluesseltypen
        nicht mehr."""
        collector = self.make()
        self.append(received(rid="aaa"))
        collector.tick()
        line = ('{"timestamp": "2026-10-07T17:19:24.000000", '
                '"event": "request.finished", "rid": 12345, '
                '"obj": {"rid": 12345}, "out": {"meta_info": {"id": "x", '
                '"prompt_tokens": 1, "completion_tokens": 2, '
                '"cached_tokens": 0}}}')
        self.append(line)
        collector.tick()
        self.assertNotIn(12345, collector.finished)
        for entry in self.history_lines():
            self.assertIsInstance(entry["rid"], str)


class TestClosedFlagRemoved(CollectorCase):
    """M6 - `_closed` wurde geschrieben und nie gelesen."""

    def test_no_closed_attribute_exists(self):
        collector = self.make()
        self.assertFalse(hasattr(collector, "_closed"))

    def test_close_still_returns_none_and_is_repeatable(self):
        collector = self.make()
        self.assertIsNone(collector.close())
        self.assertIsNone(collector.close())

    def test_state_after_close_is_still_readable(self):
        collector = self.make()
        collector.tick()
        collector.close()
        self.assertIn("slot", collector.state())


# ======================================================================
# Fix-Runde 3
# ======================================================================
#
# N5: der `num_reqs`-Waechter muss wieder arbeiten.
#
# In Runde 2 wurde er auf `generation_start is None` verengt. Das rettet N2,
# macht die Gegenprobe aber fuer die gesamte Prozesslebenszeit inert, sobald ein
# Banner existiert. Ein rid, der NACH dem Banner empfangen wurde und nie fertig
# wird - Client bricht ab, Server mitten im Request gestoppt -, wird als lebender
# Halter gemeldet:
#
#     state='busy'  rid='stale'  duration_s=36100.0
#
# Der Unterschied zum Geist aus Runde 1/2: er ist nicht ueber die
# Generationsgrenze zu erkennen, denn er gehoert zur laufenden Generation. Nur
# der Scheduler kann ihn widerlegen: `num_reqs == 0` heisst, der Server kennt
# keine Request.
#
# Damit die Gegenprobe nicht wieder N2 bricht (ein frisch eingetroffener rid ist
# im Log, aber noch nicht im Scheduler gezaehlt), haengt sie an einem **Alter**:
# nur rids, die laenger offen sind als ein Poll-Intervall, gelten als nicht
# belegbar. Frische rids bleiben unangetastet, mit ihrer Startzeit.


class TestNumReqsWitness(CollectorCase):
    """N5 - `num_reqs == 0` widerlegt offene rids, die aelter als ein Poll sind."""

    BANNER_AT = RECEIVED_AT - 5000.0

    def with_banner(self, rid, age_s, num_reqs):
        """Ein rid, der seit `age_s` Sekunden offen ist, bei `num_reqs`.

        Das Alter ist der Abstand zwischen `received` und der eingestellten
        Uhr - nicht zu `RECEIVED_AT` verschoben, damit die Grenze genau
        steuerbar ist.
        """
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": num_reqs, "num_waiting_reqs": 0})
        self.append(banner(self.BANNER_AT))
        self.append(received(rid=rid, at=self.now - age_s))
        return collector

    # --- Fall 1: alt, num_reqs == 0 -> Phantom ----------------------

    def test_old_rid_with_num_reqs_zero_is_unknown(self):
        """Der Kernfall aus dem Brief."""
        collector = self.with_banner("stale", age_s=3600.0, num_reqs=0)
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIsNone(state["slot"]["rid"])

    def test_phantom_has_no_invented_duration(self):
        """Die erfundene Zahl ist `duration_s=36100.0`. Sie darf nicht
        erscheinen - auch nicht als Restposten."""
        collector = self.with_banner("stale", age_s=3600.0, num_reqs=0)
        state = collector.tick()
        self.assertIsNone(state["slot"]["duration_s"])
        self.assertIsNone(state["slot"]["started_at"])

    def test_phantom_is_named_in_the_reason(self):
        """Auch die **Zahl** gehoert in den Grund (Runde 4).

        `assertIn("num_reqs", reason)` allein hat einen Mutanten durchgewunken,
        der einen verworfenen Geist mitzaehlt und trotzdem `0 rid(s)` meldet:
        der Grund waere dann sachlich falsch, ohne dass ein einziges Wort der
        bisherigen Assertion falsch wird. Die Zahl ist kein Beiwerk - sie sagt
        dem Leser, **wie viele** rids der Server nicht kennt.
        """
        collector = self.with_banner("stale", age_s=3600.0, num_reqs=0)
        reason = collector.tick()["slot"]["reason"]
        self.assertIsNotNone(reason)
        self.assertIn("num_reqs", reason)
        self.assertIn("1 rid(s)", reason)

    def test_phantom_is_removed_from_the_open_dict(self):
        """Nicht nur versteckt: weg aus `received_at`, sonst kehrt er beim
        naechsten Abschluss zurueck."""
        collector = self.with_banner("stale", age_s=3600.0, num_reqs=0)
        collector.tick()
        self.assertNotIn("stale", collector.received_at)

    # --- Fall 2: frisch, num_reqs == 0 -> bleibt Halter (N2) ----------

    def test_fresh_rid_with_num_reqs_zero_stays_holder(self):
        """N2-Regression. `read_load` laeuft **vor** `_drain_events()`: ein
        Request, der zwischen zwei `/get_load`-Abfragen eintrifft, steht im Log
        und ist noch nicht gezaehlt. Genau das darf die Regel nicht loeschen."""
        collector = self.with_banner("fresh", age_s=1.0, num_reqs=0)
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "fresh")
        self.assertEqual(state["slot"]["state"], "busy")

    def test_fresh_rid_keeps_its_start_time(self):
        collector = self.with_banner("fresh", age_s=1.0, num_reqs=0)
        collector.tick()
        self.assertIn("fresh", collector.received_at)
        self.assertEqual(collector.received_at["fresh"], self.now - 1.0)

    def test_fresh_rid_history_has_a_duration(self):
        """N2 in Zahlen: `duration_s` ist eine Zahl, nicht `None`."""
        collector = self.with_banner("fresh", age_s=1.0, num_reqs=0)
        collector.tick()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 1, "num_waiting_reqs": 0})
        self.append(finished(rid="fresh", at=self.now + 16.0),
                    access(at=self.now + 16.0))
        entry = collector.tick()["history"][0]
        self.assertIsNotNone(entry["duration_s"])
        self.assertAlmostEqual(entry["duration_s"], 17.0, delta=0.001)

    # --- Fall 3: alt, num_reqs > 0 -> bleibt Halter -----------------

    def test_old_rid_with_num_reqs_one_stays_holder(self):
        """Der Server kennt den Request. Die Altersregel darf **nicht**
        greifen, wenn ein Zeuge da ist - sonst wuerde sie laufende
        Requests mit hohem Kontext (Minuten Laufzeit) kuenstlich beenden."""
        collector = self.with_banner("long", age_s=3600.0, num_reqs=1)
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "long")
        self.assertEqual(state["slot"]["state"], "busy")

    def test_old_rid_with_num_reqs_one_has_no_reason(self):
        collector = self.with_banner("long", age_s=3600.0, num_reqs=1)
        self.assertIsNone(collector.tick()["slot"]["reason"])

    # --- Fall 4: kein Banner, num_reqs == 0 (Ruling 15) -------------

    def test_no_banner_and_num_reqs_zero_is_still_unknown(self):
        """Ruling 15 bleibt: ohne Banner ist die Altersregel der einzige
        Beleg, und sie muss allein greifen."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(received(rid="stale", at=self.now - 3600.0))
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIsNone(state["slot"]["rid"])
        self.assertIsNotNone(state["slot"]["reason"])

    def test_no_banner_fresh_rid_still_survives(self):
        """Ohne Banner **und** jung: kein Beleg, also kein Eingriff."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(received(rid="fresh", at=self.now - 1.0))
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "fresh")

    # --- Fall 5: unknown immer mit reason ---------------------------

    def test_unknown_never_without_a_reason(self):
        """`unknown` ohne `reason` ist verboten: das war der Phantom-Halter, der
        geglaubt wurde. In **jedem** der obigen Faelle."""
        cases = [
            ("banner+alt+num_reqs0", self.with_banner("a", 3600.0, 0)),
            ("banner+frisch+num_reqs0", self.with_banner("b", 0.5, 0)),
            ("banner+alt+num_reqs1", self.with_banner("c", 3600.0, 1)),
        ]
        for label, collector in cases:
            with self.subTest(case=label):
                state = collector.tick()
                if state["slot"]["state"] == "unknown":
                    self.assertIsNotNone(state["slot"]["reason"],
                                         f"{label}: unknown ohne reason")

    def test_unknown_from_the_truncate_path_has_a_reason(self):
        """Der andere `unknown`-Weg muss dasselbe liefern."""
        from slotmon.config import load_config as lc
        cfg = lc({
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
        self.stderr_of(collector.tick)
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIsNotNone(state["slot"]["reason"])

    def test_unknown_from_a_dead_server_has_a_reason(self):
        """Und der dritte: `/get_load` tot, kein Halter bestimmbar."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: Sample(None, None, "down")
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIsNotNone(state["slot"]["reason"])

    # --- Die Schwelle selbst -----------------------------------------

    def test_threshold_comes_from_poll_interval(self):
        """Keine eigene Env-Variable, kein Magic Number: die Schwelle ist
        `POLL_INTERVAL`. Mit 10 s ist ein 5 s alter rid noch frisch."""
        from slotmon.config import load_config as lc
        cfg = lc({
            "SERVER_LOG": self.log,
            "MONITOR_LOG": self.monitor_log,
            "HISTORY": self.history_path,
            "MAX_LOG_MB": "1024",
            "POLL_INTERVAL": "10",
        })
        collector = self.make(cfg=cfg)
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(banner(self.BANNER_AT))
        self.append(received(rid="mid", at=self.now - 5.0))
        self.assertEqual(collector.tick()["slot"]["rid"], "mid")

    def test_threshold_boundary_is_exclusive(self):
        """Genau ein Poll-Intervall alt ist noch nicht aelter als eins. `<`
        statt `<=`: sonst waere ein rid, der im selben Takt wie der Load-Sample
        eintrifft, sofort faellig."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(banner(self.BANNER_AT))
        self.append(received(rid="edge", at=self.now - self.cfg.poll_interval))
        self.assertEqual(collector.tick()["slot"]["rid"], "edge")

    def test_one_second_past_the_boundary_is_a_phantom(self):
        """**Umbenannt (Runde 3):** der Test hiess einmal
        `test_one_millisecond_past_the_boundary_is_a_phantom` und behauptete
        damit eine Praezision, die er nicht hat. Gemessen: `LogReader._collect`
        parst `stamp[:19]` und schneidet Mikrosekunden ab, ein Fixture mit
        `- 0.001` landet dadurch **eine ganze Sekunde** tiefer (3 s statt
        2,001 s) - der Name war eine Lub, die Assertion nicht.
        """
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(banner(self.BANNER_AT))
        self.append(received(rid="edge", at=self.now - self.cfg.poll_interval - 1.0))
        self.assertIsNone(collector.tick()["slot"]["rid"])

    def test_the_reader_truncates_microseconds(self):
        """Belegt die Grenze, an der die beiden Tests oben stehen.

        Das echte Log schreibt Mikrosekunden, der Reader nimmt aber nur die
        ersten 19 Zeichen. `LogEvent.at` ist deshalb auf Sekunden genau - fuer
        eine Altersgrenze in der Groessenordnung `POLL_INTERVAL` (2 s) ist das
        eine Nachkommastelle, aber ein Fixture, das `1700000099.5` behauptet,
        wuerde hier immer `1700000099.0` bekommen.
        """
        self.append(received(rid="sub", at=RECEIVED_AT + 0.75))
        collector = self.make()
        collector.tick()
        self.assertEqual(collector.received_at["sub"], RECEIVED_AT)
        # Der Log-Text selbst traegt die Mikrosekunden.
        self.assertIn(".750000", received(rid="sub", at=RECEIVED_AT + 0.75))

    def test_unknown_num_reqs_drops_nothing_at_any_age(self):
        """`num_reqs` ist `None` (Quelle tot oder Feld fehlt). Dann gilt
        ueberhaupt nichts - eine kaputte Quelle ist kein Beweis, in keinem
        Alter."""
        for age in (1.0, 3600.0):
            with self.subTest(age=age):
                self.setUp()
                collector = self.make()
                collector.sources.read_load = lambda **kw: Sample(
                    None, None, "down")
                self.append(banner(self.BANNER_AT))
                self.append(received(rid="aaa", at=self.now - age))
                state = collector.tick()
                self.assertEqual(state["slot"]["open_count"], 1)
                self.assertIn("aaa", collector.reader.state["open"])

    def test_a_phantom_does_not_survive_a_later_busy_tick(self):
        """**Der** Beweis fuer Aufgabe 3: zwei Ticks, dazwischen `num_reqs > 0`.

        Der erste Tick verwirft den Phantom. Der zweite laeuft mit
        `num_reqs == 1`, greift der Waechter also **nicht** - und `evaluate()`
        nimmt den aeltesten offenen rid. Waere das Phantom nur aus dem lokalen
        `open_rids` entfernt worden, stuende es im `reader.state["open"]` noch
        und waere genau hier wieder der Halter: 3600 s erfundene Laufzeit.
        """
        collector = self.with_banner("stale", age_s=3600.0, num_reqs=0)
        first = collector.tick()
        self.assertEqual(first["slot"]["state"], "unknown")

        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 1, "num_waiting_reqs": 0})
        second = collector.tick()
        self.assertIsNone(second["slot"]["rid"])
        self.assertEqual(second["slot"]["open_count"], 0)
        self.assertIsNone(second["slot"]["duration_s"])

    def test_a_phantom_is_gone_from_the_reader_not_only_the_copy(self):
        """Der Leser-Zustand muss stimmen, das lokale Dict ist nur die
        Arbeitskopie. Genau das war der Defekt: `del open_rids[rid]` raeumt nur
        die Kopie."""
        collector = self.with_banner("stale", age_s=3600.0, num_reqs=0)
        collector.tick()
        self.assertNotIn("stale", collector.reader.state["open"])

    def test_the_reader_still_remembers_seen_rids_after_a_purge(self):
        """Nach dem Entfernen bleibt der rid in `seen`. Sonst wuerde er bei
        einem spaeteren `request.received` wieder eingetragen - ein Log kann
        sich wiederholen, der Weg muss trotzdem bleiben."""
        collector = self.with_banner("stale", age_s=3600.0, num_reqs=0)
        collector.tick()
        self.assertIn("stale", collector.reader.state["seen"])

    def test_mixed_open_rids_split_by_age(self):
        """Ein alter Geist und ein frischer echter rid gleichzeitig: nur der
        Geist faellt. Der echte bleibt Halter."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(banner(self.BANNER_AT))
        self.append(received(rid="stale", at=self.now - 3600.0))
        self.append(received(rid="fresh", at=self.now - 1.0))
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "fresh")
        self.assertEqual(state["slot"]["open_count"], 1)
        self.assertNotIn("stale", collector.received_at)

    def test_the_ghost_count_is_the_number_actually_discarded(self):
        """Runde 4, Befund 3: die Zahl im Grund ist bisher ungeprueft.

        `assertIn("1 rid(s)")` allein ist schwach: ein Mutant, der die Zahl
        **konstant** einsetzt, waere mit genau einem Geist nicht zu fangen,
        und einer, der `ghosts` vor dem `del` zaehlt, auch nicht - denn die
        Zahl der verworfenen rids ist hier dieselbe wie die der offenen. Erst
        ein Takt mit **mehreren** Geisten trennt die beiden Fehler von der
        Wahrheit:

          - `ghosts = 0`               (N5t)  -> `0 rid(s)`
          - `ghosts = 1`               (hart) -> `1 rid(s)`
          - `ghosts = len(open_rids)`  (vor dem `del`) -> hier zwar richtig,
            aber bei einem *gemischten* Takt falsch (siehe den naechsten Test).

        Die Behauptung ist bewusst stark: der Grund nennt genau die Zahl der
        rids, die dieser Tick verworfen hat.
        """
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(banner(self.BANNER_AT))
        for rid in ("g1", "g2", "g3"):
            self.append(received(rid=rid, at=self.now - 3600.0))
        state = collector.tick()
        reason = state["slot"]["reason"]
        self.assertIn("3 rid(s)", reason)
        # Und die Gegenprobe: die drei sind wirklich weg, nicht nur benannt.
        self.assertEqual(state["slot"]["open_count"], 0)
        for rid in ("g1", "g2", "g3"):
            self.assertNotIn(rid, collector.received_at)
            self.assertNotIn(rid, collector.reader.state["open"])

    def test_the_ghost_count_excludes_the_surviving_request(self):
        """Der Zaehlpunkt ist der Punkt **nach** dem Alterstest.

        Drei offene rids, davon zwei zu alt: `ghosts` ist 2, nicht 3. Ein
        Zaehler an der falschen Stelle - etwa `len(open_rids)` am Anfang des
        Blocks - meldet hier 3 und behauptet damit, auch der frische rid sei
        dem Server unbekannt. Genau das ist der Fehler, den der frische rid
        im naechsten Testfall als Halter sichtbar macht.
        """
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 0, "num_waiting_reqs": 0})
        self.append(banner(self.BANNER_AT))
        self.append(received(rid="old1", at=self.now - 3600.0))
        self.append(received(rid="old2", at=self.now - 3600.0))
        self.append(received(rid="fresh", at=self.now - 1.0))
        state = collector.tick()
        self.assertIn("2 rid(s)", state["slot"]["reason"])
        self.assertNotIn("3 rid(s)", state["slot"]["reason"])
        # Der ueberlebende rid ist weiterhin der Halter - die Zahl im Grund
        # und der Zustand muessen dieselbe Wirklichkeit beschreiben.
        self.assertEqual(state["slot"]["rid"], "fresh")


# ======================================================================
# Fix-Runde 4
# ======================================================================
#
# Der `unknown`-reason-Block nach `waiting is None` (`collector.py:662-681`)
# war bis hierher **vollstaendig ungepinnt**. Runde 3 hat die harte
# Constraint "unknown nie ohne reason" ueber den Zustand *als solchen*
# abgesichert - aber nicht ueber die drei Zweige, die diesen Zustand
# ueberhaupt erst erzeugen.
#
# Der Re-Reviewer hat zu Runde 3 die Behauptung "der `else`-Zweig ist toter
# Code" widerlegt. Sie stuetzte sich auf `slot.py:35`
# `legacy = max(0, waiting - open_count)`: der Clamp mache `legacy == 0`
# unmoeglich, und damit den Fallback unerreichbar.
#
# Der Clamp haelt aber nur `waiting >= open_count >= 0` fest. Ein **negatives**
# `num_waiting_reqs` umgeht ihn: `max(0, -1 - 0) == 0`, `complete` ist wahr,
# `open_rids` ist leer, also `label == "free"` waere... nein, `waiting == 0`
# gilt nicht, also greift der `else`-Zweig in `collector.py:642` und setzt
# `unknown`. Gemessen:
#
#     waiting=-1   state='unknown' legacy=0 reason='Halter nicht bestimmbar'
#
# Der Fallback ist damit erreichbar, und sein `reason` steht unter derselben
# harten Constraint wie jeder andere. Ein `reason = None` dort (N4c) liefert
# genau das verbotene `unknown` ohne Grund.
#
# Und derselbe Block, eine Zeile hoeher: der `legacy`-Zweig nennt den
# Altbestand, und kein einziger Test suchte je nach dem Wort "Altbestand". Der
# Zweig konnte ausfallen, ohne dass ein Test es merkte - der Grund degradiert
# dann still zu einem generischen String (N4d).


class TestUnknownReasonIsNamed(CollectorCase):
    """Runde 4 - jeder `unknown` aus dem Warteschlangenblock nennt seinen
    Grund, und zwar den **richtigen**."""

    def with_load(self, **load):
        """Collector mit einer beliebigen `/get_load`-Antwort."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(load)
        return collector

    # --- Befund 1: der `else`-Fallback ist erreichbar ------------------

    def test_negative_waiting_reaches_the_fallback(self):
        """Der Pfad, den Runde 3 fuer unmoeglich hielt.

        `slot.py:35` klemmt `waiting - open_count` auf 0 - aber nur nach oben.
        `num_waiting_reqs = -1` ergibt `legacy = 0` bei einem *negativen*
        Eingang, und `waiting == 0` trifft nicht zu. Der Slot ist damit
        `unknown` **mit** `legacy == 0`, also genau der Fall, fuer den der
        `else`-Zweig bei `collector.py:680` geschrieben wurde.
        """
        collector = self.with_load(num_reqs=0, num_waiting_reqs=-1)
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertEqual(state["slot"]["legacy"], 0)

    def test_negative_waiting_never_reports_unknown_without_a_reason(self):
        """Die harte Constraint, an dem Fall 2 aus Runde 2 durchkam.

        Der Zustand ist hier erreichbar (siehe den Test darueber), also muss
        hier derselbe Grund gelten wie ueberall: `unknown` wird nie ohne
        `reason` gemeldet. Ein `reason = None` an `collector.py:681` (N4c)
        faellt an dieser Zeile.
        """
        collector = self.with_load(num_reqs=0, num_waiting_reqs=-1)
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIsNotNone(state["slot"]["reason"],
                             "unknown ohne reason - verboten")

    def test_negative_waiting_gets_the_fallback_reason_not_a_neighbour(self):
        """Nicht *irgendein* Grund, sondern der dieses Falls.

        Die drei Zweige des Blocks nennen drei verschiedene Ursachen. Hier ist
        weder die Warteschlange unbekannt (`waiting` ist eine Zahl) noch gibt
        es Altbestand (`legacy == 0`). Also der generische Fallback - und
        **nicht** der Altbestand-Text, der sonst still degradeert.
        """
        collector = self.with_load(num_reqs=0, num_waiting_reqs=-1)
        reason = collector.tick()["slot"]["reason"]
        self.assertIn("bestimmbar", reason)
        self.assertNotIn("Altbestand", reason)
        self.assertNotIn("Warteschlange unbekannt", reason)

    def test_a_fractional_negative_waiting_behaves_the_same(self):
        """`num_waiting_reqs` ist untypisiert gelesen. Auch `-0.5` umgeht den
        Clamp auf dieselbe Weise - und wird nicht etwa durch eine
        Ganzzahligkeitsannahme vorher still zu `0`."""
        collector = self.with_load(num_reqs=0, num_waiting_reqs=-0.5)
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIsNotNone(state["slot"]["reason"])

    def test_zero_waiting_without_open_rids_is_free_and_not_unknown(self):
        """Die Gegenprobe, damit der Test oben nicht aus einem `free`-Fall ein
        `unknown` macht: `waiting == 0` ohne offene rids ist ein **freier**
        Slot. Kein Grund noetig, weil `unknown` gar nicht gemeldet wird."""
        collector = self.with_load(num_reqs=0, num_waiting_reqs=0)
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "free")
        self.assertIsNone(state["slot"]["reason"])

    # --- Befund 2: der Altbestand-Text ---------------------------------

    def test_legacy_queue_names_the_altbestand(self):
        """Kein Test suchte je nach "Altbestand" - der Zweig konnte ausfallen,
        ohne dass etwas auffiel (N4d).

        Fuenf wartende Requests, keiner davon im Log sichtbar: das ist der
        Altbestand aus der Zeit vor dem Request-Logging. Der Grund muss das
        sagen, sonst bleibt nur ein generischer String und der Leser weiss
        nicht, ob der Slot besetzt ist.
        """
        collector = self.with_load(num_reqs=5, num_waiting_reqs=5)
        state = collector.tick()
        self.assertEqual(state["slot"]["legacy"], 5)
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIn("Altbestand", state["slot"]["reason"])

    def test_legacy_reason_carries_the_number(self):
        """Der Altbestand ist eine **Zahl**, und sie ist beobachtbar. Ein
        Mutanten, der einen konstanten Wert einsetzt, waere ueber den blossen
        Textnachweis nicht zu fangen."""
        collector = self.with_load(num_reqs=7, num_waiting_reqs=7)
        reason = collector.tick()["slot"]["reason"]
        self.assertIn("Altbestand: 7 Request(s)", reason)

    def test_legacy_zero_does_not_claim_an_altbestand(self):
        """Die andere Richtung: `legacy == 0` darf keinen Altbestand behaupten.

        Es gibt keinen Altbestand, der nach der Anzeige ploetzlich auftaucht.
        Der Weg dahin ist derselbe `else`-Zweig wie in Befund 1 - der Fallback
        nennt keine Zahl, weil keine da ist.
        """
        collector = self.with_load(num_reqs=0, num_waiting_reqs=-1)
        state = collector.tick()
        self.assertEqual(state["slot"]["legacy"], 0)
        self.assertNotIn("Altbestand", state["slot"]["reason"])

    def test_a_waiting_server_with_a_visible_request_is_not_altbestand(self):
        """`legacy` ist `waiting - open_count`: eine wartende Queue **mit** einem
        sichtbaren Halter ist kein Altbestand. Der Slot ist belegt, die Zahl der
        Wartenden ist aber echt - das gehoert in den Grund, damit der
        Alteste-vs.-Halter-Unterschied sichtbar bleibt."""
        collector = self.with_load(num_reqs=1, num_waiting_reqs=1)
        self.append(received(rid="aaa"))
        state = collector.tick()
        # legacy == 0 (1 wartend, 1 offen) -> kein Altbestand.
        self.assertEqual(state["slot"]["legacy"], 0)
        self.assertEqual(state["slot"]["rid"], "aaa")
        self.assertEqual(state["slot"]["state"], "busy")
        self.assertIsNone(state["slot"]["reason"])

    def test_unknown_waiting_does_not_claim_an_altbestand(self):
        """Auch der dritte Zweig des Blocks: bei unbekannter Warteschlange ist
        `legacy` gar nicht berechenbar (`None`). Ein Altbestand-Text waere hier
        eine erfundene Zahl - er kommt aus einem anderen Grund."""
        collector = self.with_load(num_reqs=None, num_waiting_reqs=None)
        state = collector.tick()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIsNotNone(state["slot"]["reason"])
        self.assertNotIn("Altbestand", state["slot"]["reason"])

    # --- Die Regel ueber alle drei Zweige ------------------------------

    def test_every_unknown_from_this_block_names_its_own_cause(self):
        """Die Regel in einer Schleife, statt dreimal dieselbe Assertion.

        Geprueft wird pro Fall **welcher** Grund, nicht bloss dass einer da
        ist: genau daran ist der `legacy`-Zweig still gescheitert.
        """
        cases = [
            ("queue unknown", {"num_reqs": None, "num_waiting_reqs": None},
             "Warteschlange unbekannt"),
            ("legacy queue", {"num_reqs": 5, "num_waiting_reqs": 5},
             "Altbestand"),
            ("negative waiting", {"num_reqs": 0, "num_waiting_reqs": -1},
             "bestimmbar"),
        ]
        for label, load, expected in cases:
            with self.subTest(case=label):
                state = self.with_load(**load).tick()
                self.assertEqual(state["slot"]["state"], "unknown")
                self.assertIsNotNone(state["slot"]["reason"])
                self.assertIn(expected, state["slot"]["reason"])


# ======================================================================
# Die Warteschlangenliste (Task 1)
# ======================================================================
#
# `queue.build()` ist rein und wird hier nur verdrahtet geprueft. Die
# beiden Tests, die der Plan verlangt, sind die Reihenfolge und die
# Altbestand-Sperre; die weiteren sind die Nahte, an denen eine Verdrahtung
# sonst still falsch steht.


class TestWaitingList(CollectorCase):
    """`state["queue"]["waiting_list"]`, aus dem Log gelesen.

    Der gemessene Zustand vom 2026-10-12 ist die Form aller Fixtures
    hier: `num_reqs` = offene rids im Log, `num_waiting_reqs` = davon
    einer weniger, weil der aelteste offene rid gerade laeuft.
    """

    def with_queue(self, num_reqs, num_waiting_reqs):
        collector = self.make()
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": num_reqs, "num_waiting_reqs": num_waiting_reqs})
        return collector

    def test_the_list_has_one_entry_per_waiting_request(self):
        """Drei Wartende, vier offene rids: drei Eintraege, Position 1-3."""
        collector = self.with_queue(num_reqs=4, num_waiting_reqs=3)
        # Absichtlich in falscher Reihenfolge ins Log geschrieben: die Liste
        # sortiert nach Empfangszeit, nicht nach Schreibreihenfolge.
        self.append(received(rid="wartet2", at=RECEIVED_AT + 2.0))
        self.append(received(rid="laeuft", at=RECEIVED_AT))
        self.append(received(rid="wartet3", at=RECEIVED_AT + 3.0))
        self.append(received(rid="wartet1", at=RECEIVED_AT + 1.0))
        state = collector.tick()
        liste = state["queue"]["waiting_list"]
        self.assertEqual([e["rid"] for e in liste],
                         ["wartet1", "wartet2", "wartet3"])
        self.assertEqual([e["position"] for e in liste], [1, 2, 3])
        self.assertEqual([e["waiting_since"] for e in liste],
                         [RECEIVED_AT + 1.0, RECEIVED_AT + 2.0,
                          RECEIVED_AT + 3.0])

    def test_the_running_holder_is_not_in_the_list(self):
        """Der Halter steht im Slot, nicht auf Position 1."""
        collector = self.with_queue(num_reqs=4, num_waiting_reqs=3)
        self.append(received(rid="laeuft", at=RECEIVED_AT))
        self.append(received(rid="wartet1", at=RECEIVED_AT + 1.0))
        self.append(received(rid="wartet2", at=RECEIVED_AT + 2.0))
        self.append(received(rid="wartet3", at=RECEIVED_AT + 3.0))
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "laeuft")
        self.assertNotIn("laeuft",
                         [e["rid"] for e in state["queue"]["waiting_list"]])

    def test_legacy_empties_the_list(self):
        """Altbestand > 0: offene rids da, Liste trotzdem leer.

        Sonst wuerde die Liste aus den offenen rids gebaut, obwohl der
        Server mehr Wartende meldet als das Log zeigt. Die Zahl stimmt
        dann nicht, und jede Position ist geraten.
        """
        collector = self.with_queue(num_reqs=2302, num_waiting_reqs=2301)
        self.append(received(rid="aaa"))
        state = collector.tick()
        self.assertEqual(state["slot"]["legacy"], 2300)
        self.assertEqual(state["queue"]["waiting_list"], [])

    def test_no_queue_before_the_first_tick(self):
        """/api/state liefert `state()`. Vor dem ersten Poll gibt es keine
        Liste - aber die Schluessel muss es geben."""
        self.assertEqual(self.make().state()["queue"]["waiting_list"], [])

    def test_no_list_while_the_server_is_down(self):
        """Ohne `/get_load` ist die Warteschlangenlaenge `None`, und eine
        Liste daraus waere geraten."""
        collector = self.make()
        collector.sources.read_load = lambda **kw: Sample(None, None, "down")
        self.append(received(rid="aaa"))
        state = collector.tick()
        self.assertIsNone(state["queue"]["waiting"])
        self.assertEqual(state["queue"]["waiting_list"], [])

    def test_no_list_when_nobody_waits(self):
        collector = self.with_queue(num_reqs=1, num_waiting_reqs=0)
        self.append(received(rid="aaa"))
        state = collector.tick()
        self.assertEqual(state["queue"]["waiting_list"], [])

    def test_a_waiting_request_has_no_name(self):
        """Kein erfundener Name.

        Ein Wartender traegt keine IP, und ein frueher abgeschlossener rid ist
        ein **anderer** rid. Aus einem Abschluss von 192.168.9.2 folgt fuer
        den Wartenden `wartet1` nichts - die Zuordnung gibt es nicht, und sie
        wird auch nicht geraten.
        """
        collector = self.with_queue(num_reqs=2, num_waiting_reqs=1)
        self.append(received(rid="aaa", at=RECEIVED_AT))
        collector.tick()
        self.append(finished(rid="aaa"), access())
        self.append(received(rid="wartet1", at=RECEIVED_AT + 1.0))
        state = collector.tick()
        self.assertEqual(state["history"][0]["system"], "Laptop")
        liste = state["queue"]["waiting_list"]
        self.assertEqual([e["rid"] for e in liste], ["wartet1"])
        self.assertIsNone(liste[0]["system"])

    def test_a_known_ip_port_shows_the_name(self):
        """Der Namens-Rueckfall, an der einen Stelle verdrahtet.

        `_known_ips` ist die Zuordnung aus **abgeschlossenen** Eintraegen
        (`rid -> ip_port`); `_queue_name` schlaegt darin nach und gibt den
        Namen aus `systems.json` weiter. Der Wartende traegt selbst keine IP -
        was hier bekannt ist, kommt aus einem frueheren Abschluss.
        """
        collector = self.with_queue(num_reqs=2, num_waiting_reqs=1)
        self.append(received(rid="laeuft", at=RECEIVED_AT))
        self.append(received(rid="wartet1", at=RECEIVED_AT + 1.0))
        collector._known_ips["wartet1"] = "192.168.9.2:44444"
        state = collector.tick()
        liste = state["queue"]["waiting_list"]
        self.assertEqual([e["rid"] for e in liste], ["wartet1"])
        self.assertEqual(liste[0]["system"], "Laptop")

    def test_an_unknown_ip_port_is_not_a_name(self):
        """Eine IP, die nicht in `systems.json` steht, ist kein Name.

        `names.resolve()` liefert dann die rohe IP zurueck. Die zeigt der
        Collector als Name an - der Betrachter erkennt sie wieder. Der
        Monitor behauptet damit nichts Falsches.
        """
        collector = self.make(names={})
        collector.sources.read_load = lambda **kw: self.sample(
            {"num_reqs": 2, "num_waiting_reqs": 1})
        self.append(received(rid="laeuft", at=RECEIVED_AT))
        self.append(received(rid="wartet1", at=RECEIVED_AT + 1.0))
        collector._known_ips["wartet1"] = "10.9.9.9:1111"
        state = collector.tick()
        self.assertEqual(state["queue"]["waiting_list"][0]["system"],
                         "10.9.9.9")

    def test_a_completed_request_remembers_its_ip_port(self):
        """`_known_ips` wird aus dem Abschluss gefuellt, sonst waere die
        Verdrahtung in `_queue_name` tote Schrift."""
        collector = self.with_queue(num_reqs=1, num_waiting_reqs=0)
        self.append(received(rid="aaa"))
        collector.tick()
        self.append(finished(rid="aaa"), access())
        collector.tick()
        self.assertEqual(collector._known_ips.get("aaa"), "192.168.9.2:44444")

    def test_the_ip_port_memory_stays_bounded(self):
        """Ein Monitor laeuft wochenlang.

        `_known_ips` waere sonst ein Dict, das mit jedem Abschluss waechst und
        nie etwas loescht. Die Grenze ist gross genug fuer die Queue (die
        Zustaende kommen aus `request.finished`) und klein genug, um keine
        Leckstelle zu sein.
        """
        from slotmon import collector as collector_modul
        collector = self.with_queue(num_reqs=1, num_waiting_reqs=0)
        for index in range(collector_modul.KNOWN_IPS_MAX + 5):
            collector._known_ips[f"rid{index:06d}"] = "192.168.9.2:44444"
        self.append(received(rid="neu"))
        collector.tick()
        self.append(finished(rid="neu"), access())
        collector.tick()
        self.assertEqual(len(collector._known_ips),
                         collector_modul.KNOWN_IPS_MAX)
        self.assertIn("neu", collector._known_ips)
        self.assertNotIn("rid000000", collector._known_ips)


if __name__ == "__main__":
    unittest.main()
