"""Abgeschlossene Slots als JSONL. Ueberlebt einen Neustart, waechst nicht unbegrenzt."""

import json
import os
import time


class History:
    def __init__(self, path, retention_h=24):
        self.path = path
        self.retention_s = retention_h * 3600

    def _fresh(self, entry, now):
        ended_at = entry.get("ended_at")
        if not isinstance(ended_at, (int, float)):
            return False
        return (now - ended_at) <= self.retention_s

    def load(self, now=None):
        now = time.time() if now is None else now
        # **Bytes** zuerst, dann tolerant dekodieren.
        #
        # Mit `open(..., encoding="utf-8")` wirft ein ungueltiges Byte eine
        # `UnicodeDecodeError` - und die ist ein `ValueError`, **kein
        # `OSError`. Der `except OSError` am Dateioeffnen greift nicht, die
        # Exception verlaesst `load()` und damit `tick()`. Weil `compact()`
        # denselben Weg nimmt, kann der Rotator die Datei auch nicht
        # reparieren: ein einziges Byte nimmt dem Monitor dauerhaft die
        # Historie.
        #
        # `errors="replace"` kostet den einen kaputten Eintrag und laesst alle
        # anderen lesbar. Das ist der Unterschied zwischen einer Luecke in der
        # Anzeige und einem Monitor, der stehenbleibt.
        try:
            with open(self.path, "rb") as handle:
                blob = handle.read()
        except OSError:
            return []
        raw = blob.decode("utf-8", errors="replace")
        kept = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError, RecursionError):
                # `ValueError` deckt `json.JSONDecodeError` mit ab und faengt
                # zusaetzlich, was der Dekoder sonst nochwirft. `RecursionError`
                # ist ein `RuntimeError` und kommt von tief verschachteltem
                # JSON - kein Fehler des Monitors, sondern der Datei.
                continue
            if isinstance(entry, dict) and self._fresh(entry, now):
                kept.append(entry)
        return kept

    def append(self, entry):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def compact(self, now=None):
        now = time.time() if now is None else now
        kept = self.load(now)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            for entry in kept:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return len(kept)
