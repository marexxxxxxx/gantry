#!/usr/bin/env bash
# Stoppt den GPT-OSS-Ollama-Prozess aus start-gptoss.sh und gibt VRAM frei.
#
# Nur das PID-File zaehlt: der System-Ollama (11434) hat dieselbe
# Kommandozeile und darf hier nicht mit abgeschossen werden.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export FLINT_PIDFILE="${GPTOSS_PIDFILE:-$ROOT/runtime/gptoss/gptoss.pid}"

exec bash "$ROOT/runtime/flint/stop-flint.sh"
