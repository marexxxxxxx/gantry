<p align="right"><a href="docs/gantry.de.md">Deutsch</a> | <b>English</b></p>

# Gantry

Gantry is a local AI runtime gateway and dashboard. It exposes a single
OpenAI-compatible HTTP endpoint on port `30000`, routes each request to the
provider that owns the requested model, and manages the provider processes
through a file-based registry in `providers/*.toml`.

The gateway itself is small: FastAPI + uvicorn, one process, one worker. The
actual model servers stay separate (SGLang/escha, Ollama, Edge0/strata, or any
custom command). Gantry starts and stops them on demand, keeps their logs
available, and never requires clients to switch ports or restart when a model
changes.

What it provides:

- **One client port** (`127.0.0.1:30000`) for chat, completions, embeddings,
  audio and model lifecycle requests.
- **Provider registry** as the single source of truth: every provider is one
  TOML file, editable with the CLI, the dashboard, or a text editor.
- **Per-provider routing**: the request's `model` is matched against a
  provider's `model`, `id`, or display `name`.
- **Auto-load and idle unload**: a stopped provider is started on its first
  request and can be stopped again after a configurable idle period.
- **Exclusive-GPU handling**: before an exclusive provider starts, other
  exclusive providers are stopped so two large models never share the GPU.
- **Dashboard** at `http://127.0.0.1:30000/` with system metrics, provider
  list, add/edit/rename/delete, start/stop and log tails.
- **CLI** through `./server.sh provider ...` and the backend controls
  `./server.sh start|stop|status|restart|switch ...`.

Gantry is the renamed continuation of the earlier `small_qwen` checkout. The
Python package is still called `aiserver_monitor` and the environment variables
still use the `AISERVER_MONITOR_` prefix. See
[Migration from small_qwen](#migration-from-small_qwen).

## Requirements

- Linux with systemd user units (recommended) or a shell for `run.sh`.
- Python 3.12 and a virtual environment at `/home/user/gantry/.venv`.
- The runtime for every provider you want to use. Gantry does not download or
  install model weights and does not start a model server that has no working
  `start_cmd`.
- Optional: `nvidia-smi` for GPU metrics in `/api/system` and the dashboard.
  Without it, the GPU fields stay empty and `/api/system` reports the reason
  under `errors`.

## Installation

```bash
cd /home/user/gantry
python3.12 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx
```

Optional CPU audio sidecar dependencies:

```bash
.venv/bin/pip install -r aiserver-monitor/requirements-audio.txt
```

The dashboard is part of the repository (`aiserver-monitor/aiserver_monitor/dashboard/`);
there is no frontend build step for the current dashboard.

## Starting Gantry

### systemd user unit

The repository unit is
[`aiserver-monitor/systemd/gantry.service`](aiserver-monitor/systemd/gantry.service).
It runs the gateway from `/home/user/gantry`, uses the repository venv, and
starts/stops the optional CPU embedding and audio sidecars around it.

```bash
mkdir -p ~/.config/systemd/user
cp /home/user/gantry/aiserver-monitor/systemd/gantry.service \
   ~/.config/systemd/user/gantry.service

systemctl --user daemon-reload
systemctl --user enable --now gantry.service
systemctl --user status gantry.service
journalctl --user -u gantry.service -f
```

To keep the user service running after logout and across boots:

```bash
loginctl enable-linger "$USER"
```

The unit starts `python -m aiserver_monitor` from
`/home/user/gantry/aiserver-monitor` with the repository venv. Sidecar
`ExecStartPre` entries use a leading `-`, so a missing embedding or audio
sidecar does not prevent the gateway from starting.

### run.sh (foreground manager)

```bash
cd /home/user/gantry/aiserver-monitor
bash run.sh start      # start in the background, wait for /healthz
bash run.sh status     # exit 0 while running, exit 1 when stopped
bash run.sh stop       # TERM, wait up to 20 s, then KILL
bash run.sh restart    # stop, then start
```

`run.sh` also starts and stops the CPU sidecars. Do not run it while
`gantry.service` is active; both would bind port `30000`.

For a foreground process without the sidecar handling:

```bash
cd /home/user/gantry/aiserver-monitor
../.venv/bin/python -m aiserver_monitor
```

Relevant environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `AISERVER_MONITOR_LISTEN_HOST` | `0.0.0.0` | Bind address |
| `AISERVER_MONITOR_LISTEN_PORT` | `30000` | HTTP port |
| `AISERVER_MONITOR_UPSTREAM_BASE` | `http://127.0.0.1:30001` | Default upstream for providers without a port |
| `GANTRY_PROVIDERS_DIR` | `<repo>/providers` | Alternative registry directory |
| `AISERVER_MONITOR_CONFIG` | unset | Optional TOML config file |
| `AISERVER_MONITOR_DB_PATH` | `data/monitor.db` | SQLite history |
| `AISERVER_MONITOR_LOG_PATH` | `data/aiserver-monitor.log` | Rotating gateway log |

See [`aiserver-monitor/config.example.toml`](aiserver-monitor/config.example.toml)
for the full list and defaults.

## Dashboard

Open `http://127.0.0.1:30000/` (or the host address if `listen_host` is
`0.0.0.0`). The dashboard shows:

- overall gateway status and provider count;
- GPU, RAM, swap and disk values from `/api/system`;
- the provider table with model, kind, port, status and uptime;
- provider creation and editing, including start/stop commands and settings;
- rename and delete actions;
- recent log lines for a provider.

The UI is served from `aiserver-monitor/aiserver_monitor/dashboard/`. A
liveness endpoint is available at `GET /healthz` and returns
`{"status": "ok"}`.

## Provider registry

The registry lives in [`providers/`](providers/). Each provider is one file:

```
providers/<id>.toml
```

The file name must match the provider `id`; IDs are lowercase letters, digits,
`-` and `_`, at most 64 characters. Duplicate IDs and mismatched file names are
configuration errors. The gateway, the dashboard, the API and `./server.sh`
all read the same files.

Minimal example:

```toml
id = "qwen"
name = "Qwen (SGLang)"
kind = "sglang"
enabled = true
host = "127.0.0.1"
port = 30001
model = "escha-qwen38-27b-w2"
health_url = "http://127.0.0.1:30001/health"
pidfile = ""
logfile = ""
workdir = "."
start_cmd = ["systemctl", "--user", "start", "escha-server.service"]
stop_cmd = ["systemctl", "--user", "stop", "escha-server.service"]

[settings]
context = 84000
max_tokens = 8192
auto_load = false
idle_unload_min = 0
exclusive_gpu = true
parallel = 1
```

Top-level fields:

| Field | Default | Meaning |
|---|---|---|
| `id` | file name | Provider ID; must match `<id>.toml` |
| `name` | `id` | Display name |
| `kind` | `custom` | `strata`, `ollama`, `sglang` or `custom` |
| `enabled` | `true` | Disabled providers are listed but never auto-loaded |
| `host` | `127.0.0.1` | Provider host |
| `port` | `0` | Provider port; `0` uses the gateway's `upstream_base` |
| `model` | empty | Exact model name accepted by the provider |
| `health_url` | derived | Absolute URL or path; default `/api/version` for Ollama and `/health` otherwise |
| `pidfile` | empty | Optional PID file, relative to the repository or absolute |
| `logfile` | empty | Log file used for `/api/providers/<id>/logs` and error tails |
| `workdir` | `.` | Working directory for `start_cmd`/`stop_cmd`; relative to the repository or absolute |
| `start_cmd` | empty | Argument list, started detached in `workdir` |
| `stop_cmd` | empty | Argument list, run to completion in `workdir` |

`[settings]` is optional. The registry recognises these keys and fills missing
values with the defaults shown:

| Setting | Default | Acted on by Gantry |
|---|---|---|
| `auto_load` | `true` | Yes: start the provider on its first request |
| `idle_unload_min` | `0` | Yes: stop after this many idle minutes; `0` disables |
| `exclusive_gpu` | `false` | Yes: stop other exclusive providers before starting |
| `context` | `0` | Stored and displayed |
| `max_tokens` | `0` | Stored and displayed |
| `kv` | empty | Stored and displayed |
| `keep_alive` | `0` | Stored and displayed |
| `parallel` | `1` | Stored and displayed |
| `vram_reserve_mib` | `0` | Stored and displayed as a VRAM headroom hint |

Backend-specific keys are preserved as well. The current registry uses
`unit`, `process_pattern`, `scope_unit`, `memory_max`, `thinking` and
`thinking_budget_tokens` for the Edge0/Qwen providers. Gantry acts on the
systemd scope and unit keys; other values are for the backend's own
configuration and tooling.

The registry is reloaded while the gateway runs. Routing sees file changes
within about two seconds. Changes saved through the dashboard or API refresh
server control immediately. After a CLI edit to `start_cmd`/`stop_cmd`, or
after adding a provider with the CLI, restart `gantry.service` (or
`run.sh restart`) once so explicit start/stop uses the new definition.

## Provider CLI

`./server.sh provider` operates only on `providers/*.toml`; it never starts or
stops a model and never touches the running gateway.

```bash
cd /home/user/gantry

./server.sh provider list
./server.sh provider show qwen-coder
./server.sh provider show qwen-coder --json
./server.sh provider path
```

`list` prints ID, display name, kind, port, context and auto-load flag.
`path` prints the active registry directory (or the `--providers-dir`
override).

Create and edit providers:

```bash
./server.sh provider add local \
  --name "Local Ollama" \
  --kind ollama \
  --host 127.0.0.1 \
  --port 30001 \
  --model qwen3:8b \
  --health-url http://127.0.0.1:30001/api/version \
  --logfile /tmp/local_server.log \
  --start-cmd runtime/local/start-local.sh \
  --stop-cmd runtime/local/stop-local.sh \
  --context 32768 \
  --auto-load

./server.sh provider edit qwen-coder \
  --context 131072 \
  --idle-unload-min 15 \
  --no-auto-load
```

`add` requires `--name`; everything else has the same safe defaults as the
registry. `edit` changes only the fields you pass. Use `--auto-load` /
`--no-auto-load`, `--exclusive-gpu` / `--no-exclusive-gpu` and `--enabled` /
`--no-enabled` for boolean fields.

Rename and delete:

```bash
./server.sh provider rename old-id new-id
./server.sh provider rm old-id          # asks for confirmation
./server.sh provider rm old-id --yes    # no prompt
```

Use `--providers-dir DIR` on any subcommand to work on another registry
directory.

## Backend control

The same CLI also controls the registered model servers. All legacy backends
share the upstream port `30001`; switching stops the running backend, waits for
the port and starts the selected one.

```bash
cd /home/user/gantry

./server.sh status
./server.sh start qwen
./server.sh start qwen-coder
./server.sh switch flint
./server.sh switch gptoss
./server.sh switch abl
./server.sh switch edge0
./server.sh stop
./server.sh restart flint
```

Available backend IDs are the provider IDs in the registry. With the current
registry these are `qwen`, `flint`, `gptoss`, `abl`, `edge0` and
`qwen-coder`. The gateway and dashboard keep running while a backend is
switched.

## Routing, auto-load and idle unload

For `POST /v1/chat/completions` and `POST /v1/completions`, Gantry reads the
request's `model` field and looks for an exact match in this order:

1. a provider's `model`;
2. a provider's `id`;
3. a provider's display `name`.

If no provider matches, the request uses the ordinary upstream passthrough.
The configured global model alias keeps its historical meaning ("currently
loaded backend") and is not treated as a provider selector.

When a provider matches:

- If it is running, the request is sent to `http://<host>:<port>` (or to the
  gateway's `upstream_base` when `port = 0`).
- If it is stopped and `auto_load = true`, Gantry starts it, waits for its
  health URL, and then forwards the request. The default start timeout is
  600 seconds.
- If it is stopped and `auto_load = false`, or if it is disabled, the request
  fails with HTTP `503` and an error body of type `provider_unavailable`.
  The body includes the tail of the provider log when one is configured.

Before starting a provider with `exclusive_gpu = true`, Gantry stops every
other running provider that also has `exclusive_gpu = true`. This prevents two
large models from competing for the same GPU. Providers on the same host and
port are also treated as conflicts.

The idle loop checks every 30 seconds. A running provider with
`idle_unload_min > 0` is stopped after that many minutes without an active
request. Active requests, including streaming requests, keep the provider
alive; the lease is released when the response body ends or the client
disconnects. The idle clock starts when the gateway first observes the
provider running.

Example: `qwen-coder` has `auto_load = true` and `idle_unload_min = 30`. Its
first request starts the provider; after 30 idle minutes Gantry stops it again.
Set `idle_unload_min = 0` to keep it loaded indefinitely.

## Qwen-Coder provider

[`providers/qwen-coder.toml`](providers/qwen-coder.toml) registers
`Qwen3.8 Flash Next Coder (abliterated)` as a separate `strata` provider on
port `8080`, outside the shared `30001` backend port. The excerpt below is
abridged; the `model` value is the routing key and is read from the file:

```toml
id = "qwen-coder"
name = "Qwen3.8 Flash Next Coder (abliterated)"
kind = "strata"
enabled = true
host = "127.0.0.1"
port = 8080
model = "<routing model>"
health_url = "http://127.0.0.1:8080/health"
pidfile = ""
logfile = "/tmp/qwen_serve.log"
workdir = "/home/user/edge0/edge0-strata"
start_cmd = ["/home/user/edge0/edge0-strata/.venv/bin/python", "serve/server.py", "--engine", "strata", "--config", "strata-coder-iq1_m.json", "--port", "8080"]
stop_cmd = ["pkill", "-INT", "-f", "^\\.venv/bin/python serve/server.py"]

[settings]
context = 131072
max_tokens = 8192
kv = "int8"
keep_alive = -1
auto_load = true
idle_unload_min = 30
exclusive_gpu = true
vram_reserve_mib = 1152
parallel = 1
```

Read the current routing model with:

```bash
./server.sh provider show qwen-coder
```

The start command runs in `/home/user/edge0/edge0-strata` and expects that
checkout and its `.venv` to exist. The provider has no PID file; server
control uses its health URL and the spawned process to determine whether it is
running. Requests use the value of `model` from the provider file:

```bash
curl http://127.0.0.1:30000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "<routing model>",
    "messages": [{"role": "user", "content": "Say hello."}],
    "stream": false
  }'
```

The first request may take a while because Gantry waits for the model to load.
To preload or stop it explicitly:

```bash
./server.sh start qwen-coder
./server.sh stop qwen-coder
# or:
curl -X POST http://127.0.0.1:30000/api/providers/qwen-coder/start
curl -X POST http://127.0.0.1:30000/api/providers/qwen-coder/stop
```

Because `exclusive_gpu = true`, starting it stops another running exclusive
provider first. It is the only current provider with `auto_load = true`; the
others must be started explicitly or through the dashboard/API.

## HTTP API

All API routes are served on the same port as the dashboard.

### Gateway and system

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness; returns `{"status": "ok"}` |
| `GET` | `/` | Dashboard |
| `GET` | `/api/info` | Gateway name, version, uptime and provider count |
| `GET` | `/api/system` | GPU, RAM, swap and disk values; missing tools appear under `errors` |

### Provider API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/providers` | List providers with `status` and `uptime` |
| `POST` | `/api/providers` | Create a provider; `201` on success, `409` if the ID exists |
| `GET` | `/api/providers/{id}` | Provider detail |
| `GET` | `/api/providers/{id}/edit` | Full editor view, including `start_cmd` and `stop_cmd` |
| `PATCH` | `/api/providers/{id}` | Update provider fields or `[settings]`; `id` is immutable |
| `POST` | `/api/providers/{id}/rename` | Rename; body `{"id": "new-id"}` |
| `DELETE` | `/api/providers/{id}` | Delete the provider file |
| `POST` | `/api/providers/{id}/start` | Start and wait for health |
| `POST` | `/api/providers/{id}/stop` | Stop and wait until it is gone |
| `GET` | `/api/providers/{id}/logs?lines=200` | Log tail; `lines` is 1-2000 |

Provider list entries look like this (abbreviated):

```json
{
  "id": "qwen-coder",
  "name": "Qwen3.8 Flash Next Coder (abliterated)",
  "kind": "strata",
  "enabled": true,
  "model": "<routing model>",
  "host": "127.0.0.1",
  "port": 8080,
  "status": "stopped",
  "uptime": 0,
  "settings": {"context": 131072, "auto_load": true, "idle_unload_min": 30},
  "auto_load": true,
  "idle_unload_min": 30,
  "exclusive_gpu": true,
  "health_url": "http://127.0.0.1:8080/health"
}
```

Typical errors:

- `404` for an unknown provider;
- `400` for invalid TOML values, an invalid ID, an unknown PATCH field, or a
  change to `id` through PATCH;
- `409` for a duplicate ID or a start/stop already in progress;
- `503` when server control is disabled or a provider cannot be auto-loaded;
- `502` when start/stop fails; the body contains a `log_tail` when a log file
  is configured.

Examples:

```bash
curl -s http://127.0.0.1:30000/api/providers
curl -s http://127.0.0.1:30000/api/providers/qwen-coder
curl -s http://127.0.0.1:30000/api/providers/qwen-coder/logs?lines=50
curl -X POST http://127.0.0.1:30000/api/providers/qwen-coder/start
curl -X PATCH http://127.0.0.1:30000/api/providers/qwen-coder \
  -H 'Content-Type: application/json' \
  -d '{"idle_unload_min": 0}'
```

### OpenAI and lifecycle routes

`GET`/`POST` requests under `/v1/` are passed through to the selected
provider or to the gateway's configured upstream. This includes
`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` and
`/v1/audio/*`. The gateway also provides the lifecycle endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET`/`POST` | `/v1/status` | Current model/backend status |
| `POST` | `/v1/load` | Load a model by name |
| `POST` | `/v1/load/{name}` | Load a named model |
| `POST` | `/v1/unload` | Unload models and release GPU memory |
| `POST` | `/v1/unload/{name}` | Unload a named model |

The exact request body for the lifecycle routes is the same as before the
Gantry rename; configuration and defaults are documented in
[`aiserver-monitor/config.example.toml`](aiserver-monitor/config.example.toml).

## Migration from small_qwen

Gantry is the same gateway under a new checkout name. The public port, the
Python package name (`aiserver_monitor`), the `AISERVER_MONITOR_*` environment
variables and the provider file format are unchanged. The migration is mainly
a path and service-name change:

1. Stop the old gateway so both services never bind port `30000`:

   ```bash
   systemctl --user disable --now aiserver-monitor.service
   ```

2. Put the checkout at `/home/user/gantry` (clone it there, or move the old
   directory if you no longer need the old path):

   ```bash
   # either
   git clone <repository-url> /home/user/gantry
   # or, when the old checkout is no longer needed
   mv /home/user/small_qwen /home/user/gantry
   ```

3. Create the venv at the new path if it does not exist:

   ```bash
   cd /home/user/gantry
   python3.12 -m venv .venv
   .venv/bin/pip install fastapi uvicorn httpx
   ```

4. Install and start the Gantry unit:

   ```bash
   cp /home/user/gantry/aiserver-monitor/systemd/gantry.service \
      ~/.config/systemd/user/gantry.service
   systemctl --user daemon-reload
   systemctl --user enable --now gantry.service
   ```

5. Verify the registry, API and dashboard:

   ```bash
   cd /home/user/gantry
   ./server.sh provider list
   curl -s http://127.0.0.1:30000/api/providers
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:30000/
   ```

Clients keep using `http://127.0.0.1:30000`; no client configuration changes
are required.

Notes:

- The old unit file remains at
  `aiserver-monitor/systemd/aiserver-monitor.service` and still points to
  `/home/user/small_qwen`. Keep it for rollback, but do not enable both units.
- `run.sh` and `tests/e2e/run_e2e.sh` default to
  `/home/user/gantry/.venv/bin/python`. Override `PYTHON` if your venv lives
  elsewhere.
- Provider `workdir` values are absolute where the backend lives outside this
  repository (for example `qwen-coder` uses `/home/user/edge0/edge0-strata`).
  Check them after moving the checkout:

  ```bash
  grep -rn "small_qwen\|smal_qwen" providers aiserver-monitor runtime \
    --exclude-dir=.venv --exclude-dir=venv --exclude-dir=__pycache__
  ```

  Remaining matches in legacy helper scripts or old documentation are paths of
  those files, not registry paths; update only what your deployment uses.

## Troubleshooting

### `gantry.service` does not start

```bash
systemctl --user status gantry.service
journalctl --user -u gantry.service -n 100 --no-pager
```

Check that `/home/user/gantry/.venv/bin/python` exists, that the unit's
`WorkingDirectory` is `/home/user/gantry/aiserver-monitor`, and that no other
process owns port `30000`:

```bash
ss -ltnp | grep ':30000'
```

### Port 30000 is already in use

Stop the old service or foreground gateway:

```bash
systemctl --user stop aiserver-monitor.service
bash /home/user/gantry/aiserver-monitor/run.sh stop
```

Only one gateway can serve the dashboard and API on a given port.

### A request returns `503 provider_unavailable`

The model matched a provider, but it is not running and cannot be started
automatically. Check `enabled`, `auto_load`, the provider log and the response's
`log_tail`:

```bash
curl -s http://127.0.0.1:30000/api/providers/<id>
curl -s 'http://127.0.0.1:30000/api/providers/<id>/logs?lines=100'
```

A stopped provider with `auto_load = false` is expected to return `503` until
you start it with `./server.sh start <id>` or the dashboard.

### A provider is missing from the list

`./server.sh provider list` validates the registry. Common causes:

- the file name does not match `id` (`qwen.toml` must contain
  `id = "qwen"`);
- the TOML is malformed;
- two files declare the same `id`;
- an unknown top-level key is present.

Fix the file and run the command again. The gateway logs the registry error and
keeps the last valid routing state.

### Start or stop fails with `502`

The provider process did not reach its health URL or did not stop within the
timeout. The API response contains the tail of `logfile`; the same lines are in
`GET /api/providers/<id>/logs`. For a large model, allow for a long load time
and check the backend's own log and GPU memory.

### The GPU is busy or a model was stopped unexpectedly

Providers with `exclusive_gpu = true` stop each other by design, and providers
on the same host/port conflict. Check `exclusive_gpu` in the provider files and
the current states:

```bash
./server.sh status
```

Idle unload also stops running providers after `idle_unload_min` minutes. Set
it to `0` to disable that behavior.

### Dashboard works but GPU values are empty

Install `nvidia-smi` (NVIDIA driver) or ignore the empty values. The gateway
does not fail when the tool is unavailable; `/api/system` reports the reason
under `errors`.

### Logs

- Gateway: `journalctl --user -u gantry.service -f`, or
  `data/aiserver-monitor.log` for the configured rotating log.
- Provider: the `logfile` field in the provider TOML, or
  `GET /api/providers/<id>/logs`.
- `run.sh`: `AISERVER_MONITOR_RUN_LOG` (default
  `/tmp/aiserver-monitor-run.log`).

## Tests

Run the unit tests from the gateway package directory:

```bash
cd /home/user/gantry/aiserver-monitor
../.venv/bin/python -m unittest discover -s tests
```

The end-to-end dashboard test uses test ports `39000`/`39001` and does not
touch the real gateway or model backend:

```bash
cd /home/user/gantry/aiserver-monitor
bash tests/e2e/run_e2e.sh
```

It requires the Playwright Chromium executable configured through `EXE` (or
the default path in the script).

## Repository layout

| Path | Content |
|---|---|
| [`providers/`](providers/) | Provider registry, one TOML file per provider |
| [`aiserver-monitor/aiserver_monitor/`](aiserver-monitor/aiserver_monitor/) | Gateway, provider router, API and dashboard |
| [`aiserver-monitor/systemd/gantry.service`](aiserver-monitor/systemd/gantry.service) | systemd user unit for Gantry |
| [`aiserver-monitor/run.sh`](aiserver-monitor/run.sh) | Start/stop/status wrapper with CPU sidecars |
| [`server.sh`](server.sh) | CLI for providers and model backends |
| [`runtime/`](runtime/) | Launchers and per-backend runtime files |
| [`docs/`](docs/) | Architecture and operational documentation |
| [`aiserver-monitor/config.example.toml`](aiserver-monitor/config.example.toml) | Configuration reference |
