#!/usr/bin/env bash
# aiserver-monitor Stack-Start: SGLang (escha) + Gateway/Dashboard
#
#   SGLang  : 127.0.0.1:30001   (Cutover-Topologie; Log: /tmp/escha_server.log,
#                                 den slotmon passiv mitliest)
#   Gateway : 0.0.0.0:30000     -> Dashboard http://<host>:30000/
#                                 Log: /tmp/aiserver-monitor-run.log
#                                 PID: /tmp/aiserver-monitor.pid
#
# Sind die systemd-User-Units installiert (escha-server.service,
# aiserver-monitor.service), startet und stoppt dieses Skript genau diese
# Units - also denselben Weg wie der Autostart beim Boot. Sonst wird direkt
# ueber start-penalty.sh bzw. run.sh gestartet.
#
# Nutzung:
#   bash start-aiserver-monitor.sh          # alles starten (SGLang nur falls noetig)
#   bash start-aiserver-monitor.sh status   # Status beider Dienste
#   bash start-aiserver-monitor.sh stop     # Gateway + SGLang stoppen
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Gateway-Projekt: Haupt-Checkout bevorzugt, sonst Entwicklungs-Worktree
GW_PROJ="$REPO/aiserver-monitor"
if [ ! -d "$GW_PROJ" ]; then
  GW_PROJ="$REPO/.worktrees/aiserver-monitor/aiserver-monitor"
fi

SGLANG_PORT="${SGLANG_PORT:-30001}"
SGLANG_LOG="${SGLANG_LOG:-/tmp/escha_server.log}"
GW_PORT="${GW_PORT:-30000}"

USER_UNITS=(escha-server.service aiserver-monitor.service)

sglang_laeuft() { pgrep -f "[s]glang.launch_server" >/dev/null 2>&1; }

hat_systemd_units() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user cat escha-server.service >/dev/null 2>&1 &&
    systemctl --user cat aiserver-monitor.service >/dev/null 2>&1
}

warte_auf_sglang() {
  if curl -fsS "http://127.0.0.1:${SGLANG_PORT}/health" >/dev/null 2>&1; then
    echo "[stack] SGLang laeuft (Port ${SGLANG_PORT})."
    return 0
  fi
  echo "[stack] Warte auf SGLang /health - der Start dauert 2-4 Minuten..."
  for _ in $(seq 1 120); do
    if curl -fsS "http://127.0.0.1:${SGLANG_PORT}/health" >/dev/null 2>&1; then
      echo "[stack] SGLang bereit (Port ${SGLANG_PORT})."
      return 0
    fi
    sleep 3
  done
  echo "[stack] WARNUNG: SGLang nach 6 Minuten noch nicht bereit. Log: ${SGLANG_LOG}" >&2
  return 1
}

warte_auf_gateway() {
  for _ in $(seq 1 20); do
    if curl -fsS "http://127.0.0.1:${GW_PORT}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "[stack] WARNUNG: Gateway /healthz auf Port ${GW_PORT} nicht erreichbar." >&2
  return 1
}

starte_sglang() {
  if sglang_laeuft; then
    echo "[stack] SGLang laeuft bereits."
    return 0
  fi
  echo "[stack] Starte SGLang auf 127.0.0.1:${SGLANG_PORT} (16-GB-Profil, OTP an, CTX 86016)..."
  echo "[stack] Log: ${SGLANG_LOG} - Start dauert 2-4 Minuten."
  PORT="$SGLANG_PORT" LOG="$SGLANG_LOG" bash "$REPO/runtime/sglang/start-penalty.sh" 2.0
}

starte_gateway() {
  echo "[stack] Starte Gateway/Dashboard auf 0.0.0.0:${GW_PORT}..."
  (
    cd "$GW_PROJ"
    AISERVER_MONITOR_LISTEN_PORT="$GW_PORT" \
    AISERVER_MONITOR_UPSTREAM_BASE="http://127.0.0.1:${SGLANG_PORT}" \
      bash run.sh start
  )
}

starte_stack() {
  if hat_systemd_units; then
    echo "[stack] Starte ueber systemd-User-Units..."
    systemctl --user start "${USER_UNITS[@]}"
    warte_auf_sglang || true
    warte_auf_gateway || true
  else
    starte_sglang
    starte_gateway
  fi
}

status() {
  if hat_systemd_units; then
    echo "[stack] systemd-Units:"
    local unit state
    for unit in "${USER_UNITS[@]}"; do
      state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
      printf '  %-26s %s\n' "$unit" "${state:-unbekannt}"
    done
  fi

  echo "[stack] SGLang:"
  if sglang_laeuft; then
    if curl -sf "http://127.0.0.1:${SGLANG_PORT}/health" >/dev/null 2>&1; then
      echo "  laeuft, /health ok (Port ${SGLANG_PORT})"
    else
      echo "  Prozess laeuft, /health noch nicht ok (Port ${SGLANG_PORT})"
    fi
  else
    echo "  laeuft nicht"
  fi

  echo "[stack] Gateway:"
  if curl -fsS "http://127.0.0.1:${GW_PORT}/healthz" >/dev/null 2>&1; then
    echo "  laeuft, /healthz ok (Port ${GW_PORT})"
  elif hat_systemd_units; then
    echo "  /healthz nicht erreichbar (Port ${GW_PORT})"
  else
    ( cd "$GW_PROJ" && bash run.sh status ) || true
  fi
}

stop_all() {
  if hat_systemd_units; then
    echo "[stack] Stoppe Gateway (systemd)..."
    systemctl --user stop aiserver-monitor.service || true
    echo "[stack] Stoppe SGLang (systemd)..."
    systemctl --user stop escha-server.service || true
  fi

  # Reste aus manuellen Starts einsammeln (bei aktiven Units ohne Wirkung).
  ( cd "$GW_PROJ" && bash run.sh stop ) >/dev/null 2>&1 || true
  if sglang_laeuft; then
    echo "[stack] Stoppe SGLang-Restprozess..."
    bash "$REPO/runtime/sglang/stop-server.sh" || true
  elif ! hat_systemd_units; then
    echo "[stack] SGLang laeuft nicht."
  fi
}

case "${1:-start}" in
  start)
    starte_stack
    LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo
    echo "[stack] Fertig."
    echo "  Dashboard : http://127.0.0.1:${GW_PORT}/"
    if [ -n "$LAN_IP" ]; then
      echo "  Dashboard (LAN): http://${LAN_IP}:${GW_PORT}/"
    fi
    echo "  SGLang    : http://127.0.0.1:${SGLANG_PORT} (Clients: http://127.0.0.1:${GW_PORT}/v1/... via Gateway)"
    echo "  Logs      : ${SGLANG_LOG} | /tmp/aiserver-monitor-run.log | /tmp/slotmon.log"
    if hat_systemd_units; then
      echo "  Autostart : systemd-User-Units, aktiv beim Boot"
    fi
    echo "  Stop      : bash $(basename "$0") stop"
    ;;
  status)
    status
    ;;
  stop)
    stop_all
    ;;
  *)
    echo "Nutzung: $0 {start|status|stop}" >&2
    exit 2
    ;;
esac
