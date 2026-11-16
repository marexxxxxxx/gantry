#!/usr/bin/env bash
# Stoppt Edge0 aus start-edge0.sh und gibt VRAM frei.
#
# edge0-infer startet die strata-Engine als Kindprozess. Beide laufen in einer
# eigenen Prozessgruppe (setsid), deshalb wird die ganze Gruppe beendet - sonst
# bliebe strata mit ~15 GB VRAM zurueck.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIDFILE="${EDGE0_PIDFILE:-$ROOT/runtime/edge0/edge0.pid}"

pid=""
if [ -f "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
fi

if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PIDFILE"
  exit 0
fi

# Negative PID = ganze Prozessgruppe (edge0-infer + strata).
kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
for _ in $(seq 1 60); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.5
done
if kill -0 "$pid" 2>/dev/null; then
  kill -9 -- "-$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
fi

rm -f "$PIDFILE"
exit 0
