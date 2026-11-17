"""Regelbasierte Denk-Eskalation fuer Edge0 (Modus ``auto``).

Edge0 kann pro Request bis zu 48.000 Tokens denken, bevor das erste
sichtbare Token kommt. Ein festes Budget ist entweder zu klein (das Modell
kommt beim ersten Versuch nicht zum Punkt, der Agent wiederholt denselben
Tool-Call) oder zu gross (jeder Turn dauert Minuten). Dieser Baustein
entscheidet das pro Request, ohne Modell:

Zwei Regeln, beide ohne Modell und ohne Schaetzung:

**Regel 1 - der wievielte Versuch ist das?** Ein Retry mit identischem
Nachrichtenverlauf bedeutet, dass der vorige Versuch nicht genuegt hat -
also darf der naechste mehr denken. Frische Anfragen denken nicht.

    1. Versuch   -> ``none``    (kein Denkblock)
    2. Versuch   -> ``low``     (kurzes Nachdenken, hartes Budget)
    3. Versuch + -> ``medium``  (volles Budget)

Der Schluessel ist ein Hash ueber die ``messages``: jeder Agenten-Schritt
haengt ein Tool-Ergebnis an, also aendert sich der Hash; ein Retry schickt
denselben Verlauf erneut und trifft denselben Eintrag. Nach ``window_s``
Sekunden ohne Wiederholung faellt der Zaehler zurueck auf 1.

**Regel 2 - wiederholt der Agent denselben Tool-Call?** Ein Agent, der
zweimal exakt denselben Befehl mit denselben Argumenten schickt, hat beim
ersten Mal nichts gelernt. Das steht woertlich im Verlauf und ist damit
kein Indiz, sondern ein Befund - er eskaliert sofort, ohne auf den Retry
zu warten:

    2x derselbe Aufruf -> mindestens ``low``
    3x derselbe Aufruf -> ``medium``

Beide Regeln werden kombiniert: es gilt die hoehere Stufe.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter
from dataclasses import dataclass

#: Stufen vom billigsten zum teuersten Versuch.
STUFEN = ("none", "low", "medium")

#: So viele Tool-Aufrufe werden auf Wiederholungen geprueft.
LOOP_FENSTER = 8

#: Ab so vielen identischen Aufrufen gilt es als Schleife.
LOOP_AB_KURZ = 2
LOOP_AB_LANG = 3


@dataclass(frozen=True)
class Denkplan:
    """Was der Gateway fuer diesen Request senden soll.

    ``grund`` sagt, welche Regel die Stufe bestimmt hat (``versuch`` oder
    ``schleife``) - nur fuer das Log, nicht fuer die Entscheidung.
    """

    stufe: str
    versuch: int
    budget: int | None
    grund: str = "versuch"


def _normalisiere_argumente(argumente: object) -> str:
    """Argumente vergleichbar machen: JSON sortiert, sonst Whitespace weg."""
    if argumente is None:
        return ""
    if isinstance(argumente, str):
        text = argumente.strip()
        try:
            return json.dumps(json.loads(text), sort_keys=True,
                              ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return " ".join(text.split())
    return json.dumps(argumente, sort_keys=True, ensure_ascii=False,
                      default=str)


def tool_aufrufe(koerper: dict) -> list[tuple[str, str]]:
    """Signatur jedes Tool-Aufrufs im Verlauf (Name + normalisierte Argumente).

    Gelesen werden nur ``assistant``-Nachrichten: ``tool_calls`` (OpenAI)
    und das alte ``function_call``. Tool-Ergebnisse zaehlen nicht mit.
    """
    messages = koerper.get("messages")
    if not isinstance(messages, list):
        return []
    aufrufe: list[tuple[str, str]] = []
    for nachricht in messages:
        if not isinstance(nachricht, dict):
            continue
        if nachricht.get("role") != "assistant":
            continue
        calls = nachricht.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict):
                    continue
                funktion = call.get("function")
                if not isinstance(funktion, dict):
                    continue
                aufrufe.append((
                    str(funktion.get("name") or ""),
                    _normalisiere_argumente(funktion.get("arguments"))))
        funktion = nachricht.get("function_call")
        if isinstance(funktion, dict):
            aufrufe.append((
                str(funktion.get("name") or ""),
                _normalisiere_argumente(funktion.get("arguments"))))
    return aufrufe


def schleifen_tiefe(koerper: dict, fenster: int = LOOP_FENSTER) -> int:
    """Wie oft der haeufigste Tool-Aufruf zuletzt vorkam (0: keine Aufrufe).

    Nur der juengste Teil des Verlaufs zaehlt: derselbe Befehl darf frueh
    im Gespraech legitim wiederholt worden sein (z. B. ein erneutes ``ls``
    nach einer Aenderung), ohne dass es eine Schleife ist.
    """
    aufrufe = tool_aufrufe(koerper)
    if not aufrufe:
        return 0
    if fenster > 0:
        aufrufe = aufrufe[-fenster:]
    return max(Counter(aufrufe).values())


def _stufe_fuer_schleife(tiefe: int) -> str | None:
    """Stufe, die eine Schleife dieser Tiefe verlangt (oder None)."""
    if tiefe >= LOOP_AB_LANG:
        return "medium"
    if tiefe >= LOOP_AB_KURZ:
        return "low"
    return None


def _schluessel(koerper: dict) -> str:
    """Hash ueber den Nachrichtenverlauf (nicht ueber Sampling-Felder)."""
    messages = koerper.get("messages")
    roh = json.dumps(messages, sort_keys=True, ensure_ascii=False,
                     default=str)
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()[:16]


class ThinkingEscalator:
    """Leitet die Denkstufe aus zwei Beobachtungen ab.

    ``budgets`` bildet Stufe auf ``reasoning_budget_tokens`` ab; ``None``
    heisst "kein Budget senden" (``none`` schaltet den Denkblock ganz ab).
    ``loop_fenster`` begrenzt, wie viele Tool-Aufrufe auf Wiederholungen
    geprueft werden.
    """

    def __init__(self, window_s: float = 300.0, budgets: dict | None = None,
                 clock=time.time, loop_fenster: int = LOOP_FENSTER) -> None:
        if window_s <= 0:
            raise ValueError(f"window_s muss positiv sein: {window_s}")
        if loop_fenster < 0:
            raise ValueError(
                f"loop_fenster darf nicht negativ sein: {loop_fenster}")
        self.window_s = window_s
        self.loop_fenster = loop_fenster
        self.budgets = dict(budgets or {"none": None, "low": 1024,
                                        "medium": 8192})
        for stufe in STUFEN:
            self.budgets.setdefault(stufe, None)
        self._clock = clock
        self._gesehen: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def plan(self, koerper: dict) -> Denkplan:
        """Stufe und Budget fuer diesen Request (hoehere der beiden Regeln)."""
        schluessel = _schluessel(koerper)
        jetzt = self._clock()
        with self._lock:
            self._raeumen(jetzt)
            versuch, _ = self._gesehen.get(schluessel, (0, jetzt))
            versuch += 1
            self._gesehen[schluessel] = (versuch, jetzt)
        stufe = STUFEN[min(versuch, len(STUFEN)) - 1]
        grund = "versuch"
        schleife = _stufe_fuer_schleife(schleifen_tiefe(
            koerper, self.loop_fenster))
        if schleife is not None and STUFEN.index(schleife) > STUFEN.index(
                stufe):
            stufe, grund = schleife, "schleife"
        return Denkplan(stufe=stufe, versuch=versuch,
                        budget=self.budgets.get(stufe), grund=grund)

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
