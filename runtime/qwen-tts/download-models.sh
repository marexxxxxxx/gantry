#!/usr/bin/env bash
# Prueft oder laedt die Qwen3-TTS-1.7B-CustomVoice-Gewichte.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MODEL_DIR="${QWEN_TTS_MODEL_DIR:-/mnt/models/audio/experiments/qwen}"
DATEIEN=(
  config.json
  model.safetensors
  generation_config.json
  speech_tokenizer/config.json
  speech_tokenizer/model.safetensors
)

fehlt=0
for datei in "${DATEIEN[@]}"; do
  [ -s "$MODEL_DIR/$datei" ] || fehlt=1
done
if [ "$fehlt" -eq 0 ]; then
  echo "Qwen3-TTS-Modelle sind bereits vorhanden: $MODEL_DIR"
  exit 0
fi

command -v hf >/dev/null 2>&1 || {
  echo "hf CLI fehlt. Bitte zuerst installieren." >&2
  exit 1
}
mkdir -p "$MODEL_DIR"
hf download Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice \
  --local-dir "$MODEL_DIR"

for datei in "${DATEIEN[@]}"; do
  [ -s "$MODEL_DIR/$datei" ] || {
    echo "Download unvollstaendig: $datei fehlt" >&2
    exit 1
  }
done
echo "Qwen3-TTS-Modelle liegen in $MODEL_DIR"
