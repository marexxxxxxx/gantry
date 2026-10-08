"""Raeumt Logdateien ab, ohne den Server anzufassen.

truncate -s 0 statt rm: der Server haelt die Datei offen, ein rm wuerde den
Platz erst beim Neustart freigeben -- den es nicht gibt.

Inode statt Dateiname
---------------------
``rm`` loescht nur den Verzeichniseintrag. Die Inode lebt weiter, solange die
Descriptors des Servers darauf zeigen, und der Platz kommt erst bei einem
``close()`` zurueck, den nur ein Neustart liefert. ``os.truncate(path, 0)``
laesst die Inode stehen und setzt nur die Laenge auf 0: derselbe Descriptor
schreibt danach ab Offset 0 weiter, der Block wird frei.

Der Pfadwaechter ist die zweite Haelfte der Sicherung: selbst ein ``rm`` oder
ein ``open(path, "w")`` im Fehlerfall koennte hier nur eine der beiden
erlaubten Logdateien unterhalb von /tmp treffen. Die Pfade kommen aus der
Konfiguration, nie aus einem ``find`` oder einem Glob.
"""

import os
import re
from dataclasses import dataclass

#: Nur diese Basenamen werden angefasst. Nicht der Pfad, der Basename
#: entscheidet - das Log kann in einem Unterverzeichnis von /tmp liegen.
#: ``.match`` mit ``^...$`` ist damit eine vollstaendige Ueberpruefung.
ALLOWED_NAME = re.compile(r"^(escha_server.*\.log|slotmon\.log)$")

#: Aufgeloest, damit ein Symlink auf /tmp nicht als /tmp gilt und umgekehrt.
TMP_ROOT = os.path.realpath("/tmp")


def is_allowed(path):
    """Nur Logdateien unter /tmp. Der Basename entscheidet, nicht ein Glob."""
    if not ALLOWED_NAME.match(os.path.basename(path)):
        return False
    return os.path.realpath(path).startswith(TMP_ROOT + os.sep)


def _on_tick(every, counter):
    """Takt ohne Pfadbezug: der erste Aufruf und dann jeder `every`-te.

    ``every <= 0`` heisst "jeder Aufruf", nicht "Teilung durch Null" - ein
    ``POLL_INTERVAL=0`` in der Konfiguration darf den Rotator nicht killen.
    """
    return counter % max(1, every) == 0


def should_check(path, every, counter):
    """Darf der Collector bei diesem Aufruf ``check()`` rufen?

    ``False`` heisst zweierlei: der Takt ist noch nicht dran, oder der Pfad ist
    gar nicht freigegeben. Im zweiten Fall gibt es nichts zu tun - der Pfad
    wird sowieso nicht angefasst, und ein ``check()`` darauf liefert nur einen
    Fehlertext, den der Collector bei jedem Takt wiederholen muesste.

    ``check()`` benutzt ``_on_tick`` statt dieser Funktion und meldet einen
    gesperrten Pfad als Fehler: der Aufrufer von ``check()`` will wissen,
    dass der Pfad abgelehnt wurde, nicht, dass nichts passiert ist.
    """
    if not is_allowed(path):
        return False
    return _on_tick(every, counter)


@dataclass
class RotateResult:
    #: Groesse in MB zum Zeitpunkt der Pruefung, None wenn es die Datei nicht
    #: gibt oder der Takt uebersprungen wurde. Beim Kuerzen ist das die Groesse
    #: *vor* dem Kuerzen.
    size_mb: float | None = None
    truncated: bool = False
    warn: bool = False
    error: str | None = None


class LogRotator:
    def __init__(self, max_mb=1024, warn_mb=900, every=60):
        self.max_mb = max_mb
        self.warn_mb = warn_mb
        #: So viele ``check()``-Aufrufe liegen zwischen zwei ``stat()``. Der
        #: erste Aufruf prueft sofort, damit ein frisch gestarteter Monitor
        #: nicht eine Stunde blind ist.
        self.every = every
        self.counter = 0

    def check(self, path, force=False):
        """Prueft ``path`` und kuerzt ihn im Notfall auf 0.

        ``force=True`` prueft auch auf einem uebersprungenen Takt.
        """
        due = force or _on_tick(self.every, self.counter)
        self.counter = (self.counter + 1) % max(1, self.every)
        if not due:
            # Leerer Befund, nicht "unbekannt": sonst meldet der Collector im
            # Sekunden-Takt eine Groesse, die er gar nicht gemessen hat.
            return RotateResult()

        if not is_allowed(path):
            return RotateResult(error=f"pfad nicht freigegeben: {path}")
        try:
            size = os.path.getsize(path)
        except OSError:
            # Kein Log da ist kein Fehler - es laeuft eben noch keiner.
            return RotateResult()

        size_mb = size / (1024 * 1024)
        if size_mb < self.warn_mb:
            return RotateResult(size_mb=size_mb)

        if size_mb >= self.max_mb:
            try:
                os.truncate(path, 0)
            except OSError as exc:
                return RotateResult(size_mb=size_mb, error=f"truncate: {exc}")
            return RotateResult(size_mb=size_mb, truncated=True, warn=False)

        return RotateResult(size_mb=size_mb, warn=True)