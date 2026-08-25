#!/usr/bin/env bash
# Start the escha server with the OVERTHINKING PENALTY turned ON.
#
# Usage:
#   bash start-penalty.sh [LAMBDA]
#
#   LAMBDA  penalty strength, default 2.0 (paper sweep 0.5-4.0; higher = harder cut).
#
# Examples:
#   bash runtime/sglang/start-penalty.sh          # server with penalty, lambda=2.0
#   bash runtime/sglang/start-penalty.sh 3.5      # stronger penalty
#   PENALTY_ALWAYS=1 bash runtime/sglang/start-penalty.sh 2.0   # also outside <think>
#
# The server starts IN THE BACKGROUND, logs to /tmp/escha_server.log and this script
# waits until it answers on http://127.0.0.1:$PORT. Stop it again with:
#   bash runtime/sglang/stop-server.sh
set -euo pipefail

LAMBDA=${1:-2.0}
case "$LAMBDA" in ''|*[!0-9.]*|[!0-9.]*) echo "[start] ERROR: LAMBDA must be a number (got '$LAMBDA')" >&2; exit 1 ;; esac

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
export MODEL=${MODEL:-$REPO/Qwen3.8-27B-Escha-W2}
export PORT=${PORT:-30000}
export HOST=${HOST:-127.0.0.1}
export VENV=${VENV:-$REPO/.venv}   # serve.sh aborts silently without a python that has torch
# Measured 16 GB (RTX 5070 Ti) profile. serve.sh's own defaults target 24 GB cards
# (MEM=0.72 CTXLEN=65536): on 16 GB they shrink the recurrent pool until
# get_batch_sizes_to_capture() asserts capture_bs=[0] and startup dies.
export MEM=${MEM:-0.90}
export CTXLEN=${CTXLEN:-16384}
export CUDA_GRAPH_BS=${CUDA_GRAPH_BS:-"1 2 4 8"}
export OTP=1 PENALTY_LAMBDA="$LAMBDA"
export LOG=${LOG:-/tmp/escha_server.log}

[[ -d "$MODEL" ]] || { echo "[start] ERROR: model dir not found: $MODEL" >&2; exit 1; }
[[ -f "$VENV/bin/activate" ]] || { echo "[start] ERROR: venv not found: $VENV (see INSTALL.md)" >&2; exit 1; }

# Refuse to double-start: an existing server would just make the port bind fail confusingly.
if PIDS=$(pgrep -f "[s]glang.launch_server"); then
  echo "[start] ERROR: a server is already running:" >&2
  echo "$PIDS" >&2
  echo "[start]        stop it first: bash $HERE/stop-server.sh" >&2
  exit 1
fi

echo "[start] launching: model=$MODEL  lambda=$LAMBDA  http://$HOST:$PORT  (log: $LOG)"
nohup bash "$HERE/serve.sh" >"$LOG" 2>&1 &
SRV_PID=$!
echo "[start] server PID $SRV_PID — loading weights (~2-4 min)..."

for i in $(seq 1 120); do
  if ! kill -0 "$SRV_PID" 2>/dev/null; then
    echo "[start] FAILED — server exited during startup. Last log lines:" >&2
    tail -n 25 "$LOG" >&2
    exit 1
  fi
  if curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then
    echo "[start] READY: overthinking penalty ACTIVE (lambda=$LAMBDA)"
    echo "[start]   chat:      curl http://$HOST:$PORT/v1/chat/completions ..."
    echo "[start]   examples:  python $HERE/examples/penalty_example.py"
    echo "[start]   stop:      bash $HERE/stop-server.sh"
    exit 0
  fi
  sleep 3
done

echo "[start] TIMEOUT after 6 min — still not healthy. Check: tail -f $LOG" >&2
exit 1
