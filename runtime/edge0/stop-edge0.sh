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

# Nachfahren einsammeln, bevor der Elternprozess stirbt: die strata-Engine
# setzt sich per setpgid in eine eigene Prozessgruppe und ueberlebt deshalb
# ein kill auf die Gruppe von edge0-infer (dann bleiben ~15 GB VRAM belegt).
nachfahren() {
  local queue=("$1") aktuell kind
  while [ ${#queue[@]} -gt 0 ]; do
    aktuell="${queue[0]}"
    queue=("${queue[@]:1}")
    for kind in $(cat "/proc/$aktuell/task/$aktuell/children" 2>/dev/null); do
      printf '%s\n' "$kind"
      queue+=("$kind")
    done
  done
}

kinder="$(nachfahren "$pid" || true)"

# Negative PID = ganze Prozessgruppe (edge0-infer + strata).
kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
for kind in $kinder; do
  kill "$kind" 2>/dev/null || true
done
for _ in $(seq 1 60); do
  lebt=0
  kill -0 "$pid" 2>/dev/null && lebt=1
  for kind in $kinder; do
    kill -0 "$kind" 2>/dev/null && lebt=1
  done
  [ "$lebt" -eq 0 ] && break
  sleep 0.5
done
for kind in $pid $kinder; do
  kill -0 "$kind" 2>/dev/null && kill -9 "$kind" 2>/dev/null || true
done

rm -f "$PIDFILE"
exit 0
