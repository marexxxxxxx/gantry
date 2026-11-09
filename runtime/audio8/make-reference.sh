#!/usr/bin/env bash
# Erzeugt eine feste maennliche Referenzstimme mit Piper (de_DE-thorsten).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
REF_AUDIO="${AUDIO8_REFERENCE_AUDIO:-/mnt/models/audio/audio8/reference/de_male.wav}"
REF_TEXT="${AUDIO8_REFERENCE_TEXT:-/mnt/models/audio/audio8/reference/de_male.txt}"
VOICE="${AUDIO8_REFERENCE_VOICE:-de_DE-thorsten-medium}"
MODELS_DIR="${AUDIO_MODELS_DIR:-/mnt/models/audio}"
SATZ="Guten Tag. Ich spreche heute über Technik, Sprache und die kleinen Dinge des Alltags."
FORCE=0
if [ "${1:-}" = "--force" ]; then
  FORCE=1
elif [ "$#" -gt 0 ]; then
  echo "usage: $0 [--force]" >&2
  exit 2
fi

if [ "$FORCE" -eq 0 ] \
    && [ -s "$REF_AUDIO" ] && [ -s "$REF_TEXT" ]; then
  echo "Referenzstimme ist bereits vorhanden: $REF_AUDIO"
  echo "Referenztext: $REF_TEXT"
  exit 0
fi

ONNX="$MODELS_DIR/piper/$VOICE.onnx"
CFG="$ONNX.json"
if [ ! -s "$ONNX" ] || [ ! -s "$CFG" ]; then
  AUDIO_MODELS_DIR="$MODELS_DIR" \
    bash "$REPO/runtime/audio/download-models.sh" --voices thorsten
fi

mkdir -p "$(dirname "$REF_AUDIO")" "$(dirname "$REF_TEXT")"
"${PYTHON:-$REPO/.venv/bin/python}" - "$ONNX" "$REF_AUDIO" "$SATZ" <<'PY'
import sys
import wave

from piper.voice import PiperVoice, SynthesisConfig

onnx, ziel, satz = sys.argv[1], sys.argv[2], sys.argv[3]
stimme = PiperVoice.load(onnx)
teile = list(stimme.synthesize(satz, SynthesisConfig()))
if not teile:
    raise SystemExit("Piper hat kein Audio erzeugt")
rate = teile[0].sample_rate
with wave.open(ziel, "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(rate)
    for teil in teile:
        wav.writeframes(teil.audio_int16_bytes)
PY

printf '%s\n' "$SATZ" >"$REF_TEXT"
echo "Referenzstimme geschrieben: $REF_AUDIO"
echo "Referenztext geschrieben:   $REF_TEXT"
