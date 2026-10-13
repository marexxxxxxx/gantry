# KI-Server-Monitor (aiserver-monitor) — Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein Gateway-Dienst vor dem Escha-SGLang-Server: transparente OpenAI-Weiterleitung mit Cloudflare-festen Keepalives, dazu ein deutsches Dark-Theme-Dashboard mit Live-Warteschlange, Job-Einblick (Vorschau/Denken/Antwort), Einzel-/Not-Abbruch und persistierten Statistiken.

**Architecture:** Ein Python-3.12-FastAPI-Prozess (`aiserver-monitor`, eigenes Verzeichnis im Repo) hört beim Cutover auf `0.0.0.0:30000`; SGLang wechselt dann auf `127.0.0.1:30001`. Bausteine: Passthrough mit Keepalives, Job-Registry (nur RAM), SSE-Hub, Metrik-Sammler, SQLite-History, Control-API; der React/TS-Build wird als Static vom Gateway ausgeliefert (ein Dienst, ein Port, ein Tunnelziel).

**Tech Stack:** Python 3.12 stdlib (asyncio, sqlite3, tomllib) · FastAPI 0.143 / uvicorn 0.54 / httpx 0.28 (alle im venv vorhanden, **keine neuen Python-Pakete**) · React + TypeScript + Vite + uPlot (npm, Build im Gateway serviert) · `unittest` (kein pytest) · Playwright für E2E (via `uv run --with playwright` + Chromium-1234, verifiziert) · systemd-User-Unit.

**Spec:** `docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md`

## Global Constraints

1. **Kein Fork-Patch.** SGLang-Code und -Server bleiben unverändert; bestehende Clients (hermes, paperclip, OpenCode …) werden nicht angefasst (höchstens eine Adresszeile).
2. **Abbrüche nur über** `POST {upstream}/abort_request` — Einzelabbruch mit `rid`, Notaus mit `abort_all=true`. **Nie eine leere rid senden** (SGLang macht aus `None` die leere Zeichenkette; `startswith("")` trifft alles).
3. **Keine Service-Auth** (Nutzerentscheidung). UI: Deutsch, Dark Theme, handytaglich.
4. **Vorschau-/Prompttexte nie persistieren:** SQLite speichert nur Metadaten + die ersten **100 Zeichen** (`prompt_start`). Voller Prompt und Textvorschauen existieren **nur im RAM**.
5. **Ein Prozess, ein Uvicorn-Worker** (In-Memory-Zustand ist die Quelle für Live-Daten).
6. **Ports:** Gateway bindet `0.0.0.0:30000` (Default), Upstream `http://127.0.0.1:30001`. Tests nutzen TestClient/ASGITransport oder Testports (39xxx). **30000/30001 werden bis zum Cutover nicht angefasst.**
7. Code, Kommentare, Tests: Deutsch, **ASCII-only** (ue/ae/oe/ss). UI-Texte dürfen Umlaute enthalten. `.venv` des Repos: `../.venv/bin/python` (small_qwen/.venv).
8. **`unittest`** (kein pytest). Jeder Task endet mit grünen Tests + Commit. Commit-Identität (global ist `user.name` leer):
   `git -c user.name="user" -c user.email="marecsalzm@gmail.com" commit -m "…"`.
9. Die zwei modifizierten Nutzerdateien `slotmon/dashboard.html` und `tests/test_server.py` werden **nie angefasst oder committet**. Bei `git add` immer exakte Pfade verwenden.
10. Laufender SGLang wird bis zum Cutover nicht gestartet/gestoppt/verändert; `slotmon-monitor.service` bleibt bis zur Abnahme aktiv.
11. **Keepalive-Konvention:** SSE-Kommentar `: ping` alle `heartbeat_s` (Default 15 s). Für `stream=false` werden bei Verzögerung vor der JSON-Antwort einzelne Leerzeichen-Bytes gesendet (`nonstream_keepalive`, Default AN) — der Gesamt-Body bleibt gültiges JSON.
12. **„Nicht gemessen“ heißt `null`/`–`, nie `0`** (Repo-Konvention).

## Review Focus

Die fünf wahrscheinlichsten Stolperstellen (Spec impliziert sie, kein Task-Test deckt sie automatisch mit ab) — jede Zeile hat ihren Test im genannten Task:

1. `stream=false` + lange Laufzeit (>125 s): Leerzeichen-Keepalives halten die CF-Strecke; der Body bleibt vorne/hinten gültiges JSON → Task 8.
2. Mehrbyte-UTF-8 auf Chunk-Grenzen darf Parser und Vorschau nicht zerstören → Tasks 5 (Parser) und 7 (Anbindung).
3. Wartende Queue-Anfrage >125 s: Keepalives laufen, bevor das erste Byte da ist; für Clients unsichtbar → Task 7.
4. Upstream bricht mitten im Stream ab / SGLang geht offline, oder der Client schließt den Browser: Job endet sauber (Fehler/Done), keine Zombie-Zeilen, SSE schließt → Tasks 7 und 8.
5. Gateway-Neustart mit laufenden Jobs: alte `waiting`/`running`-Zeilen werden beim Start als Fehler markiert; Dashboard baut aus Snapshot neu auf → Task 4 (Logik) + Task 10 (Wiring).

## Verifizierte Grundlagen (2026-10-13)

- Basissuite: **507 Tests grün** (3,3 s) via `.venv/bin/python -m unittest discover -s tests`.
- venv: Python 3.12.3 · fastapi 0.143.0 · uvicorn 0.54.0 · httpx 0.28.1 · Node v22.22.1/npm 10.9.4.
- **rid-Durchreichung bestätigt:** `serving_chat.py:304` (`rid=request.rid`), `serving_completions.py:119`; Request-Modelle in `protocol.py` mit `rid: Optional[...]` (Z. 310, 627, 929, 952) → Body-Feld `rid` (String) wird verwendet. Der Plan setzt die rid selbst (uuid4-Hex) und reicht eine Client-rid unverändert durch.
- **Metriknamen** (Prometheus, aktiv via `ENABLE_METRICS=1`): `sglang:num_running_reqs`, `sglang:num_queue_reqs`, `sglang:gen_throughput`, `sglang:token_usage`, `sglang:max_total_num_tokens` (weitere vorhanden, nicht benötigt). `/v1/loads` existiert (`http_server.py:395-397`) — für v1 nicht nötig (YAGNI).
- `monitor.sh`-Konventionen als Vorlage für `run.sh`: `start|stop|status|restart`, pgrep-Doppelstart-Sperre, `nohup`+Log, Health-Poll, TERM → 20 s → KILL, idempotentes `start`.

## Dateistruktur

```
aiserver-monitor/
  config.example.toml          Beispielkonfiguration
  run.sh                       start|stop|status|restart (monitor.sh-Konventionen)
  systemd/aiserver-monitor.service
  README.md / README.de.md     Nutzung/Pflege, EN/DE
  aiserver_monitor/            Python-Paket
    __init__.py  __main__.py
    config.py     Config/load_config (tomllib + env)
    jobs.py       Job, JobStatus, TextRing, JobRegistry, job_to_dict
    events.py     EventHub, format_sse, format_comment, sse_stream
    sse_parse.py  SSEByteParser, summarize_chunk (Upstream-Parsing)
    metrics_parse.py  parse_prometheus, parse_nvidia_smi, extract_sample
    history.py    SQLite: Schema, Schreiben, Stats, Retention, Recovery
    cancel.py     abort_request (einzel/all)
    proxy.py      ensure_rid, Deps, handle_v1 (Passthrough+Keepalives)
    collector.py  Metrik-Sammler (Loop)
    app.py        create_app (Routen, Static, Lifespan), client_source
  webapp/                      Vite + React + TS (Build -> aiserver_monitor/static)
  tests/                       test_*.py (unittest), fakes.py, e2e/
```

---

### Task 1: Grundgerüst & Konfiguration

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/__init__.py`
- Create: `aiserver-monitor/aiserver_monitor/config.py`
- Create: `aiserver-monitor/config.example.toml`
- Create: `aiserver-monitor/.gitignore`
- Test: `aiserver-monitor/tests/test_config.py`

**Interfaces:**
- Produces: `Config` (Dataclass) und `load_config(path: str | None = None, env: Mapping[str, str] | None = None) -> Config`

**Felder `Config` (Defaults):** `listen_host="0.0.0.0"`, `listen_port=30000`, `upstream_base="http://127.0.0.1:30001"`, `db_path="data/monitor.db"`, `log_path="data/aiserver-monitor.log"`, `heartbeat_s=15.0`, `poll_interval_s=2.0`, `preview_max_chars=4096`, `prompt_start_chars=100`, `retention_jobs_days=30`, `retention_series_days=7`, `nonstream_keepalive=True`, `gpu_cmd="nvidia-smi"`, `connect_timeout_s=10.0`.

- [ ] **Step 1: Failing test schreiben** (`tests/test_config.py`)

```python
class TestLoadConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config(env={})
        self.assertEqual(cfg.listen_port, 30000)
        self.assertEqual(cfg.upstream_base, "http://127.0.0.1:30001")
        self.assertAlmostEqual(cfg.heartbeat_s, 15.0)

    def test_toml_und_env_override(self):
        # toml: heartbeat_s = 5.0 ; env: AISERVER_MONITOR_LISTEN_PORT=39999
        # Erwartung: Datei schlaegt Default, env schlaegt Datei.
```

(Toml-Datei mit `tempfile` anlegen; env-Keys: Praefix `AISERVER_MONITOR_`, Uppercase, `_` statt `-`.)

- [ ] **Step 2: Tests laufen lassen — FAIL** (`cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -p "test_config.py" -v`; ModuleNotFoundError erwartet)
- [ ] **Step 3: Implementieren** — `config.py` (Dataclass + `tomllib.load` + env-Overrides mit Typkonvertierung), `__init__.py` (`__version__ = "0.1.0"`), `.gitignore` (`__pycache__/`, `data/`, `webapp/node_modules/`, `aiserver_monitor/static/`, `*.log`), `config.example.toml` (alle Felder kommentiert).
- [ ] **Step 4: Tests laufen lassen — PASS**
- [ ] **Step 5: Commit** — `git add aiserver-monitor/... && git -c user.name=… commit -m "Add aiserver-monitor scaffold and configuration"`

---

### Task 2: Job-Registry mit Textvorschau

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/jobs.py`
- Test: `aiserver-monitor/tests/test_jobs.py`

**Interfaces:**
- Produces: `JobStatus` (StrEnum: `waiting|running|done|aborted|error`), `TextRing`, `JobRegistry`, `job_to_dict(job) -> dict`
- `JobRegistry(preview_max_chars: int)`; Methoden: `register(rid, *, source, model, full_prompt, now) -> Job`, `get(rid) -> Job | None`, `note_running(rid, now)`, `add_text(rid, *, content=None, reasoning=None)`, `finish(rid, status, *, prompt_tokens=None, completion_tokens=None, error=None, now)`, `snapshot() -> list[dict]` (aktive zuerst, nach `created_at` sortiert)
- `TextRing(cap)`: `add(s)`, `text() -> str` — hält immer die **letzten** `cap` Zeichen.
- Token-Fallback: `add_text` zählt nicht-leere Deltas als `fallback_completion_tokens`; `finish(prompt_tokens=…)` überschreibt (usage schlägt Zählung). Nicht vorhandene Werte bleiben `None` → UI zeigt „–“.

- [ ] **Step 1: Failing test schreiben**

```python
def test_lebenszyklus_und_ring(self):
    reg = JobRegistry(preview_max_chars=5)
    j = reg.register("a1f3", source="10.0.0.9", model="m",
                     full_prompt="Hallo Welt und mehr", now=1000.0)
    self.assertIs(j.status, JobStatus.WAITING)
    reg.note_running("a1f3", now=1002.0)
    reg.add_text("a1f3", content="abcdef")
    reg.add_text("a1f3", reasoning="DENK")
    d = job_to_dict(reg.get("a1f3"))
    self.assertEqual(d["content_preview"], "bcdef")        # letzte 5 Zeichen
    self.assertEqual(d["reasoning_preview"], "DENK")
    self.assertEqual(d["ttft_ms"], 2000.0)
    reg.finish("a1f3", JobStatus.DONE, prompt_tokens=11, completion_tokens=7, now=1009.0)
    d = job_to_dict(reg.get("a1f3"))
    self.assertEqual(d["status"], "done"); self.assertEqual(d["completion_tokens"], 7)
    self.assertEqual(d["prompt_start"], "Hallo Welt und mehr")  # erster Teil, max 100

def test_fallback_zaehlung_und_none(self):
    # ohne usage: completion_tokens == Anzahl nicht-leerer content/reasoning-Deltas
    # vor finish: prompt_tokens ist None, nicht 0
```

- [ ] **Step 2: FAIL**
- [ ] **Step 3: Implementieren** (`dataclasses`, kein I/O, keine der asyncio-Belange; `job_to_dict` liefert u. a. `rid, source, model, prompt_start, status, created_at_ms, ttft_ms, duration_ms, prompt_tokens, completion_tokens, tok_per_s, content_preview, reasoning_preview, error`)
- [ ] **Step 4: PASS**
- [ ] **Step 5: Commit** — „Add job registry with in-memory text previews“

---

### Task 3: Metrik-Parser (Prometheus, nvidia-smi)

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/metrics_parse.py`
- Test: `aiserver-monitor/tests/test_metrics_parse.py`

**Interfaces:**
- Produces: `parse_prometheus(text: str) -> dict[str, float]` (summiert Samples je Metrikname, ignoriert `#`-Zeilen; Namen ohne `{labels}` normalisiert), `GpuSample` (Dataclass: `util_pct: int | None`, `vram_used_mb: int | None`, `vram_total_mb: int | None`), `parse_nvidia_smi(csv_text: str) -> GpuSample`, `METRIC_NAMES: dict[str, str]` (`running`, `waiting`, `throughput`, `token_usage`, `max_tokens` → sglang:…), `extract_sample(parsed, gpu, ts, available) -> dict`

- [ ] **Step 1: Failing test schreiben** — Prometheus-Beispieltext mit Kommentaren, Labels und zwei Samples derselben Metrik; `sglang:gen_throughput` fehlt → Key fehlt (nicht 0). nvidia-Beispiel `"12, 3456, 16384\n"` → `GpuSample(12, 3456, 16384)`; fehlerhaftes Feld → `None`.
- [ ] **Step 2: FAIL**
- [ ] **Step 3: Implementieren** — Zeilenweise; `float(value)`-Fehler werden verworfen; Gauges mit großen Werten (Token-Gauges) sind floats.
- [ ] **Step 4: PASS**
- [ ] **Step 5: Commit** — „Add metrics parsers for Prometheus and nvidia-smi“

---

### Task 4: History (SQLite, Retention, Recovery)

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/history.py`
- Test: `aiserver-monitor/tests/test_history.py`

**Interfaces:**
- Produces: `History(path: str | Path)`, Methoden: `init()`, `record_job(job_dict: dict)` (INSERT OR REPLACE auf `rid`), `record_sample(sample: dict)`, `mark_stale_running(now: float) -> int`, `query_stats(from_ts, to_ts, bucket_s: int) -> dict`, `cleanup(now: float) -> tuple[int, int]`, `close()`; `percentile(values: list[float], p: int) -> float | None`

**Schema (v1):**

```sql
CREATE TABLE IF NOT EXISTS jobs(
  rid TEXT PRIMARY KEY, source TEXT, model TEXT, prompt_start TEXT,
  status TEXT NOT NULL, created_at REAL NOT NULL,
  first_token_at REAL, finished_at REAL,
  prompt_tokens INTEGER, completion_tokens INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS samples(
  ts REAL PRIMARY KEY, available INTEGER NOT NULL,
  running INTEGER, waiting INTEGER, throughput REAL, token_usage REAL,
  gpu_util INTEGER, vram_used_mb INTEGER, vram_total_mb INTEGER);
```

`query_stats` liefert `{"overview": {count, done, aborted, error, tokens_in, tokens_out, ttft: {p50, p95}, duration: {p50, p95}}, "buckets": [...]}` — Buckets = gemittelte Samples je `bucket_s`-Fenster (nur gefüllte Spalten mitteln; `None` bleibt `None`).

- [ ] **Step 1: Failing test schreiben** — tempfile-DB; Insert+Query über Fenster; `percentile([10,20,30,40,100],50)==30` und `p95==100`; Retention löscht alte Jobs/Samples (Fake-`now`); `mark_stale_running` setzt `waiting|running` → `status='error', error='gateway restart'` und liefert die Anzahl.
- [ ] **Step 2: FAIL** · **Step 3: Implementieren** (`sqlite3`, `check_same_thread=False` unnötig: Aufrufe single-threaded via asyncio; `PRAGMA journal_mode=WAL`) · **Step 4: PASS**
- [ ] **Step 5: Commit** — „Add SQLite history with retention and restart recovery“

---

### Task 5: SSE-Bausteine (Hub, Format, Upstream-Parser)

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/events.py`
- Create: `aiserver-monitor/aiserver_monitor/sse_parse.py`
- Test: `aiserver-monitor/tests/test_events.py`, `aiserver-monitor/tests/test_sse_parse.py`

**Interfaces:**
- Produces `events.py`: `format_sse(event: str, data: dict) -> str` (`event:`/`data:`-Zeilen, `json.dumps(..., ensure_ascii=False)`, endet `\n\n`), `format_comment(text: str) -> str` (`f": {text}\n\n"`), `EventHub` (`subscribe() -> asyncio.Queue`, `unsubscribe(q)`, `publish(event: str, data: dict)`, `subscriber_count`), `async def sse_stream(hub, initial: list[str], heartbeat_s: float) -> AsyncIterator[str]` (sendet `initial` zuerst; danach Queue-Wait mit Timeout → `format_comment("ping")`).
- Produces `sse_parse.py`: `SSEByteParser` mit `feed(chunk: bytes) -> list[dict]` (Bytes-Buffer; nur **komplette Zeilen** parsen; `data: `-Prefix strippen; `[DONE]`, Kommentare und Leerzeilen verwerfen; UTF-8 erst beim Zeilen-Decode), `ChunkSummary` (Dataclass: `content`, `reasoning`, `prompt_tokens`, `completion_tokens`, `finish_reason` — alle `| None`), `summarize_chunk(obj: dict) -> ChunkSummary`.

- [ ] **Step 1: Failing tests schreiben**

```python
def test_parser_multibyte_ueber_grenzen(self):
    p = SSEByteParser()
    raw = 'data: {"choices":[{"delta":{"content":"gru\u0308\u00df"}}]}\n'.encode("utf-8")
    cut = raw.index("\u00df".encode("utf-8")) + 1        # mitten im Mehrbyte-Zeichen
    self.assertEqual(p.feed(raw[:cut]), [])
    objs = p.feed(raw[cut:])
    self.assertEqual(objs[0]["choices"][0]["delta"]["content"], "gru\u0308\u00df")

def test_sse_stream_heartbeat_wenn_leer(self):
    # hub ohne Ereignis, heartbeat_s=0.05 -> erste Ausgabe ist "initial",
    # danach kommt ": ping\n\n" innerhalb von ~0.3 s
```

- [ ] **Step 2: FAIL** · **Step 3: Implementieren** (`summarize_chunk` liest `choices[0].delta.content`, `choices[0].delta.reasoning_content`, `choices[0].finish_reason`, sowie Top-Level `usage.prompt_tokens/completion_tokens`) · **Step 4: PASS**
- [ ] **Step 5: Commit** — „Add SSE hub, formatting and upstream chunk parser“

---

### Task 6: Abbruch-Modul

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/cancel.py`
- Test: `aiserver-monitor/tests/test_cancel.py`

**Interfaces:**
- Produces: `async def abort_request(client: httpx.AsyncClient, upstream_base: str, *, rid: str | None, abort_all: bool) -> bool`
- Guards: bei `abort_all=False` muss `rid` ein nicht-leerer String mit Inhalt sein, sonst `ValueError` **ohne HTTP-Aufruf**. Body: `{"rid": rid or "", "abort_all": abort_all}`. HTTP 200 → `True`; andere Status → `False` (Log). Idempotent: doppelter Aufruf ist kein Fehler.

- [ ] **Step 1: Failing tests** — mit `httpx.MockTransport`: Einzelabbruch sendet exakt `{"rid": "abc", "abort_all": false}`; leere rid → `ValueError` und **kein** Request (Zähler im Transport); 500 → `False`; `abort_all=True` sendet `{"rid": "", "abort_all": true}`.
- [ ] **Step 2: FAIL** · **Step 3: Implementieren** · **Step 4: PASS**
- [ ] **Step 5: Commit** — „Add abort client for SGLang /abort_request“

---

### Task 7: Passthrough `stream=true` (Relay, Keepalives, Vorschau, Zählung)

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/proxy.py`
- Test: `aiserver-monitor/tests/test_proxy.py`
- Test helper: `aiserver-monitor/tests/fakes.py` (Fake-SGLang als FastAPI-App: skriptbare SSE-Ausgabe, `/metrics`, `/health`)

**Interfaces:**
- Produces: `@dataclass class Deps: config: Config; registry: JobRegistry; hub: EventHub; history: History | None; client: httpx.AsyncClient` (Client mit optional `transport` für Tests), `ensure_rid(body: dict) -> str` (Client-rid durchreichen, sonst `uuid4().hex` setzen), `async def handle_v1(request: Request, deps: Deps) -> Response`
- `handle_v1` deckt alle `/v1/*`-Methoden ab: GET ohne Body passthrough; POST mit JSON.

**Kernverhalten (Designvorgaben):**
1. **Header zuerst:** `client.send(request, stream=True)` mit `connect_timeout_s`; Status ≥ 400 → Body lesen, Status+Body unverändert zurück (kein SSE!). Erst danach `StreamingResponse(media_type="text/event-stream")`.
2. **Keepalive:** Lesetask füllt eine `asyncio.Queue`; der Client-Generator wartet mit `timeout=heartbeat_s` und sendet `format_comment("ping")`, wenn nichts kommt. Zwischen Bytes fließen die **Original-Bytes roh** zum Client (inkl. `[DONE]`).
3. **Messung/Vorschau:** Jeder Byte-Chunk geht zusätzlich durch `SSEByteParser` → `summarize_chunk` → `registry.note_running` (bei erstem Inhalt), `add_text`, bei `finish_reason`/Streamende `finish`; danach `hub.publish("job", job_to_dict(job))`, gedrosselt auf max. 4 Ereignisse/s je Job.
4. **Entkopplung:** Eigene Lesetask liest den Upstream **bis zum Ende**, auch wenn der Client vorzeitig abbricht (Generator-Cancel) — der Job wird trotzdem sauber beendet (Resultat verworfen). Kein Zombie-`running`.
5. `history.record_job` einmal beim `finish`.
6. rid-/Prompt-Erfassung: `full_prompt` aus `messages` (chat) bzw. `prompt` (completions) zusammensetzen (nur RAM), `prompt_start` = erste `prompt_start_chars`.

- [ ] **Step 1: Failing tests** (Fake-Upstream via `httpx.ASGITransport`):
  - `test_stream_relay_bytes_identisch`: Client erhält exakt die Upstream-SSE-Bytes bis `[DONE]`; Job endet `done` mit `completion_tokens` aus `usage`.
  - `test_keepalive_waehrend_queue`: Fake liefert 0,4 s keine Bytes (heartbeat 0,1 s) → Client empfängt mindestens zwei `: ping`-Kommentare, dann die Tokens.
  - `test_upstream_400_wird_durchgereicht`: Status und Body unverändert, kein Job-Neustart nötig.
  - `test_client_abbruch_job_laeuft_zu_ende`: Client bricht nach dem ersten Token ab; Registry zeigt kurze Zeit später `done` (kein `running`).
  - `test_client_rid_wird_durchgereicht` (Body-`rid` = `"abc"` → Request an Fake trägt `rid=abc`).
- [ ] **Step 2: FAIL** · **Step 3: Implementieren** (Kernstruktur: `_Relay`: Lesetask + `asyncio.Queue`; Generator liest Queue mit Timeout; `finally` räumt nur die Client-Seite ab) · **Step 4: PASS**
- [ ] **Step 5: Commit** — „Add streaming passthrough with keepalives and job tracking“

---

### Task 8: Passthrough `stream=false` & Fehlerpfade

**Files:**
- Modify: `aiserver-monitor/aiserver_monitor/proxy.py`
- Test: `aiserver-monitor/tests/test_proxy.py` (weitere Fälle)

**Verhalten:**
- `stream=false`: Upstream-POST als Task starten; `StreamingResponse(media_type="application/json")`-Generator wartet mit `timeout=heartbeat_s`; bei Timeout wird ein einzelnes `b" "` gesendet (nur wenn `config.nonstream_keepalive`), am Ende der vollständige Upstream-Body. **Kein** Leerzeichen, wenn die Antwort schneller als ein Intervall kommt (Byte-identische Antwort).
- Verbindungsfehler (SGLang aus): HTTP **502** mit `{"error": {"message": "sglang nicht erreichbar", "type": "upstream_unavailable"}}`; im Job `status=error`, `error`-Text; `hub.publish("job", …)`.

- [ ] **Step 1: Failing tests** — `test_nonstream_langsam_mit_ws_keepalive` (Fake schläft 0,25 s, Intervall 0,1 s → Body startet mit Leerzeichen und `json.loads(body)` funktioniert); `test_nonstream_schnell_byte_identisch`; `test_upstream_down_502` (Transport wirft `httpx.ConnectError`).
- [ ] **Step 2: FAIL** · **Step 3: Implementieren** · **Step 4: PASS**
- [ ] **Step 5: Commit** — „Add non-streaming path with keepalives and upstream error mapping“

---

### Task 9: Metrik-Sammler

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/collector.py`
- Test: `aiserver-monitor/tests/test_collector.py`

**Interfaces:**
- Produces: `Collector(deps: Deps, *, gpu_reader: Callable[[], Awaitable[GpuSample]] | None = None)`; `async def sample_once() -> dict` (holt `/metrics` + `/health`, liest GPU, baut Sample-Dict, `history.record_sample`, `hub.publish("stats", sample)`, gibt das Sample zurück); `async def run()` (Loop mit `poll_interval_s`, Fehler werden geloggt und übersprungen).
- Default-`gpu_reader`: `asyncio.create_subprocess_exec(config.gpu_cmd, "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits")`.

- [ ] **Step 1: Failing tests** — Fake-Upstream (ASGITransport) liefert `/metrics`+`/health` 200 → Sample enthält `available=True`, `running`, `waiting`, `throughput`; Fake aus → `available=False`, keine Exception; injizierter `gpu_reader` wird genutzt (kein nvidia-smi im Test).
- [ ] **Step 2: FAIL** · **Step 3: Implementieren** · **Step 4: PASS**
- [ ] **Step 5: Commit** — „Add metrics collector loop“

---

### Task 10: App-Wiring (Routen, Static, Lifespan, Client-IP)

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/app.py`
- Test: `aiserver-monitor/tests/test_app.py`

**Interfaces:**
- Produces: `create_app(config: Config, *, transport: httpx.AsyncBaseTransport | None = None, gpu_reader=None, enable_background: bool = True) -> FastAPI`; `client_source(headers, peer_host: str | None) -> str` (bevorzugt `CF-Connecting-IP` wenn gültige IP, sonst `peer_host`).

**Routen:** `GET /healthz` → `{"status": "ok"}` · `GET /monitor/jobs` (Snapshot) · `GET /monitor/events` (SSE: erst `snapshot`-Event mit Jobs + letztem Sample, dann Live) · `POST /monitor/jobs/{rid}/cancel` (→ `cancel.abort_request`; bei Erfolg Job `aborted` + publish; Antwort `{"ok": bool}`) · `POST /monitor/cancel_all` (Notaus; Antwort `{"ok": bool}`) · `GET /monitor/stats?from&to&bucket` (Fenster auf ≤30 d geklemmt) · `/v1/{path:path}` GET+POST → `handle_v1` · **zuletzt** Static-Mount `aiserver_monitor/static` auf `/` (`html=True`), fehlendes Verzeichnis tolerieren (Platzhalterseite).

**Lifespan:** `history.init()` → `history.mark_stale_running(now)` → bei `enable_background`: Collector-Task starten → bei Shutdown: Task canceln, `history.close()`.

- [ ] **Step 1: Failing tests** (TestClient mit `with TestClient(app) as c:`):
  - healthz 200; `/monitor/jobs` leer/gefüllt;
  - Cancel-Route mit MockTransport prüft Weiterleitung + Job-Status `aborted`;
  - cancel_all sendet `abort_all=true`;
  - SSE: erstes Event ist `snapshot` (mit Beispielherzschlag 0,05 s);
  - Recovery: vorbereitete DB mit `running`-Job → nach Start `error`;
  - `/v1/models` passthrough an Fake (Transport);
  - `client_source`: Fake-Header `CF-Connecting-IP: 203.0.113.7` gewinnt; ohne Header die Peer-IP; ungültiger Header → Peer.
- [ ] **Step 2: FAIL** · **Step 3: Implementieren** · **Step 4: PASS**
- [ ] **Step 5: Commit** — „Add gateway app wiring, control API and lifespan“

---

### Task 11: Betrieb (Einstiegspunkt, Logging, run.sh, systemd, README)

**Files:**
- Create: `aiserver-monitor/aiserver_monitor/__main__.py`
- Create: `aiserver-monitor/run.sh`
- Create: `aiserver-monitor/systemd/aiserver-monitor.service`
- Create: `aiserver-monitor/README.md`, `aiserver-monitor/README.de.md`
- Test: `aiserver-monitor/tests/test_run_sh.sh`

**Interfaces:** `python -m aiserver_monitor` startet uvicorn mit `create_app(load_config())`; Logging: `RotatingFileHandler(config.log_path, maxBytes=5_000_000, backupCount=3)`, Format `"%(asctime)s %(levelname)s %(name)s %(message)s"`.
`run.sh`: Subkommandos `start|stop|status|restart` nach `monitor.sh`-Konventionen (pgrep-Sperre auf `aiserver_monitor`, `nohup` + Log, Health-Poll auf `/healthz`, TERM → 20 s → KILL, idempotent).
`systemd/aiserver-monitor.service`: User-Unit, `WorkingDirectory=/home/user/small_qwen/aiserver-monitor`, `ExecStart=/home/user/small_qwen/.venv/bin/python -m aiserver_monitor`, `Restart=always`, `RestartSec=3`.

- [ ] **Step 1: Failing Shell-Test schreiben** (`test_run_sh.sh`): `bash -n run.sh`; `run.sh status` auf nicht laufender Instanz → Exit 1; `run.sh stop` idempotent → Exit 0.
- [ ] **Step 2: FAIL** · **Step 3: Implementieren** (README beidsprachig: Zweck, Start/Stop, Konfiguration, Konfigdatei-Referenz, Verweis auf Spec, Rollback-Hinweis) · **Step 4: Test PASS** — plus manueller Kurzstart: `./run.sh start` auf Testport (`AISERVER_MONITOR_LISTEN_PORT=39055`) → Health-Poll ok → `./run.sh stop` (nie 30000!).
- [ ] **Step 5: Commit** — „Add run script, systemd unit and operator docs“

---

### Task 12: Webapp-Gerüst (Vite + React + TS)

**Files:**
- Create: `aiserver-monitor/webapp/package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`
- Create: `aiserver-monitor/webapp/src/main.tsx`, `App.tsx`, `styles.css`, `api.ts`
- Create: `aiserver-monitor/webapp/src/TimeChart.tsx` (uPlot-Wrapper: `props {title, unit, series: {label, points: [number, number][]}[]}`)

**api.ts (Typen + Funktionen, eingefroren):** `Job` (Felder wie `job_to_dict`), `StatsWindow`, `Sample`; `connectEvents(on: (event: string, data: any) => void) -> EventSource` (Reconnect mit Backoff 1 s/2 s/5 s, Status-Callback), `fetchJobs()`, `fetchStats(fromMs, toMs, bucketS)`, `cancelJob(rid)`, `cancelAll()`.
**Build:** `npm run build` → `outDir ../aiserver_monitor/static`, `emptyOutDir: true`; Dev-Proxy `/v1`, `/monitor`, `/healthz` → `http://127.0.0.1:30000`.
**App.tsx:** Kopfleiste (Server-Status, Modell, GPU-Kompaktanzeige, Tabs `tab-live`/`tab-stats`, Notaus) + leerer Inhaltsbereich; Dark Theme als CSS-Variablen.

- [ ] **Step 1: Dateien schreiben** (inkl. `npm install react react-dom uplot` + dev `typescript vite @vitejs/plugin-react @types/react @types/react-dom`; lockfile entsteht)
- [ ] **Step 2: Build beweisen** — `cd webapp && npm run build` → „built in …“; `../.venv/bin/python -c "from … import create_app; …"` → Gateway liefert `/` (Smoke).
- [ ] **Step 3: Commit** (ohne `node_modules`/`static` — .gitignore) — „Add React/TypeScript dashboard scaffold“

---

### Task 13: Live-Ansicht

**Files:**
- Create: `aiserver-monitor/webapp/src/LiveView.tsx`, `src/JobTable.tsx`, `src/PreviewModal.tsx`
- Modify: `aiserver-monitor/webapp/src/App.tsx`

**Verhalten (aus den akzeptierten Mockups):** Queue-Leiste (laufend/wartend + Sparkline aus `stats`-Events); Job-Tabelle (Status-Ampel, Quelle, Promptanfang, Laufzeit live, TTFT, Tokens in/out, tok/s, ✕ je Job mit `job-cancel`); Klick auf Zeile/`👁` öffnet `PreviewModal` (`modal`): voller Prompt (`modal-prompt`), „Denken“ zusammengeklappt (`modal-thinking`), „Antwort“ mitlaufend + Auto-Scroll (`modal-answer`); Filter Aktiv/Alle (`filter-active`); Notaus mit Zwei-Klick (`cancel-all`, `confirm-cancel-all`). Zustand: `connectEvents` → `snapshot` (ersetzen), `job` (upsert nach `rid`), `stats` (letzter Wert).
**data-testid-Vertrag** (Playwright in Task 15 nutzt genau diese): `tab-live`, `tab-stats`, `conn-status`, `queue-stats`, `job-row`, `job-row-{rid}`, `job-status`, `job-cancel`, `job-preview`, `modal`, `modal-thinking`, `modal-answer`, `modal-prompt`, `filter-active`, `cancel-all`, `confirm-cancel-all`.

- [ ] **Step 1: Komponenten implementieren** (Umlaute erlaubt; keine neuen Bibliotheken)
- [ ] **Step 2: Build + Typprüfung** — `npm run build` → PASS
- [ ] **Step 3: Sichtprüfung** — `npm run dev` mit laufendem Gateway? In diesem Task: stattdessen Gateway mit Fake? (Nur wenn verfügbar; sonst Task 15.) Mindestens: Build grün.
- [ ] **Step 4: Commit** — „Add live view with job table, preview modal and cancel actions“

---

### Task 14: Statistik-Ansicht

**Files:**
- Create: `aiserver-monitor/webapp/src/StatsView.tsx`
- Modify: `aiserver-monitor/webapp/src/App.tsx`

**Verhalten:** Zeitfenster-Chips `stat-window-1h|24h|7d|30d` (Bucket: 1 m/5 m/30 m/3 h); KPI-Karten (`kpi-count`, `kpi-error`, `kpi-ttft`, `kpi-duration`, `kpi-tokens`); vier `TimeChart`s (`chart-queue`, `chart-throughput`, `chart-tokens`, `chart-gpu`); Leerzustand „keine Daten im Zeitfenster“.

- [ ] **Step 1: Implementieren** · **Step 2: Build PASS** · **Step 3: Commit** — „Add statistics view with charts and time windows“

---

### Task 15: E2E mit Playwright (Fake-SGLang)

**Files:**
- Create: `aiserver-monitor/tests/e2e/fake_sglang.py` (nutzt `tests/fakes.py`)
- Create: `aiserver-monitor/tests/e2e/dashboard.spec.py` (Playwright sync API, deutsch kommentiert)
- Create: `aiserver-monitor/tests/e2e/run_e2e.sh`
- Create: `aiserver-monitor/tests/e2e/gpu_stub.sh` (`echo "33, 4096, 16384"`)

**Ablauf `run_e2e.sh`:** Fake-SGLang auf `127.0.0.1:39001` (skriptete Jobs: ein langsamer Token-Strom, ein Queue-Fall), Gateway auf `127.0.0.1:39000` mit `AISERVER_MONITOR_UPSTREAM_BASE=http://127.0.0.1:39001` und `AISERVER_MONITOR_GPU_CMD=tests/e2e/gpu_stub.sh`; dann Playwright (verifizierter Weg):

```bash
uv run --with playwright python dashboard.spec.py   # plus EXE=/home/user/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome
```

**Szenarien (Assertions):** Live-Tab zeigt Queue + Job `job-row-{rid}` (wartend→laufend), Vorschau wächst; Modal: `modal-thinking` eingeklappt, `modal-answer` streamt; `job-cancel` × 1 → Status `abgebrochen`; Notaus-Zwei-Klick; Statistik-Tab: KPIs + vier Charts sichtbar, Zeitchip wechselt; Reconnect: Gateway kurz stoppen/starten → `conn-status` „verbunden“ innerhalb 10 s (Snapshot wiederaufgebaut).

- [ ] **Step 1: Dateien schreiben** · **Step 2: `bash tests/e2e/run_e2e.sh` → alle Szenarien PASS** · **Step 3: Commit** — „Add Playwright end-to-end dashboard tests with fake SGLang“

---

### Task 16: Cutover & Abnahme — **NUTZER-GATED: nicht ohne ausdrückliche Freigabe ausführen**

**Files:**
- Modify: `runtime/sglang/serve.sh` (`PORT`-Default → `30001`, Kommentar)
- Create: `aiserver-monitor/docs/cutover.md` (Runbook, DE/EN-Kurzfassung)

- [ ] **Step 1: Stopp — Freigabe + CF-Hostname-Liste vom Nutzer einholen** (spec §11: Hostnames/Zielports aus dem CF-Dashboard; lokale Configs mit „30000“ sichten: `grep -rn "30000" ~/hermes ~/paperclip ~/9Router-* 2>/dev/null` als *Lese*-Check).
- [ ] **Step 2: Vorflug** — Gateway lokal gegen Fake geprüft (Task 15 grün); `run.sh status` sauber; Backup: `cp serve.sh serve.sh.bak-20261013`.
- [ ] **Step 3: SGLang umstellen** — `PORT=30001` committen; Server stoppen/starten (`runtime/sglang/stop-server.sh`, `serve.sh`); `curl /health` auf 30001.
- [ ] **Step 4: Gateway starten** — `run.sh start`; Checks: `/healthz`, `/v1/models` über `:30000`, Dashboard im LAN (`http://10.24.1.50:30000`).
- [ ] **Step 5: systemd** — Unit installieren (`~/.config/systemd/user/`, `systemctl --user daemon-reload && enable --now aiserver-monitor`), `loginctl enable-linger` prüfen; Neustart-Test (`kill -TERM` → Auto-Restart).
- [ ] **Step 6: Cloudflare (Nutzer)** — Monitor-Hostname aufs Gateway zeigen lassen; danach `curl` gegen die öffentliche URL.
- [ ] **Step 7: Abnahmetests (echt)** — (a) Dashboard-SSE 10 min offen: `curl -N https://monitor-aiserver.marechomes.com/monitor/events | grep -c "ping"` (Heartbeats laufen); (b) Job >125 s über die öffentliche URL (langen Prompt/`max_tokens` wählen) → antwortet vollständig; (c) Live-Abbruch im Browser wirkt (GPU idle in `nvidia-smi`).
- [ ] **Step 8: Altbestand** — `systemctl --user stop slotmon-monitor && systemctl --user disable slotmon-monitor` (erst JETZT, nach bestandenen Abnahmetests).
- [ ] **Step 9: Rollback dokumentieren** — `serve.sh` zurück auf 30000, Gateway stoppen, slotmon wieder starten; Runbook committen.

---

## Selbstprüfung des Plans

- **Spec-Deckung:** §1 Auftrag → Tasks 2/7/10/13–15 (Queue, Einblick, Abbruch, Statistik, CF); §2 Randbedingungen → Global Constraints 1–4, 9–11; §5 Architektur/Module → Tasks 2–10; §6 UI → Tasks 12–14; §7 Transport → Tasks 5/7/8; §8 Rollout → Task 16; §9 Betrieb → Tasks 4/9/10/11; §10 Tests → Unit/Integration in Tasks 2–10, Browser in 15, CF-Abnahme in 16. §11: Punkt 1/2 → Task 16 Steps 1/6; Punkt 3 (`aiserver-monitor/`) bestätigt im Design; Punkt 4 (rid) → verifizierte Grundlagen + Tasks 6/7.
- **Typkonsistenz:** `job_to_dict`-Felder (Task 2) ↔ `History.record_job` (Task 4) ↔ `api.ts`-Typen (Task 12); `Deps` (Task 7) von 9/10 genutzt; `EventHub.subscribe/publish` konsistent (Tasks 5/9/10); `abort_request`-Signatur (Task 6) ↔ Route (Task 10).
- **Review-Focus:** 1→Task 8 Step 1; 2→Task 5 Step 1 + Task 7 Step 1; 3→Task 7 Step 1; 4→Task 7 Step 1 + Task 8 Step 1; 5→Task 4 Step 1 + Task 10 Step 1.
