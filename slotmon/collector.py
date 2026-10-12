"""Fuehrt alle Einheiten zusammen und haelt den Gesamtzustand.

Hier trifft der Code die Aussenwelt: HTTP, nvidia-smi, Logdatei, Historie. Ab
hier ist der Zustand ein Dict, das das Dashboard nur noch liest.

Zwei Uhren, nicht verwechseln
-----------------------------
``LogReader.state["open"][rid]`` und ``LogEvent.at`` sind **Unix-Zeit** (~1.79e9,
aus dem ``timestamp``-Feld des Logs). ``meta["received_time"]`` ist sglangs
**monotoner** Zaehler ab Serverstart (~4.7e5). Jede Laufzeit in diesem Modul ist
``now - <Unix-Zeit>``; ein ``now - received_time`` ergaebe ~-1.79 Mrd. Sekunden.
Deshalb haelt der Collector die Startzeit selbst: `_drain_events` merkt sich zu
jeder ``received``-rid deren ``event.at``, und die ``finished``-Rekorde lesen
daraus. ``received_time`` wandert nach ``received_time`` in den Datensatz und
wird nirgends subtrahiert.

Eine rotator-Instanz pro Logdatei
---------------------------------
``LogRotator`` hat genau einen internen Taktzaehler. Zwei Dateien auf einer
Instanz teilen sich ihn, und eine der beiden bekommt dauerhaft keinen Takt
zugeteilt und wird gar nicht gekuerzt (gemessen: 0 Kuerzungen in 31 Polls).
Deshalb ``self.rotator`` fuer das Server-Log und ``self.monitor_rotator`` fuer
das Monitor-Log, und beide ``check()`` wird **jeden** Pol gerufen - `check()`
springt auf seinen eigenen Takt, uebersprungene Takte kosten null Syscalls.
``every`` bleibt 60 (alle 60 Polls, bei ``POLL_INTERVAL=2.0`` also zwei Minuten);
aus ``poll_interval`` abgeleitet waeren es 120.

``RotateResult.size_mb is None`` heisst **nicht gemessen** (uebersprungener Takt
oder Pfad nicht freigegeben), nicht "Groesse null". Der Zustand reicht das None
unveraendert durch, damit das Dashboard fuer einen nicht gemessenen Wert kein
"0 MB" erfindet.

Ein LogRotator kuerzt, der Collector meldet
--------------------------------------------
Der Rotator setzt nur ein Flag. Die Protokollzeile `log-truncated` schreibt der
Collector, weil nur er weiss, in welchem Lauf gekuerzt wurde. Fehler des
Rotators werden genau einmal protokolliert, nicht in jedem Takt.
"""

import os
import re
import sys
import time

from slotmon.config import load_config
from slotmon.history import History
from slotmon.logreader import FINISHED, RECEIVED, LogReader
from slotmon.logrotate import LogRotator
from slotmon.names import resolve
from slotmon.queue import build as build_queue
from slotmon.slot import count_of, evaluate
from slotmon.sources import ServerSources

#: sglangs interner Health-Check nutzt ``HEALTH_CHECK_<unix>.<usec>`` als rid
#: (im echten Log nachgewiesen). Ein solcher Request geht durch dieselben
#: ``request.received``/``request.finished``-Zeilen wie ein Modell-Request und
#: wuerde ohne Filter dauerhaft als Halter dastehen. Verglichen wird als
#: Praefix, weil der echte rid einen Suffix traegt.
RESERVED_RID_PREFIXES = ("HEALTH_CHECK",)

#: So viele Bytes am Logende werden nach den Batch-Zeilen durchsucht. Das
#: Server-Log haelt sehr lange JSON-Zeilen (ein `request.finished` ist ~15 kB),
#: darum etwas mehr als die 8 kB des Plans - ohne die Batch-Zeilen zu verlieren.
BATCH_TAIL_BYTES = 16384

#: Wie oft die Historie auf das Aufbewahrungsfenster gekuerzt wird. `append`
#: waechst linear, `compact` liest die ganze Datei; beides in jedem Pol waere
#: Arbeit ohne Nutzen. Beim Start wird immer gekuerzt, damit eine Altdatei aus
#: einem frueheren Betrieb nicht unbegrenzt weiterwaechst.
COMPACT_EVERY_S = 3600.0

#: Begruendung fuer `slot.state == "unknown"` nach einer Log-Truncate. Der Wort
#: Teil `truncated` ist derselbe wie in der Protokollzeile, damit der Operator
#: die Begruendung im Dashboard mit der Zeile im Log zusammenfuehren kann.
BLIND_REASON = ("log-truncated: die offenen rid(s) sind nicht mehr beobachtbar, "
                "bis wieder ein request.received im Log steht")

#: `#full token: 41997` aus `Decode batch, ...`. Nicht `full token usage` - der
#: Ausdruck verlangt das Doppelpunkt direkt hinter "token".
BATCH_TOKENS = re.compile(r"#full token:\s*(\d+)")

#: `gen throughput (token/s): 36.02` aus derselben Zeile. Dieselbe Decode-
#: Stufe wie `sglang:gen_throughput` aus /metrics, also **keine** zweite
#: Wahrheit: der Wert aus /metrics hat Vorrang, die Logzeile ist der Fallback,
#: wenn /metrics schweigt.
BATCH_THROUGHPUT = re.compile(r"gen throughput \(token/s\):\s*([\d.]+)")

#: So viele `rid -> ip_port`-Zustaende aus abgeschlossenen Requests werden
#: gehalten. `_queue_name()` schlaegt darin nach, wenn ein Wartender einen
#: Namen bekommen soll. Der Monitor laeuft wochenlang, die Zahl der
#: Abschluesse nicht begrenzt - ohne diese Grenze waere es ein Dict, das nur
#: waechst. 500 ist um mehr als eine Groessenordnung groesser, als eine
#: Warteschlange braucht (gemessen 34).
KNOWN_IPS_MAX = 500


def host_of(ip_port):
    """Aus "192.168.9.2:44444" die IP machen, damit systems.json greift.

    Kein Port im Namen: die Zuordnung ist IP -> Name.
    """
    if not ip_port:
        return None
    if ip_port.startswith("["):
        return ip_port[1:ip_port.find("]")] if "]" in ip_port else ip_port
    return ip_port.rsplit(":", 1)[0] if ":" in ip_port else ip_port


def is_reserved(rid):
    """Eigenverkehr des Servers, kein Nutzer-Request.

    Der Typ wird zuerst geprueft, weil `logreader._collect` jede truthy rid
    akzeptiert: `"rid": 12345` ergibt `AttributeError`, eine Liste ergibt
    zusaetzlich `TypeError: unhashable type`, wenn sie als Dict-Schluessel in
    `received_at` landet. Beides verlaesst `tick()`. Ein rid, der keine
    Zeichenkette ist, kann kein Health-Check und kein Slot-Halter sein.
    """
    if not isinstance(rid, str):
        return False
    return any(rid.startswith(prefix) for prefix in RESERVED_RID_PREFIXES)


def usable_rid(rid):
    """Nur eine nichtleere Zeichenkette ist als Dict-Schluessel brauchbar."""
    return isinstance(rid, str) and bool(rid)


class Collector:
    """Ein Durchlauf je `tick()`, der Zustand bleibt fuer `state()` liegen."""

    def __init__(self, cfg=None, names=None, now_fn=time.time):
        self.cfg = cfg or load_config()
        self.names = names if names is not None else {}
        self.now_fn = now_fn
        self.sources = ServerSources(self.cfg)
        self.reader = LogReader(self.cfg.server_log, self.cfg.log_pair_window_s)
        self.history = History(self.cfg.history, self.cfg.retention_h)
        # Zwei Logdateien, zwei Instanzen, zwei Takte. `every` ist der Default
        # des Rotators (60 Polls) und wird nicht aus poll_interval abgeleitet.
        #
        # Die Schwellen sind **verschieden**: das Server-Log haelt die 1 GB
        # aus `MAX_LOG_MB`, das eigene Monitor-Log die 5 MB aus der Spec
        # (Z. 324). Der Warnwert folgt als 90 % davon - `LogRotator` zieht
        # ihn selbst nach, wenn jemand `MAX_MONITOR_LOG_MB` unter
        # `WARN_LOG_MB` setzt und so ein totes Band erzeugt haette.
        self.rotator = LogRotator(self.cfg.max_log_mb, self.cfg.warn_log_mb)
        self.monitor_rotator = LogRotator(self.cfg.max_monitor_log_mb,
                                          self.cfg.max_monitor_log_mb * 0.9)
        self.compact_every_s = COMPACT_EVERY_S
        self.max_req_input_len = None
        self.finished = {}
        #: rid -> ip_port, ausschliesslich aus **abgeschlossenen** Requests
        #: (`request.finished`). Das ist die einzige Stelle, an der eine IP
        #: ueberhaupt im Log steht. Ein wartender Request traegt keine
        #: (gemessen an 61 ueber alle Felder von `request.received`: null
        #: Treffer), deshalb ist dies eine Naht fuer einen Rueckfall und
        #: keine Zuordnung, mit der sich die Queue bestimmen laesst.
        self._known_ips = {}
        #: rid -> Unix-Zeit des `request.received`. Aus `state["open"]` geht das
        #: nicht mehr hervor, sobald der Reader die rid beim `finished` entfernt.
        self.received_at = {}
        self._last_compact = None
        #: Zuletzt protokollierter Rotationsfehler je Logdatei.
        self._last_rotate_error = {}
        #: Zuletzt **gemessener** Rotationsbefund je Logdatei. Der Rotator prueft
        #: nur jeden 60. Takt und liefert sonst absichtlich nichts - ohne diese
        #: Merkung war eine Warnung in 2 von 120 Ticks sichtbar (gemessen,
        #: Indizes 0 und 60) und das Dashboard zeigte 98 Sekunden lang nichts.
        #:
        #: Nicht im Rotator: dort ist "nichts melden" eine bewusste Zusage aus
        #: Task 8 - ein Takt, der nicht gemessen hat, soll das auch sagen. Hier
        #: im Collector geht es um die *Anzeige*, und da ist "zuletzt gemessen,
        #: vor N s" etwas voellig anderes als "nichts bekannt".
        self._last_rotate = {}
        #: rids aus history.jsonl, damit ein Neustart sie nicht doppelt schreibt.
        self._recorded = None
        #: Letzter Schreibfehler der Historie, fuer den `log`-Teil des Zustands.
        self._history_error = None
        #: Im laufenden Tick ist mindestens ein Schreibvorgang gescheitert.
        #: Nur am **Ende** des Ticks, und nur wenn das Flag nicht gesetzt ist,
        #: wird `_history_error` zurueckgesetzt. Ein erfolgreiches `compact`
        #: allein raeumt nichts auf - es hat den verlorenen Eintrag nicht
        #: ersetzt (N3).
        self._write_failed = False
        #: Nach einer Log-Truncate ist der Blick auf die offenen rids zerstoert.
        #: Bis wieder etwas im Log steht, ist der Slot `unknown`, nicht `free`.
        self._blind = False
        self._state = self._empty_state()

    def close(self):
        """Der Reader laeuft in keinem Thread - es gibt nichts zu stoppen.

        Der Aufrufer ruft das deterministisch am Ende (Test-`addCleanup`,
        Serve-Shutdown); danach wird nur noch `state()` benutzt. Ein Flag
        `_closed` waere hier toter Code: `logreader` hat weder Thread noch
        Queue noch async, und `tick()` darf nach `close()` weiterlaufen
        duerfen, damit ein Test-`addCleanup` keinen spaeten Fehler einwirft.
        """
        return None

    def state(self):
        """Der Zustand des letzten `tick()`, ohne I/O.

        Nur lesen. Wer ihn veraendert, verdirbt den Zustand, den der naechste
        `tick()` zurueckgibt.
        """
        return self._state

    # ------------------------------------------------------------------
    # Zustand

    def _empty_state(self):
        return {
            "at": self.now_fn(),
            "server": {"connected": False, "load_error": None, "age_s": None},
            "slot": {
                "state": "unknown", "rid": None, "system": None, "ip_port": None,
                "started_at": None, "duration_s": None, "legacy": None,
                "open_count": 0, "complete": False,
                # `reason` existiert auch vor dem ersten Tick - `/api/state`
                # liefert `state()`, und ein Client, der vor dem ersten Poll
                # liest, darf keinen `KeyError` bekommen. Es ist dort **kein
                # `None`**: `unknown` ohne `reason` ist verboten (siehe
                # `tick()`), und der Zustand vor dem ersten Poll war genau
                # das. Im Dashboard erschien daraufhin der schwache
                # generische Satz, obwohl gar kein Altbestand belegt ist.
                # Der Satz hier behauptet nichts ueber die Queue - es hat
                # noch niemand gefragt.
                "reason": "noch kein Messwert - der erste Poll steht aus",
            },
            "queue": {"waiting": None, "running": None, "waiting_list": []},
            "speed": {"tok_per_s": None, "ctx_tokens": None,
                      "max_req_input_len": self.max_req_input_len,
                      "output_pct": None},
            "gpu": {"util": None, "used_mb": None, "total_mb": None},
            "log": {"size_mb": None, "warn": False, "truncated": False,
                    "history_error": None, "measured_at": None},
            "history": [],
        }

    # ------------------------------------------------------------------
    # Log

    def _log(self, message):
        """Eine Zeile fuer das Monitor-Log.

        Der Startskript leitet stdout/stderr nach `MONITOR_LOG` um; der
        Collector kennt den Pfad nicht und schreibt nicht selbst in Dateien -
        die Log-Zuordnung gehoert dem Prozess, nicht der Fachlogik.
        """
        print(f"[slotmon] {time.strftime('%Y-%m-%d %H:%M:%S')} {message}",
              file=sys.stderr, flush=True)

    def _read_batch(self):
        """(ctx_tokens, tok_per_s) aus den letzten Batch-Zeilen des Logendes.

        Liest nur ein Fenster am Dateiende, nie die ganze Datei. Kommt nichts
        Brauchbares heraus, ist das `(None, None)` und nicht 0.
        """
        try:
            size = os.path.getsize(self.cfg.server_log)
        except OSError:
            return None, None
        if size <= 0:
            return None, None
        window = min(size, BATCH_TAIL_BYTES)
        try:
            with open(self.cfg.server_log, "rb") as handle:
                handle.seek(size - window)
                tail = handle.read(window).decode("utf-8", errors="replace")
        except OSError:
            return None, None
        # sglang schreibt die Scheduler-Ausgabe mit \r, nicht mit \n.
        tail = tail.replace("\r", "\n")
        if size > window:
            # Die erste Zeile im Fenster ist abgeschnitten. Eine dort
            # abgerissene Zahl waere ein geratener Wert - die Zeile faellt weg.
            _, _, tail = tail.partition("\n")
        ctx = None
        throughput = None
        for line in tail.split("\n"):
            found = BATCH_TOKENS.search(line)
            if found:
                ctx = int(found.group(1))
            found = BATCH_THROUGHPUT.search(line)
            if found:
                try:
                    throughput = float(found.group(1))
                except ValueError:
                    throughput = None
        return ctx, throughput

    def _drain_events(self):
        """Log-Events des Polls in `received_at` und `finished` ueberfuehren."""
        try:
            events = self.reader.poll()
        except Exception as exc:  # noqa: BLE001 - siehe Docstring
            # Der Reader ist eine Fremdeinheit, und eine kaputte Logzeile kann
            # ihn mitnehmen: `_apply` legt jede truthy rid in ein Set, und
            # `"rid": ["a"]` scheitert dort mit `TypeError: unhashable type`,
            # noch bevor der Collector die Zeile sehen kann. Der Aufruf von
            # `tick()` darf daran nicht haengen bleiben.
            self._log(f"log-read-error {type(exc).__name__}: {exc}")
            return
        for event in events:
            if not usable_rid(event.rid):
                # `"rid": 12345`, `["a"]`, `{"a": 1}` - der Reader laesst jede
                # truthy rid durch, und beide scheitern als Dict-Schluessel
                # (`unhashable type`) bzw. schon in `is_reserved`. Solche rids
                # koennen weder Halter sein noch einen Schluessel liefern.
                continue
            if is_reserved(event.rid):
                continue
            if event.kind == RECEIVED:
                # Unix-Zeit aus dem timestamp-Feld, nicht `received_time`.
                self.received_at.setdefault(event.rid, event.at)
                continue
            if event.kind != FINISHED:
                continue
            meta = event.meta
            self.finished[event.rid] = {
                "rid": event.rid,
                "ip_port": meta.get("ip_port"),
                "started_at": self.received_at.get(event.rid),
                "ended_at": event.at,
                "prompt_tokens": meta.get("prompt_tokens"),
                "completion_tokens": meta.get("completion_tokens"),
                "cached_tokens": meta.get("cached_tokens"),
                "finish_reason": meta.get("finish_reason"),
                "status": meta.get("status"),
                # Nur zur Diagnose. Wird nie subtrahiert.
                "received_time": meta.get("received_time"),
            }

    def _recorded_rids(self, now):
        """rids, die bereits in `history.jsonl` stehen.

        Ein neu startender Collector liest das Log ab Offset 0 und sieht daher
        **jedes** `request.finished` erneut, auch die von vor dem letzten
        Start. Ohne diese Menge schreibt jeder Neustart die gesamte Vergangenheit
        ein zweites Mal in die Historie - im Log sind das Tausende Zeilen, in der
        Historie eine pro Request mehrfacher Zeilen. Der Satz wird beim ersten
        Schreiben aufgebaut und bei jedem `compact` erneuert, damit er mit der
        Datei mitwaechst statt unbegrenzt zu werden.
        """
        if self._recorded is None:
            self._recorded = {entry.get("rid")
                              for entry in self.history.load(now)}
        return self._recorded

    def _retained(self, entry, now):
        """Liegt der Abschluss noch im Aufbewahrungsfenster?

        Ein neu startender Collector liest das Log ab Offset 0 und sieht daher
        **jedes** `request.finished`, auch die von vor dem letzten Start. Ohne
        diesen Vorfilter haengt er den gesamten Altbestand an - bei
        `MAX_LOG_MB=1024` gemessen 27 735 Appends und 1,4 s, bei einem
        dreifach groesseren Log 73 960 Appends und 6,9 s - und `compact()`
        verwirft die Arbeit im selben Tick wieder. Die Datei endet richtig, die
        Arbeit war nur umsonst.

        Ein `ended_at`, das keine Zahl ist, wird **behalten**: `History._fresh`
        wuerde ihn verwerfen, aber ein fehlender Zeitstempel ist kein Alter von
        ueber 24 Stunden, sondern ein Loch in der Angabe. Wegwerfen ist die
        Entscheidung, die ein Fehler falsch trifft.
        """
        ended = entry.get("ended_at")
        if not isinstance(ended, (int, float)):
            return True
        return (now - ended) <= self.history.retention_s

    def _flush_finished(self, open_rids, now):
        """Abgeschlossene Requests in die Historie schreiben.

        Ein `finished` ohne passendes `received` (Altbestand aus der Zeit vor
        dem Request-Logging) landet mit `started_at=None` und `duration_s=None`
        in der Historie: rid und Tokenzahlen sind da, die Dauer eben nicht. Der
        Zeitstempel wird nicht geraten.
        """
        written = []
        recorded = self._recorded_rids(now)
        for rid, entry in list(self.finished.items()):
            if rid in open_rids or rid in recorded:
                # Schon geschrieben: derselbe Abschluss, zweites Mal gesehen.
                self.received_at.pop(rid, None)
                del self.finished[rid]
                continue
            if not self._retained(entry, now):
                # Ausserhalb des Fensters: nicht schreiben. `recorded` bleibt
                # unberuehrt - der rid ist nie in der Datei gelandet, und wenn
                # das Fenster sich verschiebt, soll er nicht als "geschrieben"
                # gelten.
                self.received_at.pop(rid, None)
                del self.finished[rid]
                continue
            record = dict(entry)
            started = record.get("started_at")
            ended = record.get("ended_at")
            record["duration_s"] = self._duration(started, ended, now)
            record["system"] = resolve(self.names, host_of(record.get("ip_port")))
            # Die `ip_port` merken, **bevor** geschrieben wird: der
            # Historienschlag kann fehlschlagen, die IP steht trotzdem im Log.
            # `_queue_name()` liest dieses Dict; ohne den Eintrag waere der
            # Namens-Rueckfall der Warteschlange tote Schrift.
            ip_port = record.get("ip_port")
            if ip_port:
                # `while`, nicht `if`: ein einzelner `pop` haelt ein Dict, das
                # bereits ueber der Grenze liegt, nicht darueber - es waechst
                # dann mit jedem Abschluss weiter.
                while len(self._known_ips) >= KNOWN_IPS_MAX:
                    # Dicts behalten die Einfuegereihenfolge;
                    # `next(iter(...))` ist damit der aelteste Eintrag.
                    self._known_ips.pop(next(iter(self._known_ips)))
                self._known_ips[rid] = ip_port
            # Ein Abbruch bleibt in der Historie, zaehlt aber nicht in die
            # mittlere Dauer - das Dashboard filtert danach.
            aborted = record.get("finish_reason") == "abort"
            record["aborted"] = aborted
            self._append(record)
            if self._history_error is None:
                written.append(record)
                recorded.add(rid)
            self.received_at.pop(rid, None)
            del self.finished[rid]
        return written

    def _note_history_error(self, message, label):
        """Fehler festhalten, mit Text, der **einmal** in der Logdatei landet."""
        text = message
        if text != self._last_rotate_error.get(label):
            self._log(f"{label} {text}")
            self._last_rotate_error[label] = text
        self._history_error = message

    def _append(self, record):
        """Ein Historieneintrag, mit Fehlerkanal.

        `History.append` wirft `PermissionError` bei nicht beschreibbarer Datei
        und `IsADirectoryError`, wenn der Pfad ein Verzeichnis ist. Beides
        verlaesst `tick()` und damit die Poll-Schleife. Der Fehler wandert in
        `log.history_error`, damit `/api/state` ihn ausliefert - ein Fehler,
        der nur in der Logdatei steht, ist fuer den Leser des Dashboards
        unsichtbar, und `/api/state` friert auf dem letzten guten Tick ein,
        waehrend `/api/stream` Fehlerrahmen schickt.

        Wird der Eintrag nicht als geschrieben gemerkt, kann er nach einer
        Reparatur der Datei noch geschrieben werden; das ist der Grund, warum
        `recorded.add` hinter dem Aufruf steht.
        """
        try:
            self.history.append(record)
        except OSError as exc:
            self._write_failed = True
            self._note_history_error(f"{type(exc).__name__}: {exc}",
                                     "history-write-error")
            return False
        return True

    @staticmethod
    def _duration(started, ended, now):
        """Sekunden zwischen zwei Unix-Zeiten. `None`, wenn eine fehlt.

        `ended_at` hat Vorrang vor `now`: der Abschluss ist im Log ablesbar,
        `now` ist der Pol, der ihn gerade erst gesehen hat. Sonst waere jede
        Dauer um bis zu ein Poll-Intervall zu lang.
        """
        start_point = ended if isinstance(ended, (int, float)) else now
        if not isinstance(started, (int, float)):
            return None
        return max(0.0, float(start_point) - float(started))

    def _queue_name(self, rid):
        """Der Anzeigename eines Wartenden, oder `None`.

        `queue.build()` ruft das **je Position** auf und bekommt die rid. Die
        Aufloesung bleibt hier: `queue.py` weiss nichts ueber `systems.json`
        und muss es auch nicht wissen.

        **Was hier nicht passiert:** die Access-Zeile des Lesers wird nicht
        angefasst. Die gehoert dem Halter und wird erst bei
        `request.finished` verbraucht (`logreader.py:359`, `_attach_ip`) -
        waere sie hier abgegriffen, verloere der gerade laufende Auftrag
        seinen Namen.

        Im Regelfall kommt `None` heraus: `_known_ips` enthaelt `rid`s, die
        **fertig** sind, und ein Wartender ist per Definition nicht fertig.
        Das ist keine halbe Sache, sondern die Grenze der Datenlage.
        """
        ip_port = self._known_ips.get(rid)
        if not ip_port:
            return None
        return resolve(self.names, host_of(ip_port))

    # ------------------------------------------------------------------
    # Quellen

    def _max_req_input_len(self):
        """Einmalig lesen und dann cachen.

        `/get_server_info` ist ein vollstaendiger Dump des Serverzustands und
        `max_req_input_len` aendert sich zur Laufzeit nicht. Jeder Pol eine
        HTTP-Anfrage zu verschwenden ist reine Last - und genau die Gewohnheit,
        die in Task 5 als "wird einmalig beim Start gelesen" festgeschrieben ist.
        Erst nach einem erfolglosen Versuch wird erneut gelesen, damit ein
        temporaerer Fehler nicht fuer die ganze Sitzung fehlt.
        """
        if self.max_req_input_len is not None:
            return self.max_req_input_len
        info = self._source("read_server_info")
        if info.fresh:
            value = info.value.get("max_req_input_len")
            if value:
                self.max_req_input_len = value
        return self.max_req_input_len

    def _compact_history(self, now):
        """Die Historie periodisch auf das Aufbewahrungsfenster kuerzen.

        Ein erfolgreiches `compact` setzt `_history_error` **nicht** auf `None`.
        `compact` laeuft nach `_flush_finished` im selben Tick und laesst einen
        fehlgeschlagenen `append` unberuehrt: es schreibt die Datei neu, aber
        es hat den verlorenen Eintrag nicht ersetzt. Ein Kanal, der hier
        aufgeraeumt wird, verstummt genau dann, wenn es ihn braucht -
        temporaeres `ENOSPC`, Remount, `chmod`-Race.
        """
        if (self._last_compact is not None
                and now - self._last_compact < self.compact_every_s):
            return
        try:
            self.history.compact(now)
        except OSError as exc:
            # `compact` schreibt die Datei neu: `PermissionError` bei
            # schreibgeschuetzter Datei, `IsADirectoryError` bei einem
            # Verzeichnis als Pfad. Der Fehler darf `tick()` nicht verlassen,
            # und er gehoert in denselben Kanal wie der Schreibfehler.
            self._write_failed = True
            self._note_history_error(f"compact: {type(exc).__name__}: {exc}",
                                     "history-compact-error")
            return
        self._last_compact = now
        # Mit der Datei mitwachsen: was `compact` weggeworfen hat, muss auch aus
        # dem Filter raus, sonst waechst er ohne Ende.
        self._recorded = None

    def _rotate(self, name, rotator, now):
        """`check()` aufrufen und das Ergebnis melden.

        Ein Aufrufer-Gate neben `check()` waere die zweite Taktung, an der die
        Rotation schon einmal still ausgefallen ist, ohne zu melden. Deshalb
        ruft der Collector `check()` in **jedem** Pol; die Taktung ist Sache des
        Rotators. Jede Instanz bekommt ihre eigene Datei.
        """
        path = getattr(self.cfg, name)
        rotate = rotator.check(path)
        if rotate.size_mb is None and rotate.error is None:
            # Der Rotator hat diesen Takt uebersprungen. Der letzte gemessene
            # Befund wird weitergereicht, damit eine Warnung nicht zwischen
            # zwei Messungen verschwindet. Gemessen: `warn=True` in 2 von 120
            # Taks statt in allen.
            alt = self._last_rotate.get(name)
            # **Nur eine belastbare Aussage** wird weitergereicht: eine
            # Warnung oder ein Kuerzen. Eine harmlose Groesse nicht - die
            # zusage "nicht gemessen" heisst "nicht gemessen", und
            # `test_skipped_tick_reports_no_size_not_zero` haelt sie fest.
            # Die Warnung ist das einzige, was zwischen zwei Messungen
            # verschwinden und damit ungesehen bleiben wuerde.
            if alt is not None and (alt["warn"] or alt["truncated"]):
                rotate.size_mb = alt["size_mb"]
                rotate.warn = alt["warn"]
                rotate.measured_at = alt["at"]
                # **`truncated` nicht** weiterreichen. Das Kuerzen ist ein
                # **Ereignis**, kein Zustand: es hat genau einmal stattgefunden.
                # Dauerhaft gemeldet wuerde der Blind-Flag nie wieder fallen und
                # der Slot bliebe ewig `unknown` - genau das, was zwei Tests
                # aus Task 8 verhindern.
                rotate.truncated = False
        else:
            rotate.measured_at = now
            self._last_rotate[name] = {
                "size_mb": rotate.size_mb,
                "warn": rotate.warn,
                "truncated": rotate.truncated,
                "at": now,
            }
        if rotate.truncated:
            size = "unbekannt" if rotate.size_mb is None else f"{rotate.size_mb:.0f} MB"
            self._log(f"log-truncated {size} -> 0 ({path})")
            return rotate
        # Der zuletzt gemeldete Fehler haengt an der Datei, nicht am Rotator:
        # ein Fehler auf dem Server-Log darf den des Monitor-Logs nicht
        # unterdruecken und umgekehrt.
        if rotate.error and rotate.error != self._last_rotate_error.get(name):
            # Ein dauerhafter Fehler wuerde sonst in jedem Takt eine Zeile
            # schreiben. Einmal reicht: der Zustand zeigt den Fehler nicht,
            # also muss er mindestens einmal auffallen.
            self._log(f"log-rotate-error {name}: {rotate.error}")
        self._last_rotate_error[name] = rotate.error
        return rotate

    def _source(self, name, **kwargs):
        """Eine Quelle lesen, ohne dass sie `tick()` killen kann.

        `ServerSources` wirft per Konstruktion nicht - jede Methode gibt ein
        `Sample` mit `error` zurueck. Das ist die Zusage von Task 5, keine
        Tatsache ueber die Laufzeit: ein Tippfehler in `API`, ein
        `KeyboardInterrupt`, ein `SystemExit` aus einer
        KeyboardInterrupt-Kette, ein Bug in einer spaeteren Quelle. Die
        Randbedingung lautet "kein Fehler darf als Signal erscheinen", also
        faengt dieser Aufruf alles ab und fuehrt es als `Sample` mit `error`
        weiter - derselbe Kanal, den `sources` selbst benutzt. So ist die
        Stoerung im Zustand sichtbar statt nur im Log.
        """
        from slotmon.sources import Sample
        try:
            return getattr(self.sources, name)(**kwargs)
        except Exception as exc:  # noqa: BLE001 - siehe Docstring
            return Sample(None, None, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Takt

    def tick(self):
        now = self.now_fn()
        # Am Anfang des Ticks, nicht am Ende: sonst wuerde ein Fehler, der in
        # `_compact_history` entsteht, im selben Durchlauf wieder verschwinden.
        self._write_failed = False
        load = self._source("read_load")
        metrics = self._source("read_metrics")
        gpu = self._source("read_gpu")
        max_req_input_len = self._max_req_input_len()

        self._drain_events()
        # `state["open"]` enthaelt auch die reservierten rids. Die fliegen raus,
        # **bevor** `evaluate()` sie sieht - sonst wuerde der Health-Check des
        # Servers den Slot als dauerhaft belegt ausweisen.
        open_rids = {rid: since for rid, since in self.reader.state["open"].items()
                     if usable_rid(rid) and not is_reserved(rid)}

        # `num_waiting_reqs` aus /get_load ist die einzige belastbare
        # Queue-Zahl (Spec). Faellt /get_load aus, ist `waiting` unbekannt und
        # bleibt None - nicht der Wert aus /metrics: `legacy` waere sonst aus
        # zwei verschiedenen Messgroessen gerechnet.
        waiting = None
        running = None
        connected = False
        load_error = None
        age = None
        if load.fresh:
            connected = True
            # `sources.py` holt den Wert mit `.get()`, ohne ihn zu pruefen.
            # JSON erlaubt dort auch `"180"` und `true`; eine davon als
            # Wartenden-Zahl anzuzeigen, waere eine Behauptung, die nicht
            # belegt ist. Dieselbe Wache wie in `slot._count` - dort
            # schuetzt sie vor der `TypeError`, hier vor der falschen Zahl
            # im Dashboard. Ein Wert, der hier durchfaellt, kaeme als
            # "WARTEND 180" neben einem Slot-Feld daher, das "Halter nicht
            # bestimmbar" sagt.
            waiting = count_of(load.value.get("num_waiting_reqs"))
            # `Sample.at` ist die Abrufzeit, und sie entsteht **nach** dem
            # HTTP-Roundtrip (`sources.read_load`). `now` stammt dagegen vom
            # Anfang dieses Ticks und ist damit immer kleiner als `load.at`;
            # `now - load.at` waere strukturell negativ und `max(0.0, ...)`
            # klemmte auf 0.0.
            #
            # Gemessen: `age_s` war bei jedem Takt 0.0. Die
            # Veraltungs-Markierung im Dashboard konnte dadurch nie greifen -
            # ein eingefrorenes Dashboard sah dabei gesund aus.
            #
            # Also hier die Uhr neu nehmen. Nicht `now`: die Laufzeiten
            # weiter unten brauchen die eine Zeit des Tick-Anfangs.
            age = max(0.0, self.now_fn() - load.at)
        else:
            load_error = load.error
        if metrics.fresh:
            running = metrics.value.get("num_running_reqs")

        # N5 - der `num_reqs`-Zeuge. Ein rid, der laenger als ein Poll-Intervall
        # offen ist, obwohl der Scheduler null Request meldet, ist nicht
        # belegbar.
        #
        # Wovor genau? Von zwei unabhaengigen Werten, die je fuer sich nichts
        # beweisen koennen:
        #   - der Banner sagt, **aus welcher Generation** ein rid stammt. Er
        #     sieht keinen Geist aus der laufenden Generation (N5).
        #   - `num_reqs` sagt, **ob der Server den Request kennt**. Genau das
        #     ist die Luecke: ein Client bricht ab, oder der Server wird mitten
        #     im Request gestoppt. sglang schreibt fuer jeden Request ein
        #     `request.received`, aber nur fuer abgeschlossene ein
        #     `request.finished`. Der rid bleibt fuer immer offen, und mit
        #     `max_running_requests 1` waehlt `evaluate()` den **aeltesten**
        #     offenen rid als Halter. Das war die gemessene erfundene Zahl
        #     `state='busy' rid='stale' duration_s=36100.0`.
        #
        # Warum ein **Alter** und nicht "alle offenen rids"? Weil `read_load`
        # vor `_drain_events()` laeuft: ein Request, der zwischen zwei
        # `/get_load`-Abfragen eintrifft, steht im Log, ist aber vom Scheduler
        # noch nicht gezaehlt. Ohne Altersgrenze wuerde genau dieser frische rid
        # verworfen - und seine Startzeit waere fuer immer weg (N2). Ein rid,
        # der juenger als ein Poll-Intervall offen ist, hat diese Race nicht
        # durchlaufen; einer, der aelter ist, war in mindestens einem weiteren
        # Poll dem Scheduler vorgestellt worden und wurde trotzdem nicht
        # gezaehlt.
        #
        # Die Schwelle ist `cfg.poll_interval` - die eine Zahl, die den Takt
        # vorgibt. Keine eigene Env-Variable und kein Magic Number: bei
        # `POLL_INTERVAL=10` gilt dieselbe Logik mit 10 s.
        #
        # Der Vergleich ist `<`: genau ein Intervall alt ist nicht "aelter als
        # eins". Mit `<=` waere ein rid, der im selben Takt einfliesst wie der
        # Load-Sample, im selben Tick faellig - genau die Race aus N2.
        #
        # `num_reqs` kleiner als null, aber ungleich 0, beweist nichts: es zaehlt
        # wartende Request mit. Ist `num_reqs` unbekannt (Quelle tot, Feld
        # fehlt), gilt ueberhaupt nichts - eine kaputte Quelle ist kein Beweis,
        # in keinem Alter.
        num_reqs = load.value.get("num_reqs") if load.fresh else None
        ghosts = 0
        reason = None
        if num_reqs == 0 and open_rids:
            max_age = self.cfg.poll_interval
            stale = [rid for rid, since in open_rids.items()
                     if (now - since) > max_age]
            if stale:
                ghosts = len(stale)
                for rid in stale:
                    # Weg aus `received_at` und `finished`: bliebe die Startzeit
                    # im Dict, taucht der rid beim naechsten Abschluss wieder
                    # auf - mit leerer Historiezeile.
                    self.received_at.pop(rid, None)
                    self.finished.pop(rid, None)
                    del open_rids[rid]
                # Und weg aus dem **Leser**. `open_rids` ist eine Kopie, die
                # `_drain_events()` aus `reader.state["open"]` baut; ein `del`
                # auf der Kopie laesst den rid im Leser stehen, und beim
                # naechsten Tick ist er wieder als Halter da - mit
                # `max_running_requests 1` nimmt `evaluate()` den aeltesten
                # offenen rid. Das war die Fehlerklasse, die N1 ausgeloest hat.
                self.reader.drop_open(stale)
                reason = (
                    f"num_reqs=0, aber {ghosts} rid(s) im Log laenger als "
                    f"{max_age:g} s offen - der Server kennt sie nicht "
                    "(abgebrochener oder beim Stopp verworfener Request)")

        self._flush_finished(open_rids, now)

        slot_state = evaluate(open_rids, waiting)

        started_at = None
        duration = None
        if slot_state.holder_rid:
            started_at = slot_state.holder_since
            duration = max(0.0, now - started_at) if started_at is not None else None

        if slot_state.holder_rid and slot_state.complete:
            label = "busy"
        elif ghosts:
            # Die offenen rids wurden als Geister verworfen. `evaluate()` sieht
            # jetzt ein leeres Dict und wuerde `free` melden - das waere eine
            # **positive** Behauptung aus einem Widerspruch heraus: der
            # Scheduler kennt null Request, das Log nennt welche. Der Zustand
            # ist unbestimmt, nicht leer.
            label = "unknown"
        elif slot_state.complete and not open_rids and waiting == 0:
            # `waiting == 0` ist durch `complete` bei leerem `open_rids` bereits
            # impliziert (legacy = waiting - 0). Es steht trotzdem da: die drei
            # Bedingungen sind je einzeln lesbar, und eine davon ist die
            # Sicherung gegen eine zukuenftige Aenderung an `evaluate()`.
            label = "free"
        else:
            # `waiting is None` (Server nicht erreichbar) oder Altbestand > 0:
            # der Halter ist nicht bestimmbar, und das wird behauptet.
            label = "unknown"

        if label == "unknown" and reason is None:
            # **`unknown` ohne `reason` ist verboten.** Ein grundloses
            # `unknown` ist im Dashboard nicht von einem echten Leerlauf zu
            # unterscheiden - und genau diese Verwechslung hat den
            # Phantom-Halter aus Runde 2 glaubhaft gemacht. Jeder Weg hierhin
            # bekommt deshalb eine Begruendung:
            #
            #  - die `num_reqs`-Gegenprobe hat rids verworfen  -> `reason` ist
            #    bereits gesetzt (siehe oben);
            #  - `waiting is None`  -> der Server war nicht erreichbar, die
            #    Warteschlange ist damit unbekannt und `evaluate()` bestimmt
            #    absichtlich keinen Halter;
            #  - `legacy > 0`       -> Altbestand: Requests, die vor dem
            #    Request-Logging eingereiht wurden. Ihre Zahl ist echt, ihre
            #    Reihenfolge nicht ableitbar.
            if waiting is None:
                # `connected` und `waiting is None` sind **zwei verschiedene
                # Dinge**. `waiting` ist `load.value.get("num_waiting_reqs")`,
                # und `sources.py:63` holt den Wert mit `.get()`: eine frische
                # Antwort, die das Feld schlicht nicht traegt, ergibt
                # `Sample(fresh)` mit `waiting=None` bei `connected=True`.
                # Gemessen. Die Begruendung nennt deshalb `connected` selbst,
                # statt es zu behaupten - eine `reason`, die `server.connected`
                # widerspricht, waere schlimmer als gar keine.
                reason = (
                    "Warteschlange unbekannt - "
                    + (load_error if load_error else "/get_load lieferte keine Zahl")
                    + (f" (verbunden mit Server: {'ja' if connected else 'nein'})"))
            elif slot_state.legacy:
                reason = (
                    f"Altbestand: {slot_state.legacy} Request(s) vor dem "
                    "Request-Logging - Halter nicht bestimmbar, bis die "
                    "Warteschlange auf 0 ist")
            else:
                reason = "Halter nicht bestimmbar"

        ctx, throughput = self._read_batch()
        tok_per_s = metrics.value.get("gen_throughput") if metrics.fresh else None
        if tok_per_s is None:
            # Gleiche Decode-Stufe, nur aus dem Log statt aus /metrics.
            tok_per_s = throughput
        output_pct = None
        if ctx is not None and max_req_input_len:
            output_pct = round(100.0 * ctx / max_req_input_len, 2)

        rotate = self._rotate("server_log", self.rotator, now)
        self._rotate("monitor_log", self.monitor_rotator, now)
        truncated = rotate.truncated
        if truncated:
            # Die Datei ist leer; der Reader beginnt dort von vorn. Die
            # `received_at` bleiben stehen: ihre Unix-Zeiten sind gueltig,
            # unabhaengig davon, dass die Zeile im Log nicht mehr existiert.
            self.reader.reset()
            # I3 - der naechste Tick bewertet gegen ein leeres `open`-Dict und
            # wuerde `free` melden. Das ist eine **positive** Behauptung ueber
            # einen Zustand, den der Monitor nicht mehr sehen kann: die
            # `request.received`-Zeilen des laufenden Requests sind mit der
            # Datei verschwunden. `/metrics` und die GPU koennen direkt
            # daneben `num_running_reqs=1` und 100 % Auslastung sagen. Also
            # `unknown`, bis wieder etwas im Log steht.
            self._blind = True
            # Der Zustand oben wurde noch gegen das gefuellte `open`-Dict
            # bewertet, das die Truncate gerade abgeraeumt hat.
            label = "unknown" if label == "free" else label
            reason = BLIND_REASON
        elif self._blind:
            if open_rids:
                # Wieder etwas im Log gesehen - der Blick ist zurueck.
                self._blind = False
            else:
                label = "unknown"
                if reason is None:
                    reason = BLIND_REASON

        self._compact_history(now)

        # Der Kanal wird nur zurueckgesetzt, wenn in diesem Tick **kein**
        # einziger Schreibvorgang gescheitert ist. `compact` laeuft vorher und
        # darf den Kanal nicht uebertuenen: es hat den fehlgeschlagenen Eintrag
        # nicht ersetzt (N3).
        if not self._write_failed and self._history_error is not None:
            self._history_error = None
            self._last_rotate_error.pop("history-write-error", None)
            self._last_rotate_error.pop("history-compact-error", None)

        state = self._empty_state()
        state["at"] = now
        state["server"] = {
            "connected": connected, "load_error": load_error, "age_s": age
        }
        state["slot"] = {
            "state": label,
            "rid": slot_state.holder_rid,
            # Die IP des laufenden Halters ist nicht beobachtbar: die
            # Access-Zeile entsteht erst beim Abschluss. Bis dahin bleibt der
            # Name leer, statt ihn zu raten - im Datensatz steht er danach.
            "system": None,
            "ip_port": None,
            "started_at": started_at,
            "duration_s": duration,
            "legacy": slot_state.legacy,
            "open_count": slot_state.open_count,
            "complete": slot_state.complete,
            "reason": reason,
        }
        state["queue"] = {
            "waiting": waiting,
            "running": running,
            # Nach `evaluate()`, weil `legacy` von dort kommt: ein Altbestand
            # schliesst die Liste. `open_rids` ist die gefilterte Kopie aus
            # diesem Takt, Geister sind draussen.
            "waiting_list": build_queue(open_rids, waiting,
                                        slot_state.legacy, self._queue_name),
        }
        state["speed"] = {
            "tok_per_s": tok_per_s,
            "ctx_tokens": ctx,
            "max_req_input_len": max_req_input_len,
            "output_pct": output_pct,
        }
        if gpu.fresh:
            state["gpu"] = {
                "util": gpu.value.get("util"),
                "used_mb": gpu.value.get("used_mb"),
                "total_mb": gpu.value.get("total_mb"),
            }
        # `size_mb` wird durchgereicht, auch wenn es None ist: None heisst
        # "nicht gemessen", nicht 0. `history_error` ist der Kanal, ueber den
        # ein fehlgeschlagener Historie-Schreibvorgang auf `/api/state`
        # landet - sonst faellt er nur im Logfile auf und `/api/state` friert
        # bei einer Exception in `tick()` auf dem letzten guten Tick ein.
        state["log"] = {
            "size_mb": rotate.size_mb, "warn": rotate.warn,
            "truncated": truncated, "history_error": self._history_error,
            # Wann zuletzt **gemessen** wurde. Ohne das ist ein Befund von
            # diesem Takt nicht von einem von vor zwei Minuten zu unterscheiden.
            "measured_at": rotate.measured_at,
        }
        state["history"] = self.history.load(now)

        self._state = state
        return state
