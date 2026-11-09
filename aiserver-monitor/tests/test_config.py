import os
import tempfile
import unittest

from aiserver_monitor.config import Config, load_config


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
        self.assertIs(cfg.abort_on_disconnect, True)
        self.assertEqual(cfg.gpu_cmd, "nvidia-smi")
        self.assertAlmostEqual(cfg.connect_timeout_s, 10.0)
        self.assertEqual(cfg.embed_base, "http://127.0.0.1:30002")
        self.assertEqual(cfg.embed_model, "qwen3-embedding:0.6b")
        self.assertEqual(cfg.audio_base, "http://127.0.0.1:30003")
        self.assertAlmostEqual(cfg.audio_timeout_s, 1800.0)
        self.assertEqual(cfg.audio_max_upload_mb, 100)
        self.assertEqual(cfg.abl_start_script, "runtime/abl/start-abl.sh")
        self.assertEqual(cfg.abl_stop_script, "runtime/abl/stop-abl.sh")
        self.assertEqual(cfg.abl_pidfile, "runtime/abl/abl.pid")
        self.assertEqual(
            cfg.abl_model,
            "hf.co/bartowski/huihui-ai_Qwen3-14B-abliterated-GGUF:Q4_K_M")

    def test_server_steuerung_defaults_und_env(self):
        cfg = load_config(env={})
        self.assertIs(cfg.server_control_enabled, True)
        self.assertEqual(cfg.server_unit, "escha-server.service")
        self.assertEqual(cfg.server_start_script,
                         "runtime/sglang/start-penalty.sh")
        self.assertEqual(cfg.server_stop_script,
                         "runtime/sglang/stop-server.sh")
        cfg = load_config(env={
            "AISERVER_MONITOR_SERVER_CONTROL_ENABLED": "false",
            "AISERVER_MONITOR_SERVER_UNIT": "test-server.service",
        })
        self.assertIs(cfg.server_control_enabled, False)
        self.assertEqual(cfg.server_unit, "test-server.service")

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

    def test_abort_on_disconnect_override(self):
        # Default true (Disconnect-Aufraeumen); ENV und TOML koennen auf
        # false stellen (= Altverhalten, Jobs laufen zu Ende).
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("abort_on_disconnect = false\n")
            cfg = load_config(path=path, env={})
        self.assertIs(cfg.abort_on_disconnect, False)

        cfg = load_config(env={"AISERVER_MONITOR_ABORT_ON_DISCONNECT": "false"})
        self.assertIs(cfg.abort_on_disconnect, False)
        cfg = load_config(env={"AISERVER_MONITOR_ABORT_ON_DISCONNECT": "true"})
        self.assertIs(cfg.abort_on_disconnect, True)

    def test_leerer_env_wert_gilt_als_nicht_gesetzt(self):
        cfg = load_config(env={"AISERVER_MONITOR_GPU_CMD": ""})
        self.assertEqual(cfg.gpu_cmd, "nvidia-smi")

    def test_fehlende_explizite_datei(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gibt-es-nicht.toml")
            with self.assertRaises(FileNotFoundError):
                load_config(path=path, env={})

    def test_nicht_positive_intervalle_werden_abgewiesen(self):
        # 0 oder negativ wuerde die Keepalive- und Sammelschleife zum
        # Busy-Loop machen; load_config (und damit jede Quelle) wirft.
        for feld in ("heartbeat_s", "poll_interval_s"):
            for wert in ("0", "-1", "-0.5"):
                with self.subTest(feld=feld, wert=wert):
                    with self.assertRaisesRegex(ValueError, feld):
                        load_config(env={
                            f"AISERVER_MONITOR_{feld.upper()}": wert})

    def test_audio_defaults_und_env(self):
        cfg = load_config(env={
            "AISERVER_MONITOR_AUDIO_BASE": "http://127.0.0.1:39003",
            "AISERVER_MONITOR_AUDIO_TIMEOUT_S": "600",
            "AISERVER_MONITOR_AUDIO_MAX_UPLOAD_MB": "50",
        })
        self.assertEqual(cfg.audio_base, "http://127.0.0.1:39003")
        self.assertAlmostEqual(cfg.audio_timeout_s, 600.0)
        self.assertEqual(cfg.audio_max_upload_mb, 50)

    def test_nur_das_praefix_audio_base_wirkt(self):
        # AUDIO_BASE ist die Variable von run.sh, nicht die App-Konfiguration.
        # Die App liest ausschliesslich AISERVER_MONITOR_AUDIO_BASE.
        cfg = load_config(env={
            "AUDIO_BASE": "http://127.0.0.1:39999",
            "AISERVER_MONITOR_AUDIO_BASE": "http://127.0.0.1:39003",
        })
        self.assertEqual(cfg.audio_base, "http://127.0.0.1:39003")

    def test_audio_nicht_positive_limits_werden_abgewiesen(self):
        for feld in ("audio_timeout_s", "audio_max_upload_mb"):
            for wert in ("0", "-1", "-0.5"):
                with self.subTest(feld=feld, wert=wert):
                    with self.assertRaisesRegex(ValueError, feld):
                        load_config(env={
                            f"AISERVER_MONITOR_{feld.upper()}": wert})

    def test_audio_post_init_wirft_direkt(self):
        with self.assertRaisesRegex(ValueError, "audio_timeout_s"):
            Config(audio_timeout_s=0.0)
        with self.assertRaisesRegex(ValueError, "audio_max_upload_mb"):
            Config(audio_max_upload_mb=0)

    def test_post_init_wirft_direkt(self):
        # Auch ohne load_config: eine direkt gebaute Config ist nie ein
        # Busy-Loop.
        with self.assertRaisesRegex(ValueError, "heartbeat_s"):
            Config(heartbeat_s=0.0)
        with self.assertRaisesRegex(ValueError, "poll_interval_s"):
            Config(poll_interval_s=-1.0)

    def test_lifecycle_post_init_wirft_direkt(self):
        for feld, wert in (
                ("lifecycle_timeout_s", 0.0),
                ("lifecycle_unload_timeout_s", -1.0),
                ("lifecycle_poll_s", 0.0)):
            with self.subTest(feld=feld):
                with self.assertRaisesRegex(ValueError, feld):
                    Config(**{feld: wert})

    def test_qwen_tts_timeouts_muessen_positiv_sein(self):
        for feld in ("qwen_tts_load_timeout_s", "qwen_tts_unload_timeout_s"):
            with self.subTest(feld=feld):
                with self.assertRaisesRegex(ValueError, feld):
                    Config(**{feld: 0})

    def test_audio8_defaults(self):
        cfg = load_config(env={})
        self.assertEqual(cfg.audio8_model, "Audio8-Realistic-Air")
        self.assertEqual(cfg.audio8_base, "http://127.0.0.1:30006")
        self.assertEqual(cfg.audio8_model_dir, "/mnt/models/audio/audio8")
        self.assertEqual(cfg.audio8_reference_audio,
                         "/mnt/models/audio/audio8/reference/de_male.wav")
        self.assertEqual(cfg.audio8_reference_text,
                         "/mnt/models/audio/audio8/reference/de_male.txt")
        self.assertEqual(cfg.audio8_start_script,
                         "runtime/audio8/start-audio8.sh")
        self.assertEqual(cfg.audio8_stop_script,
                         "runtime/audio8/stop-audio8.sh")
        self.assertEqual(cfg.audio8_pidfile, "runtime/audio8/audio8.pid")
        self.assertEqual(cfg.audio8_health_path, "/healthz")
        self.assertAlmostEqual(cfg.audio8_load_timeout_s, 900.0)
        self.assertAlmostEqual(cfg.audio8_unload_timeout_s, 60.0)

    def test_audio8_env_und_validierung(self):
        cfg = load_config(env={
            "AISERVER_MONITOR_AUDIO8_BASE": "http://127.0.0.1:39006",
            "AISERVER_MONITOR_AUDIO8_MODEL_DIR": "/tmp/audio8",
        })
        self.assertEqual(cfg.audio8_base, "http://127.0.0.1:39006")
        self.assertEqual(cfg.audio8_model_dir, "/tmp/audio8")
        for feld in ("audio8_load_timeout_s", "audio8_unload_timeout_s"):
            with self.subTest(feld=feld):
                with self.assertRaisesRegex(ValueError, feld):
                    Config(**{feld: 0})
        self.assertEqual(Config(audio8_model="").audio8_model, "")


if __name__ == "__main__":
    unittest.main()
