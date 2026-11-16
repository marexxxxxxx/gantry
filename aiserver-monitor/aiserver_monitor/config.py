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
    # Embeddings laufen auf der CPU in einem eigenen Ollama-Prozess. Der
    # Port ist nur lokal gebunden; Clients sehen weiterhin nur listen_port.
    embed_base: str = "http://127.0.0.1:30002"
    embed_model: str = "qwen3-embedding:0.6b"
    # Lifecycle (/v1/load, /v1/unload): der Embedding-Sidecar darf lokal
    # gestartet werden; Timeouts und Poll-Takt gelten fuer alle Backends.
    embed_start_script: str = "runtime/embed/start-embed.sh"
    lifecycle_timeout_s: float = 900.0
    lifecycle_unload_timeout_s: float = 60.0
    lifecycle_poll_s: float = 0.5
    # Qwen3-TTS laeuft nur auf der GPU, nachdem der Chat entladen wurde.
    # Der Worker wird bei /v1/load gestartet und bei /v1/unload gestoppt.
    qwen_tts_model: str = "Qwen3-TTS"
    qwen_tts_base: str = "http://127.0.0.1:30005"
    qwen_tts_model_dir: str = "/mnt/models/audio/experiments/qwen"
    qwen_tts_start_script: str = "runtime/qwen-tts/start-qwen-tts.sh"
    qwen_tts_stop_script: str = "runtime/qwen-tts/stop-qwen-tts.sh"
    qwen_tts_pidfile: str = "runtime/qwen-tts/qwen-tts.pid"
    qwen_tts_health_path: str = "/healthz"
    qwen_tts_load_timeout_s: float = 900.0
    qwen_tts_unload_timeout_s: float = 60.0
    # Audio8-TTS ist das zweite GPU-TTS. Es wird wie Qwen3-TTS erst nach dem
    # Entladen des Chat-Backends gestartet und ueber /v1/load verwaltet.
    audio8_model: str = "Audio8-Realistic-Air"
    audio8_base: str = "http://127.0.0.1:30006"
    audio8_model_dir: str = "/mnt/models/audio/audio8"
    audio8_reference_audio: str = (
        "/mnt/models/audio/audio8/reference/de_male.wav")
    audio8_reference_text: str = (
        "/mnt/models/audio/audio8/reference/de_male.txt")
    audio8_start_script: str = "runtime/audio8/start-audio8.sh"
    audio8_stop_script: str = "runtime/audio8/stop-audio8.sh"
    audio8_pidfile: str = "runtime/audio8/audio8.pid"
    audio8_health_path: str = "/healthz"
    audio8_load_timeout_s: float = 900.0
    audio8_unload_timeout_s: float = 60.0
    # Audio (Whisper/Piper) laeuft in einem eigenen CPU-Prozess. Auch dieser
    # Port ist nur lokal gebunden; Clients sehen weiterhin listen_port.
    audio_base: str = "http://127.0.0.1:30003"
    audio_timeout_s: float = 1800.0
    audio_max_upload_mb: int = 100
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
    abort_on_disconnect: bool = True
    # Server-Steuerung (Dashboard-Tab "Server" und server.sh). Pfade sind
    # relativ zur Repo-Wurzel; die Unit wird nur benutzt, wenn installiert.
    server_control_enabled: bool = True
    server_unit: str = "escha-server.service"
    server_start_script: str = "runtime/sglang/start-penalty.sh"
    server_stop_script: str = "runtime/sglang/stop-server.sh"
    server_process_pattern: str = "[s]glang.launch_server"
    # Flint-Backend: eigener Ollama-Prozess auf demselben Upstream-Port.
    flint_start_script: str = "runtime/flint/start-flint.sh"
    flint_stop_script: str = "runtime/flint/stop-flint.sh"
    flint_pidfile: str = "runtime/flint/flint.pid"
    flint_health_path: str = "/api/version"
    # GPT-OSS-Backend: ebenfalls eigener Ollama-Prozess, 64k Kontext.
    gptoss_start_script: str = "runtime/gptoss/start-gptoss.sh"
    gptoss_stop_script: str = "runtime/gptoss/stop-gptoss.sh"
    gptoss_pidfile: str = "runtime/gptoss/gptoss.pid"
    # Abliterated-Backend: Qwen3-14B ohne Refusal-Verhalten, 16k Kontext.
    abl_start_script: str = "runtime/abl/start-abl.sh"
    abl_stop_script: str = "runtime/abl/stop-abl.sh"
    abl_pidfile: str = "runtime/abl/abl.pid"
    # Edge0-Backend: eigener Prozess (edge0-infer + edge0-strata), kein Ollama.
    # Braucht die ganze GPU; der Health-Pfad ist /health wie bei SGLang.
    edge0_start_script: str = "runtime/edge0/start-edge0.sh"
    edge0_stop_script: str = "runtime/edge0/stop-edge0.sh"
    edge0_pidfile: str = "runtime/edge0/edge0.pid"
    edge0_health_path: str = "/health"
    # Modellname, den Edge0 meldet (--alias in start-edge0.sh).
    edge0_model: str = "edge0-120b"
    # Modell-Alias: Clients (z. B. Hindsight und Hermes) senden weiterhin den
    # Qwen-Namen. Laeuft Flint, GPT-OSS, Abliterated oder Edge0, schreibt das
    # Gateway ihn auf das jeweilige Modell um; laeuft Qwen, bleibt er
    # unveraendert (dort ist er der echte Modellname).
    model_alias: str = "escha-qwen38-27b-w2"
    flint_model: str = "hf.co/AtomicChat/Flint-GGUF:IQ4_XS"
    gptoss_model: str = "gpt-oss:20b"
    abl_model: str = (
        "hf.co/bartowski/huihui-ai_Qwen3-14B-abliterated-GGUF:Q4_K_M")

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
        if self.audio_timeout_s <= 0:
            raise ValueError(
                f"audio_timeout_s muss positiv sein: {self.audio_timeout_s}")
        if self.audio_max_upload_mb <= 0:
            raise ValueError(
                "audio_max_upload_mb muss positiv sein: "
                f"{self.audio_max_upload_mb}")
        if self.lifecycle_timeout_s <= 0:
            raise ValueError(
                "lifecycle_timeout_s muss positiv sein: "
                f"{self.lifecycle_timeout_s}")
        if self.lifecycle_unload_timeout_s <= 0:
            raise ValueError(
                "lifecycle_unload_timeout_s muss positiv sein: "
                f"{self.lifecycle_unload_timeout_s}")
        if self.lifecycle_poll_s <= 0:
            raise ValueError(
                "lifecycle_poll_s muss positiv sein: "
                f"{self.lifecycle_poll_s}")
        for feld in ("qwen_tts_load_timeout_s",
                     "qwen_tts_unload_timeout_s"):
            if getattr(self, feld) <= 0:
                raise ValueError(
                    f"{feld} muss positiv sein: {getattr(self, feld)}")
        for feld in ("qwen_tts_base", "qwen_tts_model_dir",
                     "qwen_tts_start_script", "qwen_tts_stop_script",
                     "qwen_tts_pidfile", "qwen_tts_health_path"):
            if not str(getattr(self, feld)).strip():
                raise ValueError(f"{feld} darf nicht leer sein")
        for feld in ("audio8_load_timeout_s", "audio8_unload_timeout_s"):
            if getattr(self, feld) <= 0:
                raise ValueError(
                    f"{feld} muss positiv sein: {getattr(self, feld)}")
        for feld in ("audio8_base", "audio8_model_dir",
                     "audio8_reference_audio", "audio8_reference_text",
                     "audio8_start_script", "audio8_stop_script",
                     "audio8_pidfile", "audio8_health_path"):
            if not str(getattr(self, feld)).strip():
                raise ValueError(f"{feld} darf nicht leer sein")


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
