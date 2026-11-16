# Architektur des AI-Servers

> Stand: 16.11.2026 · beschreibt den Aufbau des Rechners unter
> `/home/user/small_qwen`, so wie er in diesem Repository betrieben wird.
> Maschinenlesbare Defaults: [`aiserver-monitor/config.example.toml`](../aiserver-monitor/config.example.toml).

Dieses Dokument erklärt **wie** der Server zusammengesetzt ist: welche Prozesse
es gibt, wer mit wem spricht, wo die Modelle liegen, wer die GPU besitzt und
welche Entscheidungen dahinterstehen. Es ist bewusst keine Bedienungsanleitung –
die steht im [README](../README.de.md) und in
[`runtime/sglang/`](../runtime/sglang/README.de.md).

---

## Inhaltsverzeichnis

1. [Leitidee](#1-leitidee)
2. [Hardware und Betriebssystem](#2-hardware-und-betriebssystem)
3. [Ports und Prozesse](#3-ports-und-prozesse)
4. [Das Gateway `aiserver-monitor`](#4-das-gateway-aiserver-monitor)
5. [Die Chat-Backends](#5-die-chat-backends)
6. [Der Modellname-Alias](#6-der-modellname-alias)
7. [Sidecars: Embeddings, Audio, TTS](#7-sidecars-embeddings-audio-tts)
8. [GPU-Besitz und VRAM-Arbitrierung](#8-gpu-besitz-und-vram-arbitrierung)
9. [Datenhaltung und Telemetrie](#9-datenhaltung-und-telemetrie)
10. [Dashboard (Webapp)](#10-dashboard-webapp)
11. [Betrieb: systemd, `server.sh`, Logs](#11-betrieb-systemd-serversh-logs)
12. [Konfiguration](#12-konfiguration)
13. [Sicherheit und Netzwerkexposition](#13-sicherheit-und-netzwerkexposition)
14. [Tests und Qualitätssicherung](#14-tests-und-qualitätssicherung)
15. [Verzeichnisstruktur](#15-verzeichnisstruktur)
16. [Bekannte Eigenheiten und Grenzen](#16-bekannte-eigenheiten-und-grenzen)

---

## 1. Leitidee

Ein einziger offener Port (**30000**) spricht OpenAI-kompatibel. Dahinter
liegen mehrere Modelle, die sich **eine** Consumer-GPU teilen und deshalb nie
gleichzeitig laufen. Welches Modell antwortet, entscheidet ein Backend-Schalter;
die Clients merken davon nichts, weil das Gateway den von ihnen gesendeten
Modellnamen auf das aktive Backend umschreibt.

Drei Entwurfsentscheidungen prägen alles Weitere:

| Entscheidung | Grund |
|---|---|
| **Ein fester Upstream-Port (30001)** statt eines Ports pro Modell | Jedes Backend bindet denselben Port. Ein Wechsel ist damit nur ein Prozess-Stopp + Prozess-Start; das Gateway muss nie umkonfiguriert werden. |
| **Ein stabiler Client-Alias** (`escha-qwen38-27b-w2`) | Hermes, Hindsight und andere Agenten behalten ihre Konfiguration, auch wenn dahinter längst ein anderes Modell rechnet (heute z. B. Edge0-120B). |
| **Prozess-Besitz statt Koexistenz** | 16 GB VRAM reichen für genau ein großes Chat-Modell. Ollama-Backends entladen deshalb fremde Modelle, Qwen und Edge0 schließen sich gegenseitig aus. |

```
                            ┌──────────────────────────────┐
   Hermes / Hindsight ─────▶│  Gateway  :30000             │
   Skripte / curl      ─────▶│  aiserver-monitor (FastAPI)  │
   Browser (Dashboard) ─────▶│  OpenAI-Passthrough + UI     │
                            └───────┬──────────────┬───────┘
                                    │              │
                    /v1/chat|completions     /v1/embeddings · /v1/audio/*
                                    │              │
                                    ▼              ▼
                     ┌──────────────────────┐  ┌──────────────────────────┐
                     │ Upstream :30001      │  │ CPU-Sidecars             │
                     │ genau EIN Backend:   │  │  :30002 Embedding-Ollama │
                     │  qwen | flint |      │  │  :30003 Audio (Whisper/  │
                     │  gptoss | abl |      │  │          Piper)          │
                     │  edge0               │  │  :30004 Chatterbox (TTS) │
                     └──────────┬───────────┘  └──────────────────────────┘
                                │
                                ▼
                     ┌──────────────────────┐
                     │ RTX 5070 Ti, 16 GB   │
                     │ ein Modell zur Zeit  │
                     └──────────────────────┘
```

Auf der GPU gibt es zusätzlich **on-demand-TTS-Worker** (Qwen3-TTS auf 30005,
Audio8 auf 30006). Sie werden über die Lifecycle-API geladen, *nachdem* das
Chat-Backend entladen wurde, und bei `/v1/unload` wieder gestoppt.

---

## 2. Hardware und Betriebssystem

| Komponente | Wert |
|---|---|
| CPU | AMD Ryzen 7 9700X (8 Kerne / 16 Threads) |
| RAM | 64 GB |
| GPU | NVIDIA GeForce RTX 5070 Ti, 16 GB VRAM (`17094934528` Bytes ≈ 15,9 GiB) |
| OS | Linux, Kernel 7.0 (`x86_64`) |
| Treiber-Stack | CUDA 12.8 (SGLang/escha), NVFP4/Blackwell für Edge0 |
| Modellablage | `/mnt/models` (NVMe) |

Die 16 GB VRAM sind die härteste Randbedingung des ganzen Systems. Alle
folgenden Entscheidungen (Kontextgrößen, Parallelität, Entladeverhalten)
leiten sich daraus ab.

---

## 3. Ports und Prozesse

| Port | Bind | Prozess | Rolle | Start durch |
|---:|---|---|---|---|
| **30000** | `0.0.0.0` | `python -m aiserver_monitor` | Gateway: OpenAI-API, Dashboard, Lifecycle | `aiserver-monitor.service` |
| **30001** | `127.0.0.1` | genau eines von `qwen`, `flint`, `gptoss`, `abl`, `edge0` | Chat-Upstream | `server.sh` / Dashboard |
| **30002** | `127.0.0.1` | `ollama serve` (CPU) | Embeddings (`qwen3-embedding:0.6b`) | `ExecStartPre` des Gateways |
| **30003** | `127.0.0.1` | `python -m aiserver_audio` | STT (Whisper) + TTS (Piper), Audio-Proxy | `ExecStartPre` des Gateways |
| **30004** | `127.0.0.1` | Chatterbox-Worker | optionaler CPU-TTS | `ExecStartPre` des Gateways |
| **30005** | `127.0.0.1` | `python -m aiserver_qwen_tts` | GPU-TTS Qwen3-TTS | `/v1/load` (Lifecycle) |
| **30006** | `127.0.0.1` | `python -m aiserver_audio8_tts` | GPU-TTS Audio8 | `/v1/load` (Lifecycle) |
| 8094 | `127.0.0.1` | `edge0-infer serve` | **Altbestand**, eigenständiger Edge0-Dienst | `edge0.service` (deaktiviert) |
| 8090 | `0.0.0.0` | slotmon | liest das SGLang-Log passiv mit | `slotmon-monitor.service` |
| 11434 | `*` | System-Ollama (anderer Benutzer) | fremder Modelstore, nur lesend genutzt | Systemdienst |

**Nur 30000 ist von außen erreichbar.** Alle Sidecars und der Chat-Upstream
binden an Loopback. Das Gateway ist damit die einzige Vertrauensgrenze.

---

## 4. Das Gateway `aiserver-monitor`

Paket: [`aiserver-monitor/aiserver_monitor/`](../aiserver-monitor/aiserver_monitor/) ·
Start: `python -m aiserver_monitor` (uvicorn, **ein** Worker) ·
Rahmen: Starlette/FastAPI, `httpx` als Upstream-Client, SQLite für Historie.

### 4.1 Aufgaben

1. **OpenAI-Passthrough** für `/v1/*` – Chat, Completions, Modelle, Embeddings,
   Audio.
2. **Modellname-Umschreibung** und Ergänzung von `/v1/models`.
3. **Job-Tracking**: jeder Chat-Request wird als Job registriert, live
   verfolgt, abbrechbar, mit Token- und Timing-Metriken.
4. **Monitoring** für das Dashboard: GPU, Slots, Latenzen, Verlauf.
5. **Server-Steuerung**: Backends starten/stoppen/wechseln.
6. **Modell-Lebenszyklus**: Profile laden/entladen (`/v1/status`,
   `/v1/load`, `/v1/unload`).

### 4.2 Module

| Datei | Aufgabe |
|---|---|
| `__main__.py` | liest `AISERVER_MONITOR_CONFIG`, richtet rotierendes Dateilogging ein, startet uvicorn mit einem Worker |
| `config.py` | `Config`-Dataclass, TOML + `AISERVER_MONITOR_*`-Overrides, Validierung |
| `app.py` | Routen, `Deps`-Verdrahtung, statische Auslieferung der Webapp |
| `proxy.py` | der eigentliche Request-Pfad (1389 Zeilen): Routing, Alias, Relay, Non-Stream, Jobs, Abbrüche |
| `jobs.py` | Job-Registry im RAM, Statusmodell, Cancel-Token |
| `cancel.py` | best-effort `abort_request` gegen SGLang |
| `history.py` | SQLite: Jobs, Samples, Settings; Perzentile, Aggregation, Retention |
| `collector.py` | pollt Upstream-Metriken und `nvidia-smi`, schreibt Samples |
| `metrics_parse.py` | parst SGLang-Metriken und `nvidia-smi`-Ausgabe |
| `sse_parse.py` | inkrementeller SSE-Parser für Upstream-Streams |
| `events.py` | SSE-Hub für das Dashboard (`/monitor/events`) |
| `priority.py` | Max-Priorisierung anhand von System-Prompt-Markern |
| `hindsight.py` | blockt/freigibt Hindsight-Requests anhand derselben Marker |
| `serverctl.py` | Backend-Registry, Start/Stop über Skripte oder systemd, Zustandsautomat, CLI |
| `lifecycle.py` | `ProfileCatalog`, Laden/Entladen von Chat-, TTS- und Embedding-Profilen |

### 4.3 Request-Pfad

`handle_v1()` in `proxy.py` entscheidet anhand von Methode und Body:

```
POST /v1/chat/completions
├── stream: true   → Relay
│     Upstream-Kopfzeile prüfen
│     ├── Status ≥ 400 → unverändert zurück, kein Job
│     └── 200          → Job anlegen, SSE live weiterreichen,
│                        Chunks parsen (Tokens, TTFT), Job abschließen
└── stream: false  → interner Stream
      Job sofort als "waiting" publizieren
      Upstream mit stream=true + stream_options.include_usage aufrufen
      SSE reassemblieren zu einem ChatCompletion-JSON
      innerhalb heartbeat_s: eine Antwort ohne Leerzeichen
      sonst: Keepalive-Leerzeichen vor dem JSON (nonstream_keepalive)

POST /v1/completions        → wie Chat, ohne Chat-spezifische Felder
GET  /v1/models             → Passthrough + Alias-Ergänzung
POST /v1/embeddings         → eigener CPU-Ollama (30002)
     /v1/audio/*            → Audio-Sidecar (30003), gestreamt, ungepuffert
alles andere                → Byte-Passthrough
```

Warum der Umweg „Non-Stream intern als Stream“? SGLang und Edge0 liefern
Token-Metriken nur im Stream. Das Gateway bekommt so TTFT, Token-Zahlen und
Abbruchmöglichkeit auch für Clients, die `stream: false` senden – und kann
trotzdem ein einzelnes JSON zurückgeben.

**Abbruch:** Bei `abort_on_disconnect` (Default) überwacht je getracktem
Request eine `_DisconnectWatcher` die Client-Verbindung. Bricht der Client ab,
wird der Upstream-Aufruf gecancelt, der Job als `aborted` geschlossen und
best effort ein `abort_request` an SGLang geschickt. Explizite Abbrüche laufen
über `/monitor/jobs/{rid}/cancel` (Status 499, `aborted`-JSON).

### 4.4 HTTP-Oberfläche

**OpenAI-kompatibel (nach außen):**

| Route | Zweck |
|---|---|
| `POST /v1/chat/completions` | Chat, streamend oder als JSON |
| `POST /v1/completions` | Text-Completion |
| `GET /v1/models` | Modellliste inkl. Alias und `edge0-120b` |
| `POST /v1/embeddings` | Embeddings über den CPU-Sidecar |
| `POST /v1/audio/speech`, `/v1/audio/transcriptions`, … | Audio-Sidecar |
| `GET/POST /v1/status` | geladene Profile + alle ladbaren Namen |
| `POST /v1/load[/{name}]` | Profile laden (additiv) |
| `POST /v1/unload[/{name}]` | Profile entladen (Gewichte freigeben) |

**Dashboard/Monitoring:**

| Route | Zweck |
|---|---|
| `GET /healthz` | Liveness des Gateways |
| `GET /monitor/jobs` | Job-Registry (aktiv + Historie) |
| `GET /monitor/events` | SSE-Strom für Live-Ansicht |
| `POST /monitor/jobs/{rid}/cancel`, `/monitor/cancel_all` | Abbrüche |
| `GET /monitor/stats` | Zeitreihen, Perzentile, Zeitfenster |
| `GET /monitor/priority` | Max-Priorisierung lesen/schreiben |
| `GET /monitor/hindsight` | Hindsight-Blocker lesen/schreiben |
| `GET /monitor/server` | Backend-Zustände, Health, Transition |
| `POST /monitor/server/start`, `/stop` | Backend starten/stoppen |
| `GET /` | gebaute React-Webapp (`aiserver_monitor/static/`) |

---

## 5. Die Chat-Backends

Alle Backends liegen hinter **demselben Port 30001**. Genau eines läuft; der
Wechsel ist ein Stopp-/Start-Paar. Die Registry steht in
[`serverctl.py`](../aiserver-monitor/aiserver_monitor/serverctl.py)
(`BACKEND_NAMES`, `Backend`-Dataclass), die Start-/Stop-Skripte in `runtime/`.

| Name | Engine | Modell | Kontext | Parallelität | Health | Start/Stop |
|---|---|---|---:|---:|---|---|
| `qwen` | SGLang (escha-Fork) | `Qwen3.8-27B-Escha-W2` (2-Bit, ~10 GB) | 65 536 | 1 (SERIAL) | `/health` | `escha-server.service` |
| `flint` | Ollama (eigener Prozess) | `hf.co/AtomicChat/Flint-GGUF:IQ4_XS` | 32 768 | 2 | `/api/version` | `runtime/flint/*.sh` |
| `gptoss` | Ollama | `gpt-oss:20b` | 65 536 | 1 | `/api/version` | `runtime/gptoss/*.sh` |
| `abl` | Ollama | `Qwen3-14B-abliterated-GGUF:Q4_K_M` | 40 960 | 1 | `/api/version` | `runtime/abl/*.sh` |
| `edge0` | edge0-infer + edge0-strata | `Edge0-120B-NVFP4` (~33 GB Experten auf NVMe) | 131 072 | 1 | `/health` | `runtime/edge0/*.sh` |

### 5.1 Qwen / Escha (SGLang)

Die ursprüngliche Laufzeit: der `escha`-SGLang-Fork mit dem 2-Bit-Modell
`EschaLabs/Qwen3.8-27B-Escha-W2` und der **Overthinking-Penalty**. Sie
bestraft Zögerungsmarker („Wait“, „But“, „Alternatively“) und verhindert, dass
sich das quantisierte Reasoning-Modell aus einer gefundenen Antwort
hinausredet. Aktiviert wird sie über `OTP=1` (Server-Default-Patch) plus
`otp.json`; Details in [`runtime/sglang/`](../runtime/sglang/README.de.md).

Wichtige Launcher-Defaults (`runtime/sglang/serve.sh`):

| Variable | Default | Bedeutung |
|---|---|---|
| `PORT` | 30001 | Upstream-Port |
| `CTXLEN` | 65536 | Kontext-Cap pro Request (Modell könnte 262 144) |
| `MEM` | 0.72 | `mem-fraction-static`; Rest für CUDA-Graphen und KV |
| `CHUNK` | 2048 | `chunked-prefill-size` gegen Aktivierungs-Spikes |
| `SERIAL` | 1 | Single-Flight: eine Generierung zur Zeit (Multi-Agent-Konsistenz) |

Betrieben wird Qwen über die User-Unit `escha-server.service`
(`ExecStart=runtime/sglang/start-penalty.sh start-systemd 2.0`). Sie ist
derzeit **failed/gestoppt** – Edge0 hat sie faktisch abgelöst.

### 5.2 Flint, GPT-OSS, Abliterated (Ollama)

Alle drei sind **eigene `ollama serve`-Prozesse**, die auf 30001 lauschen –
nicht der System-Ollama auf 11434. Vorteile: eigener Modelstore, eigene
Kontext-/Parallelitätswerte, kein fremder Dienst muss umkonfiguriert werden.
Der System-Ollama wird über `OLLAMA_NOPRUNE=true` nur **lesend** mitbenutzt
und vor dem Start entladen, damit kein fremdes Modell VRAM blockiert.

- **Flint** – 2 parallele Slots à 32k, gemessen ~70 tok/s pro Anfrage,
  ~130 tok/s zusammen (`FLINT_PARALLEL=2`, `keep_alive=-1`).
- **GPT-OSS 20B** – 64k, 1 Slot. Das Gateway setzt bei fehlenden
  Client-Vorgaben `temperature: 0` und `reasoning_effort: "low"`.
- **Abliterated** – Qwen3-14B ohne Refusal-Verhalten, 40k, 1 Slot;
  `reasoning_effort` wird entfernt, weil Ollama es bei diesem Modell nicht kennt.

Start-/Stop laufen über `runtime/<backend>/start-*.sh` mit PID-File in
`runtime/<backend>/*.pid`. `gptoss` und `abl` exportieren nur ihre
`*_CTX`/`*_MODEL`-Werte und rufen das Flint-Skript auf – eine Implementierung,
drei Profile.

### 5.3 Edge0 120B (NVFP4)

Edge0 ist kein Ollama- und kein SGLang-Modell, sondern ein eigener Stack:

```
edge0-infer serve            ← HTTP-Frontend, OpenAI-API, Prefix-Cache
   └── edge0-strata          ← Engine (MoE-Experten-Streaming)
         └── /mnt/models/Edge0-120B-NVFP4   ← ~33 GB Experten auf NVMe
```

Das Modell ist ein 120B-MoE in NVFP4. Nur ein Teil der Experten passt in die
16 GB VRAM; der Rest wird per DMA von der NVMe nachgeladen. Tuning in
[`runtime/edge0/start-edge0.sh`](../runtime/edge0/start-edge0.sh):

| Argument | Wert | Wirkung |
|---|---|---|
| `--context` | 131 072 | großer Kontext für lange Hermes-System-Prompts |
| `--vram-reserve-mib` | 400 | Luft für den Treiber |
| `--kv-resident` | 20 480 | KV-Cache im VRAM |
| `--expert-cache-per-layer` | an | Experten-Slots pro Layer statt global |
| `--kv-grow` | an | KV wächst mit dem Bedarf |
| `--pool-workers` | 7 | 8 Kerne minus Host-Thread |
| `--pcie-mode dma` | an | direkter NVMe→VRAM-Transfer |
| `--resident-budget-gib` | 16 | VRAM-Budget |

Gemessen auf dieser Maschine: **~60–70 tok/s Decode**, **~1 900 tok/s Prefill**,
**Prefix-Cache** greift bei stabilem Prompt-Anfang (identischer 83,7k-Prompt:
2. Lauf 1,7 s statt 43,7 s). Deshalb ist Edge0 für Hermes mit langem,
stabilem System-Prompt klar besser als Escha, das jeden Prompt neu verarbeitet.

Besonderheit beim Stoppen: `edge0-infer` startet `edge0-strata` als
Kindprozess. Beide laufen via `setsid` in einer eigenen Prozessgruppe; das
Stop-Skript killt die **ganze Gruppe** (`kill -- -$pid`), sonst bliebe strata
mit ~15 GB VRAM zurück.

### 5.4 Der Backend-Zustandsautomat

`ServerControl.snapshot()` liefert pro Backend `running`, `starting`,
`stopping` oder `stopped`:

- **qwen** wird über die systemd-Unit geführt (`unit` gesetzt).
- Alle anderen werden über PID-File + Health-Pfad geführt.
- Während eines Wechsels überlagert eine Transition (`starting`/`stopping`)
  den abgeleiteten Zustand, damit das Dashboard nicht flackert.
- `start`/`stop` rufen die Skripte über einen Spawner auf (fire-and-forget);
  das Warten auf Health übernimmt der Aufrufer mit Timeout (900 s Start,
  90 s Stop).

---

## 6. Der Modellname-Alias

Clients senden weiterhin **`escha-qwen38-27b-w2`** – unabhängig davon, welches
Backend läuft. Das Gateway löst das an drei Stellen auf:

1. **Request-Umschreibung** (`_modell_anpassen`): Steht im Body ein bekannter
   Name (`model_alias`, `flint_model`, `gptoss_model`, `abl_model`,
   `edge0_model`), wird er durch das **aktiv laufende** Modell ersetzt. Das
   aktive Modell kommt aus dem PID-File des laufenden Backends
   (`BackendProbe._pidfile_modell`), mit `/api/ps` als Fallback für Ollama.
   Der Dashboard-Schalter ist damit autoritativ – nicht der Client.
2. **`/v1/models`** (`_modelle_mit_alias`): Die Upstream-Liste wird um
   `edge0-120b` und den Alias ergänzt, damit Clients den Namen auch entdecken.
3. **Embeddings** (`_embedding_modell_anpassen`): Chat-Alias, Flint-/GPT-OSS-/
   Abl-/Edge0-Name und `embed_model` werden auf das eine Embedding-Modell
   abgebildet.

Folge: Ein Agent kann ohne Änderung von Escha auf Edge0 umziehen. Sichtbar
ist der Wechsel nur am `model`-Feld der Antwort (`edge0-120b`).

---

## 7. Sidecars: Embeddings, Audio, TTS

Sidecars sind eigenständige Prozesse mit eigenem Lebenszyklus. Die
CPU-Sidecars startet die Gateway-Unit per `ExecStartPre` (mit `-`, damit ein
Fehler den Chat nie blockiert); die GPU-TTS-Worker werden über die
Lifecycle-API geladen.

### 7.1 Embeddings – Port 30002 (CPU)

Eigener `ollama serve` mit `qwen3-embedding:0.6b`. `CUDA_VISIBLE_DEVICES` ist
leer, Ollama fällt auf die CPU zurück, `keep_alive=-1` hält das Modell warm.
Dadurch laufen Embeddings **parallel** zum Chat-Backend, ohne VRAM zu
konkurrieren. `/v1/embeddings` wird dorthin geroutet.

### 7.2 Audio – Port 30003 (CPU)

Paket `aiserver_audio`: Whisper (STT) und Piper (TTS) hinter einer
FastAPI-App. Das Gateway reicht `/v1/audio/*` **gestreamt** durch, ohne
Audiodateien zu puffern; Upload-Limit 100 MB, Timeout 1800 s. Optional wird
ein Chatterbox-Worker auf 30004 als weitere TTS-Stimme angesprochen.

### 7.3 GPU-TTS – Ports 30005 / 30006

Zwei TTS-Modelle, die **VRAM brauchen** und deshalb nicht dauerhaft laufen:

| Profil | Port | Modell | Besonderheit |
|---|---:|---|---|
| `Qwen3-TTS` | 30005 | Qwen3-TTS | Worker-Prozess, Health `/healthz` |
| `Audio8-Realistic-Air` | 30006 | Audio8, 0,6B, 23 Sprachen, 44,1 kHz | feste männliche Referenzstimme (`de_male`) |

Beide werden über `/v1/load` gestartet (dabei wird das Chat-Backend vorher
entladen) und bei `/v1/unload` gestoppt, damit der CUDA-Kontext verschwindet.
Der ProfileCatalog in `lifecycle.py` führt sie als `kind="tts"`.

### 7.4 Lebenszyklus-Modell

`ProfileCatalog` ist die einzige Wahrheit über ladbare Namen:

```
(chat)      model_alias → qwen · flint_model → flint · gptoss_model → gptoss
            abl_model → abl · edge0_model → edge0
(tts)       qwen_tts_model → qwen-tts · audio8_model → audio8
(embedding) embed_model → embed
```

`/v1/load` arbeitet **additiv** und ist idempotent: Was schon läuft, bleibt.
`/v1/unload` gibt Gewichte frei – bei `qwen`/`edge0` durch Stoppen des
Prozesses, bei Ollama durch `keep_alive: 0`, bei TTS durch Stoppen des Workers.

---

## 8. GPU-Besitz und VRAM-Arbitrierung

Es gibt genau **einen** Besitzer der GPU pro Zeit. Drei Mechanismen greifen
ineinander:

1. **Ein Upstream-Port.** Zwei Chat-Backends können nicht gleichzeitig auf
   30001 lauschen. Der Wechsel ist strukturell exklusiv.
2. **Ollama entlädt fremde Modelle.** `start-*.sh` fragt `127.0.0.1:11434/api/ps`
   ab und schickt `keep_alive: 0` für alles, was dort geladen ist. Sonst
   scheitert der Start an fehlendem VRAM.
3. **Qwen ↔ Edge0 gegenseitig.** Beide halten ihre Gewichte im eigenen Prozess
   und passen nicht nebeneinander. `Lifecycle._ensure_chat` stoppt deshalb ein
   laufendes Prozess-Backend, bevor das andere startet
   (`_PROZESS_BACKENDS = ("qwen", "edge0")`).
4. **TTS nach Chat.** Die GPU-TTS-Worker starten erst, nachdem der Chat
   entladen wurde, und werden beim Unload gestoppt.

Praktische Belegung:

| Zustand | VRAM |
|---|---|
| Leerlauf (nur Sidecars) | ~0–2 MiB |
| Edge0 120B | ~15,8 GB von 15,9 GB |
| Qwen/Escha | ~10 GB Gewichte + KV/CUDA-Graphen (MEM=0.72) |
| Flint/GPT-OSS/Abl | je nach Modell, ein Slot |

Der systemd-Dienst `edge0.service` (Port 8094) ist **deaktiviert**. Er war der
ursprüngliche Einzelstart; heute ist der Monitor alleiniger Besitzer, sonst
würden zwei Instanzen um dieselbe GPU kämpfen. `/home/user/start-edge0.sh`
bleibt als manueller Standalone-Start erhalten.

---

## 9. Datenhaltung und Telemetrie

SQLite unter `aiserver-monitor/data/monitor.db` (Pfad aus `db_path`). Drei
Tabellen:

| Tabelle | Inhalt | Retention |
|---|---|---|
| `jobs` | ein Datensatz pro Chat-Request: `rid`, Quelle, Modell, Prompt-Anfang, Status, Zeitstempel (erstellt/erstes Token/fertig), Token-Zahlen, Fehler | 30 Tage |
| `samples` | Zeitreihe im Poll-Takt: Upstream `running`/`waiting`/`throughput`/`token_usage`, GPU-Auslastung und VRAM | 7 Tage |
| `settings` | JSON-Werte für Dashboard-Schalter (Priorität, Hindsight) | – |

**Collector** (`collector.py`): alle `poll_interval_s` (2 s) werden
Upstream-Metriken und `nvidia-smi` gelesen
(`utilization.gpu,memory.used,memory.total`), als Sample gespeichert und an
den SSE-Hub gemeldet. Fehler werden zu `None`-Werten – ein hängendes
`nvidia-smi` darf das Gateway nie blockieren (eigenes Zeitlimit).

**History** (`history.py`): schreibt Jobs, aggregiert Zeitreihen, berechnet
Perzentile (Nearest-Rank, ohne Interpolation) und räumt alte Zeilen auf.
`/monitor/stats` liefert Zeitfenster, Durchsatz, TTFT und Fehlerquellen.

**Priorität und Hindsight** (`priority.py`, `hindsight.py`): Beide erkennen
den Max-Agenten an Markern in `role: system`-Nachrichten
(Default `You are Max, built for Marec`). Priorität setzt `priority: 100`
für Max und normal für alle anderen; der Hindsight-Blocker weist passende
Requests sofort ab, ohne das Backend zu belasten. Beide sind im Dashboard
schaltbar und werden in `settings` persistiert.

---

## 10. Dashboard (Webapp)

React + TypeScript + Vite in [`aiserver-monitor/webapp/`](../aiserver-monitor/webapp/).
Der Build landet in `aiserver_monitor/static/` (gitignored) und wird vom
Gateway unter `/` ausgeliefert. Kein separater Webserver, kein zweiter Port.

| View | Inhalt |
|---|---|
| `LiveView` + `CurrentJobPanel` + `JobTable` | laufende und vergangene Jobs, Live-Stream über SSE, Prompt-Vorschau, Abbruch |
| `LiveInsights` | abgeleitete Kennzahlen aus dem Live-Strom |
| `StatsView` + `TimeChart` | Zeitfenster, Durchsatz, TTFT, GPU/VRAM-Verlauf, Fehlerverteilung |
| `ServerView` | Backend-Kacheln mit Zustand und Health, Start/Stop/Wechsel, Transitionen |
| `PriorityView` | Max-Priorisierung: Marker, An/Aus |
| `PreviewModal` | vollständiger Prompt/Response eines Jobs |

Die Webapp spricht ausschließlich mit demselben Origin (`/monitor/*`,
`/v1/*`). SSE (`/monitor/events`) trägt Job-Updates und Samples; die
Verbindungsanzeige unterscheidet `verbinde`, `verbunden`, `getrennt`.

---

## 11. Betrieb: systemd, `server.sh`, Logs

### 11.1 systemd-User-Units

| Unit | Rolle | Besonderheit |
|---|---|---|
| `aiserver-monitor.service` | Gateway + CPU-Sidecars | `Restart=always`; `ExecStartPre=-` startet Embed/Audio/Chatterbox; `ExecStopPost=-` stoppt sie; `TimeoutStopSec=15` mit `SuccessExitStatus=SIGKILL`, weil offene SSE-Streams den Shutdown aufhalten können |
| `escha-server.service` | SGLang/Qwen | `Restart=always`, `RestartSec=15`; Start über `start-penalty.sh start-systemd 2.0`; Log nach `/tmp/escha_server.log` |
| `slotmon-monitor.service` | GPU-Slot-Monitor | liest das SGLang-Log passiv, Port 8090 |
| `edge0.service` | Altbestand Port 8094 | **deaktiviert** |

Die Unit-Datei des Gateways liegt im Repo
([`aiserver-monitor/systemd/`](../aiserver-monitor/systemd/)); die
SGLang-Units liegen unter `runtime/sglang/`.

### 11.2 `server.sh`

Dünner Wrapper um `python -m aiserver_monitor.serverctl`; arbeitet mit
**derselben** Logik wie der Dashboard-Tab „Server“.

```bash
bash server.sh start [qwen|flint|gptoss|abl|edge0]
bash server.sh stop  [backend]
bash server.sh switch <backend>     # implizit start; alter Prozess weicht
bash server.sh restart [backend]
bash server.sh status
```

`start` wartet bis zu 900 s auf Health (Edge0/Qwen brauchen 2–4 min,
Ollama-Backends wenige Sekunden), `stop` bis zu 90 s. Das Gateway bleibt davon
unberührt.

### 11.3 Logs und PID-Files

| Was | Wo |
|---|---|
| Gateway (rotierend) | `aiserver-monitor/data/aiserver-monitor.log` |
| Qwen/Escha | `/tmp/escha_server.log` |
| Flint / GPT-OSS / Abl | `/tmp/flint_server.log` (gemeinsam) |
| Edge0 | `/tmp/edge0_server.log` |
| Embedding | `/tmp/embed_server.log` |
| PID-Files | `runtime/<backend>/*.pid` (gitignored) |

---

## 12. Konfiguration

Reihenfolge: **Defaults < TOML < Umgebungsvariablen**.

- TOML-Datei über `AISERVER_MONITOR_CONFIG=/pfad/config.toml`.
- Jedes Feld einzeln über `AISERVER_MONITOR_<FELDNAME>` (Großbuchstaben),
  z. B. `AISERVER_MONITOR_LISTEN_PORT=39999`.
- Leere Werte gelten als nicht gesetzt.
- `Config.__post_init__` weist nicht-positive Intervalle und leere
  Pflichtfelder ab – ein `heartbeat_s=0` würde sonst zur Busy-Loop.

Die vollständige, kommentierte Liste steht in
[`config.example.toml`](../aiserver-monitor/config.example.toml). Wichtige
Gruppen: Netzwerk, Embeddings, Lifecycle, Qwen3-TTS, Audio8, Audio,
Pfade/Retention, Server-Steuerung, die fünf Chat-Backends, Chatterbox.

---

## 13. Sicherheit und Netzwerkexposition

- **Nur 30000 ist öffentlich.** Upstream und alle Sidecars binden an
  `127.0.0.1`. Es gibt keine Authentifizierung – der Schutz ist die
  Netzwerktopologie.
- **Keine Secrets im Repo.** API-Keys, falls nötig, gehören in die Unit oder
  eine nicht versionierte TOML; `otp.json` (Penalty-Payload) ist gitignored.
- **Modell-Downloads** laufen über Hugging Face; die Skripte in `runtime/*/`
  dokumentieren die erwarteten Pfade unter `/mnt/models`.
- **Fremder Ollama** (11434) wird nur lesend benutzt (`OLLAMA_NOPRUNE`), nie
  beschrieben.

---

## 14. Tests und Qualitätssicherung

`python -m unittest discover -s tests` im Verzeichnis `aiserver-monitor/`.

- **596 Tests**, aktuell grün.
- Kein echter Server, keine echte GPU: Runner (systemctl/pgrep), Spawner
  (Start-/Stop-Skripte), Health-Reader und `httpx`-Transport werden injiziert.
- Abgedeckt sind u. a.: Alias-Umschreibung, `/v1/models`-Ergänzung, Job-Status,
  Abbrüche, Non-Stream-Reassembly, Keepalives, History/Retention, Priorität,
  Hindsight, Backend-Zustandsautomat, Transitionen, ProfileCatalog,
  Lifecycle-Laden/Entladen, TTS-Worker.
- E2E-Spec für das Dashboard: `tests/e2e/dashboard.spec.py`.
- Webapp-Build: `npm run build` in `aiserver-monitor/webapp/` (TypeScript-Check
  + Vite-Build).

---

## 15. Verzeichnisstruktur

```
small_qwen/
├── server.sh                     # CLI: start/stop/switch/status/restart
├── README.md / README.de.md      # Einstieg, Schnellstart, Doku-Karte
├── docs/                         # technische Vertiefungen (dieses Dokument)
├── aiserver-monitor/
│   ├── aiserver_monitor/         # Gateway (FastAPI, proxy, lifecycle, …)
│   ├── aiserver_audio/           # CPU-Sidecar: Whisper + Piper + Chatterbox
│   ├── aiserver_qwen_tts/        # GPU-TTS-Worker Qwen3-TTS
│   ├── aiserver_audio8_tts/      # GPU-TTS-Worker Audio8
│   ├── aiserver_chatterbox/      # CPU-TTS-Worker
│   ├── webapp/                   # React/Vite-Dashboard
│   ├── systemd/                  # Unit des Gateways
│   ├── tests/                    # unittest-Suite + E2E-Spec
│   ├── config.example.toml       # alle Stellschrauben, kommentiert
│   └── data/                     # SQLite + Log (gitignored)
├── runtime/
│   ├── sglang/                   # escha/SGLang: serve.sh, Penalty, Units
│   ├── flint/ gptoss/ abl/       # Ollama-Backends (gemeinsames Skript)
│   ├── edge0/                    # edge0-infer + edge0-strata
│   ├── embed/                    # CPU-Ollama für Embeddings
│   ├── audio/                    # Whisper/Piper
│   ├── chatterbox/               # CPU-TTS
│   ├── qwen-tts/ audio8/         # GPU-TTS mit eigenem venv
│   └── …
├── slotmon/                      # passiver GPU-Slot-Monitor
├── ctxmon.sh                     # tok/s- und Kontext-Monitor
└── Qwen3.8-27B-Escha-W2/         # Modellgewichte (nicht Teil des Repos)
```

---

## 16. Bekannte Eigenheiten und Grenzen

- **Escha ist derzeit gestoppt** (`escha-server.service` failed). Edge0 hat die
  Rolle übernommen; der Alias funktioniert unverändert weiter.
- **Edge0 lädt 33 GB Experten von der NVMe.** Der erste Start dauert 2–4 min;
  danach ist der Prefix-Cache entscheidend für schnelle Follow-ups. Ein
  wechselnder Prompt-Anfang kostet jedes Mal volles Prefill (~1 900 tok/s).
- **Ollama-Backends teilen sich ein Log** (`/tmp/flint_server.log`), weil sie
  dasselbe Startskript benutzen.
- **`pkill -f` ist gefährlich:** Muster wie `strata`, `sglang` oder
  `edge0-infer` treffen auch die aufrufende Shell. Immer über
  `server.sh`, `systemctl` oder das PID-File stoppen.
- **Kein Multi-Modell-Betrieb.** Zwei Chat-Modelle gleichzeitig passen nicht in
  die 16 GB; das System ist bewusst auf strikten Wechsel ausgelegt.
- **`/v1/models` ist eine Momentaufnahme.** Die Liste nennt den aktiven
  Upstream-Namen plus die stabilen Gateway-Namen; sie sagt nicht, welche
  Gewichte geladen sind. Dafür ist `/v1/status` zuständig.
