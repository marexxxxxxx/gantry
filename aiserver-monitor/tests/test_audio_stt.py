import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiserver_audio.catalog import resolve_whisper
from aiserver_audio.config import AudioSettings
from aiserver_audio.ffmpeg import probe_duration
from aiserver_audio.stt import FasterWhisperEngine


class FakeWhisperModel:
    loads = []
    calls = []
    transcribe_started = None
    transcribe_release = None

    def __init__(self, name, **kwargs):
        FakeWhisperModel.loads.append((name, kwargs))
        time.sleep(0.02)

    def transcribe(self, path, **kwargs):
        FakeWhisperModel.calls.append((path, kwargs))
        if FakeWhisperModel.transcribe_started is not None:
            FakeWhisperModel.transcribe_started.set()
            FakeWhisperModel.transcribe_release.wait(timeout=2)
        segmente = [
            SimpleNamespace(start=0.0, end=1.0, text=" Hallo"),
            SimpleNamespace(start=1.0, end=2.5, text=" Welt "),
        ]
        info = SimpleNamespace(language="de", duration=2.5)
        return iter(segmente), info


def fake_modul():
    return SimpleNamespace(WhisperModel=FakeWhisperModel)


class TestFasterWhisperEngine(unittest.TestCase):
    def setUp(self):
        FakeWhisperModel.loads = []
        FakeWhisperModel.calls = []
        FakeWhisperModel.transcribe_started = None
        FakeWhisperModel.transcribe_release = None
        self.settings = AudioSettings(model_cache=1, threads=8)
        self.small = resolve_whisper("small")
        self.medium = resolve_whisper("medium")

    def test_preload_laedt_einmal(self):
        engine = FasterWhisperEngine(self.settings)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_modul()}):
            engine.preload(self.small)
            engine.preload(self.small)
        self.assertEqual(len(FakeWhisperModel.loads), 1)
        name, kwargs = FakeWhisperModel.loads[0]
        self.assertEqual(name, str(Path("/mnt/models/audio/whisper/small")))
        self.assertEqual(kwargs["device"], "cpu")
        self.assertEqual(kwargs["compute_type"], "int8")
        self.assertEqual(kwargs["cpu_threads"], 8)
        self.assertEqual(kwargs["num_workers"], 1)

    def test_cache_eins_verdraengt(self):
        engine = FasterWhisperEngine(self.settings)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_modul()}):
            engine.preload(self.small)
            engine.preload(self.medium)
        self.assertEqual(list(engine._cache), ["whisper-medium"])

    def test_cache_zwei_behaelt_beide(self):
        engine = FasterWhisperEngine(AudioSettings(model_cache=2))
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_modul()}):
            engine.preload(self.small)
            engine.preload(self.medium)
        self.assertEqual(set(engine._cache),
                         {"whisper-small", "whisper-medium"})

    def test_gleichzeitiges_preload_laedt_nur_einmal(self):
        engine = FasterWhisperEngine(self.settings)
        barrier = threading.Barrier(3)

        def laden():
            barrier.wait()
            engine.preload(self.small)

        with mock.patch.dict(sys.modules, {"faster_whisper": fake_modul()}):
            threads = [threading.Thread(target=laden) for _ in range(2)]
            for t in threads:
                t.start()
            barrier.wait()
            for t in threads:
                t.join(timeout=2)
        self.assertEqual(len(FakeWhisperModel.loads), 1)

    def test_transcribe_reicht_parameter_durch(self):
        engine = FasterWhisperEngine(self.settings)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_modul()}):
            result = engine.transcribe(
                Path("/tmp/a.wav"), info=self.small, language="de",
                prompt="Tisch reservieren", temperature=0.2)
        self.assertEqual(result.text, "Hallo Welt")
        self.assertEqual(result.language, "de")
        self.assertEqual(result.duration, 2.5)
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[0].start, 0.0)
        self.assertEqual(result.segments[1].end, 2.5)
        path, kwargs = FakeWhisperModel.calls[0]
        self.assertEqual(path, "/tmp/a.wav")
        self.assertEqual(kwargs["language"], "de")
        self.assertEqual(kwargs["task"], "transcribe")
        self.assertEqual(kwargs["beam_size"], 1)
        self.assertIs(kwargs["vad_filter"], True)
        self.assertIs(kwargs["condition_on_previous_text"], False)
        self.assertEqual(kwargs["initial_prompt"], "Tisch reservieren")
        self.assertEqual(kwargs["temperature"], 0.2)

    def test_laufendes_modell_wird_nicht_evictet(self):
        engine = FasterWhisperEngine(self.settings)
        FakeWhisperModel.transcribe_started = threading.Event()
        FakeWhisperModel.transcribe_release = threading.Event()

        with mock.patch.dict(sys.modules, {"faster_whisper": fake_modul()}):
            lauf = threading.Thread(target=lambda: engine.transcribe(
                Path("/tmp/a.wav"), info=self.small, language="de",
                prompt=None, temperature=0.0))
            lauf.start()
            self.assertTrue(FakeWhisperModel.transcribe_started.wait(timeout=1))

            wechsel = threading.Thread(target=lambda: engine.preload(self.medium))
            wechsel.start()
            time.sleep(0.05)
            self.assertTrue(wechsel.is_alive())
            self.assertEqual(list(engine._cache), ["whisper-small"])

            FakeWhisperModel.transcribe_release.set()
            lauf.join(timeout=2)
            wechsel.join(timeout=2)
        self.assertEqual(list(engine._cache), ["whisper-medium"])


class TestProbeDuration(unittest.TestCase):
    def test_gueltige_dauer(self):
        def runner(cmd, **kwargs):
            self.assertIn("-show_entries", cmd)
            self.assertIn("format=duration", cmd)
            return SimpleNamespace(returncode=0, stdout="2.500000\n", stderr="")

        self.assertEqual(probe_duration(Path("/tmp/a.wav"), runner=runner), 2.5)

    def test_fehler_und_ungueltige_ausgabe(self):
        for ergebnis in (
            SimpleNamespace(returncode=1, stdout="2.5", stderr="kaputt"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="nan\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="keine zahl", stderr=""),
        ):
            with self.subTest(ergebnis=ergebnis):
                self.assertIsNone(probe_duration(
                    Path("/tmp/a.wav"), runner=lambda *a, **k: ergebnis))

    def test_runner_ausnahme(self):
        def runner(*args, **kwargs):
            raise OSError("ffprobe fehlt")

        self.assertIsNone(probe_duration(Path("/tmp/a.wav"), runner=runner))
