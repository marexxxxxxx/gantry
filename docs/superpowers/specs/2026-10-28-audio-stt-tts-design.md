# Design: Audio-Bausteine (Whisper STT + Piper TTS) auf der CPU

Datum: 2026-10-28
Erweitert: `2026-10-13-aiserver-monitor-design.md` (Gateway auf Port 30000)
Status: Design freigegeben, Spezifikation zur Prüfung

## Warum es das gibt

Der Server soll Sprache verstehen und erzeugen können, ohne die GPU zu belasten.
Ein typischer Telefon-Agent besteht aus dieser Kette:

```
Anrufer → Transport → VAD → STT → LLM → TTS → Audio zurück
```

Transport, VAD, Barge-in und die State Machine übernimmt Pipecat. Dieser Server
liefert die beiden CPU-Bausteine:

- **STT:** gesammelter Satz als Audiodatei → Text (Whisper)
- **TTS:** Satz als Text → Audiodaten (Piper)

Die GPU bleibt vollständig dem Chat-Modell vorbehalten. Embeddings laufen bereits
auf der CPU; Audio kommt als eigener Sidecar dazu.

## Entscheidungen des Nutzers

| Frage | Entscheidung |
|---|---|
| Umfang | Nur Server-Bausteine, kein Twilio-/Pipecat-Agent, keine State Machine |
| Richtungen | Beides: Speech-to-Text und Text-to-Speech |
| Whisper-Modelle | `small`, `medium`, `large-v3-turbo` |
| Piper-Stimmen | Thorsten (männlich), Kerstin (weiblich), Eva (weiblich) |
| Architektur | Eigener Audio-Sidecar auf `127.0.0.1:30003` |
| Live-Verarbeitung | Satzweise, kein 20-ms-Chunk-Streaming im Server |
| TTS-Ausgabe | HTTP-Streaming, satzweise, erster Ton vor dem Ende der Synthese |
| Downloads | Skript, kein automatischer Download im Request |
| Sprache | Default Deutsch, `auto` möglich |
| DACH-Stimmen | Standarddeutsch; keine eigenen AT/CH-Piper-Stimmen vorhanden |

## Nicht-Ziele

- Kein WebSocket-Endpunkt für 20-ms-Audiochunks.
- Keine VAD-, Barge-in-, Twilio- oder Pipecat-Logik in diesem Repo.
- Keine State Machine, keine Tool Calls, keine Kundenkonfiguration.
- Kein LLM-Aufruf durch den Audio-Sidecar; das macht die Pipeline.
- Keine Authentifizierung (der Sidecar ist nur lokal erreichbar).
- Kein GPU-Betrieb, kein Training, kein Fine-Tuning.
- Keine Diarisierung, keine Übersetzung, keine Sprachidentifikation.
- Keine Audio-Modelle im Gateway-Prozess.

## Architektur

```
Client / Pipecat
       │  HTTP :30000
       ▼
┌─────────────────────────────┐
│ Gateway (aiserver_monitor)  │
│  /v1/audio/transcriptions   │──┐
│  /v1/audio/speech           │  │  HTTP :30003, nur localhost
│  /v1/audio/models           │  ▼
│  Chat → :30001              │ ┌──────────────────────────┐
│  Embeddings → :30002        │ │ Audio-Sidecar            │
└─────────────────────────────┘ │  faster-whisper (int8)   │
                                │  piper-tts + ffmpeg      │
                                │  Queue + Modell-Cache    │
                                └──────────────────────────┘
```

Der Gateway bleibt der einzige öffentliche Einstiegspunkt. Der Audio-Sidecar
bindet ausschließlich an `127.0.0.1:30003`.

### Komponenten

| Komponente | Aufgabe |
|---|---|
| `aiserver_monitor/proxy.py` | erkennt Audio-Pfade, streamt Uploads und TTS-Antworten, legt keine Jobs an |
| `aiserver_audio/app.py` | FastAPI-Endpunkte, Validierung, Fehlerformat |
| `aiserver_audio/catalog.py` | Modell-IDs, Aliase, Pfade, Installationsstatus |
| `aiserver_audio/stt.py` | faster-whisper, Modell-Cache, Transkription |
| `aiserver_audio/tts.py` | Piper, Satz-Splitting, PCM/WAV/MP3/Opus-Streaming |
| `aiserver_audio/queue.py` | Semaphoren, Warteschlange, 429 |
| `aiserver_audio/download.py` | Manifest-Downloads, `--check` |
| `runtime/audio/*.sh` | Setup, Start, Stopp, Download |

## API

Alle Endpunkte liegen unter dem bestehenden Gateway `http://<server>:30000`.
Der direkte Sidecar-Port ist nicht Teil der öffentlichen Schnittstelle.

### `POST /v1/audio/transcriptions`

Multipart, OpenAI-kompatibel.

| Feld | Pflicht | Default | Bedeutung |
|---|---|---|---|
| `file` | ja | – | Audiodatei; ffmpeg unterstützt die üblichen Formate |
| `model` | nein | `whisper-small` | Modell-ID oder Alias |
| `language` | nein | `de` | ISO-Code oder `auto` |
| `response_format` | nein | `json` | `json`, `text`, `srt`, `vtt`, `verbose_json` |
| `prompt` | nein | – | Fachbegriffe, Namen, Kontext |
| `temperature` | nein | `0` | wird an Whisper weitergereicht |

Beispiel:

```bash
curl -X POST http://server:30000/v1/audio/transcriptions \
  -F file=@aufnahme.mp3 \
  -F model=whisper-medium \
  -F language=de \
  -F response_format=json
```

Antwort `json`:

```json
{"text": "Guten Tag, ich möchte einen Tisch reservieren."}
```

`verbose_json` enthält zusätzlich `task`, `language`, `duration` und `segments`
mit `start`, `end` und `text`. `text`, `srt` und `vtt` liefern die entsprechenden
Content-Types.

**Modell-IDs und Aliase:**

| Kanonisch | Akzeptierte Aliase |
|---|---|
| `whisper-small` | `small`, `whisper-1` |
| `whisper-medium` | `medium` |
| `whisper-large-v3-turbo` | `turbo`, `large-v3-turbo` |

`task=translate` wird mit `400` abgelehnt; dieser Server transkribiert nur.

### `POST /v1/audio/speech`

JSON, OpenAI-kompatibel.

| Feld | Pflicht | Default | Bedeutung |
|---|---|---|---|
| `input` | ja | – | Text, max. 5.000 Zeichen |
| `model` | nein | `piper` | `piper`, `tts-1`, `tts-1-hd` |
| `voice` | nein | `de_DE-thorsten-medium` | Stimmen-ID oder Kurzalias |
| `response_format` | nein | `wav` | `wav`, `pcm`, `mp3`, `opus` |
| `speed` | nein | `1.0` | 0,5–2,0; wird auf Piper `length_scale` abgebildet |

Beispiel:

```bash
curl -X POST http://server:30000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"piper","input":"Guten Tag, wie kann ich helfen?",
       "voice":"de_DE-thorsten-medium","response_format":"wav"}' \
  --output antwort.wav
```

**Stimmen:**

| Kanonisch | Kurzalias | Sample Rate | Charakter |
|---|---|---|---|
| `de_DE-thorsten-medium` | `thorsten` | 22.050 Hz | männlich, Default |
| `de_DE-kerstin-low` | `kerstin` | 22.050 Hz | weiblich |
| `de_DE-eva_k-x_low` | `eva` | 16.000 Hz | weiblich, schnell |

**Formate:**

| `response_format` | Content-Type | Ausgabe |
|---|---|---|
| `pcm` | `audio/pcm` | 24 kHz, 16 Bit, mono, little-endian (OpenAI-kompatibel) |
| `wav` | `audio/wav` | native Stimmen-Rate, Streaming-WAV |
| `mp3` | `audio/mpeg` | mono, 64 kbit/s |
| `opus` | `audio/ogg` | mono, 32 kbit/s, Ogg-Container |

Die Antwort wird **immer gestreamt**. Bei mehreren Sätzen im `input` wird jeder
Satz einzeln synthetisiert und sofort ausgeliefert. Der erste Ton kommt nach dem
ersten Satz, nicht nach dem gesamten Text.

### `GET /v1/audio/models`

```json
{
  "whisper": {
    "default": "whisper-small",
    "models": [
      {"id": "whisper-small", "installed": true, "size_mb": 484},
      {"id": "whisper-medium", "installed": true, "size_mb": 1528},
      {"id": "whisper-large-v3-turbo", "installed": false, "size_mb": 1618}
    ]
  },
  "tts": {
    "default_model": "piper",
    "default_voice": "de_DE-thorsten-medium",
    "voices": [
      {"id": "de_DE-thorsten-medium", "installed": true, "size_mb": 63},
      {"id": "de_DE-kerstin-low", "installed": true, "size_mb": 63},
      {"id": "de_DE-eva_k-x_low", "installed": true, "size_mb": 21}
    ]
  }
}
```

Der Endpunkt liest nur das Dateisystem; er lädt keine Modelle.

### OpenAI-/Pipecat-Kompatibilität

Pipecat kann seine OpenAI-Services auf den Gateway zeigen lassen:

```python
base_url = "http://server:30000/v1"
api_key = "beliebig"  # der Gateway prüft keine Schlüssel
```

- STT: `model` auf `whisper-1` oder `whisper-small` setzen.
- TTS: `model` auf `tts-1` setzen und `voice` auf eine deutsche Stimmen-ID.
- Die Pipeline schickt pro Satz eine Datei bzw. einen Satz Text; keine Chunks.

## Datenfluss

### Transkription

1. Client sendet Multipart-Body an `:30000`.
2. Gateway erkennt den Pfad, streamt den Body ohne Puffern an `:30003` und
   setzt keinen Job an.
3. Sidecar speichert den Upload in eine temporäre Datei, prüft Größe und Dauer
   per `ffprobe`, lädt bei Bedarf das Whisper-Modell und transkribiert.
4. Antwort geht unverändert zurück; die temporäre Datei wird gelöscht.

### Sprachausgabe

1. Client sendet JSON an `:30000`.
2. Gateway streamt den kleinen Request-Body an `:30003` und öffnet die Antwort
   als Stream.
3. Sidecar teilt den Text in Sätze, synthetisiert den ersten Satz, schreibt PCM
   in den gewünschten Ausgabestrom und liefert die ersten Bytes sofort.
4. Gateway reicht die Bytes chunkweise durch, ohne sie zu sammeln.
5. Bei Client-Disconnect wird der Upstream-Stream geschlossen und ffmpeg beendet.

### Kein Job-Tracking

Audio erscheint nicht in `/monitor/jobs`, wird nicht priorisiert, bekommt keine
Keepalives und keinen Hindsight-Blocker. Es ist kein Chat-Completion-Request.

## Modelle und Download

### Whisper

| ID | Repository | Größe | Lizenz |
|---|---|---|---|
| `whisper-small` | `Systran/faster-whisper-small` | 484 MB | MIT |
| `whisper-medium` | `Systran/faster-whisper-medium` | 1.528 MB | MIT |
| `whisper-large-v3-turbo` | `deepdml/faster-whisper-large-v3-turbo-ct2` | 1.618 MB | MIT |

Alle drei sind mehrsprachig. `small` ist Default und für Telefon-Sätze gedacht;
`medium` und `turbo` liefern bei Akzenten und Fachbegriffen bessere Ergebnisse,
sind auf der CPU aber deutlich langsamer.

**DACH:** Standarddeutsch ist der Default. Deutsche Standardsprache mit
österreichischem oder Schweizer Akzent erkennt Whisper in der Regel; Mundart
ist nicht garantiert. Piper spricht Standarddeutsch, da es keine eigenen
AT/CH-Stimmen gibt.

### Piper

| ID | Repository-Pfad | Größe | Lizenz |
|---|---|---|---|
| `de_DE-thorsten-medium` | `rhasspy/piper-voices` → `de/de_DE/thorsten/medium` | 63 MB | CC0 |
| `de_DE-kerstin-low` | `rhasspy/piper-voices` → `de/de_DE/kerstin/low` | 63 MB | CC0 |
| `de_DE-eva_k-x_low` | `rhasspy/piper-voices` → `de/de_DE/eva_k/x_low` | 21 MB | Dataset-Lizenz, siehe MODEL_CARD |

Gesamter Download: **ca. 3,8 GB**.

### Ablage

```
/mnt/models/audio/
├── whisper/
│   ├── small/               # model.bin, config.json, tokenizer.json, vocabulary.txt
│   ├── medium/
│   └── large-v3-turbo/
└── piper/
    ├── de_DE-thorsten-medium.onnx
    ├── de_DE-thorsten-medium.onnx.json
    ├── de_DE-kerstin-low.onnx
    ├── de_DE-kerstin-low.onnx.json
    ├── de_DE-eva_k-x_low.onnx
    └── de_DE-eva_k-x_low.onnx.json
```

Der Pfad ist über `AUDIO_MODELS_DIR` konfigurierbar. Die Modelle liegen nicht im
Repo und nicht im Home-Verzeichnis.

### Download-Skript

`runtime/audio/download-models.sh` ist ein Wrapper um
`python -m aiserver_audio.download`.

```bash
runtime/audio/download-models.sh --all
runtime/audio/download-models.sh --whisper small,medium
runtime/audio/download-models.sh --voices thorsten,eva
runtime/audio/download-models.sh --check
```

- Ohne Argumente: alle drei Whisper-Modelle und alle drei Stimmen.
- `--check`: zeigt installiert/fehlend und die Größe; Exit 1, wenn etwas fehlt.
- Idempotent: vorhandene Dateien werden geprüft, nicht erneut geladen.
- Prüft vor dem Download mindestens 5 GB freien Platz.
- Nutzt `huggingface_hub.snapshot_download` mit explizitem `local_dir` und
  `allow_patterns`; kein Cache-Umweg über das Home-Verzeichnis.
- Verifiziert nach dem Download die erwarteten Dateien und ihre Größe.
- Bei Abbruch bleiben Teil-Dateien liegen; ein erneuter Lauf setzt fort.
- Kein Download im Request-Pfad. Fehlt ein Modell, antwortet der Server `503`
  mit dem exakten Befehl, der es nachinstalliert.

`run.sh audio-models` ruft das Skript auf. `run.sh status` zeigt zusätzlich, wie
viele Whisper-Modelle und Stimmen installiert sind.

## CPU, Concurrency und Cache

| Ressource | Vorgabe |
|---|---|
| Whisper-Threads | 8 (`AUDIO_THREADS`) |
| Whisper-Compute | `int8`, `device=cpu` |
| Whisper-Cache | 1 Modell; Wechsel entlädt das vorherige |
| STT-Parallelität | 1 |
| STT-Warteschlange | 8, danach `429` |
| TTS-Parallelität | 2 |
| TTS-Threads | 2 pro Synthese |
| TTS-Warteschlange | 8, danach `429` |
| Vorladen | `whisper-small` beim Start, Fehler blockieren den Start nicht |
| Priorität | `nice 5`; Chat und Gateway bleiben bevorzugt |
| VAD | faster-whisper `vad_filter=True` |
| Decoding | `beam_size=1`, `condition_on_previous_text=False` |

Die Piper-Stimme wird mit einer eigenen ONNX-Runtime-Session geladen
(`intra_op_num_threads=2`, `inter_op_num_threads=1`), damit zwei parallele
Synthesen das CPU-Budget nicht unbegrenzt überschreiten.

Bei gleichzeitiger STT- und TTS-Last können kurzzeitig mehr als 8 Threads
anliegen; Whisper bleibt der Hauptverbraucher, TTS ist kurz und leicht. Die
STT-Verarbeitung ist bewusst seriell, damit ein Anruf nicht durch parallele
Whisper-Läufe ausgebremst wird.

**Speicherbedarf (Richtwerte):**

| Zustand | RAM |
|---|---|
| Sidecar idle | ca. 0,3 GB |
| `whisper-small` geladen | ca. 1,0 GB |
| `whisper-medium` geladen | ca. 2,5 GB |
| `whisper-large-v3-turbo` geladen | ca. 2,7 GB |
| drei Piper-Stimmen | ca. 0,2 GB |

**Ladezeiten:** Ein Modellwechsel kostet einige Sekunden. Das passiert nur beim
ersten Request eines anderen Modells, nicht bei jedem Satz. Der Default
`whisper-small` ist beim Start vorgeladen.

## Streaming-Implementierung

### Gateway

- Audio-Pfade werden **vor** dem Lesen des Request-Bodys erkannt.
- Uploads werden als async Iterator an httpx weitergereicht; der Gateway hält
  keine 100-MB-Datei im Speicher.
- Das 100-MB-Limit wird anhand `Content-Length` und zusätzlich beim Durchzählen
  des Streams geprüft. Überschreitung → `413`, Upstream wird abgebrochen.
- TTS-Antworten laufen über `httpx.AsyncClient.stream` und
  `StreamingResponse`. Status und Content-Type kommen vom Sidecar.
- Fehler vor dem Antwortstart (4xx/5xx) werden gepuffert gelesen und als
  JSON/Text zurückgegeben.
- Verbindungsfehler → `502`, Timeout → `504`.
- Der Upstream-Stream wird auch bei Client-Disconnect geschlossen.

### Sidecar

- `pcm`: Piper-PCM wird auf 24 kHz resampelt (ffmpeg) und als rohe Bytes
  ausgeliefert.
- `wav`: ein WAV-Header mit Platzhaltergrößen (`0xFFFFFFFF`) gefolgt von den
  PCM-Chunks; die Probe-Rate ist die native Rate der Stimme.
- `mp3`/`opus`: ffmpeg liest PCM aus einer Pipe und schreibt in eine zweite Pipe;
  der Sidecar liefert die Ausgabe fortlaufend.
- `piper-tts` 1.8 liefert mit `PiperVoice.synthesize()` bereits einen
  `AudioChunk` pro Satz. Diese Chunks werden direkt weitergereicht; ein eigener
  Satz-Splitter ist nur für die Steuerung von `speed` und die Protokollierung
  nötig.
- Bei mehreren Sätzen wird ffmpeg nur einmal gestartet; die Sätze werden
  nacheinander in die Eingabe-Pipe geschrieben.
- Client-Disconnect: Writer-Task abbrechen, ffmpeg `SIGTERM`, Pipes schließen,
  temporäre Dateien löschen.
- `speed` wird vor der Synthese als `length_scale = 1 / speed` gesetzt.

## Grenzen und Timeouts

| Grenze | Wert | Verhalten |
|---|---|---|
| Upload | 100 MB | `413 audio_too_large` |
| Audiodauer | 60 Minuten | `413 audio_too_long` |
| TTS-Text | 5.000 Zeichen | `400 invalid_request` |
| STT-Timeout | 30 Minuten | `504 timeout` |
| TTS-Timeout | 2 Minuten | `504 timeout` |
| STT-Queue | 8 | `429 queue_full`, `Retry-After: 5` |
| TTS-Queue | 8 | `429 queue_full`, `Retry-After: 5` |

Die Audiodauer wird mit `ffprobe` geprüft. Ist die Datei kein lesbares Audio,
antwortet der Server `400 invalid_audio`.

Die Audiodauer ist doppelt so groß wie der STT-Timeout: Eine 60-Minuten-
Datei muss innerhalb von 30 Minuten verarbeitet sein. Bei `small` ist das auf
dieser CPU realistisch, bei `medium`/`turbo` kann eine sehr lange Datei in den
Timeout laufen; dann antwortet der Server `504`. Für Telefon-Sätze ist das
irrelevant.

## Fehlerformat

Alle Fehler des Audio-Sidecars und des Gateways für Audio-Pfade:

```json
{
  "error": {
    "message": "Modell 'whisper-large-v3-turbo' ist nicht installiert.",
    "type": "invalid_request_error",
    "code": "model_not_installed"
  }
}
```

| HTTP | Code | Ursache |
|---|---|---|
| 400 | `invalid_request` | fehlendes Feld, ungültiger Wert, `translate` |
| 400 | `invalid_audio` | Datei nicht lesbar, kein Audio |
| 404 | `model_not_found` | unbekannte Modell-ID oder Stimme |
| 413 | `audio_too_large` | Upload > 100 MB |
| 413 | `audio_too_long` | Audio > 60 Minuten |
| 429 | `queue_full` | Warteschlange voll |
| 502 | `upstream_unavailable` | Audio-Sidecar nicht erreichbar |
| 503 | `model_not_installed` | Modell bekannt, aber nicht heruntergeladen |
| 504 | `timeout` | STT/TTS-Timeout |

Bei `503` enthält `message` den konkreten Download-Befehl, z. B.
`runtime/audio/download-models.sh --whisper medium`.

## Konfiguration

### Gateway (`config.py`, TOML, Env)

| Feld | Default | Bedeutung |
|---|---|---|
| `audio_base` | `http://127.0.0.1:30003` | Sidecar |
| `audio_timeout_s` | `1800` | Lesetimeout für Audio |
| `audio_max_upload_mb` | `100` | Upload-Limit |

Env-Präfix wie bisher: `AISERVER_MONITOR_AUDIO_BASE`,
`AISERVER_MONITOR_AUDIO_TIMEOUT_S`, `AISERVER_MONITOR_AUDIO_MAX_UPLOAD_MB`.

### Sidecar (Env)

| Variable | Default | Bedeutung |
|---|---|---|
| `AUDIO_HOST` | `127.0.0.1` | Bind-Adresse |
| `AUDIO_PORT` | `30003` | Port |
| `AUDIO_MODELS_DIR` | `/mnt/models/audio` | Modellablage |
| `AUDIO_THREADS` | `8` | Whisper-Threads |
| `AUDIO_PRELOAD` | `whisper-small` | Vorladen beim Start; leer = aus |
| `AUDIO_MODEL_CACHE` | `1` | Anzahl Whisper-Modelle im RAM |
| `AUDIO_MAX_QUEUE` | `8` | Wartende pro Art |
| `AUDIO_STT_TIMEOUT` | `1800` | Sekunden |
| `AUDIO_TTS_TIMEOUT` | `120` | Sekunden |
| `AUDIO_MAX_UPLOAD_MB` | `100` | Upload-Limit |
| `AUDIO_LOG` | `/tmp/audio_server.log` | Logdatei (Startskript) |

Die Modelle selbst werden nicht über Env-Pfade pro Request gewählt; es gilt die
Whitelist in `catalog.py`.

## Lebenszyklus

### Skripte

- `runtime/audio/setup.sh`: prüft `ffmpeg` und `.venv`, installiert
  `aiserver-monitor/requirements-audio.txt` (`faster-whisper==1.2.1`,
  `piper-tts==1.8.0`, `python-multipart==0.0.20`), prüft die Imports.
  Idempotent.
- `runtime/audio/download-models.sh`: siehe oben.
- `runtime/audio/start-audio.sh`: startet Uvicorn auf `127.0.0.1:30003`,
  PID-Datei `runtime/audio/audio.pid`, Log `/tmp/audio_server.log`, wartet auf
  `/healthz`, ist idempotent. Setzt `CUDA_VISIBLE_DEVICES=""`,
  `OMP_NUM_THREADS=8`, `AUDIO_MODELS_DIR`.
- `runtime/audio/stop-audio.sh`: liest nur die PID-Datei, `TERM`, 20 s warten,
  `KILL`, PID-Datei entfernen.
- `runtime/audio/selftest.sh`: optionaler echter Test (Piper → WAV → Whisper),
  nur wenn Modelle installiert sind; nicht Teil der Unit-Suite.

### `run.sh`

- `start`: startet Embedding-Sidecar, dann Audio-Sidecar, dann Gateway. Fehler
  beim Audio-Start sind eine Warnung, kein Abbruch.
- `stop`: stoppt Gateway, dann Audio, dann Embedding.
- `status`: prüft Gateway, Embedding-Sidecar und Audio-`/healthz`; zeigt die
  Anzahl installierter Whisper-Modelle und Stimmen.
- `audio-setup`: ruft `setup.sh`.
- `audio-models`: ruft `download-models.sh` mit den übergebenen Argumenten.

### systemd

`aiserver-monitor.service` bekommt:

```ini
Environment=AISERVER_MONITOR_AUDIO_BASE=http://127.0.0.1:30003
ExecStartPre=-/home/user/small_qwen/runtime/audio/start-audio.sh
ExecStopPost=-/home/user/small_qwen/runtime/audio/stop-audio.sh
```

Damit startet und stoppt der Audio-Sidecar mit dem Gateway, ohne dessen
Hauptprozess zu blockieren.

## Sicherheit und Datenschutz

- Der Sidecar bindet nur an `127.0.0.1`; der öffentliche Zugriff läuft über den
  bestehenden Gateway.
- Modell-IDs und Stimmen kommen aus einer Whitelist; Request-Daten werden nie
  als Dateipfad verwendet.
- Downloads nur aus den im Manifest hinterlegten Repositories.
- ffmpeg wird mit festen Argumentlisten gestartet, nie über eine Shell.
- Uploads werden nach der Verarbeitung gelöscht; temporäre Dateien liegen in
  `tempfile`-Verzeichnissen.
- **Keine Audioinhalte und keine Transkripte im Log.** Geloggt werden nur
  Request-Art, Modell, Dauer, Audio-Sekunden, RTF, Queue-Wartezeit und Status.
- Keine Authentifizierung: der Sidecar ist Teil der lokalen Vertrauensgrenze,
  genau wie SGLang und der Embedding-Ollama.

## Tests

### Gateway (`aiserver-monitor/tests`)

- Transkriptions-Multipart geht byte-genau an `:30003`, inklusive Query; kein Job
  in der Registry.
- TTS-Request geht an `:30003`; die Antwort wird chunkweise weitergereicht und
  nicht gepuffert.
- `GET /v1/audio/models` wird durchgereicht.
- Unbekannter Audio-Pfad → `404`; falsche Methode → `405`.
- Audio-Sidecar down → `502`; Timeout → `504`.
- Upload über Limit → `413`, ohne Upstream-Aufruf.
- Chat- und Embedding-Routing bleiben unverändert.

### Sidecar

- Katalog: kanonische IDs, Aliase, `whisper-1`, `tts-1`, unbekannt → `404`.
- Installationsstatus aus dem Dateisystem; `503` mit Download-Befehl.
- Default `language=de`; `auto`; `translate` → `400`.
- STT-Queue voll → `429`; Freigabe nach Abschluss.
- TTS: Satz-Splitting, `speed`-Abbildung, alle vier Formate, korrekte
  Content-Types, erster Chunk vor dem letzten Satz.
- Upload-Limit, Dauer-Limit (`ffprobe`-Fake), ungültiges Audio.
- Download: Manifest, `--check`, idempotentes Überspringen, zu wenig Platz,
  fehlende Dateien; `snapshot_download` wird gemockt.
- Health und Models-Endpunkt ohne geladene Modelle.

Die echten Modelle werden in Unit-Tests **nicht** geladen. STT- und TTS-Engines
werden über Interfaces ersetzt; die Tests prüfen Verdrahtung, Validierung,
Queues und Streaming, nicht die Modellqualität.

### Manuell / Integration

`runtime/audio/selftest.sh`:

1. Piper synthetisiert „Hallo, das ist ein Test.“ als WAV.
2. Whisper `small` transkribiert die Datei mit `language=de`.
3. Der Test prüft, dass „Hallo“ im Ergebnis vorkommt.
4. Laufzeit und RTF werden ausgegeben.

Dieser Test braucht die heruntergeladenen Modelle und läuft nicht in der
automatischen Suite.

### Regression

Der bestehende Befehl

```bash
cd aiserver-monitor
../.venv/bin/python -m unittest discover -s tests -q
```

muss grün bleiben. Die neuen Tests laufen in derselben Discovery.

## Akzeptanzkriterien

1. `runtime/audio/setup.sh` installiert die Abhängigkeiten ohne Eingriff in den
   Gateway-Code.
2. `runtime/audio/download-models.sh --all` lädt die drei Whisper-Modelle und
   drei Stimmen nach `/mnt/models/audio` und `--check` meldet sie als vorhanden.
3. `run.sh restart` startet Gateway, Embedding- und Audio-Sidecar; `status`
   zeigt alle drei.
4. `POST /v1/audio/transcriptions` über `:30000` liefert für deutsche Sprache
   Text; `language` default ist `de`.
5. `POST /v1/audio/speech` über `:30000` liefert hörbares Deutsch; die ersten
   Audiodaten kommen vor dem Ende der kompletten Synthese.
6. `GET /v1/audio/models` zeigt installierte und fehlende Modelle korrekt.
7. Ein fehlendes Modell liefert `503` mit Download-Befehl; ein unbekanntes `404`.
8. Audio erscheint nicht in `/monitor/jobs`; Chat und Embeddings funktionieren
   unverändert weiter.
9. Bei gestopptem Audio-Sidecar liefert Audio `502`, der Chat bleibt erreichbar.
10. Die vollständige Testsuite ist grün.
11. README (DE/EN) und `config.example.toml` dokumentieren Endpunkte, Modelle,
    Downloads, Grenzen und `run.sh`-Befehle.

## Risiken

| Risiko | Umgang |
|---|---|
| `medium`/`turbo` sind auf der CPU zu langsam für Telefonie | Default `small`; Laufzeit im Selftest messen und dokumentieren |
| Whisper verpasst leise oder abgehackte Sätze | Pipecat/VAD schickt vollständige Sätze; `vad_filter` im Sidecar |
| Piper-Stimmen klingen robotisch | drei Stimmen zur Auswahl; Wechsel ohne Neustart |
| Keine AT/CH-Stimmen | Standarddeutsch; Akzente werden in der Regel erkannt, Mundart nicht garantiert |
| Streaming-WAV mit Platzhaltergröße wird von manchen Playern nicht akzeptiert | `pcm` und `mp3` als Alternativen; WAV nur für kompatible Clients |
| CPU-Konkurrenz mit Embeddings | `nice 5`, serielle STT-Verarbeitung, Queue-Limits |
| Modellwechsel kostet Sekunden | Default vorladen; Cache 1; Wechsel im Log sichtbar |
| Große Uploads belasten den Gateway | Streaming-Upload, 100-MB-Limit, keine Pufferung |
| Abhängigkeiten verändern die bestehende `.venv` | exakte Pins, Setup-Skript, Import-Check; Audio bleibt eigener Prozess |

## Offene Punkte

Keine. Alle fachlichen Entscheidungen sind getroffen; die Umsetzung kann in
kleine, testgetriebene Schritte zerlegt werden.
