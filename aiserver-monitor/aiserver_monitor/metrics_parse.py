"""Metrik-Parser fuer das Prometheus-Textformat und nvidia-smi-CSV.

Reine Funktionen ohne I/O: Der Metrik-Sammler holt die Rohdaten und baut mit
:func:`extract_sample` das Sample-Dict fuer History, SSE und UI. Konvention:
"Nicht gemessen" heisst None bzw. fehlender Key, nie 0.
"""

import re
from dataclasses import dataclass

#: Zuordnung Sample-Key -> sglang-Metrikname (am 2026-10-13 verifiziert).
METRIC_NAMES = {
    "running": "sglang:num_running_reqs",
    "waiting": "sglang:num_queue_reqs",
    "throughput": "sglang:gen_throughput",
    "token_usage": "sglang:token_usage",
    "max_tokens": "sglang:max_total_num_tokens",
}

#: Prometheus-Textformat: ``name{label="x",...} wert [zeitstempel]``. Der
#: Label-Block ist optional, der Name darf ``:`` enthalten (sglang-Namespace);
#: ein optionaler Zeitstempel hinter dem Wert wird ignoriert.
_SAMPLE_LINE = re.compile(r"^(?P<name>[\w:]+)(?:\{[^}]*\})?\s+(?P<value>\S+)")


def parse_prometheus(text: str) -> dict[str, float]:
    """Summiert die Samples je Metrikname; Labels werden entfernt.

    Kommentar- (``#``) und Leerzeilen werden ignoriert. Werte, die ``float``
    nicht lesen kann, werden verworfen. Metriken, die im Text nicht vorkommen,
    fehlen im Ergebnis ("nicht gemessen", nie 0.0).
    """
    totals: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_LINE.match(line)
        if match is None:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        name = match.group("name")
        totals[name] = totals.get(name, 0.0) + value
    return totals


@dataclass
class GpuSample:
    """Ein nvidia-smi-Messwert; nicht lesbare Felder bleiben None."""

    util_pct: int | None
    vram_used_mb: int | None
    vram_total_mb: int | None


def _feld_zu_int(feld: str) -> int | None:
    """Ein CSV-Feld als int; unlesbare Felder (z. B. ``[N/A]``) werden None."""
    try:
        return int(float(feld))
    except ValueError:
        return None


def parse_nvidia_smi(csv_text: str) -> GpuSample:
    """Erste Zeile der nvidia-smi-CSV (noheader, nounits) als GpuSample.

    Erwartet drei Felder: Auslastung in Prozent, VRAM belegt MB, VRAM gesamt
    MB. Leere Ausgabe, falsche Spaltenzahl oder unlesbare Felder ergeben None
    ("nicht gemessen", nie 0).
    """
    zeilen = csv_text.strip().splitlines()
    if not zeilen:
        return GpuSample(None, None, None)
    felder = [feld.strip() for feld in zeilen[0].split(",")]
    if len(felder) != 3:
        return GpuSample(None, None, None)
    return GpuSample(
        _feld_zu_int(felder[0]),
        _feld_zu_int(felder[1]),
        _feld_zu_int(felder[2]),
    )


def extract_sample(parsed: dict[str, float], gpu: GpuSample, ts: float,
                   available: bool) -> dict:
    """Baut das Sample-Dict fuer History, SSE und UI.

    Nur gemessene Werte landen im dict: fehlende Metriken und nicht lesbare
    GPU-Felder fehlen als Key ("nicht gemessen", nie 0). ``running``/``waiting``
    sind Zaehler und werden als int gefuehrt; ``max_tokens`` ist ein Extra-Key
    ohne History-Spalte (dient der Kontextanzeige im UI).
    """
    sample: dict = {"ts": ts, "available": bool(available)}
    for key, metric in METRIC_NAMES.items():
        if metric in parsed:
            value = parsed[metric]
            sample[key] = int(value) if key in ("running", "waiting") else value
    for key, value in (("gpu_util", gpu.util_pct),
                       ("vram_used_mb", gpu.vram_used_mb),
                       ("vram_total_mb", gpu.vram_total_mb)):
        if value is not None:
            sample[key] = value
    return sample
