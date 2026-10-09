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
from slotmon.slot import evaluate
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

#: `#full token: 41997` aus `Decode batch, ...`. Nicht `full token usage` - der
#: Ausdruck verlangt das Doppelpunkt direkt hinter "token".
BATCH_TOKENS = re.compile(r"#full token:\s*(\d+)")

#: `gen throughput (token/s): 36.02` aus derselben Zeile. Dieselbe Decode-
#: Stufe wie `sglang:gen_throughput` aus /metrics, also **keine** zweite
#: Wahrheit: der Wert aus /metrics hat Vorrang, die Logzeile ist der Fallback,
#: wenn /metrics schweigt.
BATCH_THROUGHPUT = re.compile(r"gen throughput \(token/s\):\s*([\d.]+)")


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
    """Eigenverkehr des Servers, kein Nutzer-Request."""
    return any(rid.startswith(prefix) for prefix in RESERVED_RID_PREFIXES)


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
        self.rotator = LogRotator(self.cfg.max_log_mb, self.cfg.warn_log_mb)
        self.monitor_rotator = LogRotator(self.cfg.max_log_mb, self.cfg.warn_log_mb)
        self.compact_every_s = COMPACT_EVERY_S
        self.max_req_input_len = None
        self.finished = {}
        #: rid -> Unix-Zeit des `request.received`. Aus `state["open"]` geht das
        #: nicht mehr hervor, sobald der Reader die rid beim `finished` entfernt.
        self.received_at = {}
        self._last_compact = None
        #: Zuletzt protokollierter Rotationsfehler je Logdatei.
        self._last_rotate_error = {}
        #: rids aus history.jsonl, damit ein Neustart sie nicht doppelt schreibt.
        self._recorded = None
        self._closed = False
        self._state = self._empty_state()

    def close(self):
        """Der Reader laeuft in keinem Thread - es gibt nichts zu stoppen.

        Der Aufrufer ruft das deterministisch am Ende (Test-`addCleanup`,
        Serve-Shutdown); danach wird nur noch `state()` benutzt.
        """
        self._closed = True
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
            },
            "queue": {"waiting": None, "running": None},
            "speed": {"tok_per_s": None, "ctx_tokens": None,
                      "max_req_input_len": self.max_req_input_len,
                      "output_pct": None},
            "gpu": {"util": None, "used_mb": None, "total_mb": None},
            "log": {"size_mb": None, "warn": False, "truncated": False},
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
        for event in self.reader.poll():
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
        Datei mitwächst statt unbegrenzt zu werden.
        """
        if self._recorded is None:
            self._recorded = {entry.get("rid")
                              for entry in self.history.load(now)}
        return self._recorded

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
            record = dict(entry)
            started = record.get("started_at")
            ended = record.get("ended_at")
            record["duration_s"] = self._duration(started, ended, now)
            record["system"] = resolve(self.names, host_of(record.get("ip_port")))
            self.history.append(record)
            written.append(record)
            recorded.add(rid)
            self.received_at.pop(rid, None)
            del self.finished[rid]
        return written

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
        info = self.sources.read_server_info()
        if info.fresh:
            value = info.value.get("max_req_input_len")
            if value:
                self.max_req_input_len = value
        return self.max_req_input_len

    def _compact_history(self, now):
        """Die Historie periodisch auf das Aufbewahrungsfenster kuerzen."""
        if (self._last_compact is not None
                and now - self._last_compact < self.compact_every_s):
            return
        self.history.compact(now)
        self._last_compact = now
        # Mit der Datei mitwachsen: was `compact` weggeworfen hat, muss auch aus
        # dem Filter raus, sonst waechst er ohne Ende.
        self._recorded = None

    def _rotate(self, name, rotator):
        """`check()` aufrufen und das Ergebnis melden.

        Ein Aufrufer-Gate neben `check()` waere die zweite Taktung, an der die
        Rotation schon einmal still ausgefallen ist, ohne zu melden. Deshalb
        ruft der Collector `check()` in **jedem** Pol; die Taktung ist Sache des
        Rotators. Jede Instanz bekommt ihre eigene Datei.
        """
        path = getattr(self.cfg, name)
        rotate = rotator.check(path)
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

    # ------------------------------------------------------------------
    # Takt

    def tick(self):
        now = self.now_fn()
        load = self.sources.read_load()
        metrics = self.sources.read_metrics()
        gpu = self.sources.read_gpu()
        max_req_input_len = self._max_req_input_len()

        self._drain_events()
        # `state["open"]` enthaelt auch die reservierten rids. Die fliegen raus,
        # **bevor** `evaluate()` sie sieht - sonst wuerde der Health-Check des
        # Servers den Slot als dauerhaft belegt ausweisen.
        open_rids = {rid: since for rid, since in self.reader.state["open"].items()
                     if not is_reserved(rid)}
        self._flush_finished(open_rids, now)

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
            waiting = load.value.get("num_waiting_reqs")
            # `Sample.at` ist die Abrufzeit. Genau darauf beruht dieses Alter.
            age = max(0.0, now - load.at)
        else:
            load_error = load.error
        if metrics.fresh:
            running = metrics.value.get("num_running_reqs")

        slot_state = evaluate(open_rids, waiting)

        started_at = None
        duration = None
        if slot_state.holder_rid:
            started_at = slot_state.holder_since
            duration = max(0.0, now - started_at) if started_at is not None else None

        if slot_state.holder_rid and slot_state.complete:
            label = "busy"
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

        ctx, throughput = self._read_batch()
        tok_per_s = metrics.value.get("gen_throughput") if metrics.fresh else None
        if tok_per_s is None:
            # Gleiche Decode-Stufe, nur aus dem Log statt aus /metrics.
            tok_per_s = throughput
        output_pct = None
        if ctx is not None and max_req_input_len:
            output_pct = round(100.0 * ctx / max_req_input_len, 2)

        rotate = self._rotate("server_log", self.rotator)
        self._rotate("monitor_log", self.monitor_rotator)
        if rotate.truncated:
            # Die Datei ist leer; der Reader beginnt dort von vorn. Die
            # `received_at` bleiben stehen: ihre Unix-Zeiten sind gueltig,
            # unabhaengig davon, dass die Zeile im Log nicht mehr existiert.
            self.reader.reset()

        self._compact_history(now)

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
        }
        state["queue"] = {"waiting": waiting, "running": running}
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
        # "nicht gemessen", nicht 0.
        state["log"] = {
            "size_mb": rotate.size_mb, "warn": rotate.warn, "truncated": rotate.truncated
        }
        state["history"] = self.history.load(now)

        self._state = state
        return state
