#!/usr/bin/env bash
# Prueft oder laedt die Audio8-TTS-Realistic-Air-Gewichte und die Referenzstimme.
set -euo pipefail

MODEL_DIR="${AUDIO8_MODEL_DIR:-/mnt/models/audio/audio8}"
REF_AUDIO="${AUDIO8_REFERENCE_AUDIO:-/mnt/models/audio/audio8/reference/de_male.wav}"
REF_TEXT="${AUDIO8_REFERENCE_TEXT:-/mnt/models/audio/audio8/reference/de_male.txt}"
DATEIEN=(
  config.json
  model.safetensors
  generation_config.json
  codec.pth
  tokenizer.json
  preprocessor_config.json
  configuration_arktts.py
  modeling_arktts.py
  modeling_arktts_codec.py
  processing_arktts.py
)

fehlende() {
  local datei fehlt=0
  for datei in "${DATEIEN[@]}"; do
    if [ ! -s "$MODEL_DIR/$datei" ]; then
      echo "missing: $datei"
      fehlt=1
    fi
  done
  return "$fehlt"
}

if [ "${1:-}" = "--check" ]; then
  if fehlende; then
    echo "Audio8-TTS-Modelle sind vollstaendig: $MODEL_DIR"
    exit 0
  fi
  exit 1
fi
if [ "$#" -gt 0 ]; then
  echo "usage: $0 [--check]" >&2
  exit 2
fi

if fehlende; then
  :
else
  command -v hf >/dev/null 2>&1 || {
    echo "hf CLI fehlt. Bitte zuerst installieren." >&2
    exit 1
  }
  mkdir -p "$MODEL_DIR"
  hf download Edge0/Audio8-TTS-Realistic-Air --local-dir "$MODEL_DIR"
  if ! fehlende; then
    echo "Download unvollstaendig." >&2
    exit 1
  fi
fi

if [ ! -s "$REF_AUDIO" ] || [ ! -s "$REF_TEXT" ]; then
  AUDIO8_REFERENCE_AUDIO="$REF_AUDIO" \
  AUDIO8_REFERENCE_TEXT="$REF_TEXT" \
    bash "$(dirname "$0")/make-reference.sh"
fi

echo "Audio8-TTS-Modelle liegen in $MODEL_DIR"
echo "Referenzstimme: $REF_AUDIO"
echo "Referenztext:   $REF_TEXT"
