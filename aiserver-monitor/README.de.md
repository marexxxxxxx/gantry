<p align="right"><a href="README.md">🇬🇧 English</a> | <b>🇩🇪 Deutsch</b></p>

# aiserver-monitor

Gateway vor dem Escha-SGLang-Inferenzserver. Es leitet Anfragen im
OpenAI-Format an SGLang weiter (inklusive Streaming), hält lange Wartezeiten
auf der Cloudflare-Strecke offen und liefert ein Dashboard für den
Warteschlangen-Zustand, Live-Einblick je Job, Einzelabbruch/„Alles stoppen“
und Statistiken. Ein Prozess, ein uvicorn-Worker.

Der geplante Rollout tauscht die Ports: Das Gateway übernimmt
`0.0.0.0:30000`, SGLang zieht auf `127.0.0.1:30001` (Details im
Design-Dokument, siehe [Weiterlesen](#weiterlesen)). Bis zum Cutover bleibt
alles unverändert — das Gateway läuft nur auf Testports (`39xxx`).

## Voraussetzungen

- Python 3.12 mit der Repo-Venv unter `/home/user/small_qwen/.venv`
  (FastAPI, uvicorn und httpx sind dort installiert).
- Der SGLang-Server erreichbar unter `upstream_base` (Default
  `http://127.0.0.1:30001`). Das Gateway startet auch, wenn SGLang aus ist;
  Anfragen scheitern dann mit sauberen Fehlern.
- `nvidia-smi` für die GPU-Werte (optional; ohne bleibt es bei „nicht
  gemessen“).

## Start, Stop, Status

```bash
cd /home/user/small_qwen/aiserver-monitor
bash run.sh start      # im Hintergrund starten, wartet auf /healthz
bash run.sh status     # Exit 0 wenn laeuft, Exit 1 wenn nicht
bash run.sh stop       # TERM, bis zu 20 s warten, dann KILL
bash run.sh restart    # stop und start
```

`start` und `stop` sind idempotent: Ein zweiter `start` meldet „laeuft
bereits“ und liefert Exit 0, ein `stop` ohne laufendes Gateway ebenfalls.
`run.sh` fasst ausschließlich den Gateway-Prozess an — der SGLang-Server
wird nie gestartet, gestoppt oder umgestellt.

Umgebungs-Overrides von `run.sh`:

| Variable | Default | Bedeutung |
|---|---|---|
| `PYTHON` | `/home/user/small_qwen/.venv/bin/python` | Interpreter für `python -m aiserver_monitor` |
| `AISERVER_MONITOR_LISTEN_PORT` | `30000` | Port für den Health-Poll (`/healthz`); zugleich der App-Port, solange nicht anders konfiguriert |
| `PIDFILE` | `/tmp/aiserver-monitor.pid` | PID-Datei für `stop` |
| `AISERVER_MONITOR_RUN_LOG` | `/tmp/aiserver-monitor-run.log` | stdout/stderr-Log des Hintergrundprozesses |
| `AISERVER_MONITOR_PATTERN` | `[a]iserver_monitor` | `pgrep -f`-Muster (leere Werte werden abgewiesen, der Default gilt) |
| `STOP_GRACE` | `20` | Sekunden zwischen TERM und KILL |
| `START_WAIT` | `20` | Sekunden Warten auf `/healthz` beim Start |

Start im Vordergrund (Entwicklung, systemd):

```bash
cd /home/user/small_qwen/aiserver-monitor
/home/user/small_qwen/.venv/bin/python -m aiserver_monitor
```

Der Einstiegspunkt liest `AISERVER_MONITOR_CONFIG` (Pfad zu einer
TOML-Datei, optional), schreibt ein rotierendes Log nach `log_path` (5 MB
pro Datei, 3 Backups) und beantwortet `/healthz` als Lebenszeichen.

## systemd (User-Unit)

`systemd/aiserver-monitor.service` ist eine User-Unit:

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

Installieren (kopieren oder verlinken) und aktivieren:

```bash
mkdir -p ~/.config/systemd/user
cp /home/user/small_qwen/aiserver-monitor/systemd/aiserver-monitor.service \
   ~/.config/systemd/user/
# oder: ln -s /home/user/small_qwen/aiserver-monitor/systemd/aiserver-monitor.service \
#           ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now aiserver-monitor.service
```

Prüfen mit `systemctl --user status aiserver-monitor.service`, Ausgaben
verfolgen mit `journalctl --user -u aiserver-monitor.service -f`. Damit die
Unit auch nach dem Logout weiterläuft / beim Boot startet, einmalig Linger
aktivieren: `loginctl enable-linger $USER`. Die Unit startet die App direkt;
`run.sh start` nicht aufrufen, solange die Unit aktiv ist.

## Konfiguration

Quellen, von schwach nach stark: eingebaute Defaults < TOML-Datei <
Umgebungsvariablen.

- **Umgebung:** Jedes `Config`-Feld lässt sich als
  `AISERVER_MONITOR_<FELD>` setzen (Feldname in Großbuchstaben), z. B.
  `AISERVER_MONITOR_LISTEN_PORT=39999` oder
  `AISERVER_MONITOR_UPSTREAM_BASE=http://127.0.0.1:39099`. Ein leerer Wert
  zählt als nicht gesetzt.
- **TOML-Datei:** `AISERVER_MONITOR_CONFIG=/pfad/zu/config.toml` setzen
  (leer oder nicht gesetzt = keine Datei). Die Datei schlägt Defaults, die
  Umgebung schlägt die Datei.
- **[`config.example.toml`](config.example.toml)** listet jedes Feld
  auskommentiert mit seinem Default: `listen_host`, `listen_port`,
  `upstream_base`, `db_path`, `log_path`, `heartbeat_s`, `poll_interval_s`,
  `connect_timeout_s`, `preview_max_chars`, `prompt_start_chars`,
  `retention_jobs_days`, `retention_series_days`, `nonstream_keepalive`,
  `gpu_cmd`.

`db_path` und `log_path` sind relativ zum Arbeitsverzeichnis. `run.sh`
wechselt zuerst in das Projektverzeichnis, deshalb zeigen die Defaults auf
`aiserver-monitor/data/monitor.db` und
`aiserver-monitor/data/aiserver-monitor.log`.

## Betrieb und Rollback

Bis zum Cutover bleibt das laufende Setup unangetastet: SGLang behält seinen
aktuellen Port, der bestehende slotmon-Dienst läuft weiter. Das Gateway
stattdessen auf einem Testport prüfen, z. B.
`AISERVER_MONITOR_LISTEN_PORT=39055 bash run.sh start` (mit
`AISERVER_MONITOR_UPSTREAM_BASE` auf einen Test-Upstream).

Rollback jederzeit: `bash run.sh stop` (bzw. nach dem Cutover
`systemctl --user disable --now aiserver-monitor.service`). SGLang läuft
weiter, wo er lief — das Gateway ändert den Lebenszyklus des Servers nie.

## Weiterlesen

- Design: [`../docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md`](../docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md)
  (Repo-Pfad `docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md`)
- Implementierungsplan:
  [`../docs/superpowers/plans/2026-10-13-aiserver-monitor.md`](../docs/superpowers/plans/2026-10-13-aiserver-monitor.md)
