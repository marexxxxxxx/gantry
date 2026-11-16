"""Max-Priorisierung: System-Prompt-Marker, An/Aus und SGLang-Prioritaet.

Das Gateway erkennt Max an konfigurierbaren Markern in den ``system``-
Nachrichten eines Chat-Requests. Ein Treffer bekommt die hohe Prioritaet,
alle anderen Chat-Requests die normale Prioritaet. Ist die Funktion aus,
wird ein eventuell mitgeschicktes ``priority``-Feld entfernt; SGLang faellt
dann fuer alle Requests auf seinen Default zurueck.

Die Einstellungen liegen als JSON unter :attr:`PriorityManager.KEY` im
Settings-Store (in der App die SQLite-History). Ohne Store arbeitet der
Manager mit den Defaults im Speicher (Tests).
"""

import json
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

#: Standardmarker des Max-Agenten (Anfang seines System-Prompts).
DEFAULT_MARKER = "You are Max, built for Marec"

#: Hoechste Prioritaet fuer Max; SGLang sortiert hoehere Werte zuerst.
MAX_PRIORITY = 100

#: Normale Prioritaet fuer alle anderen Chat-Requests.
NORMAL_PRIORITY = 0

#: Grenzen der Dashboard-Eingabe (verhindern riesige Eintraege in der DB).
MAX_MARKER = 500
MAX_MARKER_ANZAHL = 50


class SettingsStore(Protocol):
    """Minimaler Store fuer die persistierten Prioritaetseinstellungen."""

    def get_setting(self, key: str) -> str | None: ...

    def set_setting(self, key: str, value: str) -> None: ...


@dataclass(frozen=True)
class PrioritySettings:
    """Aktuelle Einstellung: Schalter und System-Prompt-Marker."""

    enabled: bool
    markers: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "markers": list(self.markers)}


#: Default beim ersten Start und bei unlesbaren gespeicherten Daten.
DEFAULT_SETTINGS = PrioritySettings(enabled=True, markers=(DEFAULT_MARKER,))


def normalize_markers(markers: object) -> tuple[str, ...]:
    """Bereinigt die Markerliste: trimmen, leere entfernen, deduplizieren.

    Verglichen wird ohne Beachtung der Gross-/Kleinschreibung; der erste
    Eintrag einer Gruppe bleibt in seiner Originalschreibweise erhalten.
    Nicht-Listen und Nicht-Strings sind ein ``ValueError`` (API -> 400).
    """
    if not isinstance(markers, (list, tuple)):
        raise ValueError("markers muss eine Liste sein")
    if len(markers) > MAX_MARKER_ANZAHL:
        raise ValueError(
            f"hoechstens {MAX_MARKER_ANZAHL} Marker erlaubt")
    ergebnis: list[str] = []
    gesehen: set[str] = set()
    for eintrag in markers:
        if not isinstance(eintrag, str):
            raise ValueError("jeder Marker muss ein String sein")
        text = eintrag.strip()
        if not text:
            continue
        if len(text) > MAX_MARKER:
            raise ValueError(
                f"ein Marker darf hoechstens {MAX_MARKER} Zeichen haben")
        schluessel = text.casefold()
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)
        ergebnis.append(text)
    return tuple(ergebnis)


def _text(inhalt: object) -> str:
    """Nachrichtentext wie in proxy._text_von: String, None oder JSON."""
    if isinstance(inhalt, str):
        return inhalt
    if inhalt is None:
        return ""
    return json.dumps(inhalt, ensure_ascii=False, default=str)


def _normalisiere(text: str) -> str:
    """Abstaende/Zeilenumbrueche vereinheitlichen und casefolden."""
    return " ".join(text.split()).casefold()


def system_text(koerper: dict) -> str:
    """Nur die ``system``-Nachrichten eines Chat-Bodys als Text."""
    messages = koerper.get("messages")
    if not isinstance(messages, list):
        return ""
    teile = [
        _text(nachricht.get("content"))
        for nachricht in messages
        if isinstance(nachricht, dict) and nachricht.get("role") == "system"
    ]
    return "\n".join(teile)


def find_marker(koerper: dict, markers: tuple[str, ...] | list[str]) -> str | None:
    """Der erste Marker, der in einer System-Nachricht vorkommt (oder None)."""
    if not markers:
        return None
    text = _normalisiere(system_text(koerper))
    for marker in markers:
        if marker and _normalisiere(marker) in text:
            return marker
    return None


def matches_markers(koerper: dict, markers: tuple[str, ...] | list[str]) -> bool:
    """True, wenn ein Marker in einer System-Nachricht vorkommt.

    User- und Assistant-Inhalte werden bewusst nicht durchsucht: dort
    koennte derselbe Text zufaellig stehen, ohne dass der Request von Max
    kommt. Abstaende und Gross-/Kleinschreibung sind irrelevant.
    """
    return find_marker(koerper, markers) is not None


class PriorityManager:
    """Haelt die Einstellungen im Speicher und markiert Chat-Bodys.

    ``store`` ist optional; ohne Store bleiben Aenderungen nur im Speicher
    (Tests). Die App reicht die History durch, damit Dashboard-Aenderungen
    einen Neustart ueberleben.
    """

    KEY = "max_priority"

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store
        self._settings: PrioritySettings | None = None

    def settings(self) -> PrioritySettings:
        """Aktuelle Einstellung; beim ersten Zugriff aus dem Store geladen."""
        if self._settings is None:
            self._settings = self._laden()
        return self._settings

    def as_dict(self) -> dict:
        return self.settings().as_dict()

    def _laden(self) -> PrioritySettings:
        if self._store is None:
            return DEFAULT_SETTINGS
        roh = self._store.get_setting(self.KEY)
        if roh is None:
            return DEFAULT_SETTINGS
        try:
            daten = json.loads(roh)
            enabled = daten["enabled"]
            if not isinstance(enabled, bool):
                raise ValueError("enabled ist kein Bool")
            return PrioritySettings(
                enabled=enabled,
                markers=normalize_markers(daten["markers"]))
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.warning(
                "Gespeicherte Prioritaetseinstellungen sind ungueltig;"
                " benutze Defaults")
            return DEFAULT_SETTINGS

    def update(self, *, enabled: bool, markers: object) -> PrioritySettings:
        """Validiert, persistiert und aktiviert eine neue Einstellung.

        Persistiert wird vor dem Cache-Update: schlaegt der Store fehl, bleibt
        die alte Einstellung aktiv. ``ValueError`` bedeutet ungueltige Eingabe.
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled muss ein Bool sein")
        neu = PrioritySettings(enabled=enabled,
                               markers=normalize_markers(markers))
        if self._store is not None:
            self._store.set_setting(self.KEY, json.dumps(
                neu.as_dict(), ensure_ascii=False))
        self._settings = neu
        return neu

    def mark(self, koerper: dict) -> bool:
        """Setzt die Prioritaet im Chat-Body; True bei einem Max-Treffer.

        Aus: vorhandenes ``priority`` wird entfernt, damit kein Client die
        abgeschaltete Priorisierung umgeht. An: Max bekommt
        :data:`MAX_PRIORITY`, alle anderen :data:`NORMAL_PRIORITY`. Ein
        bereits mitgeschicktes ``priority`` wird dabei ueberschrieben.
        """
        einstellungen = self.settings()
        if not einstellungen.enabled:
            koerper.pop("priority", None)
            return False
        if matches_markers(koerper, einstellungen.markers):
            koerper["priority"] = MAX_PRIORITY
            return True
        koerper["priority"] = NORMAL_PRIORITY
        return False
