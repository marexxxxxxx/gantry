#!/usr/bin/env bash
# run.sh gegen sich selbst pruefen.
#
# Der Test startet ein EIGENES Gateway auf Port 39055 gegen einen Upstream auf
# 39099, an dem absichtlich nichts lauscht. Der echte SGLang auf 30001 und der
# spaetere Gateway-Port 30000 werden nie angefasst - der SGLang-Zustand wird am
# Ende gegengeprueft.
#
# Sechs Dinge halten die Testinstanz von der Produktion fern:
#   * eigener Port (39055) ueber AISERVER_MONITOR_LISTEN_PORT
#   * eigener Upstream (39099) ueber AISERVER_MONITOR_UPSTREAM_BASE
#   * DB, Anwendungs-Log und Lauf-Log liegen im Temp-Verzeichnis
#   * Audio-Sidecar auf 39003 mit eigenem PID-File und Modellverzeichnis
#   * STOP_GRACE=5, damit `stop` im Fehlerfall nicht 20 s wartet
#   * START_WAIT=15
#
# Ohne `-e`: ein Test soll nach dem ersten Fehler weiterlaufen. Mit `-e` bricht
# die Assertion ab, und der Fehler sieht wie ein Erfolg aus.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJEKT=$(cd "$HERE/.." && pwd)
RUN="$PROJEKT/run.sh"

TEST_PORT=${TEST_PORT:-39055}
TEST_UPSTREAM=${TEST_UPSTREAM:-http://127.0.0.1:39099}

WORKDIR=$(mktemp -d /tmp/aiserver-monitor-test.XXXXXX)
# Die App liest ihre Konfiguration aus der Umgebung. Der Upstream auf 39099
# lauscht nicht: So ist ausgeschlossen, dass dieser Test den echten SGLang auf
# 30001 beruehrt. Das run-Log wird umgelenkt, damit /tmp/aiserver-monitor-run.log
# der spaeteren Produktion nicht ueberschrieben wird.
export AISERVER_MONITOR_LISTEN_PORT="$TEST_PORT"
export AISERVER_MONITOR_UPSTREAM_BASE="$TEST_UPSTREAM"
export AISERVER_MONITOR_DB_PATH="$WORKDIR/monitor.db"
export AISERVER_MONITOR_LOG_PATH="$WORKDIR/aiserver-monitor.log"
export PIDFILE="$WORKDIR/run.pid"
export AISERVER_MONITOR_RUN_LOG="$WORKDIR/run.log"
# Der Audio-Sidecar bekommt einen eigenen Port, ein eigenes PID-File und ein
# leeres Modellverzeichnis. Die Produktion auf 30003 und /mnt/models/audio
# bleibt damit unberuehrt; AUDIO_PRELOAD="" verhindert das Laden von Whisper.
export AISERVER_MONITOR_AUDIO_BASE="http://127.0.0.1:39003"
export AUDIO_PORT=39003
export AUDIO_PRELOAD=""
export AUDIO_MODELS_DIR="$WORKDIR/audio"
export AUDIO_PIDFILE="$WORKDIR/audio.pid"
export AUDIO_LOG="$WORKDIR/audio.log"
export AISERVER_MONITOR_CHATTERBOX_BASE="http://127.0.0.1:39004"
export CHATTERBOX_PORT=39004
export CHATTERBOX_PIDFILE="$WORKDIR/chatterbox.pid"
export CHATTERBOX_LOG="$WORKDIR/chatterbox.log"
export CHATTERBOX_VENV="$WORKDIR/chatterbox-venv"
# Qwen3-TTS bekommt ebenfalls eigene Pfade. Der Test startet den GPU-Worker
# nicht; er prueft nur Skripte, Syntax und die Status-/Usage-Ausgabe.
export AISERVER_MONITOR_QWEN_TTS_BASE="http://127.0.0.1:39005"
export QWEN_TTS_PORT=39005
export QWEN_TTS_PIDFILE="$WORKDIR/qwen-tts.pid"
export QWEN_TTS_LOG="$WORKDIR/qwen-tts.log"
export QWEN_TTS_VENV="$WORKDIR/qwen-tts-venv"
export QWEN_TTS_MODEL_DIR="$WORKDIR/qwen-tts-model"
# Audio8 bekommt eigene Pfade. Der Test startet den GPU-Worker nicht; er
# prueft nur Skripte, Syntax und die Status-/Usage-Ausgabe.
export AISERVER_MONITOR_AUDIO8_BASE="http://127.0.0.1:39006"
export AUDIO8_PORT=39006
export AUDIO8_PIDFILE="$WORKDIR/audio8.pid"
export AUDIO8_LOG="$WORKDIR/audio8.log"
export AUDIO8_VENV="$WORKDIR/audio8-venv"
export AUDIO8_MODEL_DIR="$WORKDIR/audio8-model"
export AUDIO8_REFERENCE_AUDIO="$WORKDIR/ref.wav"
export AUDIO8_REFERENCE_TEXT="$WORKDIR/ref.txt"
# Auch der Embedding-Sidecar braucht ein eigenes PID-File: `run.sh stop` ruft
# stop-embed.sh auf und darf den Produktions-Ollama auf 30002 nicht beenden.
export AISERVER_MONITOR_EMBED_BASE="http://127.0.0.1:39002"
export EMBED_PIDFILE="$WORKDIR/embed.pid"
export STOP_GRACE=5
export START_WAIT=15

FAILURES=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

# Eigenes Gateway restlos einsammeln - auch wenn der Test mitten drin
# abbricht, deshalb der Trap und nicht die Abschlusszeile. Idempotent, damit
# die Aufraeumung am Ende selbst geprueft werden kann und der Trap sie nicht
# ein zweites Mal macht.
CLEANED=0
cleanup() {
    [ "$CLEANED" -eq 1 ] && return 0
    CLEANED=1
    trap - EXIT
    if [ -f "$RUN" ]; then
        "$RUN" stop >/dev/null 2>&1 || true
    fi
    rm -rf "$WORKDIR"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$TEST_PORT") 2>/dev/null; }

# SGLang-Zustand vor der Pruefung festhalten. Diese Linie darf sich nicht
# aendern - sie beweist, dass der Test den Server nicht angefasst hat.
SERVER_BEFORE=$(pgrep -f "[s]glang" | tr '\n' ' ')

# --- Formales ---------------------------------------------------------------

if [ ! -f "$RUN" ]; then
    fail "run.sh fehlt"
    exit 1
fi
pass "run.sh existiert"

if bash -n "$RUN" 2>/dev/null; then pass "Syntax ist gueltig"; else fail "Syntaxfehler in run.sh"; fi
grep -q 'set -euo pipefail' "$RUN" && pass "set -euo pipefail gesetzt" || fail "set -euo pipefail fehlt"
grep -q '\[a\]iserver_monitor' "$RUN" && pass "pgrep-Muster mit [a]iserver_monitor" || fail "pgrep-Muster mit [a]iserver_monitor fehlt"
grep -q 'nohup' "$RUN" && pass "nohup verwendet" || fail "nohup fehlt"
grep -q 'healthz' "$RUN" && pass "Health-Poll auf /healthz" || fail "Health-Poll auf /healthz fehlt"
if [ -x "$RUN" ]; then pass "run.sh ist ausfuehrbar"; else fail "run.sh ist nicht ausfuehrbar (chmod +x fehlt)"; fi

for skript in "$PROJEKT/../runtime/chatterbox/setup.sh" \
              "$PROJEKT/../runtime/chatterbox/download-models.sh" \
              "$PROJEKT/../runtime/chatterbox/start-chatterbox.sh" \
              "$PROJEKT/../runtime/chatterbox/stop-chatterbox.sh" \
              "$PROJEKT/../runtime/audio/setup.sh" \
              "$PROJEKT/../runtime/audio/download-models.sh" \
              "$PROJEKT/../runtime/audio/start-audio.sh" \
              "$PROJEKT/../runtime/audio/stop-audio.sh" \
              "$PROJEKT/../runtime/audio/selftest.sh" \
              "$PROJEKT/../runtime/qwen-tts/setup.sh" \
              "$PROJEKT/../runtime/qwen-tts/download-models.sh" \
              "$PROJEKT/../runtime/qwen-tts/start-qwen-tts.sh" \
              "$PROJEKT/../runtime/qwen-tts/stop-qwen-tts.sh" \
              "$PROJEKT/../runtime/audio8/setup.sh" \
              "$PROJEKT/../runtime/audio8/download-models.sh" \
              "$PROJEKT/../runtime/audio8/make-reference.sh" \
              "$PROJEKT/../runtime/audio8/start-audio8.sh" \
              "$PROJEKT/../runtime/audio8/stop-audio8.sh"; do
    name=$(basename "$skript")
    if [ ! -f "$skript" ]; then
        fail "$name fehlt"
    elif ! bash -n "$skript" 2>/dev/null; then
        fail "Syntaxfehler in $name"
    elif [ ! -x "$skript" ]; then
        fail "$name ist nicht ausfuehrbar"
    else
        pass "$name: Syntax gueltig und ausfuehrbar"
    fi
done

out=$("$RUN" 2>&1); rc=$?
if [ "$rc" -eq 2 ]; then pass "usage liefert 2"; else fail "usage lieferte $rc statt 2: $out"; fi
case "$out" in *audio-setup*) pass "usage nennt audio-setup" ;; *) fail "usage nennt audio-setup nicht: $out" ;; esac
case "$out" in *audio-models*) pass "usage nennt audio-models" ;; *) fail "usage nennt audio-models nicht: $out" ;; esac
case "$out" in *qwen-tts-setup*) pass "usage nennt qwen-tts-setup" ;; *) fail "usage nennt qwen-tts-setup nicht: $out" ;; esac
case "$out" in *qwen-tts-models*) pass "usage nennt qwen-tts-models" ;; *) fail "usage nennt qwen-tts-models nicht: $out" ;; esac
case "$out" in *audio8-setup*) pass "usage nennt audio8-setup" ;; *) fail "usage nennt audio8-setup nicht: $out" ;; esac
case "$out" in *audio8-models*) pass "usage nennt audio8-models" ;; *) fail "usage nennt audio8-models nicht: $out" ;; esac

# --- Vorbedingung: nichts laeuft --------------------------------------------

if [ -n "$(pgrep -f '[a]iserver_monitor' 2>/dev/null)" ]; then
    fail "es laeuft schon ein aiserver_monitor - Test kann nicht starten"
    echo "$FAILURES Skript-Test(s) fehlgeschlagen"
    exit 1
fi
pass "Vorbedingung: kein aiserver_monitor laeuft"

# --- status ohne laufende Instanz -------------------------------------------

out=$("$RUN" status 2>&1); rc=$?
if [ "$rc" -eq 1 ]; then pass "status liefert 1, wenn nichts laeuft"; else fail "status lieferte $rc statt 1: $out"; fi
case "$out" in *"laeuft nicht"*) pass "status meldet 'laeuft nicht'" ;; *) fail "status meldet nicht 'laeuft nicht': $out" ;; esac
if [ ! -e "$PIDFILE" ]; then pass "status hat keine PID-Datei angelegt"; else fail "status hat $PIDFILE angelegt"; fi
if [ ! -e "$AISERVER_MONITOR_RUN_LOG" ]; then pass "status hat kein Lauf-Log angelegt"; else fail "status hat $AISERVER_MONITOR_RUN_LOG angelegt"; fi

# --- stop auf nicht laufender Instanz (idempotent) --------------------------

out=$("$RUN" stop 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "stop auf nicht laufender Instanz liefert 0"; else fail "stop lieferte $rc statt 0: $out"; fi
case "$out" in *"laeuft nicht"*) pass "stop meldet 'laeuft nicht'" ;; *) fail "stop meldet nicht 'laeuft nicht': $out" ;; esac

# --- Port frei? --------------------------------------------------------------

if port_busy; then
    fail "Port $TEST_PORT ist belegt - Test kann nicht laufen"
    echo "$FAILURES Skript-Test(s) fehlgeschlagen"
    exit 1
fi
pass "Port $TEST_PORT ist frei"

# --- audio-models --check ohne Modelle ---------------------------------------
# Das Modellverzeichnis ist leer: --check darf nichts herunterladen und muss
# mit Exit 1 enden.
out=$("$RUN" audio-models --check 2>&1); rc=$?
if [ "$rc" -eq 1 ]; then pass "audio-models --check liefert 1 bei leerem Verzeichnis"; else fail "audio-models --check lieferte $rc statt 1: $out"; fi
case "$out" in *missing*) pass "audio-models --check meldet fehlende Modelle" ;; *) fail "audio-models --check meldet keine fehlenden Modelle: $out" ;; esac

# --- Audio-Port frei? ---------------------------------------------------------
if (exec 3<>"/dev/tcp/127.0.0.1/39003") 2>/dev/null; then
    fail "Port 39003 ist belegt - Audio-Test kann nicht laufen"
    echo "$FAILURES Skript-Test(s) fehlgeschlagen"
    exit 1
fi
pass "Port 39003 ist frei"

# --- Audio-Sidecar direkt aus einem fremden Arbeitsverzeichnis ---------------
# start-audio.sh muss das Paket auch finden, wenn es nicht aus
# aiserver-monitor/ heraus aufgerufen wird (Doku nennt die Repo-Wurzel).
out=$( (cd "$WORKDIR" && "$PROJEKT/../runtime/audio/start-audio.sh") 2>&1 ); rc=$?
if [ "$rc" -eq 0 ]; then
    pass "start-audio.sh funktioniert aus fremdem Arbeitsverzeichnis"
else
    fail "start-audio.sh scheitert aus fremdem Arbeitsverzeichnis: $out"
fi
if curl -sf -o /dev/null "http://127.0.0.1:39003/healthz"; then
    pass "direkt gestarteter Audio-Sidecar antwortet"
else
    fail "direkt gestarteter Audio-Sidecar antwortet nicht"
fi
out=$("$PROJEKT/../runtime/audio/stop-audio.sh" 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then
    pass "stop-audio.sh stoppt den direkt gestarteten Sidecar"
else
    fail "stop-audio.sh lieferte $rc: $out"
fi

# --- start -------------------------------------------------------------------

out=$("$RUN" start 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "start liefert 0"; else fail "start liefert $rc: $out"; fi
PID1=$(head -n 1 -- "$PIDFILE" 2>/dev/null || true)
if [ -n "$PID1" ] && kill -0 "$PID1" 2>/dev/null; then
    pass "Prozess laeuft (PID $PID1)"
else
    fail "kein Prozess nach start: $out"
fi
if curl -sf -o /dev/null "http://127.0.0.1:$TEST_PORT/healthz"; then
    pass "healthz auf $TEST_PORT erreichbar"
else
    fail "healthz auf $TEST_PORT nicht erreichbar"
fi
if curl -sf -o /dev/null "http://127.0.0.1:39003/healthz"; then
    pass "Audio-Sidecar healthz auf 39003 erreichbar"
else
    fail "Audio-Sidecar healthz auf 39003 nicht erreichbar"
fi
if curl -sf -m 2 -o /dev/null "http://127.0.0.1:39006/healthz" 2>/dev/null; then
    fail "Audio8-Worker wurde von start unerwartet gestartet"
else
    pass "start startet den Audio8-Worker nicht"
fi
AUDIO_PID=$(head -n 1 -- "$AUDIO_PIDFILE" 2>/dev/null || true)
if [ -n "$AUDIO_PID" ] && kill -0 "$AUDIO_PID" 2>/dev/null; then
    pass "Audio-Sidecar-Prozess laeuft (PID $AUDIO_PID)"
else
    fail "kein Audio-Sidecar-Prozess nach start"
fi
if [ -n "$PID1" ] && tr '\0' ' ' <"/proc/$PID1/cmdline" 2>/dev/null |
    grep -qE -- '(^|[[:space:]])-m[[:space:]]+aiserver_monitor([[:space:]]|$)'; then
    pass "Prozess ist 'python -m aiserver_monitor'"
else
    fail "Prozess ist nicht 'python -m aiserver_monitor'"
fi
if [ -n "$(pgrep -f '[a]iserver_monitor' 2>/dev/null)" ]; then
    pass "pgrep findet die Instanz"
else
    fail "pgrep findet die Instanz nicht"
fi
if [ -f "$AISERVER_MONITOR_LOG_PATH" ]; then
    pass "Anwendungs-Log angelegt"
else
    fail "Anwendungs-Log fehlt: $AISERVER_MONITOR_LOG_PATH"
fi
if [ -f "$AISERVER_MONITOR_DB_PATH" ]; then
    pass "SQLite-Datei angelegt"
else
    fail "SQLite-Datei fehlt: $AISERVER_MONITOR_DB_PATH"
fi

# --- Doppelstart --------------------------------------------------------------

out=$("$RUN" start 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "Doppelstart liefert 0"; else fail "Doppelstart liefert $rc statt 0: $out"; fi
case "$out" in *"laeuft bereits"*) pass "Doppelstart meldet 'laeuft bereits'" ;; *) fail "Doppelstart meldet nicht 'laeuft bereits': $out" ;; esac
PID2=$(head -n 1 -- "$PIDFILE" 2>/dev/null || true)
if [ "$PID2" = "$PID1" ]; then pass "Doppelstart hat keinen zweiten Prozess erzeugt"; else fail "Doppelstart hat den Prozess ersetzt: $PID1 zu $PID2"; fi

# --- status bei laufender Instanz --------------------------------------------

out=$("$RUN" status 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "status liefert 0, wenn er laeuft"; else fail "status liefert $rc statt 0: $out"; fi
case "$out" in *"$PID1"*) pass "status nennt die PID" ;; *) fail "status nennt keine PID: $out" ;; esac
case "$out" in *"$TEST_PORT"*) pass "status nennt den Port" ;; *) fail "status nennt keinen Port: $out" ;; esac
case "$out" in *"Audio-Sidecar (CPU) laeuft"*) pass "status meldet den Audio-Sidecar" ;; *) fail "status meldet den Audio-Sidecar nicht: $out" ;; esac
case "$out" in *"Qwen3-TTS-Worker"*) pass "status meldet den Qwen3-TTS-Worker" ;; *) fail "status meldet den Qwen3-TTS-Worker nicht: $out" ;; esac
case "$out" in *"Audio8-TTS-Worker"*) pass "status meldet den Audio8-TTS-Worker" ;; *) fail "status meldet den Audio8-TTS-Worker nicht: $out" ;; esac
PID_STILL=$(head -n 1 -- "$PIDFILE" 2>/dev/null || true)
if [ "$PID_STILL" = "$PID1" ]; then pass "status hat den Prozess nicht angefasst"; else fail "status hat den Prozess ersetzt: $PID1 zu $PID_STILL"; fi

# --- stop ---------------------------------------------------------------------

out=$("$RUN" stop 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "stop liefert 0"; else fail "stop liefert $rc: $out"; fi
if [ -z "$(pgrep -f '[a]iserver_monitor' 2>/dev/null)" ]; then
    pass "kein aiserver_monitor-Prozess nach stop"
else
    fail "Prozess laeuft nach stop noch: $(pgrep -f '[a]iserver_monitor' 2>/dev/null | tr '\n' ' ')"
fi
if [ ! -e "$PIDFILE" ]; then pass "PID-Datei nach stop entfernt"; else fail "PID-Datei nach stop nicht entfernt"; fi
if ! curl -sf -o /dev/null "http://127.0.0.1:$TEST_PORT/healthz"; then
    pass "healthz nach stop nicht erreichbar"
else
    fail "healthz nach stop noch erreichbar"
fi
if ! curl -sf -o /dev/null "http://127.0.0.1:39003/healthz"; then
    pass "Audio-Sidecar nach stop nicht erreichbar"
else
    fail "Audio-Sidecar nach stop noch erreichbar"
fi
if [ ! -e "$AUDIO_PIDFILE" ]; then pass "Audio-PID-Datei nach stop entfernt"; else fail "Audio-PID-Datei nach stop nicht entfernt"; fi

out=$("$RUN" stop 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then pass "stop zweimal liefert 0"; else fail "stop zweimal liefert $rc statt 0: $out"; fi
case "$out" in *"laeuft nicht"*) pass "stop zweimal meldet 'laeuft nicht'" ;; *) fail "stop zweimal meldet nicht 'laeuft nicht': $out" ;; esac

# --- leeres AISERVER_MONITOR_PATTERN ------------------------------------------
# `${VAR:-default}` greift bei einem **leeren** VAR nicht: leer ist gesetzt.
# Ein leeres Muster ist bei `pgrep -f` kein "kein Treffer", sondern "alles".
# Der Wert wird deshalb abgewiesen und der Default benutzt - hier messbar:
# es laeuft nichts, also muss status 1 und "laeuft nicht" liefern.
out=$(AISERVER_MONITOR_PATTERN="" "$RUN" status 2>&1); rc=$?
if [ "$rc" -eq 1 ]; then
    pass "leeres AISERVER_MONITOR_PATTERN: status liefert 1 (Default-Muster greift)"
else
    fail "leeres AISERVER_MONITOR_PATTERN: status lieferte $rc statt 1: $out"
fi
case "$out" in *"laeuft nicht"*) pass "leeres Muster meldet 'laeuft nicht'" ;; *) fail "leeres Muster meldet nicht 'laeuft nicht': $out" ;; esac

# --- fremder Dienst unberuehrt -------------------------------------------------

SERVER_AFTER=$(pgrep -f "[s]glang" | tr '\n' ' ')
if [ "$SERVER_BEFORE" = "$SERVER_AFTER" ]; then
    pass "SGLang-Zustand unberuehrt (${SERVER_AFTER:-keiner})"
else
    fail "SGLang-Zustand veraendert: vorher '$SERVER_BEFORE', nachher '$SERVER_AFTER'"
fi

# Aufraeumen zuerst, dann den Rest pruefen - nicht umgekehrt. Ein Test, der
# seinen eigenen Dreck liegen laesst, bricht den naechsten Lauf und den
# Menschen dahinter.
cleanup
if [ -z "$(pgrep -f '[a]iserver_monitor' 2>/dev/null)" ]; then
    pass "nach dem Aufraeumen laeuft kein Testprozess"
else
    fail "Testprozess laeuft noch: $(pgrep -f '[a]iserver_monitor' 2>/dev/null | tr '\n' ' ')"
fi
if [ ! -d "$WORKDIR" ]; then pass "Testverzeichnis entfernt"; else fail "Testverzeichnis liegt noch da: $WORKDIR"; fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "Alle Skript-Tests bestanden"
else
    echo "$FAILURES Skript-Test(s) fehlgeschlagen"
    exit 1
fi
