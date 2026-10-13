"""Tests fuer SSE-Formatierung, EventHub und sse_stream."""

import asyncio
import json
import unittest

from aiserver_monitor.events import (
    EventHub,
    format_comment,
    format_sse,
    sse_stream,
)


class TestFormatSse(unittest.TestCase):
    def test_event_und_data_zeilen_mit_leerzeile_am_ende(self):
        text = format_sse("job", {"rid": "abc", "status": "running"})
        self.assertEqual(
            text, 'event: job\ndata: {"rid": "abc", "status": "running"}\n\n')

    def test_umlaute_bleiben_lesbar(self):
        # ensure_ascii=False: echte UTF-8-Zeichen statt \uXXXX-Escapes.
        text = format_sse("job", {"vorschau": "gru\u00dfe"})
        self.assertIn("gru\u00dfe", text)
        self.assertNotIn("\\u00df", text)

    def test_zeilenumbruch_im_datensatz_bricht_den_frame_nicht(self):
        # json.dumps escapt den Umbruch; der Frame bleibt zwei Zeilen + Leerzeile.
        text = format_sse("job", {"text": "a\nb"})
        self.assertEqual(text, 'event: job\ndata: {"text": "a\\nb"}\n\n')

    def test_datensatz_ist_json_lesbar(self):
        data = {"ts": 1.5, "verfuegbar": True, "liste": [1, None]}
        text = format_sse("stats", data)
        self.assertTrue(text.startswith("event: stats\ndata: "))
        self.assertTrue(text.endswith("\n\n"))
        self.assertEqual(json.loads(text.split("data: ", 1)[1].rstrip("\n")),
                         data)


class TestFormatComment(unittest.TestCase):
    def test_format_comment(self):
        self.assertEqual(format_comment("ping"), ": ping\n\n")


class TestEventHub(unittest.TestCase):
    def test_publish_erreicht_den_abonnenten(self):
        hub = EventHub()
        queue = hub.subscribe()
        self.assertEqual(hub.subscriber_count, 1)
        hub.publish("job", {"rid": "a"})
        self.assertEqual(queue.get_nowait(), format_sse("job", {"rid": "a"}))

    def test_publish_an_alle_abonnenten(self):
        hub = EventHub()
        q1 = hub.subscribe()
        q2 = hub.subscribe()
        self.assertEqual(hub.subscriber_count, 2)
        hub.publish("stats", {"ts": 1.0})
        frame = format_sse("stats", {"ts": 1.0})
        self.assertEqual(q1.get_nowait(), frame)
        self.assertEqual(q2.get_nowait(), frame)

    def test_unsubscribe_entfernt_queue(self):
        hub = EventHub()
        queue = hub.subscribe()
        hub.unsubscribe(queue)
        self.assertEqual(hub.subscriber_count, 0)
        hub.publish("job", {"rid": "a"})            # kein Abonnent: kein Fehler
        self.assertTrue(queue.empty())

    def test_unsubscribe_unbekannter_queue_ist_tolerant(self):
        hub = EventHub()
        hub.unsubscribe(asyncio.Queue())            # idempotent, kein KeyError
        self.assertEqual(hub.subscriber_count, 0)

    def test_publish_ohne_abonnenten_ist_kein_fehler(self):
        EventHub().publish("job", {"rid": "a"})


class TestSseStream(unittest.IsolatedAsyncioTestCase):
    async def test_initial_zuerst_dann_ereignis(self):
        hub = EventHub()
        stream = sse_stream(hub, ["initial"], heartbeat_s=0.05)
        self.assertEqual(await anext(stream), "initial")
        hub.publish("job", {"rid": "a"})
        frame = await asyncio.wait_for(anext(stream), timeout=0.3)
        self.assertEqual(frame, format_sse("job", {"rid": "a"}))
        await stream.aclose()

    async def test_heartbeat_wenn_leer(self):
        # Laut Brief: hub ohne Ereignis, heartbeat_s=0.05 -> erste Ausgabe ist
        # "initial", danach kommt ": ping\n\n" innerhalb von ~0.3 s.
        hub = EventHub()
        stream = sse_stream(hub, ["initial"], heartbeat_s=0.05)
        self.assertEqual(await anext(stream), "initial")
        self.assertEqual(
            await asyncio.wait_for(anext(stream), timeout=0.3), ": ping\n\n")
        self.assertEqual(
            await asyncio.wait_for(anext(stream), timeout=0.3), ": ping\n\n")
        await stream.aclose()

    async def test_mehrere_initiale_frames_in_reihenfolge(self):
        hub = EventHub()
        stream = sse_stream(hub, ["eins", "zwei"], heartbeat_s=0.05)
        self.assertEqual(await anext(stream), "eins")
        self.assertEqual(await anext(stream), "zwei")
        await stream.aclose()

    async def test_ereignisse_kommen_in_reihenfolge(self):
        hub = EventHub()
        stream = sse_stream(hub, [], heartbeat_s=0.05)
        # subscribe passiert erst beim ersten anext -> erst anziehen, dann senden.
        self.assertEqual(await asyncio.wait_for(anext(stream), timeout=0.3),
                         ": ping\n\n")
        hub.publish("a", {"n": 1})
        hub.publish("b", {"n": 2})
        self.assertEqual(
            await asyncio.wait_for(anext(stream), timeout=0.3),
            format_sse("a", {"n": 1}))
        self.assertEqual(
            await asyncio.wait_for(anext(stream), timeout=0.3),
            format_sse("b", {"n": 2}))
        await stream.aclose()

    async def test_aclose_meldet_den_abonnenten_ab(self):
        hub = EventHub()
        stream = sse_stream(hub, [], heartbeat_s=0.05)
        self.assertEqual(hub.subscriber_count, 0)    # lazy: noch nicht gestartet
        self.assertEqual(await asyncio.wait_for(anext(stream), timeout=0.3),
                         ": ping\n\n")
        self.assertEqual(hub.subscriber_count, 1)
        await stream.aclose()
        self.assertEqual(hub.subscriber_count, 0)


if __name__ == "__main__":
    unittest.main()
