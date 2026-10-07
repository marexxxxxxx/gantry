"""Liest das Server-Log inkrementell und wertet den Request-Lifecycle aus.

Rein: bekommt einen Pfad, gibt Events zurueck. Kein Netzwerk, keine Uhr.

Zwei Uhren, nicht verwechseln
-----------------------------
``LogEvent.at`` und ``state["open"]`` sind **Unix-Zeit** (~1.79e9, aus dem
``timestamp``-Feld). ``meta["received_time"]`` ist sglangs **monotoner**
Zaehler (~4.7e5, ab Serverstart). Nur die Unix-Zeit darf mit ``time.time()``
verglichen werden; die received_time taugt zum Ordnen, nicht als Uhrzeit.
Deshalb wird sie nicht in ``state["open"]`` gespeichert.

Bekannte Grenze: IP-Zuordnung ist nicht rueckwirkend
----------------------------------------------------
Ein ``finished``-Event bekommt seine IP nur, wenn die zugehoerige Access-Zeile
bereits im selben oder einem frueheren ``poll()`` gesehen wurde. Taucht die
Access-Zeile erst in einem *spaeteren* ``poll()`` auf, ist das Event längst
an den Aufrufer (Task 8 Collector) ausgeliefert und bleibt bei ``ip_port=None``.
Das entspricht der Logreihenfolge (uvicorn loggt die Access-Zeile beim
Abschluss, das JSON-``request.finished`` kurz danach), ist aber nicht
behebbar, ohne die Events zurueckzuhalten - dann haette der Collector keine
entgueltige, aktuelle Slot-Anzeige mehr. ``test_ip_cannot_backfill_a_previously_returned_event``
haelt diese Grenze fest.
"""

import codecs
import json
import os
import re
import time
from dataclasses import dataclass, field

RECEIVED = "received"
FINISHED = "finished"

#: So viele Bytes liest ein poll() maximal auf einmal. Ein 1-GB-Log (max_log_mb
#: steht auf 1024) waere sonst ein ~4-GB-Block im Speicher und ein langer
#: Stall. 1 MB ist gross genug fuer einen schnellen Durchlauf und klein genug,
#: dass der Speicherbedarf konstant bleibt.
CHUNK_BYTES = 1 << 20

#: Nur 2xx gilt als erfolgreicher Request. Eine 500 oder ein abgebrochener
#: Client (499) darf nicht als Slot-Halter erscheinen.
OK_STATUS_MIN = 200
OK_STATUS_MAX = 299

ACCESS_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+INFO:\s+"
    r"(?P<peer>\S+)\s+-\s+\"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/[\d.]+\"\s+"
    r"(?P<status>\d{3})"
)


@dataclass
class LogEvent:
    kind: str
    rid: str
    at: float
    meta: dict = field(default_factory=dict)


def _parse_access_ts(text):
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def _finish_reason(meta_info):
    reason = meta_info.get("finish_reason")
    if isinstance(reason, dict):
        return reason.get("type")
    return reason


def _meta_from_finished(record):
    obj = record.get("obj") or {}
    meta_info = ((record.get("out") or {}).get("meta_info")) or {}
    return {
        "prompt_tokens": meta_info.get("prompt_tokens"),
        "completion_tokens": meta_info.get("completion_tokens"),
        "cached_tokens": meta_info.get("cached_tokens"),
        "finish_reason": _finish_reason(meta_info),
        "matched": (meta_info.get("finish_reason") or {}).get("matched")
        if isinstance(meta_info.get("finish_reason"), dict) else None,
        "received_time": obj.get("received_time"),
        "ip_port": None,
        "status": None,
    }


class LogReader:
    def __init__(self, path, pair_window_s=30.0):
        self.path = path
        self.pair_window_s = pair_window_s
        self.offset = 0
        self.partial = ""
        self.pending_access = []
        self.state = {"open": {}, "seen": set()}
        self._identity = None
        #: Letzter Lesefehler als Text, sonst None. "Kein neues Byte" und
        #: "konnte nicht lesen" sind verschiedene Zustaende und werden nicht
        #: mehr beide als leeres Ergebnis gemeldet.
        self.last_error = None
        # Zeichenweiser Decoder: ein ueber die Chunk-Grenze geteiltes UTF-8-
        # Zeichen darf nicht zu U+FFFD werden.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def reset(self):
        self.offset = 0
        self.partial = ""
        self.pending_access = []
        self.state = {"open": {}, "seen": set()}
        self._identity = None
        self.last_error = None
        self._decoder.reset()

    def _file_identity(self):
        """(st_dev, st_ino) oder None. Erkennt truncate und Neuanlage."""
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_dev, info.st_ino)

    def _restart(self):
        """Von vorn lesen: Offset, Restzeile, Decoder und Zuordnungsreserve."""
        self.offset = 0
        self.partial = ""
        self.pending_access = []
        self._decoder.reset()

    def _read_raw(self, limit):
        """Bis zu `limit` rohe Bytes ab self.offset. None bei Lesefehler."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read(limit)
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        self.offset = self.offset + len(data)
        return data

    def _read_bytes(self):
        """Naechsten Chunk als Text. Leerer String = nichts Neues."""
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            self.last_error = None
            return ""
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""

        identity = self._file_identity()
        # Gleiche inode nach logrotate/truncate -s 0: ab vorn lesen. Die Groesse
        # allein reicht nicht, sie kann nach dem Neuschreiben wieder exakt gleich
        # sein (195 Bytes -> 195 Bytes).
        if self._identity is not None and identity != self._identity:
            self._restart()
        self._identity = identity
        if size < self.offset:
            # Datei wurde extern gekuerzt -> von vorn lesen
            self._restart()
        if size == self.offset:
            self.last_error = None
            return ""

        want = min(CHUNK_BYTES, size - self.offset)
        data = self._read_raw(want)
        if data is None:
            return ""
        self.last_error = None
        return self._decoder.decode(data)

    def _collect(self, line):
        access = ACCESS_RE.search(line)
        if access:
            self.pending_access.append(
                {
                    "ip_port": access.group("peer"),
                    "path": access.group("path"),
                    "at": _parse_access_ts(access.group("ts")),
                    "status": int(access.group("status")),
                }
            )
            return None

        stripped = line.strip()
        if not stripped.startswith("{"):
            return None
        try:
            record = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(record, dict):
            return None

        kind_name = record.get("event")
        rid = record.get("rid")
        if not rid or kind_name not in ("request.received", "request.finished"):
            return None

        stamp = record.get("timestamp", "")
        try:
            at = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            at = time.time()

        if kind_name == "request.received":
            meta = {"received_time": (record.get("obj") or {}).get("received_time"),
                    "ip_port": None}
            kind = RECEIVED
        else:
            meta = _meta_from_finished(record)
            kind = FINISHED

        return LogEvent(kind=kind, rid=rid, at=at, meta=meta)

    def _attach_ip(self, events):
        for event in events:
            if event.kind != FINISHED:
                continue
            best = None
            for access in self.pending_access:
                if not access["path"].startswith("/v1/"):
                    continue
                # Nur erfolgreiche Requests koennen den Slot gehalten haben.
                if not OK_STATUS_MIN <= access["status"] <= OK_STATUS_MAX:
                    continue
                if abs(access["at"] - event.at) <= self.pair_window_s:
                    if best is None or abs(access["at"] - event.at) < abs(best["at"] - event.at):
                        best = access
            if best is not None:
                event.meta["ip_port"] = best["ip_port"]
                event.meta["status"] = best["status"]
                self.pending_access.remove(best)
            elif self._nearest_failed(event) is not None:
                # Fehlgeschlagener Request: Status mitgeben, aber keine IP als
                # Halter. Task 8 kann das als Fehlerfall behandeln.
                event.meta["status"] = self._nearest_failed(event)["status"]
        self._prune_access(events)
        return events

    def _nearest_failed(self, event):
        best = None
        for access in self.pending_access:
            if not access["path"].startswith("/v1/"):
                continue
            if OK_STATUS_MIN <= access["status"] <= OK_STATUS_MAX:
                continue
            if abs(access["at"] - event.at) <= self.pair_window_s:
                if best is None or abs(access["at"] - event.at) < abs(best["at"] - event.at):
                    best = access
        return best

    def _prune_access(self, events):
        """Nur /v1/-Zeilen im Fenster behalten.

        Im echten Log stehen 92450 Access-Zeilen, davon ~2300 unter /v1/. Ohne
        dieses Aufraeumen waechst pending_access um ~4300 Eintraege pro Stunde
        (die /metrics-Scrapes des Monitors) und frisst unbegrenzt Speicher.
        """
        newest = max((event.at for event in events), default=None)
        newest_access = max((a["at"] for a in self.pending_access), default=None)
        if newest_access is not None:
            newest = newest_access if newest is None else max(newest, newest_access)
        if newest is None:
            return
        cutoff = newest - self.pair_window_s
        self.pending_access = [
            a
            for a in self.pending_access
            if a["path"].startswith("/v1/") and a["at"] >= cutoff
        ]

    def _apply(self, events):
        for event in events:
            if event.kind == RECEIVED:
                if event.rid not in self.state["seen"]:
                    self.state["seen"].add(event.rid)
                    # Unix-Zeit, NICHT meta["received_time"]: Task 8 rechnet
                    # now - open[rid] und wuerste sonst ~1,79 Mrd. Sekunden
                    # bekommen. Die received_time bleibt in meta fuer Orden.
                    self.state["open"][event.rid] = event.at
            else:
                self.state["seen"].add(event.rid)
                self.state["open"].pop(event.rid, None)
        return events

    def _process(self, text):
        """Text + mitgeschleppte Restzeile -> Events, neue Restzeile merken."""
        events = []
        # self.partial gehoert VOR den neuen Chunk, sonst faellt genau die eine
        # Zeile weg, die ueber die Chunk-Grenze laeuft.
        lines = (self.partial + text).split("\n")
        # Die letzte Zeile ist entweder unvollstaendig oder der Leerstring nach
        # einem abschliessenden Newline - die gehoert in partial.
        self.partial = lines.pop()
        for line in lines:
            found = self._collect(line)
            if found is not None:
                events.append(found)
        return events

    def poll(self):
        events = []
        # Mehrere Chunks in einem poll(), damit ein grosser Rueckstand nicht
        # ueber viele Takte verteilt ist. Das Lesen selbst bleibt in
        # CHUNK_BYTES-Bloecken, der Speicherbedarf also konstant.
        while True:
            chunk = self._read_bytes()
            if not chunk and not self.partial:
                break
            events.extend(self._process(chunk))
            if not chunk:
                break
        # Auch ohne Events aufraeumen: eine Stunde nur /metrics-Scrapes darf
        # die Zuordnungsreserve nicht unbegrenzt wachsen lassen.
        self._attach_ip(events)
        return self._apply(events)
