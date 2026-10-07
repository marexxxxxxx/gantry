# GPU-Slot-Monitor — Design

Datum: 2026-10-07
Status: Design freigegeben

## Zweck

Ein Dashboard, das jederzeit zeigt, welcher Client gerade den einzelnen GPU-Slot des
sglang-Servers hält, wie lange schon, mit welcher Geschwindigkeit, und wer als nächstes
dran ist.

## Harte Randbedingung

**Der sglang-Server wird nie neugestartet und nie verändert.** Kein Neustart, keine
Flag-Änderung, kein Eingriff in den Datenpfad, kein Eingriff in Client-Konfigurationen.
Der Monitor liest ausschließlich.

## Ausgangslage (gemessen am 2026-10-07)

- Server lauscht auf `0.0.0.0:30000`, PID 145265, läuft seit 4. Oktober
- `--max-running-requests 1` und `--max-mamba-cache-size 1` → genau **ein** GPU-Slot,
  alle anderen warten FIFO
- `/get_load` liefert `rid: null` → keine Request-Identität vom Server
- `log_requests: False` → das Log enthält keine Prompts und keine Client-Zuordnung
- `/metrics` (ohne Auth) liefert `sglang:gen_throughput`, `sglang:num_queue_reqs`,
  `sglang:num_running_reqs`
- Log: `/tmp/escha_server_lan.log`, ist `fd 1` **und** `fd 2` von PID 145265
- `/metrics` braucht keinen API-Key, `/get_load` braucht `Authorization: Bearer marec`

**Konsequenz:** Prompt-Text ist ohne Server-Änderung nicht auslesbar. Der Monitor zeigt
daher ausschließlich Metadaten: Systemname, IP:Port, Laufzeit, Token, Kontextfüllung,
Durchsatz, Position.

## Wie der Slot-Halter erkannt wird

Das einzige zuverlässige Unterscheidungsmerkmal ohne Eingriff ist Datenfluss auf
Socket-Ebene:

```
ss -tinH state established '( sport = :30000 )'
```

Zwei Snapshots im Abstand von 2 s, Differenz auf `bytes_sent` je Verbindung. Die
Verbindung mit deutlichem Zuwachs sendet SSE-Tokens und hat damit den Slot. Alle
anderen sind wartend. Verifiziert am 2026-10-07: 35 Verbindungen, genau eine mit
+112 KB in 15 s (≈ 7,5 KB/s).

## Architektur

Vier Einheiten, jede mit genau einer Aufgabe.

### `monitor.sh` — Lebenszyklus

Nur starten/stoppen/status. Keine Zustandslogik. Folgt dem Muster von
`runtime/sglang/start-penalty.sh`:

- `set -euo pipefail`
- Doppelstart-Sperre über `pgrep -f "[s]lotmon"`
- `nohup` ins Log, Log nach `/tmp`
- Health-Poll beim Start
- `stop` mit `TERM`, 20 s Grace, dann `KILL`

### `slotmon/collector.py` — liest, schreibt nie in den Datenpfad

Pollt alle 2 s:

| Quelle | Liefert |
|---|---|
| `/get_load` mit `Authorization: Bearer marec` | `num_reqs`, `num_waiting_reqs` |
| `/get_server_info` mit `Authorization: Bearer marec` | `max_req_input_len`, einmalig beim Start gelesen |
| `/metrics` ohne Auth | `gen_throughput`, `num_queue_reqs`, `num_running_reqs` |
| `nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total` | GPU-Auslastung, VRAM |
| `ss -tinH ... sport = :30000` | Verbindungen, `bytes_sent` |
| `tail -c` auf `/tmp/escha_server_lan.log` | `#full token`, `#queue-req` |

### `slotmon/history.py` — Persistenz

Schreibt abgeschlossene Slot-Übergaben als eine JSON-Zeile nach
`/tmp/slotmon/history.jsonl`. Beim Start werden nur die letzten 24 h gelesen, die Datei
beim nächsten Schreiben auf diesen Stand gekürzt. Überlebt einen Neustart, wächst
nicht unbegrenzt.

### `slotmon/server.py` — Dashboard

FastAPI/uvicorn (beides in der vorhandenen venv vorhanden). Eine Seite, die sich per
SSE aktualisiert statt zu pollen. Bindet auf `0.0.0.0:8090`, ohne Auth.

### `systems.json` — IP → Name

```json
{ "192.168.9.2": "Laptop (Claude Code)" }
```

Unbekannte IP fällt auf die rohe IP zurück, nie auf einen Fehler.

## Datenfluss je Poll-Zyklus

1. `/get_load`, `/metrics`, `nvidia-smi` lesen → aktuelle Zahlen
2. `ss`-Snapshot nehmen, `bytes_sent` je Verbindung merken
3. 2 s später Differenz bilden → Verbindung mit Zuwachs = Slot-Halter
4. IP → Name über `systems.json`, Port aus dem Sockelpfad
5. Mit dem letzten bekannten Halter vergleichen:
   - **gleicher Halter** → laufender Slot: `started_at` bleibt, Laufzeit hochzählen,
     Fortschritt aus `#full token`, `gen_throughput` in die Sparkline
   - **neuer Halter** → alter Eintrag wird abgeschlossen (`ended_at`, Dauer, letzter
     ctx-Wert, Ø tok/s) und landet in `history.jsonl`; neuer Eintrag startet mit
     `started_at = jetzt`
   - **kein Halter** → Slot ist frei; wartende Connection mit der längsten Wartezeit
     wird als `NÄCHSTER` markiert

### Bekannte Grenzen

- Der Halter-Wechsel wird erst erkannt, wenn der nächste Halter tatsächlich sendet.
  Im Extremfall steht der alte Name wenige Sekunden zu lang.
- Die Reihenfolge der Wartenden ist aus dem Socket-Stand nicht exakt ableitbar, weil
  sglang intern nach eigener Scheduler-Logik sortiert. `Position 1` ist eine Näherung aus
  der Queue-Länge, nicht die echte Position.

Beide stehen als Hinweis im Dashboard.

### Abgeleitete Werte

- **WARTEZEIT** — Schätzung aus Queue-Länge mal mittlerer Slot-Dauer. Die mittlere
  Dauer kommt aus den abgeschlossenen Einträgen der Historie; solange die Historie leer
  ist, wird sie aus der aktuellen Laufzeit des laufenden Slots genommen. Fehlt auch die,
  zeigt die Kachel `–` statt einer erfundenen Zahl. Die Kachel ist als Schätzung
  gekennzeichnet.
- **Position des NÄCHSTEN** — 1, sobald der Slot frei wird; während ein Slot belegt ist,
  zeigt der Zeitstrahl die Position aus der Queue-Länge, ebenfalls als Näherung markiert.
- **Ø tok/s je abgeschlossener Eintrag** — Mittel der über die Lebensdauer gemittelten
  Durchsätze, nicht der Summe.

## Dashboard-Aufbau

Zwei Reiter. Der Reiterwechsel ändert nur die Ansicht, nicht die Daten; die Werte
aktualisieren sich im Hintergrund weiter. „Verlauf" merkt sich seine Position.

**Reiter „Jetzt"**
- KPI-Raster (5): SLOT (BELEGT/frei), QUEUE, TOK/S, GPU, WARTEZEIT
- Große Karte des aktiven Halters: grüner Rand, Badge `HAT DEN SLOT`, IP:Port darunter
- Vier Kennzahlen: LÄUFT (seit `started_at`), TOK/S (`gen_throughput`), CTX (`#full token`
  aus dem Server-Log), AUSGABEN in Prozent
- Fortschrittsbalken: `AUSGABEN = #full token / max_req_input_len`. `max_req_input_len`
  kommt einmalig aus `/get_server_info` (83994). Der Balken misst die Füllung des
  Eingabekontexts, nicht die Antwortlänge — er zeigt, wie viel vom Fenster verbraucht ist
- Tabelle der Wartplätze, nach System gruppiert, mit Anzahl je System

**Reiter „Verlauf"**
- Zeitstrahl der Slot-Übergaben: LÄUFT (grün), FERTIG (grau), NÄCHSTER (gelb)
  mit Dauer, ctx und tok/s je Eintrag
- Sparkline tok/s der letzten 60 s
- Summen: Übergaben, Ø Dauer, verbleibende Queue

## Fehlerbehandlung

Grundsatz: Ein Fehler im Monitor darf nie als Signal erscheinen. Jeder Datenpunkt hat
einen bekannten Zustand statt eines geratenen Werts.

| Fehler | Verhalten |
|---|---|
| `/get_load` oder `/metrics` nicht erreichbar | Quellen als getrennte Altersangaben führen. Ist nur eine tot, zeigen die übrigen Kacheln weiter; das Slot-Feld wird grau mit „verbunden mit Server: nein". Monitor läuft weiter und erholt sich ohne Neustart. |
| Kein Socket mit Datenfluss | Slot-Karte: „frei", mit Wartezeit. Kein Name wird erfunden. |
| `ss` fehlt / keine Verbindungen | Slot-Kachel zeigt „unbekannt" statt einer Zahl, Slot-Halter-Name bleibt leer. Die Kachel bleibt sichtbar, damit der Ausfall nicht wie ein freier Server aussieht. |
| `nvidia-smi` fehlt oder scheitert | GPU- und VRAM-Kachel zeigen `–`. Nie `0`, weil 0 als Aussage gelesen wird. |
| `systems.json` fehlt oder Syntaxfehler | Start bricht mit klarer Meldung ab. Läuft nicht halb konfiguriert. Fehlende *IP* im File ist kein Fehler. |
| `history.jsonl` defekt | Kaputte Zeile wird übersprungen, Datei beim nächsten Schreiben neu aufgebaut. Kein Absturz. |
| Log wächst unbegrenzt | Collector liest nur die letzten Bytes (`tail -c`), nie die ganze Datei. Eigenes Monitor-Log rotiert bei 5 MB. |

### Log-Aufräumen ab 1 GB

**Nicht `rm`.** Der Server hält die Logdatei offen (`fd 1` und `fd 2` von PID 145265).
Ein `rm` entfernt nur den Verzeichniseintrag; die Datei lebt als Anonymous-Inode weiter,
der Server schreibt hinein und der Platz wird erst beim nächsten Neustart frei — genau
den, den wir nicht haben.

`truncate -s 0` setzt die Größe auf 0. Der offene Deskriptor bleibt gültig, der Server
schreibt danach an Position 0 weiter. Platz sofort frei, Server unberührt.

- Aufräumschritt im Collector, aber nur alle 60 Poll-Zyklen (≈ alle 2 min), nur ein
  `stat` auf die Dateigröße, kein zusätzlicher I/O im Hot-Path
- **ab 1 GB** → `truncate -s 0` auf `/tmp/escha_server_lan.log`, Protokollzeile
  `log-truncated 1.0GB → 0`
- **ab 900 MB** → Warnung im Dashboard „Log nähert sich 1 GB (912 MB)"
- Vor dem Leeren gilt die Historie als vollständig. Der Zeitstrahl behält seine
  abgeschlossenen Einträge, die stehen ja bereits in `history.jsonl`
- Schwelle konfigurierbar über `MAX_LOG_MB`, Standard `1024`
- Greift nur auf Pfade, die `/tmp/escha_server*.log` oder `/tmp/slotmon.log` entsprechen.
  Kein `find` über `/tmp`, kein Globbing über fremde Dateien

## Konfiguration

| Variable | Standard | Bedeutung |
|---|---|---|
| `PORT` | `8090` | Port des Dashboards |
| `HOST` | `0.0.0.0` | Bind-Adresse |
| `API` | `http://127.0.0.1:30000` | sglang-Basis-URL |
| `AUTH` | `Bearer marec` | Header für `/get_load` |
| `SERVER_LOG` | `/tmp/escha_server_lan.log` | Log des Servers |
| `MONITOR_LOG` | `/tmp/slotmon.log` | Log des Monitors |
| `HISTORY` | `/tmp/slotmon/history.jsonl` | Historie-Datei |
| `RETENTION_H` | `24` | Aufbewahrung der Historie |
| `MAX_LOG_MB` | `1024` | Schwelle für `truncate` |
| `WARN_LOG_MB` | `900` | Schwelle für die Warnung |
| `POLL_INTERVAL` | `2` | Poll-Takt in Sekunden |

## Zugang

`0.0.0.0:8090`, ohne Auth. Der Monitor zeigt ausschließlich Metadaten der eigenen
Infrastruktur — IP-Adressen, Systemnamen, Laufzeiten, Tokenzahlen. Keine Prompts, keine
API-Keys, keine Inhalte.

## Lifecycle

Manuell, kein systemd-Autostart. Der Server läuft dauerhaft, der Monitor soll
ausschaltbar sein.

```bash
./monitor.sh start
./monitor.sh status
./monitor.sh stop
```

## Testing

Gegen die echten Daten des laufenden Servers, im Nur-Lese-Modus.

1. **Erkennt den echten Halter** — Monitor starten, 5 s warten. Erwartung:
   `192.168.9.2:53268` als Halter, alle anderen als Wartende.
2. **Erkennt einen Wechsel** — kleiner echter Request durch, während ein anderer wartet.
   Erwartung: neuer Halter erscheint, alter Eintrag mit Dauer und Ø tok/s in der Historie.
3. **`ss`-Delta-Logik gegen Fixtures** — aufgezeichnete Snapshot-Paare: gleiche Paare →
   kein Halter, ein Paar mit wachsendem `bytes_sent` → Halter. Läuft ohne Server,
   entkoppelt von Timing.
4. **IP-Auflösung** — bekannte IP → Name, unbekannte IP → rohe IP, kaputte Syntax →
   Startabbruch.
5. **Truncate** — 20-MB-Testdatei, Schwelle testweise 5 MB: Datei existiert, Größe 0.
   Umkehrfall: offene Datei, truncate, weiter schreiben, Inhalte vorhanden, Deskriptor
   gültig.
6. **Historie-Zeitfenster** — JSONL mit Einträgen von vor 2 h und vor 30 h,
   `RETENTION_H=24` → nur die jungen geladen, Datei danach gekürzt.
7. **Server-Ausfall** — `/get_load` künstlich fehlerhaft (Fixture, kein Eingriff am echten
   Server) → Kacheln zeigen Alter statt Zahl, Monitor läuft weiter.
8. **Doppelstart** — `monitor.sh start` zweimal → zweiter Aufruf meldet „läuft bereits",
   Exit 0.

### Was nicht getestet wird

Ein mehrtägiger Dauerlauf mit ~422 Wartenden lässt sich nicht simulieren. Wird nicht
behauptet.

### Kein Eingriff in den Server

Kein Test schreibt auf den echten Server, ändert eine seiner Flags oder killt einen
seiner Prozesse. `monitor.sh` fasst nur eigene PIDs an. Das `pgrep`-Muster `[s]lotmon`
trifft den sglang-Server nicht, dessen Kommandozeile lautet `sglang.launch_server`.