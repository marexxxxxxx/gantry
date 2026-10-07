"""Liest das Server-Log inkrementell und wertet den Request-Lifecycle aus.

Rein: bekommt einen Pfad, gibt Events zurueck. Kein Netzwerk, keine Uhr.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field

RECEIVED = "received"
FINISHED = "finished"

ACCESS_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+INFO:\s+"
    r"(?P<peer>\S+)\s+-\s+\"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/[\d.]+\"\s+"
    r"(?P<status>\d+)"
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

    def reset(self):
        self.offset = 0
        self.partial = ""
        self.pending_access = []
        self.state = {"open": {}, "seen": set()}
        self._identity = None

    def _file_identity(self):
        """(st_dev, st_ino) oder None. Erkennt truncate und Neuanlage."""
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_dev, info.st_ino)

    def _read_bytes(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return ""
        identity = self._file_identity()
        # Gleiche inode nach logrotate/truncate -s 0: ab vorn lesen. Die Groesse
        # allein reicht nicht, sie kann nach dem Neuschreiben wieder exakt gleich
        # sein (195 Bytes -> 195 Bytes).
        if self._identity is not None and identity != self._identity:
            self.offset = 0
            self.partial = ""
            self.pending_access = []
        self._identity = identity
        if size < self.offset:
            # Datei wurde extern gekuerzt -> von vorn lesen
            self.offset = 0
            self.partial = ""
        if size == self.offset:
            return ""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(size - self.offset)
                self.offset = handle.tell()
        except OSError:
            return ""
        return chunk.decode("utf-8", errors="replace")

    def _collect(self, line):
        access = ACCESS_RE.search(line)
        if access:
            self.pending_access.append(
                {
                    "ip_port": access.group("peer"),
                    "path": access.group("path"),
                    "at": _parse_access_ts(access.group("ts")),
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
                if abs(access["at"] - event.at) <= self.pair_window_s:
                    if best is None or abs(access["at"] - event.at) < abs(best["at"] - event.at):
                        best = access
            if best is not None:
                event.meta["ip_port"] = best["ip_port"]
                self.pending_access.remove(best)
        self._prune_access(events)
        return events

    def _prune_access(self, events):
        """Nur /v1/-Zeilen im Fenster behalten.

        Im echten Log stehen 92450 Access-Zeilen, davon ~2300 unter /v1/. Ohne
        dieses Aufräumen wächst pending_access um ~4300 Einträge pro Stunde
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
                    self.state["open"][event.rid] = event.meta.get("received_time") or event.at
            else:
                self.state["seen"].add(event.rid)
                self.state["open"].pop(event.rid, None)
        return events

    def poll(self):
        chunk = self._read_bytes()
        if not chunk and not self.partial:
            return []
        text = self.partial + chunk
        lines = text.split("\n")
        self.partial = lines.pop()
        events = []
        for line in lines:
            found = self._collect(line)
            if found is not None:
                events.append(found)
        self._attach_ip(events)
        return self._apply(events)
