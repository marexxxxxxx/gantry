"""Whitelist der Audio-Modelle und Installationsstatus."""

from dataclasses import dataclass
from pathlib import Path

from .errors import AudioError


@dataclass(frozen=True)
class WhisperInfo:
    id: str
    aliases: tuple[str, ...]
    repo: str
    size_mb: int
    dirname: str
    required_files: tuple[str, ...] = (
        "model.bin", "config.json", "tokenizer.json")


@dataclass(frozen=True)
class PiperInfo:
    id: str
    aliases: tuple[str, ...]
    repo: str
    repo_path: str
    size_mb: int
    sample_rate: int


@dataclass(frozen=True)
class ChatterboxInfo:
    id: str
    aliases: tuple[str, ...]
    size_mb: int
    dirname: str
    required_files: tuple[str, ...] = (
        "ve.pt", "s3gen.pt", "t3_mtl23ls_v3.safetensors",
        "grapheme_mtl_merged_expanded_v1.json", "Cangjie5_TC.json")


WHISPER_MODELS: tuple[WhisperInfo, ...] = (
    WhisperInfo("whisper-small", ("small", "whisper-1"),
                "Systran/faster-whisper-small", 484, "small"),
    WhisperInfo("whisper-medium", ("medium",),
                "Systran/faster-whisper-medium", 1528, "medium"),
    WhisperInfo("whisper-large-v3-turbo",
                ("turbo", "large-v3-turbo"),
                "deepdml/faster-whisper-large-v3-turbo-ct2", 1618,
                "large-v3-turbo"),
)

PIPER_VOICES: tuple[PiperInfo, ...] = (
    PiperInfo("de_DE-thorsten-medium", ("thorsten",),
              "rhasspy/piper-voices", "de/de_DE/thorsten/medium",
              63, 22050),
    PiperInfo("de_DE-kerstin-low", ("kerstin",),
              "rhasspy/piper-voices", "de/de_DE/kerstin/low",
              63, 22050),
    PiperInfo("de_DE-eva_k-x_low", ("eva",),
              "rhasspy/piper-voices", "de/de_DE/eva_k/x_low",
              21, 16000),
)

DEFAULT_WHISPER = "whisper-small"
DEFAULT_VOICE = "de_DE-thorsten-medium"

CHATTERBOX_MODEL = ChatterboxInfo(
    "chatterbox", ("chatterbox-v3",), 3000, "chatterbox")


def _aufloesen(name: str | None, modelle, default: str, art: str):
    gesucht = (name or default).strip().casefold()
    for info in modelle:
        if gesucht == info.id.casefold() \
                or gesucht in (a.casefold() for a in info.aliases):
            return info
    raise AudioError(404, "model_not_found",
                     f"Unbekanntes {art}: {name!r}")


def resolve_whisper(name: str | None) -> WhisperInfo:
    return _aufloesen(name, WHISPER_MODELS, DEFAULT_WHISPER,
                      "Whisper-Modell")


def resolve_voice(name: str | None) -> PiperInfo:
    return _aufloesen(name, PIPER_VOICES, DEFAULT_VOICE, "Stimme")


def whisper_dir(models_dir: Path, info: WhisperInfo) -> Path:
    return Path(models_dir) / "whisper" / info.dirname


def piper_files(models_dir: Path, info: PiperInfo) -> tuple[Path, Path]:
    basis = Path(models_dir) / "piper" / info.id
    return basis.with_suffix(".onnx"), basis.with_suffix(".onnx.json")


def chatterbox_dir(models_dir: Path) -> Path:
    return Path(models_dir) / CHATTERBOX_MODEL.dirname


def chatterbox_installed(models_dir: Path) -> bool:
    ziel = chatterbox_dir(models_dir)
    return all(_datei_ok(ziel / name)
               for name in CHATTERBOX_MODEL.required_files)


def _datei_ok(pfad: Path) -> bool:
    try:
        return pfad.is_file() and pfad.stat().st_size > 0
    except OSError:
        return False


def whisper_installed(models_dir: Path, info: WhisperInfo) -> bool:
    ziel = whisper_dir(models_dir, info)
    return all(_datei_ok(ziel / name) for name in info.required_files)


def voice_installed(models_dir: Path, info: PiperInfo) -> bool:
    return all(_datei_ok(pfad) for pfad in piper_files(models_dir, info))


def models_payload(models_dir: Path) -> dict:
    return {
        "whisper": {
            "default": DEFAULT_WHISPER,
            "models": [
                {"id": info.id,
                 "installed": whisper_installed(models_dir, info),
                 "size_mb": info.size_mb}
                for info in WHISPER_MODELS
            ],
        },
        "tts": {
            "default_model": "piper",
            "default_voice": DEFAULT_VOICE,
            "models": [
                {"id": "piper", "installed": True, "size_mb": 0},
                {"id": "chatterbox",
                 "installed": chatterbox_installed(models_dir),
                 "size_mb": CHATTERBOX_MODEL.size_mb},
            ],
            "voices": [
                {"id": info.id,
                 "installed": voice_installed(models_dir, info),
                 "size_mb": info.size_mb}
                for info in PIPER_VOICES
            ],
        },
    }
