"""Tests fuer die Audio-FastAPI-App (Health, Modelle, Transkription)."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from aiserver_audio.app import create_audio_app, format_transcript
from aiserver_audio.config import AudioSettings
from aiserver_audio.queue import QueueFull
from aiserver_audio.stt import Segment, Transcription


def _modelle_anlegen(models_dir: Path, modell: str = "small") -> None:
    ziel = models_dir / "whisper" / modell
    ziel.mkdir(parents=True, exist_ok=True)
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (ziel / name).write_bytes(b"x")


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
            text="Hallo", language=language, duration=2.5,
            segments=(Segment(0.0, 1.0, "Hallo"),
                      Segment(1.0, 2.5, "Welt")))


class FakeTts:
    pass


class AudioAppTestBasis(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.models_dir = Path(self._tmp.name)
        self.stt = FakeStt()
        self.settings = AudioSettings(models_dir=self.models_dir,
                                      preload="")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def app(self, **kwargs):
        return create_audio_app(
            self.settings, stt_engine=self.stt, tts_engine=FakeTts(),
            duration_probe=kwargs.pop("duration_probe", lambda _: 2.5),
            **kwargs)

    async def request(self, app, methode, pfad, **kwargs):
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app),
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
        self.assertEqual(self.stt.aufrufe[0]["language"], "auto")

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

    async def test_keine_transkripte_oder_prompts_im_log(self):
        with self.assertLogs("aiserver_audio.app", level="INFO") as logs:
            antwort = await self.transkription(
                self.app(), daten={"prompt": "streng-geheim"})
        self.assertEqual(antwort.status_code, 200)
        ausgabe = "\n".join(logs.output)
        self.assertNotIn("Hallo", ausgabe)
        self.assertNotIn("streng-geheim", ausgabe)


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
