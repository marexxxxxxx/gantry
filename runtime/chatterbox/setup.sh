#!/usr/bin/env bash
# Legt die isolierte Chatterbox-Umgebung an. Piper/Whisper bleiben unberuehrt.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="${CHATTERBOX_VENV:-$ROOT/runtime/chatterbox/venv}"
REQ="$ROOT/runtime/chatterbox/requirements.txt"
PYTHON="${PYTHON:-python3}"

if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install torch==2.6.0+cpu torchaudio==2.6.0+cpu \
  --index-url https://download.pytorch.org/whl/cpu
"$VENV/bin/python" -m pip install -r "$REQ"
"$VENV/bin/python" - <<'PY'
import chatterbox
import torch
print(f"chatterbox ok, torch {torch.__version__}")
PY
