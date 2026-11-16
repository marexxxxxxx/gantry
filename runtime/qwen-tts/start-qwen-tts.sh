#!/usr/bin/env bash
# Startet den Qwen3-TTS-GPU-Worker (Port 30005) idempotent.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOST="${QWEN_TTS_HOST:-127.0.0.1}"
PORT="${QWEN_TTS_PORT:-30005}"
PIDFILE="${QWEN_TTS_PIDFILE:-$ROOT/runtime/qwen-tts/qwen-tts.pid}"
LOG="${QWEN_TTS_LOG:-/tmp/qwen_tts_server.log}"
PYTHON="${PYTHON:-$ROOT/runtime/qwen-tts/venv/bin/python}"

if [ ! -x "$PYTHON" ]; then
    echo "[qwen-tts] Python nicht gefunden: $PYTHON - bitte zuerst setup.sh ausfuehren." >&2
    exit 1
fi

laeuft() {
    [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

antwortet() {
    curl -sf --max-time 2 "http://$HOST:$PORT/healthz" >/dev/null 2>&1
}

if antwortet; then
    echo "[qwen-tts] laeuft bereits auf http://$HOST:$PORT"
    exit 0
fi

if laeuft; then
    for _ in $(seq 1 20); do
        antwortet && exit 0
        sleep 0.5
    done
    echo "[qwen-tts] Prozess laeuft, antwortet aber nicht auf Port $PORT" >&2
    exit 1
fi

mkdir -p "$(dirname "$PIDFILE")" "$(dirname "$LOG")"
CUDA_VISIBLE_DEVICES="${QWEN_TTS_CUDA_VISIBLE_DEVICES:-0}" \
QWEN_TTS_HOST="$HOST" \
QWEN_TTS_PORT="$PORT" \
QWEN_TTS_MODEL_DIR="${QWEN_TTS_MODEL_DIR:-/mnt/models/audio/experiments/qwen}" \
PYTHONPATH="$ROOT/aiserver-monitor${PYTHONPATH:+:$PYTHONPATH}" \
nohup nice -n 5 "$PYTHON" -B -m aiserver_qwen_tts >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"

for _ in $(seq 1 40); do
    if antwortet; then
        echo "[qwen-tts] laeuft auf http://$HOST:$PORT (PID $(cat "$PIDFILE"))"
        exit 0
    fi
    sleep 0.5
done

echo "[qwen-tts] Start fehlgeschlagen; letzte Logzeilen:" >&2
tail -n 20 "$LOG" >&2 || true
exit 1
