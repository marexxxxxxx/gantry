"""Tests fuer den Metrik-Parser (Prometheus, nvidia-smi) und extract_sample."""

import json
import unittest

from aiserver_monitor.metrics_parse import (
    METRIC_NAMES,
    GpuSample,
    extract_sample,
    parse_nvidia_smi,
    parse_prometheus,
)

#: Beispiel im Prometheus-Textformat: Kommentarzeilen, Labels, zwei Samples
#: derselben Metrik (werden summiert) und eine Zeile mit unlesbarem Wert.
PROMETHEUS_BEISPIEL = """\
# HELP sglang:num_running_reqs Laufende Anfragen
# TYPE sglang:num_running_reqs gauge
sglang:num_running_reqs{engine_type="unified",model_name="qwen"} 1
sglang:num_running_reqs 2
sglang:num_queue_reqs 7
sglang:gen_throughput{engine_type="unified"} 49.05
sglang:token_usage 123456.5
kaputt kein_wert
"""


class TestParsePrometheus(unittest.TestCase):
    def test_summiert_samples_je_metrikname(self):
        parsed = parse_prometheus(PROMETHEUS_BEISPIEL)
        # Labels werden entfernt, zwei Samples derselben Metrik summiert.
        self.assertEqual(parsed["sglang:num_running_reqs"], 3.0)
        self.assertEqual(parsed["sglang:num_queue_reqs"], 7.0)
        self.assertEqual(parsed["sglang:gen_throughput"], 49.05)
        # Token-Gauges mit grossen Werten bleiben floats.
        self.assertEqual(parsed["sglang:token_usage"], 123456.5)
        self.assertIsInstance(parsed["sglang:token_usage"], float)

    def test_fehlende_metrik_kein_null(self):
        # sglang:gen_throughput kommt im Text nicht vor -> Key fehlt, nicht 0.
        ohne = parse_prometheus("sglang:num_running_reqs 1\n")
        self.assertNotIn("sglang:gen_throughput", ohne)
        self.assertNotIn("sglang:max_total_num_tokens", ohne)

    def test_unlesbare_werte_werden_verworfen(self):
        parsed = parse_prometheus("kaputt kein_wert\nsglang:num_running_reqs 1\n")
        self.assertEqual(parsed, {"sglang:num_running_reqs": 1.0})

    def test_nan_wird_verworfen(self):
        # NaN ist im Prometheus-Format gueltig, aber kein brauchbarer Messwert;
        # gueltige Samples derselben Metrik zaehlen weiter.
        parsed = parse_prometheus(
            "sglang:num_running_reqs 1\nsglang:num_running_reqs NaN\n")
        self.assertEqual(parsed, {"sglang:num_running_reqs": 1.0})

    def test_inf_werte_werden_verworfen(self):
        parsed = parse_prometheus(
            "sglang:gen_throughput +Inf\n"
            "sglang:token_usage -Inf\n"
            "sglang:num_queue_reqs Inf\n"
        )
        self.assertNotIn("sglang:gen_throughput", parsed)
        self.assertNotIn("sglang:token_usage", parsed)
        self.assertNotIn("sglang:num_queue_reqs", parsed)

    def test_leerer_text_ergibt_leeres_dict(self):
        self.assertEqual(parse_prometheus(""), {})


class TestParseNvidiaSmi(unittest.TestCase):
    def test_beispielzeile(self):
        self.assertEqual(parse_nvidia_smi("12, 3456, 16384\n"),
                         GpuSample(12, 3456, 16384))

    def test_fehlerhaftes_feld_wird_none(self):
        sample = parse_nvidia_smi("12, [N/A], 16384\n")
        self.assertEqual(sample, GpuSample(12, None, 16384))
        self.assertIsNone(sample.vram_used_mb)

    def test_unendliches_feld_wird_none(self):
        # "inf" ist mit float lesbar, aber int(float("inf")) wirft
        # OverflowError; das Feld bleibt None statt die Zeile zu sprengen.
        self.assertEqual(parse_nvidia_smi("inf, 12, 34\n"),
                         GpuSample(None, 12, 34))
        self.assertEqual(parse_nvidia_smi("-inf, 12, 34\n"),
                         GpuSample(None, 12, 34))

    def test_leere_oder_falsche_ausgabe(self):
        self.assertEqual(parse_nvidia_smi(""), GpuSample(None, None, None))
        self.assertEqual(parse_nvidia_smi("1, 2\n"), GpuSample(None, None, None))


class TestExtractSample(unittest.TestCase):
    def test_vollstaendiges_sample(self):
        parsed = {
            "sglang:num_running_reqs": 2.0,
            "sglang:num_queue_reqs": 5.0,
            "sglang:gen_throughput": 41.5,
            "sglang:token_usage": 4096.0,
            "sglang:max_total_num_tokens": 32768.0,
        }
        sample = extract_sample(parsed, GpuSample(12, 3456, 16384),
                                ts=1000.5, available=True)
        self.assertEqual(sample["ts"], 1000.5)
        self.assertIs(sample["available"], True)
        self.assertEqual(sample["running"], 2)
        self.assertIsInstance(sample["running"], int)
        self.assertEqual(sample["waiting"], 5)
        self.assertEqual(sample["throughput"], 41.5)
        self.assertEqual(sample["token_usage"], 4096.0)
        self.assertEqual(sample["max_tokens"], 32768.0)
        self.assertEqual(sample["gpu_util"], 12)
        self.assertEqual(sample["vram_used_mb"], 3456)
        self.assertEqual(sample["vram_total_mb"], 16384)

    def test_nicht_verfuegbar_nur_ts_und_available(self):
        sample = extract_sample({}, GpuSample(None, None, None),
                                ts=1.0, available=False)
        self.assertEqual(sample, {"ts": 1.0, "available": False})

    def test_fehlende_werte_fehlen_als_key(self):
        sample = extract_sample(
            {"sglang:num_running_reqs": 1.0},
            GpuSample(12, None, None),
            ts=2.0,
            available=True,
        )
        self.assertEqual(sample["running"], 1)
        self.assertEqual(sample["gpu_util"], 12)
        # nicht gemessen -> Key fehlt, nie 0 / None-Eintrag
        for key in ("waiting", "throughput", "token_usage", "max_tokens",
                    "vram_used_mb", "vram_total_mb"):
            self.assertNotIn(key, sample)

    def test_nicht_finite_werte_erreichen_das_sample_nicht(self):
        # NaN/+Inf aus dem Prometheus-Text werden verworfen; extract_sample
        # bleibt crashfrei (int(nan)/int(inf)) und liefert strenges JSON.
        parsed = parse_prometheus(
            "sglang:num_running_reqs NaN\n"
            "sglang:num_queue_reqs +Inf\n"
            "sglang:gen_throughput 1.5\n"
        )
        sample = extract_sample(parsed, GpuSample(None, None, None),
                                ts=1.0, available=True)
        self.assertEqual(sample["throughput"], 1.5)
        self.assertNotIn("running", sample)
        self.assertNotIn("waiting", sample)
        json.dumps(sample, allow_nan=False)     # wirft bei NaN/Infinity

    def test_metriknamen_mapping(self):
        self.assertEqual(METRIC_NAMES, {
            "running": "sglang:num_running_reqs",
            "waiting": "sglang:num_queue_reqs",
            "throughput": "sglang:gen_throughput",
            "token_usage": "sglang:token_usage",
            "max_tokens": "sglang:max_total_num_tokens",
        })


if __name__ == "__main__":
    unittest.main()
