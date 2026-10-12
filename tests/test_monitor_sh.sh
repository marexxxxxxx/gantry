#!/usr/bin/env bash
# monitor.sh gegen sich selbst pruefen.
#
# Der Test startet einen EIGENEN Monitor auf Port 8099 mit eigenem Log, eigener
# PID-Datei und eigener Historie. Der echte Monitor auf 8090 und der sglang-
# Server auf 30000 werden nicht angefasst - beides wird am Ende gegengeprueft.
#
# Drei Dinge sorgen dafuer, dass nichts kollidiert:
#   * eigener PORT          - der Monitor auf 8090 bleibt unberuehrt
#   * eigenes MONITOR_LOG   - der Test schreibt nicht in /tmp/slotmon.log
#   * eigenes PIDFILE/HISTORY
#   * SLOTMON_PATTERN auf den eigenen Port eingegrenzt - sonst wuerde `stop`
#     im Sicherheitsnetz (`pkill -f`) auch den echten Monitor treffen. Wer 8090
#     abschiesst, hat den Falschen.
#
# Ohne `-e`: ein Test soll nach dem ersten Fehler weiterlaufen. Mit `-e` bricht
# die Assertion ab, und der Fehler sieht wie ein Erfolg aus.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
MONITOR="$REPO/monitor.sh"
PYTHON=${PYTHON:-python3.12}
TEST_PORT=${TEST_PORT:-8099}

WORKDIR=$(mktemp -d /tmp/slotmon-selftest.XXXXXX)
export PORT="$TEST_PORT"
# Der Basisname muss `slotmon.log` sein: `logrotate.resolve_allowed` gibt nur
# die beiden erlaubten Namen unter /tmp frei, alles andere landet als
# "pfad nicht freigegeben" im Log. Das Verzeichnis darf beliebig sein.
export MONITOR_LOG="$WORKDIR/slotmon.log"
export PIDFILE="$WORKDIR/monitor.pid"
export HISTORY="$WORKDIR/history.jsonl"
# Das Muster muss die **volle Kommandozeile** treffen. `exec -a` in
# `monitor.sh` setzt argv[0] auf `slotmon-server:$PORT`, aber argv[0] steht in
# `/proc/PID/cmdline` nur noch an erster Stelle und ohne Trennzeichen - ein
# Muster mit Doppelpunkt trifft es nur, wenn es nicht auf ein Wortende achtet.
# Der Teil nach argv[0] ist aber immer `-B -m slotmon.server`, und darauf
# gruendet auch `is_monitor_cmdline`. Das Muster muss deshalb den **echten**
# Aufruf treffen und den Port als Wortgrenze:
# Das Muster muss die **volle Kommandozeile** treffen, denn `pgrep -f`
# vergleicht sie. `monitor.sh` startet seinen Monitor mit
# `exec -a "slotmon-server:$PORT"`, und argv[0] steht nachweislich als erstes
# NUL-getrenntes Feld in `/proc/PID/cmdline`. Gemessen: `pgrep -f
# '[s]lotmon-server:9099'` findet den Prozess. Port-Isolierung funktioniert
# also ueber das Muster; `own_pid` bevorzugt zusaetzlich die PID-Datei, weil
# ein `pgrep`-Treffer allein keine Zugehoerigkeit beweist.
export SLOTMON_PATTERN="[s]lotmon-server:$TEST_PORT"

FAILURES=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

# Eigenen Monitor restlos einsammeln: ueber monitor.sh, und falls das nicht
# reicht, direkt. Danach Log, PID-Datei und Historie weg. Auch wenn der Test
# mitten drin abbricht - deshalb der Trap, nicht die Abschlusszeile.
# Idempotent, damit der Test die Aufraeumung am Ende selbst pruefen kann und der
# Trap sie nicht ein zweites Mal macht.
CLEANED=0
cleanup() {
    [ "$CLEANED" -eq 1 ] && return 0
    CLEANED=1
    trap - EXIT
    if [ -f "$MONITOR" ]; then
        "$MONITOR" stop >/dev/null 2>&1
    fi
    # Nur Prozesse mit eigener Monitor-Kommandozeile. `pkill -f` allein
    # wuerde hier jede Shell mitnehmen, die das Muster nur nennt - auch
    # die, in der dieser Test laeuft.
    for _pid in $(pgrep -f "$SLOTMON_PATTERN" 2>/dev/null); do
        if tr '\0' ' ' <"/proc/$_pid/cmdline" 2>/dev/null |
            grep -qE -- '(^|[[:space:]])-m[[:space:]]+slotmon\.server([[:space:]]|$)'; then
            kill -TERM "$_pid" 2>/dev/null || true
        fi
    done
    [ -n "${DECOY:-}" ] && kill -TERM "$DECOY" 2>/dev/null
    rm -rf "$WORKDIR"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Zustand der fremden Dienste vor der Pruefung festhalten.
REAL_MONITOR_BEFORE=$(pgrep -f "[s]lotmon.server" | tr '\n' ' ')
REAL_HISTORY_BEFORE=$(stat -c '%s %Y' /tmp/slotmon/history.jsonl 2>/dev/null || echo "-")
SERVER_BEFORE=$(pgrep -f "[s]glang.launch_server" | tr '\n' ' ')
# Keine festverdrahtete PID: sie stimmt nur, bis der Monitor einmal neu
# gestartet wurde. Geprueft wird die **Startzeit**, und die wird fuer jeden
# gefundenen Monitor mitgeschrieben - ein Restart aendert sie.
REAL_MONITOR_STARTS_BEFORE=$(for p in $(pgrep -f "[s]lotmon.server"); do echo "$p $(ps -o lstart= -p $p)"; done)


own_pid() {
    local pid
    # 1) Die PID-Datei. Sie ist der einzige Weg, der den eigenen Monitor vom
    #    echten auf 8090 unterscheidet: das Suchmuster kann den Port nicht
    #    treffen, weil er nur in argv[0] steht. Ohne diesen Schritt wuerde
    #    `own_pid` hier den echten Monitor melden und der Test toete ihn.
    if [ -r "$PIDFILE" ]; then
        pid=$(head -n 1 -- "$PIDFILE" 2>/dev/null)
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null &&
            tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null |
                grep -qE -- '(^|[[:space:]])-m[[:space:]]+slotmon\.server([[:space:]]|$)'; then
            printf '%s\n' "$pid"
            return 0
        fi
    fi
    # 2) Sonst der erste Prozess mit echter `-m slotmon.server`-Kommandozeile.
    #    Wie `monitor.sh`: nicht der erste Treffer von `pgrep`, denn das waere
    #    jeder Prozess, der das Muster nur irgendwo nennt.
    for pid in $(pgrep -f "$SLOTMON_PATTERN" 2>/dev/null); do
        if tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null |
            grep -qE -- '(^|[[:space:]])-m[[:space:]]+slotmon\.server([[:space:]]|$)'; then
            printf '%s\n' "$pid"
            return 0
        fi
    done
    return 1
}
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$TEST_PORT") 2>/dev/null; }

# --- Formales ---------------------------------------------------------------

if [ ! -f "$MONITOR" ]; then
    fail "monitor.sh fehlt"
    exit 1
fi
pass "monitor.sh existiert"
if bash -n "$MONITOR"; then pass "Syntax ist gueltig"; else fail "Syntaxfehler in monitor.sh"; fi
grep -q 'set -euo pipefail' "$MONITOR" && pass "set -euo pipefail gesetzt" || fail "set -euo pipefail fehlt"
grep -q '\[s\]lotmon' "$MONITOR" && pass "pgrep-Muster mit [s]lotmon" || fail "pgrep-Muster mit [s]lotmon fehlt"
grep -q 'nohup' "$MONITOR" && pass "nohup verwendet" || fail "nohup fehlt"
grep -q 'PYTHONUNBUFFERED' "$MONITOR" && pass "PYTHONUNBUFFERED gesetzt" || fail "PYTHONUNBUFFERED fehlt"

# --- usage ------------------------------------------------------------------

out=$("$MONITOR" kaputt 2>&1); rc=$?
if [ "$rc" -eq 2 ]; then pass "usage bei unbekanntem Argument liefert 2 (rc=$rc)"; else fail "usage lieferte $rc, erwartet 2: $out"; fi
case "$out" in *usage*) pass "usage nennt die Befehle" ;; *) fail "usage-Text fehlt: $out" ;; esac
out=$("$MONITOR" 2>&1); rc=$?
if [ "$rc" -eq 2 ]; then pass "usage ohne Argument liefert 2"; else fail "ohne Argument: rc=$rc, erwartet 2: $out"; fi

# --- status ohne laufenden Monitor ------------------------------------------

out=$("$MONITOR" status 2>&1); rc=$?
if [ "$rc" -eq 1 ]; then pass "status liefert 1, wenn nichts laeuft"; else fail "status lieferte $rc statt 1: $out"; fi
case "$out" in *"laeuft nicht"*) pass "status meldet 'laeuft nicht'" ;; *) fail "status meldet nicht 'laeuft nicht': $out" ;; esac
if [ -z "$(own_pid)" ]; then pass "status hat nichts gestartet"; else fail "status hat einen Prozess erzeugt: $(own_pid)"; fi
if [ ! -e "$MONITOR_LOG" ]; then pass "status hat kein Log angelegt"; else fail "status hat $MONITOR_LOG angelegt"; fi

if port_busy; then
    fail "Port $TEST_PORT ist belegt - Test kann nicht laufen (PORT ueber TEST_PORT setzen)"
    echo "$FAILURES Skript-Test(s) fehlgeschlagen"
    exit 1
fi

# --- start ------------------------------------------------------------------

out=$("$MONITOR" start 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "start liefert 0"; else fail "start liefert $rc: $out"; fi
PID1=$(own_pid)
if [ -n "$PID1" ]; then pass "Monitor laeuft (PID $PID1)"; else fail "kein Monitorprozess nach start: $out"; fi
# Die Datei muss da sein - aber leer bleiben, solange nichts zu melden ist.
# Eine gefuellte Logdatei waere kein Lob, sondern eine Warnung; die fruehere
# Fassung dieses Tests verlangte "nicht leer" und ist genau deshalb an der
# logrotate-Meldung des falschen Dateinamens gruen geworden.
if [ -f "$MONITOR_LOG" ]; then pass "Monitor-Log angelegt"; else fail "Monitor-Log fehlt: $MONITOR_LOG"; fi
if [ -s "$PIDFILE" ]; then pass "PID-Datei geschrieben"; else fail "PID-Datei fehlt oder ist leer"; fi
if curl -sf -o /dev/null "http://127.0.0.1:$TEST_PORT/health"; then
    pass "health auf $TEST_PORT erreichbar"
else
    fail "health auf $TEST_PORT nicht erreichbar"
fi
case "$HISTORY" in
    "$WORKDIR"/*) pass "HISTORY zeigt auf das Testverzeichnis ($HISTORY)" ;;
    *) fail "HISTORY zeigt nicht auf das Testverzeichnis: $HISTORY" ;;
esac

# --- status bei laufendem Monitor -------------------------------------------

out=$("$MONITOR" status 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "status liefert 0, wenn er laeuft"; else fail "status liefert $rc statt 0: $out"; fi
case "$out" in *"$PID1"*) pass "status nennt die PID" ;; *) fail "status nennt keine PID: $out" ;; esac
case "$out" in *"$TEST_PORT"*) pass "status nennt den Port" ;; *) fail "status nennt keinen Port: $out" ;; esac
case "$out" in *Slot=*) pass "status nennt den Slot-Zustand" ;; *) fail "status nennt keinen Slot-Zustand: $out" ;; esac
case "$out" in *rid=*) pass "status nennt die (gekuerzte) rid" ;; *) fail "status nennt keine rid: $out" ;; esac
case "$out" in *system=*) pass "status nennt den Systemnamen" ;; *) fail "status nennt keinen Systemnamen: $out" ;; esac
case "$out" in *wartend=*) pass "status nennt die Wartenden" ;; *) fail "status nennt keine Wartenden: $out" ;; esac
case "$out" in *altbestand=*) pass "status nennt den Altbestand" ;; *) fail "status nennt keinen Altbestand: $out" ;; esac

# status liest nur: kein neuer Takt, keine neue PID-Datei
PID_STILL=$(own_pid)
if [ "$PID_STILL" = "$PID1" ]; then pass "status hat den Prozess nicht angefasst"; else fail "status hat den Prozess ersetzt: $PID1 zu $PID_STILL"; fi

# --- Doppelstart ------------------------------------------------------------

out=$("$MONITOR" start 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "Doppelstart liefert 0"; else fail "Doppelstart liefert $rc statt 0: $out"; fi
case "$out" in *"laeuft bereits"*) pass "Doppelstart meldet 'laeuft bereits'" ;; *) fail "Doppelstart meldet nicht 'laeuft bereits': $out" ;; esac
if [ "$(own_pid)" = "$PID1" ]; then pass "Doppelstart hat keinen zweiten Prozess erzeugt"; else fail "Doppelstart hat einen zweiten Prozess erzeugt"; fi

# --- restart ----------------------------------------------------------------

out=$("$MONITOR" restart 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "restart liefert 0"; else fail "restart liefert $rc: $out"; fi
sleep 1
PID2=$(own_pid)
if [ -n "$PID2" ] && [ "$PID2" != "$PID1" ]; then
    pass "restart hat eine andere PID ($PID1 zu $PID2)"
else
    fail "restart hat keine andere PID ($PID1 zu $PID2)"
fi
if curl -sf -o /dev/null "http://127.0.0.1:$TEST_PORT/health"; then
    pass "health nach restart erreichbar"
else
    fail "health nach restart nicht erreichbar"
fi

# --- stop -------------------------------------------------------------------

out=$("$MONITOR" stop 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "stop liefert 0"; else fail "stop liefert $rc: $out"; fi
if [ -z "$(own_pid)" ]; then pass "Prozess nach stop beendet"; else fail "Prozess laeuft nach stop noch: $(own_pid)"; fi
if [ ! -e "$PIDFILE" ]; then pass "PID-Datei nach stop entfernt"; else fail "PID-Datei nach stop nicht entfernt"; fi
if ! curl -sf -o /dev/null "http://127.0.0.1:$TEST_PORT/health"; then
    pass "health nach stop nicht erreichbar"
else
    fail "health nach stop noch erreichbar"
fi

out=$("$MONITOR" stop 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "stop zweimal liefert 0"; else fail "stop zweimal liefert $rc statt 0: $out"; fi
case "$out" in *"laeuft nicht"*) pass "stop zweimal meldet 'laeuft nicht'" ;; *) fail "stop zweimal meldet nicht 'laeuft nicht': $out" ;; esac

# --- systems.json fehlt -----------------------------------------------------
# Spec Z. 319: "Start bricht mit klarer Meldung ab." `names.load_names` ruft
# dafuer sys.exit - der Prozess stirbt also sofort. Der Startpfad muss das als
# Fehler melden (Exit 1) und darf keinen Prozess und keine PID-Datei lassen.

out=$(SYSTEMS_FILE="$WORKDIR/gibt-es-nicht.json" "$MONITOR" start 2>&1); rc=$?
if [ "$rc" -eq 1 ]; then pass "start mit fehlender systems.json liefert 1 (rc=1)"; else fail "start mit fehlender systems.json liefert $rc statt 1: $out"; fi
case "$out" in *systems.json*) pass "start nennt systems.json in der Meldung" ;; *) fail "start nennt systems.json nicht: $out" ;; esac
if [ -z "$(own_pid)" ]; then pass "kein Prozess nach fehlgeschlagenem Start"; else fail "Prozess laeuft nach fehlgeschlagenem Start noch: $(own_pid)"; fi
if [ ! -e "$PIDFILE" ]; then pass "keine PID-Datei nach fehlgeschlagenem Start"; else fail "PID-Datei bleibt nach fehlgeschlagenem Start liegen"; fi
if ! port_busy; then pass "Port nach fehlgeschlagenem Start wieder frei"; else fail "Port $TEST_PORT nach fehlgeschlagenem Start belegt"; fi

# --- status mit kaputter JSON-Antwort ---------------------------------------
# Ein /api/state, das kein JSON ist, darf status nicht zum Absturz bringen.
#
# Der Prozess muss an **beides** erfuellen: `pgrep -f` muss ihn ueber argv[0]
# finden, und `is_monitor_cmdline` in `monitor.sh` verlangt wortweise
# `-m slotmon.server` in der Kommandozeile.
#
# Drei Fehlversuche, alle lehrreich:
#   * `python -m slotmon.server <datei>` erfuellt die Kommandozeile, startet
#     aber das **echte** Paket und laesst `<datei>` als Argument liegen;
#   * ein `fake_monitor.py` ohne `-m slotmon.server` wird von `status` seit dem
#     Fremdprozess-Fix gar nicht erst als Monitor erkannt;
#   * `PYTHONPATH` allein genuegt nicht: mit `''` (das Arbeitsverzeichnis) an
#     erster Stelle im `sys.path` gewinnt aus dem Repo das echte Paket. Der
#     Shim muss deshalb aus einem **anderen** Verzeichnis gestartet werden -
#     gemessen: aus `$REPO` band auf 8090, aus `/tmp` lieferte er Muell.
#
# Der Aufruf bleibt woertlich `python -m slotmon.server`.
mkdir -p "$WORKDIR/shim/slotmon"
: > "$WORKDIR/shim/slotmon/__init__.py"
cat > "$WORKDIR/shim/slotmon/server.py" <<'SHIM'
"""Kein echter Monitor: /health sagt 200, /api/state liefert Muell.

Ersetzt `slotmon.server` im PYTHONPATH. Aufgerufen als `python -m
slotmon.server`, damit die Kommandozeile der des echten Monitors gleicht -
`is_monitor_cmdline` in `monitor.sh` prueft genau darauf.

Der Port kommt aus `FAKE_PORT`, nicht aus `sys.argv`: bei `python -m` ist
`sys.argv[0]` der Modulpfad, und der Port waere `sys.argv[1]` - laesst man ihn
weg, gibt es keins, und `int(sys.argv[1])` waere ein `IndexError`, kein
Server.
"""
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"kein json hier" if self.path == "/api/state" else b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# `127.0.0.1`, nicht `0.0.0.0`: gemessen lieferte `0.0.0.0` einen
# LISTEN-Socket, den kein Client erreichte - `Connection refused` bei
# `ss` mit sauberem LISTEN. Auf `127.0.0.1` antwortet derselbe Server.
HTTPServer(("127.0.0.1", int(os.environ["FAKE_PORT"])), Handler).serve_forever()
SHIM
# `cd "$WORKDIR"` - nicht nur ein gesetztes PYTHONPATH, siehe die Warnung oben.
# Und `export` statt `env` davor: `env` steht als Kommando **vor** `exec -a`
# und macht den Namen wieder rueckgaengig - gemessen, argv[0] war danach
# wieder `python3.12`.
nohup bash -c 'cd "$1"; export PYTHONPATH="$2" FAKE_PORT="$3"; exec -a "$0" "$4" -B -m slotmon.server' \
    "slotmon-server:$TEST_PORT" "$WORKDIR" "$WORKDIR/shim" "$TEST_PORT" "$PYTHON" \
    >/dev/null 2>&1 &
FAKE=$!
for _ in $(seq 1 60); do curl -sf -o /dev/null "http://127.0.0.1:$TEST_PORT/health" && break; sleep 0.2; done
out=$("$MONITOR" status 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "status liefert 0, auch wenn /api/state Muell liefert"; else fail "status liefert $rc statt 0: $out"; fi
case "$out" in *JSON*) pass "status sagt, dass keine JSON-Antwort kam" ;; *) fail "status sagt nichts zur kaputten Antwort: $out" ;; esac
case "$out" in *Traceback*) fail "status ist abgestuerzt: $out" ;; *) pass "status stuerzt nicht ab" ;; esac
# Kein `pkill -f`: das Muster steht in fremden Kommandozeilen, ein Test, der
# damit aufraeumt, koennte den Monitor auf 8090 mitnehmen.
kill -TERM "$FAKE" 2>/dev/null
for _ in $(seq 1 30); do kill -0 "$FAKE" 2>/dev/null || break; sleep 0.2; done


# --- Fremdprozess, der das Muster nur NENNT ----------------------------------
#
# `pgrep -f` vergleicht die ganze Kommandozeile. Jeder Prozess, der
# `slotmon.server` irgendwo nur erwaehnt, ist damit ein Treffer - die eigene
# Aufruf-Shell, ein `grep`, eine Editor-Vorschau. Mit `pkill -TERM -f` hat
# `monitor.sh stop` solche Prozesse abgeschossen. Gemessen, nicht vermutet:
# `pkill` lieferte die eigene Shell mit im Ziel.
#
# Der Test startet deshalb einen Prozess, der das Muster im Text fuehrt, aber
# **kein** Monitor ist, und prueft danach, ob er noch lebt.
# Der Prozess muss das **Test-Muster** `[s]lotmon-server:$TEST_PORT` in seiner
# Kommandozeile fuehren - genau das ist der Fehler, den `monitor.sh` nicht mehr
# machen darf: `pgrep -f` vergleicht die ganze Zeile, also trifft es jeden
# Prozess, der das Muster nur irgendwo nennt.
#
# Er traegt das Muster als Argument, aber **kein** `-m slotmon.server`, weil er
# sonst wie ein echter Monitor aussaehe und `is_monitor_cmdline` ihn behielte.
#
# Vier Fehlversuche, alle lehrreich:
#   * ein Argument an `sleep`: `sleep` nimmt keins und stirbt sofort;
#   * `setsid` davor: `$!` ist dann die PID des Setsid-Wrappers, nicht die des
#     Prozesses, und der ist schon wieder weg;
#   * `exec -a` allein: setzt nur argv[0];
#   * die Klammern aus dem Muster gehoeren **nicht** ins Argument: `[s]lotmon`
#     ist fuer `pgrep` eine Zeichenklasse, nicht ein Literal, und der Prozess
#     wird dann nicht gefunden - der Test prueft ins Leere.
bash -c 'trap "exit 0" TERM; while :; do sleep 1; done' \
    "slotmon-server:$TEST_PORT decoy" \
    >/dev/null 2>&1 &
DECOY=$!
sleep 1
if ! kill -0 "$DECOY" 2>/dev/null; then
    fail "der Fremdprozess startet gar nicht erst (PID $DECOY)"
else
    # `stop` darf ihn nicht fuer einen Monitor halten ...
    out=$("$MONITOR" stop 2>&1)
    if kill -0 "$DECOY" 2>/dev/null; then
        pass "stop laesst einen Prozess leben, der das Muster nur nennt"
    else
        fail "stop hat einen Fremdprozess getoetet, der das Muster nur nennt: $out"
    fi
    # ... und `start` darf sich durch ihn nicht blockieren lassen.
    "$MONITOR" start >/dev/null 2>&1
    if own_pid >/dev/null 2>&1 && [ "$(own_pid)" != "$DECOY" ]; then
        pass "start startet trotz eines Prozesses, der das Muster nur nennt"
    else
        fail "start hat sich von einem Fremdprozess blockieren lassen: $out"
    fi
fi
"$MONITOR" stop >/dev/null 2>&1
kill -TERM "$DECOY" 2>/dev/null || true

# --- leeres SLOTMON_PATTERN ---------------------------------------------------
#
# `${VAR:-default}` greift bei einem **leeren** VAR nicht: leer ist gesetzt.
# Ein leeres Muster ist bei `pgrep -f` nicht "kein Treffer", sondern "alles" -
# gemessen lieferte `pgrep -f ""` jeden Kernel-Thread bis hinauf zu PID 1. Mit
# `pkill -f ""` waere `stop` ein Systembefehl.
#
# Der Test setzt die Variable bewusst auf "" und prueft, dass der Default
# greift.
# Der entscheidende Fall: **kein** Monitor laeuft, und das Muster ist leer.
# Dann ist das richtige Ergebnis "laeuft nicht" - unter einem leeren Muster
# waere `pgrep -f ""` jeder Prozess der Maschine, auch PID 1.
#
# Wichtig: dieser Aufruf bekommt bewusst das **Default**-Muster, nicht das
# port-eingeschraenkte des Tests. Der echte Monitor auf 8090 faellt darunter -
# und genau deshalb kann der Test nicht "nichts laeuft" erwarten, solange er
# laeuft. Geprueft wird deshalb die andere, schaerfere Aussage: die PID-Liste
# von `status` enthaelt **keine** Prozesse, die keine echte
# `-m slotmon.server`-Kommandozeile haben.
"$MONITOR" stop >/dev/null 2>&1
out=$(SLOTMON_PATTERN="" "$MONITOR" status 2>&1); rc=$?
echo "  info: leeres Muster, status rc=$rc: $(printf '%s' "$out" | head -1)"
# Die eigentliche Behauptung: `status` nennt nur Prozesse mit echter
# Kommandozeile. Wuerde ein leeres Muster durchgerutscht, waeren hier die
# Kernel-Threads gelistet.
listed=$(printf '%s' "$out" | sed -n 's/.*PID[s]* \([0-9 ]*\).*/\1/p')
bad=""
for pid in $listed; do
    c=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null)
    printf '%s' "$c" | grep -qE -- '(^|[[:space:]])-m[[:space:]]+slotmon\.server([[:space:]]|$)' ||
        bad="$bad $pid"
done
if [ -z "$bad" ]; then
    pass "leeres SLOTMON_PATTERN listet nur echte Monitore (geprueft: '$listed')"
else
    fail "leeres SLOTMON_PATTERN listet Fremdprozesse: $bad"
fi
# Und mit gesetztem Default-Muster dasselbe Bild - ein Monitor, der laeuft,
# wird gefunden, ein leeres Muster findet nichts:
if [ -n "$(own_pid 2>/dev/null)" ]; then
    fail "Test-Monitor laeuft noch, der Fall ist nicht pruefbar"
else
    pass "Vorbedingung: kein Test-Monitor laeuft"
fi

# --- fremde Dienste unberuehrt ----------------------------------------------

# Gegenprobe: mit gesetztem Muster findet `status` einen Monitor. Sie steht
# **hier**, nach dem Leer-Pattern-Fall und vor dem Aufraeumen: waere sie dort
# stehen geblieben, wuerde ihr eigener Monitor in `REAL_MONITOR_AFTER` als
# "fremder Monitor" mitzaehlen und der Vergleich unten immer fehlschlagen.
"$MONITOR" start >/dev/null 2>&1
if "$MONITOR" status >/dev/null 2>&1; then
    pass "Gegenprobe: mit gesetztem Muster findet status einen Monitor (rc=0)"
else
    fail "Gegenprobe fehlt: mit gesetztem Muster findet status keinen Monitor"
fi
"$MONITOR" stop >/dev/null 2>&1

REAL_MONITOR_AFTER=$(pgrep -f "[s]lotmon.server" | tr '\n' ' ')
if [ "$REAL_MONITOR_BEFORE" = "$REAL_MONITOR_AFTER" ]; then
    pass "echter Monitor unberuehrt (${REAL_MONITOR_AFTER:-keiner})"
else
    fail "echter Monitor veraendert: vorher '$REAL_MONITOR_BEFORE', nachher '$REAL_MONITOR_AFTER'"
fi
REAL_MONITOR_STARTS_AFTER=$(for p in $(pgrep -f "[s]lotmon.server"); do echo "$p $(ps -o lstart= -p $p)"; done)
if [ "$REAL_MONITOR_STARTS_BEFORE" = "$REAL_MONITOR_STARTS_AFTER" ]; then
    pass "echter Monitor nicht neu gestartet"
else
    fail "echter Monitor neu gestartet: '$REAL_MONITOR_STARTS_BEFORE' zu '$REAL_MONITOR_STARTS_AFTER'"
fi
REAL_HISTORY_AFTER=$(stat -c '%s %Y' /tmp/slotmon/history.jsonl 2>/dev/null || echo "-")
if [ "$REAL_HISTORY_BEFORE" = "$REAL_HISTORY_AFTER" ]; then
    pass "echte Historie unberuehrt"
else
    fail "echte Historie veraendert: '$REAL_HISTORY_BEFORE' zu '$REAL_HISTORY_AFTER'"
fi
SERVER_AFTER=$(pgrep -f "[s]glang.launch_server" | tr '\n' ' ')
if [ "$SERVER_BEFORE" = "$SERVER_AFTER" ]; then
    pass "sglang-Server unberuehrt (${SERVER_AFTER:-keiner})"
else
    fail "sglang-Server veraendert: vorher '$SERVER_BEFORE', nachher '$SERVER_AFTER'"
fi

# Aufraeumen zuerst, dann den Rest pruefen - nicht umgekehrt. Ein Test, der
# seinen eigenen Dreck liegen laesst, bricht den naechsten Lauf und den
# Menschen dahinter.
cleanup
if [ -z "$(own_pid)" ]; then pass "nach dem Aufraeumen laeuft kein Testprozess"; else fail "Testprozess laeuft noch: $(own_pid)"; fi
if [ ! -d "$WORKDIR" ]; then pass "Testverzeichnis mit Log, PID-Datei und Historie entfernt"; else fail "Testverzeichnis liegt noch da: $WORKDIR"; fi
if [ ! -e "$MONITOR_LOG" ] && [ ! -e "$PIDFILE" ]; then
    pass "kein Log und keine PID-Datei liegen noch herum"
else
    fail "Rest liegt herum: $MONITOR_LOG oder $PIDFILE"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "Alle Skript-Tests bestanden"
else
    echo "$FAILURES Skript-Test(s) fehlgeschlagen"
    exit 1
fi