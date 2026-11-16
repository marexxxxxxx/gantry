# Qwen3-TTS als GPU-TTS über `/v1/audio/speech`

## Ziel

Qwen3-TTS-12Hz-1.7B-CustomVoice lokal installieren und über den bestehenden
Audio-Endpunkt `POST /v1/audio/speech` anbietbar machen. Das Modell läuft auf
der GPU, aber erst nachdem das Chat-Backend entladen wurde.

## Warum

Der Aufrufer schickt bereits `"model": "Qwen3-TTS"` an `/v1/audio/speech`.
Der Audio-Sidecar kennt bisher nur Piper und Chatterbox und antwortet deshalb
mit `404`. Das Modell liegt mit ca. 4,3 GB bereits unter
`/mnt/models/audio/experiments/qwen`; es fehlen die Laufzeitumgebung und die
Anbindung.

## Nicht-Ziele

- Kein CPU-Fallback. Reicht der VRAM nicht, gibt es einen klaren Fehler.
- Kein Voice-Cloning und kein Voice-Design; genutzt wird CustomVoice mit den
  offiziellen Sprechern.
- Kein Cloud-Aufruf und kein Partner-Endpunkt.
- Keine Änderung an Piper, Chatterbox oder Whisper.

## Randbedingungen

- GPU: RTX 5070 Ti mit 16 GB. Das Chat-Backend belegt aktuell ca. 15 GB.
- Qwen3-TTS braucht eine eigene Python-3.12-Umgebung (`qwen-tts`), getrennt
  vom Gateway und vom Chatterbox-Venv.
- Der Audio-Sidecar bleibt der einzige Einstiegspunkt für `/v1/audio/*`.
- Der Aufrufer steuert die Reihenfolge über `/v1/unload` und `/v1/load`.
- Vorhandene, nicht committete Änderungen im Repo dürfen nicht überschrieben
  werden.

## Ansätze

### A – Eigener TTS-Worker auf Port 30005 (gewählt)

Der Worker ist ein separater FastAPI-Prozess mit eigener CUDA-Umgebung. Der
Audio-Sidecar leitet Qwen3-TTS-Anfragen an ihn weiter. Vorteile: klare
Trennung, GPU-Fehler treffen nur TTS, Piper/Chatterbox/STT bleiben unberührt.
Nachteil: ein Prozess und ein Port mehr.

### B – Qwen3-TTS direkt in den Audio-Sidecar

Weniger Prozesse, aber der Audio-Sidecar müsste CUDA-Torch laden und würde
bei einem GPU-Modell den gesamten Audio-Dienst blockieren. Verworfen.

## Architektur

### Worker `aiserver_qwen_tts`

Neues Paket `aiserver-monitor/aiserver_qwen_tts/` mit:

- `config.py`: `QwenTTSSettings` (`QWEN_TTS_HOST`, `QWEN_TTS_PORT=30005`,
  `QWEN_TTS_MODEL_DIR=/mnt/models/audio/experiments/qwen`,
  `QWEN_TTS_DEVICE=cuda:0`, `QWEN_TTS_DTYPE=bfloat16`,
  `QWEN_TTS_DEFAULT_LANGUAGE=German`).
- `engine.py`: lädt `Qwen3TTSModel.from_pretrained(...)` lazy beim ersten
  Bedarf; hält einen Lock; ruft
  `generate_custom_voice(text=..., language=..., speaker=..., instruct=...)`
  auf und liefert int16-PCM mit 24 kHz. CUDA-OOM wird als eigener Fehler
  gemeldet, niemals als CPU-Fallback.
- `app.py`:
  - `GET /healthz` → `{"status": "ok", "model_loaded": bool}`
  - `POST /load` → lädt das Modell und antwortet erst, wenn es bereit ist
  - `POST /unload` → gibt Modell und CUDA-Cache frei
  - `POST /synthesize` → `{text, voice, language, instruct?, speed?}`,
    Antwort `audio/pcm` mit `X-Sample-Rate: 24000`.
- `__main__.py`: startet den Server auf `QWEN_TTS_HOST:QWEN_TTS_PORT`.

Das Modell wird mit `torch.bfloat16` auf `cuda:0` geladen. FlashAttention ist
optional und nicht Voraussetzung.

### Audio-Sidecar

- Neuer Client `aiserver_audio/qwen_tts.py` analog zu `ChatterboxClient`.
- `AudioSettings` bekommt `qwen_tts_base=http://127.0.0.1:30005` und
  `qwen_tts_timeout_s` (Default 600).
- `SpeechRequest` bekommt optionale Felder `language` (Default `German`) und
  `instruct`.
- `sprache()` erkennt `model.casefold()` in
  `{"qwen3-tts", "qwen3-tts-1.7b", "qwen3-tts-12hz-1.7b-customvoice"}` und
  nutzt den Qwen-Client. `voice` wird ohne Beachtung der Groß-/Kleinschreibung
  auf die offiziellen Sprecher abgebildet; leer, `default` oder der
  Piper-Default werden zu `Vivian`.
- `GET /v1/audio/models` listet `Qwen3-TTS` mit `installed: true`, wenn der
  Modellordner existiert, plus die offiziellen Sprecher als `voices`.
- Der bestehende PCM-Streaming-/Encoder-Pfad bleibt unverändert.

### Lifecycle

`ModelLifecycle` bekommt ein neues Profil:

- `ModelProfile("Qwen3-TTS", "qwen-tts", "tts")`

Regeln:

- `/v1/load` mit `Qwen3-TTS` startet den Worker über
  `runtime/qwen-tts/start-qwen-tts.sh`, wartet auf `/healthz` und ruft
  `POST /load` auf.
- Läuft noch ein Chat-Backend, antwortet `/v1/load` mit `409` und lädt nichts.
- Chat und Qwen3-TTS dürfen nicht gleichzeitig angefordert werden (`409`).
- `/v1/unload` mit `Qwen3-TTS` ruft `POST /unload` auf und stoppt danach den
  Worker-Prozess, damit auch der CUDA-Kontext sicher freigegeben wird.
- `/v1/unload` ohne Namen stoppt Chat, entlädt Embeddings und stoppt den
  Qwen3-TTS-Worker.
- `/v1/status` meldet das Profil nur als geladen, wenn `/healthz`
  `model_loaded: true` liefert.

Neue Gateway-Konfigurationsfelder:

- `qwen_tts_base=http://127.0.0.1:30005`
- `qwen_tts_start_script=runtime/qwen-tts/start-qwen-tts.sh`
- `qwen_tts_stop_script=runtime/qwen-tts/stop-qwen-tts.sh`
- `qwen_tts_pidfile=runtime/qwen-tts/qwen-tts.pid`
- `qwen_tts_health_path=/healthz`
- `qwen_tts_load_timeout_s=900`
- `qwen_tts_unload_timeout_s=60`

### Skripte und Umgebung

`runtime/qwen-tts/`:

- `setup.sh`: legt `venv` an, installiert CUDA-Torch (cu128, passend zur
  RTX 5070 Ti) und `qwen-tts==0.1.1`.
- `requirements.txt`: pinnt `qwen-tts`, `transformers==4.57.3`,
  `accelerate==1.12.0`.
- `download-models.sh`: nutzt den vorhandenen Ordner
  `/mnt/models/audio/experiments/qwen`, wenn er vollständig ist; sonst
  `hf download Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice --local-dir ...`.
- `start-qwen-tts.sh`: idempotent, setzt `CUDA_VISIBLE_DEVICES=0`,
  `PYTHONPATH=aiserver-monitor`, schreibt PID-Datei und Log, wartet auf
  `/healthz`. Startet das Modell **nicht** vorab.
- `stop-qwen-tts.sh`: beendet nur den Worker anhand der PID-Datei.

`run.sh` bekommt `start-qwen-tts`, `stop-qwen-tts`, `status-qwen-tts`,
`qwen-tts-setup` und `qwen-tts-models`. Der Standardstart startet Qwen3-TTS
**nicht** mit, damit die GPU frei bleibt.

## Ablauf der Pipeline

1. `POST /v1/unload` → Chat und alte Modelle entladen.
2. `POST /v1/load` mit `{"model": "Qwen3-TTS"}` → Worker starten, Modell auf
   die GPU laden.
3. `POST /v1/audio/speech` mit `{"model": "Qwen3-TTS", ...}`.
4. `POST /v1/unload` → Worker stoppen, VRAM freigeben.
5. Nächster Schritt.

## Fehlerfälle

- Chat läuft noch → `409`, nichts wird gestartet.
- Kein oder zu wenig VRAM → `503` mit klarer Meldung, kein CPU-Fallback.
- Worker startet nicht → `502`.
- Laden überschreitet das Zeitlimit → `504`.
- Unbekannter Sprecher → `400`.
- Unbekanntes Modell → `404`, nichts wird geladen.

## Tests

- Worker: Lazy-Load, `/load`, `/unload`, PCM-Ausgabe, OOM-Meldung,
  Sprechervalidierung (Engine gefälscht, kein echtes Modell im Unit-Test).
- Audio-Sidecar: Modellauflösung, Voice-Default, Client-Fehler 502/504,
  `models_payload`.
- Lifecycle: Profil im Katalog, `/v1/load` startet Worker und lädt, Chat
  läuft noch → 409, `/v1/unload` stoppt Worker, `/v1/status` meldet korrekt.
- Gateway-Routen: `Qwen3-TTS` über `/v1/load` und `/v1/unload`.
- Skripte: Syntax, Ausführbarkeit, isolierte Ports in `test_run_sh.sh`.
- Voller Testlauf muss grün bleiben (aktuell 503 Tests).

## Annahmen

- Der vorhandene Modellordner ist vollständig und wird von `qwen-tts`
  akzeptiert. Falls nicht, wird erneut vom offiziellen HF-Repo geladen.
- Die RTX 5070 Ti braucht CUDA-12.8-Wheels; das Setup prüft die tatsächliche
  Torch-/CUDA-Kombination und dokumentiert sie.
- Der Aufrufer nennt das Modell exakt `Qwen3-TTS`; der Audio-Sidecar matcht
  zusätzlich case-insensitiv.
