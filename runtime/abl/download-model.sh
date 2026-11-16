#!/usr/bin/env bash
# Laedt Qwen3-14B-Abliterated (Q4_K_M, ~9 GB) in den lokalen Ollama-Modelstore.
#
# Bevorzugt den laufenden System-Ollama (11434): er schreibt als Benutzer
# "ollama" in /mnt/models und darf den Store befuellen. Ist er nicht
# erreichbar, startet das Skript einen kurzlebigen ollama-Prozess auf einem
# freien Port (nur sinnvoll, wenn ABL_MODELS beschreibbar ist). Der
# Modellserver auf 30001 bleibt in beiden Faellen unberuehrt.
#
# Nach dem Pull bekommt das lokale Manifest den Qwen3.5-Renderer/Parser.
# Damit meldet Ollama die Faehigkeiten "tools" und "thinking"; ohne diesen
# Schritt lehnt es Hermes-Anfragen mit "does not support tools" ab.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODEL="${ABL_MODEL:-hf.co/bartowski/huihui-ai_Qwen3-14B-abliterated-GGUF:Q4_K_M}"
MODELS="${ABL_MODELS:-/mnt/models/.ollama/models}"
SYSTEM_HOST="${ABL_SYSTEM_HOST:-127.0.0.1:11434}"
HOST="${ABL_DOWNLOAD_HOST:-127.0.0.1:30101}"
LOG="${ABL_DOWNLOAD_LOG:-/tmp/abl_download.log}"

if ! command -v ollama >/dev/null 2>&1; then
  echo "[abl] ollama nicht gefunden." >&2
  exit 1
fi

renderer_aktivieren() {
  local host="$1" modelfile
  modelfile="$(mktemp)"
  {
    echo "FROM $MODEL"
    echo "RENDERER qwen3.5"
    echo "PARSER qwen3.5"
  } >"$modelfile"
  OLLAMA_HOST="$host" ollama create "$MODEL" -f "$modelfile"
  rm -f "$modelfile"
  echo "[abl] Tool-/Thinking-Renderer aktiviert: $MODEL"
}

if curl -s --max-time 2 "http://$SYSTEM_HOST/api/version" >/dev/null 2>&1; then
  OLLAMA_HOST="$SYSTEM_HOST" ollama pull "$MODEL"
  renderer_aktivieren "$SYSTEM_HOST"
  echo "[abl] Modell bereit: $MODEL"
  exit 0
fi

mkdir -p "$MODELS"

OLLAMA_HOST="$HOST" \
OLLAMA_MODELS="$MODELS" \
OLLAMA_NOPRUNE=true \
nohup ollama serve >>"$LOG" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  curl -s --max-time 2 "http://$HOST/api/version" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -s --max-time 2 "http://$HOST/api/version" >/dev/null 2>&1; then
  echo "[abl] Download-Server startet nicht (Log: $LOG)." >&2
  exit 1
fi

OLLAMA_HOST="$HOST" ollama pull "$MODEL"
renderer_aktivieren "$HOST"
echo "[abl] Modell bereit: $MODEL"
