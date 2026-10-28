<p align="right"><a href="README.md">🇬🇧 English</a> | <b>🇩🇪 Deutsch</b></p>

# aiserver-monitor

Gateway vor dem Escha-SGLang-Inferenzserver. Es leitet Anfragen im
OpenAI-Format an SGLang weiter (inklusive Streaming), hält lange Wartezeiten
auf der Cloudflare-Strecke offen und liefert ein Dashboard für den
Warteschlangen-Zustand, Live-Einblick je Job, Einzelabbruch/„Alles stoppen“
und Statistiken. Ein Prozess, ein uvicorn-Worker.

Nicht-streamende Chat-Anfragen (`stream=false`) werden intern als Stream
abgewickelt: Sie erscheinen live im Dashboard (laufend, TTFT, wachsende
Vorschauen, Tokens) und lassen sich wirklich abbrechen; der Client bekommt
weiterhin gültiges Chat-Completion-JSON.

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
`run.sh` startet und stoppt den CPU-Embedding-Sidecar zusammen mit dem
Gateway, weil `/v1/embeddings` keinen anderen Upstream hat. Der SGLang-Server
wird davon nie gestartet, gestoppt oder umgestellt.

### Embeddings (CPU)

`POST /v1/embeddings` geht an `embed_base` (Default
`http://127.0.0.1:30002`), einen eigenstaendigen Ollama-Prozess mit
`qwen3-embedding:0.6b`. Er laeuft auf der CPU und belegt keinen VRAM,
funktioniert also, waehrend Qwen, Flint oder GPT-OSS die GPU nutzt. Das Gateway
behandelt ihn als gepufferten Passthrough: kein Job-Tracking, keine
Keepalives, Antwort unveraendert. Bekannte Modellnamen (Qwen-Alias, Flint,
GPT-OSS, Embedding-Modell) und ein fehlender/leerer Name werden auf
`embed_model` abgebildet; unbekannte Namen laufen unveraendert durch, damit
Ollama mit einem klaren „model not found“ antworten kann.

```bash
curl http://127.0.0.1:30000/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model": "escha-qwen38-27b-w2", "input": "Hallo Welt"}'
```

Der Sidecar laesst sich auch direkt steuern:

```bash
bash runtime/embed/start-embed.sh
bash runtime/embed/stop-embed.sh
```

### Server ein-/ausschalten

Der Modellserver lässt sich im Dashboard unter **Server** ausschalten und
wieder einschalten. Derselbe Weg im Terminal (Repo-Wurzel):

```bash
bash server.sh status   # Zustand + /health
bash server.sh stop     # Modellserver stoppen, Dashboard laeuft weiter
bash server.sh start    # starten, wartet auf /health (2-4 Minuten)
bash server.sh restart
```

Ist die systemd-User-Unit `escha-server.service` installiert, laufen
`start`/`stop` über `systemctl --user` (mit `--no-block`); nur so bleibt der
`Restart=always`-Schutz beim bewussten Ausschalten wirkungslos. Ohne Unit
werden `runtime/sglang/start-penalty.sh` bzw. `stop-server.sh` benutzt.
Ausschalten ist temporär: Die Unit bleibt enabled, beim nächsten Reboot
startet der Server wieder automatisch. Die Steuerung lässt sich mit
`server_control_enabled = false` (bzw.
`AISERVER_MONITOR_SERVER_CONTROL_ENABLED=false`) abschalten.

Umgebungs-Overrides von `run.sh`:

| Variable | Default | Bedeutung |
|---|---|---|
| `PYTHON` | `/home/user/small_qwen/.venv/bin/python` | Interpreter für `python -m aiserver_monitor` |
| `AISERVER_MONITOR_LISTEN_PORT` | `30000` | Port für den Health-Poll (`/healthz`); zugleich der App-Port, solange nicht anders konfiguriert |
| `AISERVER_MONITOR_EMBED_BASE` | `http://127.0.0.1:30002` | Upstream für `/v1/embeddings` (CPU-Ollama); wird auch von `run.sh status` genutzt |
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

## Dashboard-Build (Webapp)

Das Dashboard ist eine Vite/React-App in `webapp/`; der Build wird nicht
eingecheckt. Ohne ihn liefert das Gateway eine kleine Platzhalterseite aus.
Einmal bauen (und nach jeder Webapp-Änderung erneut):

```bash
cd webapp
npm install
npm run build
```

Voraussetzung ist Node 22+. Der Build landet in `aiserver_monitor/static/`,
das gitignored ist und nicht eingecheckt werden darf. `tests/e2e/run_e2e.sh`
führt den Build vor den Browser-Szenarien automatisch aus.

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
  `gpu_cmd`, `abort_on_disconnect`, `server_control_enabled`, `server_unit`,
  `server_start_script`, `server_stop_script`, `server_process_pattern`.

Standardmäßig (`abort_on_disconnect = true`) überwacht das Gateway die
Client-Verbindung: verschwindet ein Client, während sein Job noch wartet,
wird der wartende Upstream-Aufruf abgebrochen und der Job als `aborted`
abgeschlossen (best effort `abort_request` an SGLang), während `false` das
alte Verhalten herstellt, in dem jeder Job zu Ende läuft.

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
weiter, wo er lief — nur die ausdrückliche Server-Steuerung (Dashboard-Tab
„Server“ oder `server.sh`) schaltet ihn um.

## Weiterlesen

- Design: [`../docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md`](../docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md)
  (Repo-Pfad `docs/superpowers/specs/2026-10-13-aiserver-monitor-design.md`)
- Implementierungsplan:
  [`../docs/superpowers/plans/2026-10-13-aiserver-monitor.md`](../docs/superpowers/plans/2026-10-13-aiserver-monitor.md)
