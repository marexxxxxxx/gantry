"""Schuetzt vor der Rueckkehr der widerlegten Socket-Methode.

Die erste Fassung der Spec erkannte den Slot-Halter ueber `ss -tinH` und ein
Delta auf `bytes_sent`. Diese Methode ist **widerlegt**: gemessen am
2026-10-07 fiel die Zahl offener TCP-Verbindungen von 35 auf 1, waehrend die
Warteschlange von 180 auf 1492 stieg. Die beiden Zahlen laufen gegenlaeufig;
aus Sockets ist keine Zuordnung ableitbar.

Warum dieser Test trotzdem noetig ist: `collectors.py` enthaelt heute keinen
Socket-Zugriff, und das ist eine Entscheidung, die beim naechsten Refactoring
leicht wieder verlorengeht - zumal der alte Code plausibel aussah und die
erste Version der Spec auf ihm beruhte.

Der Test ist bewusst **grob**. Er sucht Zeichenketten, nicht Semantik, und
kann darum false positives liefern, wenn jemand ueber das Thema schreibt. Ein
Treffer ist deshalb kein Beweis fuer Socket-Zugriff, sondern ein Hinweis, dass
gesehen werden muss.
"""

import glob
import os
import re
import unittest

REPO = os.path.join(os.path.dirname(__file__), "..")
SLOTMON = os.path.join(REPO, "slotmon")

#: Zugriff auf die Socket-Tabellen des Kernels. `/proc/net/tcp6` ist
#: enthalten, weil es derselbe Zugriff auf IPv6 ist und nicht erst dann
#: verdaechtig wird, wenn jemand IPv6 braucht.
FORBIDDEN = ("ss -t", "/proc/net/tcp", "bytes_sent")

#: Erlaubt sind Kommentare, die das Thema **nennen**. Diese Nennungen sind
#: selbst Teil der Absicherung: sie sagen, warum die Methode nicht zurueckkommt.
ALLOWED_MENTIONS = (
    "widerlegt",
    "socket",
    "Socket",
)


def _python_sources():
    for path in sorted(glob.glob(os.path.join(SLOTMON, "*.py"))):
        with open(path, encoding="utf-8") as handle:
            yield path, handle.read()


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestNoSocketSource(unittest.TestCase):
    """Kein Modul liest die Socket-Tabellen des Kernels."""

    def test_no_module_reads_sockets(self):
        found = []
        for path, source in _python_sources():
            for needle in FORBIDDEN:
                if needle in source:
                    found.append(f"{os.path.basename(path)}: {needle}")
        self.assertEqual(found, [], f"Socket-Zugriff gefunden: {found}")

    def test_monitor_script_is_clean(self):
        script = _read(os.path.join(REPO, "monitor.sh"))
        for needle in FORBIDDEN:
            self.assertNotIn(needle, script)

    def test_the_socket_approach_is_named_as_refuted(self):
        """Die Begruendung steht im Code, nicht nur in einem Git-Commit.

        Ohne diese Erwaehnung liest sich die Abwesenheit des Zugriffs in
        ein paar Monaten wie ein Versehen und wird "korrigiert".
        """
        mentioned = False
        for _, source in _python_sources():
            if "widerlegt" in source or "widerlegt" in source:
                mentioned = True
                break
        self.assertTrue(mentioned,
                        "Kein Modul nennt die Socket-Methode als widerlegt.")


class TestNurAbortSchreibt(unittest.TestCase):
    """Genau eine Datei schreibt je zum Server: `abort.py`.

    Bisher prueft der Test, dass niemand `/abort_request` aufruft. Jetzt gilt
    das Gegenteil: `abort.py` darf es, und sonst niemand. Der Test wird
    strenger, nicht weicher - ein `POST` in einem anderen Modul ist jetzt ein
    Fehler, wo es vorher nur unerwuenscht war.

    `@app.post(...)` ist der Routen-Dekorator des Monitors selbst und kein
    Aufruf zum Server. Genau diese Form ist ausgenommen, sonst meldete der Test
    `server.py` als schreibend.
    """

    POST_ZEUGEN = ("httpx.post", "requests.post", "urlopen", ".post(")
    ROUTEN_DEKORATOR = "@app.post("

    def test_genau_ein_modul_schreibt(self):
        schreibend = []
        for pfad in sorted(glob.glob(os.path.join(SLOTMON, "*.py"))):
            if self._schreibt(_read(pfad)):
                schreibend.append(os.path.basename(pfad))
        self.assertEqual(schreibend, ["abort.py"],
                         f"mehr als eine Datei schreibt zum Server: {schreibend}")

    def test_das_dashboard_nutzt_keine_python_schreibbibliothek(self):
        html = _read(os.path.join(SLOTMON, "dashboard.html"))
        for zeuge in ("httpx.post", "requests.post", "urlopen"):
            self.assertNotIn(zeuge, html)

    @classmethod
    def _schreibt(cls, quelle):
        for zeile in quelle.split("\n"):
            if zeile.lstrip().startswith(cls.ROUTEN_DEKORATOR):
                continue
            if any(zeuge in zeile for zeuge in cls.POST_ZEUGEN):
                return True
        return False


class TestMonitorStaysAwayFromTheServer(unittest.TestCase):
    """`monitor.sh` fasst den sglang-Server nicht an.

    Zwei Lebenszyklen: der Server auf 30000 und der Monitor auf 8090. Wer
    den falschen beendet, hat den Falschen. Geprueft wird nicht der Wortlaut,
    sondern dass `monitor.sh` **keinen** Prozessbefehl mit `sglang` baut.
    """

    def setUp(self):
        self.script = _read(os.path.join(REPO, "monitor.sh"))

    def test_no_signal_command_names_the_server(self):
        """Kein `kill`/`pgrep`/`pkill` bezieht sich auf sglang.

        Kommentare duerfen den Server erwaehnen - das Muster in Zeile 6
        verweist auf `start-penalty.sh`. Geprueft wird deshalb nur, was in
        einer ausfuehrbaren Zeile steht.

        Zwei Entwuerfe sind hier gescheitert und stehen als Warnung in der
        Historie:

        1. `sglang` direkt nach dem Befehl suchen. Das Suchmuster von `pgrep`
           schreibt `[s]glang`, die Klammern unterbrechen das Wort - ein
           `pkill -f "[s]glang.launch_server"` rutschte durch.
        2. Buchstabenweise suchen (`l[a]?u?n?c?h`). Das findet `[s]glang`
           nicht: dort steht kein `l` vor `ang`.

        Der Trick ist deshalb der umgekehrte: **erst die Klammern entfernen,
        dann das ganze Wort suchen.** `[s]glang` wird so zu `slang` - und der
        Rest des Namens (`slang`) ist genau das, was `pgrep -f` sucht.
        """
        verboten = []
        for zeile in self.script.split("\n"):
            if zeile.lstrip().startswith("#"):
                continue
            if not re.search(r"\b(kill|pkill|pgrep)\b", zeile):
                continue
            # Suchmuster entschaerfen: `[s]glang` -> `slang`.
            entschaerft = zeile.replace("[", "").replace("]", "")
            # Ein Name genuegt ...
            if "sglang" in entschaerft or "slang" in entschaerft:
                verboten.append(zeile.strip())
                continue
            # ... eine nackte PID nicht. `kill -TERM 51952` trifft genau den
            # Server und nennt ihn nicht. Zwei Mutanten sind genau daran
            # gescheitert: ein Name im Skript waere lesbar, eine PID nicht -
            # und die PID ist der Fall, der im Betrieb wehtut.
            #
            # Die PID des laufenden Servers ist nicht im Skript festgeschrieben
            # (das waere noch ein Fehler), also suchen wir nach jeder Zahl, die
            # einer PID entspricht, zusammen mit einem Signal-Befehl.
            if re.search(r"\b(kill|pkill)\b.*\b\d{2,}\b", zeile):
                verboten.append(zeile.strip())
        self.assertEqual(verboten, [],
                         f"monitor.sh steuert den Server: {verboten}")

    def test_the_process_pattern_is_the_monitor(self):
        self.assertIn("[s]lotmon.server", self.script)


class TestLogRequestsIsDurable(unittest.TestCase):
    """`ENABLE_LOG_REQUESTS` ist per Default **an** und abschaltbar.

    Aus dem Whole-Branch-Review. Die Spec mass, dass der laufende Server die
    Events auch ohne die Flags schreibt; damit die Halter-Erkennung aber
    *dauerhaft* funktioniert, setzt `serve.sh` `ENABLE_LOG_REQUESTS` auf 1. Das
    war in keinem der Dokumente festgehalten, die die Feature-Messwerte
    fuehren - die Spec sagte sogar ausdruecklich, der Monitor stelle nichts
    ein.

    Der Test haelt die Abschalt-Absicht fest. Ohne ihn koennte jemand die
    Zeile entfernen, und der naechste Neustart verlierst die Events, ohne dass
    jemand etwas bemerkte: der Monitor meldet dann `unknown` mit einer
    Begruendung, die stimmt - nur faellt niemandem auf, dass sie falsch ist.
    """

    SCRIPT = os.path.join(REPO, "runtime", "sglang", "serve.sh")

    def test_the_flag_starts_with_a_default_of_one(self):
        serve = _read(self.SCRIPT)
        self.assertIn('ENABLE_LOG_REQUESTS:-1', serve,
                      "serve.sh setzt ENABLE_LOG_REQUESTS nicht per Default")

    def test_it_still_passes_the_json_format(self):
        """Nur `--log-requests` reicht nicht.

        sglangs Default ist `text` (`Receive: obj=...`), und nur die
        json-Variante liefert die `{"event": ..., "rid": ...}`-Zeilen, aus
        denen der Leser seinen Halter bestimmt. Ohne `-format json` waere die
        Zeile vorhanden und trotzdem unlesbar - der Monitor meldet dann
        `unknown` mit derselben plausiblen Begruendung.
        """
        serve = _read(self.SCRIPT)
        self.assertIn("--log-requests-format json", serve)

    def test_it_can_be_switched_off(self):
        """Abschalten muss moeglich sein, sonst ist es keine Konfiguration.

        Die Bedingung vergleicht auf `"1"` - alles andere schaltet ab.
        """
        serve = _read(self.SCRIPT)
        zeile = [z for z in serve.split("\n") if "LOGREQ_ARGS=()" in z]
        self.assertEqual(len(zeile), 1, "die LOGREQ_ARGS-Zeile ist nicht eindeutig")
        self.assertIn('${ENABLE_LOG_REQUESTS:-1}" == "1"', zeile[0])
    """Die Spec haelt fest, worauf sich der Monitor stuetzt."""

    def setUp(self):
        self.spec = _read(os.path.join(
            REPO, "docs", "superpowers", "specs",
            "2026-10-07-gpu-slot-monitor-design.md"))

    def test_spec_records_the_refutation(self):
        self.assertIn("widerlegt", self.spec)

    def test_spec_names_the_actual_source(self):
        """`request.received` ist die Quelle, aus der der Halter abgeleitet wird."""
        self.assertIn("request.received", self.spec)
        self.assertIn("Altbestand", self.spec)

    def test_spec_cannot_silently_lose_the_queue_claim(self):
        """Die Kachel WARTEND ist die einzige belegte Warteschlangenzahl.

        Faellt dieser Satz aus der Spec, koennte jemand die
        Verbindungszahl zurueckbringen - dann waere der Unsinn wieder eine
        dokumentierte Entscheidung.
        """
        self.assertIn("num_waiting_reqs", self.spec)


if __name__ == "__main__":
    unittest.main()