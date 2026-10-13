import os
import tempfile
import unittest

from aiserver_monitor.config import load_config


class TestLoadConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config(env={})
        self.assertEqual(cfg.listen_host, "0.0.0.0")
        self.assertEqual(cfg.listen_port, 30000)
        self.assertEqual(cfg.upstream_base, "http://127.0.0.1:30001")
        self.assertEqual(cfg.db_path, "data/monitor.db")
        self.assertEqual(cfg.log_path, "data/aiserver-monitor.log")
        self.assertAlmostEqual(cfg.heartbeat_s, 15.0)
        self.assertAlmostEqual(cfg.poll_interval_s, 2.0)
        self.assertEqual(cfg.preview_max_chars, 4096)
        self.assertEqual(cfg.prompt_start_chars, 100)
        self.assertEqual(cfg.retention_jobs_days, 30)
        self.assertEqual(cfg.retention_series_days, 7)
        self.assertIs(cfg.nonstream_keepalive, True)
        self.assertEqual(cfg.gpu_cmd, "nvidia-smi")
        self.assertAlmostEqual(cfg.connect_timeout_s, 10.0)

    def test_toml_und_env_override(self):
        # toml: heartbeat_s = 5.0 ; env: AISERVER_MONITOR_LISTEN_PORT=39999
        # Erwartung: Datei schlaegt Default, env schlaegt Datei.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("heartbeat_s = 5.0\nlisten_port = 31000\n")
            cfg = load_config(path=path, env={"AISERVER_MONITOR_LISTEN_PORT": "39999"})
        self.assertAlmostEqual(cfg.heartbeat_s, 5.0)
        self.assertEqual(cfg.listen_port, 39999)

    def test_env_typkonvertierung(self):
        cfg = load_config(env={
            "AISERVER_MONITOR_NONSTREAM_KEEPALIVE": "false",
            "AISERVER_MONITOR_CONNECT_TIMEOUT_S": "2.5",
            "AISERVER_MONITOR_RETENTION_SERIES_DAYS": "4",
        })
        self.assertIs(cfg.nonstream_keepalive, False)
        self.assertAlmostEqual(cfg.connect_timeout_s, 2.5)
        self.assertEqual(cfg.retention_series_days, 4)
        self.assertIsInstance(cfg.retention_series_days, int)

    def test_leerer_env_wert_gilt_als_nicht_gesetzt(self):
        cfg = load_config(env={"AISERVER_MONITOR_GPU_CMD": ""})
        self.assertEqual(cfg.gpu_cmd, "nvidia-smi")

    def test_fehlende_explizite_datei(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gibt-es-nicht.toml")
            with self.assertRaises(FileNotFoundError):
                load_config(path=path, env={})


if __name__ == "__main__":
    unittest.main()
