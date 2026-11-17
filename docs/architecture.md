# Architecture of the AI server

> As of 2026-11-16 · describes the machine under `/home/user/small_qwen` as this
> repository runs it. Machine-readable defaults:
> [`aiserver-monitor/config.example.toml`](../aiserver-monitor/config.example.toml).

This document explains **how** the server is put together: which processes
exist, who talks to whom, where the models live, who owns the GPU, and which
decisions sit behind all of it. It is deliberately not a user manual — that
lives in the [README](../README.md) and in
[`runtime/sglang/`](../runtime/sglang/README.md).

---

## Table of contents

1. [Design idea](#1-design-idea)
2. [Hardware and operating system](#2-hardware-and-operating-system)
3. [Ports and processes](#3-ports-and-processes)
4. [The `aiserver-monitor` gateway](#4-the-aiserver-monitor-gateway)
5. [Chat backends](#5-chat-backends)
6. [The model-name alias](#6-the-model-name-alias)
7. [Sidecars: embeddings, audio, TTS](#7-sidecars-embeddings-audio-tts)
8. [GPU ownership and VRAM arbitration](#8-gpu-ownership-and-vram-arbitration)
9. [Data and telemetry](#9-data-and-telemetry)
10. [Dashboard (web app)](#10-dashboard-web-app)
11. [Operations: systemd, `server.sh`, logs](#11-operations-systemd-serversh-logs)
12. [Configuration](#12-configuration)
13. [Security and network exposure](#13-security-and-network-exposure)
14. [Tests and quality assurance](#14-tests-and-quality-assurance)
15. [Directory layout](#15-directory-layout)
16. [Known quirks and limits](#16-known-quirks-and-limits)

---

## 1. Design idea

A single public port (**30000**) speaks OpenAI-compatible HTTP. Behind it sit
several models that share **one** consumer GPU and therefore never run at the
same time. A backend switch decides which model answers; clients never notice,
because the gateway rewrites the model name they send to whichever backend is
active.

Three design decisions shape everything else:

| Decision | Reason |
|---|---|
| **One fixed upstream port (30001)** instead of a port per model | Every backend binds the same port. A switch is just stop + start; the gateway never needs reconfiguring. |
| **One stable client alias** (`escha-qwen38-27b-w2`) | Hermes, Hindsight and other agents keep their configuration even when a different model (today e.g. Edge0-120B) does the work. |
| **Process ownership instead of coexistence** | 16 GB of VRAM fit exactly one large chat model. Ollama backends unload foreign models; Qwen and Edge0 exclude each other. |

```
                            ┌──────────────────────────────┐
   Hermes / Hindsight ─────▶│  gateway  :30000             │
   scripts / curl      ─────▶│  aiserver-monitor (FastAPI)  │
   browser (dashboard) ─────▶│  OpenAI passthrough + UI     │
                            └───────┬──────────────┬───────┘
                                    │              │
                    /v1/chat|completions     /v1/embeddings · /v1/audio/*
                                    │              │
                                    ▼              ▼
                     ┌──────────────────────┐  ┌──────────────────────────┐
                     │ upstream :30001      │  │ CPU sidecars             │
                     │ exactly ONE backend: │  │  :30002 embedding Ollama │
                     │  qwen | flint |      │  │  :30003 audio (Whisper/  │
                     │  gptoss | abl |      │  │          Piper)          │
                     │  edge0               │  │  :30004 Chatterbox (TTS) │
                     └──────────┬───────────┘  └──────────────────────────┘
                                │
                                ▼
                     ┌──────────────────────┐
                     │ RTX 5070 Ti, 16 GB   │
                     │ one model at a time  │
                     └──────────────────────┘
```

On top of that, the GPU hosts **on-demand TTS workers** (Qwen3-TTS on 30005,
Audio8 on 30006). They are loaded through the lifecycle API *after* the chat
backend has been unloaded, and stopped on `/v1/unload`.

---

## 2. Hardware and operating system

| Component | Value |
|---|---|
| CPU | AMD Ryzen 7 9700X (8 cores / 16 threads) |
| RAM | 64 GB |
| GPU | NVIDIA GeForce RTX 5070 Ti, 16 GB VRAM (`17094934528` bytes ≈ 15.9 GiB) |
| OS | Linux, kernel 7.0 (`x86_64`) |
| Driver stack | CUDA 12.8 (SGLang/escha), NVFP4/Blackwell for Edge0 |
| Model storage | `/mnt/models` (NVMe) |

The 16 GB of VRAM are the hardest constraint in the system. Every decision
below (context sizes, parallelism, unload behaviour) follows from it.

---

## 3. Ports and processes

| Port | Bind | Process | Role | Started by |
|---:|---|---|---|---|
| **30000** | `0.0.0.0` | `python -m aiserver_monitor` | gateway: OpenAI API, dashboard, lifecycle | `aiserver-monitor.service` |
| **30001** | `127.0.0.1` | exactly one of `qwen`, `flint`, `gptoss`, `abl`, `edge0` | chat upstream | `server.sh` / dashboard |
| **30002** | `127.0.0.1` | `ollama serve` (CPU) | embeddings (`qwen3-embedding:0.6b`) | gateway `ExecStartPre` |
| **30003** | `127.0.0.1` | `python -m aiserver_audio` | STT (Whisper) + TTS (Piper), audio proxy | gateway `ExecStartPre` |
| **30004** | `127.0.0.1` | Chatterbox worker | optional CPU TTS | gateway `ExecStartPre` |
| **30005** | `127.0.0.1` | `python -m aiserver_qwen_tts` | GPU TTS Qwen3-TTS | `/v1/load` (lifecycle) |
| **30006** | `127.0.0.1` | `python -m aiserver_audio8_tts` | GPU TTS Audio8 | `/v1/load` (lifecycle) |
| 8094 | `127.0.0.1` | `edge0-infer serve` | **legacy**, standalone Edge0 | `edge0.service` (disabled) |
| 8090 | `0.0.0.0` | slotmon | passively tails the SGLang log | `slotmon-monitor.service` |
| 11434 | `*` | system Ollama (other user) | foreign model store, read-only | system service |

**Only 30000 is reachable from outside.** Every sidecar and the chat upstream
bind to loopback. The gateway is therefore the only trust boundary.

---

## 4. The `aiserver-monitor` gateway

Package: [`aiserver-monitor/aiserver_monitor/`](../aiserver-monitor/aiserver_monitor/) ·
start: `python -m aiserver_monitor` (uvicorn, **one** worker) ·
stack: Starlette/FastAPI, `httpx` as upstream client, SQLite for history.

### 4.1 Responsibilities

1. **OpenAI passthrough** for `/v1/*` — chat, completions, models, embeddings,
   audio.
2. **Model-name rewriting** and enrichment of `/v1/models`.
3. **Job tracking**: every chat request becomes a job, tracked live,
   cancellable, with token and timing metrics.
4. **Monitoring** for the dashboard: GPU, slots, latencies, history.
5. **Server control**: start/stop/switch backends.
6. **Model lifecycle**: load/unload profiles (`/v1/status`, `/v1/load`,
   `/v1/unload`).

### 4.2 Modules

| File | Purpose |
|---|---|
| `__main__.py` | reads `AISERVER_MONITOR_CONFIG`, sets up rotating file logging, starts uvicorn with one worker |
| `config.py` | `Config` dataclass, TOML + `AISERVER_MONITOR_*` overrides, validation |
| `app.py` | routes, `Deps` wiring, static serving of the web app |
| `proxy.py` | the actual request path (1389 lines): routing, alias, relay, non-stream, jobs, aborts |
| `jobs.py` | in-memory job registry, status model, cancel token |
| `cancel.py` | best-effort `abort_request` against SGLang |
| `history.py` | SQLite: jobs, samples, settings; percentiles, aggregation, retention |
| `collector.py` | polls upstream metrics and `nvidia-smi`, writes samples |
| `metrics_parse.py` | parses SGLang metrics and `nvidia-smi` output |
| `sse_parse.py` | incremental SSE parser for upstream streams |
| `events.py` | SSE hub for the dashboard (`/monitor/events`) |
| `priority.py` | Max prioritisation based on system-prompt markers |
| `hindsight.py` | blocks/releases Hindsight requests using the same markers |
| `serverctl.py` | backend registry, start/stop via scripts or systemd, state machine, CLI |
| `lifecycle.py` | `ProfileCatalog`, loading/unloading chat, TTS and embedding profiles |

### 4.3 Request path

`handle_v1()` in `proxy.py` decides by method and body:

```
POST /v1/chat/completions
├── stream: true   → relay
│     inspect upstream headers
│     ├── status ≥ 400 → returned unchanged, no job
│     └── 200          → create job, forward SSE live,
│                        parse chunks (tokens, TTFT), finish job
└── stream: false  → internal stream
      publish job as "waiting" immediately
      call upstream with stream=true + stream_options.include_usage
      reassemble SSE into one ChatCompletion JSON
      within heartbeat_s: a single response without spaces
      otherwise: keepalive spaces before the JSON (nonstream_keepalive)

POST /v1/completions        → like chat, without chat-specific fields
GET  /v1/models             → passthrough + alias enrichment
POST /v1/embeddings         → dedicated CPU Ollama (30002)
     /v1/audio/*            → audio sidecar (30003), streamed, unbuffered
everything else             → byte passthrough
```

Why the detour of "non-stream internally as a stream"? SGLang and Edge0 only
report token metrics inside a stream. This way the gateway gets TTFT, token
counts and cancellation even for clients that send `stream: false` — and can
still return a single JSON document.

**Cancellation:** with `abort_on_disconnect` (default), a `_DisconnectWatcher`
guards each tracked request's client connection. If the client disconnects,
the upstream call is cancelled, the job is closed as `aborted`, and a
best-effort `abort_request` is sent to SGLang. Explicit cancellations go
through `/monitor/jobs/{rid}/cancel` (status 499, `aborted` JSON).

### 4.4 HTTP surface

**OpenAI-compatible (external):**

| Route | Purpose |
|---|---|
| `POST /v1/chat/completions` | chat, streaming or JSON |
| `POST /v1/completions` | text completion |
| `GET /v1/models` | model list including alias and `edge0-120b` |
| `POST /v1/embeddings` | embeddings via the CPU sidecar |
| `POST /v1/audio/speech`, `/v1/audio/transcriptions`, … | audio sidecar |
| `GET/POST /v1/status` | loaded profiles + all loadable names |
| `POST /v1/load[/{name}]` | load profiles (additive) |
| `POST /v1/unload[/{name}]` | unload profiles (release weights) |

**Dashboard/monitoring:**

| Route | Purpose |
|---|---|
| `GET /healthz` | gateway liveness |
| `GET /monitor/jobs` | job registry (active + history) |
| `GET /monitor/events` | SSE stream for the live view |
| `POST /monitor/jobs/{rid}/cancel`, `/monitor/cancel_all` | cancellations |
| `GET /monitor/stats` | time series, percentiles, windows |
| `GET /monitor/priority` | read/write Max prioritisation |
| `GET /monitor/hindsight` | read/write the Hindsight blocker |
| `GET /monitor/server` | backend states, health, transition |
| `POST /monitor/server/start`, `/stop` | start/stop a backend |
| `GET /` | built React web app (`aiserver_monitor/static/`) |

---

## 5. Chat backends

All backends sit behind **the same port 30001**. Exactly one runs; a switch is
a stop/start pair. The registry lives in
[`serverctl.py`](../aiserver-monitor/aiserver_monitor/serverctl.py)
(`BACKEND_NAMES`, `Backend` dataclass), the start/stop scripts in `runtime/`.

| Name | Engine | Model | Context | Parallelism | Health | Start/stop |
|---|---|---|---:|---:|---|---|
| `qwen` | SGLang (escha fork) | `Qwen3.8-27B-Escha-W2` (2-bit, ~10 GB) | 65,536 | 1 (SERIAL) | `/health` | `escha-server.service` |
| `flint` | Ollama (own process) | `hf.co/AtomicChat/Flint-GGUF:IQ4_XS` | 32,768 | 2 | `/api/version` | `runtime/flint/*.sh` |
| `gptoss` | Ollama | `gpt-oss:20b` | 65,536 | 1 | `/api/version` | `runtime/gptoss/*.sh` |
| `abl` | Ollama | `Qwen3-14B-abliterated-GGUF:Q4_K_M` | 40,960 | 1 | `/api/version` | `runtime/abl/*.sh` |
| `edge0` | edge0-infer + edge0-strata | `Edge0-120B-NVFP4` (~33 GB of experts on NVMe) | 131,072 | 1 | `/health` | `runtime/edge0/*.sh` |

### 5.1 Qwen / Escha (SGLang)

The original runtime: the `escha` SGLang fork with the 2-bit
`EschaLabs/Qwen3.8-27B-Escha-W2` model and the **overthinking penalty**. It
penalises hesitation markers ("Wait", "But", "Alternatively") and stops the
quantised reasoning model from talking itself out of an answer it already
found. It is enabled via `OTP=1` (server-default patch) plus `otp.json`;
details in [`runtime/sglang/`](../runtime/sglang/README.md).

Important launcher defaults (`runtime/sglang/serve.sh`):

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | 30001 | upstream port |
| `CTXLEN` | 65536 | per-request context cap (the model could do 262,144) |
| `MEM` | 0.72 | `mem-fraction-static`; the rest is for CUDA graphs and KV |
| `CHUNK` | 2048 | `chunked-prefill-size` against activation spikes |
| `SERIAL` | 1 | single-flight: one generation at a time (multi-agent consistency) |

Qwen runs through the user unit `escha-server.service`
(`ExecStart=runtime/sglang/start-penalty.sh start-systemd 2.0`). It is
currently **failed/stopped** — Edge0 has effectively replaced it.

### 5.2 Flint, GPT-OSS, Abliterated (Ollama)

All three are **dedicated `ollama serve` processes** listening on 30001 — not
the system Ollama on 11434. Benefits: own model store, own context/parallelism
values, no foreign service to reconfigure. The system Ollama is only used
**read-only** via `OLLAMA_NOPRUNE=true` and is unloaded before a start so that
no foreign model blocks VRAM.

- **Flint** — 2 parallel slots at 32k, measured ~70 tok/s per request,
  ~130 tok/s combined (`FLINT_PARALLEL=2`, `keep_alive=-1`).
- **GPT-OSS 20B** — 64k, 1 slot. The gateway sets `temperature: 0` and
  `reasoning_effort: "low"` when the client does not specify them.
- **Abliterated** — Qwen3-14B without refusal behaviour, 40k, 1 slot;
  `reasoning_effort` is removed because Ollama does not know it for this model.

Start/stop run through `runtime/<backend>/start-*.sh` with a PID file in
`runtime/<backend>/*.pid`. `gptoss` and `abl` only export their
`*_CTX`/`*_MODEL` values and call the Flint script — one implementation,
three profiles.

### 5.3 Edge0 120B (NVFP4)

Edge0 is neither an Ollama nor an SGLang model but its own stack:

```
edge0-infer serve            ← HTTP frontend, OpenAI API, prefix cache
   └── edge0-strata          ← engine (MoE expert streaming)
         └── /mnt/models/Edge0-120B-NVFP4   ← ~33 GB of experts on NVMe
```

The model is a 120B MoE in NVFP4. Only part of the experts fits into 16 GB of
VRAM; the rest is DMA-loaded from NVMe. Tuning in
[`runtime/edge0/start-edge0.sh`](../runtime/edge0/start-edge0.sh):

| Argument | Value | Effect |
|---|---|---|
| `--context` | 131,072 | large context for long Hermes system prompts |
| `--vram-reserve-mib` | 400 | headroom for the driver |
| `--kv-resident` | 20,480 | KV cache in VRAM |
| `--expert-cache-per-layer` | on | expert slots per layer instead of global |
| `--kv-grow` | on | KV grows with demand |
| `--pool-workers` | 7 | 8 cores minus the host thread |
| `--pcie-mode dma` | on | direct NVMe→VRAM transfer |
| `--resident-budget-gib` | 16 | VRAM budget |

Measured on this machine: **~60–70 tok/s decode**, **~1,900 tok/s prefill**,
**prefix cache** engages on a stable prompt prefix (identical 83.7k prompt:
second run 1.7 s instead of 43.7 s). That is why Edge0 beats Escha for Hermes
with a long, stable system prompt, whereas Escha re-processes every prompt.

Stopping is special: `edge0-infer` starts `edge0-strata` as a child process.
Both run in their own process group via `setsid`; the stop script kills the
**whole group** (`kill -- -$pid`), otherwise strata would keep ~15 GB of VRAM.

#### Thinking control (`edge0_thinking`)

On the OpenAI route Edge0 thinks up to 48,000 tokens on its own before the
first visible token, and `max_tokens` does **not** cut that block. A single
request therefore blocks the only slot for minutes. The gateway intervenes in
two places:

| Place | Effect |
|---|---|
| `runtime/edge0/start-edge0.sh` / model template `reasoning_budget_tokens` | hard cap on the thinking block, also for clients that bypass the gateway |
| `edge0_max_tokens` (default 8192) | caps `max_tokens` so one answer cannot reach 64k tokens and 15 minutes |

Four modes for `edge0_thinking`:

| Mode | Behaviour |
|---|---|
| `off` | `reasoning_effort: "none"`, no thinking block |
| `low` | `reasoning_effort: "low"` + `reasoning_budget_tokens` (default 1024) |
| `on` | override nothing – model/client default |
| `auto` | escalate on repetition (below) |

`auto` (`aiserver_monitor/thinking_auto.py`) counts how often the same message
history arrives within 300 s. The key is a hash over `messages`: every agent
step appends a tool result and is therefore a new history, while a retry sends
the same history again.

| Attempt | Tier | Budget |
|---|---|---|
| 1 | `none` | – |
| 2 | `low` | 1024 |
| 3+ | `medium` | 8192 |

So the first attempt answers immediately, and only when the agent repeats the
same step – the signal that the last attempt was not enough – does it get
thinking time. A `thinking: true` in the request overrides every mode (opt-in
for hard tasks).

What does **not** help: Laya, Jev or a trained router as a classifier in front
of the request. On 42 real German agent requests `convaiinnovations/laya`
scored 0.43 and `reasoning-router-mmbert-small` 0.55 accuracy – the majority
baseline ("never think") sits at 0.52. Terse German agent language carries too
little signal for an encoder.

### 5.4 The backend state machine

`ServerControl.snapshot()` reports `running`, `starting`, `stopping` or
`stopped` per backend:

- **qwen** is driven through its systemd unit (`unit` set).
- All others are driven through PID file + health path.
- During a switch a transition (`starting`/`stopping`) overlays the derived
  state so the dashboard does not flicker.
- `start`/`stop` invoke the scripts through a spawner (fire-and-forget); the
  caller waits for health with a timeout (900 s start, 90 s stop).

---

## 6. The model-name alias

Clients keep sending **`escha-qwen38-27b-w2`** regardless of which backend is
running. The gateway resolves that in three places:

1. **Request rewriting** (`_modell_anpassen`): if the body carries a known
   name (`model_alias`, `flint_model`, `gptoss_model`, `abl_model`,
   `edge0_model`), it is replaced by the **currently running** model. The
   active model comes from the running backend's PID file
   (`BackendProbe._pidfile_modell`), with `/api/ps` as fallback for Ollama.
   The dashboard switch is therefore authoritative — not the client.
2. **`/v1/models`** (`_modelle_mit_alias`): the upstream list is extended with
   `edge0-120b` and the alias so clients can discover the name too.
3. **Embeddings** (`_embedding_modell_anpassen`): chat alias, Flint/GPT-OSS/
   Abl/Edge0 name and `embed_model` all map onto the single embedding model.

Consequence: an agent can move from Escha to Edge0 without a config change.
The switch is only visible in the response's `model` field (`edge0-120b`).

---

## 7. Sidecars: embeddings, audio, TTS

Sidecars are independent processes with their own lifecycle. The gateway unit
starts the CPU sidecars via `ExecStartPre` (prefixed with `-` so a failure can
never block chat); the GPU TTS workers are loaded through the lifecycle API.

### 7.1 Embeddings — port 30002 (CPU)

A dedicated `ollama serve` with `qwen3-embedding:0.6b`. `CUDA_VISIBLE_DEVICES`
is empty, Ollama falls back to the CPU, `keep_alive=-1` keeps the model warm.
Embeddings therefore run **in parallel** with the chat backend without
competing for VRAM. `/v1/embeddings` is routed there.

### 7.2 Audio — port 30003 (CPU)

Package `aiserver_audio`: Whisper (STT) and Piper (TTS) behind one FastAPI app.
The gateway passes `/v1/audio/*` through **streamed**, never buffering audio
files; upload limit 100 MB, timeout 1800 s. Optionally a Chatterbox worker on
30004 is addressed as an additional TTS voice.

### 7.3 GPU TTS — ports 30005 / 30006

Two TTS models that **need VRAM** and therefore do not run permanently:

| Profile | Port | Model | Note |
|---|---:|---|---|
| `Qwen3-TTS` | 30005 | Qwen3-TTS | worker process, health `/healthz` |
| `Audio8-Realistic-Air` | 30006 | Audio8, 0.6B, 23 languages, 44.1 kHz | fixed male reference voice (`de_male`) |

Both are started via `/v1/load` (which unloads the chat backend first) and
stopped on `/v1/unload` so the CUDA context disappears. The `ProfileCatalog`
in `lifecycle.py` lists them with `kind="tts"`.

### 7.4 Lifecycle model

`ProfileCatalog` is the single source of truth for loadable names:

```
(chat)      model_alias → qwen · flint_model → flint · gptoss_model → gptoss
            abl_model → abl · edge0_model → edge0
(tts)       qwen_tts_model → qwen-tts · audio8_model → audio8
(embedding) embed_model → embed
```

`/v1/load` is **additive** and idempotent: whatever already runs stays.
`/v1/unload` releases weights — for `qwen`/`edge0` by stopping the process,
for Ollama via `keep_alive: 0`, for TTS by stopping the worker.

---

## 8. GPU ownership and VRAM arbitration

There is exactly **one** owner of the GPU at a time. Three mechanisms work
together:

1. **One upstream port.** Two chat backends cannot listen on 30001 at once.
   Switching is structurally exclusive.
2. **Ollama unloads foreign models.** `start-*.sh` queries
   `127.0.0.1:11434/api/ps` and sends `keep_alive: 0` for everything loaded
   there. Otherwise the start would fail for lack of VRAM.
3. **Qwen ↔ Edge0 mutually.** Both keep their weights inside their own process
   and do not fit side by side. `Lifecycle._ensure_chat` therefore stops a
   running process backend before starting the other
   (`_PROZESS_BACKENDS = ("qwen", "edge0")`).
4. **TTS after chat.** The GPU TTS workers start only after the chat backend
   has been unloaded and are stopped on unload.

Real-world occupancy:

| State | VRAM |
|---|---|
| Idle (sidecars only) | ~0–2 MiB |
| Edge0 120B | ~15.8 GB of 15.9 GB |
| Qwen/Escha | ~10 GB weights + KV/CUDA graphs (MEM=0.72) |
| Flint/GPT-OSS/Abl | depends on the model, one slot |

The `edge0.service` systemd unit (port 8094) is **disabled**. It was the
original standalone launcher; today the monitor is the sole owner, otherwise
two instances would fight over the same GPU. `/home/user/start-edge0.sh`
remains available as a manual standalone start.

---

## 9. Data and telemetry

SQLite at `aiserver-monitor/data/monitor.db` (path from `db_path`). Three
tables:

| Table | Content | Retention |
|---|---|---|
| `jobs` | one row per chat request: `rid`, source, model, prompt start, status, timestamps (created/first token/finished), token counts, error | 30 days |
| `samples` | time series at poll interval: upstream `running`/`waiting`/`throughput`/`token_usage`, GPU utilisation and VRAM | 7 days |
| `settings` | JSON values for dashboard switches (priority, Hindsight) | — |

**Collector** (`collector.py`): every `poll_interval_s` (2 s) it reads upstream
metrics and `nvidia-smi`
(`utilization.gpu,memory.used,memory.total`), stores a sample and pushes it to
the SSE hub. Errors become `None` values — a hanging `nvidia-smi` must never
block the gateway (own timeout).

**History** (`history.py`): writes jobs, aggregates time series, computes
percentiles (nearest rank, no interpolation) and prunes old rows.
`/monitor/stats` serves windows, throughput, TTFT and error sources.

**Priority and Hindsight** (`priority.py`, `hindsight.py`): both recognise the
Max agent by markers in `role: system` messages (default
`You are Max, built for Marec`). Priority assigns `priority: 100` to Max and
normal priority to everyone else; the Hindsight blocker rejects matching
requests immediately without touching the backend. Both are switchable in the
dashboard and persisted in `settings`.

---

## 10. Dashboard (web app)

React + TypeScript + Vite in [`aiserver-monitor/webapp/`](../aiserver-monitor/webapp/).
The build lands in `aiserver_monitor/static/` (gitignored) and is served by the
gateway at `/`. No separate web server, no second port.

| View | Content |
|---|---|
| `LiveView` + `CurrentJobPanel` + `JobTable` | running and past jobs, live stream via SSE, prompt preview, cancellation |
| `LiveInsights` | derived metrics from the live stream |
| `StatsView` + `TimeChart` | windows, throughput, TTFT, GPU/VRAM history, error distribution |
| `ServerView` | backend tiles with state and health, start/stop/switch, transitions |
| `PriorityView` | Max prioritisation: markers, on/off |
| `PreviewModal` | full prompt/response of a job |

The web app only talks to the same origin (`/monitor/*`, `/v1/*`). SSE
(`/monitor/events`) carries job updates and samples; the connection indicator
distinguishes `verbinde`, `verbunden`, `getrennt` (connecting, connected,
disconnected).

---

## 11. Operations: systemd, `server.sh`, logs

### 11.1 systemd user units

| Unit | Role | Note |
|---|---|---|
| `aiserver-monitor.service` | gateway + CPU sidecars | `Restart=always`; `ExecStartPre=-` starts embed/audio/Chatterbox; `ExecStopPost=-` stops them; `TimeoutStopSec=15` with `SuccessExitStatus=SIGKILL` because open SSE streams can stall shutdown |
| `escha-server.service` | SGLang/Qwen | `Restart=always`, `RestartSec=15`; started via `start-penalty.sh start-systemd 2.0`; log to `/tmp/escha_server.log` |
| `slotmon-monitor.service` | GPU slot monitor | passively tails the SGLang log, port 8090 |
| `edge0.service` | legacy port 8094 | **disabled** |

The gateway unit file lives in the repo
([`aiserver-monitor/systemd/`](../aiserver-monitor/systemd/)); the SGLang units
live under `runtime/sglang/`.

### 11.2 `server.sh`

A thin wrapper around `python -m aiserver_monitor.serverctl`; it uses
**the same** logic as the dashboard's "Server" tab.

```bash
bash server.sh start [qwen|flint|gptoss|abl|edge0]
bash server.sh stop  [backend]
bash server.sh switch <backend>     # implicit start; the old process yields
bash server.sh restart [backend]
bash server.sh status
```

`start` waits up to 900 s for health (Edge0/Qwen need 2–4 min, Ollama backends
a few seconds), `stop` up to 90 s. The gateway is never touched by this.

### 11.3 Logs and PID files

| What | Where |
|---|---|
| gateway (rotating) | `aiserver-monitor/data/aiserver-monitor.log` |
| Qwen/Escha | `/tmp/escha_server.log` |
| Flint / GPT-OSS / Abl | `/tmp/flint_server.log` (shared) |
| Edge0 | `/tmp/edge0_server.log` |
| embeddings | `/tmp/embed_server.log` |
| PID files | `runtime/<backend>/*.pid` (gitignored) |

---

## 12. Configuration

Order: **defaults < TOML < environment variables**.

- TOML file via `AISERVER_MONITOR_CONFIG=/path/config.toml`.
- Every field individually via `AISERVER_MONITOR_<FIELDNAME>` (uppercase),
  e.g. `AISERVER_MONITOR_LISTEN_PORT=39999`.
- Empty values count as unset.
- `Config.__post_init__` rejects non-positive intervals and empty required
  fields — `heartbeat_s=0` would otherwise become a busy loop.

The full, commented list is in
[`config.example.toml`](../aiserver-monitor/config.example.toml). Main groups:
network, embeddings, lifecycle, Qwen3-TTS, Audio8, audio, paths/retention,
server control, the five chat backends, Chatterbox.

---

## 13. Security and network exposure

- **Only 30000 is public.** The upstream and all sidecars bind to
  `127.0.0.1`. There is no authentication — the network topology is the
  protection.
- **No secrets in the repo.** API keys, if needed, belong in the unit or an
  untracked TOML; `otp.json` (penalty payload) is gitignored.
- **Model downloads** go through Hugging Face; the scripts in `runtime/*/`
  document the expected paths under `/mnt/models`.
- **The foreign Ollama** (11434) is used read-only (`OLLAMA_NOPRUNE`), never
  written to.

---

## 14. Tests and quality assurance

`python -m unittest discover -s tests` from `aiserver-monitor/`.

- **596 tests**, currently green.
- No real server, no real GPU: runner (systemctl/pgrep), spawner (start/stop
  scripts), health reader and `httpx` transport are injected.
- Covered: alias rewriting, `/v1/models` enrichment, job status, cancellations,
  non-stream reassembly, keepalives, history/retention, priority, Hindsight,
  backend state machine, transitions, `ProfileCatalog`, lifecycle
  load/unload, TTS workers.
- E2E spec for the dashboard: `tests/e2e/dashboard.spec.py`.
- Web app build: `npm run build` in `aiserver-monitor/webapp/` (TypeScript
  check + Vite build).

---

## 15. Directory layout

```
small_qwen/
├── server.sh                     # CLI: start/stop/switch/status/restart
├── README.md / README.de.md      # entry point, quick start, docs map
├── docs/                         # technical deep dives (this document)
├── aiserver-monitor/
│   ├── aiserver_monitor/         # gateway (FastAPI, proxy, lifecycle, …)
│   ├── aiserver_audio/           # CPU sidecar: Whisper + Piper + Chatterbox
│   ├── aiserver_qwen_tts/        # GPU TTS worker Qwen3-TTS
│   ├── aiserver_audio8_tts/      # GPU TTS worker Audio8
│   ├── aiserver_chatterbox/      # CPU TTS worker
│   ├── webapp/                   # React/Vite dashboard
│   ├── systemd/                  # gateway unit
│   ├── tests/                    # unittest suite + E2E spec
│   ├── config.example.toml       # every knob, commented
│   └── data/                     # SQLite + log (gitignored)
├── runtime/
│   ├── sglang/                   # escha/SGLang: serve.sh, penalty, units
│   ├── flint/ gptoss/ abl/       # Ollama backends (shared script)
│   ├── edge0/                    # edge0-infer + edge0-strata
│   ├── embed/                    # CPU Ollama for embeddings
│   ├── audio/                    # Whisper/Piper
│   ├── chatterbox/               # CPU TTS
│   ├── qwen-tts/ audio8/         # GPU TTS with their own venv
│   └── …
├── slotmon/                      # passive GPU slot monitor
├── ctxmon.sh                     # tok/s and context monitor
└── Qwen3.8-27B-Escha-W2/         # model weights (not part of the repo)
```

---

## 16. Known quirks and limits

- **Escha is currently stopped** (`escha-server.service` failed). Edge0 has
  taken over the role; the alias keeps working unchanged.
- **Edge0 loads 33 GB of experts from NVMe.** The first start takes 2–4 min;
  afterwards the prefix cache is what makes follow-ups fast. A changing prompt
  prefix costs a full prefill every time (~1,900 tok/s).
- **Ollama backends share one log** (`/tmp/flint_server.log`) because they use
  the same start script.
- **`pkill -f` is dangerous:** patterns like `strata`, `sglang` or
  `edge0-infer` also match the calling shell. Always stop via `server.sh`,
  `systemctl` or the PID file.
- **No multi-model operation.** Two chat models do not fit into 16 GB; the
  system is deliberately built around strict switching.
- **`/v1/models` is a snapshot.** The list names the active upstream name plus
  the stable gateway names; it does not say which weights are loaded. That is
  what `/v1/status` is for.
