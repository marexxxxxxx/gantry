"""Die Warteschlange in der Reihenfolge, in der der Server sie abarbeitet.

Rein: keine Uhr, kein I/O, kein HTTP. Die Zeit kommt von aussen.

Reihenfolge
-----------
Der Server arbeitet FIFO. Der naechste dran ist der **aelteste Wartende**.
`LogReader.state["open"]` liefert `rid -> at`, wobei `at` die Unix-Zeit aus
dem `timestamp`-Feld ist. Der Reader parst aber nur die ersten 19 Zeichen
(`logreader.py:342`) und **schneidet die Mikrosekunden ab**: `at` ist auf
ganze Sekunden genau. Zwei rids, die im selben Takt eintreffen, stehen
deshalb gleichauf.

Der Tie-Break nach `rid` macht die Reihenfolge damit *deterministisch* statt
von der Dict-Implementierung abhaengig. Er **ersetzt aber keine** Information,
die im Log fehlt: innerhalb einer Sekunde ist die Eingangsreihenfolge nicht
belegt. Genau das ist der Punkt, an dem diese Liste eine Aussage macht, die
sie belegen kann (die Reihenfolge ueber Sekunden) und eine, die sie nicht
(die Feinordnung innerhalb einer Sekunde).

Kein Halter in der Liste
------------------------
Der aelteste offene rid ist bei `max_running_requests 1` nicht der naechste
Wartende, sondern der **laufende** Auftrag - er wurde zuerst aus der
Warteschlange in den Slot geholt. Die Liste beginnt deshalb hinter ihm.

Keine Namen fuer Wartende
-------------------------
Die IP steht ausschliesslich in `request.finished`. Ein Wartender hat keine -
gemessen an 61 wartenden Requests ueber alle Felder von `request.received`:
null Treffer. Der Name wird nur gezeigt, wenn dieselbe `ip_port` aus einem
frueheren Abschluss bekannt ist. Sonst `None`, was die Anzeige als
"unbekannt, steht erst nach dem Abschluss fest" rendert.
"""

from dataclasses import asdict, dataclass


@dataclass
class WaitingEntry:
    position: int
    rid: str
    waiting_since: float
    system: str | None

    def as_dict(self):
        return asdict(self)


def build(open_rids, waiting, legacy, resolve):
    """Die Warteschlange als Liste, Position 1 zuerst.

    `open_rids` sind die rids, die ein `request.received` hatten und noch kein
    `request.finished`. Das sind Halter **und** Wartende.

    `legacy > 0` heisst: die Warteschlangenlaenge passt nicht zu den Zeilen im
    Log, die Liste waere geraten. Dann wird **nichts** geliefert - eine leere
    Liste, nicht eine unvollstaendige.

    Der **aelteste offene** rid laeuft gerade (FIFO, ein Slot) und steht
    deshalb nicht in der Liste. Wer nichts kuerzt, liefert eine Position 1,
    die es so nicht gibt.

    `waiting`, das keine ganze Zahl ist (oder `None`), liefert `[]`.

    `resolve` bekommt die **rid** und liefert den Anzeigenamen oder `None`.
    Die Aufloesung des Namens bleibt beim Aufrufer: dieses Modul weiss
    nichts ueber `systems.json`.
    """
    if legacy:
        return []
    # `count_of()` laesst einen `float` und einen Wahrheitswert durch, weil
    # JSON `3.0` und `true` an dieser Stelle erlaubt. Ein Slice mit
    # nicht-ganzzahliger Grenze wirft `TypeError`, ein Vergleich mit einem
    # Text auch - und beides verlaesst `tick()`. Eine Position ist nur dann
    # eine Zahl, wenn der Server eine geliefert hat.
    if isinstance(waiting, bool) or not isinstance(waiting, int):
        return []
    ordnung = sorted(open_rids.items(), key=lambda paar: (paar[1], paar[0]))
    # Nur so viele, wie der Server auch bestaetigt. Die Liste ist sonst
    # laenger als die Queue, und Position 2 existiert dann nicht.
    grenze = max(0, waiting)
    # Der **aelteste offene** rid ist bei `max_running_requests 1` der
    # laufende Auftrag, nicht der naechste Wartende. Er wird uebersprungen -
    # sonst staende der Halter auf Position 1, und ein Abbruch von Position 1
    # traefe den Halter statt eines Wartenden.
    #
    # Ob ueberhaupt jemand laeuft, steht in denselben zwei Zahlen, die schon
    # da sind: `evaluate()` rechnet `legacy = max(0, waiting - open_count)`.
    # Nach dem `legacy`-Test oben gilt also `open_count >= waiting`, und
    # `open_count - waiting` ist genau die Zahl der laufenden Request. Mehr
    # offene rids als Wartende heisst: einer laeuft. Gleichstand heisst: es
    # laeuft gerade nichts, und die Liste ist vollstaendig die offenen rids.
    ordnung = ordnung[1:grenze + 1] if len(ordnung) > grenze else ordnung[:grenze]
    eintraege = []
    for index, (rid, since) in enumerate(ordnung, 1):
        system = resolve(rid)
        eintraege.append(WaitingEntry(
            position=index, rid=rid, waiting_since=since,
            system=system if isinstance(system, str) and system else None))
    return [e.as_dict() for e in eintraege]