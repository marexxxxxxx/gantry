#!/usr/bin/env bash
# End-to-End-Pruefung des Dashboards: Fake-SGLang, Gateway, Playwright.
#
# Ablauf:
#   1. Vorbedingungen: Ports 39000/39001 frei, kein aiserver_monitor-Prozess;
#      der SGLang-Zustand wird fuer den Vergleich am Ende festgehalten.
#   2. Webapp bauen (npm run build; E2E_SKIP_BUILD=1 ueberspringt den Schritt).
#   3. Temp-Verzeichnis mit DB, Logs und PID-Datei; Umgebung fuer das Gateway
#      (127.0.0.1:39000, Upstream 127.0.0.1:39001, GPU-Stub).
#   4. Fake-SGLang starten und auf /health warten (Deadline 15 s).
#   5. Gateway per run.sh start starten und /healthz nachpruefen.
#   6. Playwright-Spezifikation ausfuehren (dashboard.spec.py).
#   7. Aufraeumen im Trap: Gateway stoppen, Fake beenden, Prozess- und
#      SGLang-Zustand pruefen, Temp-Verzeichnis loeschen (E2E_KEEP_TMP=1
#      behaelt es). Bei Fehlschlag gibt es die letzten Logzeilen.
#
# Die echten Dienste auf 30000 (Gateway) und 30001 (SGLang) bleiben unberuehrt:
# der Lauf benutzt ausschliesslich die Testports 39000/39001.
#
# Umgebung (alles mit Default):
#   E2E_PORT=39000 E2E_FAKE_PORT=39001 PYTHON=/home/user/gantry/.venv/bin/python
#   EXE=/home/user/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome
#   UV=uv NPM=npm E2E_SKIP_BUILD=0 E2E_KEEP_TMP=0
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

E2E_PORT="${E2E_PORT:-39000}"
E2E_FAKE_PORT="${E2E_FAKE_PORT:-39001}"
BASE="http://127.0.0.1:${E2E_PORT}"
FAKE_BASE="http://127.0.0.1:${E2E_FAKE_PORT}"
PYTHON="${PYTHON:-/home/user/gantry/.venv/bin/python}"
UV="${UV:-uv}"
NPM="${NPM:-npm}"
EXE="${EXE:-/home/user/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome}"

# Vor dem Trap gesetzt, damit die Aufraeumung auch nach einem fruehen
# Abbruch (z. B. Vorbedingung verletzt) unbekannte Variablen kennt.
WORK=""
FAKE_PID=""
RUN_LOG=""
GATEWAY_LOG=""
FAKE_LOG=""
SERVER_VORHER=""
# Erst stoppen, wenn dieses Skript wirklich ein Gateway gestartet hat: der
# Trap darf nie einen fremden Prozess anfassen.
GATEWAY_GESTARTET=0

abbrich() {
    echo "[e2e] ABBRUCH: $1" >&2
    exit 1
}

# Ist der TCP-Port lokal belegt? (Kein nc noetig, bash kann /dev/tcp.)
port_frei() {
    ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# Echte Gateway-Prozesse: `pgrep -f` vergleicht die ganze Kommandozeile und
# wuerde auch jede Shell treffen, die das Muster nur nennt (z. B. ein
# Wrapper-Skript). Deshalb derselbe zweite Filter wie in run.sh.
monitor_pids() {
    local pid
    for pid in $(pgrep -f '[a]iserver_monitor' 2>/dev/null || true); do
        if tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null |
            grep -qE -- '(^|[[:space:]])-m[[:space:]]+aiserver_monitor([[:space:]]|$)'; then
            printf '%s ' "$pid"
        fi
    done
}

# Lebt die PID und ist sie kein Zombie? (kill -0 allein reicht nicht: ein
# beendeter Kindprozess bleibt bis zum wait als Zombie in der Prozessliste.)
alive() {
    local stat
    kill -0 "$1" 2>/dev/null || return 1
    stat=$(ps -o stat= -p "$1" 2>/dev/null) || return 1
    if [[ $stat == Z* ]]; then
        return 1
    fi
    return 0
}

# Fake-SGLang beenden: TERM, 5 s Gnade, dann KILL; danach einsammeln.
fake_beenden() {
    [ -n "$FAKE_PID" ] || return 0
    if alive "$FAKE_PID"; then
        kill -TERM "$FAKE_PID" 2>/dev/null || true
        local waited=0
        while alive "$FAKE_PID" && [ "$waited" -lt 5 ]; do
            sleep 1
            waited=$((waited + 1))
        done
        if alive "$FAKE_PID"; then
            echo "[e2e] Fake-SGLang reagiert nicht auf TERM, sende KILL" >&2
            kill -KILL "$FAKE_PID" 2>/dev/null || true
        fi
    fi
    wait "$FAKE_PID" 2>/dev/null || true
    FAKE_PID=""
}

# Aufraeumen in der Reihenfolge des Rulings; der Exit-Code bleibt erhalten.
cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    echo "[e2e] Aufraeumen (Gateway, Fake, Temp-Verzeichnis) ..."

    # 1. Gateway stoppen (PIDFILE und RUN_LOG stammen aus diesem Lauf).
    if [ "$GATEWAY_GESTARTET" -eq 1 ] && [ -x "$ROOT/run.sh" ]; then
        bash run.sh stop >/dev/null 2>&1 || true
    fi

    # 2. Fake-SGLang beenden.
    fake_beenden

    # 3. Kein aiserver_monitor-Prozess mehr uebrig?
    local uebrig
    uebrig=$(monitor_pids)
    if [ -n "$uebrig" ]; then
        echo "[e2e] ERROR: aiserver_monitor laeuft nach dem Aufraeumen noch: $uebrig" >&2
        rc=1
    fi

    # 4. SGLang-Zustand vergleichen - erst nach dem Teardown, damit der Fake
    #    nicht selbst als SGLang-Treffer zaehlt.
    local nachher
    nachher=$( { pgrep -f '[s]glang' 2>/dev/null || true; } | tr '\n' ' ')
    if [ "$SERVER_VORHER" != "$nachher" ]; then
        echo "[e2e] ERROR: SGLang-Zustand veraendert: vorher '$SERVER_VORHER', nachher '$nachher'" >&2
        rc=1
    elif [ "$rc" -eq 0 ]; then
        echo "[e2e] SGLang-Zustand unberuehrt (${nachher:-keiner})"
    fi

    # 5. Bei Fehlschlag die letzten Logzeilen zum Diagnostizieren zeigen.
    #    Das Anwendungs-Log traegt die Ursachen (uvicorn schreibt in die
    #    rotierende Datei, das run-Log bleibt daher meist leer).
    if [ "$rc" -ne 0 ]; then
        echo "[e2e] FEHLGESCHLAGEN (Exit $rc). Letzte 40 Zeilen $RUN_LOG:" >&2
        tail -n 40 "$RUN_LOG" 2>/dev/null || true
        echo "[e2e] Letzte 40 Zeilen $GATEWAY_LOG:" >&2
        tail -n 40 "$GATEWAY_LOG" 2>/dev/null || true
        echo "[e2e] Letzte 40 Zeilen $FAKE_LOG:" >&2
        tail -n 40 "$FAKE_LOG" 2>/dev/null || true
    fi

    # 6. Temp-Verzeichnis entfernen; E2E_KEEP_TMP=1 behaelt es zum Debuggen.
    if [ -n "$WORK" ] && [ -d "$WORK" ]; then
        if [ "${E2E_KEEP_TMP:-0}" = "1" ]; then
            echo "[e2e] E2E_KEEP_TMP=1: $WORK bleibt liegen"
        else
            rm -rf -- "$WORK"
        fi
    fi

    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# --- 1. Vorbedingungen ------------------------------------------------------

if ! port_frei "$E2E_PORT"; then
    abbrich "Port $E2E_PORT ist belegt (Testport des Gateways muss frei sein)"
fi
if ! port_frei "$E2E_FAKE_PORT"; then
    abbrich "Port $E2E_FAKE_PORT ist belegt (Testport des Fake-SGLang muss frei sein)"
fi
if [ -n "$(monitor_pids)" ]; then
    abbrich "es laeuft schon ein aiserver_monitor-Prozess: $(monitor_pids)"
fi
[ -x "$PYTHON" ] || abbrich "Python nicht ausfuehrbar: $PYTHON"
[ -x "$EXE" ] || abbrich "Chromium nicht gefunden: $EXE (EXE setzen)"
command -v "$UV" >/dev/null || abbrich "uv nicht gefunden: $UV"
command -v "$NPM" >/dev/null || abbrich "npm nicht gefunden: $NPM (oder E2E_SKIP_BUILD=1 setzen)"

# Zustand des echten SGLang vor dem Lauf (Vergleich in cleanup).
SERVER_VORHER=$( { pgrep -f '[s]glang' 2>/dev/null || true; } | tr '\n' ' ')
echo "[e2e] SGLang vor dem Lauf: ${SERVER_VORHER:-keiner}"

# --- 2. Webapp bauen --------------------------------------------------------

if [ "${E2E_SKIP_BUILD:-0}" = "1" ]; then
    echo "[e2e] E2E_SKIP_BUILD=1: ueberspringe npm run build"
else
    echo "[e2e] Webapp bauen (npm run build)"
    ( cd webapp && "$NPM" run build )
fi

# --- 3. Temp-Verzeichnis und Umgebung ---------------------------------------

WORK=$(mktemp -d /tmp/aiserver-e2e.XXXXXX)
RUN_LOG="$WORK/run.log"
GATEWAY_LOG="$WORK/aiserver-monitor.log"
FAKE_LOG="$WORK/fake.log"

export AISERVER_MONITOR_LISTEN_HOST=127.0.0.1
export AISERVER_MONITOR_LISTEN_PORT="$E2E_PORT"
export AISERVER_MONITOR_UPSTREAM_BASE="$FAKE_BASE"
export AISERVER_MONITOR_GPU_CMD="$ROOT/tests/e2e/gpu_stub.sh"
export AISERVER_MONITOR_DB_PATH="$WORK/monitor.db"
export AISERVER_MONITOR_LOG_PATH="$GATEWAY_LOG"
export PIDFILE="$WORK/run.pid"
export AISERVER_MONITOR_RUN_LOG="$RUN_LOG"
# Kurze Wartezeiten: das Ruling verlangt keinen 20-s-Default, und ein
# haengendes `stop` soll den Lauf nicht ausbremsen.
export STOP_GRACE=5
export START_WAIT=20

# --- 4. Fake-SGLang starten -------------------------------------------------

echo "[e2e] Fake-SGLang starten auf $FAKE_BASE"
"$PYTHON" tests/e2e/fake_sglang.py >"$FAKE_LOG" 2>&1 &
FAKE_PID=$!

fake_bereit() {
    local waited=0
    while [ "$waited" -lt 15 ]; do
        if curl -sf -o /dev/null "$FAKE_BASE/health"; then
            return 0
        fi
        if ! alive "$FAKE_PID"; then
            echo "[e2e] ERROR: Fake-SGLang ist beim Start gestorben; Log:" >&2
            tail -n 40 "$FAKE_LOG" >&2 || true
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "[e2e] ERROR: Fake-SGLang /health nach 15s nicht erreichbar" >&2
    return 1
}

if ! fake_bereit; then
    exit 1
fi
echo "[e2e] Fake-SGLang ist bereit ($FAKE_BASE/health)"

# --- 5. Gateway starten -----------------------------------------------------

echo "[e2e] Gateway starten auf $BASE"
bash run.sh start
# Ab hier hat dieses Skript ein Gateway gestartet: der Trap muss es stoppen.
GATEWAY_GESTARTET=1
healthz_bereit() {
    local waited=0
    while [ "$waited" -lt 10 ]; do
        if curl -sf -o /dev/null "$BASE/healthz"; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "[e2e] ERROR: /healthz antwortet nach run.sh start nicht ($BASE/healthz)" >&2
    return 1
}
if ! healthz_bereit; then
    exit 1
fi
echo "[e2e] Gateway ist bereit ($BASE/healthz)"

# --- 6. Playwright-Spezifikation -------------------------------------------

echo "[e2e] Playwright-Spezifikation (dashboard.spec.py)"
set +e
( cd tests/e2e && EXE="$EXE" E2E_BASE_URL="$BASE" \
    "$UV" run --with playwright python dashboard.spec.py )
SPEC_RC=$?
set -e
if [ "$SPEC_RC" -ne 0 ]; then
    echo "[e2e] FEHLGESCHLAGEN: dashboard.spec.py endet mit Exit $SPEC_RC" >&2
    exit "$SPEC_RC"
fi

# --- 7. Abschluss -----------------------------------------------------------

echo
echo "[e2e] Alle Szenarien PASS"
exit 0
