# Audio-Bausteine (Whisper STT + Piper TTS) Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein CPU-Audio-Sidecar auf `127.0.0.1:30003`, das über das Gateway auf `:30000` OpenAI-kompatible Whisper-Transkription und Piper-Sprachausgabe für deutsche Sätze liefert.

**Architecture:** Ein eigener FastAPI-Prozess (`aiserver_audio`) hält Whisper- und Piper-Modelle, Warteschlangen und ffmpeg. Das Gateway erkennt `/v1/audio/*` vor dem Body-Lesen, streamt Uploads bzw. Antworten zu diesem Sidecar und legt keine Jobs an. Modelle liegen unter `/mnt/models/audio` und werden ausschließlich per Skript geladen.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, faster-whisper 1.2.1, piper-tts 1.8.0, python-multipart 0.0.20, ffmpeg/ffprobe, huggingface_hub, stdlib `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-28-audio-stt-tts-design.md`

## Globale Vorgaben

- Python-Interpreter: `/home/user/small_qwen/.venv/bin/python`; Tests immer aus `aiserver-monitor`.
- Abhängigkeiten exakt pinnen: `faster-whisper==1.2.1`, `piper-tts==1.8.0`, `python-multipart==0.0.20`.
- Modellablage: `/mnt/models/audio/whisper/<small|medium|large-v3-turbo>` und `/mnt/models/audio/piper/<voice>.onnx` plus `.onnx.json`.
- Whisper: CPU, `int8`, 8 Threads, Cache 1, STT parallel 1, Queue 8, `vad_filter=True`, `beam_size=1`, `condition_on_previous_text=False`.
- TTS: parallel 2, 2 ONNX-Threads je Synthese, Queue 8, `speed` → `length_scale = 1 / speed`.
- Grenzen: Upload 100 MB, Audio 3600 s, TTS-Text 5000 Zeichen, STT 1800 s, TTS 120 s, Queue voll → 429 mit `Retry-After: 5`.
- Keine Audioinhalte, Prompts oder Transkripte loggen; nur Art, Modell, Dauer, Audio-Sekunden, RTF, Queue-Zeit, Status.
- Fehlerformat immer `{"error": {"message": str, "type": str, "code": str}}`.
- Bestehende Chat-/Embedding-Routen und alle bisherigen Tests bleiben unverändert grün.

## Review Focus

Diese Fälle sind in der Spec nicht ausdrücklich mit einem Test belegt und müssen in den genannten Tasks gepinnt werden:

1. Leerer oder nur aus Leerzeichen bestehender TTS-Text → `400 invalid_request`, kein Piper-Aufruf (Task 7).
2. Multipart ohne `file` oder mit kaputtem Body → `400 invalid_request`, kein `500` (Task 6).
3. Client trennt mitten in einer TTS-Antwort → ffmpeg wird beendet, kein Zombie, Temp-Dateien weg (Task 7).
4. Zwei STT-Anfragen mit unterschiedlichen Modellen gleichzeitig → Cache lädt nicht dasselbe Modell doppelt und evictet kein laufendes Modell (Task 3).
5. Text mit Umlauten und ein sehr langer Satz ohne Punkt → vollständig und unverändert synthetisiert (Task 4/7).

---

## Vorbereitung (einmalig, vor Task 1)

- [ ] `aiserver-monitor/requirements-audio.txt` mit den drei exakten Pins anlegen.
- [ ] `cd /home/user/small_qwen && .venv/bin/pip install -r aiserver-monitor/requirements-audio.txt`
- [ ] Importe prüfen: `.venv/bin/python -c "import faster_whisper, piper, multipart; print('ok')"`

---

### Task 1: Einstellungen, Katalog und Fehlerformat

**Files:**
- Create: `aiserver-monitor/aiserver_audio/__init__.py`
- Create: `aiserver-monitor/aiserver_audio/config.py`
- Create: `aiserver-monitor/aiserver_audio/catalog.py`
- Create: `aiserver-monitor/aiserver_audio/errors.py`
- Create: `aiserver-monitor/tests/test_audio_catalog.py`
- Create: `aiserver-monitor/tests/test_audio_config.py`

**Interfaces:**
- Produces: `AudioSettings`, `load_settings(env=None) -> AudioSettings`
- Produces: `WhisperInfo`, `PiperInfo`, `WHISPER_MODELS`, `PIPER_VOICES`
- Produces: `resolve_whisper(name: str | None) -> WhisperInfo`, `resolve_voice(name: str | None) -> PiperInfo`
- Produces: `whisper_dir(models_dir: Path, info: WhisperInfo) -> Path`, `piper_files(models_dir: Path, info: PiperInfo) -> tuple[Path, Path]`
- Produces: `whisper_installed(...) -> bool`, `voice_installed(...) -> bool`, `models_payload(models_dir: Path) -> dict`
- Produces: `AudioError(status_code: int, code: str, message: str)`, `audio_error_body(message, code) -> dict`

- [ ] **Step 1: Failing tests schreiben**

`test_audio_config.py`: Defaults (`host=127.0.0.1`, `port=30003`, `models_dir=/mnt/models/audio`, `threads=8`, `preload=whisper-small`, `cache=1`, `max_queue=8`, `stt_timeout_s=1800`, `tts_timeout_s=120`, `max_upload_mb=100`), Env-Overrides (`AUDIO_PORT`, `AUDIO_MODELS_DIR`, `AUDIO_PRELOAD=""`), Ablehnung von `AUDIO_PORT=0`, `AUDIO_THREADS=0`, `AUDIO_MAX_QUEUE=-1`.

`test_audio_catalog.py`: Aliase `whisper-1/small → whisper-small`, `medium`, `turbo/large-v3-turbo`; `thorsten/kerstin/eva`; unbekannt wirft `AudioError` mit `404 model_not_found`. Installationsstatus mit `TemporaryDirectory`: leer → false, nach Anlegen von `model.bin`/`config.json`/`tokenizer.json` → true; Piper braucht `.onnx` und `.onnx.json`. `models_payload` enthält Defaults, drei Whisper-IDs und drei Stimmen mit `size_mb`. `audio_error_body` liefert exakt die verschachtelte Form.

- [ ] **Step 2: RED prüfen**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -p 'test_audio_*.py' -v`
Expected: FAIL mit `ModuleNotFoundError: aiserver_audio`.

- [ ] **Step 3: Minimale Module implementieren**

`config.py`: `@dataclass(frozen=True)` mit den Defaults; `load_settings` liest `AUDIO_<FELD>` (Großbuchstaben), leere Werte gelten als nicht gesetzt; positive Ganzzahlen/Timeouts validieren. `models_dir` als `Path`.

`catalog.py`: eingefrorene Dataclasses mit `id`, `aliases`, `repo`, `size_mb`; Whisper zusätzlich `dirname` und `required_files=("model.bin","config.json","tokenizer.json")`; Piper zusätzlich `repo_path` und `sample_rate` (22050/22050/16000). Alias-Auflösung case-insensitiv, Default bei `None`/`""`. Größen aus der Spec (484/1528/1618 MB, 63/63/21 MB). Keine Modell-Datei anfassen.

`errors.py`: `AudioError` und `audio_error_body`; `type` ist `"invalid_request_error"` für 4xx, `"server_error"` für 5xx.

- [ ] **Step 4: GREEN prüfen**

Run wie Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_audio/__init__.py aiserver-monitor/aiserver_audio/config.py aiserver-monitor/aiserver_audio/catalog.py aiserver-monitor/aiserver_audio/errors.py aiserver-monitor/tests/test_audio_config.py aiserver-monitor/tests/test_audio_catalog.py aiserver-monitor/requirements-audio.txt
git commit -m "feat(audio): Katalog, Einstellungen und Fehlerformat"
```

---

### Task 2: Warteschlange und 429

**Files:**
- Create: `aiserver-monitor/aiserver_audio/queue.py`
- Create: `aiserver-monitor/tests/test_audio_queue.py`

**Interfaces:**
- Produces: `QueueFull(retry_after: int = 5)`
- Produces: `Admission(parallel: int, max_queue: int, retry_after: int = 5)`
- Produces: `await admission.acquire()`, `admission.release()`, `admission.waiting -> int`

- [ ] **Step 1: Failing tests**

Test: `parallel=1, max_queue=1`; erster Acquire erfolgreich; zweiter wartet; dritter wirft `QueueFull` mit `retry_after == 5`; nach `release()` kann der Wartende eintreten; `waiting` fällt danach auf 0. Test für `parallel=2`: zwei gleichzeitig, dritter wartet.

- [ ] **Step 2: RED**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -p 'test_audio_queue.py' -v`
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implementieren**

`asyncio.Semaphore(parallel)` plus Zähler `_waiting`. `acquire`: wenn `_waiting >= max_queue` → `QueueFull`; sonst Zähler erhöhen, `await sem.acquire()`, im `finally` dekrementieren. `release` ruft `sem.release()`.

- [ ] **Step 4: GREEN**

Run wie Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_audio/queue.py aiserver-monitor/tests/test_audio_queue.py
git commit -m "feat(audio): Warteschlange mit 429-Grenze"
```

---

### Task 3: Whisper-Engine und Dauer-Probe

**Files:**
- Create: `aiserver-monitor/aiserver_audio/stt.py`
- Create: `aiserver-monitor/aiserver_audio/ffmpeg.py`
- Create: `aiserver-monitor/tests/test_audio_stt.py`

**Interfaces:**
- Produces: `Segment(start: float, end: float, text: str)`, `Transcription(text: str, language: str, duration: float, segments: tuple[Segment, ...])`
- Produces: `SttEngine` Protocol mit `preload(info)` und `transcribe(path, *, info, language, prompt, temperature) -> Transcription`
- Produces: `FasterWhisperEngine(settings: AudioSettings)`
- Produces: `probe_duration(path: Path, *, runner=subprocess.run) -> float | None`

- [ ] **Step 1: Failing tests**

`test_audio_stt.py`:
- Fake-`faster_whisper`-Modul (über `mock.patch.dict(sys.modules, ...)`) zeichnet `WhisperModel`-Argumente auf; `FasterWhisperEngine.preload` lädt einmal, ein zweiter Aufruf nutzt den Cache.
- Zwei verschiedene Modelle bei `model_cache=1`: das erste wird nach dem zweiten Aufruf verworfen; bei `model_cache=2` bleiben beide.
- Gleichzeitige `preload`-Aufrufe für dasselbe Modell laden nur einmal (Lock; Review-Focus 4).
- `transcribe` reicht `language`, `initial_prompt=prompt`, `temperature`, `vad_filter=True`, `beam_size=1`, `condition_on_previous_text=False` weiter und wandelt Segmente in `Transcription` (Text mit Leerzeichen verbunden, `info.duration`, `info.language`).
- `probe_duration`: gültige Zahl → float; Exitcode != 0, leere Ausgabe, `NaN` → `None`; Befehl enthält `-show_entries format=duration`.

- [ ] **Step 2: RED**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -p 'test_audio_stt.py' -v`
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implementieren**

`stt.py` importiert `faster_whisper` erst in `_laden`; ein `threading.RLock` schützt Cache, Laden **und** die gesamte Transkription, damit ein Modell während des Laufs nicht evictet wird (STT ist ohnehin parallel 1). `device="cpu"`, `compute_type="int8"`, `cpu_threads=settings.threads`, `num_workers=1`. `transcribe` ruft `model.transcribe(str(path), language=..., task="transcribe", beam_size=1, vad_filter=True, condition_on_previous_text=False, initial_prompt=prompt, temperature=temperature)` und materialisiert die Segment-Iterable.

`ffmpeg.py` startet `ffprobe` mit fester Argumentliste, parst die erste Zeile als `float`, gibt bei jedem Fehler `None` zurück.

- [ ] **Step 4: GREEN**

Run wie Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_audio/stt.py aiserver-monitor/aiserver_audio/ffmpeg.py aiserver-monitor/tests/test_audio_stt.py
git commit -m "feat(audio): Whisper-Engine mit Modell-Cache"
```

---

### Task 4: Piper-Engine und Satz-Splitting

**Files:**
- Create: `aiserver-monitor/aiserver_audio/tts.py`
- Create: `aiserver-monitor/tests/test_audio_tts.py`

**Interfaces:**
- Produces: `AudioChunk(pcm: bytes, sample_rate: int)`
- Produces: `TtsEngine` Protocol mit `synthesize(text, *, info, speed) -> Iterator[AudioChunk]`
- Produces: `PiperEngine(settings: AudioSettings)`
- Produces: `split_sentences(text: str) -> list[str]`

- [ ] **Step 1: Failing tests**

- `split_sentences("Hallo. Wie geht es dir? Gut!")` → drei Sätze inklusive Satzzeichen.
- Text ohne Punkt bleibt ein Satz; Umlaute bleiben unverändert; `"  "` → `[]`.
- `PiperEngine.synthesize` lädt die Stimme einmal (Fake-`piper`-Modul) und übergibt `length_scale == 1/speed`; `AudioChunk.audio_int16_bytes` und `sample_rate` werden übernommen.
- Zwei Aufrufe mit derselben Stimme laden nicht neu; ein Wechsel lädt die neue Stimme.
- Zwei parallele Synthesen derselben Stimme erhalten zwei verschiedene `PiperVoice`-Instanzen (Pool für Parallelität 2), keine gemeinsam genutzte Session.
- Review-Focus 5: langer Satz ohne Punkt wird als genau ein Synthese-Aufruf übergeben.

- [ ] **Step 2: RED**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -p 'test_audio_tts.py' -v`
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implementieren**

`split_sentences`: `re.split(r"(?<=[.!?])\s+", text.strip())`, leere Teile verwerfen; keine inhaltliche Veränderung. `PiperEngine` importiert `piper` und `onnxruntime` lazy, lädt pro Stimme einen Pool aus zwei `PiperVoice`-Instanzen, jede mit eigener `InferenceSession` (`intra_op_num_threads=2`, `inter_op_num_threads=1`); `synthesize` leiht eine Instanz aus dem Pool und gibt sie im `finally` zurück. `SynthesisConfig(length_scale=1/speed)`, Chunks aus dem Generator weiterreichen. Ein `dict`-Cache pro Stimme genügt; ein Lock schützt nur das Befüllen des Pools.

- [ ] **Step 4: GREEN**

Run wie Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_audio/tts.py aiserver-monitor/tests/test_audio_tts.py
git commit -m "feat(audio): Piper-Engine und Satz-Splitting"
```

---

### Task 5: Ausgabeformate PCM, WAV, MP3, Opus

**Files:**
- Modify: `aiserver-monitor/aiserver_audio/tts.py`
- Modify: `aiserver-monitor/aiserver_audio/ffmpeg.py`
- Modify: `aiserver-monitor/tests/test_audio_tts.py`

**Interfaces:**
- Produces: `content_type(fmt: str) -> str`
- Produces: `stream_audio(chunks: AsyncIterable[AudioChunk], fmt: str, *, encoder_factory=start_encoder) -> AsyncIterator[bytes]`
- Produces: `start_encoder(fmt: str, sample_rate: int) -> asyncio.subprocess.Process`
- Produces: `wav_header(sample_rate: int) -> bytes`

- [ ] **Step 1: Failing tests**

- Content-Types: `pcm → audio/pcm`, `wav → audio/wav`, `mp3 → audio/mpeg`, `opus → audio/ogg`.
- `wav_header(22050)` ist 44 Bytes, beginnt mit `RIFF`/`WAVE`, Sample-Rate 22050, Kanäle 1, 16 Bit, RIFF- und data-Größe `0xFFFFFFFF`.
- `stream_audio` mit zwei `AudioChunk`s in einem Async-Iterator und `fmt="wav"` liefert Header + beide PCM-Blöcke in Reihenfolge.
- Mit `fmt="pcm"` bei 22050 wird `encoder_factory` genau einmal mit `("pcm", 22050)` aufgerufen; bei 24000 wird nichts gestartet.
- `fmt="mp3"`/`"opus"` startet den Encoder genau einmal für alle Chunks (nicht je Satz) und liefert dessen Ausgabe.
- Fake-Encoder zeichnet `write`-Reihenfolge auf; nach dem letzten Chunk wird `stdin` geschlossen und auf das Prozessende gewartet.

- [ ] **Step 2: RED**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -p 'test_audio_tts.py' -v`
Expected: FAIL wegen fehlender Funktionen.

- [ ] **Step 3: Implementieren**

`wav_header` mit `struct.pack("<4sI4s4sIHHIIHH4sI", ...)`. `stream_audio`:
- `wav`: Header, dann alle Chunks unverändert.
- `pcm` bei 24000: unverändert; sonst Encoder.
- `mp3`/`opus`: Encoder starten, eine Writer-Task schreibt alle Chunks in `stdin`, parallel `stdout` in 64-KiB-Blöcken liefern; `stderr=DEVNULL`; im `finally` `stdin.close()`, `terminate()`, `wait()` mit Timeout, sonst `kill()`.

`start_encoder` nutzt feste Argumente: `ffmpeg -hide_banner -loglevel error -f s16le -ar <rate> -ac 1 -i pipe:0` plus `-f s16le -ar 24000 -ac 1 pipe:1` (pcm), `-f mp3 -b:a 64k pipe:1` (mp3), `-f opus -c:a libopus -b:a 32k pipe:1` (opus).

- [ ] **Step 4: GREEN**

Run wie Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_audio/tts.py aiserver-monitor/aiserver_audio/ffmpeg.py aiserver-monitor/tests/test_audio_tts.py
git commit -m "feat(audio): PCM-, WAV-, MP3- und Opus-Streaming"
```

---

### Task 6: Audio-App mit Health, Modellen und Transkription

**Files:**
- Create: `aiserver-monitor/aiserver_audio/app.py`
- Create: `aiserver-monitor/tests/test_audio_app.py`

**Interfaces:**
- Produces: `create_audio_app(settings=None, *, stt_engine=None, tts_engine=None, duration_probe=probe_duration) -> FastAPI`
- Produces: `GET /healthz` → `{"status": "ok"}`
- Produces: `GET /v1/audio/models` → Katalog-JSON
- Produces: `POST /v1/audio/transcriptions` (multipart)

- [ ] **Step 1: Failing tests**

Mit `httpx.AsyncClient(transport=ASGITransport(app=app))` und `async with app.router.lifespan_context(app)`:
- `/healthz` 200 ohne Modelle.
- `/v1/audio/models` mit leerem Temp-Modellverzeichnis: alle `installed=false`, Defaults korrekt.
- Transkription mit Fake-STT und `duration_probe=lambda _: 2.5`: `json` → `{"text": "Hallo"}`; `text` → `text/plain`; `verbose_json` enthält `task="transcribe"`, `language`, `duration`, `segments`; `srt`/`vtt` enthalten Zeitstempel und Text.
- Default `model=whisper-small`, Default `language=de`; `model=whisper-1` funktioniert; `language=auto` wird durchgereicht; `task=translate` → 400; unbekanntes `response_format` → 400.
- Unbekanntes Modell → 404 `model_not_found`; bekannt, aber Verzeichnis leer → 503 `model_not_installed` mit `runtime/audio/download-models.sh --whisper medium`.
- Multipart ohne `file` → 400 `invalid_request` (Review-Focus 2), nicht 422/500.
- Upload über `max_upload_mb` → 413 `audio_too_large`; `duration_probe` > 3600 → 413 `audio_too_long`; `None` → 400 `invalid_audio`.
- STT-Queue voll → 429 `queue_full` mit `Retry-After: 5`; Timeout → 504 `timeout`.
- Transkript oder Prompt erscheint nicht im Log (`self.assertLogs` und `assertNotIn`).

- [ ] **Step 2: RED**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -p 'test_audio_app.py' -v`
Expected: FAIL `ModuleNotFoundError`/Importfehler.

- [ ] **Step 3: Implementieren**

`app.py`:
- Exception-Handler für `AudioError`, `QueueFull` und `RequestValidationError` (400 `invalid_request`).
- Upload in `tempfile.NamedTemporaryFile(delete=False)` blockweise (64 KiB); Größe zählen; im `finally` löschen.
- `async with app.router.lifespan_context` Preload: `settings.preload` auflösen und `await asyncio.to_thread(stt.preload, info)`; jeder Fehler nur loggen.
- Admission `Admission(1, settings.max_queue)`; Transkription in `asyncio.to_thread`, Timeout `settings.stt_timeout_s`.
- Formate über eine reine Funktion `format_transcript(transcription, fmt, task="transcribe")`; SRT/VTT mit `HH:MM:SS,mmm` bzw. `HH:MM:SS.mmm`.
- Logzeile `audio stt model=%s duration=%.1f rtf=%.2f status=%s` ohne Text.

- [ ] **Step 4: GREEN**

Run wie Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_audio/app.py aiserver-monitor/tests/test_audio_app.py
git commit -m "feat(audio): Transkriptions-Endpunkt"
```

---

### Task 7: Sprachausgabe-Endpunkt

**Files:**
- Modify: `aiserver-monitor/aiserver_audio/app.py`
- Modify: `aiserver-monitor/tests/test_audio_app.py`

**Interfaces:**
- Produces: `POST /v1/audio/speech` mit `SpeechRequest(input, model="piper", voice="de_DE-thorsten-medium", response_format="wav", speed=1.0)`
- Produces: `stream_speech(...) -> AsyncIterator[bytes]`

- [ ] **Step 1: Failing tests**

- Default `piper`/`thorsten`/`wav`; Aliase `tts-1`, `tts-1-hd`, `kerstin`, `eva` funktionieren.
- Leerer/Whitespace-Text → 400 `invalid_request`, Fake-TTS wurde nicht aufgerufen (Review-Focus 1); >5000 Zeichen → 400; `speed=0.4`/`2.1` → 400; unbekanntes `response_format` → 400.
- Unbekannte Stimme → 404 `model_not_found`; nicht installierte Stimme → 503 `model_not_installed`.
- Alle vier Formate liefern den korrekten Content-Type; `wav` beginnt mit `RIFF`; `pcm` liefert rohe Bytes.
- Mehrere Sätze: der erste Chunk ist beim Client, bevor der Fake die zweite Synthese startet (Event-Liste im Fake).
- Client-Disconnect: Generator schließen → Fake-Engine/Encoder wird geschlossen, kein Task bleibt übrig (Review-Focus 3).
- Queue voll → 429 mit `Retry-After`.
- Umlaute und ein 3000-Zeichen-Satz ohne Punkt kommen vollständig bei der Engine an (Review-Focus 5).

- [ ] **Step 2: RED**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -p 'test_audio_app.py' -v`
Expected: FAIL 404/Importfehler für Speech.

- [ ] **Step 3: Implementieren**

Pydantic-Modell `SpeechRequest`; Auflösung und Validierung vor dem Stream. `Admission(2, settings.max_queue)`; `acquire` vor der Response, `release` im Generator-`finally`. `stream_speech` synthetisiert Satz für Satz in `asyncio.to_thread` (Chunks eines Satzes werden gesammelt), gibt sie als Async-Iterator weiter und ruft `stream_audio` einmal mit dem gesamten Iterator auf (ein ffmpeg-Prozess für alle Sätze). Gesamt-Timeout `settings.tts_timeout_s`; nach dem Antwortstart nur noch Stream schließen und loggen. `StreamingResponse` mit Content-Type; kein `Content-Length`.

- [ ] **Step 4: GREEN**

Run wie Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_audio/app.py aiserver-monitor/tests/test_audio_app.py
git commit -m "feat(audio): Sprachausgabe mit Satz-Streaming"
```

---

### Task 8: Download-CLI und Manifest

**Files:**
- Create: `aiserver-monitor/aiserver_audio/download.py`
- Create: `aiserver-monitor/tests/test_audio_download.py`

**Interfaces:**
- Produces: `Selection(whisper: tuple[str, ...] = (), voices: tuple[str, ...] = ())`
- Produces: `download(selection: Selection, models_dir, *, check=False, snapshot=None, disk_usage=None) -> int`
- Produces: CLI `python -m aiserver_audio.download [--all|--whisper ...|--voices ...|--check]`

- [ ] **Step 1: Failing tests**

- `--check` mit leerem Verzeichnis: Exit 1, alle drei Whisper-IDs und drei Stimmen als fehlend; nach Anlegen der Required Files Exit 0.
- `download` ruft `snapshot_download` mit `repo_id`, `local_dir` und `allow_patterns` auf; Whisper-Ziel ist `whisper/<name>`, Piper landet flach als `<id>.onnx` + `.onnx.json`.
- Idempotenz: bereits installierte Modelle werden nicht erneut geladen.
- Weniger als 5 GB frei → Exit 1 mit klarer Meldung, kein Download.
- Fehlende Datei nach dem Download → Exit 1; `snapshot_download`-Fehler wird als Exit 1 gemeldet.
- Auswahl `--whisper small,medium` lädt nur diese; `--voices thorsten,eva` nur diese; ohne Argumente alles.
- Unbekannte ID → Exit 2 mit Usage.

- [ ] **Step 2: RED**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -p 'test_audio_download.py' -v`
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implementieren**

Manifest aus `catalog.py` nutzen. Freien Platz mit `disk_usage(models_dir).free >= 5 * 1024**3` prüfen. Piper in ein `TemporaryDirectory` laden und die zwei Dateien nach `piper/` kopieren. Nach dem Download `required_files` prüfen (existiert, Größe > 0). Ausgabe deutsch, `--check` mit `installed`/`missing` und Größe. `main(argv=None) -> int`; `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 4: GREEN**

Run wie Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_audio/download.py aiserver-monitor/tests/test_audio_download.py
git commit -m "feat(audio): Modell-Download mit Manifest und Check"
```

---

### Task 9: Gateway-Routing für Audio

**Files:**
- Modify: `aiserver-monitor/aiserver_monitor/config.py`
- Modify: `aiserver-monitor/aiserver_monitor/proxy.py`
- Modify: `aiserver-monitor/config.example.toml`
- Modify: `aiserver-monitor/tests/test_config.py`
- Modify: `aiserver-monitor/tests/test_proxy.py`

**Interfaces:**
- Produces Config: `audio_base="http://127.0.0.1:30003"`, `audio_timeout_s=1800.0`, `audio_max_upload_mb=100`
- Produces: `_ist_audio_pfad(path) -> bool`; `handle_audio(request, deps) -> Response`
- Produces: Audio errors `audio_too_large`, `upstream_unavailable`, `timeout`

- [ ] **Step 1: Failing tests**

`test_config.py`: Defaults und Env `AISERVER_MONITOR_AUDIO_BASE`, `_TIMEOUT_S`, `_MAX_UPLOAD_MB`; nicht-positive Timeouts/Upload-Limits werden mit `ValueError` abgewiesen.

`test_proxy.py` neue Klasse `TestAudioRouting`:
- `POST /v1/audio/transcriptions?x=1` mit Multipart-Rohbytes geht an `http://127.0.0.1:30003/v1/audio/transcriptions?x=1`, Body bytegleich, `Content-Type` bleibt Multipart, kein Job.
- `POST /v1/audio/speech` → `:30003`; Antwort ist `StreamingResponse`, Chunks kommen in Reihenfolge, Content-Type `audio/wav`, kein Job.
- `GET /v1/audio/models` → `:30003`, Body bytegleich.
- Unbekannter Pfad `/v1/audio/foo` → 404; `GET /v1/audio/transcriptions` und `POST /v1/audio/models` → 405; kein Upstream-Aufruf.
- `Content-Length` > Limit → 413 `audio_too_large`, Upstream wird nicht aufgerufen.
- Chunked Body über Limit → 413 (Body-Iterator zählt mit).
- `httpx.ConnectError` → 502 `upstream_unavailable`; `httpx.ReadTimeout` → 504 `timeout`.
- Chat- und Embedding-Routing bleiben unverändert (bestehende Tests).

- [ ] **Step 2: RED**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -k Audio -v`
Expected: FAIL (Audio landet bei `:30001`, Config-Felder fehlen).

- [ ] **Step 3: Implementieren**

In `handle_v1` **vor** `await request.body()`: Punktsegment-Prüfung, dann `if _ist_audio_pfad(...): return await handle_audio(...)`.

`handle_audio`:
- Pfad-/Methoden-Whitelist (models GET, transcriptions POST, speech POST); sonst 404/405 mit Audio-Fehlerbody.
- Transkription: `Content-Length` prüfen; `content=` als async Iterator über `request.stream()` mit Zähler; `build_request` + `send(stream=True)`; `_AudioZuGross` → 413. Timeout `httpx.Timeout(config.audio_timeout_s, connect=config.connect_timeout_s)`.
- Speech: JSON-Body gepuffert lesen (klein), an `:30003` senden, Antwort streamen.
- Status ≥ 400 vor dem Stream: `aread()` und gepufferte Response. Sonst `StreamingResponse` mit gefilterten Headern; Generator `aiter_raw()` und `finally: aclose()`.
- `TimeoutException` → 504, `RequestError` → 502; Meldungen ohne interne Details.
- Keine `registry`, `priority`, `hindsight`, `ensure_rid`-Aufrufe.

`config.example.toml`: Block `--- Audio (CPU) ---` mit den drei Feldern, auskommentiert. `audio_timeout_s` und `audio_max_upload_mb` in `Config.__post_init__` positiv prüfen.

- [ ] **Step 4: GREEN**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -k Audio -v`
Expected: PASS. Danach komplette Suite.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_monitor/config.py aiserver-monitor/aiserver_monitor/proxy.py aiserver-monitor/config.example.toml aiserver-monitor/tests/test_config.py aiserver-monitor/tests/test_proxy.py
git commit -m "feat(gateway): Audio-Routing mit Streaming und Limits"
```

---

### Task 10: Lebenszyklus, run.sh und systemd

**Files:**
- Create: `aiserver-monitor/aiserver_audio/__main__.py`
- Create: `runtime/audio/setup.sh`
- Create: `runtime/audio/download-models.sh`
- Create: `runtime/audio/start-audio.sh`
- Create: `runtime/audio/stop-audio.sh`
- Create: `runtime/audio/selftest.sh`
- Modify: `aiserver-monitor/run.sh`
- Modify: `aiserver-monitor/systemd/aiserver-monitor.service`
- Modify: `aiserver-monitor/tests/test_run_sh.sh`
- Modify: `aiserver-monitor/tests/test_config.py`

**Interfaces:**
- Produces: `python -m aiserver_audio` startet uvicorn mit `settings.host/port`
- Produces: `run.sh audio-setup`, `run.sh audio-models [args...]`
- Produces: `AUDIO_BASE="${AISERVER_MONITOR_AUDIO_BASE:-http://127.0.0.1:30003}"`

- [ ] **Step 1: Failing Tests**

`test_run_sh.sh` erweitern:
- `bash -n` für alle neuen Skripte.
- Testumgebung exportiert `AISERVER_MONITOR_AUDIO_BASE=http://127.0.0.1:39003`, `AUDIO_PORT=39003`, `AUDIO_PRELOAD=""`, `AUDIO_MODELS_DIR=$WORKDIR/audio`, damit die Produktion auf 30003 unberührt bleibt.
- `run.sh status` enthält eine Zeile `Audio-Sidecar (CPU)`.
- `usage` nennt `audio-setup` und `audio-models`.
- `run.sh audio-models --check` läuft mit leerem Verzeichnis und Exit 1, ohne Netzwerk.
- `stop` beendet den Audio-Sidecar, falls gestartet.

Config-Test: `AUDIO_BASE` wird von run.sh nicht in die App-Config geschrieben (nur Env-Durchreichung), aber Gateway-Config bleibt unverändert.

- [ ] **Step 2: RED**

Run: `cd aiserver-monitor && bash tests/test_run_sh.sh`
Expected: FAIL (Audio-Zeile/Subkommandos fehlen).

- [ ] **Step 3: Implementieren**

Skripte nach dem Muster von `runtime/embed/`:
- `setup.sh`: `ROOT` ermitteln, `.venv/bin/python` prüfen, `ffmpeg`/`ffprobe` prüfen, `pip install -r requirements-audio.txt`, Importcheck; idempotent.
- `download-models.sh`: `cd aiserver-monitor`, `exec "$PYTHON" -m aiserver_audio.download "$@"`.
- `start-audio.sh`: idempotent über `/healthz`; `nohup nice -n 5 "$PYTHON" -B -m aiserver_audio`, Env `CUDA_VISIBLE_DEVICES=""`, `OMP_NUM_THREADS=8`, `AUDIO_*`; PID `runtime/audio/audio.pid`, Log `/tmp/audio_server.log`; 20 s Health-Poll.
- `stop-audio.sh`: nur PID-Datei, TERM, 20 s, KILL, PID-Datei entfernen.
- `selftest.sh`: Modelle prüfen, per `curl` einen deutschen Satz als WAV erzeugen, per `curl` transkribieren, „Hallo“ prüfen, Laufzeit/RTF ausgeben; bei fehlenden Modellen Exit 0 mit Hinweis.

`run.sh`: `start_audio` nach `start_embed` und vor Gateway-Start (Fehler nur Warnung); `stop_audio` vor `stop_embed`; `status_audio` prüft `${AUDIO_BASE%/}/healthz` und zeigt die Anzahl installierter Modelle über `curl /v1/audio/models` (Fallback: `0/3`, `0/3`); `audio-setup` und `audio-models` in `usage` und `case`.

`systemd`: `Environment=AISERVER_MONITOR_AUDIO_BASE=http://127.0.0.1:30003`, `ExecStartPre=-.../start-audio.sh`, `ExecStopPost=-.../stop-audio.sh`.

- [ ] **Step 4: GREEN**

Run: `cd aiserver-monitor && bash -n run.sh && bash -n ../runtime/audio/*.sh && bash tests/test_run_sh.sh`
Expected: alle PASS; Produktionsports 30000/30001/30002/30003 unberührt.

- [ ] **Step 5: Commit**

```bash
git add aiserver-monitor/aiserver_audio/__main__.py runtime/audio aiserver-monitor/run.sh aiserver-monitor/systemd/aiserver-monitor.service aiserver-monitor/tests/test_run_sh.sh aiserver-monitor/tests/test_config.py
git commit -m "feat(audio): Sidecar-Lebenszyklus und run.sh"
```

---

### Task 11: Dokumentation, echte Modelle und Abnahme

**Files:**
- Modify: `README.md`, `README.de.md`
- Modify: `aiserver-monitor/README.md`, `aiserver-monitor/README.de.md`
- Modify: `aiserver-monitor/config.example.toml`

- [ ] **Step 1: Doku schreiben**

Je Sprache ein Abschnitt „Audio (CPU)“ / „Audio on the CPU“: Architektur, drei Whisper-Modelle, drei Stimmen, Download-Befehle, `curl`-Beispiele für Transkription und Speech, Grenzen/Fehlercodes, `run.sh audio-setup|audio-models`, Datenschutz, DACH-Hinweis (Standarddeutsch, keine AT/CH-Stimmen). `config.example.toml` verweist auf die drei Gateway-Felder.

- [ ] **Step 2: Modelle laden**

Run: `cd /home/user/small_qwen && runtime/audio/download-models.sh --all`
Expected: drei Whisper-Modelle + drei Stimmen unter `/mnt/models/audio`, ca. 3,8 GB; danach `runtime/audio/download-models.sh --check` Exit 0.

- [ ] **Step 3: Sidecar und Selftest**

Run: `cd /home/user/small_qwen && runtime/audio/start-audio.sh && runtime/audio/selftest.sh`
Expected: `/healthz` 200, Selftest erzeugt WAV, Whisper erkennt „Hallo“, Laufzeit/RTF werden ausgegeben.

- [ ] **Step 4: E2E über das Gateway**

`run.sh restart` (nur wenn kein aktiver Chat-Job), dann:
- `GET :30000/v1/audio/models` zeigt alle Modelle installiert.
- `POST :30000/v1/audio/transcriptions` mit der Selftest-WAV liefert deutschen Text.
- `POST :30000/v1/audio/speech` mit „Guten Tag.“ liefert `audio/wav` und erste Bytes vor Syntheseende.
- `/monitor/jobs` bleibt unverändert (kein Audio-Job).
- Audio-Sidecar stoppen → Audio 502, Chat `/healthz` und `/v1/models` weiter 200.

- [ ] **Step 5: Volle Suite**

Run: `cd aiserver-monitor && ../.venv/bin/python -m unittest discover -s tests -q && bash tests/test_run_sh.sh`
Expected: alle Tests grün (bisher 333 + neue Audio-Tests), keine Warnungen.

- [ ] **Step 6: Commit**

```bash
git add README.md README.de.md aiserver-monitor/README.md aiserver-monitor/README.de.md aiserver-monitor/config.example.toml
git commit -m "docs(audio): Endpunkte, Modelle und Betrieb"
```

---

## Abschluss-Check

- [ ] `runtime/audio/setup.sh` idempotent; Importcheck grün.
- [ ] `--check` Exit 1 bei fehlenden, Exit 0 bei vollständigen Modellen.
- [ ] `run.sh restart` startet Gateway, Embedding und Audio; `status` zeigt alle drei.
- [ ] Transkription und Speech über `:30000` funktionieren; `language` default `de`.
- [ ] 503 mit Download-Befehl, 404 bei unbekannter ID, 429 bei voller Queue, 502/504 bei Upstream-Problemen.
- [ ] Keine Audio-/Transkript-Inhalte im Log.
- [ ] Audio erscheint nicht in `/monitor/jobs`; Chat/Embeddings unverändert.
- [ ] Volle Suite und `test_run_sh.sh` grün.
