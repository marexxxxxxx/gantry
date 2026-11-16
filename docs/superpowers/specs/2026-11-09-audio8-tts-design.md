# Audio8-TTS-Realistic-Air als zweites GPU-TTS über `/v1/audio/speech`

## Ziel

Audio8-TTS-Realistic-Air lokal installieren und über den bestehenden
Audio-Endpunkt `POST /v1/audio/speech` anbietbar machen. Das Modell läuft wie
Qwen3-TTS auf der GPU, aber erst nachdem das Chat-Backend entladen wurde.
Zusätzlich wird es über `/v1/load`, `/v1/unload` und `/v1/status` des Gateways
verwaltet.

Die Stimme ist fest hinterlegt, männlich und möglichst natürlich. Audio8 hat
keine benannten Sprecher; die Stimme entsteht deshalb aus einer festen
Referenzaufnahme plus exakt passendem Transkript.

## Warum

Der Aufrufer schickt `"model": "Audio8-Realistic-Air"` an
`/v1/audio/speech`. Der Audio-Sidecar kennt bisher nur Piper, Chatterbox und
Qwen3-TTS und antwortet deshalb mit `404`. Audio8 ist mit 0,6B kompakt, spricht
23 Sprachen inklusive Deutsch, liefert 44,1 kHz und ist für Zero-Shot-
Stimmklonen ausgelegt.

## Nicht-Ziele

- Kein Voice-Cloning pro Request und kein Hochladen von Referenzaudio durch
  den Aufrufer. Es gibt genau eine fest konfigurierte Stimme.
- Kein CPU-Fallback. Reicht der VRAM nicht, gibt es einen klaren Fehler.
- Kein ONNX-Export, kein SGLang-Omni-Server, kein Cloud- oder Partner-Aufruf.
- Keine Änderung an Piper, Chatterbox oder Whisper.
- `speed` und `instruct` bleiben aus Kompatibilitätsgründen im Request
  akzeptiert, haben bei Audio8 aber keine Wirkung.
- Chat und TTS laufen nicht gleichzeitig; zwei verschiedene TTS-Backends
  ebenfalls nicht.

## Randbedingungen

- GPU: RTX 5070 Ti mit 16 GB. Das Chat-Backend belegt aktuell ca. 15 GB.
- Audio8 braucht eine eigene Python-3.12-Umgebung mit CUDA-Torch, getrennt vom
  Gateway, vom Audio-Sidecar und vom Chatterbox-Venv.
- Das Modell benötigt `transformers>=4.57,<5` und `trust_remote_code=True`.
- Der Audio-Sidecar bleibt der einzige Einstiegspunkt für `/v1/audio/*`.
- Der Aufrufer steuert die Reihenfolge über `/v1/unload` und `/v1/load`.
- Vorhandene, nicht committete Änderungen im Repo dürfen nicht überschrieben
  werden. Der Audio8-Ausbau baut auf dem uncommitteten Qwen3-TTS-Stand auf.
- Das Modell liegt unter `/mnt/models/audio/audio8`, die Referenzstimme unter
  `/mnt/models/audio/audio8/reference/`.

## Ansätze

### A – Eigener TTS-Worker auf Port 30006 (gewählt)

Separater FastAPI-Prozess mit eigener CUDA-Umgebung, analog zu Qwen3-TTS. Der
Audio-Sidecar leitet Audio8-Anfragen an ihn weiter. Vorteile: klare Trennung,
GPU-Fehler treffen nur TTS, Piper/Chatterbox/STT bleiben unberührt, und die
bestehende Satz-für-Satz-Streaming-Pipeline kann unverändert genutzt werden.
Nachteil: ein Prozess und ein Port mehr.

### B – SGLang-Omni-Server

SGLang Omni bietet Paged Attention, dynamisches Batching und eine
OpenAI-kompatible Speech-API. Für zwei gleichzeitige TTS-Slots bringt das
keinen Vorteil, kostet aber einen weiteren schweren Serving-Stack, eigene
Versionskopplung und eine andere Voice-Cloning-Schnittstelle. Verworfen.

### C – Audio8 direkt im Audio-Sidecar laden

Weniger Prozesse, aber der CPU-Audio-Dienst würde CUDA-Torch laden und bei
einem GPU-Modell den gesamten Audio-Dienst blockieren. Verworfen.

### D – Audio8 ohne Referenz betreiben

Am einfachsten, garantiert aber keine männliche Stimme. Verworfen, weil die
Anforderung „möglichst natürliche männliche Stimme“ sonst nicht erfüllt ist.

### Referenzstimme

Audio8 braucht für eine feste Stimme eine Referenzaufnahme und deren exaktes
Transkript. Drei Quellen wurden geprüft:

1. **Echte eigene Aufnahme (beste Qualität).** Der Aufrufer kann später eine
   Datei hinterlegen; die Pfade sind konfigurierbar. Ohne Datei nicht
   verfügbar.
2. **Öffentliche deutsche Männerstimme.** Die Audio8-Demos enthalten nur eine
   weibliche deutsche Referenz („Susanne Stichler“, Tagesschau-Kontext) und
   keine geeignete männliche Aufnahme mit klarer Lizenz. Verworfen.
3. **Lokal mit Piper erzeugte männliche Referenz (Default).** Die vorhandene
   Stimme `de_DE-thorsten-medium` ist männlich, offline verfügbar und
   lizenzseitig unproblematisch. Das Skript erzeugt daraus eine kurze
   Referenzaufnahme und schreibt das exakte Transkript daneben. Audio8 klont
   diese Stimme. Die Natürlichkeit ist dadurch an Piper gekoppelt; für mehr
   Natürlichkeit genügt später der Austausch der Referenzdatei über die
   Konfiguration.

Gewählt wird Variante 3 als funktionierender Default mit einfacher
Austauschmöglichkeit. Das ist eine bewusste Abwägung: lieber sofort eine
männliche, reproduzierbare Stimme als eine unbestimmte Zufallsstimme.

## Architektur

### Worker `aiserver_audio8_tts`

Neues Paket `aiserver-monitor/aiserver_audio8_tts/`. Das HTTP-Gerüst wird
nicht dupliziert: Die bestehenden Routen `GET /healthz`, `POST /load` und
`POST /unload` werden aus dem Qwen3-TTS-Worker in einen gemeinsamen Baustein
`aiserver-monitor/aiserver_tts_worker.py` gezogen. Qwen3-TTS und Audio8
registrieren damit dieselbe Lifecycle-Implementierung; Audio8 ergänzt nur
`POST /synthesize` und seinen Fehler-Handler.

- `config.py`: `Audio8Settings`
  (`AUDIO8_HOST=127.0.0.1`, `AUDIO8_PORT=30006`,
  `AUDIO8_MODEL_DIR=/mnt/models/audio/audio8`,
  `AUDIO8_DEVICE=cuda:0`, `AUDIO8_DTYPE=bfloat16`,
  `AUDIO8_REFERENCE_AUDIO=/mnt/models/audio/audio8/reference/de_male.wav`,
  `AUDIO8_REFERENCE_TEXT=/mnt/models/audio/audio8/reference/de_male.txt`,
  `AUDIO8_DEFAULT_LANGUAGE=German`, `AUDIO8_MAX_NEW_TOKENS=1024`,
  `AUDIO8_RETRY_MAX_NEW_TOKENS=2000`, `AUDIO8_TEMPERATURE=0.8`,
  `AUDIO8_TOP_P=0.95`, `AUDIO8_TOP_K=50`, `AUDIO8_SEED=42`).
- `engine.py`: lädt Prozessor und Modell lazy beim ersten Bedarf, hält einen
  Lock und liefert int16-PCM mit der Codec-Samplerate.
  - Laden:
    `AutoProcessor.from_pretrained(model_dir, trust_remote_code=True)` und
    `AutoModel.from_pretrained(model_dir, trust_remote_code=True, dtype=torch.bfloat16).eval().to(device)`.
  - Vor dem Erfolg von `/load` werden Referenzaudio und Transkript geprüft
    (existiert, nicht leer, Referenzaudio lesbar) und der Codec mit
    `model.load_codec()` vorgeladen. `model_loaded: true` bedeutet damit
    „Modell, Codec und feste Stimme sind einsatzbereit“; der erste
    Syntheseaufruf kann nicht mehr an einem lazy geladenen Codec scheitern.
  - Synthese: Referenzdatei und Transkript aus den Settings lesen;
    `processor(text=[text], reference_audio=[pfad], reference_text=[transkript], return_tensors="pt")`;
    `model.generate(..., max_new_tokens=1024, temperature=0.8, top_p=0.95,
    top_k=50, do_sample=True, return_dict_in_generate=True)`;
    `model.decode_audio(output.codes)`.
  - Ist `output.finished` falsch, wird einmal mit 2000 Tokens wiederholt.
    Bleibt es unvollständig, wird das vorhandene Audio trotzdem
    zurückgegeben und geloggt.
  - CUDA-OOM wird als `503 vram_exhausted` gemeldet, niemals als
    CPU-Fallback. Fehlende Referenzdatei wird als `503 reference_missing`,
    eine unlesbare als `503 reference_invalid` gemeldet.
- `app.py`: `create_audio8_tts_app(...)` registriert das gemeinsame
  Lifecycle-Gerüst (`/healthz`, `/load`, `/unload`) und ergänzt nur
  `POST /synthesize` (`{text, voice?, language?}`; `voice` akzeptiert nur
  `de_male`, `default`, `audio8` und den Piper-Default; Antwort `audio/pcm`
  mit `X-Sample-Rate: 44100`).
- `errors.py`, `__main__.py`, `__init__.py`.

### Audio-Sidecar

- Neuer Client `aiserver_audio/audio8.py` mit `Audio8Voice(id, voice)` und
  `Audio8Client` analog zu `QwenTTSClient`. Der Client sendet
  `{text, voice, language}` an `<audio8_base>/synthesize`, bildet Timeout auf
  `504` und Verbindungsfehler auf `502` ab. Bei einem Upstream-Fehler wird
  das JSON-Feld `error.code`/`error.message` übernommen, damit
  `vram_exhausted`, `reference_missing` und `reference_invalid` beim
  Aufrufer ankommen; fehlt das JSON, bleibt es bei `upstream_error`. Der
  Client liefert `AudioChunk` mit 44.100 Hz als Default.
- `errors.py` ergänzt `upstream_error`, `vram_exhausted`, `reference_missing`
  und `reference_invalid` in `_SERVER_CODES`, damit diese Fehler als
  `server_error` und nicht als `invalid_request_error` klassifiziert werden.
- `AudioSettings` bekommt `audio8_base=http://127.0.0.1:30006`,
  `audio8_timeout_s=600` und `audio8_model_dir=/mnt/models/audio/audio8`
  (`AUDIO_AUDIO8_BASE`, `AUDIO_AUDIO8_TIMEOUT`, `AUDIO_AUDIO8_MODEL_DIR`,
  mit denselben Validierungen wie die bestehenden Bases/Timeouts). Der
  Installationsstatus und der Dispatch nutzen dieses Verzeichnis, nicht
  `${AUDIO_MODELS_DIR}/audio8`, damit Sidecar und Worker dieselbe Quelle
  prüfen. `AUDIO8_MODEL_DIR` des Workers und `AUDIO_AUDIO8_MODEL_DIR` des
  Sidecars müssen auf denselben Pfad zeigen; beide Defaults tun das.
- `SpeechRequest` braucht keine neuen Pflichtfelder. `voice` wird auf die
  feste Audio8-Stimme abgebildet: leer, `default`, `audio8`, `de_male` und
  der Piper-Default werden akzeptiert; alles andere ergibt `400`.
  `language` wird durchgereicht und geloggt, beeinflusst die Synthese aber
  nicht. `instruct` wird ignoriert.
- `POST /v1/audio/speech` erkennt
  `{"audio8", "audio8-realistic-air", "audio8-tts-realistic-air",
  "audio8-tts"}`. Bei fehlender Installation gibt es `503
  model_not_installed`; der Hinweis nennt
  `runtime/audio8/download-models.sh`.
- `GET /v1/audio/models` listet `Audio8-Realistic-Air` mit
  `installed`-Status und Größe sowie einen `audio8`-Block mit der festen
  Stimme `de_male`.
- Der bestehende PCM-Vertrag bleibt unverändert: `response_format=pcm` ist
  immer mono 24 kHz. `stream_audio` reicht nur bei einer Quellrate von
  24 kHz direkt durch und resampelt sonst mit ffmpeg auf 24 kHz. Audio8
  liefert intern 44,1 kHz; WAV, MP3 und Opus behalten 44,1 kHz, rohes PCM
  wird auf 24 kHz gebracht. Das ist eine bewusste Beibehaltung des
  bestehenden Verhaltens für Piper und Qwen3-TTS und keine Änderung am
  gemeinsamen Encoder.

### Katalog

`catalog.py` bekommt `Audio8Info` und:

- `AUDIO8_MODEL = Audio8Info("Audio8-Realistic-Air",
  ("audio8", "audio8-realistic-air", "audio8-tts-realistic-air",
  "audio8-tts"), 1600, "audio8", required_files=(...),
  voices=("de_male",))`
- Pflichtdateien: `config.json`, `model.safetensors`,
  `generation_config.json`, `codec.pth`, `tokenizer.json`,
  `preprocessor_config.json`, `configuration_arktts.py`,
  `modeling_arktts.py`, `modeling_arktts_codec.py`,
  `processing_arktts.py`.
- Helfer `audio8_dir`, `audio8_installed` und `resolve_audio8_voice`;
  `audio8_installed` bekommt den konfigurierten `audio8_model_dir` und
  hängt keinen festen Unterordner an.
- `models_payload` ergänzt den Audio8-Eintrag und den `audio8`-Block; für
  bestehende Aufrufer bleibt der Default `${models_dir}/audio8`, der
  Dispatch übergibt `settings.audio8_model_dir`.

### Lifecycle

Die bisher fest auf Qwen3-TTS verdrahtete TTS-Logik wird auf Backends
verallgemeinert:

- Ein Backend-Eintrag bündelt Modellname, Base-URL, Modellordner,
  Start-/Stop-Skript, PID-Datei, Health-Pfad, Timeouts und das Env-Präfix
  (`QWEN_TTS_` bzw. `AUDIO8_`).
- `ProfileCatalog` bekommt zusätzlich
  `(config.audio8_model, "audio8", "tts")`.
- `/v1/status` prüft für jedes TTS-Profil den Health-Endpunkt seines eigenen
  Workers. Ein laufendes Qwen3-TTS darf Audio8 nicht als geladen melden.
- `/v1/load` startet für das gewählte TTS-Profil den passenden Worker, wartet
  auf `/healthz` und ruft `POST /load` auf.
- Läuft noch ein Chat-Backend, antwortet `/v1/load` mit `409`.
- Läuft bereits ein anderes TTS-Backend, antwortet `/v1/load` ebenfalls mit
  `409`; der Aufrufer muss zuerst `/v1/unload` ausführen.
- `/v1/unload` mit Namen entlädt nur das genannte TTS-Backend:
  `POST /unload`, danach Stop-Skript, damit auch der CUDA-Kontext sicher
  freigegeben wird. Das gilt auch dann, wenn der Worker zwar läuft, aber
  `model_loaded: false` meldet (z. B. nach einem fehlgeschlagenen OOM-Load).
  Der bisherige `vorher_namen`-Guard wird dafür für TTS durch eine
  Health-Prüfung ersetzt; ein hängender Worker darf den nächsten Load nicht
  blockieren. Dieses Verhalten gilt für Qwen3-TTS und Audio8 gleichermaßen.
- `/v1/unload` ohne Namen entlädt Chat, Embeddings und alle geladenen
  TTS-Worker.
- Beim Start werden die Audio8-spezifischen Env-Variablen
  `AUDIO8_HOST`, `AUDIO8_PORT`, `AUDIO8_MODEL_DIR`,
  `AUDIO8_REFERENCE_AUDIO` und `AUDIO8_REFERENCE_TEXT` gesetzt.

Neue Gateway-Konfigurationsfelder:

- `audio8_model="Audio8-Realistic-Air"`
- `audio8_base="http://127.0.0.1:30006"`
- `audio8_model_dir="/mnt/models/audio/audio8"`
- `audio8_reference_audio="/mnt/models/audio/audio8/reference/de_male.wav"`
- `audio8_reference_text="/mnt/models/audio/audio8/reference/de_male.txt"`
- `audio8_start_script="runtime/audio8/start-audio8.sh"`
- `audio8_stop_script="runtime/audio8/stop-audio8.sh"`
- `audio8_pidfile="runtime/audio8/audio8.pid"`
- `audio8_health_path="/healthz"`
- `audio8_load_timeout_s=900`
- `audio8_unload_timeout_s=60`

### Skripte und Umgebung

`runtime/audio8/`:

- `setup.sh`: legt `venv` an, installiert die Requirements und CUDA-Torch
  (cu128, passend zur RTX 5070 Ti), prüft anschließend `torch.cuda`.
- `requirements.txt`: pinnt `torch==2.8.0`, `torchaudio==2.8.0`,
  `transformers==4.57.3`, `soundfile`, `safetensors`, `numpy`, `fastapi`,
  `uvicorn`, `httpx`, `huggingface_hub`.
- `download-models.sh`: nutzt den vorhandenen Ordner
  `/mnt/models/audio/audio8`, wenn er vollständig ist; sonst
  `hf download Edge0/Audio8-TTS-Realistic-Air --local-dir ...`. Prüft die
  Pflichtdateien und ruft `make-reference.sh` auf, wenn die Referenz fehlt.
- `make-reference.sh`: erzeugt mit der vorhandenen Piper-Stimme
  `de_DE-thorsten-medium` und einem festen deutschen Satz die Referenzdatei
  `de_male.wav` plus `de_male.txt`; idempotent, `--force` erzeugt neu.
  Stellt vorher sicher, dass die Piper-Stimme installiert ist.
- `start-audio8.sh`: idempotent, setzt `CUDA_VISIBLE_DEVICES=0`,
  `AUDIO8_HOST/PORT/MODEL_DIR/REFERENCE_AUDIO/REFERENCE_TEXT`,
  `PYTHONPATH=aiserver-monitor`, startet
  `nohup nice -n 5 venv/bin/python -B -m aiserver_audio8_tts`, schreibt
  PID-Datei und Log und wartet auf `/healthz`. Lädt das Modell **nicht**
  vorab.
- `stop-audio8.sh`: beendet nur den Worker anhand der PID-Datei.

`aiserver-monitor/run.sh` bekommt `start-audio8`, `stop-audio8`,
`status-audio8`, `audio8-setup` und `audio8-models` sowie
`AISERVER_MONITOR_AUDIO8_BASE`. Der Standardstart startet Audio8 **nicht**
mit, damit die GPU frei bleibt; `stop` stoppt es mit.

### Doku

- `aiserver-monitor/config.example.toml`: Audio8-Block mit denselben
  Kommentaren wie der Qwen3-TTS-Block.
- `README.md` und `README.de.md` sowie die beiden
  `aiserver-monitor/README*.md`: Modellliste, `/v1/load`/`/v1/unload`,
  Referenzstimme, Setup- und Download-Befehle, VRAM-Hinweis.
- Design und Plan unter `docs/superpowers/`.

## Ablauf der Pipeline

1. `POST /v1/unload` → Chat und alte Modelle entladen.
2. `POST /v1/load` mit `{"model": "Audio8-Realistic-Air"}` → Worker starten,
   Referenz prüfen, Modell auf die GPU laden.
3. `POST /v1/audio/speech` mit `{"model": "Audio8-Realistic-Air",
   "input": "...", "voice": "de_male"}`.
4. `POST /v1/unload` → Worker stoppen, VRAM freigeben.
5. Nächster Schritt.

## Fehlerfälle

- Chat läuft noch → `409`, nichts wird gestartet.
- Anderes TTS-Backend läuft noch → `409`, nichts wird gestartet.
- Modell fehlt → `503 model_not_installed` mit Download-Hinweis.
- Referenzdatei oder Transkript fehlt → `503 reference_missing` mit Hinweis
  auf `make-reference.sh`.
- Kein oder zu wenig VRAM → `503 vram_exhausted`, kein CPU-Fallback.
- Worker startet nicht → `502`.
- Laden überschreitet das Zeitlimit → `504`.
- Unbekannte Stimme → `400`.
- Unbekanntes Modell → `404`, nichts wird geladen.
- Unvollständige Generierung → einmalige Wiederholung mit größerem Limit;
  danach wird das vorhandene Audio mit Warnung ausgeliefert.

## Tests

- Worker: Settings/Env, Lazy-Load, `/load`, `/unload`, PCM-Ausgabe mit
  44,1 kHz, OOM-Meldung, fehlende und unlesbare Referenz, Vorladen des Codec
  beim Load, Wiederholung bei unvollständiger Generierung,
  Stimmenvalidierung. Engine, Prozessor und Modell werden in den Unit-Tests
  gefälscht; kein echtes Modell und kein CUDA nötig. Das gemeinsame
  Lifecycle-Gerüst wird über beide Apps getestet; die bestehenden
  Qwen3-TTS-Tests müssen unverändert grün bleiben.
- Audio-Sidecar: Modellauflösung, Voice-Default und -Validierung,
  Client-Fehler 502/504, Weitergabe der Upstream-Codes, `models_payload`,
  Dispatch, konfiguriertes `audio8_model_dir` und der unveränderte
  24-kHz-Vertrag für rohes PCM.
- Lifecycle: Profil im Katalog, `/v1/load` startet den richtigen Worker,
  Chat läuft noch → 409, anderes TTS läuft noch → 409, `/v1/unload` stoppt
  nur das gewählte Backend und räumt auch einen laufenden, aber nicht
  geladenen TTS-Worker ab, `/v1/status` trennt Qwen3-TTS und Audio8,
  Env-Weitergabe inklusive Referenzpfaden, `/v1/unload` ohne Namen stoppt
  beide.
- Skripte: Syntax, Ausführbarkeit, isolierte Ports und Kommandos in
  `test_run_sh.sh`.
- Voller Testlauf muss grün bleiben (aktuell 503 Tests).
- Ein echter Smoke-Test nach dem Modelldownload: laden, deutschen Satz
  synthetisieren, 44,1 kHz prüfen, entladen.

## Annahmen

- Das HF-Repo `Edge0/Audio8-TTS-Realistic-Air` lässt sich vollständig
  herunterladen und der Custom-Code läuft mit `transformers 4.57.3` und
  `torch 2.8.0` unter CUDA.
- Das Modell belegt nach dem Laden ca. 2–3 GB VRAM und passt nach dem
  Entladen des Chats auf die RTX 5070 Ti.
- Die Piper-Referenz ist als Default akzeptabel; wer eine echte Aufnahme
  bevorzugt, tauscht nur `AUDIO8_REFERENCE_AUDIO` und
  `AUDIO8_REFERENCE_TEXT` aus.
- WAV, MP3 und Opus übernehmen die 44,1 kHz aus dem ersten Chunk; rohes PCM
  bleibt beim bestehenden 24-kHz-Vertrag. Die 44,1 kHz sind damit kein
  Bestandteil des rohen PCM-Formats.
- Das Custom-Modell bietet `load_codec()` und lädt den Codec damit auf das
  konfigurierte Device; `/load` nutzt das für eine echte
  Bereitschaftsgarantie.
- `AUDIO8_MODEL_DIR` des Workers und `AUDIO_AUDIO8_MODEL_DIR` des Sidecars
  zeigen im Betrieb auf denselben Pfad; die Defaults und die
  Gateway-Weitergabe stellen das sicher.
- Der Aufrufer nennt das Modell exakt `Audio8-Realistic-Air`; der
  Audio-Sidecar matcht zusätzlich case-insensitiv und über die Aliase.
