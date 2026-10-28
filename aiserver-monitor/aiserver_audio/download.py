"""Modelldownload fuer Whisper und Piper.

Der Download laeuft bewusst nur ueber dieses CLI und nie automatisch aus
einer Anfrage: Modelle sind mehrere Gigabyte gross, und ein Request darf
nicht minutenlang auf einen Hugging-Face-Download warten. Das Manifest
steht in :mod:`aiserver_audio.catalog`; hier wird es nur ausgefuehrt.
"""

import argparse
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage
from typing import Callable

from huggingface_hub import snapshot_download

from .catalog import (
    PIPER_VOICES,
    WHISPER_MODELS,
    PiperInfo,
    WhisperInfo,
    piper_files,
    resolve_voice,
    resolve_whisper,
    voice_installed,
    whisper_dir,
    whisper_installed,
)
from .config import load_settings
from .errors import AudioError

#: Reserve, die vor einem Download frei sein muss. Die Summe aller Modelle
#: liegt bei rund 3,8 GB; 5 GB lassen Luft fuer temporaere Piper-Dateien.
MIN_FREE_BYTES = 5 * 1024 ** 3


@dataclass(frozen=True)
class Selection:
    """Auswahl als IDs oder Aliase; leer bedeutet "alle"."""

    whisper: tuple[str, ...] = ()
    voices: tuple[str, ...] = ()


def _auswahl(roh, modelle, aufloesen) -> list:
    """Loest Namen auf, entfernt Dubletten und behaelt die Reihenfolge."""
    if not roh:
        return list(modelle)
    gewaehlt: list = []
    gesehen: set[str] = set()
    for name in roh:
        info = aufloesen(name)
        if info.id not in gesehen:
            gesehen.add(info.id)
            gewaehlt.append(info)
    return gewaehlt


def _gruppen(selection: Selection):
    """Leere Auswahl = alles; sonst zaehlt jede nichtleere Gruppe."""
    alle = not selection.whisper and not selection.voices
    whisper = (_auswahl(selection.whisper, WHISPER_MODELS,
                        resolve_whisper)
               if alle or selection.whisper else [])
    voices = (_auswahl(selection.voices, PIPER_VOICES,
                       resolve_voice)
              if alle or selection.voices else [])
    return whisper, voices


def _meldung(text: str) -> None:
    print(text, file=sys.stderr)


def _freier_platz(models_dir: Path):
    """Indirektion, damit Tests den Standard bequem ersetzen koennen."""
    return disk_usage(models_dir)


def _check(whisper, voices, models_dir: Path) -> int:
    fehlt = False
    for info in whisper:
        da = whisper_installed(models_dir, info)
        fehlt = fehlt or not da
        print(f"{info.id}: {'installed' if da else 'missing'} "
              f"({info.size_mb} MB)")
    for info in voices:
        da = voice_installed(models_dir, info)
        fehlt = fehlt or not da
        print(f"{info.id}: {'installed' if da else 'missing'} "
              f"({info.size_mb} MB)")
    return 1 if fehlt else 0


def _whisper_laden(info: WhisperInfo, models_dir: Path,
                   snapshot: Callable) -> bool:
    ziel = whisper_dir(models_dir, info)
    print(f"Lade {info.id} ({info.size_mb} MB) ...")
    try:
        snapshot(info.repo, local_dir=str(ziel),
                 allow_patterns=["*"])
    except Exception as exc:
        _meldung(f"Download von {info.id} fehlgeschlagen: {exc}")
        return False
    if not whisper_installed(models_dir, info):
        _meldung(f"{info.id}: Download unvollstaendig, Datei fehlt")
        return False
    return True


def _stimme_laden(info: PiperInfo, models_dir: Path,
                  snapshot: Callable) -> bool:
    onnx, cfg = piper_files(models_dir, info)
    print(f"Lade Stimme {info.id} ({info.size_mb} MB) ...")
    try:
        with tempfile.TemporaryDirectory(prefix="piper-download-") as tmp:
            snapshot(info.repo, local_dir=tmp,
                     allow_patterns=[f"{info.repo_path}/*"])
            quelle = Path(tmp) / info.repo_path
            ziele = (
                (quelle / f"{info.id}.onnx", onnx),
                (quelle / f"{info.id}.onnx.json", cfg),
            )
            for src, dst in ziele:
                if not src.is_file() or src.stat().st_size == 0:
                    _meldung(f"{info.id}: Datei {src.name} fehlt im "
                             f"Download")
                    return False
            onnx.parent.mkdir(parents=True, exist_ok=True)
            for src, dst in ziele:
                shutil.copy2(src, dst)
    except Exception as exc:
        _meldung(f"Download von {info.id} fehlgeschlagen: {exc}")
        return False
    if not voice_installed(models_dir, info):
        _meldung(f"{info.id}: Download unvollstaendig, Datei fehlt")
        return False
    return True


def download(selection: Selection, models_dir,
             *, check: bool = False,
             snapshot: Callable | None = None,
             disk_usage: Callable | None = None) -> int:
    """Laedt die Auswahl oder prueft nur den Status.

    Rueckgabe ist ein Exit-Code: 0 erfolgreich/vollstaendig, 1 Fehler
    oder unvollstaendig, 2 unbekannte Modell-ID (nur CLI).
    """
    models_dir = Path(models_dir)
    whisper, voices = _gruppen(selection)
    if check:
        return _check(whisper, voices, models_dir)

    snapshot = snapshot or snapshot_download

    fehlend_whisper = [i for i in whisper
                       if not whisper_installed(models_dir, i)]
    fehlend_voices = [i for i in voices
                      if not voice_installed(models_dir, i)]
    if not fehlend_whisper and not fehlend_voices:
        print("Alle ausgewaehlten Modelle sind bereits installiert.")
        return 0

    models_dir.mkdir(parents=True, exist_ok=True)
    frei = (disk_usage or _freier_platz)(models_dir).free
    if frei < MIN_FREE_BYTES:
        _meldung(f"Zu wenig Speicherplatz: {frei / 1024 ** 3:.1f} GB frei, "
                 f"mindestens 5 GB noetig.")
        return 1

    for info in fehlend_whisper:
        if not _whisper_laden(info, models_dir, snapshot):
            return 1
    for info in fehlend_voices:
        if not _stimme_laden(info, models_dir, snapshot):
            return 1
    print("Fertig.")
    return 0


def _ids(text: str) -> tuple[str, ...]:
    return tuple(teil.strip() for teil in text.split(",") if teil.strip())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aiserver_audio.download",
        description="Whisper-Modelle und Piper-Stimmen herunterladen.")
    parser.add_argument("--all", action="store_true",
                        help="alle Modelle und Stimmen (Standard)")
    parser.add_argument("--whisper", metavar="IDS",
                        help="kommagetrennt, z. B. small,medium")
    parser.add_argument("--voices", metavar="IDS",
                        help="kommagetrennt, z. B. thorsten,eva")
    parser.add_argument("--check", action="store_true",
                        help="nur Installationsstatus pruefen")
    args = parser.parse_args(argv)

    if args.all:
        auswahl = Selection()
    elif args.whisper is not None or args.voices is not None:
        auswahl = Selection(whisper=_ids(args.whisper or ""),
                            voices=_ids(args.voices or ""))
    else:
        auswahl = Selection()

    settings = load_settings()
    try:
        return download(auswahl, settings.models_dir, check=args.check)
    except AudioError as exc:
        _meldung(str(exc))
        _meldung("Nutzung: python -m aiserver_audio.download "
                 "[--all|--whisper IDS|--voices IDS] [--check]")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
