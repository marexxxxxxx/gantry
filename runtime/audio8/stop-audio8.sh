#!/usr/bin/env bash
# Stoppt den Audio8-TTS-GPU-Worker (nur ueber sein PID-File).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIDFILE="${AUDIO8_PIDFILE:-$ROOT/runtime/audio8/audio8.pid}"

if [[ ! -f "$PIDFILE" ]]; then
    echo "[audio8] laeuft nicht"
    exit 0
fi
PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PIDFILE"
    echo "[audio8] laeuft nicht"
    exit 0
fi

kill -TERM "$PID" 2>/dev/null || true
for _ in $(seq 1 40); do
    if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$PIDFILE"
        echo "[audio8] gestoppt"
        exit 0
    fi
    sleep 0.5
done
kill -KILL "$PID" 2>/dev/null || true
rm -f "$PIDFILE"
echo "[audio8] gestoppt (KILL)"
