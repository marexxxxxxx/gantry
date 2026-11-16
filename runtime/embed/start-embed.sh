#!/usr/bin/env bash
# Embedding-Modell als eigener Ollama-Prozess auf der CPU (127.0.0.1:30002).
#
# Das Gateway (Port 30000) routet /v1/embeddings hierher; Clients sprechen
# weiterhin nur mit dem Gateway. Der Prozess laeuft unabhaengig vom
# Chat-Backend (Qwen/Flint/GPT-OSS) und belegt kein VRAM:
# CUDA_VISIBLE_DEVICES/HIP_VISIBLE_DEVICES sind leer, Ollama faellt damit auf
# die CPU zurueck.
#
# Der System-Ollama (Port 11434) laeuft als anderer Benutzer; OLLAMA_NOPRUNE
# macht seinen Modelstore hier lesbar. Das Modell bleibt mit keep_alive=-1
# geladen, damit der erste Request nicht auf das Laden wartet.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

HOST="${EMBED_HOST:-127.0.0.1:30002}"
PIDFILE="${EMBED_PIDFILE:-$ROOT/runtime/embed/embed.pid}"
LOG="${EMBED_LOG:-/tmp/embed_server.log}"
MODELS="${EMBED_MODELS:-/mnt/models/.ollama/models}"
CTX="${EMBED_CTX:-8192}"
PARALLEL="${EMBED_PARALLEL:-1}"
KEEP_ALIVE="${EMBED_KEEP_ALIVE:--1}"
MODEL="${EMBED_MODEL:-qwen3-embedding:0.6b}"
LABEL="${EMBED_LABEL:-embed}"

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

warte_auf_modelle() {
  # /mnt/models ist ein eigenes Dateisystem. Der User-Dienst kann beim Boot
  # vor dem System-Mount starten; dann waere der Ollama-Modelstore leer.
  local mountpoint="${EMBED_MODELS_MOUNT:-/mnt/models}"
  local i ziel
  grep -qs "[[:space:]]${mountpoint}[[:space:]]" /etc/fstab || return 0
  for i in $(seq 1 120); do
    ziel="$(findmnt -rn -o TARGET -T "$MODELS" 2>/dev/null || true)"
    [ -n "$ziel" ] && [ "$ziel" != "/" ] && return 0
    sleep 1
  done
  echo "[$LABEL] $mountpoint ist nach 120 s nicht gemountet." >&2
  return 1
}

lade_modell() {
  # Ein winziger Embedding-Aufruf laedt das Modell und beweist, dass der
  # CPU-Runner wirklich arbeitet. /api/ps zeigt es danach sofort geladen.
  [ -n "$MODEL" ] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  curl -s --max-time 180 "http://$HOST/api/embed" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"input\":\"ping\",\"keep_alive\":$KEEP_ALIVE}" \
    >/dev/null 2>&1 || true
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

warte_auf_modelle

mkdir -p "$(dirname "$PIDFILE")"

# Leere *_VISIBLE_DEVICES sind der harte Ausschluss der GPU. Ollama 0.40
# nutzt zusaetzlich Vulkan, das CUDA_VISIBLE_DEVICES ignoriert; OLLAMA_VULKAN=0
# und das leere GGML_VK_VISIBLE_DEVICES schalten auch diesen Backend ab.
# OLLAMA_NUM_GPU=0 ist zusaetzlich gesetzt, falls eine Ollama-Version die
# Variablen anders auswertet; der Server-Log bestaetigt "library=cpu".
CUDA_VISIBLE_DEVICES="" \
HIP_VISIBLE_DEVICES="" \
OLLAMA_VULKAN=0 \
GGML_VK_VISIBLE_DEVICES="" \
OLLAMA_NUM_GPU=0 \
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
