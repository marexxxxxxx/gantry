"""Tests fuer die regelbasierte Denk-Eskalation (Modus ``auto``)."""

import unittest

from aiserver_monitor.thinking_auto import STUFEN, ThinkingEscalator


def _body(text: str = "Hallo") -> dict:
    return {"messages": [{"role": "user", "content": text}]}


class ThinkingEscalatorTests(unittest.TestCase):
    def test_erster_versuch_denkt_nicht(self):
        plan = ThinkingEscalator().plan(_body())
        self.assertEqual((plan.stufe, plan.versuch), ("none", 1))
        self.assertIsNone(plan.budget)

    def test_zweiter_versuch_denkt_kurz(self):
        eskalator = ThinkingEscalator()
        eskalator.plan(_body())
        plan = eskalator.plan(_body())
        self.assertEqual((plan.stufe, plan.versuch), ("low", 2))
        self.assertEqual(plan.budget, 1024)

    def test_dritter_versuch_denkt_lang(self):
        eskalator = ThinkingEscalator()
        for _ in range(2):
            eskalator.plan(_body())
        plan = eskalator.plan(_body())
        self.assertEqual((plan.stufe, plan.versuch), ("medium", 3))
        self.assertEqual(plan.budget, 8192)

    def test_stufe_steigt_nicht_ueber_das_maximum(self):
        eskalator = ThinkingEscalator()
        for _ in range(10):
            plan = eskalator.plan(_body())
        self.assertEqual(plan.stufe, STUFEN[-1])
        self.assertEqual(plan.versuch, 10)

    def test_anderer_verlauf_ist_ein_neuer_versuch(self):
        eskalator = ThinkingEscalator()
        eskalator.plan(_body("Hallo"))
        plan = eskalator.plan(_body("Andere Frage"))
        self.assertEqual(plan.stufe, "none")
        self.assertEqual(plan.versuch, 1)

    def test_tool_ergebnis_zaehlt_als_neuer_schritt(self):
        # Ein Agenten-Schritt haengt ein Tool-Ergebnis an; das ist kein Retry.
        eskalator = ThinkingEscalator()
        erst = {"messages": [{"role": "user", "content": "ls"}]}
        zweit = {"messages": erst["messages"] + [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a b c"},
        ]}
        eskalator.plan(erst)
        self.assertEqual(eskalator.plan(erst).stufe, "low")
        self.assertEqual(eskalator.plan(zweit).stufe, "none")

    def test_verlauf_ohne_messages_ist_ein_eigener_schluessel(self):
        eskalator = ThinkingEscalator()
        self.assertEqual(eskalator.plan({}).stufe, "none")
        self.assertEqual(eskalator.plan({}).stufe, "low")

    def test_fenster_ablauf_setzt_zurueck(self):
        jetzt = [1000.0]
        eskalator = ThinkingEscalator(window_s=60.0, clock=lambda: jetzt[0])
        eskalator.plan(_body())
        self.assertEqual(eskalator.plan(_body()).stufe, "low")
        jetzt[0] += 61.0
        self.assertEqual(eskalator.plan(_body()).stufe, "none")
        self.assertEqual(len(eskalator), 1)

    def test_reset_leert_den_zaehler(self):
        eskalator = ThinkingEscalator()
        eskalator.plan(_body())
        self.assertEqual(len(eskalator), 1)
        eskalator.reset()
        self.assertEqual(len(eskalator), 0)
        self.assertEqual(eskalator.plan(_body()).versuch, 1)

    def test_eigene_budgets_gehen_vor(self):
        eskalator = ThinkingEscalator(
            budgets={"none": None, "low": 256, "medium": 4096})
        eskalator.plan(_body())
        self.assertEqual(eskalator.plan(_body()).budget, 256)
        self.assertEqual(eskalator.plan(_body()).budget, 4096)

    def test_null_fenster_wird_abgelehnt(self):
        with self.assertRaises(ValueError):
            ThinkingEscalator(window_s=0)

    def test_kein_budget_fuer_fehlende_stufe(self):
        # budgets ohne "medium": die Stufe bleibt, das Budget ist None.
        eskalator = ThinkingEscalator(budgets={"none": None, "low": 128})
        eskalator.plan(_body())
        eskalator.plan(_body())
        self.assertIsNone(eskalator.plan(_body()).budget)


if __name__ == "__main__":
    unittest.main()
