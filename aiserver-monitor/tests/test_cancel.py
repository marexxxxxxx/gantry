"""Tests fuer den Abbruch-Aufruf an den SGLang-Upstream (cancel.py).

Alle Faelle laufen ueber httpx.MockTransport; der Transport zaehlt die
Anfragen, damit "kein HTTP-Aufruf" bei leerer rid beweisbar ist.
"""

import json
import unittest

import httpx

from aiserver_monitor.cancel import abort_request

UPSTREAM = "http://127.0.0.1:30001"


class TestAbortRequest(unittest.IsolatedAsyncioTestCase):
    async def test_einzelabbruch_sendet_exakten_body(self):
        gesehen = []

        def handler(request):
            gesehen.append(request)
            return httpx.Response(200, json={"status": "ok"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ok = await abort_request(client, UPSTREAM, rid="abc", abort_all=False)

        self.assertTrue(ok)
        self.assertEqual(len(gesehen), 1)
        request = gesehen[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/abort_request")
        self.assertEqual(json.loads(request.content),
                         {"rid": "abc", "abort_all": False})

    async def test_leere_rid_valueerror_ohne_request(self):
        anfragen = []

        def handler(request):
            anfragen.append(request)
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for rid in (None, ""):
                with self.subTest(rid=repr(rid)):
                    with self.assertRaises(ValueError):
                        await abort_request(client, UPSTREAM, rid=rid,
                                            abort_all=False)

        self.assertEqual(anfragen, [])                      # kein HTTP-Aufruf

    async def test_http_500_liefert_false_und_loggt(self):
        def handler(request):
            return httpx.Response(500, text="kaputt")

        with self.assertLogs("aiserver_monitor.cancel", level="WARNING") as cm:
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)) as client:
                ok = await abort_request(client, UPSTREAM, rid="abc",
                                         abort_all=False)

        self.assertFalse(ok)
        self.assertTrue(any("500" in zeile for zeile in cm.output))

    async def test_abort_all_sendet_leere_rid(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for rid in (None, ""):
                with self.subTest(rid=repr(rid)):
                    ok = await abort_request(client, UPSTREAM, rid=rid,
                                             abort_all=True)
                    self.assertTrue(ok)

        self.assertEqual(bodies, [{"rid": "", "abort_all": True},
                                  {"rid": "", "abort_all": True}])

    async def test_doppelter_abort_ist_kein_fehler(self):
        def handler(request):
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            erster = await abort_request(client, UPSTREAM, rid="abc",
                                         abort_all=False)
            zweiter = await abort_request(client, UPSTREAM, rid="abc",
                                          abort_all=False)

        self.assertTrue(erster)                             # idempotent
        self.assertTrue(zweiter)


if __name__ == "__main__":
    unittest.main()
