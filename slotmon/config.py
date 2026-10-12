"""Alle Einstellungen an einer Stelle. Sonst liest niemand os.environ."""

import os


class Config:
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)

    def __repr__(self):
        fields = ", ".join(f"{k}={v!r}" for k, v in sorted(self.__dict__.items()))
        return f"Config({fields})"


DEFAULTS = {
    "PORT": ("port", int, 8090),
    "HOST": ("host", str, "0.0.0.0"),
    "API": ("api", str, "http://127.0.0.1:30000"),
    "AUTH": ("auth", str, "Bearer marec"),
    "SERVER_LOG": ("server_log", str, "/tmp/escha_server_lan.log"),
    "MONITOR_LOG": ("monitor_log", str, "/tmp/slotmon.log"),
    "HISTORY": ("history", str, "/tmp/slotmon/history.jsonl"),
    "SYSTEMS_FILE": ("systems_file", str, "systems.json"),
    "RETENTION_H": ("retention_h", int, 24),
    "MAX_LOG_MB": ("max_log_mb", int, 1024),
    "WARN_LOG_MB": ("warn_log_mb", int, 900),
    # Eigenes Schwelle fuer das Monitor-Log, nicht die des Server-Logs.
    # Spec Z. 324: das Monitor-Log rotiert bei 5 MB. Wer darin nach einem
    # Fehler sucht, findet die Antwort sonst erst nach einem Gigabyte.
    "MAX_MONITOR_LOG_MB": ("max_monitor_log_mb", int, 5),
    "POLL_INTERVAL": ("poll_interval", float, 2.0),
    "LOG_PAIR_WINDOW_S": ("log_pair_window_s", float, 30.0),
}


def load_config(env=None):
    source = os.environ if env is None else env
    values = {}
    for env_name, (attr, cast, default) in DEFAULTS.items():
        raw = source.get(env_name)
        if raw is None or raw == "":
            values[attr] = default
        else:
            values[attr] = cast(raw)
    return Config(**values)
