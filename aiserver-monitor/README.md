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
starts and stops the CPU embedding sidecar together with the gateway, because
`/v1/embeddings` has no other upstream. The SGLang server is never started,
stopped or flipped by `run.sh`.

### Embeddings (CPU)

`POST /v1/embeddings` goes to `embed_base` (default
`http://127.0.0.1:30002`), an independent Ollama process with
`qwen3-embedding:0.6b`. It runs on the CPU and uses no VRAM, so it works while
Qwen, Flint or GPT-OSS occupies the GPU. The gateway treats it as a buffered
passthrough: no job tracking, no keepalives, response unchanged. Known model
names (Qwen alias, Flint, GPT-OSS, embedding model) and a missing/empty name are
mapped to `embed_model`; unknown names pass through unchanged, so Ollama can
answer with a clear "model not found".

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
  `server_start_script`, `server_stop_script`, `server_process_pattern`.

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
