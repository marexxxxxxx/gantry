"""Hindsight-Blocker: System-Prompt-Marker, An/Aus und Persistenz.

Hindsight ist der Memory-Hintergrunddienst des Max-Agenten. Seine Requests
werden an denselben System-Prompt-Markern erkannt wie Max selbst: nur
``role: system``-Nachrichten zaehlen, Abstaende und Gross-/Kleinschreibung
sind egal. Ist der Blocker an, weist das Gateway passende Chat-Requests
sofort ab, ohne SGLang zu kontaktieren. Ist er aus, laufen sie normal.

Die Einstellungen liegen als JSON unter :attr:`HindsightBlocker.KEY` im
Settings-Store (in der App die SQLite-History). Ohne Store arbeitet der
Manager mit den Defaults im Speicher (Tests).
"""

import json
import logging
from dataclasses import dataclass

from .priority import SettingsStore, matches_markers, normalize_markers

logger = logging.getLogger(__name__)

#: Standardmarker der beiden Hindsight-Phasen. Weitere Marker (z. B. fuer
#: spaetere Hindsight-Prompts) lassen sich im Dashboard ergaenzen.
DEFAULT_HINDSIGHT_MARKERS = (
    "Extract SIGNIFICANT facts from text",
    "You are a memory consolidation system",
)


@dataclass(frozen=True)
class HindsightSettings:
    """Aktuelle Einstellung: Schalter und System-Prompt-Marker."""

    enabled: bool
    markers: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "markers": list(self.markers)}


#: Default beim ersten Start und bei unlesbaren gespeicherten Daten.
DEFAULT_SETTINGS = HindsightSettings(
    enabled=False, markers=DEFAULT_HINDSIGHT_MARKERS)


class HindsightBlocker:
    """Haelt die Einstellungen im Speicher und erkennt Hindsight-Bodys.

    ``store`` ist optional; ohne Store bleiben Aenderungen nur im Speicher
    (Tests). Die App reicht die History durch, damit Dashboard-Aenderungen
    einen Neustart ueberleben.
    """

    KEY = "hindsight_block"

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store
        self._settings: HindsightSettings | None = None

    def settings(self) -> HindsightSettings:
        """Aktuelle Einstellung; beim ersten Zugriff aus dem Store geladen."""
        if self._settings is None:
            self._settings = self._laden()
        return self._settings

    def as_dict(self) -> dict:
        return self.settings().as_dict()

    def _laden(self) -> HindsightSettings:
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
            return HindsightSettings(
                enabled=enabled,
                markers=normalize_markers(daten["markers"]))
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.warning(
                "Gespeicherte Hindsight-Einstellungen sind ungueltig;"
                " benutze Defaults")
            return DEFAULT_SETTINGS

    def update(self, *, enabled: bool, markers: object) -> HindsightSettings:
        """Validiert, persistiert und aktiviert eine neue Einstellung.

        Persistiert wird vor dem Cache-Update: schlaegt der Store fehl, bleibt
        die alte Einstellung aktiv. ``ValueError`` bedeutet ungueltige Eingabe.
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled muss ein Bool sein")
        neu = HindsightSettings(enabled=enabled,
                                markers=normalize_markers(markers))
        if self._store is not None:
            self._store.set_setting(self.KEY, json.dumps(
                neu.as_dict(), ensure_ascii=False))
        self._settings = neu
        return neu

    def matches(self, koerper: dict) -> bool:
        """True, wenn ein Hindsight-Marker im System-Prompt vorkommt.

        Der Treffer ist unabhaengig vom Schalter: So kann ein Job auch dann
        als Hindsight markiert werden, wenn der Blocker noch aus ist.
        """
        return matches_markers(koerper, self.settings().markers)

    def blocked(self, koerper: dict) -> bool:
        """True, wenn der Blocker an ist und der Body Hindsight ist."""
        einstellungen = self.settings()
        return (einstellungen.enabled
                and matches_markers(koerper, einstellungen.markers))
