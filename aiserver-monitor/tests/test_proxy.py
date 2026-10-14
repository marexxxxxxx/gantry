"""Tests fuer den /v1-Passthrough (proxy.py).

Die Tests rufen ``handle_v1`` direkt an (Task 10 haengt die Funktion in die
App) und lesen die Antwort so, wie der Client sie bekommt: die Bytes des
Streaming-Generators beziehungsweise der gepufferte Body.

Der Upstream ist der Fake-SGLang aus ``fakes.py``; die meisten Faelle laufen
ueber ``httpx.ASGITransport`` (keine Sockets). Wo echte Zeitabstaende
geprueft werden (Keepalive, Client-Abbruch), puffert der ASGITransport die
Antwort und taugt nicht; dort liefern ``skript_transport`` (SSE) und
``json_transport`` (Nicht-Stream) dieselben Bytes zeitgetreu.
"""

import asyncio
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
             roh: bytes | None = None, query: str = "",
             client_ip: str | None = "10.0.0.5",
             kanal: "_ClientKanal | None" = None) -> Request:
    """Baut eine Starlette-Anfrage ohne ASGI-Lauf.

    ``body`` wird zu JSON kodiert; ``roh`` ersetzt das durch exakte Bytes
    (z. B. fuer einen Nicht-JSON-Body). ``kanal`` ist die steuerbare
    receive-Queue der Disconnect-Tests; ohne ihn bleibt die Verbindung nach
    dem Body offen (kein Disconnect).
    """
    if roh is None:
        roh = b"" if body is None else json.dumps(body).encode("utf-8")
    if kanal is None:
        kanal = _ClientKanal()
    kanal.fuellen(roh)
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
    return Request(scope, kanal.receive)


class _ClientKanal:
    """Steuerbare receive-Queue einer Anfrage (Disconnect-Tests).

    ``fuellen`` legt den Request-Body als erste Nachricht ab; ``abmelden``
    speist ``http.disconnect`` ein (Client weg). Danach blockiert die Queue
    wieder wie eine offene Verbindung.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue()

    def fuellen(self, roh: bytes) -> None:
        """Legt den Body als erste receive-Nachricht ab."""
        self._queue.put_nowait(
            {"type": "http.request", "body": roh, "more_body": False})

    def abmelden(self) -> None:
        """Der Client schliesst: ``http.disconnect`` einspielen."""
        self._queue.put_nowait({"type": "http.disconnect"})

    async def receive(self) -> dict:
        return await self._queue.get()


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


def _rahmen_lesen(queue: asyncio.Queue) -> list[dict]:
    """Alle bereitliegenden Hub-Frames als Job-Dicts (Queue wird geleert)."""
    daten = []
    while not queue.empty():
        rahmen = queue.get_nowait()
        daten.append(json.loads(rahmen.split("data: ", 1)[1]))
    return daten


class ProxyTestBasis(unittest.IsolatedAsyncioTestCase):
    """Gemeinsamer Aufbau: Registry, Hub und ein httpx-Client je Test."""

    async def asyncSetUp(self):
        self.registry = JobRegistry(preview_max_chars=4096)
        self.hub = EventHub()
        self._client: httpx.AsyncClient | None = None

    def deps(self, transport, *, history=None, heartbeat_s: float = 15.0,
             connect_timeout_s: float = 5.0,
             nonstream_keepalive: bool = True,
             abort_on_disconnect: bool = True) -> Deps:
        """Deps mit dem gegebenen Transport; der Client wird am Testende geschlossen."""
        if self._client is None:
            self._client = httpx.AsyncClient(transport=transport)
            self.addAsyncCleanup(self._client.aclose)
        return Deps(
            config=Config(heartbeat_s=heartbeat_s,
                          connect_timeout_s=connect_timeout_s,
                          nonstream_keepalive=nonstream_keepalive,
                          abort_on_disconnect=abort_on_disconnect),
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
    async def test_job_wird_sofort_als_waiting_publiziert(self):
        # Register-Publizierung wie im Nicht-Stream-Pfad: wartende Stream-Jobs
        # stehen sofort im Hub, nicht erst mit dem ersten Token.
        schritte = [(0.3, fakes.chat_chunk({"content": "spaet"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(schritte), heartbeat_s=0.05)
        queue = self.hub.subscribe()

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "warten-1", "stream": True}),
            deps)
        ereignisse = _rahmen_lesen(queue)
        self.assertEqual(ereignisse[0]["status"], "waiting")
        self.assertEqual(ereignisse[0]["rid"], "warten-1")

        await _body(antwort)
        self.assertIs(self.registry.get("warten-1").status, JobStatus.DONE)

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
        # Altverhalten (abort_on_disconnect=false): der Client-Abbruch stoert
        # nicht, der Job laeuft zu Ende (done); Ruling R4.
        schritte = [(0.0, fakes.chat_chunk({"content": "A"})),
                    (0.3, fakes.chat_chunk({"content": "B"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        deps = self.deps(fakes.skript_transport(schritte),
                         abort_on_disconnect=False)

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


class TestPfadschutz(ProxyTestBasis):
    """Punkt-Segmente im Pfad enden mit 404, bevor der Upstream kontaktiert wird.

    httpx normalisiert ``..`` beim Bau der Upstream-URL; ohne die Sperre
    waere ``/v1/../get_server_info`` beim Upstream ``/get_server_info`` und
    damit eine ungeschuetzte SGLang-Admin-Route.
    """

    async def test_punktsegmente_ergeben_404_ohne_upstream_kontakt(self):
        pfade: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            pfade.append(str(request.url))
            return httpx.Response(200, json={})

        deps = self.deps(httpx.MockTransport(handler))
        faelle = (
            ("POST", "/v1/../get_server_info", {"stream": True}),
            ("GET", "/v1/../get_server_info", None),
            ("POST", "/v1/a/../b", {"stream": False}),
        )
        for methode, pfad, body in faelle:
            with self.subTest(methode=methode, pfad=pfad):
                antwort = await handle_v1(_request(methode, pfad, body), deps)
                self.assertEqual(antwort.status_code, 404)

        self.assertEqual(pfade, [])                     # kein Upstream-Kontakt
        self.assertEqual(self.registry.snapshot(), [])


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

    async def test_post_ohne_json_ist_passthrough_ohne_job(self):
        # Ruling 9: POSTs ohne JSON-Objekt bleiben der getreue, gepufferte
        # Passthrough ohne Job (Task 8 aendert nur den Nicht-Stream-Pfad).
        fest = b'{"ok": true}'
        deps = self.deps(fakes.json_transport(fest, content_type="text/plain"))

        antwort = await handle_v1(
            _request("POST", "/v1/completions", roh=b"kein json"), deps)

        self.assertNotIsInstance(antwort, StreamingResponse)
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(await _body(antwort), fest)
        self.assertEqual(self.registry.snapshot(), [])

    async def test_anderer_post_pfad_bleibt_passthrough_ohne_job(self):
        # R1/R7.5: nur ``chat/completions`` wird intern gestreamt; andere
        # JSON-POSTs (z. B. ``/v1/embeddings``) bleiben der getreue,
        # gepufferte Byte-Passthrough ohne Job und ohne stream-Umschreibung.
        fest = b'{"data": [{"embedding": [0.1], "index": 0}]}'
        gesehen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            gesehen.append(request)
            return httpx.Response(200, content=fest, headers={
                "content-type": "application/json"})

        deps = self.deps(httpx.MockTransport(handler))
        body = {"model": "fake-modell", "input": "Hallo", "stream": False}

        antwort = await handle_v1(
            _request("POST", "/v1/embeddings", body), deps)

        self.assertNotIsInstance(antwort, StreamingResponse)
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(await _body(antwort), fest)     # byte-identisch
        self.assertEqual(json.loads(gesehen[0].content), body)  # unveraendert
        self.assertEqual(self.registry.snapshot(), [])  # kein Job


class TestNichtstreamPfad(ProxyTestBasis):
    """POST ``chat/completions`` mit ``stream != true``: interner Stream.

    Der Upstream-Aufruf laeuft mit ``stream=true`` (plus
    ``stream_options.include_usage``); die SSE-Antwort wird mit demselben
    Parser/Tracker wie das Relay live verfolgt und danach zu einem
    ChatCompletion-JSON reassembliert (bewusst ohne SGLang-``meta_info``).
    Schnelle Antworten gehen als einzelne Response ohne Leerzeichen raus,
    langsame mit WS-Keepalives; die Fehlerpfade (502, spaeter
    Transportfehler) bleiben wie im alten Nicht-Stream-Pfad.
    """

    async def test_chat_nonstream_wird_intern_gestreamt_und_gemessen(self):
        # R7.1: der Fake-Upstream bekommt stream=true; waehrend des Laufs ist
        # der Job running (TTFT, wachsende Vorschau, live zaehlende Tokens),
        # danach kommt das reassemblierte ChatCompletion-JSON.
        schritte = [
            (0.0, fakes.chat_chunk({"role": "assistant", "content": ""})),
            (0.0, fakes.chat_chunk({"content": "Hal"})),
            (0.3, fakes.chat_chunk({"content": "lo"})),
            (0.0, fakes.chat_chunk({}, finish_reason="stop")),
            (0.0, fakes.chat_chunk({}, usage={"prompt_tokens": 5,
                                              "completion_tokens": 2,
                                              "total_tokens": 7})),
            (0.0, fakes.DONE),
        ]
        recorder = _AbbruchRecorder(schritte)
        deps = self.deps(httpx.MockTransport(recorder), heartbeat_s=0.05)
        queue = self.hub.subscribe()
        body = {"model": "fake-modell", "rid": "live-1", "stream": False,
                "messages": [{"role": "user", "content": "Hi"}]}

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions", body), deps)
        self.assertIsInstance(antwort, StreamingResponse)
        erst = await anext(antwort.body_iterator)       # Leerzeichen (langsam)
        self.assertEqual(erst, b" ")

        laeuft = await _warte_bis(
            lambda: self.registry.get("live-1").status is JobStatus.RUNNING)
        self.assertTrue(laeuft, "Job wurde nicht als running sichtbar")
        daten = job_to_dict(self.registry.get("live-1"))
        self.assertIsNotNone(daten["ttft_ms"])
        self.assertEqual(daten["content_preview"], "Hal")
        self.assertEqual(daten["completion_tokens"], 1)  # Fallback waechst live
        self.assertIsNone(daten["prompt_tokens"])        # usage noch nicht da

        daten = json.loads(erst + await _body(antwort))
        self.assertEqual(daten["id"], "live-1")
        self.assertEqual(daten["object"], "chat.completion")
        self.assertEqual(daten["model"], "fake-modell")
        self.assertIsInstance(daten["created"], int)
        choice = daten["choices"][0]
        self.assertEqual(choice["index"], 0)
        self.assertEqual(choice["message"]["role"], "assistant")
        self.assertEqual(choice["message"]["content"], "Hallo")
        self.assertEqual(choice["message"]["reasoning_content"], "")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(daten["usage"], {"prompt_tokens": 5,
                                          "completion_tokens": 2,
                                          "total_tokens": 7})
        self.assertNotIn("meta_info", daten)             # bewusste Abweichung

        job = self.registry.get("live-1")                # usage schlaegt Zaehlung
        self.assertIs(job.status, JobStatus.DONE)
        self.assertEqual(job.prompt_tokens, 5)
        self.assertEqual(job.completion_tokens, 2)

        anfrage = json.loads(recorder.anfragen[0].content)
        self.assertIs(anfrage["stream"], True)
        self.assertEqual(anfrage["stream_options"], {"include_usage": True})
        self.assertEqual(anfrage["rid"], "live-1")
        self.assertEqual(anfrage["model"], "fake-modell")
        self.assertEqual(anfrage["messages"], body["messages"])

        status = [rahmen["status"] for rahmen in _rahmen_lesen(queue)]
        self.assertEqual(status[0], "waiting")
        self.assertEqual(status[-1], "done")

    async def test_schnelle_chat_antwort_ist_einzelnes_json(self):
        # R7.1: alles innerhalb eines Heartbeats -> einzelne Response ohne
        # Leerzeichen, mit content, reasoning_content, finish_reason, usage.
        schritte = [(0.0, fakes.chat_chunk({"role": "assistant",
                                            "content": ""})),
                    (0.0, fakes.chat_chunk({"reasoning_content": "Erst denken"})),
                    (0.0, fakes.chat_chunk({"content": "Hallo"})),
                    (0.0, fakes.chat_chunk({"content": " Welt"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.chat_chunk({}, usage={"prompt_tokens": 7,
                                                      "completion_tokens": 4,
                                                      "total_tokens": 11})),
                    (0.0, fakes.DONE)]
        fake = fakes.FakeSglang(schritte)
        deps = self.deps(httpx.ASGITransport(app=fake.app))
        body = {"model": "fake-modell", "rid": "fast-1", "stream": False,
                "messages": [{"role": "user", "content": "Hi"}]}

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions", body), deps)

        self.assertNotIsInstance(antwort, StreamingResponse)
        self.assertEqual(antwort.status_code, 200)
        self.assertTrue(
            antwort.headers["content-type"].startswith("application/json"))
        self.assertFalse(antwort.body.startswith(b" "))  # schnell: kein WS
        daten = json.loads(antwort.body)
        choice = daten["choices"][0]
        self.assertEqual(choice["message"]["content"], "Hallo Welt")
        self.assertEqual(choice["message"]["reasoning_content"], "Erst denken")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(daten["usage"], {"prompt_tokens": 7,
                                          "completion_tokens": 4,
                                          "total_tokens": 11})
        job = self.registry.get("fast-1")
        self.assertIs(job.status, JobStatus.DONE)
        self.assertEqual(job.content_ring.text(), "Hallo Welt")
        self.assertEqual(job.reasoning_ring.text(), "Erst denken")
        self.assertIs(fake.anfragen[0]["stream"], True)
        self.assertEqual(fake.anfragen[0]["stream_options"],
                         {"include_usage": True})
        self.assertEqual(fake.anfragen[0]["messages"], body["messages"])

    async def test_chat_ohne_stream_feld_wird_konvertiert(self):
        # ``stream is not True`` schliesst das fehlende Feld ein: auch ohne
        # stream-Angabe (Default false bei OpenAI) laeuft der interne Stream.
        fake = fakes.FakeSglang(_chat_skript(["ok"]))
        deps = self.deps(httpx.ASGITransport(app=fake.app))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "ohne-feld", "model": "fake-modell",
                      "messages": [{"role": "user", "content": "Hi"}]}),
            deps)

        self.assertIs(fake.anfragen[0]["stream"], True)
        self.assertEqual(
            json.loads(antwort.body)["choices"][0]["message"]["content"], "ok")

    async def test_ohne_usage_fehlt_das_usage_feld(self):
        # R4: keine usage im Stream -> Feld fehlt ganz (nie 0 erfinden); der
        # Job behaelt die Fallback-Zaehlung als completion_tokens.
        fake = fakes.FakeSglang(_chat_skript(["Nur Text"]))
        deps = self.deps(httpx.ASGITransport(app=fake.app))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "nousage-1", "stream": False}), deps)

        daten = json.loads(antwort.body)
        self.assertNotIn("usage", daten)
        self.assertEqual(daten["choices"][0]["message"]["content"], "Nur Text")
        job = self.registry.get("nousage-1")
        self.assertIs(job.status, JobStatus.DONE)
        self.assertIsNone(job.prompt_tokens)
        self.assertIsNone(job.completion_tokens)
        self.assertEqual(job_to_dict(job)["completion_tokens"], 1)

    async def test_langsamer_upstream_mit_ws_keepalive(self):
        # R7.2/T8: der Upstream streamt langsam (Pause > Heartbeat): der
        # Client bekommt Leerzeichen, der Gesamtbody bleibt gueltiges JSON.
        schritte = [(0.0, fakes.chat_chunk({"role": "assistant",
                                            "content": ""})),
                    (0.25, fakes.chat_chunk({"content": "spaet"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.chat_chunk({}, usage={"prompt_tokens": 3,
                                                      "completion_tokens": 1,
                                                      "total_tokens": 4})),
                    (0.0, fakes.DONE)]
        recorder = _AbbruchRecorder(schritte)
        deps = self.deps(httpx.MockTransport(recorder), heartbeat_s=0.1)
        queue = self.hub.subscribe()

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "ns-langsam", "stream": False,
                      "messages": [{"role": "user", "content": "Hi"}]}),
            deps)

        self.assertIsInstance(antwort, StreamingResponse)
        self.assertTrue(
            antwort.headers["content-type"].startswith("application/json"))
        roh = await _body(antwort)

        self.assertTrue(roh.startswith(b" "))           # WS vor dem JSON
        daten = json.loads(roh)                         # Whitespace ist erlaubt
        self.assertEqual(daten["choices"][0]["message"]["content"], "spaet")
        job = self.registry.get("ns-langsam")
        self.assertIs(job.status, JobStatus.DONE)
        self.assertEqual(job.prompt_tokens, 3)
        self.assertEqual(job.completion_tokens, 1)

        status = [rahmen["status"] for rahmen in _rahmen_lesen(queue)]
        self.assertEqual(status[0], "waiting")
        self.assertEqual(status[-1], "done")

    async def test_ohne_keepalive_keine_leerzeichen(self):
        # Config-Schalter aus: auch eine langsame Antwort kommt ohne WS.
        schritte = [(0.0, fakes.chat_chunk({"role": "assistant",
                                            "content": ""})),
                    (0.15, fakes.chat_chunk({"content": "ohne WS"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        recorder = _AbbruchRecorder(schritte)
        deps = self.deps(httpx.MockTransport(recorder), heartbeat_s=0.05,
                         nonstream_keepalive=False)

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "ns-ohne-ws", "stream": False}),
            deps)
        roh = await _body(antwort)

        self.assertFalse(roh.startswith(b" "))          # kein Leerzeichen
        self.assertEqual(
            json.loads(roh)["choices"][0]["message"]["content"], "ohne WS")
        self.assertIs(self.registry.get("ns-ohne-ws").status, JobStatus.DONE)

    async def test_nonstream_upstream_400_durchgereicht(self):
        # Upstream lehnt ab (vor jedem SSE-Delta): Status und Body
        # unveraendert, Job error mit Statustext (Ruling R2).
        fake = fakes.FakeSglang()
        fake.fehler = (400, b'{"error": {"message": "kaputte Anfrage"}}')
        deps = self.deps(httpx.ASGITransport(app=fake.app))

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "ns-bad", "stream": False}),
            deps)

        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.headers["content-type"], "application/json")
        self.assertEqual(await _body(antwort),
                         b'{"error": {"message": "kaputte Anfrage"}}')
        job = self.registry.get("ns-bad")
        self.assertIs(job.status, JobStatus.ERROR)
        self.assertEqual(job.error, "upstream 400")
        self.assertIs(fake.anfragen[0]["stream"], True)  # intern gestreamt

    async def test_upstream_down_502(self):
        # SGLang aus: 502 mit festem Fehlerkoerper, Job error, publiziert.
        deps = self.deps(fakes.json_transport(
            fehler=httpx.ConnectError("Verbindung abgelehnt")))
        queue = self.hub.subscribe()

        with self.assertLogs("aiserver_monitor.proxy", level="WARNING") as cm:
            antwort = await handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "down-1", "stream": False,
                          "messages": [{"role": "user", "content": "Hi"}]}),
                deps)

        self.assertEqual(antwort.status_code, 502)
        self.assertEqual(antwort.headers["content-type"], "application/json")
        self.assertEqual(
            antwort.body,
            b'{"error": {"message": "sglang nicht erreichbar", '
            b'"type": "upstream_unavailable"}}')
        self.assertTrue(any("down-1" in zeile for zeile in cm.output))

        job = self.registry.get("down-1")               # Job trotz Ausfall
        self.assertIs(job.status, JobStatus.ERROR)
        self.assertEqual(job.error, "ConnectError: Verbindung abgelehnt")
        ereignisse = _rahmen_lesen(queue)
        self.assertEqual(ereignisse[0]["status"], "waiting")
        self.assertEqual(ereignisse[-1]["status"], "error")
        self.assertEqual(ereignisse[-1]["error"], job.error)

    async def test_nonstream_spaeter_transportfehler_liefert_fehlerkoerper(self):
        # Transportfehler erst nach dem Heartbeat (Antwortstart ist schon
        # draussen): nach den Keepalive-Leerzeichen kommt der 502-Koerper,
        # der Gesamtbody bleibt parsebares JSON; der Job endet als error.
        schritte = [(0.0, fakes.chat_chunk({"role": "assistant",
                                            "content": "halb"})),
                    (0.25, b"")]
        deps = self.deps(fakes.skript_transport(
            schritte, fehler=httpx.ReadError("spaet weg")), heartbeat_s=0.05)

        with self.assertLogs("aiserver_monitor.proxy", level="WARNING") as cm:
            antwort = await handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "ns-spaet", "stream": False}),
                deps)
            self.assertIsInstance(antwort, StreamingResponse)
            roh = await _body(antwort)

        self.assertTrue(roh.startswith(b" "))           # WS vor dem JSON
        self.assertEqual(json.loads(roh)["error"], {
            "message": "sglang nicht erreichbar",
            "type": "upstream_unavailable"})
        self.assertTrue(any("ns-spaet" in zeile for zeile in cm.output))
        job = self.registry.get("ns-spaet")
        self.assertIs(job.status, JobStatus.ERROR)
        self.assertIn("ReadError", job.error)

    async def test_nonstream_client_abbruch_job_laeuft_zu_ende(self):
        # Client bricht ab, waehrend der Upstream noch rechnet: die Lesetask
        # schliesst den Job trotzdem ab (kein running-Zombie); der Abbruch
        # greift nur mit abort_on_disconnect (hier aus).
        schritte = [(0.0, fakes.chat_chunk({"role": "assistant",
                                            "content": ""})),
                    (0.0, fakes.chat_chunk({"content": "A"})),
                    (0.3, fakes.chat_chunk({"content": "B"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        recorder = _AbbruchRecorder(schritte)
        deps = self.deps(httpx.MockTransport(recorder), heartbeat_s=0.05,
                         abort_on_disconnect=False)

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "ns-abbruch", "stream": False}),
            deps)
        self.assertIsInstance(antwort, StreamingResponse)
        self.assertEqual(await anext(antwort.body_iterator), b" ")
        self.assertIs(self.registry.get("ns-abbruch").status, JobStatus.RUNNING)

        await antwort.body_iterator.aclose()            # Browser weg: Abbruch

        fertig = await _warte_bis(
            lambda: self.registry.get("ns-abbruch").status is not JobStatus.RUNNING)
        self.assertTrue(fertig, "Job blieb haengen (Zombie)")
        job = self.registry.get("ns-abbruch")
        self.assertIs(job.status, JobStatus.DONE)
        # Die Lesetask hat den Rest trotz Abbruch gemessen (Resultat verworfen).
        self.assertEqual(job.content_ring.text(), "AB")
        self.assertIsNone(job.prompt_tokens)            # ohne usage: null, nie 0
        self.assertIsNone(job.completion_tokens)
        self.assertEqual(job_to_dict(job)["completion_tokens"], 2)  # Zaehlung


class TestAbbruchSignal(ProxyTestBasis):
    """R5: die Cancel-Route setzt das Signal; der Pfad beendet den Lauf.

    Der Route-Teil (Signal setzen) ist in ``test_app`` abgedeckt; hier wartet
    der Non-Stream-Pfad auf das Signal und beendet den Upstream-Task bzw. den
    laufenden Read-Stream wirklich.
    """

    async def test_abbruch_im_pool_wait_beendet_den_task(self):
        # R7.4: der Upstream antwortet nie; das Signal beendet den wartenden
        # lokalen Task wirklich (kein Zombie), Job aborted, Client 499.
        upstream = _WartenderUpstream()
        deps = self.deps(httpx.MockTransport(upstream), heartbeat_s=15.0)
        queue = self.hub.subscribe()
        aufgabe = asyncio.create_task(handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "pool-1", "stream": False,
                      "messages": [{"role": "user", "content": "Hi"}]}),
            deps))
        await asyncio.wait_for(upstream.eingetreten.wait(), 2.0)

        signal = self.registry.abort_signal("pool-1")
        self.assertIsNotNone(signal)
        signal.set()

        antwort = await asyncio.wait_for(aufgabe, 2.0)
        self.assertNotIsInstance(antwort, StreamingResponse)
        self.assertEqual(antwort.status_code, 499)
        self.assertEqual(antwort.headers["content-type"], "application/json")
        self.assertEqual(json.loads(antwort.body),
                         {"error": {"message": "Job wurde abgebrochen",
                                    "type": "aborted"}})
        self.assertTrue(
            await _warte_bis(lambda: upstream.abgebrochen.is_set()),
            "der wartende Upstream-Aufruf lief weiter")
        await _warte_bis(lambda: upstream.abbrueche)
        self.assertEqual(upstream.abbrueche,
                         [{"rid": "pool-1", "abort_all": False}])
        job = self.registry.get("pool-1")
        self.assertIs(job.status, JobStatus.ABORTED)
        self.assertIsNone(job.error)
        self.assertEqual([rahmen["status"] for rahmen in _rahmen_lesen(queue)],
                         ["waiting", "aborted"])

    async def test_abbruch_beendet_den_laufenden_read_stream(self):
        # R7.3: Abbruch waehrend des Laufs (Antwortstart noch nicht): 499 mit
        # aborted-JSON, abort_request raus, Job ABORTED, Read-Stream wirklich
        # geschlossen.
        geschlossen = asyncio.Event()
        recorder = _AbbruchRecorder(
            [(0.0, fakes.chat_chunk({"role": "assistant", "content": ""})),
             (0.0, fakes.chat_chunk({"content": "A"})),
             (5.0, fakes.chat_chunk({"content": "B"})),
             (0.0, fakes.chat_chunk({}, finish_reason="stop")),
             (0.0, fakes.DONE)],
            geschlossen=geschlossen)
        deps = self.deps(httpx.MockTransport(recorder), heartbeat_s=15.0)
        queue = self.hub.subscribe()
        aufgabe = asyncio.create_task(handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "abb-live", "stream": False,
                      "messages": [{"role": "user", "content": "Hi"}]}),
            deps))

        def laeuft_jetzt() -> bool:
            job = self.registry.get("abb-live")
            return job is not None and job.status is JobStatus.RUNNING

        self.assertTrue(await _warte_bis(laeuft_jetzt),
                        "Job wurde nicht als running sichtbar")

        self.registry.abort_signal("abb-live").set()
        antwort = await asyncio.wait_for(aufgabe, 2.0)

        self.assertEqual(antwort.status_code, 499)
        self.assertEqual(json.loads(antwort.body),
                         {"error": {"message": "Job wurde abgebrochen",
                                    "type": "aborted"}})
        self.assertEqual(recorder.abbrueche,
                         [{"rid": "abb-live", "abort_all": False}])
        self.assertTrue(geschlossen.is_set(),
                        "der Read-Stream wurde nicht beendet")
        job = self.registry.get("abb-live")
        self.assertIs(job.status, JobStatus.ABORTED)
        self.assertIsNone(job.error)
        status = [rahmen["status"] for rahmen in _rahmen_lesen(queue)]
        self.assertEqual(status[0], "waiting")
        self.assertEqual(status[-1], "aborted")
        self.assertNotIn("done", status)

    async def test_abbruch_nach_antwortstart_liefert_abgebrochenes_json(self):
        # R5: laeuft der Antwortbody schon (Heartbeat vorbei), folgt dasselbe
        # aborted-JSON hinter den Leerzeichen; der Gesamtbody bleibt JSON.
        geschlossen = asyncio.Event()
        recorder = _AbbruchRecorder(
            [(0.0, fakes.chat_chunk({"role": "assistant", "content": ""})),
             (0.0, fakes.chat_chunk({"content": "A"})),
             (5.0, fakes.chat_chunk({"content": "B"})),
             (0.0, fakes.chat_chunk({}, finish_reason="stop")),
             (0.0, fakes.DONE)],
            geschlossen=geschlossen)
        deps = self.deps(httpx.MockTransport(recorder), heartbeat_s=0.05)
        queue = self.hub.subscribe()

        antwort = await handle_v1(
            _request("POST", "/v1/chat/completions",
                     {"rid": "abb-spaet", "stream": False}),
            deps)
        self.assertIsInstance(antwort, StreamingResponse)
        erst = await anext(antwort.body_iterator)
        self.assertEqual(erst, b" ")

        self.registry.abort_signal("abb-spaet").set()
        roh = erst + await asyncio.wait_for(_body(antwort), 2.0)

        self.assertTrue(roh.startswith(b" "))
        self.assertEqual(json.loads(roh),
                         {"error": {"message": "Job wurde abgebrochen",
                                    "type": "aborted"}})
        self.assertEqual(recorder.abbrueche,
                         [{"rid": "abb-spaet", "abort_all": False}])
        self.assertTrue(geschlossen.is_set())
        job = self.registry.get("abb-spaet")
        self.assertIs(job.status, JobStatus.ABORTED)
        self.assertNotIn(
            "done", [rahmen["status"] for rahmen in _rahmen_lesen(queue)])


class _WartenderUpstream:
    """MockTransport-Handler: der Upstream antwortet nie (nur Abbrueche).

    ``/abort_request`` wird protokolliert und mit 200 beantwortet; jede
    andere Anfrage merkt den Eintritt und wartet endlos, bis sie (durch den
    Disconnect-Pfad) abgebrochen wird. ``abbruch_fehler`` laesst stattdessen
    genau den Abbruch-Aufruf scheitern.
    """

    def __init__(self, *, abbruch_fehler: Exception | None = None):
        self.anfragen: list[httpx.Request] = []
        self.eingetreten = asyncio.Event()
        self.abgebrochen = asyncio.Event()
        self._abbruch_fehler = abbruch_fehler

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        if request.url.path == "/abort_request":
            if self._abbruch_fehler is not None:
                raise self._abbruch_fehler
            return httpx.Response(200, json={"status": "ok"})
        self.eingetreten.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.abgebrochen.set()
            raise

    @property
    def abbrueche(self) -> list[dict]:
        return [json.loads(request.content) for request in self.anfragen
                if request.url.path == "/abort_request"]

    @property
    def abbrueche_versuche(self) -> int:
        """Zahl der versuchten Abbrueche (auch die gescheiterten)."""
        return sum(1 for request in self.anfragen
                   if request.url.path == "/abort_request")


class _AbbruchRecorder:
    """MockTransport-Handler: zeichnet Anfragen auf, streamt das SSE-Skript.

    Alle Pfade ausser ``/abort_request`` liefern die Skript-Schritte als
    zeitgetreuen SSE-Strom; Abbrueche werden protokolliert (und optional mit
    ``abbruch_fehler`` beantwortet). ``geschlossen`` wird gesetzt, sobald der
    SSE-Strom endet oder abgebrochen wird (Zombie-Pruefung der Tests).
    """

    def __init__(self, schritte=(), *, abbruch_fehler: Exception | None = None,
                 geschlossen: asyncio.Event | None = None):
        self.anfragen: list[httpx.Request] = []
        self._schritte = list(schritte)
        self._abbruch_fehler = abbruch_fehler
        self.geschlossen = (geschlossen if geschlossen is not None
                            else asyncio.Event())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        if request.url.path == "/abort_request":
            if self._abbruch_fehler is not None:
                raise self._abbruch_fehler
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=fakes.SkriptStream(self._schritte,
                                      geschlossen=self.geschlossen))

    @property
    def abbrueche(self) -> list[dict]:
        return [json.loads(request.content) for request in self.anfragen
                if request.url.path == "/abort_request"]


class TestDisconnectAufraeumen(ProxyTestBasis):
    """Client-Disconnect: Aufraeumen verwaister Jobs (nur mit Flag, Ruling R3).

    Der Disconnect kommt ueber eine steuerbare receive-Queue (``_ClientKanal``);
    erkannt wird er vom Watcher im Testintervall (``_DISCONNECT_CHECK_S``
    gepatcht).
    """

    async def test_nichtstream_disconnect_bricht_wartenden_job_ab(self):
        # flag=true, Disconnect waehrend der Upstream-Wartezeit: die pending
        # Aufgabe wird gecancelt, der Job sofort aborted + publiziert, ein
        # abort_request mit der rid geht raus; kein done.
        upstream = _WartenderUpstream()
        kanal = _ClientKanal()
        deps = self.deps(httpx.MockTransport(upstream), heartbeat_s=0.5)
        queue = self.hub.subscribe()

        with mock.patch("aiserver_monitor.proxy._DISCONNECT_CHECK_S", 0.05):
            aufgabe = asyncio.create_task(handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "disc-ns", "stream": False,
                          "messages": [{"role": "user", "content": "Hi"}]},
                         kanal=kanal),
                deps))
            await asyncio.wait_for(upstream.eingetreten.wait(), 2.0)
            kanal.abmelden()
            antwort = await asyncio.wait_for(aufgabe, 2.0)

        self.assertTrue(
            await _warte_bis(lambda: upstream.abgebrochen.is_set()),
            "der wartende Upstream-Aufruf wurde nicht abgebrochen")
        await _warte_bis(lambda: upstream.abbrueche)
        self.assertEqual(upstream.abbrueche,
                         [{"rid": "disc-ns", "abort_all": False}])
        job = self.registry.get("disc-ns")
        self.assertIs(job.status, JobStatus.ABORTED)
        self.assertIsNone(job.error)                    # aborted, nie error
        self.assertEqual([rahmen["status"] for rahmen in _rahmen_lesen(queue)],
                         ["waiting", "aborted"])
        # Die Antwort erreicht den Client nicht mehr; kein Traceback.
        self.assertNotIsInstance(antwort, StreamingResponse)
        self.assertEqual(antwort.status_code, 499)

    async def test_nichtstream_disconnect_im_streaming_teil_raeumt_ab(self):
        # Disconnect erst, wenn die Antwort schon als Streaming laeuft: der
        # Generator endet ohne Traceback, der Job bleibt aborted.
        upstream = _WartenderUpstream()
        kanal = _ClientKanal()
        deps = self.deps(httpx.MockTransport(upstream), heartbeat_s=0.05)
        queue = self.hub.subscribe()

        with mock.patch("aiserver_monitor.proxy._DISCONNECT_CHECK_S", 0.05):
            aufgabe = asyncio.create_task(handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "disc-ns-spaet", "stream": False},
                         kanal=kanal),
                deps))
            await asyncio.wait_for(upstream.eingetreten.wait(), 2.0)
            antwort = await asyncio.wait_for(aufgabe, 2.0)
            self.assertIsInstance(antwort, StreamingResponse)
            kanal.abmelden()
            roh = await asyncio.wait_for(_body(antwort), 2.0)

        self.assertEqual(roh.strip(), b"")              # nur evtl. Keepalives
        await _warte_bis(lambda: upstream.abbrueche)
        self.assertEqual(upstream.abbrueche,
                         [{"rid": "disc-ns-spaet", "abort_all": False}])
        job = self.registry.get("disc-ns-spaet")
        self.assertIs(job.status, JobStatus.ABORTED)
        self.assertEqual([rahmen["status"] for rahmen in _rahmen_lesen(queue)],
                         ["waiting", "aborted"])

    async def test_nichtstream_disconnect_ohne_flag_laeuft_zu_ende(self):
        # flag=false == Altverhalten: der Disconnect stoert nicht, der Job
        # laeuft zu Ende (done); kein abort_request.
        schritte = [(0.0, fakes.chat_chunk({"role": "assistant",
                                            "content": ""})),
                    (0.2, fakes.chat_chunk({"content": "spaet"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        recorder = _AbbruchRecorder(schritte)
        kanal = _ClientKanal()
        deps = self.deps(httpx.MockTransport(recorder),
                         heartbeat_s=0.05, abort_on_disconnect=False)
        queue = self.hub.subscribe()

        with mock.patch("aiserver_monitor.proxy._DISCONNECT_CHECK_S", 0.05):
            aufgabe = asyncio.create_task(handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "disc-aus", "stream": False}, kanal=kanal),
                deps))
            await asyncio.sleep(0.05)
            kanal.abmelden()
            antwort = await asyncio.wait_for(aufgabe, 2.0)
            roh = await _body(antwort)

        self.assertEqual(json.loads(roh)["choices"][0]["message"]["content"],
                         "spaet")
        self.assertIs(self.registry.get("disc-aus").status, JobStatus.DONE)
        status = [rahmen["status"] for rahmen in _rahmen_lesen(queue)]
        self.assertEqual(status[0], "waiting")
        self.assertEqual(status[-1], "done")
        self.assertNotIn("aborted", status)
        self.assertEqual(recorder.abbrueche, [])        # kein Abort raus

    async def test_stream_disconnect_bricht_laufenden_job_ab(self):
        # flag=true, Disconnect waehrend der laufenden Antwort: abort_request
        # mit der rid, Job aborted, Relay beendet, kein done-Frame.
        schritte = [(0.0, fakes.chat_chunk({"content": "A"})),
                    (5.0, fakes.chat_chunk({"content": "B"})),
                    (0.0, fakes.chat_chunk({}, finish_reason="stop")),
                    (0.0, fakes.DONE)]
        recorder = _AbbruchRecorder(schritte)
        kanal = _ClientKanal()
        deps = self.deps(httpx.MockTransport(recorder), heartbeat_s=0.05)
        queue = self.hub.subscribe()

        with mock.patch("aiserver_monitor.proxy._DISCONNECT_CHECK_S", 0.05):
            antwort = await asyncio.wait_for(handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "disc-stream", "stream": True}, kanal=kanal),
                deps), 2.0)
            self.assertIsInstance(antwort, StreamingResponse)
            await _warte_bis(lambda: self.registry.get(
                "disc-stream").status is JobStatus.RUNNING)
            kanal.abmelden()
            fertig = await _warte_bis(lambda: self.registry.get(
                "disc-stream").status is JobStatus.ABORTED)
            self.assertTrue(fertig, "Job blieb haengen (Zombie)")
            # Relay beendet: der Generator laeuft aus, ohne weiterzulesen.
            roh = await asyncio.wait_for(_body(antwort), 2.0)
            await _warte_bis(lambda: recorder.abbrueche)

        self.assertNotIn(b'"B"', roh)
        self.assertNotIn(fakes.DONE, roh)
        self.assertEqual(recorder.abbrueche,
                         [{"rid": "disc-stream", "abort_all": False}])
        job = self.registry.get("disc-stream")
        self.assertIs(job.status, JobStatus.ABORTED)
        self.assertIsNone(job.error)
        status = [rahmen["status"] for rahmen in _rahmen_lesen(queue)]
        self.assertEqual(status[0], "waiting")
        self.assertEqual(status[-1], "aborted")
        self.assertNotIn("done", status)

    async def test_stream_disconnect_vor_der_kopfzeile_bricht_ab(self):
        # Der Upstream liefert noch keine Kopfzeile: der Disconnect bricht den
        # wartenden Aufruf ab, abort_request geht mit der bekannten rid raus;
        # es entsteht kein Job und die Funktion kehrt ohne Traceback zurueck.
        upstream = _WartenderUpstream()
        kanal = _ClientKanal()
        deps = self.deps(httpx.MockTransport(upstream), heartbeat_s=0.5)

        with mock.patch("aiserver_monitor.proxy._DISCONNECT_CHECK_S", 0.05):
            aufgabe = asyncio.create_task(handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "disc-kopf", "stream": True}, kanal=kanal),
                deps))
            await asyncio.wait_for(upstream.eingetreten.wait(), 2.0)
            kanal.abmelden()
            antwort = await asyncio.wait_for(aufgabe, 2.0)

        self.assertTrue(upstream.abgebrochen.is_set())
        await _warte_bis(lambda: upstream.abbrueche)
        self.assertEqual(upstream.abbrueche,
                         [{"rid": "disc-kopf", "abort_all": False}])
        self.assertIsNone(self.registry.get("disc-kopf"))
        self.assertNotIsInstance(antwort, StreamingResponse)
        self.assertEqual(antwort.status_code, 499)

    async def test_stream_disconnect_beim_kopfzeilenwechsel_kein_zombie(self):
        # Der Disconnect rennt mit dem Kopfzeilen-Empfang: der Job wird noch
        # angelegt und sofort als aborted abgeschlossen (kein Zombie). Der
        # doppelte abort_request ist idempotent.
        recorder = _AbbruchRecorder([(5.0, fakes.chat_chunk({"content": "B"}))])
        kanal = _ClientKanal()
        # Anfrage bauen (Body), dann ist der Client schon weg.
        request = _request("POST", "/v1/chat/completions",
                           {"rid": "disc-wechsel", "stream": True},
                           kanal=kanal)
        kanal.abmelden()
        deps = self.deps(httpx.MockTransport(recorder), heartbeat_s=0.05)
        queue = self.hub.subscribe()

        def job_abortiert() -> bool:
            job = self.registry.get("disc-wechsel")
            return job is not None and job.status is JobStatus.ABORTED

        with mock.patch("aiserver_monitor.proxy._DISCONNECT_CHECK_S", 0.05):
            antwort = await asyncio.wait_for(handle_v1(request, deps), 2.0)
            fertig = await _warte_bis(job_abortiert)
            self.assertTrue(fertig, "Job blieb haengen (Zombie)")
            await asyncio.wait_for(_body(antwort), 2.0)  # Relay endet sofort
            await _warte_bis(lambda: recorder.abbrueche)

        job = self.registry.get("disc-wechsel")
        self.assertIs(job.status, JobStatus.ABORTED)
        self.assertIn({"rid": "disc-wechsel", "abort_all": False},
                      recorder.abbrueche)
        status = [rahmen["status"] for rahmen in _rahmen_lesen(queue)]
        self.assertEqual(status[0], "waiting")
        self.assertEqual(status[-1], "aborted")
        self.assertNotIn("done", status)

    async def test_abort_fehler_beim_disconnect_wird_nur_geloggt(self):
        # R3d: scheitert abort_request, wird das nur geloggt; der Abbruch
        # selbst (Task canceln, Job aborted, publish) laeuft weiter.
        upstream = _WartenderUpstream(
            abbruch_fehler=httpx.ConnectError("upstream weg"))
        kanal = _ClientKanal()
        deps = self.deps(httpx.MockTransport(upstream), heartbeat_s=0.5)
        queue = self.hub.subscribe()

        with self.assertLogs("aiserver_monitor.proxy", level="WARNING") as cm:
            with mock.patch("aiserver_monitor.proxy._DISCONNECT_CHECK_S", 0.05):
                aufgabe = asyncio.create_task(handle_v1(
                    _request("POST", "/v1/chat/completions",
                             {"rid": "disc-fehler", "stream": False},
                             kanal=kanal),
                    deps))
                await asyncio.wait_for(upstream.eingetreten.wait(), 2.0)
                kanal.abmelden()
                antwort = await asyncio.wait_for(aufgabe, 2.0)
                await _warte_bis(lambda: upstream.abbrueche_versuche >= 1)

        self.assertTrue(any("disc-fehler" in zeile for zeile in cm.output),
                        "Abort-Fehler wurde nicht geloggt")
        self.assertTrue(upstream.abgebrochen.is_set())
        job = self.registry.get("disc-fehler")
        self.assertIs(job.status, JobStatus.ABORTED)
        self.assertEqual([rahmen["status"] for rahmen in _rahmen_lesen(queue)],
                         ["waiting", "aborted"])
        self.assertNotIsInstance(antwort, StreamingResponse)
        self.assertEqual(antwort.status_code, 499)


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
        # Schneller Schwall: die Drossel laesst nur wenige Live-Ereignisse
        # durch, der Abschluss kommt aber immer (auch wenn er in die Drossel
        # faellt). Das erste Frame ist das Anlage-Ereignis "waiting"
        # (Register-Publizierung wie im Nicht-Stream-Pfad).
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
        self.assertGreaterEqual(len(rahmen), 2)
        self.assertLessEqual(len(rahmen), 5)            # waiting + ~4/s
        daten = [json.loads(rahmen_eintrag.split("data: ", 1)[1])
                 for rahmen_eintrag in rahmen]
        self.assertEqual(daten[0]["status"], "waiting")
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


class _KaputteHistory:
    """History, deren Schreiben immer wirft (Task-7-Minor: best-effort)."""

    def record_job(self, job_dict: dict) -> None:
        raise RuntimeError("DB kaputt")


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

    async def test_history_wird_beim_nichtstream_chat_genau_einmal_geschrieben(
            self):
        # R3 gilt auch fuer den konvertierten Non-Stream-Chat: genau ein
        # History-Eintrag mit usage-Werten.
        schritte = _chat_skript(
            ["Hallo"],
            usage={"prompt_tokens": 7, "completion_tokens": 4,
                   "total_tokens": 11})
        with tempfile.TemporaryDirectory() as tmp:
            hist = History(Path(tmp) / "monitor.db")
            hist.init()
            self.addCleanup(hist.close)
            zaehler = _ZaehlendeHistory(hist)
            deps = self.deps(fakes.skript_transport(schritte),
                             history=zaehler)

            antwort = await handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "hist-ns", "stream": False,
                          "messages": [{"role": "user", "content": "Hi"}]}),
                deps)
            await _body(antwort)

            self.assertEqual(zaehler.aufrufe, 1)
            fenster = hist.query_stats(0.0, time.time() + 60.0, 60)
            self.assertEqual(fenster["overview"]["count"], 1)
            self.assertEqual(fenster["overview"]["done"], 1)
            self.assertEqual(fenster["overview"]["tokens_in"], 7)
            self.assertEqual(fenster["overview"]["tokens_out"], 4)

    async def test_history_fehler_verschluckt_abschluss_nicht(self):
        # Task-7-Minor: das Endereignis geht auch raus, wenn die History
        # wirft; der Job endet trotzdem sauber (kein un-abgeholter Fehler).
        queue = self.hub.subscribe()
        with self.assertLogs("aiserver_monitor.proxy", level="WARNING") as cm:
            antwort = await handle_v1(
                _request("POST", "/v1/chat/completions",
                         {"rid": "hist-kaputt", "stream": False}),
                self.deps(fakes.json_transport(b'{"choices": []}'),
                          history=_KaputteHistory()))
            await _body(antwort)

        self.assertTrue(any("History-Eintrag" in zeile for zeile in cm.output))
        self.assertIs(self.registry.get("hist-kaputt").status, JobStatus.DONE)
        ereignisse = _rahmen_lesen(queue)
        self.assertEqual(ereignisse[-1]["status"], "done")


if __name__ == "__main__":
    unittest.main()
