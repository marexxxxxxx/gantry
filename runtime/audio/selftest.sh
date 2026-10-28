#!/usr/bin/env bash
# Prueft den laufenden Audio-Sidecar Ende-zu-Ende: Piper erzeugt einen
# deutschen Satz als WAV, Whisper transkribiert ihn wieder.
#
# Fehlen die Modelle, endet der Test mit Exit 0 und einem Hinweis - das ist
# kein Defekt des Sidecars. Ein echter Fehler (Server down, falscher Text)
# liefert Exit 1.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE="${AISERVER_MONITOR_AUDIO_BASE:-http://127.0.0.1:30003}"
MODELS="${AUDIO_MODELS_DIR:-/mnt/models/audio}"
WHISPER="${AUDIO_SELFTEST_MODEL:-small}"
TEXT="${AUDIO_SELFTEST_TEXT:-Hallo, wie geht es dir?}"

if [ ! -s "$MODELS/whisper/$WHISPER/model.bin" ]; then
    echo "[audio] Whisper-Modell '$WHISPER' fehlt unter $MODELS/whisper/$WHISPER."
    echo "[audio] Zuerst: runtime/audio/download-models.sh --all"
    exit 0
fi
if [ ! -s "$MODELS/piper/de_DE-thorsten-medium.onnx" ]; then
    echo "[audio] Piper-Stimme 'thorsten' fehlt unter $MODELS/piper."
    echo "[audio] Zuerst: runtime/audio/download-models.sh --all"
    exit 0
fi
for werkzeug in curl ffprobe; do
    command -v "$werkzeug" >/dev/null 2>&1 || {
        echo "[audio] $werkzeug fehlt" >&2
        exit 1
    }
done

TMPDIR=$(mktemp -d /tmp/audio-selftest.XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT
WAV="$TMPDIR/satz.wav"

echo "[audio] TTS: $TEXT"
tts_start=$(date +%s%N)
curl -sf --max-time 120 "$BASE/v1/audio/speech" \
    -H 'Content-Type: application/json' \
    -d "{\"input\":\"$TEXT\",\"voice\":\"thorsten\",\"response_format\":\"wav\"}" \
    -o "$WAV"
tts_ms=$(( ($(date +%s%N) - tts_start) / 1000000 ))

dauer=$(ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$WAV" 2>/dev/null || true)
if [ -z "$dauer" ]; then
    echo "[audio] WAV ist nicht lesbar: $WAV" >&2
    exit 1
fi
echo "[audio] TTS ok: ${tts_ms} ms fuer ${dauer}s Audio"

echo "[audio] STT: Whisper $WHISPER"
stt_start=$(date +%s%N)
antwort=$(curl -sf --max-time 600 "$BASE/v1/audio/transcriptions" \
    -F "file=@$WAV;type=audio/wav" \
    -F "model=$WHISPER" \
    -F "language=de" \
    -F "response_format=text")
stt_ms=$(( ($(date +%s%N) - stt_start) / 1000000 ))

echo "[audio] Transkript: $antwort"
case "${antwort,,}" in
    *hallo*)
        rtf=$(awk -v ms="$stt_ms" -v d="$dauer" 'BEGIN { printf "%.2f", (ms/1000)/d }')
        echo "[audio] STT ok: ${stt_ms} ms, RTF ${rtf}"
        ;;
    *)
        echo "[audio] STT lieferte nicht den erwarteten Text ('Hallo')" >&2
        exit 1
        ;;
esac
