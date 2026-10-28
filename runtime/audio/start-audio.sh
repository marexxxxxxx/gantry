#!/usr/bin/env bash
# Startet den Audio-Sidecar (Whisper-STT + Piper-TTS) auf der CPU.
#
# Der Sidecar hoert standardmaessig auf 127.0.0.1:30003. Clients sprechen
# weiterhin nur mit dem Gateway auf 30000; dort routet /v1/audio/* hierher.
# Idempotent: laeuft bereits ein gesunder Prozess, passiert nichts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

HOST="${AUDIO_HOST:-127.0.0.1}"
PORT="${AUDIO_PORT:-30003}"
PIDFILE="${AUDIO_PIDFILE:-$ROOT/runtime/audio/audio.pid}"
LOG="${AUDIO_LOG:-/tmp/audio_server.log}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
LABEL="${AUDIO_LABEL:-audio}"

laeuft() {
    local pid
    [ -f "$PIDFILE" ] || return 1
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

antwortet() {
    curl -sf --max-time 2 "http://$HOST:$PORT/healthz" >/dev/null 2>&1
}

if antwortet; then
    echo "[$LABEL] Auf $HOST:$PORT antwortet bereits ein Sidecar - nichts zu tun."
    exit 0
fi

if laeuft; then
    for _ in $(seq 1 20); do
        antwortet && exit 0
        sleep 0.5
    done
    echo "[$LABEL] Prozess laeuft, aber $HOST:$PORT antwortet nicht (Log: $LOG)." >&2
    exit 1
fi

if [ ! -x "$PYTHON" ]; then
    echo "[$LABEL] Python nicht gefunden: $PYTHON" >&2
    exit 1
fi

mkdir -p "$(dirname "$PIDFILE")" "$(dirname "$LOG")"

# nice 5: Embeddings und Chat bleiben vordringlich, Audio ist Hintergrundarbeit.
# CUDA_VISIBLE_DEVICES bleibt leer: der Sidecar rechnet ausschliesslich auf der
# CPU und darf kein VRAM belegen.
CUDA_VISIBLE_DEVICES="" \
OMP_NUM_THREADS="${AUDIO_THREADS:-8}" \
PYTHONPATH="$ROOT/aiserver-monitor${PYTHONPATH:+:$PYTHONPATH}" \
nohup nice -n 5 "$PYTHON" -B -m aiserver_audio >>"$LOG" 2>&1 &
echo $! > "$PIDFILE"

for _ in $(seq 1 40); do
    if antwortet; then
        exit 0
    fi
    if ! laeuft; then
        break
    fi
    sleep 0.5
done

echo "[$LABEL] Start fehlgeschlagen (Log: $LOG)." >&2
tail -n 20 "$LOG" >&2 || true
exit 1
