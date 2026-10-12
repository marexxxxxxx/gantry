"""Der eine schreibende Zugriff des Monitors.

Jeder Test hier prueft eine Sache, die **nicht** passieren darf. Ein
"funktioniert"-Test waere wertlos - der Abbruch funktioniert oder er
funktioniert nicht, und interessant ist ausschliesslich, wo er zerstoert.
"""

import json
import unittest
from unittest import mock

import httpx

from slotmon import abort


class AbortCase(unittest.TestCase):
    def setUp(self):
        self.cfg = mock.Mock()
        self.cfg.api = "http://127.0.0.1:30000"
        self.cfg.auth = "Bearer marec"
        self.busy = {
            "slot": {"state": "busy", "rid": "aaa111", "system": None,
                     "ip_port": None, "legacy": 0, "reason": None,
                     "open_count": 1, "complete": True,
                     "duration_s": 12.0, "started_at": 100.0},
            "queue": {"waiting": 2, "running": 1,
                      "waiting_list": [
                          {"position": 1, "rid": "bbb222", "waiting_since": 90.0,
                           "system": None},
                          {"position": 2, "rid": "ccc333", "waiting_since": 80.0,
                           "system": "Laptop"}]},
        }
        # Jeder Test laeuft unter diesem Mock. Vergisst ein Test den Patch,
        # wuerde er sonst den echten Server auf Port 30000 erreichen und dort
        # einen Abbruch ausloesen.
        patcher = mock.patch.object(abort.httpx, "post")
        self.httpx_post = patcher.start()
        self.addCleanup(patcher.stop)
        self.httpx_post.return_value = mock.Mock(status_code=200)

    def post(self, ergebnis):
        """Gibt das Ergebnis und den gemockten POST dazu zurueck.

        Der Patch laeuft seit `setUp`. Die Auswertung des Arguments passiert
        also unter dem Mock, egal in welcher Reihenfolge Python die Ausdruecke
        ausfuehrt.
        """
        return ergebnis, self.httpx_post


class TestDerErzeugteRequest(AbortCase):
    """Der erzeugte Request, nicht die Absicht.

    `io_struct.py:1541` macht aus `rid=None` die leere Zeichenkette, und
    `scheduler.py:2814` vergleicht mit `req.rid.startswith(recv_req.rid)`.
    `"x".startswith("")` ist **wahr** - eine leere rid wuerde die ganze
    Warteschlange loeschen, ohne dass `abort_all` gesetzt waere. Gemessen am
    echten Server: der `rid_to_state`-Guard im TokenizerManager haelt das auf,
    aber wir verlassen uns nicht auf eine fremde Verteidigung.
    """

    def test_sendet_nie_eine_leere_rid(self):
        ergebnis, post = self.post(abort.abort_holder(self.cfg, self.busy))
        body = post.call_args.kwargs["json"]
        self.assertNotEqual(body["rid"], "")
        self.assertTrue(body["rid"], "leere rid im Request")

    def test_setzt_nie_abort_all(self):
        _, post = self.post(abort.abort_holder(self.cfg, self.busy))
        body = post.call_args.kwargs["json"]
        self.assertNotIn("abort_all", body)
        self.assertIs(body.get("abort_all", False), False)

    def test_sendet_genau_eine_rid(self):
        _, post = self.post(abort.abort_holder(self.cfg, self.busy))
        body = post.call_args.kwargs["json"]
        self.assertIsInstance(body["rid"], str)
        self.assertEqual(body["rid"], "aaa111")

    def test_sendet_nie_eine_liste(self):
        """`abort_all` ist die Sammelvariante. Es darf keine geben."""
        _, post = self.post(abort.abort_waiting(self.cfg, self.busy, 2))
        body = post.call_args.kwargs["json"]
        self.assertNotIsInstance(body, list)


class TestHalterGuards(AbortCase):
    def test_kein_halter_heisst_kein_post(self):
        zustand = json.loads(json.dumps(self.busy))
        zustand["slot"]["state"] = "free"
        zustand["slot"]["rid"] = None
        ergebnis, post = self.post(abort.abort_holder(self.cfg, zustand))
        post.assert_not_called()
        self.assertFalse(ergebnis["aborted"])
        self.assertIn("Kein Auftrag", ergebnis["reason"])

    def test_nur_busy_mit_rid_ist_ein_halter(self):
        """Stufe 1 verlangt `busy` UND eine rid. Eine rid bei `free` ist ein Widerspruch."""
        for status in ("free", "unknown"):
            zustand = json.loads(json.dumps(self.busy))
            zustand["slot"]["state"] = status
            ergebnis, post = self.post(abort.abort_holder(self.cfg, zustand))
            post.assert_not_called()
            self.assertFalse(ergebnis["aborted"])

    def test_altbestand_blockiert_den_halter(self):
        zustand = json.loads(json.dumps(self.busy))
        zustand["slot"]["legacy"] = 3
        ergebnis, post = self.post(abort.abort_holder(self.cfg, zustand))
        post.assert_not_called()
        self.assertIn("Altbestand", ergebnis["reason"])

    def test_ein_reason_blockiert_den_halter(self):
        """`reason` gesetzt heisst: der Monitor bezweifelt seinen Halter.

        Toter Code im Normalfall (`reason` wird nur bei `unknown` gesetzt),
        aber die Stelle faengt den Widerspruch, falls `state` spaeter
        erweitert wird.
        """
        zustand = json.loads(json.dumps(self.busy))
        zustand["slot"]["reason"] = "Geister-rid"
        ergebnis, post = self.post(abort.abort_holder(self.cfg, zustand))
        post.assert_not_called()
        self.assertIn("Geister", ergebnis["reason"])

    def test_leere_rid_am_halter_heisst_kein_post(self):
        """Stufe 7 der Sicherheitskette: die rid ist nicht leer.

        Konstruktionsbedingt, aber die Spec verlangt den Test.
        """
        zustand = json.loads(json.dumps(self.busy))
        zustand["slot"]["rid"] = ""
        ergebnis, post = self.post(abort.abort_holder(self.cfg, zustand))
        post.assert_not_called()
        self.assertFalse(ergebnis["aborted"])


class TestWartendeGuards(AbortCase):
    def test_position_eins_ist_der_erste_eintrag(self):
        _, post = self.post(abort.abort_waiting(self.cfg, self.busy, 1))
        self.assertEqual(post.call_args.kwargs["json"]["rid"], "bbb222")

    def test_position_zwei_ist_der_zweite_eintrag(self):
        _, post = self.post(abort.abort_waiting(self.cfg, self.busy, 2))
        self.assertEqual(post.call_args.kwargs["json"]["rid"], "ccc333")

    def test_position_ueber_die_liste_hinaus_sendet_nichts(self):
        ergebnis, post = self.post(abort.abort_waiting(self.cfg, self.busy, 3))
        post.assert_not_called()
        self.assertIn("gibt es nicht", ergebnis["reason"])

    def test_position_null_und_negativ_senden_nichts(self):
        for position in (0, -1, -99):
            ergebnis, post = self.post(
                abort.abort_waiting(self.cfg, self.busy, position))
            post.assert_not_called()
            self.assertFalse(ergebnis["aborted"])

    def test_nicht_ganzzahl_sendet_nichts(self):
        """Text und Wahrheitswerte sind keine Position, auch wenn Python sie zaehlt."""
        for position in ("2", True, 2.0, None):
            ergebnis, post = self.post(
                abort.abort_waiting(self.cfg, self.busy, position))
            post.assert_not_called()
            self.assertFalse(ergebnis["aborted"])

    def test_altbestand_blockiert_die_ganze_liste(self):
        zustand = json.loads(json.dumps(self.busy))
        zustand["slot"]["legacy"] = 2
        ergebnis, post = self.post(abort.abort_waiting(self.cfg, zustand, 1))
        post.assert_not_called()
        self.assertIn("Altbestand", ergebnis["reason"])

    def test_leere_rid_in_der_liste_heisst_kein_post(self):
        for wert in ("", None):
            zustand = json.loads(json.dumps(self.busy))
            zustand["queue"]["waiting_list"][0]["rid"] = wert
            ergebnis, post = self.post(abort.abort_waiting(self.cfg, zustand, 1))
            post.assert_not_called()
            self.assertFalse(ergebnis["aborted"])

    def test_die_position_erreicht_den_server_nie_roh(self):
        """Der Nutzer tippt eine Zahl; die rid loest der Monitor auf.

        Damit kann keine rid verwechselt werden - die Zahl, die er sieht,
        ist genau die, die gemeint ist.
        """
        _, post = self.post(abort.abort_waiting(self.cfg, self.busy, 2))
        self.assertNotIn("position", post.call_args.kwargs["json"])


class TestServerVerhalten(AbortCase):
    def test_toter_server_wirft_nicht(self):
        with mock.patch.object(abort.httpx, "post",
                               side_effect=httpx.ConnectError("down")):
            ergebnis = abort.abort_holder(self.cfg, self.busy)
        self.assertFalse(ergebnis["aborted"])
        self.assertIn("nicht erreichbar", ergebnis["reason"])

    def test_http_500_wird_gemeldet_nicht_geworfen(self):
        self.httpx_post.return_value = mock.Mock(status_code=500)
        ergebnis = abort.abort_holder(self.cfg, self.busy)
        self.assertFalse(ergebnis["aborted"])
        self.assertIn("500", ergebnis["reason"])

    def test_erfolg_wird_nur_bei_http_200_gemeldet(self):
        self.httpx_post.return_value = mock.Mock(status_code=200)
        ergebnis = abort.abort_holder(self.cfg, self.busy)
        self.assertTrue(ergebnis["aborted"])
        self.assertEqual(ergebnis["rid"], "aaa111")


if __name__ == "__main__":
    unittest.main()
