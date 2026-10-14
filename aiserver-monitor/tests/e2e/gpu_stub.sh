#!/usr/bin/env bash
# GPU-Stub fuer die E2E-Pruefung: der Sammler ruft AISERVER_MONITOR_GPU_CMD
# mit nvidia-smi-Argumenten auf und erwartet genau diese drei Felder
# (utilization.gpu, memory.used, memory.total - noheader, nounits). Feste
# Werte halten die Statistik deterministisch; echte Hardware wird nie
# abgefragt, der Aufruf ignoriert die uebergebenen Argumente.
echo "33, 4096, 16384"
