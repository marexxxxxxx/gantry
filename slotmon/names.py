"""IP zu Anzeigename. Fehlende IP ist kein Fehler, kaputte Datei schon."""

import json
import sys


def load_names(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        sys.exit(f"[slotmon] systems.json fehlt: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"[slotmon] systems.json ist kaputt: {path} ({exc})")
    except OSError as exc:
        sys.exit(f"[slotmon] systems.json nicht lesbar: {path} ({exc})")

    if not isinstance(data, dict):
        sys.exit(f"[slotmon] systems.json muss ein Objekt sein, nicht {type(data).__name__}")
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            sys.exit(f"[slotmon] systems.json: Eintrag {key!r} ist kein Text-Text-Paar")
    return data


def resolve(names, ip):
    if not ip:
        return None
    return names.get(ip, ip)
