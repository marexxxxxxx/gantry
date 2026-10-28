#!/usr/bin/env bash
# Laedt Whisper-Modelle und Piper-Stimmen nach AUDIO_MODELS_DIR.
#
# Beispiele:
#   runtime/audio/download-models.sh --all
#   runtime/audio/download-models.sh --whisper small,medium
#   runtime/audio/download-models.sh --voices thorsten,kerstin
#   runtime/audio/download-models.sh --check
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

cd "$ROOT/aiserver-monitor"
exec "$PYTHON" -B -m aiserver_audio.download "$@"
