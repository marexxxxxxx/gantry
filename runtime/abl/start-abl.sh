#!/usr/bin/env bash
# Qwen3-14B-Abliterated als eigener Ollama-Prozess auf dem SGLang-Port (30001).
#
# Abliterated = Refusal-Richtung entfernt, gedacht fuer freie Chats ohne
# Zensur. Q4_K_M (~9 GB) passt mit 40k Kontext und einem Slot in 16 GB VRAM,
# wenn der KV-Cache quantisiert ist (q8_0, per ABL_KV_CACHE_TYPE aenderbar).
# Die Start-/Stop-Logik teilt sich das Skript mit Flint
# (runtime/flint/start-flint.sh) - nur die Env-Werte unterscheiden sich.
# Das Gateway (Port 30000) und alle Clients bleiben unveraendert.
#
# Der System-Ollama (Port 11434) laeuft als anderer Benutzer; OLLAMA_NOPRUNE
# macht seinen Modelstore hier lesbar. Vor dem Start werden dort geladene
# Modelle entladen, damit Qwen3-14B nicht mit ihnen um VRAM konkurriert.
#
# Modell vorher laden: bash runtime/abl/download-model.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export FLINT_HOST="${ABL_HOST:-127.0.0.1:30001}"
export FLINT_PIDFILE="${ABL_PIDFILE:-$ROOT/runtime/abl/abl.pid}"
export FLINT_LOG="${ABL_LOG:-/tmp/abl_server.log}"
export FLINT_MODELS="${ABL_MODELS:-/mnt/models/.ollama/models}"
export FLINT_CTX="${ABL_CTX:-40960}"
export FLINT_PARALLEL="${ABL_PARALLEL:-1}"
export FLINT_KEEP_ALIVE="${ABL_KEEP_ALIVE:--1}"
export FLINT_MODEL="${ABL_MODEL:-hf.co/bartowski/huihui-ai_Qwen3-14B-abliterated-GGUF:Q4_K_M}"
export FLINT_LABEL="abl"

# q8_0 halbiert den KV-Cache gegenueber f16 und laesst 40k Kontext in 16 GB
# VRAM zu. start-flint.sh reicht die Variable an `ollama serve` weiter.
export OLLAMA_KV_CACHE_TYPE="${ABL_KV_CACHE_TYPE:-q8_0}"

exec bash "$ROOT/runtime/flint/start-flint.sh"
