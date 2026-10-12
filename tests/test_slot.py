import unittest

from slotmon.slot import evaluate


class TestHolder(unittest.TestCase):
    def test_oldest_rid_is_holder(self):
        state = evaluate({"aaa": 100.0, "bbb": 300.0, "ccc": 200.0}, waiting=0)
        self.assertEqual(state.holder_rid, "aaa")
        self.assertEqual(state.holder_since, 100.0)

    def test_single_open_rid_is_holder(self):
        state = evaluate({"aaa": 100.0}, waiting=0)
        self.assertEqual(state.holder_rid, "aaa")

    def test_no_open_rid_means_free(self):
        state = evaluate({}, waiting=40)
        self.assertIsNone(state.holder_rid)
        self.assertIsNone(state.holder_since)
        self.assertEqual(state.open_count, 0)


class TestLegacy(unittest.TestCase):
    def test_legacy_is_queue_minus_open(self):
        state = evaluate({"a": 1.0, "b": 2.0, "c": 3.0}, waiting=2301)
        self.assertEqual(state.legacy, 2298)
        self.assertEqual(state.open_count, 3)

    def test_legacy_blocks_the_holder(self):
        state = evaluate({"a": 1.0}, waiting=2301)
        self.assertEqual(state.legacy, 2300)
        self.assertIsNone(state.holder_rid)
        self.assertFalse(state.complete)

    def test_zero_legacy_means_complete(self):
        state = evaluate({"a": 1.0}, waiting=1)
        self.assertEqual(state.legacy, 0)
        self.assertEqual(state.holder_rid, "a")
        self.assertTrue(state.complete)

    def test_unknown_waiting_yields_unknown_legacy(self):
        state = evaluate({"a": 1.0}, waiting=None)
        self.assertIsNone(state.legacy)
        self.assertIsNone(state.holder_rid)
        self.assertFalse(state.complete)

    def test_negative_result_is_clamped_to_zero(self):
        state = evaluate({"a": 1.0, "b": 2.0}, waiting=0)
        self.assertEqual(state.legacy, 0)
        self.assertTrue(state.complete)


class TestUnusableWaiting(unittest.TestCase):
    """Ein `waiting`, das keine Zahl ist, ist **unbekannt**, nicht null.

    `evaluate()` bekommt `waiting` aus `/get_load`. sglang ist JSON, also
    steht dort normalerweise eine Zahl - aber JSON erlaubt auch `"180"`
    oder `true`, und `sources.py:63` holt den Wert mit `.get()`, ohne ihn
    zu pruefen. `max(0, "0" - 0)` wirft `TypeError`, und damit verlaesst
    die Exception `tick()`.

    Das widerspricht der Zusicherung "keine Quelle wirft aus `tick()`"
    und der Fehlerbehandlung "Ist nur eine Quelle tot, zeigen die
    uebrigen Kacheln weiter; Monitor laeuft weiter".

    Befund aus Task 8, dort bewusst auf den Whole-Branch-Review
    verschoben, weil `slot.py` dort nicht freigegeben war.
    """

    def test_a_numeric_string_is_not_a_number(self):
        """`"0"` ist keine 0. Es ist eine Behauptung, die wir nicht lesen koennen."""
        state = evaluate({}, waiting="0")
        self.assertIsNone(state.legacy)
        self.assertFalse(state.complete)
        self.assertIsNone(state.holder_rid)

    def test_a_non_empty_string_yields_unknown_too(self):
        state = evaluate({"a": 1.0}, waiting="180")
        self.assertIsNone(state.legacy)
        self.assertIsNone(state.holder_rid)

    def test_a_bool_is_not_a_count(self):
        """`True` ist im JSON eine Zahl, aber keine Warteschlangenlaenge.

        `bool` ist eine Subklasse von `int` in Python; `max(0, True - 0)`
        ergaebe 1 und damit eine erfundene Wartenden-Zahl.
        """
        state = evaluate({}, waiting=True)
        self.assertIsNone(state.legacy)

    def test_a_float_count_still_works(self):
        """Kein Ueberdrehen: `180.0` ist eine Zahl und bleibt eine."""
        state = evaluate({"a": 1.0}, waiting=180.0)
        self.assertEqual(state.legacy, 179)


class TestHolderSinceClock(unittest.TestCase):
    """`holder_since` ist Unix-Zeit aus dem Log-`timestamp`, nicht `received_time`.

    Live gemessen am echten Server: `timestamp` -> 1791393308.0 (Unix),
    `obj.received_time` -> 469943.46 (monotoner Zaehler). Wer die beiden
    verwechselt, rechnet eine Laufzeit von -1.780.923.364 s.
    """

    UNIX_TS = 1791393308.0
    RECEIVED_TIME = 469943.46

    def test_holder_since_is_returned_unchanged(self):
        state = evaluate({"aaa": self.UNIX_TS}, waiting=0)
        self.assertEqual(state.holder_since, self.UNIX_TS)

    def test_holder_since_is_subtractable_from_wall_clock(self):
        import time as _time

        state = evaluate({"aaa": self.UNIX_TS}, waiting=0)
        elapsed = _time.time() - state.holder_since
        self.assertGreater(elapsed, 0)

    def test_monotonic_received_time_is_not_mistaken_for_unix(self):
        """Gegenprobe: eine received_time als offene Zeit ergaebe Unsinn."""
        state = evaluate({"aaa": self.RECEIVED_TIME}, waiting=0)
        self.assertEqual(state.holder_since, self.RECEIVED_TIME)
        # evaluate() rechnet nicht um - es gibt genau das zurueck, was reinkommt.
        # Der Unterschied muss der Aufrufer kennen, sonst entsteht die negative
        # Laufzeit. Hier festgehalten, damit der Unterschied nicht wegoptimiert wird.
        self.assertLess(state.holder_since, 1e9)


if __name__ == "__main__":
    unittest.main()
