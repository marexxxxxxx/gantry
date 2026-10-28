#!/usr/bin/env bash
# Flint als eigenstaendiger Ollama-Prozess auf dem SGLang-Port (30001).
#
# Damit bleibt das Gateway (Port 30000) unveraendert: es zeigt weiterhin auf
# http://127.0.0.1:30001, dort lauscht je nach Wahl SGLang (Qwen) oder dieser
# Ollama-Prozess. Zwei parallele Slots (OLLAMA_NUM_PARALLEL=2) mit je 32k
# Kontext: gemessen ~70 tok/s pro Anfrage, ~130 tok/s zusammen.
#
# Der System-Ollama (Port 11434) laeuft als anderer Benutzer und darf seinen
# Modelstore nicht beschreiben; OLLAMA_NOPRUNE=true macht ihn trotzdem
# lesbar. Vor dem Start werden geladene Modelle am System-Ollama entladen,
# damit Flint nicht mit ihnen um VRAM konkurriert.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

HOST="${FLINT_HOST:-127.0.0.1:30001}"
PIDFILE="${FLINT_PIDFILE:-$ROOT/runtime/flint/flint.pid}"
LOG="${FLINT_LOG:-/tmp/flint_server.log}"
MODELS="${FLINT_MODELS:-/mnt/models/.ollama/models}"
CTX="${FLINT_CTX:-32768}"
PARALLEL="${FLINT_PARALLEL:-2}"
KEEP_ALIVE="${FLINT_KEEP_ALIVE:--1}"
MODEL="${FLINT_MODEL:-hf.co/AtomicChat/Flint-GGUF:IQ4_XS}"
LABEL="${FLINT_LABEL:-flint}"

laeuft() {
  local pid
  [ -f "$PIDFILE" ] || return 1
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

antwortet() {
  curl -s --max-time 2 "http://$HOST/api/version" >/dev/null 2>&1
}

lade_modell() {
  # Modell vorladen: /api/ps zeigt es dann sofort (Backend-Erkennung im
  # Gateway) und der erste echte Request wartet nicht auf das Laden.
  [ -n "$MODEL" ] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  curl -s --max-time 180 "http://$HOST/api/generate" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"\",\"stream\":false,\"options\":{\"num_predict\":1},\"keep_alive\":$KEEP_ALIVE}" \
    >/dev/null 2>&1 || true
}

entlade_system_ollama() {
  # Best effort: alle am System-Dienst (11434) geladenen Modelle freigeben.
  command -v curl >/dev/null 2>&1 || return 0
  local ps
  ps="$(curl -s --max-time 2 http://127.0.0.1:11434/api/ps || true)"
  [ -n "$ps" ] || return 0
  python3 - "$ps" <<'PY' 2>/dev/null || true
import json
import sys
import urllib.request

for modell in json.loads(sys.argv[1]).get("models", []):
    daten = json.dumps({"model": modell["name"], "keep_alive": 0}).encode()
    anfrage = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate", data=daten,
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(anfrage, timeout=5).read()
    except OSError:
        pass
PY
}

if antwortet; then
  echo "[$LABEL] Auf $HOST antwortet bereits ein Server - nichts zu tun."
  exit 0
fi

if laeuft; then
  # Prozess lebt, aber /api/version noch nicht erreichbar: kurz warten.
  for _ in $(seq 1 20); do
    antwortet && exit 0
    sleep 0.5
  done
  echo "[$LABEL] Prozess laeuft, aber $HOST antwortet nicht (Log: $LOG)." >&2
  exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "[$LABEL] ollama nicht gefunden." >&2
  exit 1
fi

entlade_system_ollama
mkdir -p "$(dirname "$PIDFILE")"

OLLAMA_HOST="$HOST" \
OLLAMA_MODELS="$MODELS" \
OLLAMA_NOPRUNE=true \
OLLAMA_NUM_PARALLEL="$PARALLEL" \
OLLAMA_CONTEXT_LENGTH="$CTX" \
OLLAMA_KEEP_ALIVE="$KEEP_ALIVE" \
nohup ollama serve >>"$LOG" 2>&1 &
echo $! > "$PIDFILE"

for _ in $(seq 1 40); do
  if antwortet; then
    lade_modell
    exit 0
  fi
  laeuft || break
  sleep 0.5
done

echo "[$LABEL] Start fehlgeschlagen (Log: $LOG)." >&2
exit 1
