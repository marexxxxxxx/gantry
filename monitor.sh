#!/usr/bin/env bash
# Lebenszyklus des Slot-Monitors: start, stop, status, restart.
#
# usage: bash monitor.sh {start|stop|status|restart}
#
# Muster aus runtime/sglang/start-penalty.sh: `set -euo pipefail`,
# Doppelstart-Sperre ueber `pgrep -f`, `nohup` mit Logumleitung, Health-Poll,
# `stop` mit TERM, 20 s Grace, dann KILL.
#
# Konfiguration kommt aus der Umgebung, alles mit Default:
#   PYTHON=python3.12            PORT=8090
#   API=http://127.0.0.1:30000   MONITOR_LOG=/tmp/slotmon.log
#   PIDFILE=/tmp/slotmon.pid     SLOTMON_PATTERN='[s]lotmon.server'
#
# `start` ist idempotent: ein zweiter Start ist kein Fehler, sondern Exit 0 mit
# "laeuft bereits". Wer in einem Skript `monitor.sh start` aufruft und danach
# fragt, ob es geklappt hat, will keine Fehlermeldung, weil schon etwas laeuft.
# Auffaellig wird der Zustand ueber `status` - das liefert Exit 1, wenn nichts
# laeuft. Genau diese Rechnung macht start-penalty.sh auch.
#
# Der Server auf Port 30000 wird von diesem Skript nicht angefasst. Sein
# Lebenszyklus gehoert in runtime/sglang/, der des Monitors hier - zwei
# getrennte Lebenszyklen. Wer den Server killt, hat den Falschen.
set -euo pipefail

# monitor.sh liegt im Repo-Wurzelverzeichnis; `python -m slotmon.server`
# findet das Paket nur von dort aus.
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

PYTHON="${PYTHON:-python3.12}"
PORT="${PORT:-8090}"
API="${API:-http://127.0.0.1:30000}"
MONITOR_LOG="${MONITOR_LOG:-/tmp/slotmon.log}"
PIDFILE="${PIDFILE:-/tmp/slotmon.pid}"
# `[s]lotmon` statt `slotmon`: pgrep sucht in der vollen Kommandozeile und
# wuerde dabei sich selbst finden. Mit der Klammer matcht das Muster nur den
# echten Prozess. Der Default fasst jeden `slotmon.server` - auch einen, den
# jemand von Hand gestartet hat. Wer einen Monitor auf einem zweiten Port
# daneben laesst, grenzt das Muster selbst ein.
# `:-` reicht hier nicht: das ist "unset ODER leer", ein **leeres**
# SLOTMON_PATTERN waere gesetzt und kaeme durch. Ein leeres Muster ist bei
# `pgrep -f` kein "kein Treffer", sondern "alles": gemessen lieferte
# `pgrep -f ""` jeden Kernel-Thread der Maschine, bis hinauf zu PID 1. Mit
# `pkill -f ""` waere `stop` ein Systembefehl. Ein leerer Wert wird deshalb
# abgewiesen, nicht stillschweigend zum Default.
if [ -n "${SLOTMON_PATTERN:-}" ]; then
    PATTERN="$SLOTMON_PATTERN"
else
    PATTERN='[s]lotmon.server'
fi
# 20 s sind die Grenze aus dem Muster. Ueberschreibbar, damit ein Test nicht
# 20 s auf das Beenden wartet.
STOP_GRACE="${STOP_GRACE:-20}"
START_WAIT="${START_WAIT:-20}"

usage() {
    echo "usage: $0 {start|start-systemd|stop|status|restart}"
    echo "  start     Monitor im Hintergrund starten und auf /health warten"
    echo "  stop      Monitor beenden (TERM, ${STOP_GRACE}s, dann KILL)"
    echo "  status    Zustand lesen, Exit 1 wenn der Monitor nicht laeuft"
    echo "  restart   stop und start"
    echo "Umgebung: PORT=$PORT MONITOR_LOG=$MONITOR_LOG PIDFILE=$PIDFILE"
    exit 2
}

# `kill -0` allein reicht nicht: ein beendeter Kindprozess bleibt als Zombie in
# der Prozessliste stehen und beantwortet `kill -0` weiterhin mit Erfolg. Ohne
# diese Zombie-Pruefung wartet `start` die vollen 20 s, obwohl der Monitor
# nach einer Sekunde schon tot war - und genau so stirbt er, wenn
# `names.load_names` bei fehlender systems.json `sys.exit` ruft.
alive() {
    local stat
    kill -0 "$1" 2>/dev/null || return 1
    stat=$(ps -o stat= -p "$1" 2>/dev/null) || return 1
    if [[ $stat == Z* ]]; then
        return 1
    fi
    return 0
}

# Anmerkung fuer Leser: `is_running` und `monitor_pids` stehen hier **einmal**,
# mit dem `is_monitor_cmdline`-Filter darunter. Sie waren vorher zweimal
# definiert - zuerst ohne Filter, dann mit. Bash nimmt die spaetere, also war
# die erste Fassung toter Code, der beim Lesen so aussah, als gaebe es einen
# Pfad ohne Filter.

# `pgrep -f` vergleicht die **ganze Kommandozeile**, nicht den Prozessnamen.
# Das heisst: jeder Prozess, der das Muster nur *nennt*, gilt als Treffer -
# auch die Shell, in der `monitor.sh stop` getippt wurde, auch ein `grep`, auch
# eine Editor-Vorschau. Gemessen: `pgrep -f '[s]lotmon.server'` lieferte die
# eigene Aufruf-Shell mit, und `pkill -f` haette sie beendet.
#
# Der zweite Filter ist deshalb nicht Kosmetik, sondern die Grenze zwischen
# "Monitor beenden" und "fremden Prozess beenden". Geprueft wird die Kommandozeile
# **wortweise**: `-m slotmon.server` ist der Aufruf, den `slotmon/server.py`
# selbst dokumentiert; ein blosses Nennen genuegt nicht.
is_monitor_cmdline() {
    tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null |
        grep -qE -- '(^|[[:space:]])-m[[:space:]]+slotmon\.server([[:space:]]|$)'
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

# Genau die *Monitor*-PIDs. `pkill -f` kann nicht filtern - es beendet alles mit
# passendem Text, und dieser Text steht in Kommandozeilen fremder Prozesse.
# `monitor_pids` + `kill_one` statt `pkill`.
signal_monitors() {
    local sig=$1 pid any=0
    for pid in $(monitor_pids); do
        kill -"$sig" "$pid" 2>/dev/null && any=1
    done
    return $((1 - any))
}

# Die PID-Datei ist kein Buch ueber den Zustand - sie ist ein Ziel fuer `stop`.
# `is_running` fragt `pgrep`, weil ein Monitor auch ohne Datei laufen kann (von
# Hand, aus einer anderen Shell, nach einem reboot ohne /tmp). Benutzt wird sie
# trotzdem: nur so trifft `stop` genau einen Prozess, statt die erste Zeile aus
# `pgrep` zu raten.
read_pidfile() {
    local pid
    [[ -r "$PIDFILE" ]] || return 1
    pid=$(head -n 1 -- "$PIDFILE" 2>/dev/null) || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$pid"
}

# Gehoert die PID wirklich zum Monitor? Eine PID-Datei kann veralten, und eine
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
        echo "[slotmon] PID $pid reagiert nicht auf TERM, sende KILL" >&2
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi
}

start() {
    if is_running; then
        echo "[slotmon] laeuft bereits (PID $(monitor_pids), Port $PORT)"
        return 0
    fi

    mkdir -p "$(dirname "$MONITOR_LOG")" "$(dirname "$PIDFILE")"
    cd "$REPO"

    # Ohne PYTHONUNBUFFERED landen die `[slotmon]`-Zeilen des Collectors erst
    # beim Prozessende im Log. Genau die Zeile mit der Health-Ursache waere dann
    # weg, wenn `start` sie ausgeben will.
    export PYTHONUNBUFFERED=1 PORT API MONITOR_LOG

    # argv[0] nennt den Port: `ps aux` zeigt dann, welcher Monitor auf welchem
    # Port laeuft, und ein Monitor auf einem zweiten Port kann ueber sein Muster
    # gezielt gefunden werden. Der Befehl bleibt `python -m slotmon.server`,
    # genau das, was auch von Hand laeuft. `-B`, weil der Monitor nicht in den
    # Repo-Baum schreiben darf - __pycache__ wuerde den Arbeitsbaum verwahren.
    nohup bash -c 'exec -a "$0" "$1" -B -m slotmon.server' \
        "slotmon-server:$PORT" "$PYTHON" >>"$MONITOR_LOG" 2>&1 &
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
            echo "[slotmon] FAILED: der Monitor ist nach ${waited}s mit Exit $rc gestorben." >&2
            echo "[slotmon] letzte 20 Zeilen aus $MONITOR_LOG:" >&2
            tail -n 20 "$MONITOR_LOG" >&2 || true
            rm -f "$PIDFILE"
            return 1
        fi
        if curl -sf -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null; then
            echo "[slotmon] laeuft auf http://127.0.0.1:$PORT (PID $child, Log: $MONITOR_LOG)"
            return 0
        fi
        if [ "$waited" -ge "$START_WAIT" ]; then
            echo "[slotmon] ERROR: nach ${START_WAIT}s keine Health-Antwort auf http://127.0.0.1:$PORT/health" >&2
            tail -n 20 "$MONITOR_LOG" >&2 || true
            # Aufraeumen. Nur der Prozess aus diesem Lauf - ein stehen
            # gelassener, halb gestarteter Monitor wuerde beim naechsten
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
        echo "[slotmon] laeuft nicht"
        rm -f "$PIDFILE"
        return 0
    fi

    # Zuerst die PID aus der Datei: gezielt, ohne den Warteweg ueber `pgrep`.
    local pid pids
    pid=$(read_pidfile || true)
    if [ -n "$pid" ] && owned_by_us "$pid"; then
        echo "[slotmon] stoppe PID $pid aus $PIDFILE"
        kill_one "$pid"
    else
        pids=$(monitor_pids)
        echo "[slotmon] stoppe PID(s) $pids (kein brauchbarer Eintrag in $PIDFILE)"
        for pid in $pids; do
            kill_one "$pid"
        done
    fi

    # Sicherheitsnetz fuer Prozesse ohne (richtigen) Eintrag in der PID-Datei:
    # ein zweiter Monitor, oder einer, der zwischen Datei und Prozessliste
    # aufgetaucht ist.
    #
    # Hier stand vorher `pkill -TERM -f "$PATTERN"`. Das ist trotz Muster ein
    # Massaker: `pgrep -f` sieht die ganze Kommandozeile, also trifft es jede
    # Shell, die `slotmon.server` nur irgendwo nennt - die Shell, in der
    # `stop` getippt wurde, eingeschlossen. `signal_monitors` filtert vorher
    # auf echte Aufrufe von `-m slotmon.server`.
    if is_running; then
        signal_monitors TERM || true
        local waited=0
        while is_running && [ "$waited" -lt "$STOP_GRACE" ]; do
            sleep 1
            waited=$((waited + 1))
        done
    fi
    if is_running; then
        echo "[slotmon] nach ${STOP_GRACE}s noch aktiv: $(monitor_pids)- sende KILL" >&2
        signal_monitors KILL || true
        sleep 1
    fi

    rm -f "$PIDFILE"
    if is_running; then
        echo "[slotmon] ERROR: $(monitor_pids) laeuft noch nach dem KILL" >&2
        return 1
    fi
    echo "[slotmon] gestoppt"
}

status() {
    # `status` liest. Es startet nichts und loest keinen Takt aus - die Frage
    # "laeuft er" beantwortet `pgrep`, der Zustand kommt aus dem letzten
    # `tick()`, den der Monitor selbst geschrieben hat.
    if ! is_running; then
        echo "[slotmon] laeuft nicht"
        return 1
    fi

    echo "[slotmon] laeuft (PID $(monitor_pids), Port $PORT)"
    if ! curl -sf -m 5 "http://127.0.0.1:$PORT/api/state" 2>/dev/null | "$PYTHON" -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    print("[slotmon] /api/state antwortet nicht mit JSON")
    raise SystemExit(0)
if not isinstance(data, dict):
    print("[slotmon] /api/state antwortet nicht mit einem JSON-Objekt")
    raise SystemExit(0)

slot = data.get("slot") or {}
queue = data.get("queue") or {}
rid = slot.get("rid")

# None heisst "nicht gemessen", nicht 0. Eine erfundene Zahl ist schlimmer als
# ein "unbekannt" - im Zweifel ist der Zustand eben nicht bekannt.
print("[slotmon] Slot={} rid={} system={} wartend={} altbestand={}".format(
    slot.get("state") or "unbekannt",
    rid[:8] if isinstance(rid, str) and rid else "-",
    slot.get("system") or slot.get("ip_port") or "unbekannt",
    "unbekannt" if queue.get("waiting") is None else queue.get("waiting"),
    "unbekannt" if slot.get("legacy") is None else slot.get("legacy"),
))
'; then
        echo "[slotmon] /api/state liefert keine verwertbare Antwort (laeuft der Monitor wirklich?)"
    fi
}

# Vordergrund fuer systemd: systemd ueberwacht den Prozess selbst und startet
# ihn bei Absturz neu. Im Hintergrund wuerde `start` sofort zurueckkehren, und
# systemd saehe einen Erfolg ohne ueberwachbaren Prozess.
start_systemd() {
    cd "$REPO"
    export PYTHONUNBUFFERED=1 PORT API MONITOR_LOG
    exec -a "slotmon-server:$PORT" "$PYTHON" -B -m slotmon.server
}

case "${1:-}" in
    start)          start ;;
    start-systemd)  start_systemd ;;
    stop)           stop ;;
    status)         status ;;
    restart)        stop; start ;;
    *)              usage ;;
esac