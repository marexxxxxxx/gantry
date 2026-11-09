#!/usr/bin/env bash
# Legt die isolierte Audio8-TTS-Umgebung mit CUDA-Torch an.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="${AUDIO8_VENV:-$ROOT/runtime/audio8/venv}"
REQ="$ROOT/runtime/audio8/requirements.txt"
PYTHON="${PYTHON:-python3.12}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3

if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r "$REQ"
"$VENV/bin/python" -m pip install --upgrade --force-reinstall \
  torch torchaudio --index-url https://download.pytorch.org/whl/cu128
"$VENV/bin/python" - <<'PY'
import torch
import soundfile
import transformers
print(f"audio8-tts ok, torch {torch.__version__}, "
      f"transformers {transformers.__version__}")
assert torch.cuda.is_available(), "CUDA ist nicht verfuegbar"
PY
