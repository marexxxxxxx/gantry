#!/usr/bin/env bash
# Stoppt den Audio-Sidecar aus start-audio.sh.
#
# Nur das PID-File zaehlt. Ein fremder Prozess auf Port 30003 wird nie
# angefasst; ohne PID-File ist hier nichts zu tun.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIDFILE="${AUDIO_PIDFILE:-$ROOT/runtime/audio/audio.pid}"

pid=""
if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
fi

if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PIDFILE"
    exit 0
fi

kill "$pid" 2>/dev/null || true
for _ in $(seq 1 40); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
done
if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
fi

rm -f "$PIDFILE"
exit 0
