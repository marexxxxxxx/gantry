#!/usr/bin/env bash
# Laedt die Chatterbox-V3-Gewichte nach /mnt/models/audio/chatterbox.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MODELS="${CHATTERBOX_MODELS_DIR:-/mnt/models/audio/chatterbox}"
DATEIEN=(
  ve.pt
  s3gen.pt
  t3_mtl23ls_v3.safetensors
  conds.pt
  grapheme_mtl_merged_expanded_v1.json
  Cangjie5_TC.json
)

fehlt=0
for datei in "${DATEIEN[@]}"; do
  [ -s "$MODELS/$datei" ] || fehlt=1
done
if [ "$fehlt" -eq 0 ]; then
  echo "Chatterbox-Modelle sind bereits vorhanden: $MODELS"
  exit 0
fi

command -v hf >/dev/null 2>&1 || {
  echo "hf CLI fehlt. Bitte zuerst installieren." >&2
  exit 1
}
mkdir -p "$MODELS"
args=()
for datei in "${DATEIEN[@]}"; do args+=(--include "$datei"); done
hf download ResembleAI/chatterbox --local-dir "$MODELS" "${args[@]}"

for datei in "${DATEIEN[@]}"; do
  [ -s "$MODELS/$datei" ] || {
    echo "Download unvollstaendig: $datei fehlt" >&2
    exit 1
  }
done
echo "Chatterbox-Modelle liegen in $MODELS"
