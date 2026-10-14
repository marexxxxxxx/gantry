"""Konfiguration fuer aiserver-monitor.

Reihenfolge der Quellen: Defaults < TOML-Datei < Umgebungsvariablen.
"""

import dataclasses
import os
import tomllib
from dataclasses import dataclass
from typing import Any, Mapping

ENV_PREFIX = "AISERVER_MONITOR_"

_TRUE_WERTE = frozenset(("1", "true", "yes", "on"))
_FALSE_WERTE = frozenset(("0", "false", "no", "off"))


@dataclass
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 30000
    upstream_base: str = "http://127.0.0.1:30001"
    db_path: str = "data/monitor.db"
    log_path: str = "data/aiserver-monitor.log"
    heartbeat_s: float = 15.0
    poll_interval_s: float = 2.0
    preview_max_chars: int = 4096
    prompt_start_chars: int = 100
    retention_jobs_days: int = 30
    retention_series_days: int = 7
    nonstream_keepalive: bool = True
    gpu_cmd: str = "nvidia-smi"
    connect_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        """Weist nicht-positive Intervalle ab (sie waeren Busy-Loops).

        ``heartbeat_s`` taktet die Keepalive- und Nicht-Stream-Schleife,
        ``poll_interval_s`` die Sammelschleife; 0 oder negativ wuerde beide
        ohne Pause drehen. Deshalb ValueError statt stiller Uebernahme.
        """
        if self.heartbeat_s <= 0:
            raise ValueError(
                f"heartbeat_s muss positiv sein: {self.heartbeat_s}")
        if self.poll_interval_s <= 0:
            raise ValueError(
                f"poll_interval_s muss positiv sein: {self.poll_interval_s}")


def _coerce(feld: str, typ: Any, wert: Any) -> Any:
    """Wandelt einen Rohwert (env-String oder TOML-Wert) in den Feldtyp um."""
    try:
        if typ is bool:
            if isinstance(wert, bool):
                return wert
            text = str(wert).strip().lower()
            if text in _TRUE_WERTE:
                return True
            if text in _FALSE_WERTE:
                return False
            raise ValueError
        if typ is int:
            if isinstance(wert, bool):
                raise ValueError
            return int(wert)
        if typ is float:
            if isinstance(wert, bool):
                raise ValueError
            return float(wert)
        if typ is str:
            if not isinstance(wert, str):
                raise ValueError
            return wert
        return wert
    except (TypeError, ValueError):
        raise ValueError(f"Ungueltiger Wert fuer {feld}: {wert!r}") from None


def load_config(path: str | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Laedt Config aus optionaler TOML-Datei plus env-Overrides.

    path=None laesst die Datei aus (reine Defaults plus env). env=None liest
    os.environ; env={} unterdrueckt die Umgebung (fuer Tests). Praefix der
    Variablen: AISERVER_MONITOR_ plus Feldname in Grossbuchstaben, z. B.
    AISERVER_MONITOR_LISTEN_PORT=39999. Leere Werte gelten als nicht gesetzt.
    """
    quelle = os.environ if env is None else env
    rohwerte: dict[str, Any] = {}
    if path is not None:
        with open(path, "rb") as fh:
            rohwerte.update(tomllib.load(fh))

    felder = dataclasses.fields(Config)
    for feld in felder:
        wert = quelle.get(ENV_PREFIX + feld.name.upper())
        if wert is not None and wert != "":
            rohwerte[feld.name] = wert

    werte = {
        feld.name: _coerce(feld.name, feld.type, rohwerte[feld.name])
        if feld.name in rohwerte
        else feld.default
        for feld in felder
    }
    return Config(**werte)
