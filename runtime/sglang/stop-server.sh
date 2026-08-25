#!/usr/bin/env bash
# Stop the running escha sglang server (whatever started it) and free the GPU.
# Usage: bash stop-server.sh
set -u

pat='[s]glang.launch_server'   # bracket keeps pgrep from matching THIS script
PIDS=$(pgrep -f "$pat" || true)
if [[ -z "$PIDS" ]]; then
  echo "[stop] no sglang server is running."
  exit 0
fi

echo "[stop] stopping: $PIDS"
kill -TERM $PIDS 2>/dev/null || true

for i in $(seq 1 20); do
  PIDS=$(pgrep -f "$pat" || true)
  [[ -z "$PIDS" ]] && break
  sleep 1
done

PIDS=$(pgrep -f "$pat" || true)
if [[ -n "$PIDS" ]]; then
  echo "[stop] graceful TERM did not finish in 20s — sending KILL to: $PIDS"
  kill -KILL $PIDS 2>/dev/null || true
  sleep 2
fi

if pgrep -f "$pat" >/dev/null; then
  echo "[stop] ERROR: processes still alive:" >&2
  pgrep -af "$pat" >&2
  exit 1
fi

USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')
echo "[stop] server stopped. GPU memory used: ${USED:-?} MiB"
