"""Fragt den sglang-Server. Jede Quelle liefert einen Zustand, wirft nichts."""

import json
import re
import subprocess
import time
from dataclasses import dataclass

import httpx

METRIC_NAMES = ("gen_throughput", "num_queue_reqs", "num_running_reqs")

#: Prometheus-Textformat: `sglang:gen_throughput{engine_type="unified",...} 50.79`.
#: Der echte Server haengt fast immer ein Label-Bundle an den Namen, deshalb ist
#: der Block optional statt wegoptimiert - und `:` gehoert in die Namensklasse.
#: Ohne beides lieferte der Ausdruck auf dem echten /metrics null Treffer.
METRIC_LINE = re.compile(r"^(?P<name>[\w:]+)(?:\{[^}]*\})?\s+(?P<value>[-\d.eE+]+)$")

#: sglang praefixt seine Metriken mit dem Namespace. METRIC_NAMES fuehrt die
#: nackten Namen, weil das die Stable Interface des Dashboards ist.
METRIC_PREFIX = "sglang:"


@dataclass
class Sample:
    value: object = None
    at: float | None = None
    error: str | None = None

    @property
    def fresh(self):
        return self.value is not None and self.at is not None


class ServerSources:
    def __init__(self, cfg):
        self.cfg = cfg

    def _get(self, path, headers=None, timeout=3.0):
        url = self.cfg.api + path
        try:
            response = httpx.get(url, headers=headers or {}, timeout=timeout)
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}"
        return response.text, None

    def read_load(self, timeout=3.0):
        text, error = self._get(
            "/get_load", headers={"Authorization": self.cfg.auth}, timeout=timeout
        )
        if error:
            return Sample(None, None, error)
        try:
            payload = json.loads(text)
            entry = payload[0] if isinstance(payload, list) else payload
            value = {
                "num_reqs": entry.get("num_reqs"),
                "num_waiting_reqs": entry.get("num_waiting_reqs"),
            }
        except (json.JSONDecodeError, IndexError, KeyError, TypeError, AttributeError) as exc:
            return Sample(None, None, f"parse: {exc}")
        return Sample(value, time.time(), None)

    def read_server_info(self, timeout=3.0):
        text, error = self._get(
            "/get_server_info", headers={"Authorization": self.cfg.auth}, timeout=timeout
        )
        if error:
            return Sample(None, None, error)
        try:
            payload = json.loads(text)
            value = {"max_req_input_len": payload.get("max_req_input_len")}
        except (json.JSONDecodeError, AttributeError) as exc:
            return Sample(None, None, f"parse: {exc}")
        return Sample(value, time.time(), None)

    def read_metrics(self, timeout=3.0):
        text, error = self._get("/metrics", timeout=timeout)
        if error:
            return Sample(None, None, error)
        found = {}
        for line in text.splitlines():
            match = METRIC_LINE.match(line.strip())
            if not match:
                continue
            name = match.group("name")
            if name.startswith(METRIC_PREFIX):
                name = name[len(METRIC_PREFIX):]
            if name in METRIC_NAMES:
                try:
                    found[name] = float(match.group("value"))
                except ValueError:
                    continue
        for name in METRIC_NAMES:
            found.setdefault(name, None)
        return Sample(found, time.time(), None)

    def read_gpu(self, timeout=3.0):
        try:
            done = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return Sample(None, None, f"{type(exc).__name__}: {exc}")
        if done.returncode != 0:
            return Sample(None, None, f"rc={done.returncode}: {done.stderr.strip()[:80]}")
        line = done.stdout.strip().splitlines()
        if not line:
            return Sample(None, None, "leere Ausgabe")
        parts = [part.strip() for part in line[0].split(",")]
        if len(parts) != 3:
            return Sample(None, None, "unerwartetes Format")
        try:
            util, used, total = (int(float(part)) for part in parts)
        except ValueError as exc:
            return Sample(None, None, f"parse: {exc}")
        return Sample({"util": util, "used_mb": used, "total_mb": total}, time.time(), None)