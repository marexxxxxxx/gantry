import json
import os
import shutil
import tempfile
import unittest

from slotmon.history import History

NOW = 1_000_000.0


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
