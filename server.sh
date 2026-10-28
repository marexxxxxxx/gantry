#!/usr/bin/env bash
# SGLang-/Ollama-Modellserver starten/stoppen - ohne das Dashboard anzufassen.
#
#   bash server.sh start            # Qwen starten, wartet bis /health ok
#   bash server.sh start flint      # Flint (Ollama, 2 Slots) starten
#   bash server.sh start gptoss     # GPT-OSS 20B (Ollama, 64k) starten
#   bash server.sh switch gptoss    # von Qwen/Flint auf GPT-OSS wechseln
#   bash server.sh stop             # stoppen, GPU freigeben
#   bash server.sh status           # alle Backends und Health anzeigen
#   bash server.sh restart [flint|gptoss]  # stop, dann start
#
# Benutzt dieselbe Logik wie der Dashboard-Tab "Server": ist die
# systemd-User-Unit escha-server.service installiert, laufen start/stop ueber
# `systemctl --user`; sonst ueber runtime/sglang/start-penalty.sh bzw.
# stop-server.sh. Flint und GPT-OSS laufen als eigene Ollama-Prozesse.
# Das Gateway/Dashboard bleibt immer unberuehrt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON=python3
fi

# Das Paket liegt unter aiserver-monitor/; relative Pfade der Config (DB,
# Log) stimmen nur von dort. Die Repo-Wurzel findet serverctl selbst.
cd "$ROOT/aiserver-monitor"
exec "$PYTHON" -B -m aiserver_monitor.serverctl "$@"
