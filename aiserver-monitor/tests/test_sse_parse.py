"""Tests fuer den Upstream-SSE-Parser und die Chunk-Zusammenfassung."""

import unittest

from aiserver_monitor.sse_parse import ChunkSummary, SSEByteParser, summarize_chunk


class TestSSEByteParser(unittest.TestCase):
    def test_parser_multibyte_ueber_grenzen(self):
        p = SSEByteParser()
        raw = 'data: {"choices":[{"delta":{"content":"gru\u0308\u00df"}}]}\n'.encode("utf-8")
        cut = raw.index("\u00df".encode("utf-8")) + 1        # mitten im Mehrbyte-Zeichen
        self.assertEqual(p.feed(raw[:cut]), [])
        objs = p.feed(raw[cut:])
        self.assertEqual(objs[0]["choices"][0]["delta"]["content"], "gru\u0308\u00df")

    def test_nur_komplette_zeilen_werden_geparst(self):
        p = SSEByteParser()
        self.assertEqual(p.feed(b'data: {"a": 1}'), [])      # noch kein \n
        self.assertEqual(p.feed(b'\n'), [{"a": 1}])

    def test_mehrere_zeilen_in_einem_chunk(self):
        p = SSEByteParser()
        self.assertEqual(p.feed(b'data: {"n": 1}\ndata: {"n": 2}\n'),
                         [{"n": 1}, {"n": 2}])

    def test_done_kommentar_und_leerzeilen_verworfen(self):
        p = SSEByteParser()
        self.assertEqual(
            p.feed(b": keepalive\n"
                   b"\n"
                   b"data: [DONE]\n"
                   b'data: {"n": 3}\n'),
            [{"n": 3}])

    def test_andere_felder_werden_verworfen(self):
        p = SSEByteParser()
        self.assertEqual(p.feed(b"event: message\nid: 7\n" + b'data: {"n": 4}\n'),
                         [{"n": 4}])

    def test_crlf_zeilenenden(self):
        p = SSEByteParser()
        self.assertEqual(p.feed(b'data: {"n": 5}\r\n'), [{"n": 5}])

    def test_data_ohne_leerzeichen_nach_doppelpunkt(self):
        p = SSEByteParser()
        self.assertEqual(p.feed(b'data:{"n": 6}\n'), [{"n": 6}])

    def test_kaputtes_json_wird_verworfen(self):
        p = SSEByteParser()
        self.assertEqual(p.feed(b'data: kein json\n' + b'data: {"n": 7}\n'),
                         [{"n": 7}])

    def test_nur_json_objekte_werden_geliefert(self):
        p = SSEByteParser()
        self.assertEqual(p.feed(b'data: [1, 2]\ndata: "text"\ndata: 5\n'), [])

    def test_ungueltiges_utf8_wird_verworfen(self):
        # 0xFF ist in UTF-8 nie gueltig; die Zeile faellt weg, die naechste bleibt.
        p = SSEByteParser()
        self.assertEqual(p.feed(b'data: \xff\xfe\n' + b'data: {"n": 8}\n'),
                         [{"n": 8}])

    def test_leerer_feed(self):
        p = SSEByteParser()
        self.assertEqual(p.feed(b""), [])


class TestSummarizeChunk(unittest.TestCase):
    def test_vollstaendiger_chunk(self):
        s = summarize_chunk({
            "choices": [{
                "delta": {"content": "hallo", "reasoning_content": "denk"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        })
        self.assertEqual(s, ChunkSummary("hallo", "denk", 11, 7, "stop"))

    def test_usage_chunk_ohne_choices(self):
        # SGLang schickt usage im letzten Chunk oft mit leeren choices.
        s = summarize_chunk({"choices": [],
                             "usage": {"prompt_tokens": 11,
                                       "completion_tokens": 7}})
        self.assertEqual(s, ChunkSummary(None, None, 11, 7, None))

    def test_content_und_reasoning(self):
        s = summarize_chunk({"choices": [
            {"delta": {"content": "a", "reasoning_content": "b"}}]})
        self.assertEqual((s.content, s.reasoning), ("a", "b"))

    def test_nur_finish_reason(self):
        s = summarize_chunk({"choices": [
            {"delta": {}, "finish_reason": "length"}]})
        self.assertEqual(s.finish_reason, "length")
        self.assertIsNone(s.content)

    def test_null_werte_bleiben_none(self):
        s = summarize_chunk({"choices": [
            {"delta": {"content": None, "reasoning_content": None},
             "finish_reason": None}]})
        self.assertEqual(s, ChunkSummary(None, None, None, None, None))

    def test_leerer_und_unerwarteter_chunk(self):
        leer = ChunkSummary(None, None, None, None, None)
        self.assertEqual(summarize_chunk({}), leer)
        self.assertEqual(summarize_chunk({"choices": None, "usage": None}), leer)
        self.assertEqual(summarize_chunk({"choices": [{}]}), leer)

    def test_nicht_string_content_gilt_als_nicht_gemessen(self):
        # ChunkSummary fuehrt nur Strings; alles andere ist None.
        s = summarize_chunk({"choices": [{"delta": {"content": 42}}]})
        self.assertIsNone(s.content)


if __name__ == "__main__":
    unittest.main()
