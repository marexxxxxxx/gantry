[English](../README.md) | **Deutsch**

# Gantry (Kurzfassung)

Gantry ist das lokale Gateway und Dashboard für mehrere OpenAI-kompatible
Modellserver auf einer Maschine. Es bietet einen festen Client-Port
`127.0.0.1:30000`, routet Anfragen anhand des angefragten Modells zu einem
Provider und verwaltet dessen Prozess über die Datei-Registry
`providers/*.toml`.

Die ausführliche englische Dokumentation steht in
[`README.md`](../README.md); diese Seite ist die deutsche Kurzfassung.

## Installation und Start

```bash
cd /home/user/gantry
python3.12 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx
```

Empfohlener Start über die systemd-User-Unit:

```bash
mkdir -p ~/.config/systemd/user
cp /home/user/gantry/aiserver-monitor/systemd/gantry.service \
   ~/.config/systemd/user/gantry.service
systemctl --user daemon-reload
systemctl --user enable --now gantry.service
systemctl --user status gantry.service
```

Alternativ im Vordergrund bzw. über das Verwaltungsskript:

```bash
cd /home/user/gantry/aiserver-monitor
bash run.sh start      # startet das Gateway und die CPU-Sidecars
bash run.sh status
bash run.sh stop
```

`gantry.service` und `run.sh start` nicht gleichzeitig verwenden: beide binden
Port `30000`.

Dashboard: `http://127.0.0.1:30000/` · Health: `GET /healthz`

## Provider-Registry

Ein Provider ist eine TOML-Datei `providers/<id>.toml`; der Dateiname muss zur
`id` passen. Die Registry ist die einzige Quelle für Gateway, Dashboard, API
und CLI.

Wichtige Top-Level-Felder: `id`, `name`, `kind` (`strata`, `ollama`,
`sglang`, `custom`), `enabled`, `host`, `port`, `model`, `health_url`,
`pidfile`, `logfile`, `workdir`, `start_cmd`, `stop_cmd`.

Wichtige `[settings]`:

| Einstellung | Default | Bedeutung |
|---|---|---|
| `auto_load` | `true` | Beim ersten Request starten |
| `idle_unload_min` | `0` | Nach so vielen Leerlaufminuten stoppen; `0` = nie |
| `exclusive_gpu` | `false` | Vor dem Start andere exklusive Provider stoppen |
| `context`, `max_tokens`, `kv`, `keep_alive`, `parallel`, `vram_reserve_mib` | siehe Registry | Werden gespeichert und angezeigt |

Der Gateway-Cache liest Änderungen an der Registry innerhalb von etwa zwei
Sekunden neu. Ein über die CLI neu angelegter Provider kann für die explizite
Start/Stop-Steuerung einen Neustart von `gantry.service` benötigen.

## CLI

```bash
cd /home/user/gantry

./server.sh provider list
./server.sh provider show qwen-coder
./server.sh provider show qwen-coder --json
./server.sh provider add local --name "Local" --kind ollama --port 30001 \
  --model qwen3:8b --start-cmd runtime/local/start.sh \
  --stop-cmd runtime/local/stop.sh --auto-load
./server.sh provider edit qwen-coder --idle-unload-min 15
./server.sh provider rename old-id new-id
./server.sh provider rm old-id --yes
./server.sh provider path
```

Modell-Backends werden mit demselben Skript gesteuert:

```bash
./server.sh status
./server.sh start qwen
./server.sh start qwen-coder
./server.sh switch flint
./server.sh stop
```

Die aktuellen Backend-IDs sind `qwen`, `flint`, `gptoss`, `abl`, `edge0` und
`qwen-coder`. Die klassischen Backends teilen sich den Upstream-Port `30001`;
ein Wechsel stoppt das laufende Backend und startet das neue.

## API

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/healthz` | Liveness |
| `GET` | `/api/info` | Name, Version, Uptime, Provider-Anzahl |
| `GET` | `/api/system` | GPU-, RAM-, Swap- und Disk-Werte |
| `GET` | `/api/providers` | Provider-Liste mit Status und Uptime |
| `POST` | `/api/providers` | Provider anlegen |
| `GET` | `/api/providers/{id}` | Provider-Details |
| `GET` | `/api/providers/{id}/edit` | Vollständige Editor-Sicht |
| `PATCH` | `/api/providers/{id}` | Felder bzw. `[settings]` ändern |
| `POST` | `/api/providers/{id}/rename` | Umbenennen, Body `{"id": "neu"}` |
| `DELETE` | `/api/providers/{id}` | Provider-Datei löschen |
| `POST` | `/api/providers/{id}/start` | Starten und auf Health warten |
| `POST` | `/api/providers/{id}/stop` | Stoppen und auf Ende warten |
| `GET` | `/api/providers/{id}/logs?lines=200` | Letzte Logzeilen |

`GET`/`POST` unter `/v1/` werden an den passenden Provider bzw. den Upstream
weitergeleitet, darunter `/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`, `/v1/models` und `/v1/audio/*`. Dazu kommen die
Lifecycle-Routen `/v1/status`, `/v1/load` und `/v1/unload` (jeweils auch mit
Modellname im Pfad).

Beispiele:

```bash
curl -s http://127.0.0.1:30000/api/providers
curl -s http://127.0.0.1:30000/api/providers/qwen-coder
curl -X POST http://127.0.0.1:30000/api/providers/qwen-coder/start
curl -X PATCH http://127.0.0.1:30000/api/providers/qwen-coder \
  -H 'Content-Type: application/json' -d '{"idle_unload_min": 0}'
```

## Auto-Load, Idle-Unload und GPU

- Der Request-Modellname wird exakt gegen `model`, dann `id`, dann `name`
  gematcht.
- Läuft der Provider, wird die Anfrage an `http://<host>:<port>` geschickt.
- Ist er gestoppt und `auto_load = true`, startet Gantry ihn und wartet auf
  die Health-URL (Standard-Timeout 600 s).
- Ist er gestoppt und `auto_load = false` oder `enabled = false`, antwortet
  das Gateway mit `503` und `provider_unavailable`; ein konfigurierter
  Log-Tail wird mitgeliefert.
- Vor dem Start eines Providers mit `exclusive_gpu = true` werden andere
  laufende exklusive Provider gestoppt. So läuft immer nur ein großes Modell
  auf der GPU.
- Der Idle-Loop prüft alle 30 Sekunden. `idle_unload_min > 0` stoppt den
  Provider nach dieser Zeit ohne aktiven Request; laufende Streams halten ihn
  am Leben.

## Qwen-Coder

`providers/qwen-coder.toml` registriert den Provider
`Qwen3.8 Flash Next Coder (abliterated)`:

- `kind = "strata"`, eigener Port `8080`;
- der Wert von `model` in der Provider-Datei ist der Routing-Name
  (`./server.sh provider show qwen-coder` zeigt ihn);
- Start aus `/home/user/edge0/edge0-strata` mit dessen `.venv` und
  `serve/server.py --engine strata --config strata-coder-iq1_m.json --port 8080`;
- `auto_load = true`, `idle_unload_min = 30`, `exclusive_gpu = true`,
  `context = 131072`, `kv = "int8"`.

Der erste Request startet den Provider automatisch; nach 30 Leerlaufminuten
wird er wieder gestoppt. Manuell:

```bash
./server.sh start qwen-coder
./server.sh stop qwen-coder
```

## Migration von small_qwen

Gantry ist derselbe Gateway unter neuem Verzeichnis- und Servicenamen. Port,
Python-Paket (`aiserver_monitor`), Umgebungsvariablen (`AISERVER_MONITOR_*`)
und das Provider-Format bleiben unverändert.

1. Alten Dienst stoppen:
   `systemctl --user disable --now aiserver-monitor.service`
2. Checkout nach `/home/user/gantry` bringen (klonen oder verschieben).
3. Venv unter `/home/user/gantry/.venv` anlegen und Abhängigkeiten
   installieren.
4. `gantry.service` nach `~/.config/systemd/user/` kopieren,
   `systemctl --user daemon-reload` und
   `systemctl --user enable --now gantry.service`.
5. Mit `./server.sh provider list`, `/api/providers` und `/` prüfen.

Die alte Unit `aiserver-monitor.service` bleibt im Repository und zeigt
weiterhin auf `/home/user/small_qwen`; sie dient nur als Rollback und darf
nicht parallel zu `gantry.service` laufen. `run.sh` und
`tests/e2e/run_e2e.sh` verwenden inzwischen
`/home/user/gantry/.venv/bin/python`.

## Fehlersuche

- **Dienst startet nicht:** `systemctl --user status gantry.service` und
  `journalctl --user -u gantry.service -n 100`; Pfad zur `.venv` und
  `WorkingDirectory` prüfen.
- **Port 30000 belegt:** alten Dienst oder `run.sh`-Prozess stoppen;
  `ss -ltnp | grep ':30000'`.
- **`503 provider_unavailable`:** `enabled`, `auto_load`, Provider-Log und
  `log_tail` prüfen; Provider bei Bedarf über CLI/Dashboard starten.
- **Provider fehlt:** Dateiname muss zur `id` passen, TOML gültig sein, IDs
  dürfen nicht doppelt vorkommen.
- **`502` bei Start/Stop:** Health-URL und Log-Tail prüfen; große Modelle
  brauchen ggf. mehrere Minuten zum Laden.
- **GPU belegt:** `exclusive_gpu` stoppt andere Provider absichtlich;
  `./server.sh status` zeigt die Zustände.
- **Leere GPU-Werte:** `nvidia-smi` fehlt; `/api/system` nennt den Grund unter
  `errors`.
- **Idle-Unload überrascht:** `idle_unload_min` auf `0` setzen.

## Tests

```bash
cd /home/user/gantry/aiserver-monitor
../.venv/bin/python -m unittest discover -s tests
```

Der End-to-End-Test nutzt die Testports `39000`/`39001` und lässt den echten
Gateway auf `30000` unberührt:

```bash
cd /home/user/gantry/aiserver-monitor
bash tests/e2e/run_e2e.sh
```
