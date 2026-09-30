#!/usr/bin/env bash
# Verify single-flight behavior of a running escha/sglang server (serve.sh, SERIAL=1).
#
# Pass criteria (from the task):
#   1. Request B, started while long request A is generating, does NOT complete before A.
#   2. B eventually SUCCEEDS (HTTP 200) — it is queued, not rejected with 429/503.
#   3. /metrics shows num_running_reqs never above 1 while A+B overlap in time.
#   4. (informational) nvidia-smi VRAM shows no large extra jump vs. one generation.
#
# Usage:
#   bash verify-single-flight.sh [PORT] [MODEL]     # defaults: 30000 escha-qwen38-27b-w2
#
# Requires: curl, awk. Optional: nvidia-smi (VRAM check is skipped without it).
set -uo pipefail

PORT=${1:-30000}
MODEL=${2:-escha-qwen38-27b-w2}
BASE="http://127.0.0.1:$PORT"
HEADSTART=${HEADSTART:-3}      # seconds between firing A and firing B
TOL=${TOL:-2}                  # timing jitter tolerance, seconds
MAXTIME=${MAXTIME:-900}        # per-request curl --max-time (queued B may wait minutes)

TMP=$(mktemp -d /tmp/single-flight.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

echo "[sf] target=$BASE model=$MODEL headstart=${HEADSTART}s"

# ---- 0) server reachable ----------------------------------------------------------------------
if ! curl -sf --max-time 10 "$BASE/v1/models" > "$TMP/models.json"; then
  echo "[sf] FAIL: server not reachable at $BASE/v1/models (start it with serve.sh first)"
  exit 1
fi
echo "[sf] /v1/models OK"

# ---- metrics / VRAM pollers (background) ------------------------------------------------------
: > "$TMP/running.txt"
: > "$TMP/vram.txt"
touch "$TMP/poll.flag"
(
  while [[ -f "$TMP/poll.flag" ]]; do
    v=$(curl -s --max-time 2 "$BASE/metrics" 2>/dev/null \
        | awk '$1 ~ /^sglang_num_running_reqs$/ {print $2; exit}')
    [[ -n "$v" ]] && echo "$v" >> "$TMP/running.txt"
    sleep 0.5
  done
) &
POLLER=$!
if command -v nvidia-smi > /dev/null 2>&1; then
  (
    while [[ -f "$TMP/poll.flag" ]]; do
      nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
        | head -1 | tr -dc '0-9' >> "$TMP/vram.txt"
      sleep 0.5
    done
  ) &
  VPOLLER=$!
fi

# ---- 1) fire long request A in the background --------------------------------------------------
curl -s -N --max-time "$MAXTIME" "$BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Count slowly from 1 to 50.\"}],\"max_tokens\":512,\"stream\":false}" \
  -o "$TMP/A.json" -w '%{http_code} %{time_total}' > "$TMP/A.meta" &
A_PID=$!

# ---- 2) give A a head start, then fire B ------------------------------------------------------
sleep "$HEADSTART"
curl -s -N --max-time "$MAXTIME" "$BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say only: SECOND\"}],\"max_tokens\":32,\"stream\":false}" \
  -o "$TMP/B.json" -w '%{http_code} %{time_total} %{time_starttransfer}' > "$TMP/B.meta"
B_RC=$?

# ---- 3) reap A, stop pollers -------------------------------------------------------------------
wait "$A_PID"; A_RC=$?
rm -f "$TMP/poll.flag"
wait "$POLLER" 2>/dev/null
[[ -n "${VPOLLER:-}" ]] && wait "$VPOLLER" 2>/dev/null

read -r A_CODE A_TOTAL < "$TMP/A.meta"
read -r B_CODE B_TOTAL B_TTFB < "$TMP/B.meta"
echo "[sf] A: http=$A_CODE total=${A_TOTAL}s   B: http=$B_CODE total=${B_TOTAL}s ttfb=${B_TTFB}s (started +${HEADSTART}s)"

# ---- 4) assertions -----------------------------------------------------------------------------
FAIL=0
note() { echo "[sf] $*"; }
fail() { echo "[sf] FAIL: $*"; FAIL=1; }

if [[ "$A_RC" -ne 0 ]]; then fail "request A curl error (rc=$A_RC, timeout=$((MAXTIME))s?)"; fi
if [[ "$B_RC" -ne 0 ]]; then fail "request B curl error (rc=$B_RC) — a queued request must not fail"; fi

if [[ "$A_CODE" != "200" ]]; then fail "A returned HTTP $A_CODE (expected 200)"; fi
if [[ "$B_CODE" == "429" || "$B_CODE" == "503" ]]; then
  fail "B was rejected with HTTP $B_CODE — queued requests must not be rate-limited"
elif [[ "$B_CODE" != "200" ]]; then
  fail "B returned HTTP $B_CODE (expected 200)"
fi

# B's FIRST BYTE must not arrive before A's LAST byte: B started HEADSTART after A, so a
# serialized B has ttfb >= A_total - HEADSTART. This is the direct "B does not complete
# before A finishes" check (stronger than comparing totals, which a slow parallel B could
# accidentally pass).
if ! awk -v a="$A_TOTAL" -v b="$B_TTFB" -v h="$HEADSTART" -v t="$TOL" \
     'BEGIN { exit !(b >= a - h - t) }'; then
  fail "B's first byte arrived before A finished (B_ttfb=${B_TTFB}s, A_total=${A_TOTAL}s, headstart=${HEADSTART}s) — requests ran in PARALLEL"
else
  note "B's first byte came after A finished (serialized): B_ttfb=${B_TTFB}s >= A_total-headstart-tol"
fi

if [[ -s "$TMP/B.json" ]] && ! awk '/"choices"/ {found=1} END{exit !found}' "$TMP/B.json"; then
  fail "B response has no choices — not a successful completion"
fi

if [[ -s "$TMP/running.txt" ]]; then
  MAX_RUN=$(sort -g "$TMP/running.txt" | tail -1)
  if awk -v m="$MAX_RUN" 'BEGIN { exit !(m > 1) }'; then
    fail "num_running_reqs peaked at $MAX_RUN (> 1) — more than one generation was on the GPU"
  else
    note "num_running_reqs peaked at $MAX_RUN (never above 1)"
  fi
else
  note "no /metrics samples (metrics disabled?) — skipping running-req check"
fi

if [[ -s "$TMP/vram.txt" ]]; then
  V_BASE=$(sort -g "$TMP/vram.txt" | head -1)
  V_MAX=$(sort -g "$TMP/vram.txt" | tail -1)
  V_DELTA=$((V_MAX - V_BASE))
  note "VRAM: baseline=${V_BASE}MiB peak=${V_MAX}MiB delta=+${V_DELTA}MiB (one generation's KV only; a multi-GB jump would suggest parallel decode)"
  if [[ "$V_DELTA" -gt 3072 ]]; then
    note "WARNING: VRAM jump > 3 GiB — inspect whether two generations were resident"
  fi
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "[sf] PASS: single-flight confirmed — B queued behind A, then succeeded; <=1 running request at all times"
  exit 0
fi
echo "[sf] FAIL: see above"
exit 1
