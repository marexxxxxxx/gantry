#!/usr/bin/env bash
# Messlauf fuer Edge0: startet das Backend mit zusaetzlichen Engine-Argumenten,
# schickt drei feste Requests und liest die Metriken aus dem Server-Log.
#
#   bash runtime/edge0/bench.sh <label> ["<strata-args>"] [ctx]
#
# Beispiel:
#   bash runtime/edge0/bench.sh baseline "" 131072
#   bash runtime/edge0/bench.sh prefill32 "--prefill auto:32768" 131072
#
# Die Requests: 4k kalt, 4k warm (Prefix-Cache), 12k kalt, je 128 Tokens
# Ausgabe. Ergebnisse landen als JSONL unter runtime/edge0/bench-results/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="${1:?Label fehlt}"
EXTRA="${2:-}"
CTX="${3:-131072}"
export EDGE0_CTX="$CTX"
export EDGE0_STRATA_EXTRA="$EXTRA"
export EDGE0_LOG="${EDGE0_LOG:-/tmp/edge0_server.log}"
PIDFILE="${EDGE0_PIDFILE:-$ROOT/runtime/edge0/edge0.pid}"
HOST="${EDGE0_HOST:-127.0.0.1:30001}"
OUTDIR="$ROOT/runtime/edge0/bench-results"
mkdir -p "$OUTDIR"

bash "$ROOT/runtime/edge0/stop-edge0.sh"
# Reste einer abgebrochenen Engine einsammeln (nur eigene PID-Gruppe).
if [ -f "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -9 -- "-$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
fi
sleep 2

OFFSET=0
[ -f "$EDGE0_LOG" ] && OFFSET="$(stat -c%s "$EDGE0_LOG")"

echo "### $LABEL | ctx=$CTX | extra=${EXTRA:-<defaults>}"
bash "$ROOT/runtime/edge0/start-edge0.sh"

ok=0
for _ in $(seq 1 150); do
  if curl -s --max-time 2 "http://$HOST/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ "$ok" != 1 ]; then
  echo "FEHLER: $LABEL nicht gesund (Log: $EDGE0_LOG)" >&2
  tail -n 20 "$EDGE0_LOG" >&2 || true
  exit 1
fi
echo "health ok nach dem Start"

python3 - "$HOST" "$LABEL" <<'PY'
import json
import sys
import time
import urllib.request
import uuid

host, label = sys.argv[1], sys.argv[2]
base = f"http://{host}/v1/chat/completions"


def prompt(tokens: int, nonce: str) -> str:
    satz = ("The system records each request, its latency, token counts, "
            "and the backend that served it. ")
    return f"{nonce} " + satz * max(1, int(tokens * 4 / len(satz)))


def call(text: str, max_tokens: int = 128) -> dict:
    body = json.dumps({
        "model": "edge0-120b",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }).encode()
    req = urllib.request.Request(base, body,
                                 {"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1200) as antwort:
        daten = json.load(antwort)
    wand = time.time() - t0
    nutzung = daten.get("usage", {})
    return {"wall_s": round(wand, 2),
            "prompt_tokens": nutzung.get("prompt_tokens"),
            "completion_tokens": nutzung.get("completion_tokens")}


# Kurzer Warmlauf: laedt Kernel/Experten, ohne den Messprompt zu cachen.
call("Warmup. Reply with OK.", 8)
zeit = time.strftime("%H:%M:%S")

nonce = f"Benchmark {label} {uuid.uuid4()}"
p4 = prompt(4000, nonce)
p12 = prompt(12000, nonce + " long")
print(f"4k  kalt : {call(p4)}")
print(f"4k  warm : {call(p4)}")
print(f"12k kalt : {call(p12)}")
print(f"ende: {zeit}")
PY

sleep 1
python3 - "$EDGE0_LOG" "$OFFSET" "$LABEL" "$OUTDIR" <<'PY'
import json
import os
import sys

log, offset, label, outdir = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
with open(log, "rb") as fh:
    fh.seek(offset)
    roh = fh.read().decode("utf-8", "replace")

eintraege = []
for zeile in roh.splitlines():
    zeile = zeile.strip()
    if not (zeile.startswith("{") and "decode_tokens_per_second" in zeile):
        continue
    try:
        daten = json.loads(zeile)
    except ValueError:
        continue
    eintraege.append(daten)

hits = [z for z in roh.splitlines() if "expert cache" in z]
print("\nMesswerte (aus dem Log):")
for name, eintrag in zip(("4k kalt", "4k warm", "12k kalt"), eintraege[-3:]):
    print(f"  {name:9s} in={eintrag.get('input_tokens')} "
          f"reused={eintrag.get('reused_tokens')} "
          f"out={eintrag.get('generated_tokens')} "
          f"ttft={eintrag.get('first_token_seconds')}s "
          f"prefill={eintrag.get('prefill_tokens_per_second')} tok/s "
          f"decode={eintrag.get('decode_tokens_per_second')} tok/s")

gesamt = {
    "label": label,
    "requests": eintraege[-3:],
    "expert_cache": hits[-3:],
}
pfad = os.path.join(outdir, f"{label}.json")
with open(pfad, "w") as fh:
    json.dump(gesamt, fh, indent=2)
print(f"\ngespeichert: {pfad}")
PY

bash "$ROOT/runtime/edge0/stop-edge0.sh"
echo "### $LABEL fertig"
