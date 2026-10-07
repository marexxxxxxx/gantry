import json
import os
import unittest
from unittest import mock

import httpx

from slotmon.config import load_config
from slotmon.sources import Sample, ServerSources

METRICS_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "metrics_sample.txt"
)


def read_metrics_fixture():
    with open(METRICS_FIXTURE, "r", encoding="utf-8") as handle:
        return handle.read()


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
        # Das Praefix ist Teil des Vertrags: `f"{exc}"` allein verliert die
        # Fehlerart (ConnectError vs Timeout vs InvalidURL sind verschiedene
        # Stoerungen fuer den Leser).
        self.assertTrue(sample.error.startswith("ConnectError: "), sample.error)

    def test_load_invalid_url_is_captured_not_raised(self):
        """`httpx.InvalidURL` erbt NICHT von `httpx.HTTPError`.

        Ein Tippfehler in `API` (z. B. `http://127.0.0.1:30000{}`) darf den
        Reader nicht mitnehmen.
        """
        self.assertNotIn(httpx.InvalidURL, httpx.HTTPError.__subclasses__())
        self.assertFalse(issubclass(httpx.InvalidURL, httpx.HTTPError))
        cfg = load_config({"API": "http://127.0.0.1:30000{}"})
        sources = ServerSources(cfg)
        with mock.patch("httpx.get", side_effect=httpx.InvalidURL("bad url")):
            sample = sources.read_load()
        self.assertIsNone(sample.value)
        self.assertTrue(sample.error.startswith("InvalidURL: "), sample.error)

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

    def test_labelled_metrics_are_parsed(self):
        """Der Server emittiert jede Metrik mit Label-Bundle, nie ohne.

        Ohne Label-Toleranz im Ausdruck bleibt das hier still leer: alle drei
        Werte None, `error` trotzdem None. Genau das darf nicht passieren.
        """
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text=read_metrics_fixture())):
            sample = self.sources.read_metrics()
        self.assertIsNone(sample.error)
        self.assertAlmostEqual(sample.value["gen_throughput"], 51.34)
        self.assertEqual(sample.value["num_queue_reqs"], 1689.0)
        self.assertEqual(sample.value["num_running_reqs"], 1.0)

    def test_offline_batch_is_not_mistaken_for_running_reqs(self):
        """`sglang:num_running_reqs_offline_batch` ist eine eigene Metrik.

        Sie steht im Output hinter `num_running_reqs` und ist keine Variante
        davon - ein Praefix-Abgleich statt eines exakten Namensvergleichs
        ueberschriebe den Wert mit 0.0.
        """
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text=read_metrics_fixture())):
            sample = self.sources.read_metrics()
        self.assertEqual(sample.value["num_running_reqs"], 1.0)
        self.assertNotIn("num_running_reqs_offline_batch", sample.value)

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

    def test_gpu_permission_error_is_captured(self):
        """PermissionError ist ein OSError, kein FileNotFoundError."""
        self.assertTrue(issubclass(PermissionError, OSError))
        with mock.patch("subprocess.run", side_effect=PermissionError("denied")):
            sample = self.sources.read_gpu()
        self.assertIsNone(sample.value)
        self.assertTrue(sample.error.startswith("PermissionError: "), sample.error)

    def test_gpu_other_os_error_is_captured(self):
        """Die ganze OSError-Familie, nicht nur zwei Einzelfaelle."""
        with mock.patch("subprocess.run", side_effect=OSError(5, "io")):
            sample = self.sources.read_gpu()
        self.assertIsNone(sample.value)
        self.assertIsNotNone(sample.error)


class TestSampleContract(unittest.TestCase):
    """`fresh` und die `timeout`-Parameter sind veroeffentlichte Schnittstelle."""

    def setUp(self):
        self.sources = ServerSources(load_config({}))

    def test_fresh_needs_value_and_at(self):
        self.assertFalse(Sample().fresh)
        self.assertFalse(Sample(value={"a": 1}).fresh)
        self.assertFalse(Sample(at=100.0).fresh)
        self.assertTrue(Sample(value={"a": 1}, at=100.0).fresh)

    def test_error_sample_is_not_fresh(self):
        self.assertFalse(Sample(None, None, "boom").fresh)

    def _timeout_seen(self, method, **kwargs):
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, text="{}")) as get:
            getattr(self.sources, method)(**kwargs)
        return get.call_args.kwargs["timeout"]

    def test_http_methods_default_to_three_seconds(self):
        for method in ("read_load", "read_server_info", "read_metrics"):
            with self.subTest(method=method):
                self.assertEqual(self._timeout_seen(method), 3.0)

    def test_http_timeout_is_passed_through(self):
        self.assertEqual(self._timeout_seen("read_load", timeout=0.25), 0.25)

    def test_gpu_default_timeout_is_three_seconds(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(
                returncode=0, stdout="1, 2, 3\n", stderr="")) as run:
            self.sources.read_gpu()
        self.assertEqual(run.call_args.kwargs["timeout"], 3.0)

    def test_gpu_timeout_is_passed_through(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(
                returncode=0, stdout="1, 2, 3\n", stderr="")) as run:
            self.sources.read_gpu(timeout=0.5)
        self.assertEqual(run.call_args.kwargs["timeout"], 0.5)


if __name__ == "__main__":
    unittest.main()
