# Design: Terminal-Ansicht, Auftragsabbruch und Autostart

Datum: 2026-10-12
Folgt auf: `2026-10-07-gpu-slot-monitor-design.md` (der Slot-Monitor selbst)

## Warum es das gibt

Der Monitor beantwortet „wer hält den Slot" — aber nur im Browser. Drei Lücken:

1. **Kein Autostart.** Nach einem Reboot läuft nichts, bis jemand `./monitor.sh start`
   tippt.
2. **Nur im Browser.** Wer nebenbei im Terminal arbeitet, muss den Browser aufmachen.
3. **Kein Eingriff.** Der Monitor ist reine Anzeige. Wer eine Queue seit 40 Minuten
   blockiert vor sich herschiebt, muss zum Server wechseln.

Die ersten beiden sind Komfort und gehören sich. Die dritte ist eine
Entscheidung, die der Nutzer ausdrücklich getroffen hat — mit einer Grenze, siehe
unten.

## Was der Nutzer entschieden hat

| Frage | Antwort |
|---|---|
| Was soll das Terminal-Werkzeug können? | Auch in die Queue eingreifen |
| Welche Form? | `rich` (Live-Tabelle) |
| Umfang des Abbruchs | **Nur den laufenden Halter** |
| Aktualisierung | Live, alle 2 Sekunden |
| Autostart | Beim Boot **und** automatischer Neustart |

Die Einschränkung „nur den laufenden Halter" ist keine Vorsicht von uns, sondern eine
Grenze der Datenlage (siehe „Die Grenze, die nicht auflösbar ist").

## Die Randbedingung, die sich ändert

Bisher galt: **der Monitor ist rein passiv**, er schreibt nie zum Server. Diese
Randbedingung ist aus genau einem Grund festgelegt worden: der Server darf nicht
unverändert arbeiten, weil auf dieser Maschine andere Agents ihre Arbeit in der
Queue haben.

Der Nutzer hat entschieden, den laufenden Auftrag abbrechen zu können. Das ist ein
bewusster Eingriff in den Datenpfad — der erste in diesem Projekt. Deshalb:

- Der Eingriff ist **auf genau eine Sache beschränkt**: den Auftrag, den der Monitor
  ohnehin schon als „Hält den Slot" anzeigt.
- Der Nutzer muss ihn **bestätigen**, und der Monitor **nennt ihn vorher**.
- Nichts geschieht, was der Monitor nicht schon weiß.

Was **nicht** passiert, und das ist der wichtigste Satz dieses Dokuments:
**Der Monitor fasst nie die Warteschlange an.** Nicht über `abort_all`, nicht über
eine fremde rid, nicht durch Raten.

## Architektur

Drei neue Teile, jeder mit genau einer Aufgabe.

```
slotmon/tui.py                        NEU   Terminal-Ansicht (rich), liest nur
slotmon/abort.py                      NEU   der EINE schreibende Zugriff
slotmon/server.py                     +1    POST /api/abort--holder
runtime/sglang/slotmon-monitor.service NEU  systemd-User-Unit
```

**Die Regel, die alles zusammenhält:** `abort.py` ist die einzige Datei im ganzen
Monitor, die je ein `POST` macht. Jede andere bleibt lesend. Das ist kein Wunsch,
sonden prüfbar — `tests/test_no_socket_source.py` sucht bereits nach Netzwerk-
zugriffen und wird um genau diese Regel erweitert.

### Warum ein HTTP-Endpunkt und nicht direkt aus dem Terminal

Das Terminal liest seine Daten aus dem **laufenden** Monitor-Prozess. Würde es
selbst posten, hätte der Monitor nichts davon gewusst — und Slot-Karte,
Zeitstrahl und Historie zeigten noch 10 Sekunden lang einen Halter, den es nicht
mehr gibt. Über den Endpunkt weiß **ein einziger** Prozess davon, und alle drei
Anzeigen stimmen im selben Tick.

## Der Abbruch

### Endpunkt

```
POST /api/abort--holder
```

Ohne Body. Ohne Parameter. Ohne Query.

Das ist Absicht. Ein `?rid=` wäre die Tür, durch die jemand eine fremde rid
abbricht — genau der Fall, den die Nutzerentscheidung ausschließt. Es gibt nur ein
Ding, das man abbrechen kann, und es hat keine Parameter.

### Die Sicherheitskette

Jede Stufe einzeln, alle vor dem POST:

| # | Prüfung | Verhalten bei Nichterfüllung |
|---|---|---|
| 1 | `slot.state == "busy"` und `slot.rid` | `{aborted: false, reason: "Kein Auftrag laeuft"}` |
| 2 | `slot.legacy == 0` | `{aborted: false, reason: "Halter nicht eindeutig (Altbestand)"}` |
| 3 | `slot.reason` ist leer | `{aborted: false, reason: slot.reason}` |
| 4 | `abort_all` ist nie gesetzt | Konstruktionsbedingt, nicht geprüft |

**Warum Stufe 2.** Solange ein Altbestand in der Queue liegt, ist die Reihenfolge
der Wartenden nicht ableitbar (Spec des Grundmonitors, Z. 149-155). Der Monitor
zeigt in diesem Zustand keinen Halter — und der Abbruch baut genau darauf. Eine
dort abgeleitete rid wäre geraten, und geraten wird nicht abgebrochen.

**Warum Stufe 3 — und warum sie im Normalfall toter Code ist.** `reason` wird
ausschließlich bei `state == "unknown"` gesetzt (`collector.py:715`: „`unknown`
ohne `reason` ist verboten"), und `busy` heißt per Konstruktion `reason == null`.
Bei erfüllter Stufe 1 ist Stufe 3 also **immer wahr**.

Sie bleibt trotzdem, und zwar aus einem Grund, der sich erst später zeigt: Stufe 1
prüft `state`, nicht `reason`. Sobald jemand `state` erweitert — ein vierter
Zustand, ein behobener Geister-Fall —, ist Stufe 3 die Stelle, die den
Widerspruch fängt, bevor er einen Auftrag beendet. Der Test muss sie deshalb
erzwingen können, indem er einen Zustand mit `busy` **und** gesetztem `reason`
baut, den `tick()` heute nicht liefert. Ein Test, der nur den echten Pfad prüft,
sähe diesen Zweig nie.

**Der Server hat eine eigene Schicht**, die wir nur dokumentieren:
`tokenizer_manager.abort_request()` gibt bei `if not abort_all and rid not in
self.rid_to_state: return` sofort zurück. Eine unbekannte rid ist dort ein No-Op.
Wir verlassen uns nicht darauf — die eigene Kette steht vorher.

**Und hier liegt der eine Fall, in dem der Monitor sich nicht absichern kann.**
`slot.rid` kommt aus dem Log, nicht vom Server. Es kann eine rid sein, die der
Server nicht (mehr) kennt: ein abgebrochener Request, ein beim Stopp verworfener,
eine Geister-rid. Die eigene Kette prüft das nicht — sie kann es nicht, sie kennt
den Serverzustand nicht.

Ein `busy` mit solcher rid entsteht, wenn der `num_reqs`-Zeuge **noch** `num_reqs=1`
meldet, obwohl der Log eine alte rid zeigt; in dem Fenster zwischen beiden
Beobachtungen ist das der Normalfall, nicht die Ausnahme.

In diesem Fall ist der POST ein **No-Op auf der Serverseite**: `rid_to_state`
kennt die rid nicht, also passiert nichts. Das ist das sichere Ergebnis — kein
fremder Auftrag wird getroffen. Die Antwort behauptet trotzdem nicht
„abgebrochen"; sie sagt „unbekannt beim Server, nichts passiert", und das Terminal
zeigt das als **grau**, nicht als Erfolg.

Die dritte Zeile der Fehlerbehandlung („Antwort kam, Auftrag läuft weiter") deckt
dasselbe Feld aus der anderen Seite ab: Der Server kann die Annahme auch
zurückweisen, statt stillzuschweigen.

### Antwort

Immer HTTP 200 mit einem Grund. Nie 500, weil jemand eine Taste gedrückt hat.

```json
{"aborted": false, "reason": "Kein Auftrag laeuft - nichts zu tun"}
{"aborted": false, "reason": "Halter nicht eindeutig (Altbestand 3)"}
{"aborted": true,  "rid": "767c2484...", "reason": "abgebrochen"}
```

### Sichtbarkeit

Der Abbruch erscheint als `finish_reason` im Server-Log. Der Monitor liest ihn im
nächsten Poll, räumt den Halter und schreibt eine Historienzeile. Terminal und
Dashboard zeigen ihn in der Ereignisliste. **Kein Vorgang verschwindet spurlos.**

## Die Terminal-Ansicht

Gestartet mit `./monitor.sh watch`. Sie liest den laufenden Monitor; sie startet
keinen eigenen und sie pollt nicht selbst den Server.

### Anzeige

```
┌ GPU-Slot-Monitor ─────────────────────── 10:14:22 ─┐
  Slot BELEGT            Laufzeit  1m 23s
  Halter    Laptop 53.6 tok/s   ctx 3.4k
  Wartend   5 GPU 95 %   VRAM 14.3 GB

 Verlauf
  vor 2 min   Laptop 56s   3000 tok
  vor 5 min   Dieser Server 12s  340 tok
└─────────────────────────────────────────────────────┘
  [a] Auftrag abbrechen   [q] beenden
```

Alle 2 Sekunden neu aufgebaut. Die Werte kommen aus `/api/state`, also aus
demselben Zustand, den auch das Dashboard zeigt.

### Kein Vollbild

Bewusst. Eine `curses`-Vollbildansicht verdeckt alles andere und lässt sich nicht
nebenbei lesen. Diese Ansicht läuft in einem halben Fenster neben dem Editor. Für
„ansehen und nebenbei arbeiten" ist das der Unterschied, der zählt — und `rich`
liefert die Farben ohne den Aufwand.

### Tasten

| Taste | Wirkung |
|---|---|
| `a` | Löscht den laufenden Auftrag. **Zweimal `a` innerhalb von 3 Sekunden** |
| `q` | Beenden |

**Die Doppelbestätigung** ist keine Absage an den Nutzer. `a` steht in einer
Live-Ansicht, die man nicht immer im Blick hat, und der Preis eines Fehlklicks ist
verlorene Rechenzeit eines anderen Agenten. Beim ersten `a` erscheint nur
„wirklich?".

**Bei freiem Slot** ist `a` grau, der Hinweis lautet „kein Auftrag". Ein Tastendruck
gibt eine Zeile, keine Fehlermeldung.

### Farben

Dieselben wie im Dashboard, damit beide Ansichten dasselbe meinen:

| Bedeutung | Farbe |
|---|---|
| Halter, läuft | grün |
| Wartende, tok/s | hell |
| Halter unbekannt (`unknown`) | grau, **nicht** gelb |
| Kein Auftrag (`free`) | grau |
| Fehler, Abbruch abgelehnt | rot |
| „wirklich?"-Aufforderung | gelb |

**Grau für `unknown`, nicht gelb.** Das ist die eine Stelle, an der die
Terminal-Ansicht vom Dashboard abweichen müsste und nicht darf: `unknown` heißt
„nicht gemessen", nicht „Achtung". Der Nutzer sieht eine leere Stelle, keine
Warnung.

Dieselbe Regel gilt für **Zahlen**: `–` steht für „nicht gemessen", nie `0`. Eine
Wartenden-Zahl von 0 und eine unbekannte Wartenden-Zahl sehen gleich aus, wenn man
sie gleich formatiert — und das ist der ganze Fehler, den der Grundmonitor
vermeiden sollte.

## Autostart

`runtime/sglang/slotmon-monitor.service`. `Linger=yes` ist auf der Maschine bereits
gesetzt, die Unit startet also auch ohne eingeloggten Nutzer.

```ini
[Unit]
Description=GPU-Slot-Monitor (slotmon)
After=network-online.target

[Service]
Type=simple
ExecStart=/home/user/small_qwen/monitor.sh start-systemd
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**`start-systemd` statt `start`:** `monitor.sh start` startet im Hintergrund und
kehrt zurück — für systemd ein sofortiger Erfolg ohne überwachbaren Prozess. Die
Variante bleibt im Vordergrund.

**`Restart=always`** deckt die zweite Antwort ab: Nach einem Absturz des Monitors
startet systemd ihn neu. Weil der Monitor **unabhängig** vom Server läuft — er liest
nur das Log — ist der Regelfall ohnehin unkritisch: Der Server wird neu gestartet,
das Log gekürzt oder ersetzt, der Monitor liest weiter. Kein Eingriff nötig.

**Was nicht gemacht wird:** Keine `Wants=`-/`After=`-Abhängigkeit auf den
sglang-Server. Der Monitor muss ohne Server starten können und `unknown` zeigen —
das ist ein geprüfter Zustand, kein Notfall.

## Fehlerbehandlung

| Fehler | Verhalten | Nicht |
|---|---|---|
| Server nicht erreichbar beim Abbruch | Monitor läuft weiter, eine Zeile mit Grund, Terminal bleibt bedienbar, `q` geht | Kein Abbruch, kein Crash |
| Server antwortet 500 / Timeout | Wie oben, mit dem Grund des Servers | Wie oben |
| Antwort kam, Auftrag läuft weiter | „Zustand unbestätigt"; der nächste Poll fragt nach | Erfolg behaupten |
| Abbruch schlägt fehl | Ereignis in der Liste, danach weiter | Stille |

Die letzten beiden sind getrennt, und die Trennung ist der Punkt: **ein Fehler ist
kein Signal, und ein Erfolg ohne Bestätigung auch nicht.**

## Tests

| Test | Mutant, der ihn töten muss |
|---|---|
| `abort` bei freiem Slot | No-Op mit Grund → echter Abbruch |
| `abort` bei Altbestand | Rid wird doch abgeleitet |
| `abort` bei gesetztem `reason` | Zweifelhafter Halter wird abgebrochen |
| `abort` sendet nie `abort_all` | Feld auf `true` — würde die Queue töten |
| Endpunkt hat keine Parameter | `?rid=` oder `rid` im Body taucht auf |
| Server nicht erreichbar | Exception verlässt den Endpunkt |
| Genau eine schreibende Datei | `grep`-Test weitet sich auf `POST` aus |
| TUI bei `free` / `unknown` / totem Server | Absturz beim Rendern |
| Doppelbestätigung | Ein `a` bricht sofort ab |
| systemd ohne `Wants=` auf den Server | Abhängigkeit von der falschen Unit |
| `start-systemd` bleibt im Vordergrund | Rückkehr → systemd sieht Erfolg ohne Prozess |
| Die bestehenden 408 Tests | Alles läuft weiter |

`tests/test_no_socket_source.py` wird strenger: bisher prüft er, dass niemand
`/abort_request` aufruft. Jetzt gilt „genau eine Datei, mit dem dokumentierten
Pfad" — mit `abort_all` als ausdrücklich verbotenem Feld.

## Abnahme

1. `./monitor.sh watch` zeigt einen laufenden Auftrag.
2. `a` fragt nach, zweimal `a` bricht ihn ab, der Zustand kippt auf `free`.
3. Der Abbruch erscheint in der Verlaufsliste — spurlos verschwinden darf er nicht.
4. `./monitor.sh stop && systemctl --user stop slotmon-monitor` — beides
   widerspruchsfrei.
5. `systemctl --user restart slotmon-monitor` startet ihn ohne Fehler neu.
6. Der Server läuft unverändert weiter, `curl /get_load` antwortet.
7. Die 408 bestehenden Tests bleiben grün.