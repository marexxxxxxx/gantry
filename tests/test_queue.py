"""Die Warteschlange in der Reihenfolge, in der der Server abarbeitet.

Belegt durch Messung vom 2026-10-12: 35 offene `request.received` im Log,
`num_reqs: 35`, `num_waiting_reqs: 34`. Die Liste ist damit vollstaendig -
die alte Aussage "Reihenfolge nicht ableitbar" galt dem Zustand vor dem
Aktivieren des Request-Loggings.
"""

import unittest

from slotmon.queue import build


class TestOrder(unittest.TestCase):
    def test_oldest_comes_first(self):
        """Position 1 ist der naechste dran.

        Der Server arbeitet FIFO ab. Der aelteste `request.received` ist
        deshalb der naechste - nicht der neueste, und nicht irgendeiner.
        """
        offene = {"bbb": 300.0, "aaa": 100.0, "ccc": 200.0}
        liste = build(offene, waiting=3, legacy=0, resolve=lambda rid: None)
        self.assertEqual([e["rid"] for e in liste], ["aaa", "ccc", "bbb"])
        self.assertEqual([e["position"] for e in liste], [1, 2, 3])

    def test_order_survives_equal_timestamps(self):
        """Zwei rids im selben Takt behalten eine feste Reihenfolge.

        Kein Schoenheitswunsch: `LogReader._collect` parst `stamp[:19]` und
        **schneidet die Mikrosekunden ab** (`logreader.py:342`, siehe
        `test_the_reader_truncates_microseconds` in `test_collector.py`). Zwei
        im selben Takt eingetroffene Requests stehen im Dict also gleichauf,
        und das Log traegt ihre Reihenfolge nicht mehr. Der Tie-Break macht
        die Ausgabe wenigstens **deterministisch** statt von der
        Dict-Implementierung abhaengig - mehr kann er nicht.
        """
        offene = {"bbb": 100.0, "aaa": 100.0, "ccc": 100.0}
        liste = build(offene, waiting=3, legacy=0, resolve=lambda rid: None)
        self.assertEqual([e["rid"] for e in liste], ["aaa", "bbb", "ccc"])

    def test_seconds_stay_out_of_the_ordering(self):
        """Naeher beieinander heisst frueher, auch innerhalb einer Sekunde.

        `build()` rundet nichts - das ist getrennt von der Frage, ob der
        Leser die Nachkommastellen ueberhaupt liefert. Er liefert sie nicht;
        diese Funktion bekommt, was der Leser gibt, und darf den Unterschied
        nicht wegwerfen.
        """
        offene = {"a": 100.0, "b": 100.4, "c": 100.9}
        liste = build(offene, waiting=3, legacy=0, resolve=lambda rid: None)
        self.assertEqual([e["rid"] for e in liste], ["a", "b", "c"])


class TestAltbestand(unittest.TestCase):
    """Solange eine Zahl im Log fehlt, ist die Liste geraten.

    `legacy > 0` heisst: `num_waiting_reqs` ist groesser als die Zahl der
    offenen `request.received`. Genau die Requests, die vor dem
    Request-Logging eingereiht wurden, stehen im Log nicht. Ihre Zahl ist
    echt, ihre Reihenfolge nicht ableitbar - und eine Liste, die trotzdem
    Positionen vergibt, wuerde dem Nutzer eine Reihenfolge zeigen, die es
    nicht gibt. Leere Liste, nicht unvollstaendige.
    """

    def test_legacy_returns_an_empty_list(self):
        offene = {"aaa": 100.0, "bbb": 200.0}
        liste = build(offene, waiting=5, legacy=3, resolve=lambda rid: None)
        self.assertEqual(liste, [])


class TestOhneServer(unittest.TestCase):
    """Ohne `/get_load` ist die Warteschlangenlaenge unbekannt.

    `count_of()` liefert dann `None`. Eine Liste aus den offenen rids waere
    geraten: sie waende selbst aus, wie viele davon wirklich warten, und
    genau das ist die Zahl, die der Server bestaetigen muss.
    """

    def test_unknown_waiting_returns_an_empty_list(self):
        offene = {"aaa": 100.0, "bbb": 200.0}
        liste = build(offene, waiting=None, legacy=None, resolve=lambda rid: None)
        self.assertEqual(liste, [])


class TestName(unittest.TestCase):
    """Der Name steht nur fest, wenn die `ip_port` schon einmal fertig war.

    Ein wartender Request traegt keine IP (61 geprueft, 0 Treffer ueber alle
    Felder von `request.received`). `resolve` bekommt deshalb die **rid** und
    schlaegt damit beim Aufrufer nach - der weiss, welche `ip_port` aus einem
    frueheren Abschluss bekannt ist. Dieser Test prueft die Anzeigeregel, nicht
    die Datenlage: `resolve` ist hier ein Stub.
    """

    def names(self, verzeichnis):
        return lambda rid: verzeichnis.get(rid)

    def test_a_known_name_appears(self):
        offene = {"aaa": 100.0, "bbb": 200.0}
        resolve = self.names({"bbb": "Laptop"})
        liste = build(offene, waiting=2, legacy=0, resolve=resolve)
        self.assertEqual([e["system"] for e in liste], [None, "Laptop"])

    def test_an_unknown_name_stays_none(self):
        """Kein erfundener Name. Ein leeres Feld heisst hier nichts."""
        offene = {"aaa": 100.0}
        liste = build(offene, waiting=1, legacy=0, resolve=self.names({}))
        self.assertIsNone(liste[0]["system"])

    def test_an_empty_string_is_not_a_name(self):
        """`""` ist kein Name.

        Ein leerer String waere in der Anzeige eine Luege, die aussieht wie
        ein Wissen: "hier steht etwas, und es ist nichts".
        """
        offene = {"aaa": 100.0}
        liste = build(offene, waiting=1, legacy=0, resolve=lambda rid: "")
        self.assertIsNone(liste[0]["system"])

    def test_a_non_string_is_not_a_name(self):
        """Auch kein Messwert, kein Objekt, keine Zahl."""
        for wert in (42, ["Laptop"], {"name": "Laptop"}, True, object()):
            with self.subTest(wert=wert):
                offene = {"aaa": 100.0}
                liste = build(offene, waiting=1, legacy=0,
                              resolve=lambda rid, w=wert: w)
                self.assertIsNone(liste[0]["system"])

    def test_resolve_gets_the_rid(self):
        """Der Vertrag: rid hinein, Name oder `None` heraus.

        Steht hier ausdruecklich, weil `build()` sonst auch eine IP annehmen
        koennte - der Aufrufer koennte beide Sorten Namen liefern und die
        Verwechslung faelle erst bei einem Wartenden mit Namen auf.
        """
        gesehen = []

        def resolve(rid):
            gesehen.append(rid)
            return None

        build({"aaa": 100.0, "bbb": 200.0}, waiting=2, legacy=0,
              resolve=resolve)
        self.assertEqual(gesehen, ["aaa", "bbb"])


class TestKeineZahl(unittest.TestCase):
    """`tick()` darf an keinem Eingangsdict sterben.

    `waiting` kommt aus `/get_load` per `.get()`, und `count_of()` gibt
    einen `float` oder einen Wahrheitswert durch - JSON erlaubt `3.0` und
    `true` an dieser Stelle. Ein Slice mit nicht-ganzzahliger Grenze wirft
    `TypeError`, und die Exception verlaesst `tick()`, nimmt den restlichen
    Zustand mit und friert `/api/state` auf dem letzten guten Tick ein.
    """

    def offene(self, anzahl=3):
        return {f"rid{index:02d}": 100.0 + index for index in range(anzahl)}

    def test_a_float_waiting_returns_an_empty_list(self):
        for wert in (3.0, 3.5, True):
            with self.subTest(wartende=wert):
                liste = build(self.offene(), waiting=wert, legacy=0,
                              resolve=lambda rid: None)
                self.assertEqual(liste, [])

    def test_a_string_waiting_returns_an_empty_list(self):
        liste = build(self.offene(), waiting="3", legacy=0,
                      resolve=lambda rid: None)
        self.assertEqual(liste, [])

    def test_no_waiting_at_all_is_an_empty_list(self):
        liste = build(self.offene(), waiting=0, legacy=0,
                      resolve=lambda rid: None)
        self.assertEqual(liste, [])

    def test_empty_open_rids_returns_an_empty_list(self):
        liste = build({}, waiting=0, legacy=0, resolve=lambda rid: None)
        self.assertEqual(liste, [])


class TestLaenge(unittest.TestCase):
    """Die Liste ist so lang wie die Queue, nicht laenger.

    Die offenen rids im Log sind mehr als die Wartenden: der laufende
    Auftrag ist auch offen. Ohne die Kuerzung zeigte die Liste Eintraege,
    die es nicht gibt - und eine Position, die der Nutzer sieht, aber die
    beim Klick nicht existiert.
    """

    def offene(self, anzahl):
        return {f"rid{index:02d}": 100.0 + index for index in range(anzahl)}

    def test_list_is_at_most_waiting_long(self):
        """Der gemessene Fall: 34 Wartende, 35 offene rids."""
        liste = build(self.offene(35), waiting=34, legacy=0,
                      resolve=lambda rid: None)
        self.assertEqual(len(liste), 34)

    def test_list_stops_at_waiting(self):
        liste = build(self.offene(3), waiting=1, legacy=0,
                      resolve=lambda rid: None)
        self.assertEqual(len(liste), 1)

    def test_the_running_holder_is_not_in_the_list(self):
        """Position 1 ist der naechste Wartende, nicht der laufende Auftrag.

        Bei `max_running_requests 1` ist der aelteste offene rid der
        **laufende** Auftrag: er wurde zuerst aus der Warteschlange in den
        Slot geholt. Mit ihm in der Liste waere Position 1 falsch - und der
        Abbruch von Position 1 wuerde den Halter treffen, also genau das,
        was der Wartenden-Weg nicht tun darf.
        """
        offene = {"laeuft": 100.0, "wartet1": 200.0, "wartet2": 300.0}
        liste = build(offene, waiting=2, legacy=0, resolve=lambda rid: None)
        self.assertEqual([e["rid"] for e in liste], ["wartet1", "wartet2"])
        self.assertEqual([e["position"] for e in liste], [1, 2])

    def test_without_a_running_request_nothing_is_skipped(self):
        """Offene rids == wartende rids heisst: es laeuft gerade nichts.

        Dann gibt es keinen Halter, den man ueberspringen duerfte. Die
        Liste ist genau die offenen rids, in Reihenfolge.
        """
        offene = {"wartet1": 100.0, "wartet2": 200.0}
        liste = build(offene, waiting=2, legacy=0, resolve=lambda rid: None)
        self.assertEqual([e["rid"] for e in liste], ["wartet1", "wartet2"])

    def test_a_negative_waiting_yields_nothing_not_a_truncated_tail(self):
        """`max(0, waiting)` ist kein Kosmetikum.

        `ordnung[:waiting]` mit negativem `waiting` schneidet das **Ende** ab:
        aus drei Wartenden wuerden zwei, aus einem keiner. Eine negative Zahl
        kann nicht entstehen - aber wenn doch, darf sie keine erfundene Liste
        liefern.
        """
        for wert in range(-7, 1):
            with self.subTest(wartende=wert):
                liste = build(self.offene(3), waiting=wert, legacy=0,
                              resolve=lambda rid: None)
                self.assertEqual(liste, [])