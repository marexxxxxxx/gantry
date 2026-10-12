# GPU-Slot-Monitor — Design

Datum: 2026-10-07
Status: Design freigegeben, am 2026-10-07 17:20 nach Messungsbefund korrigiert
(Architektur der Halter-Erkennung ersetzt, siehe *Korrektur vom 2026-10-07*)

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
- `log_requests: False` laut `/get_server_info`, **trotzdem** schreibt der Server
  `request.received` / `request.finished` mit `rid` ins Log. Prompts enthält das Log
  weiterhin nicht.
  **Nachtrag (13.10.):** Diese Messung galt dem laufenden Server. Damit die
  Halter-Erkennung **dauerhaft** funktioniert, setzt `runtime/sglang/serve.sh`
  `ENABLE_LOG_REQUESTS` per Default auf 1 und damit `--log-requests
  --log-requests-format json` — der Server, der am 12.10. neu gestartet wurde,
  meldet entsprechend `log_requests: True`. Das ist eine Änderung der
  **Startkonfiguration**, nicht des laufenden Servers: der Monitor selbst stellt
  nichts ein und ruft insbesondere `/configure_logging` nicht auf. Ausgeschaltet
  werden kann sie mit `ENABLE_LOG_REQUESTS=0`; dann meldet der Monitor
  `unknown` mit Begründung, die Warteschlangenzahl funktioniert weiter.
- `/metrics` (ohne Auth) liefert `sglang:gen_throughput`, `sglang:num_queue_reqs`,
  `sglang:num_running_reqs`
- Log: `/tmp/escha_server_lan.log`, ist `fd 1` **und** `fd 2` von PID 145265
- `/metrics` braucht keinen API-Key, `/get_load` braucht `Authorization: Bearer marec`

**Konsequenz:** Prompt-Text ist ohne Server-Änderung nicht auslesbar. Der Monitor zeigt
daher ausschließlich Metadaten: Systemname, IP:Port, Laufzeit, Token, Kontextfüllung,
Durchsatz, `rid`.

## Korrektur vom 2026-10-07 — die Socket-Methode ist widerlegt

Die erste Fassung dieser Spec erkannte den Slot-Halter über Datenfluss auf
Socket-Ebene (`ss`, Differenz auf `bytes_sent`). Diese Methode ist widerlegt. Sie
funktionierte in der ersten Messung um 06:53 und fiel später stillschweigend weg, ohne
dass ein Fehler sichtbar geworden wäre — der gefährlichste der möglichen Fälle.

Drei unabhängige Messungen am 2026-10-07 um 17:17:

**Messung 1 — 40 Sekunden Sampling.** `num_waiting_reqs` 2303 → 2301, offene
TCP-Verbindungen auf `:30000` durchgehend **0**. Gleichzeitig schrieb das Server-Log
durchgehend `Decode batch, #running-req: 1, #full token: 5637 → 5677`. Ein Request
erzeugte also nachweislich Tokens, während null TCP-Verbindungen existierten.

**Messung 2 — Gegenprobe, ob `ss` überhaupt alles sieht.** Ein eigener `curl` gegen
`/v1/chat/completions` während laufendem Sampling: die Verbindung erschien sofort
(`ESTAB:30000` 0 → 2) und blieb während der gesamten Wartezeit sichtbar. `ss`
funktioniert also einwandfrei und es werden keine Verbindungen übersehen. Die 2301
anderen Wartenden haben schlicht keine offenen Sockets.

**Messung 3 — FDs des Serverprozesses.** PID 145265, Netzwerk-Namespace identisch mit
unserem (`net:[4026531833]`), also kein Namespace-Problem. Von neun Socket-Deskriptoren
ist über Inode-Abgleich mit `/proc/net/tcp{,6}` genau einer ein echter TCP-Socket:
fd 58, `LISTEN`. Die übrigen acht sind Unix-Sockets für die IPC zwischen Tokenizer und
Scheduler. Es existiert **keine einzige** ESTAB-Verbindung, obwohl `#running-req: 1`
läuft.

**Folge:** Der laufende Request hat nachweislich keinen offenen Socket. Sockets sind als
Quelle für den Halter unbrauchbar. Die frühere Beobachtung „35 Verbindungen bei 180
Wartenden" war ein einmaliger Zustand, kein Dauerzustand.

Zusätzlich verifiziert: abgebrochene Requests erzeugen **keine** uvicorn-Access-Log-Zeile.
Zwischen 17:13 und 17:16 gab es dort keine einzige `POST /v1/chat/completions`-Zeile,
obwohl in diesem Fenster 11 Requests fertig wurden. Die IP:Port-Angabe existiert
ausschließlich beim normalen Abschluss.

## Wie der Slot-Halter erkannt wird

Erkennungsquelle ist der **Request-Lifecycle im Server-Log**, nicht der Socket.

sglang schreibt `request.received` und `request.finished` als JSON-Zeilen mit `rid` in
`/tmp/escha_server_lan.log`. Verifiziert am 2026-10-07 um 17:18:

```
17:18:53 request.finished rid=baa48edfa80f479489a90ff1e3180fca
17:19:07 request.received rid=a93031d87de846e187ddc80afcfb73eb
17:19:24 request.finished rid=d7d50a43cae04ef3a11f625fc1939662
```

Diese Events laufen, obwohl `/get_server_info` weiterhin `log_requests: false` meldet.
Der Monitor liest sie nur, er stellt nichts ein — insbesondere ruft er
`/configure_logging` **nicht** auf, auch wenn die Route existiert.

Bei `--max-running-requests 1` und FIFO-Verarbeitung ist der Halter die **älteste
offene `rid`**: die älteste `request.received` ohne zugehöriges `request.finished`.

```
offene_received   = |{rid aus request.received}| − |{rid aus request.finished}|
altbestand_offen  = num_waiting_reqs − offene_received
halter_rid        = älteste offene received-rid
```

Der `obj`-Block von `request.finished` liefert zusätzlich `received_time`,
`prompt_tokens`, `completion_tokens`, `cached_tokens`, `finish_reason` und
`finish_reason.matched`. Daraus lassen sich Laufzeit, Tokenzahlen und Kontextfüllung
eines abgeschlossenen Slots berechnen — mehr Daten als die ursprüngliche
Socket-Methode sie geliefert hätte.

### Die IP erscheint erst beim Abschluss

Für laufende Requests enthält das Log **keine** IP-Adresse. `request.received` führt
kein IP-Feld, und für den noch offenen Request existiert keine Access-Log-Zeile. Die
IP:Port des Halters steht ausschließlich in der uvicorn-Zeile, die denselben Request
beim Abschluss abschließt.

Damit ist die Zuordnung Halter → IP **abschluss-basiert**. Der Monitor zeigt die IP
des aktuell laufenden Slots erst, wenn der nächste Abschluss beobachtet wurde, und
kennzeichnet sie bis dahin als `noch nicht abgeschlossen`. Für fertige Einträge in der
Historie steht sie fest. Das war in der ersten Fassung der Spec für die Historie bereits
akzeptiert und gilt jetzt für den Halter selbst.

Der Zeitstempel der Access-Log-Zeile muss zum `request.finished` derselben `rid`
passen. Der Monitor verknüpft die zuletzt gesehene Access-Zeile mit der zuletzt
abgeschlossenen `rid` und prüft, dass der Zeitabstand kleiner als
`LOG_PAIR_WINDOW_S` (Standard 30 s) ist. Passt er nicht, bleibt die IP leer — besser
eine Lücke als eine falsche Zuordnung.

### Der Altbestand

Zum Zeitpunkt der Messung waren rund 2301 Requests in der Queue, die **vor**
Aktivierung der Events eingegangen waren. Ihre `rid` kennt der Monitor nicht, sie lassen
sich nicht einzeln abbrechen und nicht zuordnen. Für sie gilt:

```
altbestand_offen = 2301
halter_rid       = unbekannt
```

**Solange `altbestand_offen > 0`, ist der Halter nicht bestimmbar.** Bei FIFO ist er der
älteste offene Request, und das ist in diesem Zustand ein Altbestand-Request. Das
Dashboard zeigt dann ausdrücklich `Halter: unbekannt (Altbestand: 2301)` und behauptet
keinen Namen.

Nach Entscheidung des Nutzers wird **nichts getan**, um das zu beschleunigen: kein
`abort_all`, kein Eingriff in fremde Arbeit. Das Tempo ist normal (~1 Abschluss pro 12 s),
die Queue damit in Stunden abgearbeitet. Sobald `altbestand_offen` auf 0 fällt, arbeitet
die Halter-Erkennung exakt.

Der Monitor muss den Startzustand sauber vom Live-Zustand trennen: Beim Start liest er
den Log von hinten, um die zuletzt gesehene `rid` zu bestimmen, und zählt
Altbestand aus der Differenz zur Queue-Länge.

### Die Warteliste bleibt eine Zahl

Aus Sockets ist keine Zuordnung ableitbar, und das ist nach Messung 1 und 2 endgültig
belegt. Belastbar ist allein `num_waiting_reqs` aus `/get_load`. Das Dashboard zeigt
deshalb **keine** Liste von wartenden Systemen, sondern eine einzige Kachel mit dieser
Zahl. Die Zuordnung einzelner Wartender zu Systemen ist ohne Eingriff in den Datenpfad
nicht feststellbar und wird nicht behauptet.

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
| `tail -c` auf `/tmp/escha_server_lan.log` | `request.received` / `request.finished` mit `rid` und `meta_info`; außerdem `#full token` und `#queue-req` aus den Batch-Zeilen |

`socket`/`ss` wird **nicht mehr abgefragt**. Die Quelle ist als widerlegt dokumentiert und
darf nicht als Reserve zurückkehren.

Der Log wird inkrementell gelesen: der Monitor merkt sich die zuletzt gelesene
Byte-Position und liest ab dort weiter, statt jedes Mal `tail -c` über dieselben Bytes
laufen zu lassen. Wird die Datei extern gekürzt (`truncate -s 0`), erkennt der Monitor
das daran, dass die aktuelle Dateigröße kleiner ist als die gemerkte Position, und
beginnt von vorn.

### `slotmon/history.py` — Persistenz

Schreibt abgeschlossene Slot-Übergaben als eine JSON-Zeile nach
`/tmp/slotmon/history.jsonl`. Beim Start werden nur die letzten 24 h gelesen, die Datei
beim nächsten Schreiben auf diesen Stand gekürzt. Überlebt einen Neustart, wächst
nicht unbegrenzt.

### `slotmon/server.py` — Dashboard

FastAPI/uvicorn. Beides ist im systemweiten `python3.12` vorhanden
(`python3.12 -c "import fastapi, uvicorn, httpx"`). Eine Seite, die sich per
SSE aktualisiert statt zu pollen. Bindet auf `0.0.0.0:8090`, ohne Auth.

**Abhängigkeiten:** kein pytest, kein `pip install` (PEP 668, externally-managed).
Tests laufen über stdlib `unittest`, Ausführung mit
`python3.12 -m unittest discover -s tests`. Kein venv nötig, keine Neuinstallation.

### `systems.json` — IP → Name

```json
{ "192.168.9.2": "Laptop (Claude Code)" }
```

Unbekannte IP fällt auf die rohe IP zurück, nie auf einen Fehler.

## Datenfluss je Poll-Zyklus

1. Neues Log-Segment ab der gemerkten Position lesen. Daraus:
   - jede `request.received` als offene `rid` mit `received_time` aufnehmen
   - jede `request.finished` schließt ihre `rid`, liefert Tokenzahlen und `finish_reason`
   - passende uvicorn-Access-Zeile im selben Segment liefert `ip:port` für diese `rid`
     (nur wenn der Zeitabstand kleiner als `LOG_PAIR_WINDOW_S` ist)
2. `/get_load`, `/metrics`, `nvidia-smi` lesen → aktuelle Zahlen
3. `offene_received` und `altbestand_offen` berechnen, Halter als älteste offene `rid`
   bestimmen
4. Halter-`rid` → Name über `systems.json`; fehlt die IP noch, Name bleibt leer
5. Mit dem letzten bekannten Halter vergleichen:
   - **gleiche `rid`** → laufender Slot: `started_at` bleibt, Laufzeit hochzählen,
     Fortschritt aus `#full token`, `gen_throughput` in die Sparkline
   - **andere `rid`** → alter Eintrag wird abgeschlossen (`ended_at`, Dauer, letzter
     ctx-Wert, Ø tok/s) und landet in `history.jsonl`; neuer Eintrag startet mit
     `started_at` aus `received_time`
   - **keine offene `rid`** → Slot ist frei. Kein Nachfolger wird benannt, weil sich die
     Reihenfolge der Wartenden nicht ableiten lässt (siehe *Bekannte Grenzen*)

### Bekannte Grenzen

- **Solange `altbestand_offen > 0`, ist der Halter nicht bestimmbar.** Das ist kein Bug,
  sondern der Zustand der Queue. Der Monitor zeigt den Altbestand-Zähler prominent, damit
  klar ist, *warum* kein Name dasteht. Er behauptet in diesem Zustand keinen.
- **Die IP des laufenden Halters erscheint erst nach seinem Abschluss.** Abschluss-basiert
  und im Dashboard als `noch nicht abgeschlossen` gekennzeichnet.
- Der Halter-Wechsel wird erst erkannt, wenn der nächste Halter tatsächlich sendet. Im
  Extremfall steht der alte Name wenige Sekunden zu lang.
- **Die Warteliste lässt sich keinem System zuordnen.** Nach den Messungen vom
  2026-10-07 ist das nicht lösbar: die Wartenden haben keine offenen Sockets. Belastbar
  ist allein `num_waiting_reqs`. Der Monitor zeigt eine Zahl und keine Namen und
  behauptet keine Zuordnung.

Die ersten beiden Punkte stehen als Hinweis im Slot-Bereich, der dritte als Fußnote an
der WARTEND-Kachel.

### Abgeleitete Werte

- **WARTEZEIT** — Schätzung aus Queue-Länge mal mittlerer Slot-Dauer. Die mittlere
  Dauer kommt aus den abgeschlossenen Einträgen der Historie; solange die Historie leer
  ist, wird sie aus der aktuellen Laufzeit des laufenden Slots genommen. Fehlt auch die,
  zeigt die Kachel `–` statt einer erfundenen Zahl. Die Kachel ist als Schätzung
  gekennzeichnet.
- **Position des NÄCHSTEN** — entfällt. Ohne belastbare Zuordnung der Wartenden zu
  Verbindungen wäre jede Position geraten. Stattdessen zeigt der Zeitstrahl bei freiem
  Slot `BEREIT FÜR NÄCHSTEN` ohne Namen.
- **Ausgabentokens eines abgeschlossenen Slots** — `completion_tokens` aus `meta_info`.
  `prompt_tokens` entspricht in etwa der Vorverarbeitungslänge, `cached_tokens` dem
  Radix-Cache-Treffer. Diese Werte stehen erst nach dem Abschluss fest.
- **Kontextfüllung** — im laufenden Slot aus `#full token` des Server-Logs, weil dort
  `completion_tokens` noch nicht existiert. Nach dem Abschluss steht der exakte Wert aus
  `meta_info` bereit.
- **Ø tok/s je abgeschlossener Eintrag** — Mittel der über die Lebensdauer gemittelten
  Durchsätze, nicht der Summe.

## Dashboard-Aufbau

Zwei Reiter. Der Reiterwechsel ändert nur die Ansicht, nicht die Daten; die Werte
aktualisieren sich im Hintergrund weiter. „Verlauf" merkt sich seine Position.

**Reiter „Jetzt"**
- KPI-Raster (6): SLOT (BELEGT/frei), WARTEND (`num_waiting_reqs`), TOK/S, GPU, VRAM,
  WARTEZEIT. Eine Kachel VERBINDUNGEN gibt es nicht mehr — sie beruhte auf der
  widerlegten Socket-Annahme.
  *(Zählung nachträglich von 5 auf 6 korrigiert: die Fehlerbehandlungstabelle unten
  verlangt eine VRAM-Kachel, die hier ursprünglich fehlte. Der Satz war in sich
  widersprüchlich.)*
- **Altbestand-Banner**, nur sichtbar wenn `altbestand_offen > 0`: orangefarbene Warnzeile
  `Altbestand: 2301 Requests vor Aktivierung der Log-Events — Halter unbekannt, bis die
  Queue auf 0 ist`. Der Grund steht dabei, nicht nur die Zahl.
- Große Karte des aktiven Halters: grüner Rand, Badge `HAT DEN SLOT`, darunter Systemname
  soweit die IP schon bekannt ist, sonst `IP noch nicht abgeschlossen` in Grau. Die
  zugrunde liegende `rid` steht klein als technische Angabe darunter.
- Vier Kennzahlen: LÄUFT (seit `received_time`), TOK/S (`gen_throughput`), CTX
  (`#full token` aus dem Server-Log), AUSGABEN in Prozent
- Fortschrittsbalken: `AUSGABEN = #full token / max_req_input_len`. `max_req_input_len`
  kommt einmalig aus `/get_server_info` (83994). Der Balken misst die Füllung des
  Eingabekontexts, nicht die Antwortlänge — er zeigt, wie viel vom Fenster verbraucht ist
- **Keine Wartelisten-Tabelle.** Sie ist durch den Altbestand-Banner und die
  WARTEND-Kachel ersetzt. Das Dashboard sagt in einem Satz, dass sich einzelne Wartende
  keinem System zuordnen lassen.

**Reiter „Verlauf"**
- Zeitstrahl der Slot-Übergaben: LÄUFT (grün), FERTIG (grau)
  mit Dauer, ctx, tok/s und Tokenzahlen je Eintrag
- Sparkline tok/s der letzten 60 s
- Summen: Übergaben, Ø Dauer, verbleibende Queue
- Einträge, deren Systemname erst durch den Abschluss bekannt wurde, zeigen ihn mit dem
  Hinweis `Name nachträglich`

## Fehlerbehandlung

Grundsatz: Ein Fehler im Monitor darf nie als Signal erscheinen. Jeder Datenpunkt hat
einen bekannten Zustand statt eines geratenen Werts.

| Fehler | Verhalten |
|---|---|
| `/get_load` oder `/metrics` nicht erreichbar | Quellen als getrennte Altersangaben führen. Ist nur eine tot, zeigen die übrigen Kacheln weiter; das Slot-Feld wird grau mit „verbunden mit Server: nein". Monitor läuft weiter und erholt sich ohne Neustart. |
| Keine offene `rid` im Log | Slot-Karte: „frei", mit Wartezeit. Kein Name wird erfunden, kein Nachfolger benannt. |
| Log-Datei wurde extern gekürzt | Reader erkennt die geschrumpfte Größe, beginnt von vorn, setzt die offenen `rid` zurück und zeigt bis zum nächsten `request.received` einen leeren Slot. Kein Absturz, keine doppelten Einträge. |
| Keine passende Access-Log-Zeile zu einer abgeschlossenen `rid` | IP bleibt leer, der Eintrag landet trotzdem mit `rid`, Dauer und Tokenzahlen in der Historie. Der Zeitstempel wird nicht geraten. |
| `num_waiting_reqs` fehlt in `/get_load` | Kachel WARTEND zeigt `–`. Der Altbestand-Zähler wird dann nicht angezeigt, weil seine Berechnung die Queue-Länge braucht. Kein Wert wird geschätzt. |
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
| `LOG_PAIR_WINDOW_S` | `30` | Maximales Zeitfenster, in dem eine Access-Log-Zeile einer `request.finished`-`rid` zugeordnet wird |

`systems.json` braucht nur noch Einträge für Systeme, deren IP aus dem Abschluss-Log
bekannt wird. Die alten Einträge aus der Socket-Ära bleiben gültig, weil sich an der
Zuordnung IP → Name nichts geändert hat.

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

1. **Erkennt den echten Halter** — Monitor starten, warten bis ein Abschluss beobachtet
   wurde. Erwartung: die `rid` aus `request.finished` steht im Zeitstrahl, mit Systemname
   aus der zugehörigen Access-Log-Zeile. Keine Festlegung auf eine bestimmte IP, die
   ändert sich mit dem Client.
2. **Erkennt einen Wechsel** — kleiner echter Request durch, während ein anderer wartet.
   Erwartung: neuer Halter erscheint, alter Eintrag mit Dauer und Ø tok/s in der Historie.
3. **Log-Parser gegen Fixtures** — echte Logzeilen als Fixtures: `request.received` öffnet
   eine `rid`, `request.finished` schließt sie, zwei `received` derselben `rid` werden
   nicht doppelt gezählt, `finished` ohne vorheriges `received` zählt als Altbestand.
   Läuft ohne Server, entkoppelt von Timing.
4. **Altbestand-Rechnung** — Fixture: 3 offene `rid`, `num_waiting_reqs: 2301` → 2298
   Altbestand, Halter wird als unbekannt gemeldet und nicht geraten.
5. **IP-Zuordnung zum Abschluss** — Fixture mit `request.finished` und einer
   Access-Log-Zeile 4 s später → IP wird übernommen. Gegenfall: Access-Zeile 90 s später →
   IP bleibt leer, weil sie das Zeitfenster `LOG_PAIR_WINDOW_S` verfehlt.
6. **IP-Auflösung** — bekannte IP → Name, unbekannte IP → rohe IP, kaputte Syntax →
   Startabbruch.
7. **Truncate** — 20-MB-Testdatei, Schwelle testweise 5 MB: Datei existiert, Größe 0.
   Umkehrfall: offene Datei, truncate, weiter schreiben, Inhalte vorhanden, Deskriptor
   gültig.
8. **Historie-Zeitfenster** — JSONL mit Einträgen von vor 2 h und vor 30 h,
   `RETENTION_H=24` → nur die jungen geladen, Datei danach gekürzt.
9. **Server-Ausfall** — `/get_load` künstlich fehlerhaft (Fixture, kein Eingriff am echten
   Server) → Kacheln zeigen Alter statt Zahl, Monitor läuft weiter.
10. **Doppelstart** — `monitor.sh start` zweimal → zweiter Aufruf meldet „läuft bereits",
    Exit 0.
11. **Kein Socket wird abgefragt** — der Collector-Quellcode enthält keinen Aufruf von
    `ss` oder `/proc/net/tcp`. Der Test greift das als Zeichenkette ab, damit die
    widerlegte Quelle nicht unbemerkt zurückkehrt.

### Was nicht getestet wird

Ein mehrtägiger Dauerlauf mit über 1400 Wartenden lässt sich nicht simulieren. Wird nicht
behauptet. Ebenso: welcher Prozess sich hinter einem der Altbestand-Requests verbirgt — der
Monitor kennt deren `rid` nicht und behauptet das auch nicht.

Der Übergang vom Altbestand- in den Normalzustand wird nicht abgewartet. Der Monitor muss
mit beiden Zuständen von Anfang an umgehen können, weil die Queue zum Zeitpunkt der
Messung nicht leer war.

### Kein Eingriff in den Server

Kein Test schreibt auf den echten Server, ändert eine seiner Flags oder killt einen
seiner Prozesse. `monitor.sh` fasst nur eigene PIDs an. Das `pgrep`-Muster `[s]lotmon`
trifft den sglang-Server nicht, dessen Kommandozeile lautet `sglang.launch_server`.