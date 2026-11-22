"""Tests fuer die Max-Priorisierung: Marker, An/Aus und Persistenz."""

import json
import unittest

from aiserver_monitor.priority import (
    DEFAULT_MARKER,
    MAX_PRIORITY,
    NORMAL_PRIORITY,
    PriorityManager,
    matches_markers,
    normalize_markers,
)


class FakeStore:
    """Minimaler Settings-Store fuer die Manager-Tests (kein SQLite)."""

    def __init__(self) -> None:
        self.werte: dict[str, str] = {}

    def get_setting(self, key: str) -> str | None:
        return self.werte.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.werte[key] = value


def body(*messages: dict, **felder) -> dict:
    """Chat-Body mit den gegebenen Nachrichten und optionalen Extra-Feldern."""
    return {"model": "m", "messages": list(messages), **felder}


def system(text: str) -> dict:
    return {"role": "system", "content": text}


class TestNormalizeMarkers(unittest.TestCase):
    def test_trimmt_und_entfernt_duplikate_ohne_schreibweise(self):
        self.assertEqual(
            normalize_markers(["  You are Max  ", "you ARE max", "", "  "]),
            ("You are Max",))

    def test_leere_liste_ist_erlaubt(self):
        self.assertEqual(normalize_markers([]), ())

    def test_system_praefix_wird_entfernt(self):
        # Marker aus Logs tragen das Rollenlabel "system: " und wuerden
        # sonst nie gegen den reinen Nachrichteninhalt matchen.
        self.assertEqual(
            normalize_markers(["system: Du bist Max", "SYSTEM: Noch einer"]),
            ("Du bist Max", "Noch einer"))

    def test_nicht_liste_wird_abgelehnt(self):
        with self.assertRaises(ValueError):
            normalize_markers("You are Max")

    def test_nicht_string_wird_abgelehnt(self):
        with self.assertRaises(ValueError):
            normalize_markers(["ok", 3])


class TestMatchesMarkers(unittest.TestCase):
    def test_system_marker_matcht_ignoriert_gross_kleinschreibung_und_abstaende(self):
        koerper = body(system(
            "  you   ARE max, built for Marec.\nBe direct: ..."))
        self.assertTrue(matches_markers(koerper, [DEFAULT_MARKER]))

    def test_marker_im_user_text_matcht_nicht(self):
        koerper = body({"role": "user", "content": DEFAULT_MARKER})
        self.assertFalse(matches_markers(koerper, [DEFAULT_MARKER]))

    def test_marker_im_assistant_text_matcht_nicht(self):
        koerper = body({"role": "assistant", "content": DEFAULT_MARKER})
        self.assertFalse(matches_markers(koerper, [DEFAULT_MARKER]))

    def test_einer_von_mehreren_systemtexte_reicht(self):
        koerper = body(system("Anderer Prompt"), system(DEFAULT_MARKER))
        self.assertTrue(matches_markers(koerper, [DEFAULT_MARKER]))

    def test_ohne_marker_kein_treffer(self):
        self.assertFalse(matches_markers(body(system(DEFAULT_MARKER)), []))

    def test_marker_mit_system_praefix_matcht_den_systemtext(self):
        # Im Dashboard werden Marker oft aus Logs kopiert; dort steht
        # "system: " vor dem eigentlichen Prompt. Der Matcher vergleicht
        # aber nur den Nachrichteninhalt, nicht das Rollenlabel.
        koerper = body(system("CRITICAL: You MUST ONLY use facts."))
        marker = "system: CRITICAL: You MUST ONLY use facts."
        self.assertTrue(matches_markers(koerper, [marker]))


class TestPriorityManager(unittest.TestCase):
    def test_defaults_sind_an_und_enthalten_max_marker(self):
        manager = PriorityManager()
        self.assertEqual(manager.as_dict(), {
            "enabled": True,
            "markers": [DEFAULT_MARKER],
        })

    def test_max_request_bekommt_hohe_prioritaet(self):
        manager = PriorityManager()
        koerper = body(system(DEFAULT_MARKER))

        self.assertTrue(manager.mark(koerper))
        self.assertEqual(koerper["priority"], MAX_PRIORITY)

    def test_anderer_request_bekommt_normale_prioritaet(self):
        manager = PriorityManager()
        koerper = body(system("You are someone else"))

        self.assertFalse(manager.mark(koerper))
        self.assertEqual(koerper["priority"], NORMAL_PRIORITY)

    def test_aus_entfernt_vorhandene_prioritaet(self):
        manager = PriorityManager()
        manager.update(enabled=False, markers=[DEFAULT_MARKER])
        koerper = body(system(DEFAULT_MARKER), priority=999)

        self.assertFalse(manager.mark(koerper))
        self.assertNotIn("priority", koerper)

    def test_update_trimmt_marker_und_persistiert(self):
        store = FakeStore()
        manager = PriorityManager(store)
        manager.update(enabled=False, markers=["  Neu  ", "neu", "Zweiter"])

        self.assertEqual(manager.as_dict(), {
            "enabled": False,
            "markers": ["Neu", "Zweiter"],
        })
        gespeichert = json.loads(store.werte[PriorityManager.KEY])
        self.assertEqual(gespeichert, {
            "enabled": False,
            "markers": ["Neu", "Zweiter"],
        })

    def test_neuer_manager_laedt_gespeicherte_einstellungen(self):
        store = FakeStore()
        PriorityManager(store).update(enabled=False, markers=["Neu"])

        self.assertEqual(PriorityManager(store).as_dict(), {
            "enabled": False,
            "markers": ["Neu"],
        })

    def test_kaputter_store_faellt_auf_defaults_zurueck(self):
        store = FakeStore()
        store.werte[PriorityManager.KEY] = "{kein json"

        with self.assertLogs("aiserver_monitor.priority",
                             level="WARNING") as cm:
            self.assertEqual(PriorityManager(store).as_dict(), {
                "enabled": True,
                "markers": [DEFAULT_MARKER],
            })
        self.assertIn("ungueltig", "\n".join(cm.output))

    def test_update_lehnt_nicht_bool_ab(self):
        with self.assertRaises(ValueError):
            PriorityManager().update(enabled="true", markers=[])


if __name__ == "__main__":
    unittest.main()
