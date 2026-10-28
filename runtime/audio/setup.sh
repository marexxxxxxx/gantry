#!/usr/bin/env bash
# Installiert die Audio-Abhaengigkeiten in die bestehende .venv.
#
# Idempotent: pip installiert nur, was fehlt. ffmpeg/ffprobe werden fuer
# Dekodierung und die TTS-Formate gebraucht; fehlen sie, bricht das Skript
# mit einer klaren Meldung ab, statt spaeter im Request zu scheitern.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
REQUIREMENTS="$ROOT/aiserver-monitor/requirements-audio.txt"

if [ ! -x "$PYTHON" ]; then
    echo "[audio] Python nicht gefunden: $PYTHON" >&2
    exit 1
fi
for werkzeug in ffmpeg ffprobe; do
    if ! command -v "$werkzeug" >/dev/null 2>&1; then
        echo "[audio] $werkzeug fehlt (z. B. apt install ffmpeg)" >&2
        exit 1
    fi
done
if [ ! -f "$REQUIREMENTS" ]; then
    echo "[audio] Requirements fehlen: $REQUIREMENTS" >&2
    exit 1
fi

"$PYTHON" -m pip install --disable-pip-version-check -r "$REQUIREMENTS"
"$PYTHON" - <<'PY'
import faster_whisper
import piper
import multipart

print(f"[audio] Abhaengigkeiten ok: faster-whisper {faster_whisper.__version__}, "
      f"piper {getattr(piper, '__version__', 'installiert')}")
PY
