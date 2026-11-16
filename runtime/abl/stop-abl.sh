#!/usr/bin/env bash
# Stoppt den Qwen3-14B-Abliterated-Ollama-Prozess aus start-abl.sh und gibt
# VRAM frei.
#
# Nur das PID-File zaehlt: der System-Ollama (11434) hat dieselbe
# Kommandozeile und darf hier nicht mit abgeschossen werden.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export FLINT_PIDFILE="${ABL_PIDFILE:-$ROOT/runtime/abl/abl.pid}"

exec bash "$ROOT/runtime/flint/stop-flint.sh"
