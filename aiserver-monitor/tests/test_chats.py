"""Tests fuer die Chat-Endpunkte ``/api/chats`` und ``/api/chats/search``.

Die Chats stammen aus der RAM-Job-Registry (voller Prompt plus Antwort-/
Denk-Vorschau), nicht aus der History-DB: die persistiert bewusst keinen
Volltext (siehe ``test_history``). ``/api/chats`` liefert die neuesten zuerst;
``/api/chats/search`` filtert nach Text (Prompt/Antwort/Denken), Modell,
Quelle und Zeitraum.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from aiserver_monitor.app import create_app
from aiserver_monitor.autoload import ProviderRouter
from aiserver_monitor.config import Config
from aiserver_monitor.jobs import JobStatus


class _Control:
    """Minimale ServerControl-Attrappe ohne Start-/Stop-Aufrufe."""

    backends: dict[str, object] = {}
    backend_names: tuple[str, ...] = ()

    async def snapshot(self) -> dict:
        return {
            "state": "stopped",
            "backend": None,
            "backends": [],
            "unit": None,
            "managed_by_systemd": False,
            "upstream": "http://127.0.0.1:30001",
            "error": None,
        }

    def refresh_from_registry(self, directory=None) -> bool:
        return True

    def log_lines(self, backend, lines=200) -> list[str]:
        return []


class ChatsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gantry-chats-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "providers"
        self.dir.mkdir()
        self.db = Path(self._tmp.name) / "monitor.db"

    def app(self):
        control = _Control()
        router = ProviderRouter(
            Config(db_path=str(self.db)), control,
            registry_dir=self.dir, registry_ttl_s=0.0)
        return create_app(
            Config(db_path=str(self.db)), server_control=control,
            provider_router=router, enable_background=False)

    @staticmethod
    def _registriere(client, rid, *, source="10.0.0.1", model="m",
                     prompt="Hallo", content="Antwort", reasoning="Denken",
                     status=JobStatus.DONE, created=1000.0,
                     prompt_tokens=3, completion_tokens=5):
        """Legt einen Job mit Text-Vorschau an und schliesst ihn ab."""
        registry = client.app.state.deps.registry
        registry.register(rid, source=source, model=model,
                          full_prompt=prompt, now=created)
        if reasoning:
            registry.add_text(rid, reasoning=reasoning)
        if content:
            registry.add_text(rid, content=content)
        registry.finish(rid, status, prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        now=created + 1.0)


class TestChatsListe(ChatsCase):
    def test_leere_registry_ist_leere_liste(self):
        with TestClient(self.app()) as client:
            antwort = client.get("/api/chats")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {"chats": []})

    def test_listet_felder_und_neueste_zuerst(self):
        app = self.app()
        with TestClient(app) as client:
            self._registriere(client, "a", model="m1", prompt="P1",
                              content="A1", reasoning="R1", created=1000.0)
            self._registriere(client, "b", model="m2", prompt="P2",
                              content="A2", reasoning="", created=2000.0)
            antwort = client.get("/api/chats")
        self.assertEqual(antwort.status_code, 200)
        chats = antwort.json()["chats"]
        self.assertEqual([chat["rid"] for chat in chats], ["b", "a"])
        erster = chats[0]
        self.assertEqual(erster["model"], "m2")
        self.assertEqual(erster["prompt"], "P2")
        self.assertEqual(erster["response"], "A2")
        self.assertEqual(erster["reasoning"], "")
        self.assertEqual(erster["status"], "done")
        self.assertEqual(erster["source"], "10.0.0.1")
        self.assertEqual(erster["prompt_tokens"], 3)
        self.assertEqual(erster["completion_tokens"], 5)
        # Das Feld heisst "prompt", nicht "full_prompt" (Registry-Internum).
        self.assertNotIn("full_prompt", erster)
        self.assertNotIn("content_preview", erster)

    def test_antwort_und_denken_als_vorschau(self):
        app = self.app()
        with TestClient(app) as client:
            self._registriere(client, "c", prompt="Frage?",
                              content="Die Antwort", reasoning="Das Denken",
                              created=3000.0)
            antwort = client.get("/api/chats")
        chat = antwort.json()["chats"][0]
        self.assertEqual(chat["response"], "Die Antwort")
        self.assertEqual(chat["reasoning"], "Das Denken")


class TestChatsSuche(ChatsCase):
    def setUp(self):
        super().setUp()
        self.app = self.app()
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self._registriere(self.client, "a", model="edge0-120b",
                          source="10.0.0.1", prompt="Wie ist das Wetter?",
                          content="Es regnet.", reasoning="Wetter-Denken",
                          created=1000.0)
        self._registriere(self.client, "b", model="qwen-coder",
                          source="10.0.0.2", prompt="Schreibe Code",
                          content="print('hi')", reasoning="",
                          created=2000.0)
        self._registriere(self.client, "c", model="edge0-120b",
                          source="10.0.0.3", prompt="Zusammenfassen",
                          content="Kurzfassung", reasoning="Denkblock",
                          created=3000.0)

    def test_suche_nach_text_in_prompt(self):
        antwort = self.client.get("/api/chats/search", params={"q": "wetter"})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual([c["rid"] for c in antwort.json()["chats"]], ["a"])

    def test_suche_nach_text_in_antwort_und_denken(self):
        antwort = self.client.get("/api/chats/search", params={"q": "regnet"})
        self.assertEqual([c["rid"] for c in antwort.json()["chats"]], ["a"])
        antwort = self.client.get("/api/chats/search", params={"q": "denkblock"})
        self.assertEqual([c["rid"] for c in antwort.json()["chats"]], ["c"])

    def test_suche_ist_case_insensitiv(self):
        antwort = self.client.get("/api/chats/search", params={"q": "WETTER"})
        self.assertEqual([c["rid"] for c in antwort.json()["chats"]], ["a"])

    def test_filter_nach_modell(self):
        antwort = self.client.get("/api/chats/search", params={"model": "edge0"})
        rids = [c["rid"] for c in antwort.json()["chats"]]
        self.assertEqual(rids, ["c", "a"])

    def test_filter_nach_quelle(self):
        antwort = self.client.get("/api/chats/search", params={"source": "10.0.0.2"})
        self.assertEqual([c["rid"] for c in antwort.json()["chats"]], ["b"])

    def test_filter_nach_zeitraum(self):
        # from/to sind Millisekunden (wie /monitor/stats); "b" liegt bei 2_000_000 ms.
        antwort = self.client.get("/api/chats/search", params={
            "from": 1_500_000, "to": 2_500_000})
        self.assertEqual([c["rid"] for c in antwort.json()["chats"]], ["b"])

    def test_kombinierte_filter(self):
        antwort = self.client.get("/api/chats/search", params={
            "model": "edge0", "from": 2_000_000})
        self.assertEqual([c["rid"] for c in antwort.json()["chats"]], ["c"])

    def test_kein_treffer_ist_leere_liste(self):
        antwort = self.client.get("/api/chats/search", params={"q": "gibts-nicht"})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {"chats": []})


class TestStatsAlias(ChatsCase):
    """``/api/stats`` ist ein Alias auf ``/monitor/stats`` (keine Doppelstruktur)."""

    def test_api_stats_liefert_gleiche_antwort_wie_monitor_stats(self):
        with TestClient(self.app()) as client:
            monitor = client.get("/monitor/stats", params={"bucket": 60})
            api = client.get("/api/stats", params={"bucket": 60})
        self.assertEqual(monitor.status_code, 200)
        self.assertEqual(api.status_code, 200)
        self.assertEqual(monitor.json(), api.json())


if __name__ == "__main__":
    unittest.main()
