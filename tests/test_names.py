import json
import os
import tempfile
import unittest

from slotmon.names import load_names, resolve


class TestLoad(unittest.TestCase):
    def _write(self, content):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        handle.write(content)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_reads_mapping(self):
        path = self._write(json.dumps({"192.168.9.2": "Laptop"}))
        self.assertEqual(load_names(path), {"192.168.9.2": "Laptop"})

    def test_missing_file_aborts(self):
        with self.assertRaises(SystemExit):
            load_names("/tmp/does-not-exist-slotmon.json")

    def test_broken_syntax_aborts(self):
        path = self._write("{not json")
        with self.assertRaises(SystemExit):
            load_names(path)

    def test_wrong_shape_aborts(self):
        path = self._write('["a", "b"]')
        with self.assertRaises(SystemExit):
            load_names(path)


class TestResolve(unittest.TestCase):
    def test_known_ip_gives_name(self):
        self.assertEqual(resolve({"192.168.9.2": "Laptop"}, "192.168.9.2"), "Laptop")

    def test_unknown_ip_falls_back_to_raw_ip(self):
        self.assertEqual(resolve({"192.168.9.2": "Laptop"}, "10.0.0.5"), "10.0.0.5")

    def test_missing_ip_gives_none(self):
        self.assertIsNone(resolve({"192.168.9.2": "Laptop"}, None))

    def test_empty_ip_gives_none(self):
        self.assertIsNone(resolve({}, ""))


if __name__ == "__main__":
    unittest.main()
