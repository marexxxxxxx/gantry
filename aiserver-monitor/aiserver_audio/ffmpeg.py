"""ffprobe/ffmpeg-Helfer ohne Shell."""

import math
import subprocess
from pathlib import Path


def probe_duration(path: Path, *, runner=subprocess.run) -> float | None:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        ergebnis = runner(cmd, capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if ergebnis.returncode != 0:
        return None
    zeile = (ergebnis.stdout or "").strip().splitlines()
    if not zeile:
        return None
    try:
        dauer = float(zeile[0])
    except ValueError:
        return None
    if not math.isfinite(dauer) or dauer < 0:
        return None
    return dauer
