import json
import unittest
from unittest import mock

import httpx

from slotmon.config import load_config
from slotmon.sources import ServerSources


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config({"API": "http://testserver:30000", "AUTH": "Bearer marec"})
        self.sources = ServerSources(self.cfg)

    def test_load_parses_queue_length(self):
        payload = [{"num_reqs": 12, "num_waiting_reqs": 11}]
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text=json.dumps(payload))) as get:
            sample = self.sources.read_load()
        self.assertEqual(sample.value["num_waiting_reqs"], 11)
        self.assertEqual(sample.value["num_reqs"], 12)
        self.assertIsNone(sample.error)
        self.assertIsNotNone(sample.at)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer marec")

    def test_load_error_is_captured_not_raised(self):
        with mock.patch("httpx.get", side_effect=httpx.ConnectError("down")):
            sample = self.sources.read_load()
        self.assertIsNone(sample.value)
        self.assertIn("down", sample.error)

    def test_load_http_error_is_captured(self):
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=401, text="unauthorized")):
            sample = self.sources.read_load()
        self.assertIsNone(sample.value)
        self.assertIn("401", sample.error)

    def test_load_garbage_is_captured(self):
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text="not json")):
            sample = self.sources.read_load()
        self.assertIsNone(sample.value)
        self.assertIsNotNone(sample.error)

    def test_metrics_are_parsed(self):
        body = (
            "# HELP x help\n"
            "sglang:gen_throughput 49.05\n"
            "sglang:num_queue_reqs 2301\n"
            "sglang:num_running_reqs 1\n"
        )
        with mock.patch("httpx.get", return_value=mock.Mock(status_code=200, text=body)):
            sample = self.sources.read_metrics()
        self.assertAlmostEqual(sample.value["gen_throughput"], 49.05)
        self.assertEqual(sample.value["num_queue_reqs"], 2301)
        self.assertEqual(sample.value["num_running_reqs"], 1)

    def test_missing_metric_yields_none(self):
        with mock.patch("httpx.get", return_value=mock.Mock(status_code=200, text="# empty\n")):
            sample = self.sources.read_metrics()
        self.assertIsNone(sample.value["gen_throughput"])
        self.assertIsNone(sample.error)

    def test_server_info_gives_max_req_input_len(self):
        payload = {"max_req_input_len": 83994, "max_total_num_tokens": 89856}
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text=json.dumps(payload))):
            sample = self.sources.read_server_info()
        self.assertEqual(sample.value["max_req_input_len"], 83994)


class TestGpu(unittest.TestCase):
    def setUp(self):
        self.sources = ServerSources(load_config({}))

    def test_gpu_values_are_parsed(self):
        output = "42, 15424, 16303\n"
        with mock.patch("subprocess.run", return_value=mock.Mock(
                returncode=0, stdout=output, stderr="")):
            sample = self.sources.read_gpu()
        self.assertEqual(sample.value["util"], 42)
        self.assertEqual(sample.value["used_mb"], 15424)
        self.assertEqual(sample.value["total_mb"], 16303)

    def test_missing_nvidia_smi_is_captured(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            sample = self.sources.read_gpu()
        self.assertIsNone(sample.value)
        self.assertIsNotNone(sample.error)

    def test_gpu_failure_never_reports_zero(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(
                returncode=1, stdout="", stderr="boom")):
            sample = self.sources.read_gpu()
        self.assertIsNone(sample.value)


if __name__ == "__main__":
    unittest.main()