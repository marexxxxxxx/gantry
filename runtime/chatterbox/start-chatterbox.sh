#!/usr/bin/env bash
# Startet den Chatterbox-CPU-Worker (Port 30004) idempotent.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOST="${CHATTERBOX_HOST:-127.0.0.1}"
PORT="${CHATTERBOX_PORT:-30004}"
PIDFILE="${CHATTERBOX_PIDFILE:-$ROOT/runtime/chatterbox/chatterbox.pid}"
LOG="${CHATTERBOX_LOG:-/tmp/chatterbox_server.log}"
PYTHON="${PYTHON:-$ROOT/runtime/chatterbox/venv/bin/python}"

if [ ! -x "$PYTHON" ]; then
    echo "[chatterbox] Python nicht gefunden: $PYTHON - bitte zuerst setup.sh ausfuehren." >&2
    exit 1
fi

laeuft() {
    [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

antwortet() {
    curl -sf --max-time 2 "http://$HOST:$PORT/healthz" >/dev/null 2>&1
}

if antwortet; then
    echo "[chatterbox] laeuft bereits auf http://$HOST:$PORT"
    exit 0
fi

if laeuft; then
    for _ in $(seq 1 20); do
        antwortet && exit 0
        sleep 0.5
    done
    echo "[chatterbox] Prozess laeuft, antwortet aber nicht auf Port $PORT" >&2
    exit 1
fi

mkdir -p "$(dirname "$PIDFILE")" "$(dirname "$LOG")"
CUDA_VISIBLE_DEVICES="" \
OMP_NUM_THREADS="${CHATTERBOX_THREADS:-8}" \
PYTHONPATH="$ROOT/aiserver-monitor${PYTHONPATH:+:$PYTHONPATH}" \
nohup nice -n 5 "$PYTHON" -B -m aiserver_chatterbox >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"

for _ in $(seq 1 40); do
    if antwortet; then
        echo "[chatterbox] laeuft auf http://$HOST:$PORT (PID $(cat "$PIDFILE"))"
        exit 0
    fi
    sleep 0.5
done

echo "[chatterbox] Start fehlgeschlagen; letzte Logzeilen:" >&2
tail -n 20 "$LOG" >&2 || true
exit 1
