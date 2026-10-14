"""Tests fuer den /v1-Passthrough (proxy.py).

Die Tests rufen ``handle_v1`` direkt an (Task 10 haengt die Funktion in die
App) und lesen die Antwort so, wie der Client sie bekommt: die Bytes des
Streaming-Generators beziehungsweise der gepufferte Body.

Der Upstream ist der Fake-SGLang aus ``fakes.py``; die meisten Faelle laufen
ueber ``httpx.ASGITransport`` (keine Sockets). Wo echte Zeitabstaende
geprueft werden (Keepalive, Client-Abbruch), puffert der ASGITransport die
Antwort und taugt nicht; dort liefert ``skript_transport`` denselben
Byte-Strom zeitgetreu.
"""

import asyncio
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

import fakes
from aiserver_monitor.config import Config
from aiserver_monitor.events import EventHub
from aiserver_monitor.history import History
from aiserver_monitor.jobs import JobRegistry, JobStatus, job_to_dict
from aiserver_monitor.proxy import Deps, ensure_rid, handle_v1

#: rid-Muster des Gateways: uuid4().hex
RID_MUSTER = re.compile(r"^[0-9a-f]{32}$")

#: Der Keepalive-Kommentar, wie er zum Client geht.
PING = b": ping\n\n"


def _request(methode: str, pfad: str, body: dict | None = None, *,
             query: str = "", client_ip: str | None = "10.0.0.5") -> Request:
    """Baut eine Starlette-Anfrage ohne ASGI-Lauf."""
    roh = b"" if body is None else json.dumps(body).encode("utf-8")
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": methode,
        "scheme": "http",
        "path": pfad,
        "raw_path": pfad.encode("utf-8"),
        "query_string": query.encode("ascii"),
        "headers": ([(b"content-type", b"application/json")]
                    if body is not None else []),
        "client": None if client_ip is None else (client_ip, 4711),
        "server": ("testserver", 80),
    }
    ausgeliefert = False

    async def receive():
        nonlocal ausgeliefert
        if ausgeliefert:
            return {"type": "http.disconnect"}
        ausgeliefert = True
        return {"type": "http.request", "body": roh, "more_body": False}

    return Request(scope, receive)


async def _body(antwort) -> bytes:
    """Die Antwort so lesen, wie Starlette sie senden wuerde."""
    if not isinstance(antwort, StreamingResponse):
        return antwort.body
    teile = []
    async for teil in antwort.body_iterator:
        teile.append(teil if isinstance(teil, bytes) else teil.encode("utf-8"))
    return b"".join(teile)


async def _warte_bis(predicate, *, timeout_s: float = 2.0) -> bool:
    """Wartet bis zur Zeitgrenze darauf, dass ``predicate()`` wahr wird."""
    ende = time.monotonic() + timeout_s
    while time.monotonic() < ende:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _chat_skript(tokens: list[str], *,
                 usage: dict | None = None) -> list[fakes.Schritt]:
    """Skript wie SGLang: Rollen-Chunk, Tokens, finish_reason, usage, [DONE]."""
    schritte = [(0.0, fakes.chat_chunk({"role": "assistant", "content": ""}))]
    schritte += [(0.0, fakes.chat_chunk({"content": token})) for token in tokens]
    schritte.append((0.0, fakes.chat_chunk({}, finish_reason="stop")))
    if usage is not None:
        schritte.append((0.0, fakes.chat_chunk({}, usage=usage)))
    schritte.append((0.0, fakes.DONE))
    return schritte


class ProxyTestBasis(unittest.IsolatedAsyncioTestCase):
    """Gemeinsamer Aufbau: Registry, Hub und ein httpx-Client je Test."""

    async def asyncSetUp(self):
        self.registry = JobRegistry(preview_max_chars=4096)
        self.hub = EventHub()
        self._client: httpx.AsyncClient | None = None

    def deps(self, transport, *, history=None, heartbeat_s: float = 15.0,
             connect_timeout_s: float = 5.0) -> Deps:
        """Deps mit dem gegebenen Transport; der Client wird am Testende geschlossen."""
        if self._client is None:
            self._client = httpx.AsyncClient(transport=transport)
            self.addAsyncCleanup(self._client.aclose)
        return Deps(
            config=Config(heartbeat_s=heartbeat_s,
                          connect_timeout_s=connect_timeout_s),
            registry=self.registry,
            hub=self.hub,
            history=history,
            client=self._client,
        )


class TestEnsureRid(unittest.TestCase):
    def test_vorhandene_rid_bleibt_unveraendert(self):
        body = {"rid": "abc", "stream": True}
        self.assertEqual(ensure_rid(body), "abc")
        self.assertEqual(body["rid"], "abc")

    def test_fehlende_rid_wird_gesetzt(self):
        body = {"stream": True}
        rid = ensure_rid(body)
        self.assertEqual(body["rid"], rid)
        self.assertRegex(rid, RID_MUSTER)

    def test_leere_oder_fremdartige_rid_wird_ersetzt(self):
        for alt in ("", None, 123):
            with self.subTest(rid=repr(alt)):
                body = {"rid": alt}
                rid = ensure_rid(body)
                self.assertRegex(rid, RID_MUSTER)
                self.assertEqual(body["rid"], rid)


class TestStreamRelay(ProxyTestBasis):
    async def test_stream_relay_bytes_identisch(self):
        schritte = _chat_skript(
            ["Hallo", " Welt"],
            usage={"prompt_tokens": 7, "completion_tokens": 4,
                   "total_tokens": 11})
        fake = fakes.FakeSglang(schritte)
        deps = self.deps(httpx.ASGITransport(app=fake.app))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"model": "fake-modell", "rid": "relay-1", "stream": True,
                      "messages": [{"role": "user", "content": "Hi"}]}),
            deps)

        self.assertEqual(antwort.status_code, 200)
        self.assertTrue(
            antwort.headers["content-type"].startswith("text/event-stream"))
        roh = await _body(antwort)

        self.assertEqual(roh, fake.skript_bytes())      # byte-identisch
        self.assertTrue(roh.endswith(b"data: [DONE]\n\n"))
        self.assertNotIn(PING, roh)                     # schnell: kein Leerlauf
        job = self.registry.get("relay-1")
        self.assertIs(job.status, JobStatus.DONE)
        self.assertEqual(job.prompt_tokens, 7)          # usage schlaegt Zaehlung
        self.assertEqual(job.completion_tokens, 4)
        self.assertIn("Hallo Welt", job.content_ring.text())

    async def test_keepalive_waehrend_queue(self):
        # 0,4 s nichts vom Upstream, Heartbeat 0,1 s: der Client wartet nicht.
        schritte = [(0.4, fakes.chat_chunk({"content": "Hallo"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(schritte), heartbeat_s=0.1)

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "ka-1", "stream": True}),
            deps)
        roh = await _body(antwort)

        self.assertGreaterEqual(roh.count(PING), 2)     # mindestens zwei Pings
        # Alle Pings stehen vor den Original-Bytes, die roh bleiben.
        self.assertEqual(roh, PING * roh.count(PING) + fakes.skript_bytes(schritte))
        self.assertIs(self.registry.get("ka-1").status, JobStatus.DONE)

    async def test_upstream_400_wird_durchgereicht(self):
        fake = fakes.FakeSglang()
        fake.fehler = (400, b'{"error": {"message": "kaputte Anfrage"}}')
        deps = self.deps(httpx.ASGITransport(app=fake.app))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "bad-1", "stream": True}),
            deps)

        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.headers["content-type"], "application/json")
        self.assertEqual(await _body(antwort),
                         b'{"error": {"message": "kaputte Anfrage"}}')
        self.assertIsNone(self.registry.get("bad-1"))   # kein SSE, kein Job

    async def test_client_abbruch_job_laeuft_zu_ende(self):
        schritte = [(0.0, fakes.chat_chunk({"content": "A"})),
                    (0.3, fakes.chat_chunk({"content": "B"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(schritte))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "abbruch-1", "stream": True}),
            deps)
        self.assertIsInstance(antwort, StreamingResponse)
        erst = await anext(antwort.body_iterator)
        self.assertIn(b'"A"', erst)
        self.assertIs(self.registry.get("abbruch-1").status, JobStatus.RUNNING)

        await antwort.body_iterator.aclose()            # Browser weg: Abbruch

        fertig = await _warte_bis(
            lambda: self.registry.get("abbruch-1").status is not JobStatus.RUNNING)
        self.assertTrue(fertig, "Job blieb haengen (Zombie)")
        job = self.registry.get("abbruch-1")
        self.assertIs(job.status, JobStatus.DONE)
        # Der Upstream wurde trotz Abbruch zu Ende gelesen (Resultat verworfen).
        self.assertIn("B", job.content_ring.text())

    async def test_upstream_ab_mitte_wird_fehler_ohne_zombie(self):
        # SGLang/Verbindung bricht mitten im Strom ab: bereits Geflossenes
        # kommt roh an, der Job endet als Fehler (kein Zombie-running).
        schritte = [(0.0, fakes.chat_chunk({"content": "halb"})),
                    (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(
            schritte, fehler=httpx.ReadError("Verbindung weg")))

        with self.assertLogs("aiserver_monitor.proxy", level="WARNING") as cm:
            antwort = await handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "ab-1", "stream": True}),
                deps)
            roh = await _body(antwort)

        self.assertTrue(any("Upstream-Abbruch" in zeile for zeile in cm.output))
        self.assertEqual(roh, fakes.skript_bytes(schritte))
        job = self.registry.get("ab-1")
        self.assertIs(job.status, JobStatus.ERROR)
        self.assertIn("ReadError", job.error)
        self.assertEqual(job.content_ring.text(), "halb")

    async def test_abgeschnittener_strom_ohne_finish_reason_ist_fehler(self):
        # Sauberes Streamende ohne finish_reason (Strom abgeschnitten) gilt
        # nicht als fertig, sondern als Fehler.
        schritte = [(0.0, fakes.chat_chunk({"content": "halb"})),
                    (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(schritte))

        with self.assertLogs("aiserver_monitor.proxy", level="WARNING") as cm:
            antwort = await handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "kurz-1", "stream": True}),
                deps)
            await _body(antwort)

        self.assertTrue(any("finish_reason" in zeile for zeile in cm.output))
        job = self.registry.get("kurz-1")
        self.assertIs(job.status, JobStatus.ERROR)
        self.assertEqual(job.error, "Stream endete ohne finish_reason")

    async def test_client_rid_wird_durchgereicht(self):
        fake = fakes.FakeSglang(_chat_skript(["Hallo"]))
        deps = self.deps(httpx.ASGITransport(app=fake.app))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "abc", "stream": True}),
            deps)
        await _body(antwort)

        self.assertEqual(fake.anfragen, [{"rid": "abc", "stream": True}])
        self.assertIsNotNone(self.registry.get("abc"))

    async def test_neue_rid_wenn_keine_mitkommt(self):
        fake = fakes.FakeSglang(_chat_skript(["Hallo"]))
        deps = self.deps(httpx.ASGITransport(app=fake.app))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions", {"stream": True}),
            deps)
        await _body(antwort)

        rid = fake.anfragen[0]["rid"]
        self.assertRegex(rid, RID_MUSTER)
        self.assertIsNotNone(self.registry.get(rid))

    async def test_mehrbyte_utf8_ueber_chunkgrenzen(self):
        # Schnitt mitten im Mehrbyte-Zeichen: roh durchgereicht, Vorschau heil.
        roh = fakes.chat_chunk({"content": "gru\u00df"})
        grenze = roh.index("\u00df".encode("utf-8")) + 1
        schritte = [(0.0, roh[:grenze]), (0.0, roh[grenze:]),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(schritte))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "utf8-1", "stream": True}),
            deps)

        self.assertEqual(await _body(antwort), fakes.skript_bytes(schritte))
        job = self.registry.get("utf8-1")
        self.assertEqual(job.content_ring.text(), "gru\u00df")
        self.assertEqual(job_to_dict(job)["completion_tokens"], 1)


class TestPassthroughOhneStream(ProxyTestBasis):
    async def test_get_ist_passthrough_ohne_job(self):
        fake = fakes.FakeSglang()
        deps = self.deps(httpx.ASGITransport(app=fake.app))

        antwort = await handle_v1(
            _request("GET", "/v1/models", query="x=1"), deps)

        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.headers["content-type"], "application/json")
        self.assertEqual(json.loads(antwort.body)["data"][0]["id"], fakes.MODELL)
        self.assertEqual(fake.pfade, ["/v1/models?x=1"])
        self.assertEqual(self.registry.snapshot(), [])

    async def test_stream_false_ist_gepufferter_passthrough_ohne_job(self):
        # Task 8 baut hier Jobs und WS-Keepalives; jetzt bleibt es getreu.
        fake = fakes.FakeSglang()
        deps = self.deps(httpx.ASGITransport(app=fake.app))
        body = {"model": "fake-modell", "rid": "ns-1", "stream": False,
                "messages": [{"role": "user", "content": "Hi"}]}

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions", body), deps)

        self.assertEqual(antwort.status_code, 200)
        self.assertTrue(
            antwort.headers["content-type"].startswith("application/json"))
        daten = json.loads(antwort.body)
        self.assertEqual(daten["choices"][0]["message"]["content"],
                         "Fake-Antwort")
        self.assertEqual(fake.anfragen, [body])         # unveraendert, ohne rid
        self.assertEqual(self.registry.snapshot(), [])


class TestJobTracking(ProxyTestBasis):
    async def test_prompt_vorschau_und_fallback_zaehlung(self):
        schritte = [(0.0, fakes.chat_chunk({"reasoning_content": "Erst denken"})),
                    (0.0, fakes.chat_chunk({"content": "dann antworten"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(schritte))
        lang = "x" * 150
        body = {"rid": "track-1", "stream": True,
                "messages": [{"role": "system", "content": "Sei knapp"},
                             {"role": "user", "content": lang}]}

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions", body), deps)
        await _body(antwort)

        job = self.registry.get("track-1")
        erwartet = f"system: Sei knapp\nuser: {lang}"
        self.assertEqual(job.full_prompt, erwartet)             # nur im RAM
        self.assertEqual(job.prompt_start, erwartet[:100])      # gekuerzt
        self.assertEqual(job.reasoning_ring.text(), "Erst denken")
        self.assertEqual(job.content_ring.text(), "dann antworten")
        daten = job_to_dict(job)
        self.assertEqual(daten["completion_tokens"], 2)         # ohne usage: Zaehlung
        self.assertIsNone(daten["prompt_tokens"])               # nicht gemessen
        self.assertEqual(job.source, "10.0.0.5")                # Peer-IP (Task 10: CF)

    async def test_quelle_ohne_client_ip_ist_unbekannt(self):
        fake = fakes.FakeSglang(_chat_skript(["Hallo"]))
        deps = self.deps(httpx.ASGITransport(app=fake.app))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "ohne-ip", "stream": True}, client_ip=None),
            deps)
        await _body(antwort)

        self.assertEqual(self.registry.get("ohne-ip").source, "unbekannt")

    async def test_completions_prompt_ist_der_full_prompt(self):
        schritte = [(0.0, fakes.chat_chunk({"content": "ok"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(schritte))

        antwort = await handle_v1(
            _request("POST", "/v1/completions",
                     {"rid": "comp-1", "stream": True, "prompt": "Ein Prompt"}),
            deps)
        await _body(antwort)

        job = self.registry.get("comp-1")
        self.assertEqual(job.full_prompt, "Ein Prompt")
        self.assertEqual(job.prompt_start, "Ein Prompt")

    async def test_ereignisse_gedrosselt_und_abschluss_immer(self):
        # Schneller Schwall: die Drossel laesst nur wenige Ereignisse durch,
        # der Abschluss kommt aber immer (auch wenn er in die Drossel faellt).
        schritte = [(0.0, fakes.chat_chunk({"content": f"t{n}"}))
                    for n in range(40)]
        schritte += [(0.0, fakes.chat_chunk({}, finish_reason="stop")),
                     (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(schritte))
        queue = self.hub.subscribe()

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "hub-1", "stream": True}),
            deps)
        await _body(antwort)

        rahmen = []
        while not queue.empty():
            rahmen.append(queue.get_nowait())
        self.assertGreaterEqual(len(rahmen), 1)
        self.assertLessEqual(len(rahmen), 4)            # ~4/s, kein Schwall
        daten = [json.loads(rahmen_eintrag.split("data: ", 1)[1])
                 for rahmen_eintrag in rahmen]
        self.assertEqual(daten[0]["status"], "running")
        self.assertEqual(daten[-1]["status"], "done")
        self.assertEqual(daten[-1]["rid"], "hub-1")


class _ZaehlendeHistory:
    """Delegiert an eine echte History und zaehlt die record_job-Aufrufe."""

    def __init__(self, inner: History):
        self._inner = inner
        self.aufrufe = 0

    def record_job(self, job_dict: dict) -> None:
        self.aufrufe += 1
        self._inner.record_job(job_dict)


class TestHistoryAnbindung(ProxyTestBasis):
    async def test_history_wird_genau_einmal_beim_finish_geschrieben(self):
        schritte = _chat_skript(
            ["Hallo"],
            usage={"prompt_tokens": 7, "completion_tokens": 4,
                   "total_tokens": 11})
        with tempfile.TemporaryDirectory() as tmp:
            hist = History(Path(tmp) / "monitor.db")
            hist.init()
            self.addCleanup(hist.close)
            zaehler = _ZaehlendeHistory(hist)
            deps = self.deps(
                httpx.ASGITransport(app=fakes.FakeSglang(schritte).app),
                history=zaehler)

            antwort = await handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "hist-1", "stream": True}),
                deps)
            await _body(antwort)

            self.assertEqual(zaehler.aufrufe, 1)
            fenster = hist.query_stats(0.0, time.time() + 60.0, 60)
            self.assertEqual(fenster["overview"]["count"], 1)
            self.assertEqual(fenster["overview"]["done"], 1)
            self.assertEqual(fenster["overview"]["tokens_in"], 7)
            self.assertEqual(fenster["overview"]["tokens_out"], 4)


if __name__ == "__main__":
    unittest.main()
