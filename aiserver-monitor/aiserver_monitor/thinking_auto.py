"""Regelbasierte Denk-Eskalation fuer Edge0 (Modus ``auto``).

Edge0 kann pro Request bis zu 48.000 Tokens denken, bevor das erste
sichtbare Token kommt. Ein festes Budget ist entweder zu klein (das Modell
kommt beim ersten Versuch nicht zum Punkt, der Agent wiederholt denselben
Tool-Call) oder zu gross (jeder Turn dauert Minuten). Dieser Baustein
entscheidet das pro Request, ohne Modell:

Regel: der **wievielte Versuch** ist das? Ein Retry mit identischem
Nachrichtenverlauf bedeutet, dass der vorige Versuch nicht genuegt hat -
also darf der naechste mehr denken. Frische Anfragen denken nicht.

    1. Versuch   -> ``none``    (kein Denkblock)
    2. Versuch   -> ``low``     (kurzes Nachdenken, hartes Budget)
    3. Versuch + -> ``medium``  (volles Budget)

Der Schluessel ist ein Hash ueber die ``messages``: jeder Agenten-Schritt
haengt ein Tool-Ergebnis an, also aendert sich der Hash; ein Retry schickt
denselben Verlauf erneut und trifft denselben Eintrag. Nach ``window_s``
Sekunden ohne Wiederholung faellt der Zaehler zurueck auf 1.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass

#: Stufen vom billigsten zum teuersten Versuch.
STUFEN = ("none", "low", "medium")


@dataclass(frozen=True)
class Denkplan:
    """Was der Gateway fuer diesen Request senden soll."""

    stufe: str
    versuch: int
    budget: int | None


def _schluessel(koerper: dict) -> str:
    """Hash ueber den Nachrichtenverlauf (nicht ueber Sampling-Felder)."""
    messages = koerper.get("messages")
    roh = json.dumps(messages, sort_keys=True, ensure_ascii=False,
                     default=str)
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()[:16]


class ThinkingEscalator:
    """Zaehlt Wiederholungen desselben Verlaufs und leitet die Stufe ab.

    ``budgets`` bildet Stufe auf ``reasoning_budget_tokens`` ab; ``None``
    heisst "kein Budget senden" (``none`` schaltet den Denkblock ganz ab).
    """

    def __init__(self, window_s: float = 300.0, budgets: dict | None = None,
                 clock=time.time) -> None:
        if window_s <= 0:
            raise ValueError(f"window_s muss positiv sein: {window_s}")
        self.window_s = window_s
        self.budgets = dict(budgets or {"none": None, "low": 1024,
                                        "medium": 8192})
        for stufe in STUFEN:
            self.budgets.setdefault(stufe, None)
        self._clock = clock
        self._gesehen: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def plan(self, koerper: dict) -> Denkplan:
        """Stufe und Budget fuer diesen Request."""
        schluessel = _schluessel(koerper)
        jetzt = self._clock()
        with self._lock:
            self._raeumen(jetzt)
            versuch, _ = self._gesehen.get(schluessel, (0, jetzt))
            versuch += 1
            self._gesehen[schluessel] = (versuch, jetzt)
        stufe = STUFEN[min(versuch, len(STUFEN)) - 1]
        return Denkplan(stufe=stufe, versuch=versuch,
                        budget=self.budgets.get(stufe))

    def _raeumen(self, jetzt: float) -> None:
        abgelaufen = [k for k, (_, ts) in self._gesehen.items()
                      if jetzt - ts > self.window_s]
        for k in abgelaufen:
            del self._gesehen[k]

    def reset(self) -> None:
        with self._lock:
            self._gesehen.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._gesehen)
