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

``O_APPEND`` ist beim laufenden Server gesetzt (fd 1 und 2,
``flags: 0102001``). Deshalb landet der naechste Schreibvorgang nach dem
Kuerzen wieder bei Offset 0 und nicht am alten Dateiende.

Eine Autoritaet fuer den Takt
-----------------------------
``check()`` wird **jeden** Pol gerufen und entscheidet selbst, ob es wirklich
``stat`` macht. Es gibt bewusst kein zweites Takt-Werkzeug daneben: zwei
unabhaengige Zaehler kommen nie gleichzeitig auf 0, und die Folge ist kein
Fehler, sondern Stille - der Aufrufer sieht ``RotateResult()`` und glaubt, es
sei nichts zu tun. Gemessen waren das 0 Kuerzungen in 31 Polls bei einem Log,
das auf 62 MB wuchs.

``every=60`` ist der Default und heisst "alle 60 Polls", bei
``POLL_INTERVAL=2.0`` also exakt zwei Minuten. Nicht umrechnen.

Ein Objekt pro Logdatei
-----------------------
Der Zaehler gehoert zum Objekt, nicht zum Pfad. Zwei Dateien auf einer
Instanz teilen sich einen Zaehler, und eine der beiden bekommt dann
dauerhaft keinen Takt zugeteilt und wird gar nicht gekuerzt. Der Collector
legt pro Log ein eigenes ``LogRotator`` an.

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


def resolve_allowed(path):
    """Der aufgeloeste, freigegebene Pfad - oder None.

    Geprueft wird der gegebene Pfad *und* sein Ziel nach ``realpath``, und
    zurueckgegeben wird genau der aufgeloeste Pfad. ``check()`` misst und
    kuerzt dann diese eine Zeichenkette: prueft man den einen Pfad und
    benutzt den anderen, kann ein zwischenzeitlich umgelegter Symlink eine
    Datei ausserhalb von /tmp treffen.
    """
    if not ALLOWED_NAME.match(os.path.basename(path)):
        return None
    target = os.path.realpath(path)
    # Auch der Zielname muss erlaubt sein: ein Link "slotmon.log" auf
    # /tmp/irgendwas/history.jsonl waere sonst freigegeben.
    if not ALLOWED_NAME.match(os.path.basename(target)):
        return None
    # Das os.sep ist nicht Kosmetik: ohne es waere "/tmpfoo/escha_x.log"
    # erlaubt, weil es mit "/tmp" beginnt.
    if not target.startswith(TMP_ROOT + os.sep):
        return None
    return target


def is_allowed(path):
    """Nur Logdateien unter /tmp. Der Basename entscheidet, nicht ein Glob."""
    return resolve_allowed(path) is not None


@dataclass
class RotateResult:
    #: Groesse in MB zum Zeitpunkt der Pruefung, None wenn es die Datei nicht
    #: gibt oder der Takt uebersprungen wurde. Beim Kuerzen ist das die Groesse
    #: *vor* dem Kuerzen.
    size_mb: float | None = None
    truncated: bool = False
    warn: bool = False
    error: str | None = None
    #: Unix-Zeit der Messung, aus der diese Werte stammen. `None`, wenn dieser
    #: Takt uebersprungen wurde. Der Collector fuellt das auf einem
    #: uebersprungenen Takt aus seinem zuletzt gemessenen Befund nach, damit
    #: eine Warnung nicht zwischen zwei Messungen verschwindet.
    measured_at: float | None = None


class LogRotator:
    """Raeumt **eine** Logdatei. Pro Log eine eigene Instanz."""

    def __init__(self, max_mb=1024, warn_mb=900, every=60):
        # Ein totes Band waere schlimmer als ein schiefer Schwellwert. Ohne
        # diese Zeile gilt bei MAX_LOG_MB=500 und WARN_LOG_MB=900 fuer jede
        # Groesse zwischen 500 und 900 MB: kein Truncate, keine Warnung, kein
        # Fehler - das Log waechst unbegrenzt und meldet sich nie.
        if warn_mb > max_mb:
            warn_mb = max_mb * 0.9
        self.max_mb = max_mb
        self.warn_mb = warn_mb
        #: Wie viele ``check()``-Aufrufe zwischen zwei ``stat()``. Der erste
        #: Aufruf prueft sofort, damit ein frisch gestarteter Monitor nicht
        #: zwei Minuten blind ist. ``every <= 0`` heisst "jeder Aufruf", nicht
        #: "Teilung durch Null".
        self.every = every
        self.counter = 0

    def check(self, path, force=False):
        """Prueft ``path`` und kuerzt ihn im Notfall auf 0.

        Wird **jeden** Pol gerufen und springt auf den eigenen Takt. Ruft der
        Aufrufer check() nur auf, wenn er selbst meint, dass es Zeit ist, ist
        das der zweite Zaehler - und daran ist die Rotation schon einmal still
        ausgefallen, ohne einen Fehler zu melden.

        ``force=True`` prueft auch auf einem uebersprungenen Takt.
        """
        due = force or self.counter % max(1, self.every) == 0
        self.counter = (self.counter + 1) % max(1, self.every)
        if not due:
            # Leerer Befund, nicht "unbekannt": sonst meldet der Collector im
            # Sekunden-Takt eine Groesse, die er gar nicht gemessen hat.
            return RotateResult()

        target = resolve_allowed(path)
        if target is None:
            return RotateResult(error=f"pfad nicht freigegeben: {path}")
        try:
            size = os.path.getsize(target)
        except OSError:
            # Kein Log da ist kein Fehler - es laeuft eben noch keiner.
            return RotateResult()

        size_mb = size / (1024 * 1024)
        if size_mb < self.warn_mb:
            return RotateResult(size_mb=size_mb)

        if size_mb >= self.max_mb:
            try:
                os.truncate(target, 0)
            except OSError as exc:
                # EACCES, EROFS, ENOSPC: genau dann, wenn es am noetigsten ist,
                # darf der Aufrufer keinen Traceback sehen.
                return RotateResult(size_mb=size_mb, error=f"truncate: {exc}")
            return RotateResult(size_mb=size_mb, truncated=True, warn=False)

        return RotateResult(size_mb=size_mb, warn=True)
