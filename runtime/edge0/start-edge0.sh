#!/usr/bin/env bash
# Edge0-120B (NVFP4, edge0-strata) als Backend auf dem gemeinsamen Port (30001).
#
# Edge0 bringt seine eigene Engine mit (edge0-strata, kein SGLang, kein Ollama).
# Das Gateway (Port 30000) bleibt unveraendert: es zeigt weiterhin auf
# http://127.0.0.1:30001, dort lauscht je nach Wahl SGLang (Qwen), Ollama
# (Flint/GPT-OSS/Abliterated) oder dieser edge0-infer-Prozess.
#
# Der Modellname ist der Alias aus EDGE0_ALIAS (Default "edge0-120b"); das
# Gateway schreibt sowohl diesen Namen als auch den Hermes-Alias
# (escha-qwen38-27b-w2) darauf um.
#
# VRAM: Edge0 belegt die ganze Karte (~15 GB). Vor dem Start werden deshalb
# die am System-Ollama (11434) geladenen Modelle entladen - sonst schlaegt der
# Start fehl, weil kein VRAM frei ist.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

HOST="${EDGE0_HOST:-127.0.0.1:30001}"
PIDFILE="${EDGE0_PIDFILE:-$ROOT/runtime/edge0/edge0.pid}"
LOG="${EDGE0_LOG:-/tmp/edge0_server.log}"
MODEL_DIR="${EDGE0_MODEL_DIR:-/mnt/models/Edge0-120B-NVFP4}"
EDGE0_ROOT="${EDGE0_ROOT:-/home/user/edge0}"
BIN="${EDGE0_BIN:-$EDGE0_ROOT/edge0-infer/build/cpu/bin/edge0-infer}"
STRATA_DIR="${EDGE0_STRATA_DIR:-$EDGE0_ROOT/edge0-strata}"
CTX="${EDGE0_CTX:-131072}"
ALIAS="${EDGE0_ALIAS:-edge0-120b}"
# Groessere Prefill-Chunks (Template erlaubt via --prefill auto nur 8192).
# Gemessen: 12k-Prompt 424 -> 537 tok/s Prefill, 64k bringt nichts mehr.
export STRATA_PREFILL_AUTO_MAX="${STRATA_PREFILL_AUTO_MAX:-32768}"
# Tuning fuer 16 GiB VRAM + 8-Kern-CPU (gemessen: ~60-70 tok/s Decode,
# ~1.900 tok/s Prefill, Prefix-Cache greift bei stabilem Prompt-Anfang).
STRATA_ARGS=(
  --vram-reserve-mib 400
  --kv-resident 20480
  --expert-cache-per-layer
  --kv-grow
  --pool-workers 7
  --pcie-mode dma
  --resident-budget-gib 16
)
# Zusaetzliche Engine-Argumente fuer Messlaeufe, z. B.
#   EDGE0_STRATA_EXTRA="--prefill auto:32768 --spec 6" bash server.sh start edge0
# Sie stehen hinter den Defaults, spaetere Flags gewinnen.
if [ -n "${EDGE0_STRATA_EXTRA:-}" ]; then
  # shellcheck disable=SC2206
  STRATA_ARGS+=($EDGE0_STRATA_EXTRA)
fi
LABEL="${EDGE0_LABEL:-edge0}"

laeuft() {
  local pid
  [ -f "$PIDFILE" ] || return 1
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

antwortet() {
  curl -s --max-time 2 "http://$HOST/health" >/dev/null 2>&1
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
  # Prozess lebt, aber /health noch nicht erreichbar: das Laden der Experten
  # dauert 1-2 Minuten. Das Gateway pollt den Health-Pfad selbst.
  echo "[$LABEL] Prozess laeuft bereits (laedt noch, Log: $LOG)."
  exit 0
fi

if [ ! -x "$BIN" ]; then
  echo "[$LABEL] edge0-infer nicht gefunden: $BIN" >&2
  exit 1
fi
if [ ! -d "$MODEL_DIR" ]; then
  echo "[$LABEL] Modellverzeichnis fehlt: $MODEL_DIR" >&2
  exit 1
fi

entlade_system_ollama
mkdir -p "$(dirname "$PIDFILE")"

# edge0-strata cached seine Startargumente in /tmp/edge0-strata-*. Bleibt der
# Ordner von einem Lauf mit anderem --context liegen, startet die Engine mit
# dem alten --max-context und bricht ab. Hier laeuft nachweislich kein Server
# mehr, deshalb ist das Aufraeumen gefahrlos.
rm -rf /tmp/edge0-strata-*

args=(
  serve
  --model "$MODEL_DIR"
  --alias "$ALIAS"
  --strata-dir "$STRATA_DIR"
  --host "${HOST%:*}"
  --port "${HOST##*:}"
  --context "$CTX"
)
if [ -n "${EDGE0_EXTRA:-}" ]; then
  # shellcheck disable=SC2206
  args+=($EDGE0_EXTRA)
fi
for wert in "${STRATA_ARGS[@]}"; do
  args+=(--strata-arg "$wert")
done

# setsid: eigener Prozessgruppe, damit stop-edge0.sh auch die von edge0-infer
# gestartete strata-Engine mit beendet (sonst bleiben ~15 GB VRAM belegt).
cd "$ROOT"
setsid nohup "$BIN" "${args[@]}" >>"$LOG" 2>&1 &
echo $! > "$PIDFILE"

# Kurz pruefen, dass der Prozess nicht sofort wieder stirbt (z. B. wegen
# belegtem Port oder fehlendem File). Das Laden selbst uebernimmt das Gateway.
for _ in $(seq 1 12); do
  sleep 0.5
  laeuft || break
  antwortet && exit 0
done

if laeuft; then
  echo "[$LABEL] gestartet (laedt noch, Log: $LOG)."
  exit 0
fi

echo "[$LABEL] Start fehlgeschlagen (Log: $LOG)." >&2
tail -n 5 "$LOG" >&2 2>/dev/null || true
rm -f "$PIDFILE"
exit 1
