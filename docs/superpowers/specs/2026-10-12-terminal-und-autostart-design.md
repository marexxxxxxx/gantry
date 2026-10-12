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

| Frage | Antwort | Runde |
|---|---|---|
| Umfang des Abbruchs | Nur den laufenden Halter | zuerst |
| Umfang des Abbruchs | **Auch einzelne Wartende**, mit Bestätigung | nach der Messung |
| Queue im Dashboard | **Reihenfolge zeigen**, mit Namens-Rückfall | nach der Messung |
| Form der Terminal-Ansicht | `rich` (Live-Tabelle) | zuerst |
| Aktualisierung | Live, alle 2 Sekunden | zuerst |
| Autostart | Beim Boot **und** automatischer Neustart | zuerst |

**Die erste Runde ist überholt, nicht widerlegt.** Der Nutzer hat den Umfang nach der
Messung erweitert, weil die Reihenfolge der Wartenden plötzlich belegt war — vorher
wussten wir nicht, ob das überhaupt geht. Die Einschränkung „nur der Halter" war nie
eine Vorsicht von uns, sondern eine Grenze der Datenlage; die Grenze ist zur Hälfte
gefallen (siehe „Die Grenze, die sich aufgelöst hat").

Was bleibt: **keine Namen für Wartende.** Und: **immer genau ein Auftrag pro
Abbruch**, nie die ganze Warteschlange.

## Die Grenze, die sich aufgelöst hat

**Die erste Fassung dieses Dokuments behauptet das Gegenteil, und sie lag falsch.**

Der Grundmonitor (Spec vom 2026-10-07) erklärt die Reihenfolge der Wartenden für
nicht ableitbar. Das war eine **Messung**, aber aus einem alten Zustand: Damals stand
`log_requests: False` in `/get_server_info` — die Zeilen mit `rid` waren im Log nur
nachträglich vorhanden.

Seit der Server mit `--log-requests --log-requests-format json` neu gestartet wurde
(2026-10-12), schreibt sglang **`request.received` beim Eintreffen**, also für jeden
Wartenden. Damit ist die Reihenfolge erstmals belegt:

| | Wert |
|---|---|
| Offene `request.received` im Log | 35 |
| `num_reqs` vom Server | 35 |
| `num_waiting_reqs` | 34 |

Der Monitor sieht die Warteschlange vollständig, und sglangs eigener Zähler
`received_time` steigt monoton mit der Warteposition (gemessen 12779.335 →
12779.346 → 12783.395). Das ist die Reihenfolge, in der abgearbeitet wird, nicht eine
Vermutung.

**Die andere Hälfte der alten Grenze bleibt.** Kein wartender Request trägt eine IP —
61 geprüft, 0 Treffer über alle Felder von `request.received`. Die Reihenfolge ist
sichtbar, **die Namen nicht**. Die Warteschlange wird als nummerierte Liste mit rid
und Wartezeit gezeigt, mit dem ausdrücklichen Zusatz, dass der Name erst nach dem
Abschluss feststeht.

Deshalb steht ab jetzt nicht mehr „eine Zahl" im Dashboard, sondern die Liste — und
deshalb heißen die betroffenen Stellen im Grundmonitor-Spec überholt.

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
**Der Monitor fasst nie die ganze Warteschlange an.** Kein `abort_all`, keine
gesammelte Aktion. Jeder Abbruch betrifft **genau einen** Auftrag, und der Nutzer
sieht vorher, welcher es ist. Der Nutzer hat das am 2026-10-12 ausdrücklich
entschieden — nach einer Messung, die die Reihenfolge der Wartenden erstmals sichtbar
gemacht hat.

## Architektur

Vier neue Teile, jeder mit genau einer Aufgabe.

```
slotmon/tui.py                        NEU   Terminal-Ansicht (rich), liest nur
slotmon/abort.py                      NEU   der EINE schreibende Zugriff
slotmon/queue.py                      NEU   offene rids in Reihenfolge, aus dem Log
slotmon/collector.py                  +1    fuehrt `queue` im Zustand mit
slotmon/server.py                     +2    POST /api/abort--holder
                                            POST /api/abort-waiting
slotmon/dashboard.html                +1    Warteschlangenliste mit Abbruch-Knoepfen
runtime/sglang/slotmon-monitor.service NEU  systemd-User-Unit
```

**Die Regel, die alles zusammenhält:** `abort.py` ist die einzige Datei im ganzen
Monitor, die je ein `POST` macht. Jede andere bleibt lesend. Das ist kein Wunsch,
sondern prüfbar — `tests/test_no_socket_source.py` sucht bereits nach Netzwerk-
zugriffen und wird um genau diese Regel erweitert.

**`queue.py` ist rein, weil `collector.py` schon zu groß ist.** Die Warteschlange ist
eine eigene Frage — offene `request.received` nach `received_time` sortieren, gegen
`num_waiting_reqs` prüfen, Namen nachschlagen — und sie hat nichts mit der
Halter-Ablösung zu tun, die in `slot.py` steht. Eine eigene Einheit mit eigener
Aufgabe ist hier billiger als eine weitere Bedingung in `evaluate()`.

**Die Schnittstelle:** `queue.py` bekommt `open_rids` und `waiting` und gibt eine
Liste zurück. Es kennt weder HTTP noch das Dashboard. `collector.py` ruft es auf und
legt das Ergebnis unter `state["queue"]["waiting_list"]` ab; alle drei Anzeigen lesen
dort.

### Warum ein HTTP-Endpunkt und nicht direkt aus dem Terminal

Das Terminal liest seine Daten aus dem **laufenden** Monitor-Prozess. Würde es
selbst posten, hätte der Monitor nichts davon gewusst — und Slot-Karte,
Zeitstrahl und Historie zeigten noch 10 Sekunden lang einen Halter, den es nicht
mehr gibt. Über den Endpunkt weiß **ein einziger** Prozess davon, und alle drei
Anzeigen stimmen im selben Tick.

## Der Abbruch

**Der Nutzer hat die Warteschlange nachträglich einbezogen** (Entscheidung vom
2026-10-12, siehe „Die Grenze, die sich aufgelöst hat"). Deshalb zwei Endpunkte und
eine Warteschlangenliste.

### Endpunkte

```
POST /api/abort--holder     ohne Body, ohne Parameter
POST /api/abort-waiting     {"position": 3}
```

**Warum `abort--holder` keinen Parameter hat.** Ein `?rid=` wäre die Tür, durch die
jemand eine beliebige rid abbrechen könnte. Der Halter ist die einzige Sache, die
der Monitor ohnehin schon anzeigt — für sie braucht es keine Eingabe.

**Warum `abort-waiting` eine Position statt einer rid hat.** Der Nutzer sieht die
Warteschlange als nummerierte Liste; er tippt Position 3. Die rid dahinter sucht
der Monitor **selbst**, im Moment des Klicks. Damit kann der Nutzer keine rid
erfinden oder verwechseln — die Zahl, die er sieht, ist genau die, die gemeint ist.
Die Antwort nennt trotzdem die aufgelöste rid, damit der Nutzer prüfen kann, was
tatsächlich beendet wurde.

### Die Warteschlange ist sichtbar — belegt

| Beobachtung | Wert |
|---|---|
| Offene `request.received` im Log | 35 |
| `num_reqs` vom Server | 35 |
| `num_waiting_reqs` | 34 |

**Übereinstimmung auf die Zahl.** Der Monitor sieht die Warteschlange vollständig
und in der Reihenfolge, in der der Server sie abarbeiten wird: sglangs eigener
Zähler `received_time` steigt monoton mit der Warteposition (gemessen
12779.335 → 12779.346 → 12783.395).

### Die Grenze: kein Name

**Kein einziger wartender Request trägt eine IP.** Gemessen an 61 wartenden
Requests über alle Felder von `request.received` (`custom_labels`,
`session_params`, `background`, …): **null Treffer**. Die IP steht ausschließlich in
`request.finished`, also **nach** dem Abschluss.

Der Halter bekommt seinen Namen, sobald er durch ist — bzw. aus einem früheren
Abschluss derselben `ip_port`. Die Wartenden stehen als **Position mit rid und
Wartezeit**, mit dem ausdrücklichen Zusatz „Name unbekannt, er steht erst nach dem
Abschluss fest".

### Abbruch eines Wartenden

Der Server kann es, und zwar einzeln: `scheduler.abort_request()`
(`scheduler.py:2811`) löscht direkt aus der `waiting_queue`, ohne dem Auftrag
zuzustellen. Das ist ein billiger Abbruch — es geht keine Rechenzeit verloren.

**Der gefährlichste Fund dieses Projekts steht in `io_struct.py:1541`:**

```python
def __post_init__(self):
    # FIXME: this is a hack to keep this the same as the old code
    if self.rid is None:
        self.rid = ""
```

Eine fehlende rid wird zu `""` — und `"irgendeine_rid".startswith("")` ist `True`.
**Eine leere rid würde die gesamte Warteschlange löschen**, ohne dass
`abort_all` gesetzt wäre. `scheduler.py:2814` vergleicht nämlich mit
`recv_req.abort_all or req.rid.startswith(recv_req.rid)`.

Der Server hat zwei Verteidigungslinien, und beide halten:

1. `tokenizer_manager.abort_request()` prüft `if not abort_all and rid not in
   self.rid_to_state: return`. `""` ist kein Schlüssel in `rid_to_state`, also
   greift der Guard und es wird **nichts** gesendet. Die leere rid erreicht den
   Scheduler nie.
2. Unsere eigene Kette sendet nie eine leere rid.

**Wir verlassen uns auf keine der beiden allein.** `abort-waiting` lehnt eine
Position ab, die es nicht gibt, und die aufgelöste rid wird gegen das gelesene Log
geprüft, bevor sie gesendet wird. Ein Test erzwingt den Fall „Position zeigt auf
eine rid, die es nicht (mehr) gibt" und erwartet, dass **nichts** gesendet wird.

### Die Sicherheitskette

Jede Stufe einzeln, alle vor dem POST:

| # | Prüfung | Verhalten bei Nichterfüllung |
|---|---|---|
| 1 | `abort--holder`: `state == "busy"` und `rid` vorhanden | `{aborted: false, reason: "Kein Auftrag laeuft"}` |
| 2 | `abort--holder`: `legacy == 0` | `{aborted: false, reason: "Halter nicht eindeutig (Altbestand)"}` |
| 3 | `abort--holder`: `reason` leer | `{aborted: false, reason: slot.reason}` |
| 4 | `abort-waiting`: Position existiert in der Liste | `{aborted: false, reason: "Position X gibt es nicht"}` |
| 5 | `abort-waiting`: `legacy == 0` | `{aborted: false, reason: "Warteschlangen nicht ableitbar (Altbestand)"}` |
| 6 | Beide: aufgelöste rid steht in dieser Sekunde im Log gelesen | `{aborted: false, reason: "rid nicht (mehr) bekannt"}` |
| 7 | Beide: rid ist nicht leer | Konstruktionsbedingt, aber **getestet** |
| 8 | Beide: `abort_all` ist nie gesetzt | Getestet über den erzeugten Request |

**Warum Stufe 2 und 5.** Solange ein Altbestand in der Queue liegt, ist die
Reihenfolge nicht ableitbar — die Anzahl im Log passt dann nicht zur
Warteschlangenlänge. Der Monitor zeigt in diesem Zustand keinen Halter, und er
zeigt keine Warteschlangenliste. Beide Abbruchwege sind damit von vornherein
geschlossen.

**Warum Stufe 3 toter Code ist und trotzdem bleibt.** `reason` wird ausschließlich
bei `state == "unknown"` gesetzt (`collector.py:715`), und `busy` heißt per
Konstruktion `reason == null`. Stufe 3 ist im Normalfall immer wahr. Sie bleibt als
die Stelle, die den Widerspruch fängt, falls `state` später erweitert wird; ihr Test
muss einen Zustand bauen, den `tick()` heute nicht liefert.

### Was ein abgebrochener Wartender im Log hinterlässt

Ein abgebrochener Wartender **wird nicht** abgearbeitet — er wird aus der
`waiting_queue` gepoppt (`scheduler.py:2820`) und bekommt einen `AbortReq` zurück an
den TokenizerManager. Im Server-Log erscheint dazu ein `request.finished` mit
`finish_reason: "abort"`.

**Das ist eine offene Frage und keine Nebensache.** Der Collector liest
`finish_reason` bereits (`collector.py:313`), **wertet es aber nirgends aus**. Ein
abgebrochener Wartender landet damit in der Historie wie ein normal abgeschlossener
Auftrag — mit einer Dauer, die der Zeit zwischen `request.received` und dem Abbruch
entspricht, also dem gesamten Wartezeitraum.

Zwei Lesarten, und die Wahl ist eine Produktentscheidung:

| Lesart | Folge im Dashboard |
|---|---|
| **Als normaler Abschluss** | Die Historie zählt einen Slot, der nie lief. Die mittlere Slot-Dauer, die im Reiter „Verlauf" steht, wird dadurch verfälscht. |
| **Eigenes Ereignis** | Der Eintrag trägt „abgebrochen", wird nicht in die Dauer-Mittelung gerechnet, zählt aber in „Übergaben" mit. |

**Empfehlung: die zweite.** Ein abgebrochener Auftrag ist kein Slot, und eine
Kennzahl, die Abbrüche als Laufzeit zählt, ist falsch. Die Umsetzung ist eine Zeile in
`_flush_finished`, aber sie gehört **vor** den ersten echten Abbruch — sonst steht
schon eine falsche Zahl im Log.

Zu entscheiden ist außerdem, ob der Wartende **nach** dem Abbruch wieder als
Wartender erscheint. Er tut es nicht: seine `request.finished`-Zeile schließt ihn.
Das ist richtig, aber es heißt auch, dass die Positionen nach einem Abbruch
durchnummeriert verschieben.

### Der Fall, den wir nicht absichern können

`slot.rid` kommt aus dem Log, nicht vom Server. Es kann eine rid sein, die der
Server nicht kennt — abgebrochener Request, Geist. In diesem Fenster ist der POST
ein serverseitiger No-Op: `rid_to_state` kennt sie nicht. Das sichere Ergebnis —
kein fremder Auftrag wird getroffen. Die Antwort behauptet trotzdem nicht
„abgebrochen", sondern „unbekannt beim Server, nichts passiert".

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

## Das Dashboard

Der Nutzer will **beides im Web-Dashboard**: die Warteschlange sehen und Aufträge
abbrechen. Diese beiden Änderungen betreffen die bestehende Seite.

### Die Warteschlangenliste

Ersetzt die bisherige Fußnote „Einzelne Wartende lassen sich keinem System zuordnen".

```
 Warteschlange (34)              Altbestand: 0
 ┌────┬────────────────┬─────────┬──────────────────────┐
 │ #  │ rid            │ wartet  │ System               │
 ├────┼────────────────┼─────────┼──────────────────────┤
 │ 1  │ b4fb636b       │ 4m 12s  │ unbekannt †          │
 │ 2  │ 5cc4e1c8       │ 4m 11s  │ unbekannt †          │
 │ 3  │ f8420988       │ 4m 07s  │ unbekannt †          │
 └────┴────────────────┴─────────┴──────────────────────┘
  Position 1 ist als nächster dran.  [ Position 3 abbrechen ]
 † Der Name steht erst nach dem Abschluss fest.
```

**Die Spalten.** Position (1-indexiert, wie angezeigt), gekürzte `rid` klein als
technische Angabe, Wartezeit seit `request.received`, Systemname.

**Der Namens-Rückfall.** Steht zu einer Position ein Name — weil dieselbe
`ip_port` earlier einmal fertig wurde und in `systems.json` steht —, wird er
gezeigt. Sonst „unbekannt †" mit derselben Fußnote. Das ist ehrlicher als eine leere
Spalte: es sagt, dass es **nichts** ist, nicht dass es nichts gibt.

**Warum `Position 1 ist als nächster dran`:** Der Satz behauptet die Reihenfolge, und
sie ist belegt. `received_time` des Servers steigt monoton mit der Warteposition. Ein
`#`-Zeilennummern-Design wäre eine schönere Oberfläche gewesen, aber die
Halbierung der Nummerierung ist es nicht.

**Was wegbleibt.** Kein „vor X Min angemeldet"-Zeitstempel aus dem Prompt — der
Monitor liest keine Prompts, und er soll es nicht.

### Der Abbruch im Dashboard

Eine Schaltfläche **pro Zeile**, nicht ein „Alles abbrechen". Der Nutzer sieht die
Position und klickt genau diese an.

**Bestätigung:** ein Zwischenfenster mit der vollständigen `rid`, der Wartezeit und
dem Satz „Das bricht den Auftrag ab. Es wird kein Ergebnis zurückgegeben." — kein
 blosses „Sind Sie sicher?", das man durchzuckt.

**Nach dem Klick** bleibt die Zeile sichtbar, bis sie verschwindet, und zeigt den
Abendbroch-Zustand („abgebrochen, 12:14:03"). Sie springt nicht sofort weg: der
Nutzer muss sehen, dass es geklappt hat, sonst klickt er zweimal.

**Ist die Position beim Klick schon weg** (ein anderer Agent war schneller), sagt
die Antwort genau das und es wird **nichts** gesendet. Kein Fehler, kein Absturz.

**Der Halter** bekommt dieselbe Schaltfläche, unverändert nach den Regeln oben.

## Die Terminal-Ansicht

Gestartet mit `./monitor.sh watch`. Sie liest den laufenden Monitor; sie startet
keinen eigenen und sie pollt nicht selbst den Server.

### Anzeige

```
┌ GPU-Slot-Monitor ─────────────────────── 10:14:22 ─┐
  Slot BELEGT            Laufzeit  1m 23s
  Halter    Laptop 53.6 tok/s   ctx 3.4k
  Wartend   34 GPU 95 %   VRAM 14.3 GB

 Warteschlange
  1  b4fb636b   4m 12s   unbekannt †
  2  5cc4e1c8   4m 11s   unbekannt †
  3  f8420988   4m 07s   unbekannt †
  …  31 weitere

 Verlauf
  vor 2 min   Laptop 56s   3000 tok
└─────────────────────────────────────────────────────┘
  [a] Halter abbrechen  [w] Wartenden 1 abbrechen  [q] Ende
```

**Warum nur die ersten drei plus „31 weitere".** Bei 34 Wartenden wäre die volle Liste
34 Zeilen lang — in einem Terminal, das man nebenbei offen hält, ist das der Rest
verschwunden. Die drei braucht der Nutzer für die Entscheidung „welcher Auftrag
wartet schon am längsten". Die Zahl der übrigen steht dabei.

Im **Dashboard** ist die Liste vollständig; dort ist Platz. Die Kürzung ist eine
Eigenschaft des Terminals, nicht der Daten.

Alle 2 Sekunden neu aufgebaut. Die Werte kommen aus `/api/state`, also aus demselben
Zustand, den auch das Dashboard zeigt.

### Kein Vollbild

Bewusst. Eine `curses`-Vollbildansicht verdeckt alles andere und lässt sich nicht
nebenbei lesen. Diese Ansicht läuft in einem halben Fenster neben dem Editor. Für
„ansehen und nebenbei arbeiten" ist das der Unterschied, der zählt — und `rich`
liefert die Farben ohne den Aufwand.

### Tasten

| Taste | Wirkung |
|---|---|
| `a` | Löscht den **laufenden** Auftrag. **Zweimal `a` innerhalb von 3 Sekunden** |
| `w` | Löscht den **ersten Wartenden**. Zweimal `w` bestätigt |
| `q` | Beenden |

**Warum zwei Tasten und nicht eine.** Ein einzelnes `a` für „irgendeinen Auftrag"
wäre die gefährlichste Form: Der Nutzer wüsste nicht, ob er den Halter oder Position 1
trifft. Zwei getrennte Tasten bedeuten, dass **jede** Eingabe eine eindeutige
Bedeutung hat.

**`w` trifft immer Position 1** — den nächsten dran. Wer Position 7 meint, wartet auf
Position 1. Das ist gewollt: „den nächsten dran abbrechen" ist die einzige
Wartenden-Aktion, die man im Kopf haben kann, ohne zu zählen. Für alles andere ist
das Dashboard da.

**Die Doppelbestätigung** ist keine Absage an den Nutzer. Beide Tasten stehen in
einer Live-Ansicht, die man nicht immer im Blick hat, und der Preis eines Fehlklicks
ist verlorene Arbeit — beim Halter echte Rechenzeit, beim Wartenden die Wartezeit von
irgendwann. Beim ersten Druck erscheint nur „wirklich?".

**Bei freiem Slot** ist `a` grau, der Hinweis lautet „kein Auftrag". Ein Tastendruck
gibt eine Zeile, keine Fehlermeldung. **`w`** ist grau, wenn niemand wartet.

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
| **Position existiert nicht (mehr)** | „Position 3 gibt es nicht mehr", **nichts** gesendet, Zahl unverändert | Neuer Abbruchversuch, Indexfehler |
| **Position zeigt auf eine unbekannte rid** | Wie oben, mit dem Hinweis auf einen Log-Sprung | Etwas senden |
| **Altbestand > 0** | Keine Liste, kein Abbruch, Banner steht | Liste aus der Warteschlangenlenzahl raten |
| Abbruch schlägt fehl | Ereignis in der Liste, danach weiter | Stille |
| `/api/state` nicht erreichbar (TUI) | Letzte bekannte Werte bleiben stehen, „nicht erreichbar", `q` geht | Auf leere Anzeige umspringen |

Die ersten drei und der letzte sind getrennt, und die Trennung ist der Punkt:
**ein Fehler ist kein Signal, und ein Erfolg ohne Bestätigung auch nicht.**

Die beiden mittleren Zeilen sind dieteuerste Stelle im ganzen Design: Sie treffen
genau den Moment, in dem der Nutzer auf „abbrechen" geklickt hat und der Klick
ins Leere ging. **Statt einer Fehlermeldung bekommt er die Auskunft, dass er zu spät
war** — das ist eine andere Erfahrung als ein Fehler, obwohl beides „hat nicht
geklappt" heißt.

## Tests

### Der Abbruch

| Test | Mutant, der ihn töten muss |
|---|---|
| `abort--holder` bei freiem Slot | No-Op mit Grund → echter Abbruch |
| `abort--holder` bei Altbestand | Rid wird doch abgeleitet |
| `abort--holder` bei gesetztem `reason` | Zweifelhafter Halter wird abgebrochen |
| `abort--waiting` mit Position außerhalb der Liste | Es wird trotzdem etwas gesendet |
| `abort--waiting` mit Position 0 oder negativ | Index-Fehler statt Ablehnung |
| **Position zeigt auf eine rid, die es nicht (mehr) gibt** | Es wird eine fremde rid gesendet |
| **Der erzeugte Request hat nie eine leere rid** | `rid=""` — würde die ganze Queue treffen |
| **Der erzeugte Request hat nie `abort_all=True`** | Feld auf `true` |
| `abort--holder` nimmt keinen `rid`-Parameter | `?rid=` taucht auf |
| Server nicht erreichbar | Exception verlässt den Endpunkt |
| Abbruch ändert den Zustand auch nach Fehler | Zustand bleibt veraltet und meldet nichts |

Der dritte Test ist der wichtigste dieser Liste: `io_struct.py:1541` macht aus einer
fehlenden rid `""`, und `"x".startswith("")` ist `True` — der Scheduler würde die
**ganze Warteschlange** löschen. Der Test prüft den erzeugten Request, nicht die
Absicht.

### Die Warteschlangenliste

| Test | Mutant, der ihn töten muss |
|---|---|
| Reihenfolge = aufsteigende `received_time` | Sortierung fällt weg oder dreht sich |
| Länge = Anzahl offener `request.received` | Liste zeigt 3 statt 34 |
| **Ein wartender Request trägt keinen Namen** | Erfundener Name erscheint |
| Ein wartender mit bekannter `ip_port` zeigt den Namen | Namens-Rückfall fehlt |
| Altbestand > 0 → **leere** Liste, kein Abbrechen | Liste wird aus der Warteschlangenlenzahl geraten |
| Position 1 = nächster dran | Aussage fällt weg |
| Leere Queue → „keine Wartenden" | Leere Tabelle ohne Erklärung |

### Das Dashboard und die TUI

| Test | Mutant, der ihn töten muss |
|---|---|
| Dashboard zeigt die Warteschlange | `<tbody>` fehlt |
| Bestätigungsfenster nennt die **vollständige** rid | Anzeige bricht ab |
| Nach dem Klick bleibt die Zeile sichtbar | Zeile verschwindet sofort |
| TUI bei `free` / `unknown` / totem Server | Absturz beim Rendern |
| Doppelbestätigung | Ein `a` bricht sofort ab |

### Der Rahmen

| Test | Mutant, der ihn töten muss |
|---|---|
| Genau eine schreibende Datei | Ein zweites `POST` taucht auf |
| systemd ohne `Wants=` auf den Server | Abhängigkeit von der falschen Unit |
| `start-systemd` bleibt im Vordergrund | Rückkehr → systemd sieht Erfolg ohne Prozess |
| Die bestehenden 408 Tests | Alles läuft weiter |

`tests/test_no_socket_source.py` wird strenger: bisher prüft er, dass niemand
`/abort_request` aufruft. Jetzt gilt „genau eine Datei, mit dem dokumentierten
Pfad" — mit `abort_all` und einer leeren `rid` als ausdrücklich verbotenen Feldern.

## Abnahme

1. `./monitor.sh watch` zeigt einen laufenden Auftrag.
2. `a` fragt nach, zweimal `a` bricht ihn ab, der Zustand kippt auf `free`.
3. Der Abbruch erscheint in der Verlaufsliste — spurlos verschwinden darf er nicht.
4. `./monitor.sh stop && systemctl --user stop slotmon-monitor` — beides
   widerspruchsfrei.
5. `systemctl --user restart slotmon-monitor` startet ihn ohne Fehler neu.
6. Der Server läuft unverändert weiter, `curl /get_load` antwortet.
7. Die 408 bestehenden Tests bleiben grün.

### Nachtrag: Warteschlange und deren Abbruch (2026-10-12)

8. Das Dashboard listet die Wartenden in der Reihenfolge, mit Position, rid und
   Wartezeit; die Zahl stimmt mit `num_waiting_reqs` überein.
9. Der Systemname steht bei einem Wartenden auf „unbekannt †" — bei einer Position
   mit bekannter `ip_port` steht der Name da.
10. Der Klick auf „Position 3 abbrechen" öffnet ein Fenster mit der vollständigen
    rid und bricht **nur diesen** Auftrag ab.
11. `num_waiting_reqs` sinkt danach um genau 1; die übrigen rids sind unverändert.
12. Ein zweiter Klick auf dieselbe, jetzt nicht mehr existierende Position sagt
    „gibt es nicht mehr" — und die Zahl der Wartenden bleibt unverändert.
13. **Der Halter bleibt unberührt**: nach einem Wartenden-Abbruch läuft der
    gerade laufende Auftrag weiter. Das ist der Test, der zeigt, dass nicht
    `abort_all` gesendet wurde.
14. Die 408 bestehenden Tests bleiben grün.