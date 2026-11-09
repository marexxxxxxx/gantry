#!/usr/bin/env bash
# Startet den Audio8-TTS-GPU-Worker (Port 30006) idempotent.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOST="${AUDIO8_HOST:-127.0.0.1}"
PORT="${AUDIO8_PORT:-30006}"
PIDFILE="${AUDIO8_PIDFILE:-$ROOT/runtime/audio8/audio8.pid}"
LOG="${AUDIO8_LOG:-/tmp/audio8_tts_server.log}"
PYTHON="${PYTHON:-$ROOT/runtime/audio8/venv/bin/python}"
MODEL_DIR="${AUDIO8_MODEL_DIR:-/mnt/models/audio/audio8}"
REFERENCE_AUDIO="${AUDIO8_REFERENCE_AUDIO:-$MODEL_DIR/reference/de_male.wav}"
REFERENCE_TEXT="${AUDIO8_REFERENCE_TEXT:-$MODEL_DIR/reference/de_male.txt}"

if [ ! -x "$PYTHON" ]; then
    echo "[audio8] Python nicht gefunden: $PYTHON - bitte zuerst setup.sh ausfuehren." >&2
    exit 1
fi

laeuft() {
    [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

antwortet() {
    curl -sf --max-time 2 "http://$HOST:$PORT/healthz" >/dev/null 2>&1
}

if antwortet; then
    echo "[audio8] laeuft bereits auf http://$HOST:$PORT"
    exit 0
fi

if laeuft; then
    for _ in $(seq 1 20); do
        antwortet && exit 0
        sleep 0.5
    done
    echo "[audio8] Prozess laeuft, antwortet aber nicht auf Port $PORT" >&2
    exit 1
fi

mkdir -p "$(dirname "$PIDFILE")" "$(dirname "$LOG")"
CUDA_VISIBLE_DEVICES="${AUDIO8_CUDA_VISIBLE_DEVICES:-0}" \
AUDIO8_HOST="$HOST" \
AUDIO8_PORT="$PORT" \
AUDIO8_MODEL_DIR="$MODEL_DIR" \
AUDIO8_REFERENCE_AUDIO="$REFERENCE_AUDIO" \
AUDIO8_REFERENCE_TEXT="$REFERENCE_TEXT" \
PYTHONPATH="$ROOT/aiserver-monitor${PYTHONPATH:+:$PYTHONPATH}" \
nohup nice -n 5 "$PYTHON" -B -m aiserver_audio8_tts >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"

for _ in $(seq 1 40); do
    if antwortet; then
        echo "[audio8] laeuft auf http://$HOST:$PORT (PID $(cat "$PIDFILE"))"
        exit 0
    fi
    sleep 0.5
done

echo "[audio8] Start fehlgeschlagen; letzte Logzeilen:" >&2
tail -n 20 "$LOG" >&2 || true
exit 1
