"""Provider registry for Gantry.

Each provider is one TOML file under ``providers/``.  The registry is the
single source of truth for what Gantry can start: serverctl, the gateway and
the dashboard all read the same files.  Editing a provider is therefore just
editing (or writing) a TOML file -- no code change, no restart.

File layout::

    providers/<id>.toml

Required keys: ``name``.  Everything else has a safe default.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only for older Pythons
    import tomli as tomllib  # type: ignore[no-redef]

PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROVIDER_KINDS = ("strata", "ollama", "sglang", "custom")

SETTING_DEFAULTS: dict[str, Any] = {
    "context": 0,
    "max_tokens": 0,
    "kv": "",
    "keep_alive": 0,
    "parallel": 1,
    "auto_load": True,
    "idle_unload_min": 0,
    "exclusive_gpu": False,
    "vram_reserve_mib": 0,
}


class RegistryError(ValueError):
    """A provider file is missing, malformed or conflicts with another one."""


@dataclass
class Provider:
    """One local AI provider, as stored in ``providers/<id>.toml``."""

    id: str
    name: str
    kind: str = "custom"
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 0
    model: str = ""
    health_url: str = ""
    pidfile: str = ""
    logfile: str = ""
    workdir: str = "."
    start_cmd: list[str] = field(default_factory=list)
    stop_cmd: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    source: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.id = str(self.id).strip().lower()
        if not PROVIDER_ID_RE.match(self.id):
            raise RegistryError(
                f"invalid provider id {self.id!r}: use lowercase letters, digits, '-' or '_'"
            )
        if not str(self.name).strip():
            raise RegistryError(f"provider {self.id!r}: 'name' must not be empty")
        if self.kind not in PROVIDER_KINDS:
            raise RegistryError(
                f"provider {self.id!r}: unknown kind {self.kind!r} "
                f"(expected one of {', '.join(PROVIDER_KINDS)})"
            )
        try:
            self.port = int(self.port or 0)
        except (TypeError, ValueError) as exc:
            raise RegistryError(f"provider {self.id!r}: port must be an integer") from exc
        if not 0 <= self.port <= 65535:
            raise RegistryError(f"provider {self.id!r}: port out of range")
        for key in ("start_cmd", "stop_cmd"):
            value = getattr(self, key)
            if value is None:
                value = []
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise RegistryError(f"provider {self.id!r}: {key} must be a list of strings")
            setattr(self, key, value)

        merged = dict(SETTING_DEFAULTS)
        merged.update(self.settings or {})
        for key, value in merged.items():
            if key not in SETTING_DEFAULTS:
                continue
            default = SETTING_DEFAULTS[key]
            if isinstance(default, bool) and not isinstance(value, bool):
                raise RegistryError(f"provider {self.id!r}: setting {key!r} must be a boolean")
            if isinstance(default, int) and not isinstance(default, bool):
                try:
                    merged[key] = int(value)
                except (TypeError, ValueError) as exc:
                    raise RegistryError(
                        f"provider {self.id!r}: setting {key!r} must be an integer"
                    ) from exc
        self.settings = merged

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "model": self.model,
            "health_url": self.health_url,
            "pidfile": self.pidfile,
            "logfile": self.logfile,
            "workdir": self.workdir,
            "start_cmd": list(self.start_cmd),
            "stop_cmd": list(self.stop_cmd),
            "settings": dict(self.settings),
        }

    def public_dict(self) -> dict[str, Any]:
        """The shape the dashboard/API exposes (no secrets, no source path)."""
        return self.to_dict()

    @property
    def context(self) -> int:
        return int(self.settings.get("context") or 0)

    @property
    def max_tokens(self) -> int:
        return int(self.settings.get("max_tokens") or 0)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise RegistryError(f"cannot serialise {type(value).__name__} to TOML")


def dumps_provider(provider: Provider) -> str:
    """Render a provider as a readable TOML document."""
    lines: list[str] = [
        "# Gantry provider. Edit with `gantry provider edit <id>` or the dashboard.",
        f"id = {_toml_value(provider.id)}",
        f"name = {_toml_value(provider.name)}",
        f"kind = {_toml_value(provider.kind)}",
        f"enabled = {_toml_value(provider.enabled)}",
        f"host = {_toml_value(provider.host)}",
        f"port = {_toml_value(provider.port)}",
        f"model = {_toml_value(provider.model)}",
        f"health_url = {_toml_value(provider.health_url)}",
        f"pidfile = {_toml_value(provider.pidfile)}",
        f"logfile = {_toml_value(provider.logfile)}",
        f"workdir = {_toml_value(provider.workdir)}",
        f"start_cmd = {_toml_value(list(provider.start_cmd))}",
        f"stop_cmd = {_toml_value(list(provider.stop_cmd))}",
        "",
        "[settings]",
    ]
    for key, value in provider.settings.items():
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def default_registry_dir() -> Path:
    """``<repo>/providers``, overridable with ``GANTRY_PROVIDERS_DIR``."""
    env = os.environ.get("GANTRY_PROVIDERS_DIR")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "providers"


def _from_mapping(raw: dict[str, Any], path: Path) -> Provider:
    data = dict(raw)
    provider_id = str(data.pop("id", path.stem)).strip().lower()
    settings = data.pop("settings", {}) or {}
    if not isinstance(settings, dict):
        raise RegistryError(f"{path}: [settings] must be a table")
    known = {
        "name", "kind", "enabled", "host", "port", "model", "health_url",
        "pidfile", "logfile", "workdir", "start_cmd", "stop_cmd",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        raise RegistryError(f"{path}: unknown key(s): {', '.join(unknown)}")
    return Provider(
        id=provider_id,
        name=str(data.pop("name", provider_id)),
        settings=settings,
        source=path,
        **data,
    )


def load_provider(path: Path) -> Provider:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"provider file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{path}: invalid TOML: {exc}") from exc
    return _from_mapping(raw, path)


def load_registry(directory: Path | None = None) -> dict[str, Provider]:
    directory = Path(directory) if directory is not None else default_registry_dir()
    providers: dict[str, Provider] = {}
    if not directory.exists():
        return providers
    for path in sorted(directory.glob("*.toml")):
        provider = load_provider(path)
        if provider.id in providers:
            raise RegistryError(
                f"duplicate provider id {provider.id!r}: "
                f"{providers[provider.id].source} and {path}"
            )
        if path.stem != provider.id:
            raise RegistryError(
                f"{path}: file name must match id {provider.id!r} ({provider.id}.toml)"
            )
        providers[provider.id] = provider
    return providers


def save_provider(provider: Provider, directory: Path | None = None) -> Path:
    directory = Path(directory) if directory is not None else default_registry_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{provider.id}.toml"
    payload = dumps_provider(provider)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{provider.id}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    provider.source = path
    return path


def delete_provider(provider_id: str, directory: Path | None = None) -> bool:
    directory = Path(directory) if directory is not None else default_registry_dir()
    path = directory / f"{provider_id}.toml"
    if not path.exists():
        return False
    path.unlink()
    return True


def rename_provider(old_id: str, new_id: str, directory: Path | None = None) -> Provider:
    directory = Path(directory) if directory is not None else default_registry_dir()
    providers = load_registry(directory)
    if old_id not in providers:
        raise RegistryError(f"provider not found: {old_id}")
    if new_id in providers:
        raise RegistryError(f"provider already exists: {new_id}")
    provider = providers[old_id]
    provider.id = new_id
    if not provider.name or provider.name == old_id:
        provider.name = new_id
    path = save_provider(provider, directory)
    old_path = directory / f"{old_id}.toml"
    if old_path != path:
        old_path.unlink(missing_ok=True)
    return provider
