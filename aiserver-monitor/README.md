<p align="right"><a href="README.de.md">🇩🇪 Deutsch</a> | <b>🇬🇧 English</b></p>

# aiserver-monitor

Gateway in front of the Escha-SGLang inference server. It forwards
OpenAI-style requests to SGLang (streaming included), keeps long waits alive
on the Cloudflare route, and serves a dashboard for queue state, live
per-job previews, single cancel / "cancel all" and statistics. One process,
one uvicorn worker.

Non-streaming chat requests (`stream=false`) are streamed internally: they
show up live in the dashboard (running, TTFT, growing previews, tokens) and
the cancel button really stops the running request; the client still
receives a valid chat completion JSON.

The planned rollout flips the ports: the gateway takes `0.0.0.0:30000` and
SGLang moves to `127.0.0.1:30001` (details in the design document, see
[Further reading](#further-reading)). Until the cutover nothing changes —
the gateway is only run on test ports (`39xxx`).

## Prerequisites

- Python 3.12 with the repository venv at `/home/user/small_qwen/.venv`
  (FastAPI, uvicorn and httpx are installed there).
- The SGLang server reachable at `upstream_base` (default
  `http://127.0.0.1:30001`). The gateway also starts while SGLang is down;
  requests then fail with clean errors.
- `nvidia-smi` for the GPU values (optional; without it the GPU fields stay
  "not measured").

## Start, stop, status

```bash
cd /home/user/small_qwen/aiserver-monitor
bash run.sh start      # start in the background, wait for /healthz
bash run.sh status     # exit 0 while running, exit 1 when not running
bash run.sh stop       # TERM, wait up to 20 s, then KILL
bash run.sh restart    # stop, then start
```

`start` and `stop` are idempotent: a second `start` prints "laeuft bereits"
and exits 0, a `stop` without a running gateway exits 0 as well. `run.sh`
starts and stops the CPU embedding and audio sidecars together with the
gateway, because `/v1/embeddings` and `/v1/audio/*` have no other upstream.
The SGLang server is never started, stopped or flipped by `run.sh`.

### Embeddings (CPU)

`POST /v1/embeddings` goes to `embed_base` (default
`http://127.0.0.1:30002`), an independent Ollama process with
`qwen3-embedding:0.6b`. It runs on the CPU and uses no VRAM, so it works while
Qwen, Flint, GPT-OSS or Abliterated occupies the GPU. The gateway treats it as
a buffered passthrough: no job tracking, no keepalives, response unchanged.
Known model names (Qwen alias, Flint, GPT-OSS, Abliterated, embedding model)
and a missing/empty name are mapped to `embed_model`; unknown names pass
through unchanged, so Ollama can answer with a clear "model not found".

```bash
curl http://127.0.0.1:30000/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model": "escha-qwen38-27b-w2", "input": "Hallo Welt"}'
```

The sidecar can also be controlled directly:

```bash
bash runtime/embed/start-embed.sh
bash runtime/embed/stop-embed.sh
```

### Model lifecycle (load/unload)

Pipeline clients can release and reload model weights between steps without
restarting the gateway:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/load` | load the named models; additive |
| `POST` | `/v1/unload` | unload the named models, or everything |
| `GET`/`POST` | `/v1/status` | loadable names and currently loaded profiles |

`POST /v1/load` accepts `{"model": "..."}`, `{"models": ["...", ...]}` or a
bare JSON list. Names are used exactly as sent: they are the same strings the
client passes as `model` to `/v1/embeddings`, `/v1/chat/completions`, etc. The
gateway maps each name to its configured backend and never rewrites or trims
it. An unknown name returns 404 and loads nothing.

For clients that address a single model in the path, `POST /v1/load/<name>`
and `POST /v1/unload/<name>` are equivalent aliases (`<name>` may contain
`:` and `/`).

Only one chat backend can be loaded at a time; requesting two returns 409.
The embedding model can run alongside a chat model. Loading a chat model
requires `server_control_enabled`, otherwise the request returns 503.
`POST /v1/unload` without a body, with `{}` or `{"all": true}` unloads every
loaded profile; with `model`/`models` it unloads only those. Unloading stops
Qwen (the GPU server) and the GPU TTS workers (Qwen3-TTS and Audio8-TTS), but
for Flint, GPT-OSS, Abliterated and embeddings it only sets Ollama
`keep_alive: 0`; those processes stay up for the next load.

```bash
curl http://127.0.0.1:30000/v1/load \
  -H 'Content-Type: application/json' \
  -d '{"models": ["qwen3-embedding:0.6b"]}'
curl http://127.0.0.1:30000/v1/status
curl -X POST http://127.0.0.1:30000/v1/unload
# Path-style alias used by some pipeline clients:
curl -X POST http://127.0.0.1:30000/v1/load/Qwen3-TTS
```

`/v1/status` returns `profiles` (all loadable names) and `loaded` entries with
`profile`, `backend` and `kind` (`chat`, `tts` or `embedding`).

### Audio (CPU)

Whisper speech-to-text and Piper text-to-speech run in an independent FastAPI
sidecar on `audio_base` (default `http://127.0.0.1:30003`). The gateway only
streams between client and sidecar; it keeps no audio in memory and creates no
monitor job.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/audio/models` | installed Whisper models and Piper voices |
| `POST` | `/v1/audio/transcriptions` | multipart upload, returns text/JSON/SRT/VTT |
| `POST` | `/v1/audio/speech` | JSON text, returns streamed audio |

Whisper models: `whisper-small` (default, 484 MB), `whisper-medium`
(1528 MB) and `whisper-large-v3-turbo` (1618 MB). Piper voices:
`de_DE-thorsten-medium` (`thorsten`, default), `de_DE-kerstin-low`
(`kerstin`) and `de_DE-eva_k-x_low` (`eva`). Model aliases are accepted, e.g.
`whisper-1`, `small`, `medium`, `turbo`, `large-v3-turbo`. Optional GPU TTS
models are documented below: Qwen3-TTS and `Audio8-Realistic-Air` (0.6B,
23 languages, 44.1 kHz).

```bash
# One-time setup and model download (from the repo root)
bash runtime/audio/setup.sh
bash runtime/audio/download-models.sh --all     # ~3.8 GB
bash runtime/audio/download-models.sh --check   # exit 0 when complete

# Or through run.sh
bash run.sh audio-setup
bash run.sh audio-models --all
bash run.sh audio-models --check
```

Transcription fields: `file` (required), `model` (default `whisper-small`),
`language` (default `de`, `auto` allowed), `response_format`
(`json`/`text`/`verbose_json`/`srt`/`vtt`), `prompt`, `temperature` and
`task` (`transcribe`; `translate` returns 400).

```bash
curl http://127.0.0.1:30000/v1/audio/transcriptions \
  -F file=@satz.wav -F model=whisper-small -F language=de
```

Speech fields: `input` (required, max 5000 characters), `model`
(`piper`/`tts-1`/`tts-1-hd`), `voice` (name or alias), `response_format`
(`pcm`, `wav`, `mp3`, `opus`) and `speed` (0.5–2.0). Audio is synthesised
sentence by sentence and the response starts with the first sentence, not
after the whole text. `wav` uses a streaming header with a placeholder size;
some players reject that, so `pcm` and `mp3` are the safer streaming formats.

```bash
curl http://127.0.0.1:30000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input": "Guten Tag. Wie geht es Ihnen?", "voice": "thorsten", "response_format": "mp3"}' \
  -o antwort.mp3
```

Limits and errors: upload 100 MB, audio 60 minutes, TTS text 5000 characters,
queue 8 per endpoint. A missing model returns `503` with the matching
`runtime/audio/download-models.sh` command, an unknown model/voice `404`, a
full queue `429` with `Retry-After`, a stopped sidecar `502` and a timeout
`504`. Bad multipart input or an unsupported format is `400`.

Settings can be changed with `AUDIO_*` environment variables before starting
the sidecar: `AUDIO_HOST`, `AUDIO_PORT`, `AUDIO_MODELS_DIR` (default
`/mnt/models/audio`), `AUDIO_THREADS` (8), `AUDIO_PRELOAD` (`whisper-small`,
empty disables), `AUDIO_MODEL_CACHE` (1), `AUDIO_MAX_QUEUE` (8),
`AUDIO_STT_TIMEOUT` (1800), `AUDIO_TTS_TIMEOUT` (120) and
`AUDIO_MAX_UPLOAD_MB` (100). Gateway-side knobs are `audio_base`,
`audio_timeout_s` and `audio_max_upload_mb`.

Direct control and smoke test:

```bash
bash runtime/audio/start-audio.sh
bash runtime/audio/selftest.sh
bash runtime/audio/stop-audio.sh
```

Audio and transcripts are never written to the log; only kind, model,
duration, real-time factor and status are recorded. The default language is
standard German. Austrian and Swiss accents are usually recognised, dialect
is not guaranteed, and Piper has no AT/CH voices.

### Switching the server on/off

The model server can be stopped and started again from the dashboard's
**Server** tab. The same path in a terminal (repo root):

```bash
bash server.sh status   # state + /health
bash server.sh stop     # stop the model server; dashboard stays up
bash server.sh start    # start and wait for /health (2-4 minutes)
bash server.sh restart
```

If the `escha-server.service` systemd user unit is installed, `start`/`stop`
go through `systemctl --user` (with `--no-block`); only that keeps the
unit's `Restart=always` from undoing a deliberate shutdown. Without the unit
it falls back to `runtime/sglang/start-penalty.sh` / `stop-server.sh`.
Stopping is temporary: the unit stays enabled, so the server starts again
after a reboot. The controls can be disabled with
`server_control_enabled = false` (or
`AISERVER_MONITOR_SERVER_CONTROL_ENABLED=false`).

`run.sh` environment overrides:

| Variable | Default | Meaning |
|---|---|---|
| `PYTHON` | `/home/user/small_qwen/.venv/bin/python` | interpreter for `python -m aiserver_monitor` |
| `AISERVER_MONITOR_LISTEN_PORT` | `30000` | port for the health poll (`/healthz`); also the app port unless configured otherwise |
| `AISERVER_MONITOR_EMBED_BASE` | `http://127.0.0.1:30002` | upstream for `/v1/embeddings` (CPU Ollama); also used by `run.sh status` |
| `PIDFILE` | `/tmp/aiserver-monitor.pid` | PID file for `stop` |
| `AISERVER_MONITOR_RUN_LOG` | `/tmp/aiserver-monitor-run.log` | stdout/stderr log of the background process |
| `AISERVER_MONITOR_PATTERN` | `[a]iserver_monitor` | `pgrep -f` pattern (empty values are rejected, the default applies) |
| `STOP_GRACE` | `20` | seconds between TERM and KILL |
| `START_WAIT` | `20` | seconds to wait for `/healthz` on start |

Foreground start (development, systemd):

```bash
cd /home/user/small_qwen/aiserver-monitor
/home/user/small_qwen/.venv/bin/python -m aiserver_monitor
```

The entry point reads `AISERVER_MONITOR_CONFIG` (path to a TOML file,
optional), writes a rotating log to `log_path` (5 MB per file, 3 backups)
and serves `/healthz` for liveness.

## Dashboard build (webapp)

The dashboard is a Vite/React app in `webapp/`; the build is not committed.
Without it the gateway serves a small placeholder page. Build it once (and
after every webapp change):

```bash
cd webapp
npm install
npm run build
```

Requires Node 22+. The build writes to `aiserver_monitor/static/`, which is
gitignored and must not be committed. `tests/e2e/run_e2e.sh` runs the build
automatically before the browser scenarios.

## systemd (user unit)

`systemd/aiserver-monitor.service` is a user unit:

```ini
[Unit]
Description=aiserver-monitor gateway (OpenAI passthrough + dashboard)

[Service]
WorkingDirectory=/home/user/small_qwen/aiserver-monitor
ExecStart=/home/user/small_qwen/.venv/bin/python -m aiserver_monitor
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

Install it (copy or symlink) and enable it:

```bash
mkdir -p ~/.config/systemd/user
cp /home/user/small_qwen/aiserver-monitor/systemd/aiserver-monitor.service \
   ~/.config/systemd/user/
# or: ln -s /home/user/small_qwen/aiserver-monitor/systemd/aiserver-monitor.service \
#         ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now aiserver-monitor.service
```

Check with `systemctl --user status aiserver-monitor.service` and follow the
console output with `journalctl --user -u aiserver-monitor.service -f`. To
keep the unit running while logged out / start it at boot, enable lingering
once: `loginctl enable-linger $USER`. The unit starts the app directly; do
not run `run.sh start` while the unit is active.

## Configuration

Sources, weakest first: built-in defaults < TOML file < environment
variables.

- **Environment:** every `Config` field can be set as
  `AISERVER_MONITOR_<FIELD>` (field name in upper case), e.g.
  `AISERVER_MONITOR_LISTEN_PORT=39999` or
  `AISERVER_MONITOR_UPSTREAM_BASE=http://127.0.0.1:39099`. An empty value
  counts as not set.
- **TOML file:** set `AISERVER_MONITOR_CONFIG=/path/to/config.toml` (empty
  or unset = no file). The file wins over defaults, the environment wins
  over the file.
- **[`config.example.toml`](config.example.toml)** lists every field
  commented out with its default: `listen_host`, `listen_port`,
  `upstream_base`, `db_path`, `log_path`, `heartbeat_s`, `poll_interval_s`,
  `connect_timeout_s`, `preview_max_chars`, `prompt_start_chars`,
  `retention_jobs_days`, `retention_series_days`, `nonstream_keepalive`,
  `gpu_cmd`, `abort_on_disconnect`, `server_control_enabled`, `server_unit`,
  `server_start_script`, `server_stop_script`, `server_process_pattern`,
  `embed_base`, `embed_model`, `embed_start_script`, `lifecycle_timeout_s`,
  `lifecycle_unload_timeout_s`, `lifecycle_poll_s`, `qwen_tts_model`,
  `qwen_tts_base`, `qwen_tts_model_dir`, `qwen_tts_start_script`,
  `qwen_tts_stop_script`, `qwen_tts_pidfile`, `qwen_tts_health_path`,
  `qwen_tts_load_timeout_s`, `qwen_tts_unload_timeout_s`, `audio8_model`,
  `audio8_base`, `audio8_model_dir`, `audio8_reference_audio`,
  `audio8_reference_text`, `audio8_start_script`, `audio8_stop_script`,
  `audio8_pidfile`, `audio8_health_path`, `audio8_load_timeout_s`,
  `audio8_unload_timeout_s`, `audio_base`, `audio_timeout_s`,
  `audio_max_upload_mb`.

By default (`abort_on_disconnect = true`) the gateway watches the client
connection: if a client disappears while its job is still waiting, the
pending upstream call is cancelled and the job is finished as `aborted`
(best-effort `abort_request` at SGLang), while `false` restores the old
behaviour where every job runs to the end.

`db_path` and `log_path` are relative to the working directory. `run.sh`
changes into the project directory first, so the defaults resolve to
`aiserver-monitor/data/monitor.db` and
`aiserver-monitor/data/aiserver-monitor.log`.

## Operations and rollback

Until the cutover the running setup stays untouched: SGLang keeps its current
port and the existing slotmon service keeps running. Test the gateway on a
test port instead, e.g. `AISERVER_MONITOR_LISTEN_PORT=39055 bash run.sh start`
(with `AISERVER_MONITOR_UPSTREAM_BASE` pointing at a test upstream).

Rollback at any time: `bash run.sh stop` (or `systemctl --user disable --now
aiserver-monitor.service` after the cutover). SGLang keeps running where it
was — only the explicit server control (dashboard "Server" tab or
`server.sh`) flips it.

## Further reading

- Design (German): [`../docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md`](../docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md)
  (repo path `docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md`)
- Implementation plan (German):
  [`../docs/superpowers/plans/2026-10-13-aiserver-monitor.md`](../docs/superpowers/plans/2026-10-13-aiserver-monitor.md)

#### Chatterbox (optional, CPU)

`model=chatterbox` or `chatterbox-v3` routes TTS text to a separate worker on
`127.0.0.1:30004`; voice `default` is the fixed checkpoint voice. The worker
uses its own venv under `runtime/chatterbox/venv` because it needs torch 2.6
and numpy < 2. Setup and models:

```bash
bash run.sh chatterbox-setup
bash run.sh chatterbox-models
bash run.sh status
```

Models live in `/mnt/models/audio/chatterbox` (~3 GB). Piper remains the
default. The first request loads the model (~8 s); subsequent synthesis is
about 2.6x slower than realtime on eight CPU threads. Formats, limits and
error codes are shared with Piper; a stopped worker returns `502` and a
timeout `504`.

#### Qwen3-TTS (optional, GPU)

`model=Qwen3-TTS` (aliases `qwen3-tts`, `qwen3-tts-1.7b`,
`qwen3-tts-12hz-1.7b-customvoice`) routes TTS text to a separate worker on
`127.0.0.1:30005`. The 1.7B CustomVoice checkpoint runs on `cuda:0`; there is
no CPU fallback. The chat backend must be unloaded first, so a pipeline step
looks like this:

```bash
curl -X POST http://127.0.0.1:30000/v1/unload
curl -X POST http://127.0.0.1:30000/v1/load \
  -H 'Content-Type: application/json' -d '{"model": "Qwen3-TTS"}'
curl http://127.0.0.1:30000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "Qwen3-TTS", "input": "Guten Tag.", "voice": "Vivian", "language": "German", "response_format": "wav"}' \
  -o antwort.wav
curl -X POST http://127.0.0.1:30000/v1/unload
```

Setup and model download (the checkpoint may already be under
`/mnt/models/audio/experiments/qwen`):

```bash
bash run.sh qwen-tts-setup
bash run.sh qwen-tts-models
bash run.sh status
```

Voices: `Vivian` (default), `Serena`, `Uncle_Fu`, `Dylan`, `Eric`, `Ryan`,
`Aiden`, `Ono_Anna`, `Sohee`. `language` defaults to `German`; `instruct` is
optional. `speed` is accepted for compatibility but currently ignored by
Qwen3-TTS. A missing checkpoint returns `503` with the
`runtime/qwen-tts/download-models.sh` hint, a running chat backend `409`, an
unreachable worker `502` and a timeout `504`. `/v1/unload` stops the worker
process so the CUDA context is released.

#### Audio8-TTS (optional, GPU)

`model=Audio8-Realistic-Air` (aliases `audio8`, `audio8-realistic-air`,
`audio8-tts-realistic-air`, `audio8-tts`) routes TTS text to a separate worker
on `127.0.0.1:30006`. The 0.6B model covers 23 languages and uses a 44.1 kHz
codec. Its only voice, `de_male`, is a fixed male reference voice; replace it
with your own recording via `AUDIO8_REFERENCE_AUDIO` and
`AUDIO8_REFERENCE_TEXT` (WAV plus exact transcript).

Audio8 needs the GPU, so unload the chat backend first:

```bash
curl -X POST http://127.0.0.1:30000/v1/unload
curl -X POST http://127.0.0.1:30000/v1/load \
  -H 'Content-Type: application/json' -d '{"model": "Audio8-Realistic-Air"}'
curl http://127.0.0.1:30000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "Audio8-Realistic-Air", "input": "Guten Tag.", "voice": "de_male", "language": "German", "response_format": "wav"}' \
  -o antwort.wav
curl -X POST http://127.0.0.1:30000/v1/unload
```

Setup, download and reference voice:

```bash
bash run.sh audio8-setup
bash run.sh audio8-models
bash run.sh status-audio8
# or directly:
bash runtime/audio8/make-reference.sh
```

`run.sh start` deliberately does not start the Audio8 worker; use
`run.sh start-audio8`, `run.sh stop-audio8` and `run.sh status-audio8`. Raw
`pcm` is resampled to the usual 24 kHz; `wav`, `mp3` and `opus` keep the native
44.1 kHz. A missing model or reference returns `503`, a running chat backend
`409`, an unreachable worker `502` and a timeout `504`. `/v1/unload` stops the
worker process so the CUDA context is released.
