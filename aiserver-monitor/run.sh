#!/usr/bin/env bash
# Lebenszyklus des aiserver-monitors: start, stop, status, restart.
#
# usage: bash run.sh {start|stop|status|restart}
#
# Konventionen wie monitor.sh: `set -euo pipefail`, Doppelstart-Sperre ueber
# `pgrep -f` mit Bracket-Muster, `nohup` mit Logumleitung, Health-Poll,
# `stop` mit TERM, ${STOP_GRACE}s Grace, dann KILL.
#
# Konfiguration kommt aus der Umgebung, alles mit Default:
#   PYTHON=/home/user/small_qwen/.venv/bin/python
#   AISERVER_MONITOR_LISTEN_PORT=30000   PIDFILE=/tmp/aiserver-monitor.pid
#   AISERVER_MONITOR_RUN_LOG=/tmp/aiserver-monitor-run.log
#   AISERVER_MONITOR_PATTERN='[a]iserver_monitor'
#
# Die Anwendung selbst liest ihre Konfiguration aus AISERVER_MONITOR_<FELD>
# (oder per AISERVER_MONITOR_CONFIG aus einer TOML-Datei); dieses Skript
# reicht die Umgebung nur durch und liest AISERVER_MONITOR_LISTEN_PORT fuer
# den Health-Poll.
#
# `start` ist idempotent: ein zweiter Start ist kein Fehler, sondern Exit 0 mit
# "laeuft bereits". `stop` ist ebenfalls idempotent. Wer in einem Skript start
# aufruft und danach fragt, ob es geklappt hat, will keine Fehlermeldung, weil
# schon etwas laeuft. Den Zustand meldet `status` - Exit 1, wenn nichts laeuft.
#
# Der SGLang-Server wird von diesem Skript nicht angefasst. Sein Lebenszyklus
# gehoert runtime/sglang/, der des Gateways hier - zwei getrennte Lebenszyklen.
# Der CPU-Embedding-Sidecar (runtime/embed/) gehoert zum Gateway: /v1/embeddings
# ist ohne ihn nicht nutzbar, also startet und stoppt run.sh ihn mit. Ein
# Backend-Wechsel (server.sh) laesst ihn unberuehrt.
set -euo pipefail

# run.sh liegt im Projektverzeichnis; `python -m aiserver_monitor` findet das
# Paket nur von dort aus, und relative Pfade wie `data/monitor.db` stimmen nur
# von hier.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

PYTHON="${PYTHON:-/home/user/small_qwen/.venv/bin/python}"
PORT="${AISERVER_MONITOR_LISTEN_PORT:-30000}"
PIDFILE="${PIDFILE:-/tmp/aiserver-monitor.pid}"
LOG="${AISERVER_MONITOR_RUN_LOG:-/tmp/aiserver-monitor-run.log}"
# `[a]iserver_monitor` statt `aiserver_monitor`: pgrep sucht in der vollen
# Kommandozeile und wuerde dabei sich selbst finden. Mit der Klammer matcht
# das Muster nur den echten Prozess. Der Default fasst jede per
# `-m aiserver_monitor` gestartete Instanz - auch eine, die von Hand oder von
# systemd gestartet wurde.
# `:-` reicht hier nicht: das ist "unset ODER leer", ein **leeres**
# AISERVER_MONITOR_PATTERN waere gesetzt und kaeme durch. Ein leeres Muster ist
# bei `pgrep -f` kein "kein Treffer", sondern "alles": gemessen lieferte
# `pgrep -f ""` jeden Kernel-Thread der Maschine, bis hinauf zu PID 1. Mit
# `pkill -f ""` waere `stop` ein Systembefehl. Ein leerer Wert wird deshalb
# abgewiesen, nicht stillschweigend zum Default.
if [ -n "${AISERVER_MONITOR_PATTERN:-}" ]; then
    PATTERN="$AISERVER_MONITOR_PATTERN"
else
    PATTERN='[a]iserver_monitor'
fi
HEALTH="http://127.0.0.1:${PORT}/healthz"
EMBED_BASE="${AISERVER_MONITOR_EMBED_BASE:-http://127.0.0.1:30002}"
# 20 s sind die Grenze aus dem Muster. Ueberschreibbar, damit ein Test nicht
# 20 s auf das Beenden wartet.
STOP_GRACE="${STOP_GRACE:-20}"
START_WAIT="${START_WAIT:-20}"

usage() {
    echo "usage: $0 {start|stop|status|restart}"
    echo "  start     Gateway im Hintergrund starten und auf /healthz warten"
    echo "  stop      Gateway beenden (TERM, ${STOP_GRACE}s, dann KILL)"
    echo "  status    Zustand lesen, Exit 1 wenn das Gateway nicht laeuft"
    echo "  restart   stop und start"
    echo "Umgebung: AISERVER_MONITOR_LISTEN_PORT=$PORT PIDFILE=$PIDFILE"
    exit 2
}

# `kill -0` allein reicht nicht: ein beendeter Kindprozess bleibt als Zombie in
# der Prozessliste stehen und beantwortet `kill -0` weiterhin mit Erfolg. Ohne
# diese Zombie-Pruefung wartet `start` die vollen ${START_WAIT}s, obwohl das
# Gateway nach einer Sekunde schon tot war.
alive() {
    local stat
    kill -0 "$1" 2>/dev/null || return 1
    stat=$(ps -o stat= -p "$1" 2>/dev/null) || return 1
    if [[ $stat == Z* ]]; then
        return 1
    fi
    return 0
}

# `pgrep -f` vergleicht die **ganze Kommandozeile**, nicht den Prozessnamen.
# Das heisst: jeder Prozess, der das Muster nur *nennt*, gilt als Treffer -
# auch die Shell, in der `run.sh stop` getippt wurde, auch ein `grep`, auch
# eine Editor-Vorschau. Der zweite Filter ist deshalb nicht Kosmetik, sondern
# die Grenze zwischen "Gateway beenden" und "fremden Prozess beenden".
# Geprueft wird wortweise auf den echten Aufruf `-m aiserver_monitor`.
is_monitor_cmdline() {
    tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null |
        grep -qE -- '(^|[[:space:]])-m[[:space:]]+aiserver_monitor([[:space:]]|$)'
}

is_running() {
    local pid
    for pid in $(pgrep -f "$PATTERN" 2>/dev/null); do
        if is_monitor_cmdline "$pid"; then
            return 0
        fi
    done
    return 1
}

monitor_pids() {
    local pid
    for pid in $(pgrep -f "$PATTERN" 2>/dev/null); do
        if is_monitor_cmdline "$pid"; then
            printf '%s ' "$pid"
        fi
    done
}

# Genau die *Gateway*-PIDs. `pkill -f` kann nicht filtern - es beendet alles
# mit passendem Text, und dieser Text steht in Kommandozeilen fremder Prozesse.
# `monitor_pids` + `kill_one` statt `pkill`.
signal_monitors() {
    local sig=$1 pid any=0
    for pid in $(monitor_pids); do
        kill -"$sig" "$pid" 2>/dev/null && any=1
    done
    return $((1 - any))
}

# Die PID-Datei ist kein Buch ueber den Zustand - sie ist ein Ziel fuer `stop`.
# `is_running` fragt `pgrep`, weil ein Gateway auch ohne Datei laufen kann (von
# Hand gestartet, aus einer anderen Shell, nach einem reboot ohne /tmp).
# Benutzt wird sie trotzdem: nur so trifft `stop` genau einen Prozess, statt
# die erste Zeile aus `pgrep` zu raten.
read_pidfile() {
    local pid
    [[ -r "$PIDFILE" ]] || return 1
    pid=$(head -n 1 -- "$PIDFILE" 2>/dev/null) || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$pid"
}

# Gehoert die PID wirklich zum Gateway? Eine PID-Datei kann veralten, und eine
# inzwischen recycelte PID gehoert dann einem fremden Prozess - den zu killen
# waere der teuerste Fehler dieses Skripts. Geprueft wird gegen dasselbe Muster
# wie `is_running`.
owned_by_us() {
    kill -0 "$1" 2>/dev/null || return 1
    is_monitor_cmdline "$1"
}

# Genau einen Prozess beenden: TERM, warten, KILL. Ohne Muster, ohne Nachbarn -
# deshalb fuer das Aufraeumen nach einem fehlgeschlagenen `start`, wo
# ausschliesslich der Prozess aus diesem Lauf hin darf.
kill_one() {
    local pid=$1 waited=0
    kill -TERM "$pid" 2>/dev/null || return 0
    while alive "$pid" && [ "$waited" -lt "$STOP_GRACE" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if alive "$pid"; then
        echo "[aiserver-monitor] PID $pid reagiert nicht auf TERM, sende KILL" >&2
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi
}

start_embed() {
    local script="$ROOT/runtime/embed/start-embed.sh"
    [ -f "$script" ] || return 0
    if ! bash "$script"; then
        echo "[aiserver-monitor] WARNUNG: Embedding-Sidecar (CPU) startet nicht; /v1/embeddings bleibt ohne Upstream." >&2
    fi
}

stop_embed() {
    local script="$ROOT/runtime/embed/stop-embed.sh"
    [ -f "$script" ] || return 0
    bash "$script" || true
}

start() {
    start_embed
    if is_running; then
        echo "[aiserver-monitor] laeuft bereits (PID $(monitor_pids), Port $PORT)"
        return 0
    fi

    mkdir -p "$(dirname "$LOG")" "$(dirname "$PIDFILE")"
    cd "$ROOT"

    # Ohne PYTHONUNBUFFERED landen die Logzeilen erst beim Prozessende im
    # Log. Genau die Zeile mit der Ursache waere dann weg, wenn `start` sie
    # nach einem Fehlschlag ausgeben will.
    export PYTHONUNBUFFERED=1

    # Kein `exec -a`: der Prozess soll als genau das erscheinen, was systemd
    # startet (`python -m aiserver_monitor`). `-B`, damit das Gateway nicht in
    # den Projektbaum schreibt.
    nohup "$PYTHON" -B -m aiserver_monitor >>"$LOG" 2>&1 &
    local child=$!
    echo "$child" >"$PIDFILE"

    local waited=0
    while true; do
        # Erst der Prozess, dann der Port: ein toter Prozess, dessen Port schon
        # besetzt ist, darf nicht als gesundes Gegenstueck durchgehen.
        if ! alive "$child"; then
            local rc=0
            wait "$child" 2>/dev/null || rc=$?
            # 127 heisst: bash kennt die PID nicht mehr (bereits geholt oder
            # nie ein Kind gewesen). Dann gibt es keinen Exit-Code zu melden.
            if [ "$rc" -eq 127 ]; then rc="unbekannt"; fi
            echo "[aiserver-monitor] FAILED: das Gateway ist nach ${waited}s mit Exit $rc gestorben." >&2
            echo "[aiserver-monitor] letzte 20 Zeilen aus $LOG:" >&2
            tail -n 20 "$LOG" >&2 || true
            rm -f "$PIDFILE"
            return 1
        fi
        if curl -sf -o /dev/null "$HEALTH" 2>/dev/null; then
            echo "[aiserver-monitor] laeuft auf http://127.0.0.1:$PORT (PID $child, Log: $LOG)"
            return 0
        fi
        if [ "$waited" -ge "$START_WAIT" ]; then
            echo "[aiserver-monitor] ERROR: nach ${START_WAIT}s keine Health-Antwort auf $HEALTH" >&2
            tail -n 20 "$LOG" >&2 || true
            # Aufraeumen. Nur der Prozess aus diesem Lauf - ein stehen
            # gelassenes, halb gestartetes Gateway wuerde beim naechsten
            # `start` nur "laeuft bereits" sagen.
            kill_one "$child"
            rm -f "$PIDFILE"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

stop() {
    if ! is_running; then
        echo "[aiserver-monitor] laeuft nicht"
        rm -f "$PIDFILE"
        stop_embed
        return 0
    fi

    # Zuerst die PID aus der Datei: gezielt, ohne den Warteweg ueber `pgrep`.
    local pid pids
    pid=$(read_pidfile || true)
    if [ -n "$pid" ] && owned_by_us "$pid"; then
        echo "[aiserver-monitor] stoppe PID $pid aus $PIDFILE"
        kill_one "$pid"
    else
        pids=$(monitor_pids)
        echo "[aiserver-monitor] stoppe PID(s) $pids (kein brauchbarer Eintrag in $PIDFILE)"
        for pid in $pids; do
            kill_one "$pid"
        done
    fi

    # Sicherheitsnetz fuer Prozesse ohne (richtigen) Eintrag in der PID-Datei:
    # ein zweites Gateway, oder eines, das zwischen Datei und Prozessliste
    # aufgetaucht ist. `signal_monitors` filtert vorher auf echte Aufrufe von
    # `-m aiserver_monitor` - `pkill -f "$PATTERN"` wuerde jede Shell treffen,
    # die das Muster nur nennt.
    if is_running; then
        signal_monitors TERM || true
        local waited=0
        while is_running && [ "$waited" -lt "$STOP_GRACE" ]; do
            sleep 1
            waited=$((waited + 1))
        done
    fi
    if is_running; then
        echo "[aiserver-monitor] nach ${STOP_GRACE}s noch aktiv: $(monitor_pids) - sende KILL" >&2
        signal_monitors KILL || true
        sleep 1
    fi

    rm -f "$PIDFILE"
    if is_running; then
        echo "[aiserver-monitor] ERROR: $(monitor_pids) laeuft noch nach dem KILL" >&2
        return 1
    fi
    stop_embed
    echo "[aiserver-monitor] gestoppt"
}

status() {
    # `status` liest. Es startet nichts und loest keinen Takt aus - die Frage
    # "laeuft er" beantwortet `pgrep`.
    if ! is_running; then
        echo "[aiserver-monitor] laeuft nicht"
        return 1
    fi

    echo "[aiserver-monitor] laeuft (PID $(monitor_pids), Port $PORT)"
    if ! curl -sf -m 5 -o /dev/null "$HEALTH" 2>/dev/null; then
        echo "[aiserver-monitor] /healthz antwortet nicht (laeuft das Gateway wirklich?)"
    fi
    if curl -sf -m 2 -o /dev/null "${EMBED_BASE%/}/api/version" 2>/dev/null; then
        echo "[aiserver-monitor] Embedding-Sidecar (CPU) laeuft auf $EMBED_BASE"
    else
        echo "[aiserver-monitor] Embedding-Sidecar (CPU) laeuft nicht; /v1/embeddings hat keinen Upstream"
    fi
}

case "${1:-}" in
    start)      start ;;
    stop)       stop ;;
    status)     status ;;
    restart)    stop; start ;;
    *)          usage ;;
esac
