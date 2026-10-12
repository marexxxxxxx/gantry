# GPU-Slot-Monitor

Zeigt, welcher Auftrag gerade den einen GPU-Slot hält — mit Namen, wenn bekannt.

Der Server wird dabei **nicht angefasst**: kein Neustart, keine Konfigurations­
änderung, kein Eingriff in den Datenpfad. Der Monitor liest nur das Log, das der
Server selbst schreibt, und fragt zwei read-only-Endpunkte ab.

## Loslegen

Aus dem Wurzelverzeichnis des Repos:

```bash
./monitor.sh start        # startet den Monitor im Hintergrund
# http://0.0.0.0:8090 im Browser aufrufen
./monitor.sh status       # eine Zeile Text, gut für Skripte
./monitor.sh stop
```

`start` ist idempotent: ein zweiter Aufruf sagt „laeuft bereits“ und tut nichts.
`status` liefert Exit 1, wenn nichts läuft, Exit 0 wenn doch.

## Was das Dashboard zeigt

Zwei Reiter.

**„Jetzt“** — die grüne Karte mit dem aktiven Halter (Systemname, `rid`, Laufzeit,
tok/s, Kontextfüllung, Fortschrittsbalken), darüber Kennzahlen für Wartende, Durch­
satz, GPU und Wartezeit.

**„Verlauf“** — Zeitstrahl der Slot-Übergaben und der Durchsatz der letzten
60 Sekunden.

## Was es nicht behauptet

Der Monitor sagt drei Dinge **nicht**, weil er sie nicht weiß:

- **Keine Zuordnung der Wartenden.** Die Warteschlange des Servers ist länger als
  die Zahl offener Verbindungen — gemessen am 2026-10-07: 35 Verbindungen bei 180
  Wartenden, später 1 Verbindung bei 1492 Wartenden. Die beiden Zahlen laufen
  gegenläufig. Angezeigt wird deshalb nur `num_waiting_reqs` aus `/get_load`,
  ohne Namen daneben.
- **Keinen Prompt-Text.** Weder im Dashboard noch in der API. Nur Metadaten:
  Tokenzahlen, Laufzeiten, Systemname.
- **Keinen Namen vor dem Abschluss.** Die IP des laufenden Auftrags steht erst in
  der Abschlusszeile des Logs. Bis dahin zeigt die Karte „IP noch nicht
  abgeschlossen“.

## Voraussetzung am Server

Für die Halter-Erkennung braucht der Monitor zwei Zeilen im Server-Log:

```
--log-requests --log-requests-format json
```

`runtime/sglang/serve.sh` setzt beides über `ENABLE_LOG_REQUESTS` (Default: an),
sodass ein neu gestarteter Server sie von selbst bekommt. **Der Monitor stellt
nichts ein** — er liest nur, was da ist.

Ohne diese Zeilen gibt es keine `request.received`/`request.finished`-Zeilen, und
der Monitor meldet `unknown` mit der Begründung, dass keine Events vorliegen.
Die Warteschlangenzahl funktioniert auch ohne.

Abschalten mit `ENABLE_LOG_REQUESTS=0`.

Beim Start liest er das Log **von vorn**. Liegt beim Start noch eine Queue aus
Aufträgen, die vor dem Aktivieren des Loggings eingereiht wurden, zeigen sich
darin keine `request.received`-Zeilen — der Monitor zählt stattdessen die
Differenz zur Warteschlangenlänge und zeigt diesen **Altbestand** als Banner an.
Einen Halter behauptet er erst, wenn die Queue auf 0 steht.

## Konfiguration

| Variable | Default | Bedeutung |
|---|---|---|
| `PORT` | `8090` | Port des Dashboards |
| `HOST` | `0.0.0.0` | Bindeadresse |
| `API` | `http://127.0.0.1:30000` | sglang-Endpunkte |
| `AUTH` | `Bearer marec` | API-Key für sglang |
| `SYSTEMS_FILE` | `systems.json` | IP → Anzeigename |
| `HISTORY` | `/tmp/slotmon/history.jsonl` | abgeschlossene Slots |
| `RETENTION_H` | `24` | wie lange die Historie gilt |
| `MAX_LOG_MB` | `1024` | ab hier wird das Server-Log gekürzt |
| `MAX_MONITOR_LOG_MB` | `5` | ab hier wird das eigene Log gekürzt |
| `WARN_LOG_MB` | `900` | Warnschwelle im Dashboard |
| `POLL_INTERVAL` | `2.0` | Sekunden je Poll |

### `systems.json`

```json
{
  "192.168.9.2": "Laptop",
  "192.168.1.24": "Dieser Server",
  "127.0.0.1": "Lokal (opencode & Co.)"
}
```

Eine fehlende IP ist **kein Fehler** — dann steht die rohe IP da. Eine fehlende
oder kaputte Datei dagegen bricht den Start ab mit klarer Meldung; der Monitor
läuft nicht halb konfiguriert.

## Log-Aufräumen

Ab 1 GB (`MAX_LOG_MB`) wird `/tmp/escha_server_lan.log` auf 0 gekürzt, ab 900 MB
warnt das Dashboard. Beide Werte sind konfigurierbar.

**Nicht `rm`.** Der Server hält die Datei offen (`fd 1` und `fd 2`). Ein `rm`
entfernt nur den Verzeichniseintrag — die Datei lebt als Anonymous-Inode weiter,
der Platz wird erst beim Neustart des Servers frei, und genau der ist hier nicht
erlaubt. `truncate -s 0` setzt die Größe auf 0, der offene Deskriptor bleibt
gültig, der Server schreibt ab Position 0 weiter.

Die Prüfung läuft alle 60 Poll-Zyklen (≈ alle 2 Minuten) und macht genau einen
`stat`.

## Zugang

Das Dashboard lauscht auf `0.0.0.0:8090` **ohne Authentifizierung**. Es gibt nur
Metadaten zu sehen — keine Prompts, keine API-Keys — aber es ist im LAN sichtbar.
Für eine Absicherung auf einen Reverse-Proxy legen.

## Tests

```bash
python3.12 -B -m unittest discover -s tests -q   # Python
bash tests/test_monitor_sh.sh                    # Skript
```

Der Skript-Test startet seine eigenen Monitor auf einem eigenen Port und räumt
sich danach vollständig auf; der laufende Produktivmonitor wird nicht angefasst.

---

→ [English](README.md)