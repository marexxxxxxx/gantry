"""Tests fuer die Audio-FastAPI-App (Health, Modelle, Transkription)."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from aiserver_audio.app import create_audio_app, format_transcript, stream_speech
from aiserver_audio.catalog import piper_files, resolve_voice
from aiserver_audio.config import AudioSettings
from aiserver_audio.errors import AudioError
from aiserver_audio.queue import QueueFull
from aiserver_audio.stt import Segment, Transcription
from aiserver_audio.tts import AudioChunk


def _modelle_anlegen(models_dir: Path, modell: str = "small") -> None:
    ziel = models_dir / "whisper" / modell
    ziel.mkdir(parents=True, exist_ok=True)
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (ziel / name).write_bytes(b"x")


def _stimme_anlegen(models_dir: Path, name: str) -> None:
    onnx, config = piper_files(models_dir, resolve_voice(name))
    onnx.parent.mkdir(parents=True, exist_ok=True)
    onnx.write_bytes(b"x")
    config.write_text("{}", encoding="utf-8")


class FakeStt:
    def __init__(self):
        self.preloaded = []
        self.aufrufe = []

    def preload(self, info):
        self.preloaded.append(info.id)

    def transcribe(self, path, *, info, language, prompt, temperature):
        self.aufrufe.append({
            "path": Path(path), "info": info, "language": language,
            "prompt": prompt, "temperature": temperature,
            "datei_war_da": Path(path).exists(),
        })
        return Transcription(
            text="Hallo", language=language or "de", duration=2.5,
            segments=(Segment(0.0, 1.0, "Hallo"),
                      Segment(1.0, 2.5, "Welt")))


class FakeTts:
    def __init__(self, *, rate=22050, pcm=b"\x01\x00" * 32,
                 delay=0.0, events=None):
        self.rate = rate
        self.pcm = pcm
        self.delay = delay
        self.events = events if events is not None else []
        self.aufrufe = []
        self.offen = 0

    def synthesize(self, text, *, info, speed):
        self.aufrufe.append({"text": text, "info": info, "speed": speed})
        self.offen += 1
        self.events.append(("start", text))
        try:
            if self.delay:
                time.sleep(self.delay)
            yield AudioChunk(self.pcm, self.rate)
        finally:
            self.offen -= 1
            self.events.append(("ende", text))


class _FakeStdin:
    def __init__(self):
        self.closed = False
        self.daten = bytearray()

    def write(self, block):
        self.daten.extend(block)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class _FakeStdout:
    def __init__(self, ausgabe):
        self.ausgabe = ausgabe
        self.gelesen = False

    async def read(self, _groesse):
        if self.gelesen:
            return b""
        self.gelesen = True
        return self.ausgabe


class _FakeProzess:
    def __init__(self, ausgabe):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(ausgabe)
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        self.returncode = 0
        return 0


class FakeEncoder:
    def __init__(self, ausgabe=b"encoded"):
        self.ausgabe = ausgabe
        self.prozesse = []

    async def __call__(self, fmt, sample_rate):
        prozess = _FakeProzess(self.ausgabe)
        prozess.fmt = fmt
        prozess.sample_rate = sample_rate
        self.prozesse.append(prozess)
        return prozess



class AudioAppTestBasis(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.models_dir = Path(self._tmp.name)
        self.stt = FakeStt()
        self.tts = FakeTts()
        self.settings = AudioSettings(models_dir=self.models_dir,
                                      preload="")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def app(self, **kwargs):
        return create_audio_app(
            self.settings, stt_engine=self.stt, tts_engine=self.tts,
            duration_probe=kwargs.pop("duration_probe", lambda _: 2.5),
            encoder_factory=kwargs.pop("encoder_factory", FakeEncoder()),
            **kwargs)

    async def request(self, app, methode, pfad, *,
                      raise_app_exceptions=True, **kwargs):
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                    transport=ASGITransport(
                        app=app,
                        raise_app_exceptions=raise_app_exceptions),
                    base_url="http://audio") as client:
                return await client.request(methode, pfad, **kwargs)

    async def transkription(self, app, daten=None, datei=None, **kwargs):
        form = {"model": "whisper-small", "language": "de"}
        form.update(daten or {})
        files = {"file": ("ton.wav", datei or b"RIFF", "audio/wav")}
        return await self.request(app, "POST", "/v1/audio/transcriptions",
                                  data=form, files=files, **kwargs)


class TestHealthUndModelle(AudioAppTestBasis):
    async def test_healthz_ohne_modelle(self):
        antwort = await self.request(self.app(), "GET", "/healthz")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {"status": "ok"})

    async def test_modelle_zeigen_installationsstatus(self):
        antwort = await self.request(self.app(), "GET", "/v1/audio/models")
        self.assertEqual(antwort.status_code, 200)
        daten = antwort.json()
        self.assertEqual(daten["whisper"]["default"], "whisper-small")
        self.assertEqual(daten["tts"]["default_voice"],
                         "de_DE-thorsten-medium")
        self.assertTrue(all(not m["installed"]
                            for m in daten["whisper"]["models"]))
        self.assertTrue(all(not v["installed"]
                            for v in daten["tts"]["voices"]))

    async def test_preload_beim_start(self):
        _modelle_anlegen(self.models_dir)
        self.settings = AudioSettings(models_dir=self.models_dir,
                                      preload="whisper-small")
        await self.request(self.app(), "GET", "/healthz")
        self.assertEqual(self.stt.preloaded, ["whisper-small"])

    async def test_unbekannter_pfad_404_mit_fehlerformat(self):
        antwort = await self.request(
            self.app(), "GET", "/v1/audio/unbekannt")
        self.assertEqual(antwort.status_code, 404)
        fehler = antwort.json()["error"]
        self.assertEqual(fehler["code"], "invalid_request")
        self.assertEqual(fehler["type"], "invalid_request_error")


class TestTranskription(AudioAppTestBasis):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        _modelle_anlegen(self.models_dir)

    async def test_json_und_defaults(self):
        antwort = await self.transkription(self.app(), daten={})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.json(), {"text": "Hallo"})
        self.assertEqual(self.stt.aufrufe[0]["info"].id, "whisper-small")
        self.assertEqual(self.stt.aufrufe[0]["language"], "de")
        self.assertTrue(self.stt.aufrufe[0]["datei_war_da"])

    async def test_text_format(self):
        antwort = await self.transkription(
            self.app(), daten={"response_format": "text"})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.text, "Hallo")
        self.assertTrue(antwort.headers["content-type"].startswith("text/plain"))

    async def test_verbose_json(self):
        antwort = await self.transkription(
            self.app(), daten={"response_format": "verbose_json"})
        daten = antwort.json()
        self.assertEqual(daten["task"], "transcribe")
        self.assertEqual(daten["language"], "de")
        self.assertEqual(daten["duration"], 2.5)
        self.assertEqual(daten["text"], "Hallo")
        self.assertEqual(len(daten["segments"]), 2)
        self.assertEqual(daten["segments"][0]["text"], "Hallo")

    async def test_srt_und_vtt_enthalten_zeitstempel(self):
        srt = await self.transkription(
            self.app(), daten={"response_format": "srt"})
        self.assertEqual(srt.status_code, 200)
        self.assertIn("00:00:00,000 --> 00:00:01,000", srt.text)
        self.assertIn("Welt", srt.text)
        vtt = await self.transkription(
            self.app(), daten={"response_format": "vtt"})
        self.assertTrue(vtt.text.startswith("WEBVTT"))
        self.assertIn("00:00:01.000 --> 00:00:02.500", vtt.text)

    async def test_alias_und_language_auto(self):
        antwort = await self.transkription(
            self.app(), daten={"model": "whisper-1", "language": "auto"})
        self.assertEqual(antwort.status_code, 200)
        self.assertIsNone(self.stt.aufrufe[0]["language"])

    async def test_language_code_wird_normalisiert(self):
        antwort = await self.transkription(
            self.app(), daten={"language": " DE "})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(self.stt.aufrufe[0]["language"], "de")

    async def test_unbekannte_sprache_400_ohne_transkription(self):
        antwort = await self.transkription(
            self.app(), daten={"language": "xx"})
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.json()["error"]["code"], "invalid_request")
        self.assertEqual(self.stt.aufrufe, [])

    async def test_prompt_und_temperature_werden_uebergeben(self):
        antwort = await self.transkription(
            self.app(), daten={"prompt": "Geheim", "temperature": "0.2"})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(self.stt.aufrufe[0]["prompt"], "Geheim")
        self.assertEqual(self.stt.aufrufe[0]["temperature"], 0.2)

    async def test_translate_und_format_werden_abgelehnt(self):
        for daten in ({"task": "translate"},
                      {"response_format": "flac"}):
            with self.subTest(daten=daten):
                antwort = await self.transkription(self.app(), daten=daten)
                self.assertEqual(antwort.status_code, 400)
                self.assertEqual(antwort.json()["error"]["code"],
                                 "invalid_request")

    async def test_unbekanntes_modell_404(self):
        antwort = await self.transkription(
            self.app(), daten={"model": "gibtsnicht"})
        self.assertEqual(antwort.status_code, 404)
        self.assertEqual(antwort.json()["error"]["code"], "model_not_found")

    async def test_nicht_installiertes_modell_503(self):
        antwort = await self.transkription(
            self.app(), daten={"model": "medium"})
        self.assertEqual(antwort.status_code, 503)
        fehler = antwort.json()["error"]
        self.assertEqual(fehler["code"], "model_not_installed")
        self.assertIn("--whisper medium", fehler["message"])

    async def test_multipart_ohne_datei_400(self):
        antwort = await self.request(
            self.app(), "POST", "/v1/audio/transcriptions",
            data={"model": "whisper-small"})
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.json()["error"]["code"], "invalid_request")

    async def test_kaputter_multipart_400_mit_fehlerformat(self):
        with self.assertLogs("python_multipart.multipart", level="WARNING"):
            antwort = await self.request(
                self.app(), "POST", "/v1/audio/transcriptions",
                content=b"kaputt",
                headers={"content-type":
                         "multipart/form-data; boundary=defekt"})
        self.assertEqual(antwort.status_code, 400)
        fehler = antwort.json()["error"]
        self.assertEqual(fehler["code"], "invalid_request")
        self.assertEqual(fehler["type"], "invalid_request_error")
        self.assertEqual(self.stt.aufrufe, [])

    async def test_zu_grosse_datei_413(self):
        self.settings = AudioSettings(models_dir=self.models_dir,
                                      preload="", max_upload_mb=1)
        antwort = await self.transkription(
            self.app(), datei=b"x" * (1024 * 1024 + 1))
        self.assertEqual(antwort.status_code, 413)
        self.assertEqual(antwort.json()["error"]["code"], "audio_too_large")

    async def test_zu_lange_datei_413(self):
        antwort = await self.transkription(
            self.app(duration_probe=lambda _: 3601.0))
        self.assertEqual(antwort.status_code, 413)
        self.assertEqual(antwort.json()["error"]["code"], "audio_too_long")

    async def test_unlesbare_datei_400(self):
        antwort = await self.transkription(
            self.app(duration_probe=lambda _: None))
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.json()["error"]["code"], "invalid_audio")

    async def test_queue_voll_429(self):
        class Voll:
            async def acquire(self):
                raise QueueFull(5)

            def release(self):
                pass

        app = self.app()
        app.state.stt_admission = Voll()
        antwort = await self.transkription(app)
        self.assertEqual(antwort.status_code, 429)
        self.assertEqual(antwort.json()["error"]["code"], "queue_full")
        self.assertEqual(antwort.headers["retry-after"], "5")

    async def test_timeout_504(self):
        class Langsam(FakeStt):
            def transcribe(self, *args, **kwargs):
                time.sleep(1.2)
                return super().transcribe(*args, **kwargs)

        self.settings = AudioSettings(models_dir=self.models_dir,
                                      preload="", stt_timeout_s=1)
        app = create_audio_app(
            self.settings, stt_engine=Langsam(), tts_engine=FakeTts(),
            duration_probe=lambda _: 2.5)
        antwort = await self.transkription(app)
        self.assertEqual(antwort.status_code, 504)
        self.assertEqual(antwort.json()["error"]["code"], "timeout")

    async def test_timeout_haelt_datei_und_slot_bis_arbeit_endet(self):
        class Langsam(FakeStt):
            pfad = None

            def transcribe(self, path, **kwargs):
                self.pfad = Path(path)
                time.sleep(0.6)
                return super().transcribe(path, **kwargs)

        class Zaehler:
            freigaben = 0

            async def acquire(self):
                pass

            def release(self):
                self.freigaben += 1

        stt = Langsam()
        zaehler = Zaehler()
        self.settings = AudioSettings(models_dir=self.models_dir,
                                      preload="", stt_timeout_s=0.1)
        app = create_audio_app(
            self.settings, stt_engine=stt, tts_engine=FakeTts(),
            duration_probe=lambda _: 2.5)
        app.state.stt_admission = zaehler
        antwort = await self.transkription(app)
        self.assertEqual(antwort.status_code, 504)
        self.assertTrue(stt.pfad.exists())
        self.assertEqual(zaehler.freigaben, 0)
        await asyncio.sleep(0.7)
        self.assertFalse(stt.pfad.exists())
        self.assertEqual(zaehler.freigaben, 1)

    async def test_keine_transkripte_oder_prompts_im_log(self):
        with self.assertLogs("aiserver_audio.app", level="INFO") as logs:
            antwort = await self.transkription(
                self.app(), daten={"prompt": "streng-geheim"})
        self.assertEqual(antwort.status_code, 200)
        ausgabe = "\n".join(logs.output)
        self.assertNotIn("Hallo", ausgabe)
        self.assertNotIn("streng-geheim", ausgabe)


class TestSprache(AudioAppTestBasis):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for name in ("thorsten", "kerstin", "eva"):
            _stimme_anlegen(self.models_dir, name)

    async def sprich(self, app, **daten):
        koerper = {"input": "Guten Tag.",
                   "model": "piper",
                   "voice": "thorsten",
                   "response_format": "wav"}
        koerper.update(daten)
        return await self.request(app, "POST", "/v1/audio/speech",
                                  json=koerper)

    async def test_defaults_und_wav(self):
        antwort = await self.sprich(self.app())
        self.assertEqual(antwort.status_code, 200)
        self.assertTrue(antwort.headers["content-type"].startswith("audio/wav"))
        self.assertEqual(antwort.content[:4], b"RIFF")
        self.assertIn(self.tts.pcm, antwort.content)
        self.assertEqual(self.tts.aufrufe[0]["text"], "Guten Tag.")
        self.assertEqual(self.tts.aufrufe[0]["info"].id,
                         "de_DE-thorsten-medium")
        self.assertEqual(self.tts.aufrufe[0]["speed"], 1.0)

    async def test_speed_wird_weitergereicht(self):
        antwort = await self.sprich(self.app(), speed=1.5)
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(self.tts.aufrufe[0]["speed"], 1.5)

    async def test_modell_und_stimmen_aliase(self):
        faelle = (("tts-1", "kerstin", "de_DE-kerstin-low"),
                  ("tts-1-hd", "eva", "de_DE-eva_k-x_low"))
        for model, voice, erwartet in faelle:
            with self.subTest(model=model, voice=voice):
                antwort = await self.sprich(
                    self.app(), model=model, voice=voice)
                self.assertEqual(antwort.status_code, 200)
                self.assertEqual(self.tts.aufrufe[-1]["info"].id, erwartet)

    async def test_alle_formate(self):
        for fmt, ctype in (("wav", "audio/wav"), ("pcm", "audio/pcm"),
                           ("mp3", "audio/mpeg"), ("opus", "audio/ogg")):
            with self.subTest(fmt=fmt):
                if fmt == "wav":
                    antwort = await self.sprich(
                        self.app(), response_format=fmt)
                    self.assertEqual(antwort.content[:4], b"RIFF")
                    self.assertIn(self.tts.pcm, antwort.content)
                else:
                    encoder = FakeEncoder(f"enc:{fmt}".encode())
                    antwort = await self.sprich(
                        self.app(encoder_factory=encoder),
                        response_format=fmt)
                    self.assertEqual(antwort.content,
                                     f"enc:{fmt}".encode())
                    self.assertEqual(encoder.prozesse[0].fmt, fmt)
                self.assertEqual(antwort.status_code, 200)
                self.assertTrue(antwort.headers["content-type"]
                                .startswith(ctype))

    async def test_pcm_24k_ohne_encoder(self):
        pcm = b"\x01\x02" * 16
        self.tts = FakeTts(rate=24000, pcm=pcm)

        def nie(*_args):
            raise AssertionError("Encoder darf nicht starten")

        antwort = await self.sprich(
            self.app(encoder_factory=nie), response_format="pcm")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.content, pcm)

    async def test_unbekanntes_modell_und_stimme_404(self):
        for daten in ({"model": "gibtsnicht"}, {"voice": "gibtsnicht"}):
            with self.subTest(daten=daten):
                antwort = await self.sprich(self.app(), **daten)
                self.assertEqual(antwort.status_code, 404)
                self.assertEqual(antwort.json()["error"]["code"],
                                 "model_not_found")

    async def test_nicht_installierte_stimme_503(self):
        self.settings = AudioSettings(
            models_dir=self.models_dir / "leer", preload="")
        antwort = await self.sprich(self.app(), voice="kerstin")
        self.assertEqual(antwort.status_code, 503)
        fehler = antwort.json()["error"]
        self.assertEqual(fehler["code"], "model_not_installed")
        self.assertIn("--voices kerstin", fehler["message"])

    async def test_ungueltige_eingaben_400_ohne_synthese(self):
        faelle = ({"input": ""}, {"input": "   "},
                  {"input": "x" * 5001}, {"speed": 0.4},
                  {"speed": 2.1}, {"response_format": "flac"})
        for daten in faelle:
            with self.subTest(daten=daten):
                self.tts = FakeTts()
                antwort = await self.sprich(self.app(), **daten)
                self.assertEqual(antwort.status_code, 400)
                self.assertEqual(antwort.json()["error"]["code"],
                                 "invalid_request")
                self.assertEqual(self.tts.aufrufe, [])

    async def test_queue_voll_429(self):
        class Voll:
            async def acquire(self):
                raise QueueFull(5)

            def release(self):
                pass

        app = self.app()
        app.state.tts_admission = Voll()
        antwort = await self.sprich(app)
        self.assertEqual(antwort.status_code, 429)
        self.assertEqual(antwort.json()["error"]["code"], "queue_full")
        self.assertEqual(antwort.headers["retry-after"], "5")

    async def test_timeout_vor_dem_antwortstart_504(self):
        self.settings = AudioSettings(
            models_dir=self.models_dir, preload="", tts_timeout_s=0.05)
        self.tts = FakeTts(delay=0.2)
        antwort = await self.sprich(self.app())
        self.assertEqual(antwort.status_code, 504)
        self.assertEqual(antwort.json()["error"]["code"], "timeout")

    async def test_timeout_haelt_slot_bis_synthese_endet(self):
        class Zaehler:
            freigaben = 0

            async def acquire(self):
                pass

            def release(self):
                self.freigaben += 1

        zaehler = Zaehler()
        self.settings = AudioSettings(
            models_dir=self.models_dir, preload="", tts_timeout_s=0.1)
        self.tts = FakeTts(delay=0.4)
        app = self.app()
        app.state.tts_admission = zaehler
        antwort = await self.sprich(app)
        self.assertEqual(antwort.status_code, 504)
        self.assertEqual(zaehler.freigaben, 0)
        await asyncio.sleep(0.5)
        self.assertEqual(zaehler.freigaben, 1)

    async def test_engine_fehler_500_mit_fehlerformat(self):
        class Kaputt(FakeTts):
            def synthesize(self, text, *, info, speed):
                raise RuntimeError("kaputt")
                yield  # pragma: no cover

        self.tts = Kaputt()
        with self.assertLogs("aiserver_audio.app", level="WARNING"):
            antwort = await self.request(
                self.app(), "POST", "/v1/audio/speech",
                json={"input": "Guten Tag.", "voice": "thorsten"},
                raise_app_exceptions=False)
        self.assertEqual(antwort.status_code, 500)
        fehler = antwort.json()["error"]
        self.assertEqual(fehler["code"], "internal_error")
        self.assertEqual(fehler["type"], "server_error")

    async def test_umlaute_und_langer_satz_ohne_punkt(self):
        text = "Grüße aus München: " + "ä" * 3000
        antwort = await self.sprich(self.app(), input=text)
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(self.tts.aufrufe[0]["text"], text)

    async def test_text_steht_nicht_im_log(self):
        with self.assertLogs("aiserver_audio.app", level="INFO") as logs:
            antwort = await self.sprich(self.app(), input="streng-geheim")
        self.assertEqual(antwort.status_code, 200)
        self.assertNotIn("streng-geheim", "\n".join(logs.output))


class TestDisconnect(unittest.IsolatedAsyncioTestCase):
    async def test_http_disconnect_beendet_encoder_und_gibt_frei(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            _stimme_anlegen(models, "thorsten")
            settings = AudioSettings(models_dir=models, preload="")
            tts = FakeTts(delay=0.3)
            encoder = FakeEncoder(b"x")

            class Zaehler:
                def __init__(self):
                    self.freigaben = 0

                async def acquire(self):
                    pass

                def release(self):
                    self.freigaben += 1

            admission = Zaehler()
            app = create_audio_app(
                settings, stt_engine=FakeStt(), tts_engine=tts,
                duration_probe=lambda _: 2.5,
                encoder_factory=encoder)
            app.state.tts_admission = admission

            koerper = json.dumps({
                "input": "Erster Satz. Zweiter Satz.",
                "voice": "thorsten", "response_format": "mp3"}).encode()
            scope = {
                "type": "http", "asgi": {"version": "3.0",
                                         "spec_version": "2.3"},
                "http_version": "1.1", "method": "POST", "scheme": "http",
                "path": "/v1/audio/speech", "raw_path": b"/v1/audio/speech",
                "query_string": b"", "root_path": "",
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(koerper)).encode())],
                "client": ("test", 1), "server": ("test", 80),
            }
            erste_ausgabe = asyncio.Event()
            anfrage_gesendet = False

            async def receive():
                nonlocal anfrage_gesendet
                if not anfrage_gesendet:
                    anfrage_gesendet = True
                    return {"type": "http.request", "body": koerper}
                await erste_ausgabe.wait()
                return {"type": "http.disconnect"}

            async def send(nachricht):
                if (nachricht["type"] == "http.response.body"
                        and nachricht.get("body")):
                    erste_ausgabe.set()

            await app(scope, receive, send)
            self.assertTrue(encoder.prozesse[0].terminated)
            self.assertTrue(encoder.prozesse[0].stdin.closed)
            self.assertEqual(admission.freigaben, 1)
            await asyncio.sleep(0.4)
            self.assertEqual(tts.offen, 0)


class TestStreaming(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.info = resolve_voice("thorsten")

    async def test_erster_chunk_vor_zweitem_satz(self):
        events = []
        engine = FakeTts(events=events)
        agen = stream_speech(
            engine, "Erster Satz. Zweiter Satz.", info=self.info,
            fmt="wav", speed=1.0, timeout_s=5)
        header = await agen.__anext__()
        self.assertEqual(header[:4], b"RIFF")
        starts = [e for e in events if e[0] == "start"]
        self.assertEqual(starts, [("start", "Erster Satz.")])
        await agen.__anext__()  # erster PCM-Block
        self.assertEqual([e for e in events if e[0] == "start"], starts)
        await agen.__anext__()  # jetzt beginnt der zweite Satz
        starts = [e for e in events if e[0] == "start"]
        self.assertEqual(starts, [("start", "Erster Satz."),
                                  ("start", "Zweiter Satz.")])
        await agen.aclose()

    async def test_disconnect_raeumt_encoder_und_engine_auf(self):
        engine = FakeTts()
        encoder = FakeEncoder(b"x")
        agen = stream_speech(
            engine, "Erster Satz. Zweiter Satz.", info=self.info,
            fmt="mp3", speed=1.0, timeout_s=5,
            encoder_factory=encoder)
        await agen.__anext__()
        await agen.aclose()
        self.assertTrue(encoder.prozesse[0].terminated)
        self.assertTrue(encoder.prozesse[0].stdin.closed)
        self.assertEqual(engine.offen, 0)

    async def test_timeout_wirft_audio_fehler(self):
        engine = FakeTts(delay=0.2)
        agen = stream_speech(
            engine, "Erster Satz.", info=self.info, fmt="wav",
            speed=1.0, timeout_s=0.05)
        with self.assertRaises(AudioError) as ctx:
            await agen.__anext__()
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertEqual(ctx.exception.code, "timeout")
        await agen.aclose()


class TestFormatierung(unittest.TestCase):
    def test_srt_und_vtt(self):
        t = Transcription("Hallo", "de", 2.5,
                          (Segment(0.0, 1.0, "Hallo"),
                           Segment(1.0, 2.5, "Welt")))
        srt = format_transcript(t, "srt")
        self.assertIn("1\n00:00:00,000 --> 00:00:01,000\nHallo", srt)
        vtt = format_transcript(t, "vtt")
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:01.000 --> 00:00:02.500", vtt)

    def test_unbekanntes_format(self):
        t = Transcription("Hallo", "de", 1.0, ())
        with self.assertRaises(ValueError):
            format_transcript(t, "flac")
