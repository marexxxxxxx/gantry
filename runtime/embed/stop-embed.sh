#!/usr/bin/env bash
# Stoppt den CPU-Embedding-Ollama-Prozess aus start-embed.sh.
#
# Nur das PID-File zaehlt: der System-Ollama (11434) und die Chat-Backends
# haben dieselbe Kommandozeile und duerfen hier nicht mit abgeschossen werden.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIDFILE="${EMBED_PIDFILE:-$ROOT/runtime/embed/embed.pid}"

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
