# GPU-Slot-Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein Dashboard, das zeigt welcher Client gerade den einzelnen GPU-Slot des sglang-Servers hält, wie lange, mit welcher Geschwindigkeit, und wie viele warten.

**Architecture:** Ein Bash-Lifecyle-Skript startet einen Python-Prozess, der alle 2 s den sglang-Server liest und den Request-Lifecycle aus dem Server-Log auswertet. Der Halter ist die älteste offene `rid` aus `request.received` ohne `request.finished` — FIFO ist die Reihenfolge bei `max_running_requests=1`. Abgeschlossene Slots landen als JSONL in der Historie, ein FastAPI-Server liefert die Daten per SSE an eine HTML-Seite.

**Tech Stack:** Bash, Python 3.12 (stdlib `unittest`, kein pytest), FastAPI + uvicorn (beides systemweit vorhanden), SSE, Vanilla JS im Browser.

**Spec:** `docs/superpowers/specs/2026-10-07-gpu-slot-monitor-design.md`

## Global Constraints

Diese Regeln gelten für **jeden** Task. Bei Widerspruch gilt die Spec, bei Widerspruch zur Spec gilt die hier stehende Regel — und die Spec wird mitgezogen.

- **Der sglang-Server wird nie neugestartet, nie verändert, nie beschädigt.** Kein Neustart, keine Flag-Änderung, kein Signal an PID 145265, kein Schreibzugriff auf `/tmp/escha_server_lan.log` außer dem dokumentierten `truncate -s 0` ab 1 GB. Der Monitor liest ausschließlich.
- **`ss` und `/proc/net/tcp` werden nicht abgefragt.** Die Socket-Methode ist widerlegt (Spec-Abschnitt *Korrektur vom 2026-10-07*) und darf nicht als Reserve zurückkehren. Task 11 prüft das per Zeichenkettensuche.
- **Kein `abort_all`, kein `abort_request`, kein Eingriff in fremde Requests.** Der Monitor liest. Punkt.
- **`/configure_logging` wird nicht aufgerufen.** Die Route existiert, aber sie zu rufen wäre ein Eingriff in die Server-Konfiguration.
- **Kein pytest, kein `pip install`.** PEP 668 verhindert Installation ins systemweite Python. Tests laufen mit `python3.12 -m unittest`. Kein venv wird angelegt.
- **`truncate -s 0`, niemals `rm`.** Der Server hält die Logdatei als `fd 1` und `fd 2` offen. Ein `rm` ließe die Datei als Anonymous-Inode weiterleben und den Platz erst beim Neustart freigeben — den es nicht gibt.
- **Niemand wird geraten.** Jeder Datenpunkt hat einen bekannten Zustand statt eines geratenen Werts. Fehlende Zahl heißt `–` oder „unbekannt", niemals `0`.
- **Tests schreiben nie auf den echten Server.** Server-Sourced Tests lesen nur. Tests mit Schreibwirkung nutzen Fixtures in `tempfile`.
- **Nach jedem Task ein Commit.** Nach jedem Task müssen alle Tests grün sein.

## File Structure

| Datei | Verantwortung |
|---|---|
| `monitor.sh` | Lebenszyklus: `start` / `stop` / `status`. Keine Zustandslogik, keine Datenauswertung. |
| `slotmon/config.py` | Liest alle Umgebungsvariablen mit Standardwerten an einer Stelle. Sonst nirgends `os.environ`. |
| `slotmon/logreader.py` | Liest das Server-Log inkrementell, parst `request.received` / `request.finished` und die uvicorn-Access-Zeilen. Kennt den Log nicht, weiß aber, wo er zuletzt war. |
| `slotmon/sources.py` | Fragt `/get_load`, `/metrics`, `nvidia-smi` und die Batch-Zeilen ab. Jede Quelle liefert einen Zustand mit Altersangabe. |
| `slotmon/slot.py` | Reiner Zustandsübergang: offene `rid`s plus `num_waiting_reqs` ergeben Halter, Altbestand, laufenden Slot. Kein I/O, keine Uhr. |
| `slotmon/history.py` | Lesen, Anhängen und Kürzen von `history.jsonl` nach Zeitfenster. |
| `slotmon/logrotate.py` | Ein einziger `stat` je Durchlauf, `truncate -s 0` ab Schwelle. |
| `slotmon/collector.py` | Führt die Einheiten zusammen und hält den Gesamtzustand. Endpunkt für das Dashboard. |
| `slotmon/server.py` | FastAPI-App, `/api/state`, `/api/stream` (SSE), `/` (HTML). |
| `slotmon/dashboard.html` | Die Seite. Reiner Lese-Puffer, kein Zustand außer der Reiterposition. |
| `systems.json` | IP → Anzeigename. |
| `tests/` | Eine Testdatei pro Einheit. |

Die Trennung folgt einer Regel: `logreader.py`, `history.py`, `logrotate.py` und `slot.py` sind **rein** — sie nehmen Bytes oder Dictionaries entgegen und geben Werte zurück. Nur `sources.py` und `collector.py` kennen die Außenwelt. Deshalb sind fast alle Tests ohne Server lauffähig.

---

## Task 1: Konfiguration

Der Rest des Codes liest seine Einstellungen hier. Steht am Anfang, weil jede spätere Datei sie braucht.

**Files:**
- Create: `slotmon/__init__.py`
- Create: `slotmon/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nichts
- Produces: `slotmon.config.Config` — eine Klasse mit Instanzattributen. `poll_interval: float`, `api: str`, `auth: str`, `server_log: str`, `monitor_log: str`, `history: str`, `retention_h: int`, `max_log_mb: int`, `warn_log_mb: int`, `host: str`, `port: int`, `systems_file: str`, `log_pair_window_s: float`
- Produces: `slotmon.config.load_config(env: dict | None = None) -> Config` — liest aus `env` (Default `os.environ`), ohne Datei-I/O, ohne Exceptions

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import os
import unittest

from slotmon.config import load_config


class TestDefaults(unittest.TestCase):
    def test_defaults_match_spec(self):
        cfg = load_config({})
        self.assertEqual(cfg.poll_interval, 2.0)
        self.assertEqual(cfg.api, "http://127.0.0.1:30000")
        self.assertEqual(cfg.auth, "Bearer marec")
        self.assertEqual(cfg.server_log, "/tmp/escha_server_lan.log")
        self.assertEqual(cfg.monitor_log, "/tmp/slotmon.log")
        self.assertEqual(cfg.history, "/tmp/slotmon/history.jsonl")
        self.assertEqual(cfg.retention_h, 24)
        self.assertEqual(cfg.max_log_mb, 1024)
        self.assertEqual(cfg.warn_log_mb, 900)
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertEqual(cfg.port, 8090)
        self.assertEqual(cfg.log_pair_window_s, 30.0)


class TestOverrides(unittest.TestCase):
    def test_every_value_is_overridable(self):
        cfg = load_config({
            "PORT": "9001",
            "HOST": "127.0.0.1",
            "API": "http://192.168.1.24:30000",
            "AUTH": "Bearer other",
            "SERVER_LOG": "/tmp/other.log",
            "MONITOR_LOG": "/tmp/m.log",
            "HISTORY": "/tmp/h.jsonl",
            "RETENTION_H": "6",
            "MAX_LOG_MB": "10",
            "WARN_LOG_MB": "8",
            "POLL_INTERVAL": "5",
            "LOG_PAIR_WINDOW_S": "60",
        })
        self.assertEqual(cfg.port, 9001)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.api, "http://192.168.1.24:30000")
        self.assertEqual(cfg.auth, "Bearer other")
        self.assertEqual(cfg.server_log, "/tmp/other.log")
        self.assertEqual(cfg.monitor_log, "/tmp/m.log")
        self.assertEqual(cfg.history, "/tmp/h.jsonl")
        self.assertEqual(cfg.retention_h, 6)
        self.assertEqual(cfg.max_log_mb, 10)
        self.assertEqual(cfg.warn_log_mb, 8)
        self.assertEqual(cfg.poll_interval, 5.0)
        self.assertEqual(cfg.log_pair_window_s, 60.0)

    def test_numeric_strings_become_numbers(self):
        cfg = load_config({"PORT": "9001", "POLL_INTERVAL": "0.5"})
        self.assertIsInstance(cfg.port, int)
        self.assertIsInstance(cfg.poll_interval, float)
        self.assertEqual(cfg.poll_interval, 0.5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_config -v` (from `/home/user/small_qwen`)
Expected: `ModuleNotFoundError: No module named 'slotmon'`

- [ ] **Step 3: Write minimal implementation**

```python
# slotmon/config.py
"""Alle Einstellungen an einer Stelle. Sonst liest niemand os.environ."""

import os


class Config:
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)

    def __repr__(self):
        fields = ", ".join(f"{k}={v!r}" for k, v in sorted(self.__dict__.items()))
        return f"Config({fields})"


DEFAULTS = {
    "PORT": ("port", int, 8090),
    "HOST": ("host", str, "0.0.0.0"),
    "API": ("api", str, "http://127.0.0.1:30000"),
    "AUTH": ("auth", str, "Bearer marec"),
    "SERVER_LOG": ("server_log", str, "/tmp/escha_server_lan.log"),
    "MONITOR_LOG": ("monitor_log", str, "/tmp/slotmon.log"),
    "HISTORY": ("history", str, "/tmp/slotmon/history.jsonl"),
    "SYSTEMS_FILE": ("systems_file", str, "systems.json"),
    "RETENTION_H": ("retention_h", int, 24),
    "MAX_LOG_MB": ("max_log_mb", int, 1024),
    "WARN_LOG_MB": ("warn_log_mb", int, 900),
    "POLL_INTERVAL": ("poll_interval", float, 2.0),
    "LOG_PAIR_WINDOW_S": ("log_pair_window_s", float, 30.0),
}


def load_config(env=None):
    source = os.environ if env is None else env
    values = {}
    for env_name, (attr, cast, default) in DEFAULTS.items():
        raw = source.get(env_name)
        if raw is None or raw == "":
            values[attr] = default
        else:
            values[attr] = cast(raw)
    return Config(**values)
```

```python
# slotmon/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m unittest tests.test_config -v`
Expected: `Ran 4 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add slotmon/__init__.py slotmon/config.py tests/test_config.py
git commit -m "feat: Konfiguration an einer Stelle"
```

---

## Task 2: Systemnamen

**Files:**
- Create: `slotmon/names.py`
- Create: `systems.json`
- Test: `tests/test_names.py`

**Interfaces:**
- Consumes: nichts
- Produces: `slotmon.names.load_names(path: str) -> dict[str, str]` — lädt die Datei, **wirft** `SystemExit` bei fehlender Datei oder Syntaxfehler, fehlende IP ist kein Fehler
- Produces: `slotmon.names.resolve(names: dict[str, str], ip: str | None) -> str | None` — `None` bei unbekannter oder fehlender IP, sonst Name bzw. rohe IP

- [ ] **Step 1: Write the failing test**

```python
# tests/test_names.py
import json
import os
import tempfile
import unittest

from slotmon.names import load_names, resolve


class TestLoad(unittest.TestCase):
    def _write(self, content):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        handle.write(content)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_reads_mapping(self):
        path = self._write(json.dumps({"192.168.9.2": "Laptop"}))
        self.assertEqual(load_names(path), {"192.168.9.2": "Laptop"})

    def test_missing_file_aborts(self):
        with self.assertRaises(SystemExit):
            load_names("/tmp/does-not-exist-slotmon.json")

    def test_broken_syntax_aborts(self):
        path = self._write("{not json")
        with self.assertRaises(SystemExit):
            load_names(path)

    def test_wrong_shape_aborts(self):
        path = self._write('["a", "b"]')
        with self.assertRaises(SystemExit):
            load_names(path)


class TestResolve(unittest.TestCase):
    def test_known_ip_gives_name(self):
        self.assertEqual(resolve({"192.168.9.2": "Laptop"}, "192.168.9.2"), "Laptop")

    def test_unknown_ip_falls_back_to_raw_ip(self):
        self.assertEqual(resolve({"192.168.9.2": "Laptop"}, "10.0.0.5"), "10.0.0.5")

    def test_missing_ip_gives_none(self):
        self.assertIsNone(resolve({"192.168.9.2": "Laptop"}, None))

    def test_empty_ip_gives_none(self):
        self.assertIsNone(resolve({}, ""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_names -v`
Expected: `ModuleNotFoundError: No module named 'slotmon.names'`

- [ ] **Step 3: Write minimal implementation**

```python
# slotmon/names.py
"""IP zu Anzeigename. Fehlende IP ist kein Fehler, kaputte Datei schon."""

import json
import sys


def load_names(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        sys.exit(f"[slotmon] systems.json fehlt: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"[slotmon] systems.json ist kaputt: {path} ({exc})")
    except OSError as exc:
        sys.exit(f"[slotmon] systems.json nicht lesbar: {path} ({exc})")

    if not isinstance(data, dict):
        sys.exit(f"[slotmon] systems.json muss ein Objekt sein, nicht {type(data).__name__}")
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            sys.exit(f"[slotmon] systems.json: Eintrag {key!r} ist kein Text-Text-Paar")
    return data


def resolve(names, ip):
    if not ip:
        return None
    return names.get(ip, ip)
```

```json
// systems.json
{
  "192.168.9.2": "Laptop",
  "192.168.1.24": "Dieser Server",
  "127.0.0.1": "Lokal (opencode & Co.)"
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m unittest tests.test_names -v`
Expected: `Ran 7 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add slotmon/names.py systems.json tests/test_names.py
git commit -m "feat: IP-zu-Name-Auflösung mit systems.json"
```

---

## Task 3: Log-Parser

Das Herzstück. Hier entscheidet sich, ob der Monitor überhaupt etwas anzeigen kann.

**Files:**
- Create: `slotmon/logreader.py`
- Create: `tests/fixtures/log_sample.txt`
- Test: `tests/test_logreader.py`

**Interfaces:**
- Consumes: keine
- Produces: `slotmon.logreader.LogEvent` — Dataclass mit `kind: str` (`"received"` oder `"finished"`), `rid: str`, `at: float` (Unix-Zeit), `meta: dict`
- Produces: `slotmon.logreader.LogReader(path: str, pair_window_s: float = 30.0)` mit:
  - `poll() -> list[LogEvent]` — liest ab der gemerkten Position, liefert alle Events seit dem letzten Aufruf, leeres Ergebnis wenn nichts Neues da ist
  - `state` — Attribut: `dict` mit den offenen `rid`s (`rid -> received_at`) und `seen: set[str]`
  - `reset()` — leert `state` und setzt die Position ans Dateiende

Die `finished`-Events tragen bereits die aufgelöste IP im Feld `meta["ip_port"]`.

**Echte Logzeilen, gegen die der Parser getestet wird** (aus dem laufenden Server, unverändert):

```
[2026-10-07 17:19:07] INFO:     127.0.0.1:40076 - "GET /get_load HTTP/1.1" 200 OK
{"timestamp": "2026-10-07T17:19:07.123456", "event": "request.received", "rid": "a93031d87de846e187ddc80afcfb73eb", "obj": {"rid": "a93031d87de846e187ddc80afcfb73eb", "received_time": 437300.5}}
[2026-10-07 17:19:20] INFO:     127.0.0.1:40076 - "POST /v1/chat/completions HTTP/1.1" 200 OK
{"timestamp": "2026-10-07T17:19:24.654321", "event": "request.finished", "rid": "d7d50a43cae04ef3a11f625fc1939662", "obj": {"rid": "d7d50a43cae04ef3a11f625fc1939662", "received_time": 437290.25}, "out": {"meta_info": {"id": "d7d50a43cae04ef3a11f625fc1939662", "finish_reason": {"type": "stop", "matched": 248046}, "prompt_tokens": 4270, "completion_tokens": 1687, "cached_tokens": 0}}}
```

- [ ] **Step 1: Write the failing test**

```python
# tests/fixtures/log_sample.txt
[2026-10-07 17:19:07] INFO:     127.0.0.1:40076 - "GET /get_load HTTP/1.1" 200 OK
{"timestamp": "2026-10-07T17:19:07.123456", "event": "request.received", "rid": "a93031d87de846e187ddc80afcfb73eb", "obj": {"rid": "a93031d87de846e187ddc80afcfb73eb", "received_time": 437300.5}}
[2026-10-07 17:19:20] INFO:     127.0.0.1:40076 - "POST /v1/chat/completions HTTP/1.1" 200 OK
{"timestamp": "2026-10-07T17:19:24.654321", "event": "request.finished", "rid": "d7d50a43cae04ef3a11f625fc1939662", "obj": {"rid": "d7d50a43cae04ef3a11f625fc1939662", "received_time": 437290.25}, "out": {"meta_info": {"id": "d7d50a43cae04ef3a11f625fc1939662", "finish_reason": {"type": "stop", "matched": 248046}, "prompt_tokens": 4270, "completion_tokens": 1687, "cached_tokens": 0}}}
```

```python
# tests/test_logreader.py
import os
import shutil
import tempfile
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
        self.assertAlmostEqual(events[0].at, 437300.5, delta=1.0)

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
        self.append(RECEIVED)
        self.reader.poll()
        self.append(FINISHED)
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


class TestTruncation(ReaderCase):
    def test_shrink_is_detected_and_reading_restarts(self):
        self.append(RECEIVED)
        self.reader.poll()
        self.assertEqual(len(self.reader.state["open"]), 1)
        open(os.path.join(self.dir, "server.log"), "w", encoding="utf-8").close()
        self.append(RECEIVED)
        events = self.reader.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(len(self.reader.state["open"]), 1)

    def test_missing_file_is_not_fatal(self):
        events = self.reader.poll()
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_logreader -v`
Expected: `ModuleNotFoundError: No module named 'slotmon.logreader'`

- [ ] **Step 3: Write minimal implementation**

```python
# slotmon/logreader.py
"""Liest das Server-Log inkrementell und wertet den Request-Lifecycle aus.

Rein: bekommt einen Pfad, gibt Events zurueck. Kein Netzwerk, keine Uhr.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field

RECEIVED = "received"
FINISHED = "finished"

ACCESS_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+INFO:\s+"
    r"(?P<peer>\S+)\s+-\s+\"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/[\d.]+\"\s+"
    r"(?P<status>\d+)"
)


@dataclass
class LogEvent:
    kind: str
    rid: str
    at: float
    meta: dict = field(default_factory=dict)


def _parse_access_ts(text):
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def _finish_reason(meta_info):
    reason = meta_info.get("finish_reason")
    if isinstance(reason, dict):
        return reason.get("type")
    return reason


def _meta_from_finished(record):
    obj = record.get("obj") or {}
    meta_info = ((record.get("out") or {}).get("meta_info")) or {}
    return {
        "prompt_tokens": meta_info.get("prompt_tokens"),
        "completion_tokens": meta_info.get("completion_tokens"),
        "cached_tokens": meta_info.get("cached_tokens"),
        "finish_reason": _finish_reason(meta_info),
        "matched": (meta_info.get("finish_reason") or {}).get("matched")
        if isinstance(meta_info.get("finish_reason"), dict) else None,
        "received_time": obj.get("received_time"),
        "ip_port": None,
    }


class LogReader:
    def __init__(self, path, pair_window_s=30.0):
        self.path = path
        self.pair_window_s = pair_window_s
        self.offset = 0
        self.partial = ""
        self.pending_access = []
        self.state = {"open": {}, "seen": set()}

    def reset(self):
        self.offset = 0
        self.partial = ""
        self.pending_access = []
        self.state = {"open": {}, "seen": set()}

    def _read_bytes(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return ""
        if size < self.offset:
            # Datei wurde extern gekuerzt -> von vorn lesen
            self.offset = 0
            self.partial = ""
        if size == self.offset:
            return ""
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                chunk = handle.read(size - self.offset)
                self.offset = handle.tell()
        except OSError:
            return ""
        return chunk

    def _collect(self, line):
        access = ACCESS_RE.search(line)
        if access:
            peer = access.group("peer")
            if ":" in peer and peer.count(":") == 1:
                ip_port = peer
            elif peer.startswith("["):
                ip_port = peer
            else:
                ip_port = peer
            self.pending_access.append(
                {
                    "ip_port": ip_port,
                    "path": access.group("path"),
                    "at": _parse_access_ts(access.group("ts")),
                }
            )
            return None

        stripped = line.lstrip()
        if not stripped.startswith('"timestamp"') and '"event"' not in line:
            return None
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None

        kind_name = record.get("event")
        rid = record.get("rid")
        if not rid or kind_name not in ("request.received", "request.finished"):
            return None

        stamp = record.get("timestamp", "")
        try:
            at = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            at = time.time()

        if kind_name == "request.received":
            meta = {"received_time": (record.get("obj") or {}).get("received_time"),
                    "ip_port": None}
            kind = RECEIVED
        else:
            meta = _meta_from_finished(record)
            kind = FINISHED

        return LogEvent(kind=kind, rid=rid, at=at, meta=meta)

    def _attach_ip(self, events):
        for event in events:
            if event.kind != FINISHED:
                continue
            best = None
            for access in self.pending_access:
                if not access["path"].startswith("/v1/"):
                    continue
                if abs(access["at"] - event.at) <= self.pair_window_s:
                    if best is None or abs(access["at"] - event.at) < abs(best["at"] - event.at):
                        best = access
            if best is not None:
                event.meta["ip_port"] = best["ip_port"]
            self.pending_access = [a for a in self.pending_access if a is not best]
        return events

    def _apply(self, events):
        for event in events:
            if event.kind == RECEIVED:
                if event.rid not in self.state["seen"]:
                    self.state["seen"].add(event.rid)
                    self.state["open"][event.rid] = event.meta.get("received_time") or event.at
            else:
                self.state["seen"].add(event.rid)
                self.state["open"].pop(event.rid, None)
        return events

    def poll(self):
        chunk = self._read_bytes()
        if not chunk and not self.partial:
            return []
        text = self.partial + chunk
        lines = text.split("\n")
        self.partial = lines.pop()
        events = []
        for line in lines:
            found = self._collect(line)
            if found is not None:
                events.append(found)
        self._attach_ip(events)
        return self._apply(events)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m unittest tests.test_logreader -v`
Expected: `Ran 17 tests ... OK`

- [ ] **Step 5: Verify against the real server log**

Run:
```bash
python3.12 -c "
from slotmon.logreader import LogReader
r = LogReader('/tmp/escha_server_lan.log')
events = r.poll()
finished = [e for e in events if e.kind == 'finished']
print(f'{len(events)} Events gelesen, davon {len(finished)} finished')
for e in finished[-3:]:
    print(e.rid[:8], e.meta['completion_tokens'], 'tok | ip:', e.meta['ip_port'])
print(f'offene rids: {len(r.state[\"open\"])}')
"
```
Expected: eine Zahl im dreistelligen Bereich, Tokenzahlen ungleich `None`. Wird `0 Events` gemeldet, dann gegenprüfen mit `grep -c "request.finished" /tmp/escha_server_lan.log` — der echte Log muss dann deutlich mehr enthalten, und `offset_muss_ans_ende_springen` ist zu prüfen.

- [ ] **Step 6: Commit**

```bash
git add slotmon/logreader.py tests/fixtures/log_sample.txt tests/test_logreader.py
git commit -m "feat: Log-Parser fuer den Request-Lifecycle"
```

---

## Task 4: Slot-Logik

Reine Zustandsberechnung, kein I/O. Hier entstehen Halter und Altbestand.

**Files:**
- Create: `slotmon/slot.py`
- Test: `tests/test_slot.py`

**Interfaces:**
- Consumes: `dict[str, float]` offene `rid`s (aus `LogReader.state["open"]`)
- Produces: `slotmon.slot.SlotState` — Dataclass:
  - `holder_rid: str | None` — `None`, wenn nichts offen oder Altbestand > 0
  - `holder_since: float | None` — `received_time` des Halters
  - `open_count: int`
  - `legacy: int` — Altbestand
  - `complete: bool` — `True`, wenn `legacy <= 0`
- Produces: `slotmon.slot.evaluate(open_rids: dict, waiting: int | None) -> SlotState` — `waiting=None` bedeutet unbekannt, dann `legacy=None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_slot.py
import unittest

from slotmon.slot import evaluate


class TestHolder(unittest.TestCase):
    def test_oldest_rid_is_holder(self):
        state = evaluate({"aaa": 100.0, "bbb": 300.0, "ccc": 200.0}, waiting=0)
        self.assertEqual(state.holder_rid, "bbb")
        self.assertEqual(state.holder_since, 300.0)

    def test_single_open_rid_is_holder(self):
        state = evaluate({"aaa": 100.0}, waiting=0)
        self.assertEqual(state.holder_rid, "aaa")

    def test_no_open_rid_means_free(self):
        state = evaluate({}, waiting=40)
        self.assertIsNone(state.holder_rid)
        self.assertIsNone(state.holder_since)
        self.assertEqual(state.open_count, 0)


class TestLegacy(unittest.TestCase):
    def test_legacy_is_queue_minus_open(self):
        state = evaluate({"a": 1.0, "b": 2.0, "c": 3.0}, waiting=2301)
        self.assertEqual(state.legacy, 2298)
        self.assertEqual(state.open_count, 3)

    def test_legacy_blocks_the_holder(self):
        state = evaluate({"a": 1.0}, waiting=2301)
        self.assertEqual(state.legacy, 2300)
        self.assertIsNone(state.holder_rid)
        self.assertFalse(state.complete)

    def test_zero_legacy_means_complete(self):
        state = evaluate({"a": 1.0}, waiting=1)
        self.assertEqual(state.legacy, 0)
        self.assertEqual(state.holder_rid, "a")
        self.assertTrue(state.complete)

    def test_unknown_waiting_yields_unknown_legacy(self):
        state = evaluate({"a": 1.0}, waiting=None)
        self.assertIsNone(state.legacy)
        self.assertIsNone(state.holder_rid)
        self.assertFalse(state.complete)

    def test_negative_result_is_clamped_to_zero(self):
        state = evaluate({"a": 1.0, "b": 2.0}, waiting=0)
        self.assertEqual(state.legacy, 0)
        self.assertTrue(state.complete)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_slot -v`
Expected: `ModuleNotFoundError: No module named 'slotmon.slot'`

- [ ] **Step 3: Write minimal implementation**

```python
# slotmon/slot.py
"""Aus offenen rids und der Queue-Laenge folgen Halter und Altbestand.

Rein: keine Uhr, kein I/O. Die Zeit kommt von aussen herein.
"""

from dataclasses import dataclass


@dataclass
class SlotState:
    holder_rid: str | None
    holder_since: float | None
    open_count: int
    legacy: int | None
    complete: bool


def evaluate(open_rids, waiting):
    open_count = len(open_rids)
    if waiting is None:
        return SlotState(None, None, open_count, None, False)

    legacy = max(0, waiting - open_count)
    complete = legacy == 0
    if not complete or not open_rids:
        return SlotState(None, None, open_count, legacy, complete)

    holder_rid = min(open_rids, key=lambda rid: open_rids[rid])
    return SlotState(holder_rid, open_rids[holder_rid], open_count, legacy, True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m unittest tests.test_slot -v`
Expected: `Ran 9 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add slotmon/slot.py tests/test_slot.py
git commit -m "feat: Halter- und Altbestandsberechnung"
```

---

## Task 5: Quellen

Der einzige Task mit Netzwerk. Jede Quelle liefert einen Zustand mit Alter.

**Files:**
- Create: `slotmon/sources.py`
- Test: `tests/test_sources.py`

**Interfaces:**
- Consumes: `slotmon.config.Config`
- Produces: `slotmon.sources.Sample` — Dataclass mit `value`, `at: float | None`, `error: str | None`. `value is None` und `error is None` bedeutet „noch nie geliefert".
- Produces: `slotmon.sources.ServerSources(cfg: Config)` mit:
  - `read_load() -> Sample` — `value` ist `dict` mit `num_reqs`, `num_waiting_reqs`
  - `read_server_info() -> Sample` — `value` ist `dict` mit `max_req_input_len`
  - `read_metrics() -> Sample` — `value` ist `dict` mit `gen_throughput`, `num_queue_reqs`, `num_running_reqs`
  - `read_gpu() -> Sample` — `value` ist `dict` mit `util`, `used_mb`, `total_mb`
- Beide HTTP-Methoden und `read_gpu` nehmen einen `timeout: float` — Default 3.0

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sources.py
import json
import unittest
from unittest import mock

import httpx

from slotmon.config import load_config
from slotmon.sources import ServerSources


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config({"API": "http://testserver:30000", "AUTH": "Bearer marec"})
        self.sources = ServerSources(self.cfg)

    def test_load_parses_queue_length(self):
        payload = [{"num_reqs": 12, "num_waiting_reqs": 11}]
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text=json.dumps(payload))) as get:
            sample = self.sources.read_load()
        self.assertEqual(sample.value["num_waiting_reqs"], 11)
        self.assertEqual(sample.value["num_reqs"], 12)
        self.assertIsNone(sample.error)
        self.assertIsNotNone(sample.at)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer marec")

    def test_load_error_is_captured_not_raised(self):
        with mock.patch("httpx.get", side_effect=httpx.ConnectError("down")):
            sample = self.sources.read_load()
        self.assertIsNone(sample.value)
        self.assertEqual(sample.error, "down")

    def test_load_http_error_is_captured(self):
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=401, text="unauthorized")):
            sample = self.sources.read_load()
        self.assertIsNone(sample.value)
        self.assertIn("401", sample.error)

    def test_load_garbage_is_captured(self):
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text="not json")):
            sample = self.sources.read_load()
        self.assertIsNone(sample.value)
        self.assertIsNotNone(sample.error)

    def test_metrics_are_parsed(self):
        body = (
            "# HELP x help\n"
            "sglang:gen_throughput 49.05\n"
            "sglang:num_queue_reqs 2301\n"
            "sglang:num_running_reqs 1\n"
        )
        with mock.patch("httpx.get", return_value=mock.Mock(status_code=200, text=body)):
            sample = self.sources.read_metrics()
        self.assertAlmostEqual(sample.value["gen_throughput"], 49.05)
        self.assertEqual(sample.value["num_queue_reqs"], 2301)
        self.assertEqual(sample.value["num_running_reqs"], 1)

    def test_missing_metric_yields_none(self):
        with mock.patch("httpx.get", return_value=mock.Mock(status_code=200, text="# empty\n")):
            sample = self.sources.read_metrics()
        self.assertIsNone(sample.value["gen_throughput"])
        self.assertIsNone(sample.error)

    def test_server_info_gives_max_req_input_len(self):
        payload = {"max_req_input_len": 83994, "max_total_num_tokens": 89856}
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text=json.dumps(payload))):
            sample = self.sources.read_server_info()
        self.assertEqual(sample.value["max_req_input_len"], 83994)


class TestGpu(unittest.TestCase):
    def setUp(self):
        self.sources = ServerSources(load_config({}))

    def test_gpu_values_are_parsed(self):
        output = "42, 15424, 16303\n"
        with mock.patch("subprocess.run", return_value=mock.Mock(
                returncode=0, stdout=output, stderr="")):
            sample = self.sources.read_gpu()
        self.assertEqual(sample.value["util"], 42)
        self.assertEqual(sample.value["used_mb"], 15424)
        self.assertEqual(sample.value["total_mb"], 16303)

    def test_missing_nvidia_smi_is_captured(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            sample = self.sources.read_gpu()
        self.assertIsNone(sample.value)
        self.assertIsNotNone(sample.error)

    def test_gpu_failure_never_reports_zero(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(
                returncode=1, stdout="", stderr="boom")):
            sample = self.sources.read_gpu()
        self.assertIsNone(sample.value)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_sources -v`
Expected: `ModuleNotFoundError: No module named 'slotmon.sources'`

- [ ] **Step 3: Write minimal implementation**

```python
# slotmon/sources.py
"""Fragt den sglang-Server. Jede Quelle liefert einen Zustand, wirft nichts."""

import json
import re
import subprocess
import time
from dataclasses import dataclass

import httpx

METRIC_NAMES = ("gen_throughput", "num_queue_reqs", "num_running_reqs")
METRIC_LINE = re.compile(r"^(?P<name>\w+)\s+(?P<value>[-\d.eE+]+)$")


@dataclass
class Sample:
    value: object = None
    at: float | None = None
    error: str | None = None

    @property
    def fresh(self):
        return self.value is not None and self.at is not None


class ServerSources:
    def __init__(self, cfg):
        self.cfg = cfg

    def _get(self, path, headers=None, timeout=3.0):
        url = self.cfg.api + path
        try:
            response = httpx.get(url, headers=headers or {}, timeout=timeout)
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}"
        return response.text, None

    def read_load(self, timeout=3.0):
        text, error = self._get(
            "/get_load", headers={"Authorization": self.cfg.auth}, timeout=timeout
        )
        if error:
            return Sample(None, None, error)
        try:
            payload = json.loads(text)
            entry = payload[0] if isinstance(payload, list) else payload
            value = {
                "num_reqs": entry.get("num_reqs"),
                "num_waiting_reqs": entry.get("num_waiting_reqs"),
            }
        except (json.JSONDecodeError, IndexError, KeyError, TypeError, AttributeError) as exc:
            return Sample(None, None, f"parse: {exc}")
        return Sample(value, time.time(), None)

    def read_server_info(self, timeout=3.0):
        text, error = self._get(
            "/get_server_info", headers={"Authorization": self.cfg.auth}, timeout=timeout
        )
        if error:
            return Sample(None, None, error)
        try:
            payload = json.loads(text)
            value = {"max_req_input_len": payload.get("max_req_input_len")}
        except (json.JSONDecodeError, AttributeError) as exc:
            return Sample(None, None, f"parse: {exc}")
        return Sample(value, time.time(), None)

    def read_metrics(self, timeout=3.0):
        text, error = self._get("/metrics", timeout=timeout)
        if error:
            return Sample(None, None, error)
        found = {}
        for line in text.splitlines():
            match = METRIC_LINE.match(line.strip())
            if match and match.group("name") in METRIC_NAMES:
                try:
                    found[match.group("name")] = float(match.group("value"))
                except ValueError:
                    continue
        for name in METRIC_NAMES:
            found.setdefault(name, None)
        return Sample(found, time.time(), None)

    def read_gpu(self, timeout=3.0):
        try:
            done = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return Sample(None, None, f"{type(exc).__name__}: {exc}")
        if done.returncode != 0:
            return Sample(None, None, f"rc={done.returncode}: {done.stderr.strip()[:80]}")
        line = done.stdout.strip().splitlines()
        if not line:
            return Sample(None, None, "leere Ausgabe")
        parts = [part.strip() for part in line[0].split(",")]
        if len(parts) != 3:
            return Sample(None, None, "unerwartetes Format")
        try:
            util, used, total = (int(float(part)) for part in parts)
        except ValueError as exc:
            return Sample(None, None, f"parse: {exc}")
        return Sample({"util": util, "used_mb": used, "total_mb": total}, time.time(), None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m unittest tests.test_sources -v`
Expected: `Ran 11 tests ... OK`

- [ ] **Step 5: Verify against the real server**

Run:
```bash
python3.12 -c "
from slotmon.config import load_config
from slotmon.sources import ServerSources
s = ServerSources(load_config({}))
for name in ('read_load', 'read_metrics', 'read_gpu'):
    sample = getattr(s, name)()
    print(f'{name}: value={sample.value} error={sample.error}')
"
```
Expected: `read_load` zeigt eine Wartenden-Zahl im dreistelligen Bereich, `read_metrics` zeigt `gen_throughput`, `read_gpu` zeigt `util`. Kein `error`.

- [ ] **Step 6: Commit**

```bash
git add slotmon/sources.py tests/test_sources.py
git commit -m "feat: Server-Quellen mit Fehlerzuständen statt Exceptions"
```

---

## Task 6: Historie

**Files:**
- Create: `slotmon/history.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Consumes: Pfad, `retention_h`
- Produces: `slotmon.history.History(path: str, retention_h: int = 24)` mit:
  - `load(now: float | None = None) -> list[dict]` — nur Einträge im Zeitfenster, überspringt kaputte Zeilen
  - `append(entry: dict)` — eine JSON-Zeile
  - `compact(now: float | None = None) -> int` — schreibt nur junge Einträge zurück, liefert deren Anzahl
- Ein Eintrag hat die Felder `rid`, `system`, `ip_port`, `started_at`, `ended_at`, `duration_s`, `prompt_tokens`, `completion_tokens`, `cached_tokens`, `finish_reason`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_history.py
import json
import os
import shutil
import tempfile
import unittest

from slotmon.history import History

NOW = 1_000_000.0


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotmon-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "history.jsonl")

    def write(self, entries):
        with open(self.path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def read(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


def entry(ended_at, rid="x"):
    return {"rid": rid, "system": "Laptop", "ended_at": ended_at, "duration_s": 12.0}


class TestLoad(HistoryCase):
    def test_missing_file_gives_empty_list(self):
        history = History(self.path, 24)
        self.assertEqual(history.load(NOW), [])

    def test_only_recent_entries_load(self):
        self.write([entry(NOW - 3600), entry(NOW - 30 * 3600)])
        loaded = History(self.path, 24).load(NOW)
        self.assertEqual(len(loaded), 1)

    def test_broken_line_is_skipped(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"rid": "a", "ended_at": 1}\n')
            handle.write("kaputt\n")
            handle.write(json.dumps(entry(NOW - 60)) + "\n")
        loaded = History(self.path, 24).load(NOW)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["rid"], "x")

    def test_entry_without_ended_at_is_skipped(self):
        self.write([{"rid": "a"}])
        self.assertEqual(History(self.path, 24).load(NOW), [])


class TestAppend(HistoryCase):
    def test_append_creates_file(self):
        history = History(self.path, 24)
        history.append(entry(NOW))
        self.assertEqual(len(self.read()), 1)

    def test_append_accumulates(self):
        history = History(self.path, 24)
        for i in range(3):
            history.append(entry(NOW, rid=f"r{i}"))
        self.assertEqual([e["rid"] for e in self.read()], ["r0", "r1", "r2"])

    def test_append_creates_missing_directory(self):
        nested = os.path.join(self.dir, "deep", "history.jsonl")
        History(nested, 24).append(entry(NOW))
        self.assertTrue(os.path.exists(nested))


class TestCompact(HistoryCase):
    def test_compact_drops_old_entries(self):
        self.write([entry(NOW - 30 * 3600, rid="old"), entry(NOW - 60, rid="new")])
        history = History(self.path, 24)
        history.compact(NOW)
        self.assertEqual([e["rid"] for e in self.read()], ["new"])

    def test_compact_returns_kept_count(self):
        self.write([entry(NOW - 30 * 3600), entry(NOW - 60), entry(NOW - 120)])
        self.assertEqual(History(self.path, 24).compact(NOW), 2)

    def test_compact_on_missing_file_is_harmless(self):
        history = History(self.path, 24)
        self.assertEqual(history.compact(NOW), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_history -v`
Expected: `ModuleNotFoundError: No module named 'slotmon.history'`

- [ ] **Step 3: Write minimal implementation**

```python
# slotmon/history.py
"""Abgeschlossene Slots als JSONL. Ueberlebt einen Neustart, waechst nicht unbegrenzt."""

import json
import os
import time


class History:
    def __init__(self, path, retention_h=24):
        self.path = path
        self.retention_s = retention_h * 3600

    def _fresh(self, entry, now):
        ended_at = entry.get("ended_at")
        if not isinstance(ended_at, (int, float)):
            return False
        return (now - ended_at) <= self.retention_s

    def load(self, now=None):
        now = time.time() if now is None else now
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            return []
        kept = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and self._fresh(entry, now):
                kept.append(entry)
        return kept

    def append(self, entry):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def compact(self, now=None):
        now = time.time() if now is None else now
        kept = self.load(now)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            for entry in kept:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return len(kept)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m unittest tests.test_history -v`
Expected: `Ran 10 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add slotmon/history.py tests/test_history.py
git commit -m "feat: JSONL-Historie mit Zeitfenster"
```

---

## Task 7: Log-Aufräumen

**Files:**
- Create: `slotmon/logrotate.py`
- Test: `tests/test_logrotate.py`

**Interfaces:**
- Consumes: Pfade, Schwellen
- Produces: `slotmon.logrotate.LogRotator(max_mb: int = 1024, warn_mb: int = 900, every: int = 60)` mit:
  - `check(path: str) -> RotateResult` — `RotateResult` ist ein Dataclass mit `size_mb: float | None`, `truncated: bool`, `warn: bool`, `error: str | None`
  - Zähler intern: `check` gibt bei jedem 60. Aufruf `stat` zurück. Das `every` wird über einen internen Zähler gesteuert und `force=True` erzwingt eine Prüfung
  - `check(path)` ist die **einzige** Taktungsautorität. Der Collector ruft sie **jeden Poll**; sie self-gated intern und kostet in den übersprungenen Ticks **null** Syscalls. Es gibt **keine** zweite Taktungsfunktion und keinen vom Aufrufer gepflegten Zähler.
  - `is_allowed(path: str) -> bool` — Basename `^(escha_server.*\.log|slotmon\.log)$` **und** `realpath` unter `/tmp`
  - `every` bleibt **60** (`60 × poll_interval 2.0 s` = exakt 2 Minuten). Nicht aus `poll_interval` ableiten — das ergäbe 120 und verdoppelte das Intervall.
  - **`size_mb` ist `None` auf einem übersprungenen Tick.** Das ist „nicht gemessen", nicht „Größe 0".

**Der entscheidende Test** ist der letzte: eine offene Datei, `truncate`, weiter schreiben, Inhalt muss da sein.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_logrotate.py
import os
import shutil
import tempfile
import unittest

from slotmon.logrotate import LogRotator, is_allowed


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
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(pattern * (size_mb * 1024 * 1024))


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
        self.write_mb(8)
        foreign = os.path.join(self.dir, "wichtige-datei.log")
        with open(foreign, "w", encoding="utf-8") as handle:
            handle.write("y" * 1000)
        LogRotator(max_mb=5, warn_mb=4).check(foreign)
        self.assertGreater(os.path.getsize(foreign), 0)

    def test_path_outside_tmp_is_rejected(self):
        outside = "/var/log/sonstwas.log"
        self.assertFalse(is_allowed(outside))
        self.assertFalse(LogRotator(max_mb=1, warn_mb=1).check(outside).truncated)

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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_logrotate -v`
Expected: `ModuleNotFoundError: No module named 'slotmon.logrotate'`

- [ ] **Step 3: Write minimal implementation**

```python
# slotmon/logrotate.py
"""Raeumt Logdateien ab, ohne den Server anzufassen.

truncate -s 0 statt rm: der Server haelt die Datei offen, ein rm wuerde den
Platz erst beim Neustart freigeben -- den es nicht gibt.
"""

import os
import re
from dataclasses import dataclass

ALLOWED_NAME = re.compile(r"^(escha_server.*\.log|slotmon\.log)$")
TMP_ROOT = os.path.realpath("/tmp")


def is_allowed(path):
    """Nur Logdateien unter /tmp. Der Basename entscheidet, nicht ein Glob."""
    if not ALLOWED_NAME.match(os.path.basename(path)):
        return False
    return os.path.realpath(path).startswith(TMP_ROOT + os.sep)


@dataclass
class RotateResult:
    size_mb: float | None = None
    truncated: bool = False
    warn: bool = False
    error: str | None = None


class LogRotator:
    def __init__(self, max_mb=1024, warn_mb=900, every=60):
        self.max_mb = max_mb
        self.warn_mb = warn_mb
        self.every = every
        self.counter = 0

    def check(self, path, force=False):
        if not force and self.counter != 0:
            self.counter = (self.counter + 1) % max(1, self.every)
            return RotateResult()
        self.counter = (self.counter + 1) % max(1, self.every)

        if not is_allowed(path):
            return RotateResult(error=f"pfad nicht freigegeben: {path}")
        try:
            size = os.path.getsize(path)
        except OSError:
            return RotateResult()

        size_mb = size / (1024 * 1024)
        if size_mb < self.warn_mb:
            return RotateResult(size_mb=size_mb)

        if size_mb >= self.max_mb:
            try:
                os.truncate(path, 0)
            except OSError as exc:
                return RotateResult(size_mb=size_mb, error=f"truncate: {exc}")
            return RotateResult(size_mb=size_mb, truncated=True, warn=False)

        return RotateResult(size_mb=size_mb, warn=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m unittest tests.test_logrotate -v`
Expected: `Ran 9 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add slotmon/logrotate.py tests/test_logrotate.py
git commit -m "feat: Log-Rotation mit truncate statt rm"
```

---

## Task 8: Collector

Setzt alles zusammen. Ab hier kennt der Code die Außenwelt.

**Files:**
- Create: `slotmon/collector.py`
- Test: `tests/test_collector.py`

**Interfaces:**
- Consumes: `Config`, `dict[str, str]` Namen, `float now` je Durchlauf (injizierbar für Tests)
- Produces: `slotmon.collector.Collector(cfg: Config, names: dict | None = None, now_fn=time.time)` mit:
  - `tick() -> dict` — liest alle Quellen und aktualisiert den Zustand, liefert den Zustand
  - `state() -> dict` — der letzte Zustand, ohne I/O
  - `close()` — hört den Log-Reader auf

Der Zustand hat diese Form:

```python
{
  "at": float,
  "server": {"connected": bool, "load_error": str | None, "age_s": float | None},
  "slot": {
      "state": "busy" | "free" | "unknown",
      "rid": str | None,
      "system": str | None,
      "ip_port": str | None,
      "started_at": float | None,
      "duration_s": float | None,
      "legacy": int | None,
      "open_count": int,
      "complete": bool,
  },
  "queue": {"waiting": int | None, "running": int | None},
  "speed": {"tok_per_s": float | None, "ctx_tokens": int | None,
            "max_req_input_len": int | None, "output_pct": float | None},
  "gpu": {"util": int | None, "used_mb": int | None, "total_mb": int | None},
  "log": {"size_mb": float | None, "warn": bool, "truncated": bool},
  "history": [dict, ...],
}
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_collector.py
import os
import shutil
import tempfile
import unittest
from unittest import mock

from slotmon.collector import Collector
from slotmon.config import load_config

RECEIVED = (
    '{"timestamp": "2026-10-07T17:19:07.000000", "event": "request.received", '
    '"rid": "aaa", "obj": {"rid": "aaa", "received_time": 900.0}}'
)
FINISHED = (
    '{"timestamp": "2026-10-07T17:19:24.000000", "event": "request.finished", '
    '"rid": "aaa", "obj": {"rid": "aaa", "received_time": 900.0}, '
    '"out": {"meta_info": {"id": "aaa", "finish_reason": {"type": "stop"}, '
    '"prompt_tokens": 100, "completion_tokens": 50, "cached_tokens": 0}}}'
)
ACCESS = (
    '[2026-10-07 17:19:20] INFO:     192.168.9.2:44444 - '
    '"POST /v1/chat/completions HTTP/1.1" 200 OK'
)


class CollectorCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotmon-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.log = os.path.join(self.dir, "server.log")
        with open(self.log, "w", encoding="utf-8") as handle:
            handle.write("")
        self.now = 1000.0
        self.cfg = load_config({
            "SERVER_LOG": self.log,
            "HISTORY": os.path.join(self.dir, "history.jsonl"),
            "MAX_LOG_MB": "1024",
        })

    def append(self, *lines):
        with open(self.log, "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def make(self, names=None, now_fn=None):
        collector = Collector(self.cfg, names or {"192.168.9.2": "Laptop"},
                              now_fn=now_fn or (lambda: self.now))
        self.addCleanup(collector.close)
        for method in ("read_load", "read_metrics", "read_gpu"):
            setattr(collector.sources, method,
                    lambda **kw: _ok({"num_reqs": 1, "num_waiting_reqs": 0,
                                      "gen_throughput": 49.0, "num_queue_reqs": 0,
                                      "num_running_reqs": 1, "util": 95,
                                      "used_mb": 15424, "total_mb": 16303}))
        collector.sources.read_server_info = lambda **kw: _ok({"max_req_input_len": 83994})
        return collector


def _ok(value):
    from slotmon.sources import Sample
    return Sample(value, 1000.0, None)


class TestEmptyState(CollectorCase):
    def test_fresh_collector_has_no_holder(self):
        state = self.make().tick()
        self.assertIsNone(state["slot"]["rid"])
        self.assertEqual(state["slot"]["open_count"], 0)

    def test_state_shape_is_complete(self):
        state = self.make().tick()
        for key in ("at", "server", "slot", "queue", "speed", "gpu", "log", "history"):
            self.assertIn(key, state)


class TestHolder(CollectorCase):
    def test_open_request_becomes_holder(self):
        collector = self.make()
        self.append(RECEIVED)
        state = collector.tick()
        self.assertEqual(state["slot"]["rid"], "aaa")
        self.assertEqual(state["slot"]["system"], "Laptop")

    def test_running_slot_has_no_ip_yet(self):
        collector = self.make()
        self.append(RECEIVED)
        state = collector.tick()
        self.assertIsNone(state["slot"]["ip_port"])

    def test_duration_is_measured_from_received_time(self):
        # received_time der Fixture ist 900.0, now startet bei 1000.0
        collector = self.make()
        self.append(RECEIVED)
        state = collector.tick()
        self.assertAlmostEqual(state["slot"]["duration_s"], 100.0, delta=1.0)
        self.now = 1030.0
        state = collector.tick()
        self.assertAlmostEqual(state["slot"]["duration_s"], 130.0, delta=1.0)

    def test_legacy_queue_reports_unknown_holder(self):
        collector = self.make()
        collector.sources.read_load = lambda **kw: _ok(
            {"num_reqs": 2302, "num_waiting_reqs": 2301})
        self.append(RECEIVED)
        state = collector.tick()
        self.assertEqual(state["slot"]["legacy"], 2300)
        self.assertIsNone(state["slot"]["rid"])
        self.assertFalse(state["slot"]["complete"])
        self.assertEqual(state["slot"]["state"], "unknown")


class TestCompletion(CollectorCase):
    def test_finished_request_lands_in_history(self):
        collector = self.make()
        self.append(RECEIVED)
        collector.tick()
        self.append(FINISHED, ACCESS)
        state = collector.tick()
        self.assertEqual(len(state["history"]), 1)
        finished = state["history"][0]
        self.assertEqual(finished["rid"], "aaa")
        self.assertEqual(finished["system"], "Laptop")
        self.assertEqual(finished["ip_port"], "192.168.9.2:44444")
        self.assertEqual(finished["completion_tokens"], 50)

    def test_history_survives_a_restart(self):
        first = self.make()
        self.append(RECEIVED)
        first.tick()
        self.append(FINISHED, ACCESS)
        first.tick()
        first.close()

        second = self.make()
        state = second.tick()
        self.assertEqual(len(state["history"]), 1)

    def test_slot_is_free_after_completion(self):
        collector = self.make()
        self.append(RECEIVED)
        collector.tick()
        self.append(FINISHED, ACCESS)
        state = collector.tick()
        self.assertIsNone(state["slot"]["rid"])
        self.assertEqual(state["slot"]["state"], "free")


class TestDegradedServer(CollectorCase):
    def test_load_failure_keeps_running_and_marks_disconnected(self):
        from slotmon.sources import Sample
        collector = self.make()
        collector.sources.read_load = lambda **kw: Sample(None, None, "ConnectError: down")
        state = collector.tick()
        self.assertFalse(state["server"]["connected"])
        self.assertEqual(state["server"]["load_error"], "ConnectError: down")
        self.assertIsNone(state["queue"]["waiting"])

    def test_unknown_waiting_hides_legacy(self):
        from slotmon.sources import Sample
        collector = self.make()
        collector.sources.read_load = lambda **kw: Sample(
            {"num_reqs": None, "num_waiting_reqs": None}, 1000.0, None)
        state = collector.tick()
        self.assertIsNone(state["slot"]["legacy"])


class TestGuard(CollectorCase):
    def test_no_socket_command_in_source(self):
        import slotmon.collector as module
        import slotmon.logreader as reader
        for source in (open(module.__file__, encoding="utf-8").read(),
                       open(reader.__file__, encoding="utf-8").read()):
            self.assertNotIn("ss -t", source)
            self.assertNotIn("/proc/net/tcp", source)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_collector -v`
Expected: `ModuleNotFoundError: No module named 'slotmon.collector'`

- [ ] **Step 3: Write minimal implementation**

```python
# slotmon/collector.py
"""Fuehrt alle Einheiten zusammen und haelt den Gesamtzustand."""

import os
import re
import time

from slotmon.config import load_config
from slotmon.history import History
from slotmon.logreader import RECEIVED, LogReader
from slotmon.logrotate import LogRotator
from slotmon.names import resolve
from slotmon.slot import evaluate
from slotmon.sources import ServerSources

def host_of(ip_port):
    """Aus "192.168.9.2:44444" die IP machen, damit systems.json greift.

    Kein Port im Namen: die Zuordnung ist IP -> Name.
    """
    if not ip_port:
        return None
    if ip_port.startswith("["):
        return ip_port[1:ip_port.find("]")] if "]" in ip_port else ip_port
    return ip_port.rsplit(":", 1)[0] if ":" in ip_port else ip_port


BATCH_TOKENS = re.compile(r"#full token:\s*(\d+)")
BATCH_QUEUE = re.compile(r"#queue-req:\s*(\d+)")
BATCH_THROUGHPUT = re.compile(r"gen throughput \(token/s\):\s*([\d.]+)")


class Collector:
    def __init__(self, cfg=None, names=None, now_fn=time.time):
        self.cfg = cfg or load_config()
        self.names = names if names is not None else {}
        self.now_fn = now_fn
        self.sources = ServerSources(self.cfg)
        self.reader = LogReader(self.cfg.server_log, self.cfg.log_pair_window_s)
        self.history = History(self.cfg.history, self.cfg.retention_h)
        self.rotator = LogRotator(self.cfg.max_log_mb, self.cfg.warn_log_mb, every=60)
        self.finished = {}
        self.current = None
        self.max_req_input_len = None
        self._state = self._empty_state()

    def close(self):
        return None

    def _empty_state(self):
        now = self.now_fn()
        return {
            "at": now,
            "server": {"connected": False, "load_error": None, "age_s": None},
            "slot": {
                "state": "unknown", "rid": None, "system": None, "ip_port": None,
                "started_at": None, "duration_s": None, "legacy": None,
                "open_count": 0, "complete": False,
            },
            "queue": {"waiting": None, "running": None},
            "speed": {"tok_per_s": None, "ctx_tokens": None,
                      "max_req_input_len": self.max_req_input_len,
                      "output_pct": None},
            "gpu": {"util": None, "used_mb": None, "total_mb": None},
            "log": {"size_mb": None, "warn": False, "truncated": False},
            "history": [],
        }

    def _read_batch(self):
        """Die letzten Batch-Zeilen aus dem Log lesen."""
        try:
            size = os.path.getsize(self.cfg.server_log)
            window = min(size, 8192)
            with open(self.cfg.server_log, "rb") as handle:
                handle.seek(size - window)
                tail = handle.read(window).decode("utf-8", errors="replace")
        except OSError:
            return None, None, None
        tail = tail.replace("\r", "\n")
        ctx = None
        for match in BATCH_TOKENS.finditer(tail):
            ctx = int(match.group(1))
        throughput = None
        for match in BATCH_THROUGHPUT.finditer(tail):
            throughput = float(match.group(1))
        queued = None
        for match in BATCH_QUEUE.finditer(tail):
            queued = int(match.group(1))
        return ctx, throughput, queued

    def _drain_events(self):
        for event in self.reader.poll():
            if event.kind == RECEIVED:
                continue
            meta = event.meta
            self.finished[event.rid] = {
                "rid": event.rid,
                "ip_port": meta.get("ip_port"),
                "ended_at": event.at,
                "started_at": meta.get("received_time"),
                "prompt_tokens": meta.get("prompt_tokens"),
                "completion_tokens": meta.get("completion_tokens"),
                "cached_tokens": meta.get("cached_tokens"),
                "finish_reason": meta.get("finish_reason"),
            }

    def _flush_finished(self, open_rids, now):
        written = []
        for rid, entry in list(self.finished.items()):
            if rid in open_rids:
                continue
            started = entry.get("started_at")
            duration = None
            if isinstance(started, (int, float)) and started <= now:
                duration = max(0.0, now - started)
            record = dict(entry)
            record["duration_s"] = duration
            record["system"] = resolve(self.names, host_of(entry.get("ip_port")))
            self.history.append(record)
            written.append(record)
            del self.finished[rid]
        return written

    def tick(self):
        now = self.now_fn()
        load = self.sources.read_load()
        metrics = self.sources.read_metrics()
        gpu = self.sources.read_gpu()
        info = self.sources.read_server_info()
        if info.fresh and info.value.get("max_req_input_len"):
            self.max_req_input_len = info.value["max_req_input_len"]

        self._drain_events()
        open_rids = dict(self.reader.state["open"])
        self._flush_finished(open_rids, now)

        waiting = None
        running = None
        connected = False
        load_error = None
        age = None
        if load.fresh:
            connected = True
            waiting = load.value.get("num_waiting_reqs")
            age = now - load.at
        else:
            load_error = load.error
            if metrics.fresh:
                waiting = metrics.value.get("num_queue_reqs")
        if metrics.fresh:
            running = metrics.value.get("num_running_reqs")

        slot_state = evaluate(open_rids, waiting)

        system = None
        ip_port = None
        started_at = None
        duration = None
        if slot_state.holder_rid:
            started_at = slot_state.holder_since
            duration = max(0.0, now - started_at) if started_at else None
            entry = self.finished.get(slot_state.holder_rid)
            if entry:
                ip_port = entry.get("ip_port")
                system = resolve(self.names, host_of(ip_port))

        if slot_state.complete and slot_state.holder_rid:
            label = "busy"
        elif slot_state.complete and not open_rids and waiting == 0:
            label = "free"
        else:
            label = "unknown"

        ctx, throughput, _queued = self._read_batch()
        tok_per_s = metrics.value.get("gen_throughput") if metrics.fresh else None
        if tok_per_s is None:
            tok_per_s = throughput
        output_pct = None
        if ctx is not None and self.max_req_input_len:
            output_pct = round(100.0 * ctx / self.max_req_input_len, 2)

        rotate = self.rotator.check(self.cfg.server_log)
        if rotate.truncated:
            self.reader.reset()

        state = self._empty_state()
        state["at"] = now
        state["server"] = {
            "connected": connected, "load_error": load_error, "age_s": age
        }
        state["slot"] = {
            "state": label,
            "rid": slot_state.holder_rid,
            "system": system,
            "ip_port": ip_port,
            "started_at": started_at,
            "duration_s": duration,
            "legacy": slot_state.legacy,
            "open_count": slot_state.open_count,
            "complete": slot_state.complete,
        }
        state["queue"] = {"waiting": waiting, "running": running}
        state["speed"] = {
            "tok_per_s": tok_per_s,
            "ctx_tokens": ctx,
            "max_req_input_len": self.max_req_input_len,
            "output_pct": output_pct,
        }
        if gpu.fresh:
            state["gpu"] = {
                "util": gpu.value.get("util"),
                "used_mb": gpu.value.get("used_mb"),
                "total_mb": gpu.value.get("total_mb"),
            }
        state["log"] = {
            "size_mb": rotate.size_mb, "warn": rotate.warn, "truncated": rotate.truncated
        }
        state["history"] = self.history.load(now)

        self._state = state
        return state

    def state(self):
        return self._state
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m unittest tests.test_collector -v`
Expected: `Ran 13 tests ... OK`

- [ ] **Step 5: Verify against the real server, read-only**

Run:
```bash
python3.12 -c "
import json, time
from slotmon.collector import Collector
c = Collector()
for i in range(3):
    state = c.tick()
    time.sleep(2)
print(json.dumps(state, indent=2, ensure_ascii=False)[:1200])
"
```
Expected: `slot.state` ist `unknown` mit `legacy` in dreistelliger Größe, `queue.waiting` zeigt eine reale Zahl, `speed.tok_per_s` ist ungleich `None`. **Wenn `slot.rid` gesetzt ist, prüfen ob `legacy` wirklich 0 ist** — sonst stimmt die Rechnung nicht.

- [ ] **Step 6: Commit**

```bash
git add slotmon/collector.py tests/test_collector.py
git commit -m "feat: Collector verbindet Quellen, Log, Historie und Rotation"
```

---

## Task 9: Dashboard-Server

**Files:**
- Create: `slotmon/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `Collector`
- Produces: FastAPI-App aus `slotmon.server.create_app(collector: Collector) -> FastAPI`
- Endpoints: `GET /api/state` → JSON-Zustand, `GET /api/stream` → SSE mit einem `data:`-Ereignis je Tick, `GET /` → HTML aus `dashboard.html`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server.py
import json
import unittest

from fastapi.testclient import TestClient

from slotmon.collector import Collector
from slotmon.config import load_config
from slotmon.server import create_app


class TestEndpoints(unittest.TestCase):
    def setUp(self):
        self.collector = Collector(load_config({}))
        self.addCleanup(self.collector.close)
        app = create_app(self.collector)
        self.client = TestClient(app)

    def test_state_returns_the_collector_state(self):
        self.collector.tick()
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("slot", payload)
        self.assertIn("queue", payload)

    def test_missing_values_stay_null_not_zero(self):
        """Ohne Serverantwort bleibt alles null, nirgends steht eine 0."""
        self.collector.tick()
        payload = self.client.get("/api/state").json()
        self.assertIsNone(payload["queue"]["waiting"])
        self.assertIsNone(payload["queue"]["running"])
        self.assertIsNone(payload["speed"]["tok_per_s"])
        self.assertIsNone(payload["speed"]["output_pct"])
        self.assertIsNone(payload["gpu"]["util"])
        self.assertIsNone(payload["slot"]["rid"])

    def test_root_serves_html(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("slotmon", response.text.lower())

    def test_stream_route_is_registered(self):
        """Der SSE-Pfad wird am Router geprueft, nicht ueber einen offenen Stream.

        TestClient.stream auf einen endlosen Generator blockiert beim Verlassen des
        Kontextmanagers. Die media_type-Pruefung passiert deshalb live in Step 5,
        wo der Server als curl erreichbar ist.
        """
        routes = [r for r in create_app(self.collector).routes
                  if getattr(r, "path", "") == "/api/stream"]
        self.assertEqual(len(routes), 1)

    def test_health_endpoint(self):
        self.assertEqual(self.client.get("/health").json(), {"ok": True})


class TestHtml(unittest.TestCase):
    def test_dashboard_file_exists_and_has_both_tabs(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "slotmon", "dashboard.html")
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('data-tab="now"', html)
        self.assertIn('data-tab="history"', html)

    def test_dashboard_has_the_altbestand_banner(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "slotmon", "dashboard.html")
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("altbestand", html.lower())

    def test_dashboard_has_no_prompt_field(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "slotmon", "dashboard.html")
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        self.assertNotIn("prompt_text", html)
        self.assertNotIn("promptText", html)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_server -v`
Expected: `ModuleNotFoundError: No module named 'slotmon.server'`

- [ ] **Step 3: Write minimal implementation**

```python
# slotmon/server.py
"""FastAPI-App: Zustand als JSON und als SSE-Stream, dazu die HTML-Seite."""

import asyncio
import json
import os

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, StreamingResponse

HERE = os.path.dirname(os.path.abspath(__file__))


def create_app(collector):
    app = FastAPI(title="slotmon", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    def state():
        return collector.state()

    @app.get("/api/stream")
    async def stream():
        async def events():
            while True:
                try:
                    payload = await asyncio.to_thread(collector.tick)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except Exception as exc:  # der Monitor darf daran nicht sterben
                    error = {"error": f"{type(exc).__name__}: {exc}"}
                    yield f"data: {json.dumps(error)}\n\n"
                await asyncio.sleep(collector.cfg.poll_interval)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as handle:
            return Response(handle.read(), media_type="text/html; charset=utf-8")

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def main():
    import uvicorn

    from slotmon.collector import Collector
    from slotmon.config import load_config
    from slotmon.names import load_names

    cfg = load_config()
    names = load_names(cfg.systems_file)
    app = create_app(Collector(cfg, names))
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
```

```html
<!-- slotmon/dashboard.html -->
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPU-Slot-Monitor</title>
<style>
  :root {
    --bg: #10131a; --card: #191d27; --line: #262b38;
    --text: #e6e9f0; --muted: #8b93a7;
    --ok: #3ecf8e; --warn: #f0a742; --idle: #4a5163; --busy: #2b6b4f;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; padding: 20px; }
  h1 { font-size: 18px; margin: 0 0 16px; font-weight: 600; }
  .tabs { display: flex; gap: 4px; margin-bottom: 18px; border-bottom: 1px solid var(--line); }
  .tab { padding: 8px 16px; cursor: pointer; color: var(--muted);
         border: 1px solid transparent; border-bottom: none; border-radius: 6px 6px 0 0; }
  .tab.active { color: var(--text); background: var(--card); border-color: var(--line); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .kpi, .card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
  .kpi .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
  .kpi .value { font-size: 24px; font-weight: 600; margin-top: 4px;
                font-variant-numeric: tabular-nums; }
  .kpi .value.unknown { color: var(--idle); font-size: 18px; }
  .card { margin-top: 14px; }
  .slot.busy { border-color: var(--ok); box-shadow: inset 0 0 0 1px var(--busy); }
  .badge { display: inline-block; padding: 3px 9px; border-radius: 4px; font-size: 11px;
           font-weight: 700; letter-spacing: .05em; }
  .badge.busy { background: var(--ok); color: #06231a; }
  .badge.free { background: var(--idle); color: var(--text); }
  .badge.unknown { background: var(--warn); color: #2a1a02; }
  .rid { font-family: ui-monospace, monospace; color: var(--muted); font-size: 12px; }
  .banner { background: rgba(240,167,66,.12); border: 1px solid var(--warn);
            color: var(--warn); border-radius: 6px; padding: 10px 14px; margin-bottom: 14px; }
  .bar { height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; margin-top: 8px; }
  .bar > div { height: 100%; background: var(--ok); transition: width .4s; }
  .note { color: var(--muted); font-size: 12px; margin-top: 8px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.now td { color: var(--ok); }
  .muted { color: var(--muted); }
  .hidden { display: none; }
</style>
</head>
<body>
<h1>GPU-Slot-Monitor</h1>

<div class="tabs">
  <div class="tab active" data-tab="now">Jetzt</div>
  <div class="tab" data-tab="history">Verlauf</div>
</div>

<section id="tab-now">
  <div id="altbestand" class="banner hidden"></div>
  <div class="grid">
    <div class="kpi"><div class="label">Slot</div><div class="value" id="k-slot">–</div></div>
    <div class="kpi"><div class="label">Wartend</div><div class="value" id="k-wait">–</div></div>
    <div class="kpi"><div class="label">Tok/s</div><div class="value" id="k-tps">–</div></div>
    <div class="kpi"><div class="label">GPU</div><div class="value" id="k-gpu">–</div></div>
    <div class="kpi"><div class="label">VRAM</div><div class="value" id="k-vram">–</div></div>
    <div class="kpi"><div class="label">Wartezeit</div><div class="value" id="k-wait-time">–</div></div>
  </div>

  <div class="card slot" id="slot-card">
    <div><span class="badge unknown" id="slot-badge">–</span>
      <strong id="slot-system">–</strong></div>
    <div class="rid" id="slot-ip"></div>
    <div class="rid" id="slot-rid"></div>
    <div class="grid" style="margin-top:12px">
      <div class="kpi"><div class="label">Läuft</div><div class="value" id="s-dur">–</div></div>
      <div class="kpi"><div class="label">Tok/s</div><div class="value" id="s-tps">–</div></div>
      <div class="kpi"><div class="label">Ctx</div><div class="value" id="s-ctx">–</div></div>
      <div class="kpi"><div class="label">Ausgaben</div><div class="value" id="s-out">–</div></div>
    </div>
    <div class="bar"><div id="s-bar" style="width:0%"></div></div>
    <div class="note" id="slot-note"></div>
  </div>

  <div class="card">
    <table>
      <thead><tr><th>System</th><th class="num">Dauer</th><th class="num">Prompt</th>
        <th class="num">Antwort</th><th class="num">Cache</th><th>Ende</th></tr></thead>
      <tbody id="history-rows"></tbody>
    </table>
    <div class="note">Einzelne Wartende lassen sich keinem System zuordnen — die
      Warteschlange des Servers ist länger als die Zahl offener Verbindungen. Angezeigt
      wird deshalb nur die Zahl.</div>
  </div>
</section>

<section id="tab-history" class="hidden">
  <div class="card">
    <div class="grid">
      <div class="kpi"><div class="label">Übergaben</div><div class="value" id="h-count">–</div></div>
      <div class="kpi"><div class="label">Ø Dauer</div><div class="value" id="h-avg">–</div></div>
      <div class="kpi"><div class="label">Queue</div><div class="value" id="h-queue">–</div></div>
      <div class="kpi"><div class="label">Tok/s jetzt</div><div class="value" id="h-tps">–</div></div>
    </div>
  </div>
  <div class="card">
    <div class="label muted">Durchsatz letzte 60 Sekunden</div>
    <div id="spark" class="bar" style="height:40px"></div>
  </div>
  <div class="card">
    <div class="label muted">Zeitstrahl</div>
    <div id="timeline"></div>
  </div>
</section>

<script>
const sparkline = [];
const fmt = (v, suffix = "") =>
  (v === null || v === undefined) ? '<span class="unknown">–</span>' : v + suffix;

function duration(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const s = Math.floor(seconds);
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h > 0 ? `${h}h ${m % 60}m` : m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

function averageWait(entries) {
  const done = entries.filter(e => typeof e.duration_s === "number");
  if (!done.length) return null;
  const avg = done.reduce((sum, e) => sum + e.duration_s, 0) / done.length;
  const queue = lastState && lastState.queue ? (lastState.queue.waiting || 0) : 0;
  return avg * queue;
}

let lastState = null;

function render(data) {
  lastState = data;
  const slot = data.slot, queue = data.queue, speed = data.speed, gpu = data.gpu;

  // Altbestand-Banner
  const banner = document.getElementById("altbestand");
  if (slot.legacy !== null && slot.legacy > 0) {
    banner.textContent =
      `Altbestand: ${slot.legacy} Requests vor Aktivierung der Log-Events — ` +
      `Halter unbekannt, bis die Queue auf 0 ist.`;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }

  // KPI
  const labels = { busy: "BELEGT", free: "FREI", unknown: "UNBEKANNT" };
  document.getElementById("k-slot").innerHTML = `<span class="unknown">${labels[slot.state]}</span>`;
  document.getElementById("k-wait").innerHTML = fmt(queue.waiting);
  document.getElementById("k-tps").innerHTML = fmt(
    speed.tok_per_s === null ? null : speed.tok_per_s.toFixed(1));
  document.getElementById("k-gpu").innerHTML = gpu.util === null ? null : gpu.util + " %";
  document.getElementById("k-vram").innerHTML = gpu.used_mb === null ? null
    : (gpu.used_mb / 1024).toFixed(1) + " GB";
  const avgWait = averageWait(data.history);
  document.getElementById("k-wait-time").innerHTML = avgWait === null ? null
    : duration(avgWait) + " (Schätzung)";

  // Slot-Karte
  const card = document.getElementById("slot-card");
  card.className = "card slot" + (slot.state === "busy" ? " busy" : "");
  const badge = document.getElementById("slot-badge");
  badge.className = "badge " + slot.state;
  badge.textContent = slot.state === "busy" ? "HAT DEN SLOT"
    : slot.state === "free" ? "FREI" : "UNBEKANNT";
  document.getElementById("slot-system").textContent = slot.system || "unbekannt";
  document.getElementById("slot-ip").textContent = slot.ip_port
    ? slot.ip_port : (slot.state === "busy" ? "IP noch nicht abgeschlossen" : "");
  document.getElementById("slot-rid").textContent = slot.rid ? "rid " + slot.rid : "";
  document.getElementById("s-dur").textContent = duration(slot.duration_s) || "–";
  document.getElementById("s-tps").textContent = speed.tok_per_s === null ? "–"
    : speed.tok_per_s.toFixed(1);
  document.getElementById("s-ctx").textContent = speed.ctx_tokens === null ? "–"
    : speed.ctx_tokens.toLocaleString("de-DE");
  document.getElementById("s-out").textContent = speed.output_pct === null ? "–"
    : speed.output_pct.toFixed(2) + " %";
  document.getElementById("s-bar").style.width =
    Math.min(100, speed.output_pct || 0) + "%";
  document.getElementById("slot-note").textContent = slot.complete ? ""
    : "Der Halter ist erst bestimmbar, wenn kein Altbestand mehr in der Queue liegt.";

  // Verlaufstabelle
  const rows = data.history.slice().reverse().map(entry => `
    <tr>
      <td>${entry.system || entry.ip_port || "unbekannt"}</td>
      <td class="num">${duration(entry.duration_s) || "–"}</td>
      <td class="num">${fmt(entry.prompt_tokens)}</td>
      <td class="num">${fmt(entry.completion_tokens)}</td>
      <td class="num">${fmt(entry.cached_tokens)}</td>
      <td class="muted">${new Date(entry.ended_at * 1000).toLocaleTimeString("de-DE")}</td>
    </tr>`).join("");
  document.getElementById("history-rows").innerHTML = rows ||
    '<tr><td colspan="6" class="muted">Noch keine abgeschlossenen Slots.</td></tr>';

  // Summen
  document.getElementById("h-count").textContent = data.history.length;
  const avg = averageWait(data.history);
  const done = data.history.filter(e => typeof e.duration_s === "number");
  const avgDur = done.length
    ? done.reduce((sum, e) => sum + e.duration_s, 0) / done.length : null;
  document.getElementById("h-avg").textContent = duration(avgDur) || "–";
  document.getElementById("h-queue").textContent = queue.waiting === null ? "–" : queue.waiting;
  document.getElementById("h-tps").textContent = speed.tok_per_s === null ? "–"
    : speed.tok_per_s.toFixed(1);

  // Sparkline
  if (speed.tok_per_s !== null) sparkline.push(speed.tok_per_s);
  if (sparkline.length > 60) sparkline.shift();
  const peak = Math.max(1, ...sparkline);
  document.getElementById("spark").innerHTML = sparkline
    .map(v => `<div style="display:inline-block;width:3px;height:${v / peak * 100}%;
                background:var(--ok);vertical-align:bottom;margin-right:1px"></div>`).join("");

  // Zeitstrahl
  document.getElementById("timeline").innerHTML = data.history.slice().reverse().map(entry => `
    <div style="border-left:3px solid var(--idle);padding:6px 0 6px 10px;margin-bottom:6px">
      <strong>${entry.system || "unbekannt"}</strong>
      <span class="muted"> · ${duration(entry.duration_s) || "–"}
      · ${fmt(entry.completion_tokens)} tok
      · ${new Date(entry.ended_at * 1000).toLocaleTimeString("de-DE")}</span>
      ${entry.system === null && entry.ip_port === null
        ? '<span class="muted"> · Name nachträglich</span>' : ""}
    </div>`).join("");
}

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll("section").forEach(s => s.classList.add("hidden"));
    tab.classList.add("active");
    document.getElementById("tab-" + tab.dataset.tab).classList.remove("hidden");
    sessionStorage.setItem("slotmon.tab", tab.dataset.tab);
  });
});

const saved = sessionStorage.getItem("slotmon.tab");
if (saved && saved !== "now") document.querySelector(`[data-tab="${saved}"]`).click();

fetch("/api/state").then(r => r.json()).then(render);

const source = new EventSource("/api/stream");
source.onmessage = event => render(JSON.parse(event.data));
</script>
</body>
</html>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m unittest tests.test_server -v`
Expected: `Ran 6 tests ... OK`. Fehlt `fastapi.testclient`, dann `python3.12 -c "import httpx; print(httpx.__version__)"` prüfen — httpx 0.28 ist mit TestClient kompatibel, sonst auf `fastapi.testclient.TestClient` verzichten und stattdessen gegen den gestarteten Server testen.

- [ ] **Step 5: Start the server and check it by hand**

Run:
```bash
python3.12 -m slotmon.server &
sleep 4
curl -s http://127.0.0.1:8090/api/state | python3.12 -m json.tool | head -20
curl -s -o /dev/null -w "dashboard: %{http_code}\n" http://127.0.0.1:8090/
curl -s -N --max-time 3 http://127.0.0.1:8090/api/stream | head -2
```
Expected: JSON mit `slot.state` und `slot.legacy`, Dashboard HTTP 200, und der
Stream liefert eine Zeile beginnend mit `data: {`. Bricht curl nach `--max-time` ab,
ist das erwartet — der Stream ist endlos.

Danach den Testserver wieder beenden:
```bash
pkill -f "[s]lotmon.server" || true
```

- [ ] **Step 6: Commit**

```bash
git add slotmon/server.py slotmon/dashboard.html tests/test_server.py
git commit -m "feat: FastAPI-Server mit SSE und Zwei-Reiter-Dashboard"
```

---

## Task 10: Lebenszyklus-Skript

**Files:**
- Create: `monitor.sh`
- Test: `tests/test_monitor_sh.sh`

**Interfaces:**
- Consumes: `Config`-Variablen aus der Umgebung
- Produces: `monitor.sh start|stop|status|restart`, Exit 0 bei Erfolg, 1 bei Fehler
- Nutzt: `slotmon.server.main`, PID-Datei `/tmp/slotmon.pid`, Monitor-Log `/tmp/slotmon.log`

Muster aus `runtime/sglang/start-penalty.sh`: `set -euo pipefail`, `pgrep -f "[s]lotmon"`, `nohup`, `kill -TERM` mit 20 s Grace.

- [ ] **Step 1: Write the failing test**

```bash
# tests/test_monitor_sh.sh
#!/usr/bin/env bash
# Prueft monitor.sh gegen sich selbst, ohne den echten Server anzufassen.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR="$REPO/monitor.sh"
FAILURES=0

pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

# Eigenes Log und eigene PID-Datei, damit der Test den Monitor nicht stoert
export MONITOR_LOG="/tmp/slotmon-selftest.log"
export PIDFILE="/tmp/slotmon-selftest.pid"

test -f "$MONITOR" && pass "monitor.sh existiert" || { fail "monitor.sh fehlt"; exit 1; }
bash -n "$MONITOR" && pass "Syntax ist gueltig" || fail "Syntaxfehler"

grep -q 'set -euo pipefail' "$MONITOR" && pass "set -euo pipefail gesetzt" || fail "set fehlt"
grep -q '\[s\]lotmon' "$MONITOR" && pass "pgrep-Muster mit [s]lotmon" || fail "pgrep-Muster fehlt"
grep -q 'nohup' "$MONITOR" && pass "nohup verwendet" || fail "nohup fehlt"

# start
"$MONITOR" start >/dev/null 2>&1
if [ $? -eq 0 ]; then pass "start liefert 0"; else fail "start fehlgeschlagen"; fi

sleep 3
curl -s -o /dev/null http://127.0.0.1:8090/health && pass "health erreichbar" \
  || fail "health nicht erreichbar"

# Doppelstart muss ablehnen
OUTPUT=$("$MONITOR" start 2>&1)
if [ $? -eq 0 ]; then pass "Doppelstart liefert 0"; else fail "Doppelstart sollte 0 liefern"; fi
case "$OUTPUT" in
  *"laeuft bereits"*) pass "Doppelstart meldet 'laeuft bereits'" ;;
  *) fail "Doppelstart meldet nicht 'laeuft bereits': $OUTPUT" ;;
esac

# status
"$MONITOR" status >/dev/null 2>&1 && pass "status liefert 0" || fail "status fehlgeschlagen"

# Der sglang-Server darf unberuehrt sein
if pgrep -f "[s]glang.launch_server" >/dev/null; then
  pass "sglang-Server laeuft noch"
else
  echo "  info: kein sglang-Server aktiv (Test laeuft auf anderem Rechner)"
fi

# stop
"$MONITOR" stop >/dev/null 2>&1 && pass "stop liefert 0" || fail "stop fehlgeschlagen"
sleep 2
if pgrep -f "[s]lotmon.server" >/dev/null; then
  fail "Prozess laeuft nach stop noch"
  pkill -f "[s]lotmon.server"
else
  pass "Prozess beendet"
fi

"$MONITOR" stop >/dev/null 2>&1 && pass "stop zweimal liefert 0" || fail "stop zweimal sollte 0 liefern"

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "Alle Skript-Tests bestanden"
else
  echo "$FAILURES Skript-Test(s) fehlgeschlagen"
  exit 1
fi
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_monitor_sh.sh`
Expected: `FAIL: monitor.sh fehlt`

- [ ] **Step 3: Write minimal implementation**

```bash
#!/usr/bin/env bash
# Lebenszyklus des Slot-Monitors. Startet, stoppt, fragt ab.
# Die Zustandslogik liegt in slotmon/, hier steht nur Prozessverwaltung.
# Muster folgt runtime/sglang/start-penalty.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3.12}"
PORT="${PORT:-8090}"
API="${API:-http://127.0.0.1:30000}"
MONITOR_LOG="${MONITOR_LOG:-/tmp/slotmon.log}"
PIDFILE="${PIDFILE:-/tmp/slotmon.pid}"
PATTERN='[s]lotmon.server'

usage() {
  echo "usage: $0 {start|stop|status|restart}"
  exit 2
}

is_running() {
  pgrep -f "$PATTERN" >/dev/null 2>&1
}

start() {
  if is_running; then
    echo "[slotmon] laeuft bereits"
    return 0
  fi
  cd "$REPO"
  mkdir -p "$(dirname "$MONITOR_LOG")" "$(dirname "$PIDFILE")"

  nohup env PORT="$PORT" API="$API" PYTHONUNBUFFERED=1 \
    "$PYTHON" -m slotmon.server >>"$MONITOR_LOG" 2>&1 &
  echo $! >"$PIDFILE"

  local waited=0
  while [ "$waited" -lt 20 ]; do
    if curl -sf -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null; then
      echo "[slotmon] laeuft auf http://0.0.0.0:$PORT (Log: $MONITOR_LOG)"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done

  echo "[slotmon] ERROR: nach 20s kein Health-Antwort" >&2
  echo "[slotmon] letzte Zeilen aus $MONITOR_LOG:" >&2
  tail -n 20 "$MONITOR_LOG" >&2 || true
  return 1
}

stop() {
  if ! is_running; then
    echo "[slotmon] laeuft nicht"
    rm -f "$PIDFILE"
    return 0
  fi

  pkill -TERM -f "$PATTERN" || true
  local waited=0
  while [ "$waited" -lt 20 ] && is_running; do
    sleep 1
    waited=$((waited + 1))
  done

  if is_running; then
    echo "[slotmon] nach 20s noch aktiv, sende KILL" >&2
    pkill -KILL -f "$PATTERN" || true
    sleep 1
  fi

  rm -f "$PIDFILE"
  echo "[slotmon] gestoppt"
}

status() {
  if is_running; then
    echo "[slotmon] laeuft (PID $(pgrep -f "$PATTERN" | head -1), Port $PORT)"
    curl -s "http://127.0.0.1:$PORT/api/state" 2>/dev/null \
      | "$PYTHON" -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print('[slotmon] Server antwortet nicht mit JSON')
    raise SystemExit(0)
slot = data.get('slot', {})
queue = data.get('queue', {})
print(f'[slotmon] Slot={slot.get(\"state\")} '
      f'rid={(slot.get(\"rid\") or \"-\")[:8]} '
      f'system={slot.get(\"system\") or \"unbekannt\"} '
      f'wartend={queue.get(\"waiting\")} '
      f'altbestand={slot.get(\"legacy\")}')
" || true
  else
    echo "[slotmon] laeuft nicht"
    return 1
  fi
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  status)  status ;;
  restart) stop; start ;;
  *)       usage ;;
esac
```

- [ ] **Step 4: Run test to verify it passes**

Run: `chmod +x monitor.sh && bash tests/test_monitor_sh.sh`
Expected: `Alle Skript-Tests bestanden`

- [ ] **Step 5: Confirm the real server is untouched**

Run:
```bash
ls -l /proc/145265/exe 2>/dev/null && ps -o pid,lstart,cmd -p 145265 | tail -1
```
Expected: PID 145265 läuft noch mit unveränderter Startzeit. Falls sich die Startzeit geändert hat, wurde der Server neu gestartet — das wäre ein Verstoß gegen die harte Randbedingung und muss sofort untersucht werden.

- [ ] **Step 6: Commit**

```bash
git add monitor.sh tests/test_monitor_sh.sh
git commit -m "feat: monitor.sh mit start/stop/status nach start-penalty-Muster"
```

---

## Task 11: Gesamttests und Dokumentation

Der letzte Task zieht alles zusammen: alle Tests, ein Test der die widerlegte Quelle fernhält, und die Doku.

**Files:**
- Modify: `README.de.md`
- Modify: `README.md`
- Create: `tests/test_no_socket_source.py`
- Test: alle bisherigen

**Interfaces:**
- Consumes: alle bisherigen Module
- Produces: nichts Neues, nur Absicherung

- [ ] **Step 1: Write the failing test**

```python
# tests/test_no_socket_source.py
"""Schuetzt vor der Rueckkehr der widerlegten Socket-Methode.

Die erste Fassung der Spec erkannte den Halter ueber `ss` und bytes_sent.
Diese Methode ist widerlegt. Dieser Test verhindert, dass sie zurueckkommt.
"""

import glob
import os
import unittest

FORBIDDEN = ("ss -t", "/proc/net/tcp", "bytes_sent", "/proc/net/tcp6")


class TestNoSocketSource(unittest.TestCase):
    def _sources(self):
        root = os.path.join(os.path.dirname(__file__), "..", "slotmon")
        for path in sorted(glob.glob(os.path.join(root, "*.py"))):
            with open(path, encoding="utf-8") as handle:
                yield path, handle.read()

    def test_no_module_reads_sockets(self):
        found = []
        for path, source in self._sources():
            for needle in FORBIDDEN:
                if needle in source:
                    found.append(f"{os.path.basename(path)}: {needle}")
        self.assertEqual(found, [], f"Socket-Zugriff gefunden: {found}")

    def test_monitor_script_is_clean(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "monitor.sh"), encoding="utf-8") as handle:
            script = handle.read()
        for needle in FORBIDDEN:
            self.assertNotIn(needle, script)

    def test_pgrep_pattern_does_not_match_the_server(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "monitor.sh"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("[s]lotmon.server", script)
        self.assertNotIn("sglang", script.replace(
            "sglang.launch_server", "").replace("sglang-Server", ""))


class TestSpecIsCurrent(unittest.TestCase):
    def test_spec_records_the_correction(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        path = os.path.join(
            root, "docs", "superpowers", "specs",
            "2026-10-07-gpu-slot-monitor-design.md")
        with open(path, encoding="utf-8") as handle:
            spec = handle.read()
        self.assertIn("widerlegt", spec)
        self.assertIn("request.received", spec)
        self.assertIn("Altbestand", spec)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m unittest tests.test_no_socket_source -v`
Expected: FAIL, wenn irgendein Modul noch `ss` oder `bytes_sent` enthält. Läuft es durch, ist die Absicherung schon erfüllt.

- [ ] **Step 3: Run the whole suite**

Run: `python3.12 -m unittest discover -s tests -v`
Expected: alle Tests `OK`. Zähle sie — es sollten mindestens 70 sein.

Falls `tests.test_config` nicht gefunden wird, liegt es daran, dass `tests/` kein `__init__.py` hat. Dann `touch tests/__init__.py` anlegen und erneut laufen lassen. Fehlt immer noch etwas, mit `python3.12 -m unittest discover -s tests -t . -v` vom Repo-Wurzelverzeichnis aus probieren.

- [ ] **Step 4: Fix what the suite finds**

Typische Fälle und ihre Korrektur:

| Fehlschlag | Ursache | Korrektur |
|---|---|---|
| `ImportError: No module named tests.test_config` | `tests/` ohne `__init__.py` | `touch tests/__init__.py` |
| `ModuleNotFoundError: fastapi.testclient` | httpx-Version passt nicht | Test auf `curl`-Gegenprobe umstellen, Test streichen |
| `test_poll_reads_only_new_bytes` schlägt fehl | `partial`-Puffer fehlt | `_read_bytes` und `poll` entsprechend Task 3 prüfen |
| `test_duration_grows_with_time` schlägt fehl | `now_fn` wird nicht benutzt | `tick()` muss `now = self.now_fn()` nutzen |

- [ ] **Step 5: Write the documentation**

In `README.de.md` einen neuen Abschnitt nach dem Abschnitt über `ctxmon.sh` einfügen. Wortlaut:

````markdown
### GPU-Slot-Monitor

Zeigt auf `http://0.0.0.0:8090`, welcher Client gerade den einen GPU-Slot des
sglang-Servers hält, wie lange schon, mit welcher Geschwindigkeit, und wie viele
warten. Ohne Auth, weil ausschließlich Metadaten der eigenen Infrastruktur
dargestellt werden.

```bash
./monitor.sh start     # starten
./monitor.sh status    # Zustand auf der Konsole
./monitor.sh stop      # beenden
```

Der Server wird dabei **nie** angefasst: kein Neustart, keine geänderten Flags,
keine geschriebenen Requests. Der Monitor liest nur `/get_load`, `/metrics`,
`nvidia-smi` und das Server-Log.

**Was angezeigt wird und was nicht.** Der Halter wird über den Request-Lifecycle
im Server-Log bestimmt (`request.received` / `request.finished`). Weil dort nur
beim Abschluss eine IP steht, erscheint der Name des laufenden Slots erst nach
seinem Abschluss. Solange die Warteschlange Requests enthält, die vor der
Aktivierung dieser Events eingegangen sind (im Altbestand), ist der Halter nicht
bestimmbar; das Dashboard sagt das mit einem Banner, statt einen Namen zu raten.
Welcher Client auf welcher Position wartet, lässt sich ohne Eingriff in den
Datenpfad nicht feststellen — angezeigt wird nur die Zahl der Wartenden.

**Konfiguration.** Alle Variablen aus der Spec-Tabelle, etwa
`PORT=8090 MAX_LOG_MB=1024 RETENTION_H=24 ./monitor.sh start`.
IP-Namen kommen aus `systems.json`; unbekannte IPs fallen auf die rohe IP zurück.
````

In `README.md` denselben Abschnitt in englischer Fassung:

````markdown
### GPU slot monitor

Shows on `http://0.0.0.0:8090` which client currently holds the single GPU slot of
the sglang server, for how long, at what speed, and how many are waiting. No auth,
because it renders metadata of your own infrastructure only.

```bash
./monitor.sh start     # start
./monitor.sh status    # state on the console
./monitor.sh stop      # stop
```

The server is **never** touched: no restart, no changed flags, no written
requests. The monitor only reads `/get_load`, `/metrics`, `nvidia-smi` and the
server log.

**What is shown and what is not.** The holder is determined from the request
lifecycle in the server log (`request.received` / `request.finished`). Since the
IP only appears there on completion, the name of the running slot shows up after
that request finished. As long as the queue holds requests that arrived before
those events were active (the legacy backlog), the holder cannot be determined;
the dashboard says so in a banner instead of guessing a name. Which client waits
at which position cannot be determined without touching the data path — only the
count of waiting requests is shown.

**Configuration.** All variables from the spec table, for example
`PORT=8090 MAX_LOG_MB=1024 RETENTION_H=24 ./monitor.sh start`. IP names come from
`systems.json`; unknown IPs fall back to the raw IP.
````

- [ ] **Step 6: Run the full suite one more time**

Run: `python3.12 -m unittest discover -s tests -v 2>&1 | tail -5`
Expected: `OK`. Zusätzlich: `bash tests/test_monitor_sh.sh` → `Alle Skript-Tests bestanden`.

- [ ] **Step 7: Confirm the server survived the whole build**

Run:
```bash
ps -o pid,lstart,etime,cmd -p 145265 | tail -1
curl -s -o /dev/null -w "server: %{http_code}\n" http://127.0.0.1:30000/health
```
Expected: PID 145265 mit derselben Startzeit wie zu Beginn (4. Oktober), `etime` mehrere Tage, HTTP 200. Der Server hat den gesamten Aufbau unberührt überstanden.

- [ ] **Step 8: Commit**

```bash
git add tests/ README.de.md README.md
git commit -m "test: Absicherung gegen die widerlegte Socket-Quelle, Doku ergaenzt"
```

---

## Was am Ende existiert

```
monitor.sh                      start | stop | status | restart
systems.json                    IP -> Anzeigename
slotmon/config.py               alle Variablen, eine Stelle
slotmon/names.py                IP-Auflösung, harter Abbruch bei kaputter Datei
slotmon/logreader.py            inkrementeller Log-Reader, rid-Lifecycle, IP-Paarung
slotmon/slot.py                 Halter und Altbestand, rein
slotmon/sources.py              /get_load, /metrics, nvidia-smi, Fehler statt Exceptions
slotmon/history.py              JSONL mit Zeitfenster
slotmon/logrotate.py            truncate -s 0, Pfad-Wächter, Taktung
slotmon/collector.py            führt zusammen, hält den Zustand
slotmon/server.py               FastAPI, /api/state, /api/stream (SSE), /
slotmon/dashboard.html          Zwei Reiter, kein Prompt-Feld
tests/                          eine Datei je Einheit
```

## Was der Monitor ausdrücklich nicht behauptet

- keinen Systemnamen, wenn die IP zum laufenden Slot noch nicht vorliegt
- keinen Halter, solange Altbestand in der Queue liegt
- keine Zuordnung einzelner Wartender zu Systemen oder Positionen
- keine `0`, wo eine Zahl unbekannt ist