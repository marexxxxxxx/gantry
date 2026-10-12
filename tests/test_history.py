import json
import os
import shutil
import tempfile
import unittest

from slotmon.history import History

NOW = 1_000_000.0

#: Ein Name mit echten Mehr-Byte-Zeichen. Hier und weiter unten stehen
#: bewusst Umlaute, obwohl die uebrigen Tests ASCII-only sind: der Punkt ist
#: genau die Bytes. `"\u00f6"` waere dasselbe Zeichen, aber als Escape
#: geschrieben sieht man der Datei nicht an, dass sie Mehr-Byte-Zeichen traegt.
NAME_MIT_UMLAUT = "Björn"
NAME_UTF8 = NAME_MIT_UMLAUT.encode("utf-8")


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotmon-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "history.jsonl")

    def write(self, entries):
        with open(self.path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def read(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


def entry(ended_at, rid="x"):
    return {"rid": rid, "system": "Laptop", "ended_at": ended_at, "duration_s": 12.0}


class TestLoad(HistoryCase):
    def test_missing_file_gives_empty_list(self):
        history = History(self.path, 24)
        self.assertEqual(history.load(NOW), [])

    def test_only_recent_entries_load(self):
        self.write([entry(NOW - 3600), entry(NOW - 30 * 3600)])
        loaded = History(self.path, 24).load(NOW)
        self.assertEqual(len(loaded), 1)

    def test_broken_line_is_skipped(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"rid": "a", "ended_at": 1}\n')
            handle.write("kaputt\n")
            handle.write(json.dumps(entry(NOW - 60)) + "\n")
        loaded = History(self.path, 24).load(NOW)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["rid"], "x")

    def test_entry_without_ended_at_is_skipped(self):
        self.write([{"rid": "a"}])
        self.assertEqual(History(self.path, 24).load(NOW), [])

    def test_a_byte_that_is_not_utf8_does_not_raise(self):
        """Ein ungueltiges Byte darf `load()` nicht verlassen.

        `UnicodeDecodeError` ist ein `ValueError` und **kein** `OSError` - der
        `except OSError` am Dateioeffnen greift nicht. Die Datei liest sich
        dann nie wieder, und weil `compact()` denselben Weg nimmt, kann der
        Rotator sie auch nicht reparieren: der Monitor verliert die Historie
        endgueltig.

        Der Ausloeser ist nicht theoretisch: `append()` schreibt mit
        `ensure_ascii=False`, ein Systemname mit Umlaut erzeugt echte
        Mehr-Byte-Zeichen, und ein `kill -9` mitten im Schreiben schneidet
        eines halb durch.

        Der bisherige Test `test_broken_line_is_skipped` deckt das nicht: er
        schreibt mit `encoding="utf-8"` und kann ein ungueltiges Byte gar nicht
        erzeugen. Genau ein Byte gross war die Luecke.

        Geprueft wird mehr als "kein Fehler": die gueltigen Eintraege **vor und
        nach** der bosen Zeile muessen beide da sein. Ein Dekodierer, der die
        ganze Datei verwirft, waere auch "ohne Absturz".
        """
        with open(self.path, "wb") as handle:
            handle.write(json.dumps(entry(NOW - 60, "a")).encode("utf-8"))
            handle.write(b"\n")
            handle.write(b"\xff\n")
            handle.write(json.dumps(entry(NOW - 30, "b")).encode("utf-8"))
            handle.write(b"\n")
        loaded = History(self.path, 24).load(NOW)
        self.assertEqual([e["rid"] for e in loaded], ["a", "b"])

    def test_a_half_written_character_keeps_its_neighbours(self):
        """Der plausible Ausloeser: ein halb geschriebenes Mehr-Byte-Zeichen.

        `ensure_ascii=False` heisst, dass `append()` echte UTF-8 schreibt.
        Bricht der Vorgang mitten in einem Zeichen ab, entsteht genau diese
        Datei: `0xc3` ist die erste Haelfte von "ae-umlaut" und hat als
        alleinstehendes Byte **keine** gueltige UTF-8-Bedeutung.
        """
        with open(self.path, "wb") as handle:
            handle.write(json.dumps(entry(NOW - 60, "a")).encode("utf-8"))
            handle.write(b"\n")
            handle.write(b"\xc3\n")
            handle.write(json.dumps(entry(NOW - 30, "c")).encode("utf-8"))
            handle.write(b"\n")
        loaded = History(self.path, 24).load(NOW)
        self.assertEqual([e["rid"] for e in loaded], ["a", "c"])

    def test_a_name_with_umlaut_survives_a_round_trip(self):
        """Gegenprobe: **gueltige** Mehr-Byte-Zeichen duerfen nicht verloren gehen.

        Es geht nicht darum, kaputte Bytes zu reparieren, sondern darum, dass
        der tolerante Dekodierer echte Umlaute durchlaesst. Der Fehlerfall
        `errors="ignore"` waere nicht durch die beiden anderen Tests zu sehen:
        beide ueberspringen die kaputte Zeile und retten die Nachbarn, auch
        `ignore` tut das.

        Deshalb wird hier das **Ergebnis** geprueft, nicht die Fehlerklasse.
        """
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"rid": "b", "ended_at": NOW - 30,
                                     "system": NAME_MIT_UMLAUT},
                                    ensure_ascii=False) + "\n")
        with open(self.path, "rb") as handle:
            self.assertIn(NAME_UTF8, handle.read())
        loaded = History(self.path, 24).load(NOW)
        self.assertEqual([e["system"] for e in loaded], [NAME_MIT_UMLAUT])


class TestAppend(HistoryCase):
    def test_append_creates_file(self):
        history = History(self.path, 24)
        history.append(entry(NOW))
        self.assertEqual(len(self.read()), 1)

    def test_append_accumulates(self):
        history = History(self.path, 24)
        for i in range(3):
            history.append(entry(NOW, rid=f"r{i}"))
        self.assertEqual([e["rid"] for e in self.read()], ["r0", "r1", "r2"])

    def test_append_creates_missing_directory(self):
        nested = os.path.join(self.dir, "deep", "history.jsonl")
        History(nested, 24).append(entry(NOW))
        self.assertTrue(os.path.exists(nested))


class TestCompact(HistoryCase):
    def test_compact_drops_old_entries(self):
        self.write([entry(NOW - 30 * 3600, rid="old"), entry(NOW - 60, rid="new")])
        history = History(self.path, 24)
        history.compact(NOW)
        self.assertEqual([e["rid"] for e in self.read()], ["new"])

    def test_compact_returns_kept_count(self):
        self.write([entry(NOW - 30 * 3600), entry(NOW - 60), entry(NOW - 120)])
        self.assertEqual(History(self.path, 24).compact(NOW), 2)

    def test_compact_on_missing_file_is_harmless(self):
        history = History(self.path, 24)
        self.assertEqual(history.compact(NOW), 0)


if __name__ == "__main__":
    unittest.main()
