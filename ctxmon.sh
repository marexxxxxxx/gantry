#!/usr/bin/env bash
# Live-Status des sglang-Servers: Tokens/s, aktueller Kontext, Kontext-Peak
# Nutzung:  watch -n 1 ./ctxmon.sh          (oder einfach ./ctxmon.sh für einmalig)
# Log-Pfad ändern:  LOG=/pfad/zum/log ./ctxmon.sh
LOG="${LOG:-/tmp/opencode/server.log}"
API="http://127.0.0.1:30000"
AUTH="Authorization: Bearer marec"

MAX=$(curl -s -m 2 -H "$AUTH" "$API/get_server_info" \
      | python3 -c "import json,sys; print(json.load(sys.stdin).get('max_total_num_tokens',0))" 2>/dev/null)

LOAD=$(curl -s -m 2 -H "$AUTH" "$API/get_load")
NREQ=$(echo "$LOAD" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['num_reqs'] if isinstance(d,list) else d.get('num_reqs',0))" 2>/dev/null)
NTOK=$(echo "$LOAD" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['num_tokens'] if isinstance(d,list) else d.get('num_tokens',0))" 2>/dev/null)

if [ ! -r "$LOG" ]; then
    echo "Log nicht lesbar: $LOG  (Server mit >/tmp/opencode/server.log starten oder LOG=... setzen)"
fi

TS=$(grep -o 'gen throughput (token/s): [0-9.]*' "$LOG" 2>/dev/null | tail -1 | awk '{print $NF}')
PEAK=$(grep -o '#full token: [0-9]*' "$LOG" 2>/dev/null | awk '{print $NF}' | sort -n | tail -1)
LAST=$(grep -o '#full token: [0-9]*' "$LOG" 2>/dev/null | tail -1 | awk '{print $NF}')

if [ "${NREQ:-0}" -gt 0 ]; then CUR=$NTOK; STATUS="aktiv"; else CUR=$LAST; STATUS="idle"; fi

echo "Status       : $STATUS"
echo "Tokens/s     : ${TS:--}   (letzte Dekodierung)"
echo "Ctx aktuell  : ${CUR:-0} / ${MAX:-?} Tokens"
echo "Ctx Peak     : ${PEAK:-0} Tokens"
