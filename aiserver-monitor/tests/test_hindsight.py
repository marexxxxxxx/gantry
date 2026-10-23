"""Tests fuer den Hindsight-Blocker: Marker, An/Aus und Persistenz."""

import json
import unittest

from aiserver_monitor.hindsight import (
    DEFAULT_HINDSIGHT_MARKERS,
    HindsightBlocker,
    HindsightSettings,
)
from aiserver_monitor.priority import normalize_markers


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


class TestDefaults(unittest.TestCase):
    def test_defaults_sind_aus_und_enthalten_beide_hindsight_marker(self):
        blocker = HindsightBlocker()
        self.assertEqual(blocker.as_dict(), {
            "enabled": False,
            "markers": list(DEFAULT_HINDSIGHT_MARKERS),
        })
        self.assertEqual(DEFAULT_HINDSIGHT_MARKERS, (
            "Extract SIGNIFICANT facts from text",
            "You are a memory consolidation system",
        ))

    def test_settings_as_dict_liefert_kopie_der_marker(self):
        einstellungen = HindsightSettings(True, ("A", "B"))
        daten = einstellungen.as_dict()
        daten["markers"].append("C")
        self.assertEqual(einstellungen.markers, ("A", "B"))


class TestMatches(unittest.TestCase):
    def test_extraktion_matcht_im_systemprompt(self):
        blocker = HindsightBlocker()
        koerper = body(system(
            "Extract SIGNIFICANT facts from text. Be SELECTIVE ..."))
        self.assertTrue(blocker.matches(koerper))

    def test_konsolidierung_matcht_im_systemprompt(self):
        blocker = HindsightBlocker()
        koerper = body(system(
            "You are a memory consolidation system. Synthesize new facts ..."))
        self.assertTrue(blocker.matches(koerper))

    def test_gross_kleinschreibung_und_abstaende_sind_egal(self):
        blocker = HindsightBlocker()
        koerper = body(system(
            "  extract   significant FACTS\nfrom text.  "))
        self.assertTrue(blocker.matches(koerper))

    def test_marker_nur_im_user_text_matcht_nicht(self):
        blocker = HindsightBlocker()
        koerper = body({
            "role": "user",
            "content": "Extract SIGNIFICANT facts from text",
        })
        self.assertFalse(blocker.matches(koerper))

    def test_ohne_marker_kein_treffer(self):
        blocker = HindsightBlocker()
        blocker.update(enabled=True, markers=[])
        self.assertFalse(blocker.matches(body(system(
            "Extract SIGNIFICANT facts from text"))))


class TestBlocked(unittest.TestCase):
    def test_aus_blockiert_nicht(self):
        blocker = HindsightBlocker()
        koerper = body(system(
            "Extract SIGNIFICANT facts from text. Be SELECTIVE ..."))
        self.assertFalse(blocker.blocked(koerper))

    def test_an_blockiert_hindsight(self):
        blocker = HindsightBlocker()
        blocker.update(enabled=True, markers=list(DEFAULT_HINDSIGHT_MARKERS))
        koerper = body(system(
            "You are a memory consolidation system. Synthesize ..."))
        self.assertTrue(blocker.blocked(koerper))

    def test_an_blockiert_andere_requests_nicht(self):
        blocker = HindsightBlocker()
        blocker.update(enabled=True, markers=list(DEFAULT_HINDSIGHT_MARKERS))
        koerper = body(system("You are Max, built for Marec"))
        self.assertFalse(blocker.blocked(koerper))


class TestUpdateUndPersistenz(unittest.TestCase):
    def test_update_trimmt_marker_und_persistiert(self):
        store = FakeStore()
        blocker = HindsightBlocker(store)
        blocker.update(enabled=True, markers=["  Neu  ", "neu", "Zweiter"])

        self.assertEqual(blocker.as_dict(), {
            "enabled": True,
            "markers": ["Neu", "Zweiter"],
        })
        gespeichert = json.loads(store.werte[HindsightBlocker.KEY])
        self.assertEqual(gespeichert, {
            "enabled": True,
            "markers": ["Neu", "Zweiter"],
        })

    def test_neuer_manager_laedt_gespeicherte_einstellungen(self):
        store = FakeStore()
        HindsightBlocker(store).update(enabled=True, markers=["Neu"])

        self.assertEqual(HindsightBlocker(store).as_dict(), {
            "enabled": True,
            "markers": ["Neu"],
        })

    def test_kaputter_store_faellt_auf_defaults_zurueck(self):
        store = FakeStore()
        store.werte[HindsightBlocker.KEY] = "{kein json"

        with self.assertLogs("aiserver_monitor.hindsight",
                             level="WARNING") as cm:
            self.assertEqual(HindsightBlocker(store).as_dict(), {
                "enabled": False,
                "markers": list(DEFAULT_HINDSIGHT_MARKERS),
            })
        self.assertIn("ungueltig", "\n".join(cm.output))

    def test_update_lehnt_nicht_bool_ab(self):
        with self.assertRaises(ValueError):
            HindsightBlocker().update(enabled="true", markers=[])

    def test_update_lehnt_ungueltige_marker_ab(self):
        with self.assertRaises(ValueError):
            HindsightBlocker().update(enabled=True, markers=[42])

    def test_normalize_markers_bleibt_die_gemeinsame_validierung(self):
        with self.assertRaises(ValueError):
            normalize_markers("kein liste")


if __name__ == "__main__":
    unittest.main()
