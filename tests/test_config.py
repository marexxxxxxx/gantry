import os
import unittest

from slotmon.config import load_config


class TestDefaults(unittest.TestCase):
    def test_defaults_match_spec(self):
        cfg = load_config({})
        self.assertEqual(cfg.poll_interval, 2.0)
        self.assertEqual(cfg.api, "http://127.0.0.1:30000")
        self.assertEqual(cfg.auth, "Bearer marec")
        self.assertEqual(cfg.server_log, "/tmp/escha_server_lan.log")
        self.assertEqual(cfg.monitor_log, "/tmp/slotmon.log")
        self.assertEqual(cfg.history, "/tmp/slotmon/history.jsonl")
        self.assertEqual(cfg.retention_h, 24)
        self.assertEqual(cfg.max_log_mb, 1024)
        self.assertEqual(cfg.warn_log_mb, 900)
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertEqual(cfg.port, 8090)
        self.assertEqual(cfg.log_pair_window_s, 30.0)


class TestOverrides(unittest.TestCase):
    def test_every_value_is_overridable(self):
        cfg = load_config({
            "PORT": "9001",
            "HOST": "127.0.0.1",
            "API": "http://192.168.1.24:30000",
            "AUTH": "Bearer other",
            "SERVER_LOG": "/tmp/other.log",
            "MONITOR_LOG": "/tmp/m.log",
            "HISTORY": "/tmp/h.jsonl",
            "RETENTION_H": "6",
            "MAX_LOG_MB": "10",
            "WARN_LOG_MB": "8",
            "POLL_INTERVAL": "5",
            "LOG_PAIR_WINDOW_S": "60",
        })
        self.assertEqual(cfg.port, 9001)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.api, "http://192.168.1.24:30000")
        self.assertEqual(cfg.auth, "Bearer other")
        self.assertEqual(cfg.server_log, "/tmp/other.log")
        self.assertEqual(cfg.monitor_log, "/tmp/m.log")
        self.assertEqual(cfg.history, "/tmp/h.jsonl")
        self.assertEqual(cfg.retention_h, 6)
        self.assertEqual(cfg.max_log_mb, 10)
        self.assertEqual(cfg.warn_log_mb, 8)
        self.assertEqual(cfg.poll_interval, 5.0)
        self.assertEqual(cfg.log_pair_window_s, 60.0)

    def test_numeric_strings_become_numbers(self):
        cfg = load_config({"PORT": "9001", "POLL_INTERVAL": "0.5"})
        self.assertIsInstance(cfg.port, int)
        self.assertIsInstance(cfg.poll_interval, float)
        self.assertEqual(cfg.poll_interval, 0.5)


if __name__ == "__main__":
    unittest.main()
