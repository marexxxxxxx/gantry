#!/usr/bin/env bash
# GPT-OSS 20B als eigener Ollama-Prozess auf dem SGLang-Port (30001).
#
# Fuer Hindsight-Batches: 64k Kontext, ein Slot (Anfragen reihen sich),
# Modell bleibt geladen. Die Start-/Stop-Logik teilt sich das Skript mit
# Flint (runtime/flint/start-flint.sh) - nur die Env-Werte unterscheiden
# sich. Das Gateway (Port 30000) und alle Clients bleiben unveraendert.
#
# Der System-Ollama (Port 11434) laeuft als anderer Benutzer; OLLAMA_NOPRUNE
# macht seinen Modelstore hier lesbar. Vor dem Start werden dort geladene
# Modelle entladen, damit GPT-OSS nicht mit ihnen um VRAM konkurriert.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export FLINT_HOST="${GPTOSS_HOST:-127.0.0.1:30001}"
export FLINT_PIDFILE="${GPTOSS_PIDFILE:-$ROOT/runtime/gptoss/gptoss.pid}"
export FLINT_LOG="${GPTOSS_LOG:-/tmp/gptoss_server.log}"
export FLINT_MODELS="${GPTOSS_MODELS:-/mnt/models/.ollama/models}"
export FLINT_CTX="${GPTOSS_CTX:-65536}"
export FLINT_PARALLEL="${GPTOSS_PARALLEL:-1}"
export FLINT_KEEP_ALIVE="${GPTOSS_KEEP_ALIVE:--1}"
export FLINT_MODEL="${GPTOSS_MODEL:-gpt-oss:20b}"
export FLINT_LABEL="gptoss"

exec bash "$ROOT/runtime/flint/start-flint.sh"
