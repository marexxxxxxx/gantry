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


@dataclass(frozen=True)
class QwenTTSInfo:
    id: str
    aliases: tuple[str, ...]
    size_mb: int
    dirname: str
    required_files: tuple[str, ...] = (
        "config.json", "model.safetensors", "generation_config.json",
        "speech_tokenizer/config.json",
        "speech_tokenizer/model.safetensors")
    speakers: tuple[str, ...] = (
        "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
        "Ryan", "Aiden", "Ono_Anna", "Sohee")


@dataclass(frozen=True)
class Audio8Info:
    id: str
    aliases: tuple[str, ...]
    size_mb: int
    dirname: str
    required_files: tuple[str, ...]
    voices: tuple[str, ...] = ("de_male",)


@dataclass(frozen=True)
class ZonosInfo:
    id: str
    aliases: tuple[str, ...]
    size_mb: int
    required_files: tuple[str, ...]
    voices: tuple[str, ...] = ("liesel",)


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

QWEN_TTS_MODEL = QwenTTSInfo(
    "Qwen3-TTS",
    ("qwen3-tts", "qwen3-tts-1.7b", "qwen3-tts-12hz-1.7b-customvoice"),
    4300,
    "experiments/qwen",
)

AUDIO8_MODEL = Audio8Info(
    "Audio8-Realistic-Air",
    ("audio8", "audio8-realistic-air", "audio8-tts-realistic-air",
     "audio8-tts"),
    1600,
    "audio8",
    ("config.json", "model.safetensors", "generation_config.json",
     "codec.pth", "tokenizer.json", "preprocessor_config.json",
     "configuration_arktts.py", "modeling_arktts.py",
     "modeling_arktts_codec.py", "processing_arktts.py"),
)

ZONOS_MODEL = ZonosInfo(
    "ZONOS3-Mini",
    ("zonos3-mini", "zonos3", "zonos", "zonos-tts"),
    1160,
    ("backbone-ptq.gguf", "zac-keep2.gguf", "spk-encoder.gguf",
     "steer.gguf"),
    ("liesel",),
)

DEFAULT_ZONOS_VOICE = "liesel"


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


def qwen_tts_dir(models_dir: Path) -> Path:
    return Path(models_dir) / QWEN_TTS_MODEL.dirname


def qwen_tts_installed(models_dir: Path) -> bool:
    ziel = qwen_tts_dir(models_dir)
    return all(_datei_ok(ziel / name)
               for name in QWEN_TTS_MODEL.required_files)


def resolve_qwen_voice(name: str | None) -> str:
    """Loest die optionale Stimme auf; Piper-Defaults werden zu Vivian."""
    gesucht = (name or "").strip().casefold()
    if gesucht in ("", "default", DEFAULT_VOICE.casefold()):
        return "Vivian"
    for stimme in QWEN_TTS_MODEL.speakers:
        if gesucht == stimme.casefold():
            return stimme
    raise AudioError(400, "invalid_request",
                     f"Unbekannte Qwen3-TTS-Stimme: {name!r}")


def audio8_dir(model_dir: Path) -> Path:
    """Audio8 liegt direkt im konfigurierten Verzeichnis."""
    return Path(model_dir)


def audio8_installed(model_dir: Path) -> bool:
    ziel = audio8_dir(model_dir)
    return all(_datei_ok(ziel / name)
               for name in AUDIO8_MODEL.required_files)


def resolve_audio8_voice(name: str | None) -> str:
    """Audio8 hat genau eine feste Referenzstimme."""
    gesucht = (name or "").strip().casefold()
    if gesucht in ("", "default", "audio8", DEFAULT_VOICE.casefold(),
                   *AUDIO8_MODEL.voices):
        return "de_male"
    raise AudioError(400, "invalid_request",
                     f"Unbekannte Audio8-Stimme: {name!r}")


def zonos_installed(model_dir: Path) -> bool:
    """ZONOS liegt im konfigurierten GGUF-Verzeichnis."""
    ziel = Path(model_dir)
    return all(_datei_ok(ziel / name)
               for name in ZONOS_MODEL.required_files)


def resolve_zonos_voice(name: str | None) -> str:
    """Loest die ZONOS-Stimme auf; es gibt aktuell nur Liesel."""
    gesucht = (name or "").strip().casefold()
    if gesucht in ("", "default", "zonos", "zonos3", "liesel22",
                   "liesel-22", "liesel_22", "happy22",
                   *ZONOS_MODEL.voices):
        return DEFAULT_ZONOS_VOICE
    raise AudioError(400, "invalid_request",
                     f"Unbekannte ZONOS3-Stimme: {name!r}")


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


def models_payload(models_dir: Path, *,
                   audio8_model_dir: Path | None = None,
                   zonos_model_dir: Path | None = None) -> dict:
    audio8_ziel = (Path(audio8_model_dir) if audio8_model_dir is not None
                   else Path(models_dir) / AUDIO8_MODEL.dirname)
    zonos_ziel = (Path(zonos_model_dir) if zonos_model_dir is not None
                  else Path(models_dir) / "zonos3" / "gguf")
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
                {"id": QWEN_TTS_MODEL.id,
                 "installed": qwen_tts_installed(models_dir),
                 "size_mb": QWEN_TTS_MODEL.size_mb},
                {"id": AUDIO8_MODEL.id,
                 "installed": audio8_installed(audio8_ziel),
                 "size_mb": AUDIO8_MODEL.size_mb},
                {"id": ZONOS_MODEL.id,
                 "installed": zonos_installed(zonos_ziel),
                 "size_mb": ZONOS_MODEL.size_mb},
            ],
            "voices": [
                {"id": info.id,
                 "installed": voice_installed(models_dir, info),
                 "size_mb": info.size_mb}
                for info in PIPER_VOICES
            ],
            "qwen3_tts": {
                "voices": [
                    {"id": stimme, "installed": True, "size_mb": 0}
                    for stimme in QWEN_TTS_MODEL.speakers
                ],
            },
            "audio8": {
                "voices": [
                    {"id": stimme, "installed": True, "size_mb": 0}
                    for stimme in AUDIO8_MODEL.voices
                ],
            },
            "zonos3": {
                "voices": [
                    {"id": stimme, "installed": True, "size_mb": 0}
                    for stimme in ZONOS_MODEL.voices
                ],
            },
        },
    }
