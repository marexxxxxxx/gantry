"""Tests fuer Piper-Engine und Satz-Splitting."""

import json
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiserver_audio.catalog import piper_files, resolve_voice
from aiserver_audio.config import AudioSettings
from aiserver_audio.tts import AudioChunk, PiperEngine, split_sentences


class FakeAudioChunk:
    def __init__(self, pcm: bytes = b"\x01\x00", sample_rate: int = 22050):
        self.audio_int16_bytes = pcm
        self.sample_rate = sample_rate


class FakeSynthesisConfig:
    def __init__(self, length_scale=None, **kwargs):
        self.length_scale = length_scale
        self.kwargs = kwargs


class FakePiperVoice:
    erzeugt: list["FakePiperVoice"] = []
    benutzt: list["FakePiperVoice"] = []
    barrier: threading.Barrier | None = None

    def __init__(self, session, config):
        self.session = session
        self.config = config
        self.calls: list[tuple[str, FakeSynthesisConfig | None]] = []
        FakePiperVoice.erzeugt.append(self)

    def synthesize(self, text, syn_config=None):
        self.calls.append((text, syn_config))
        FakePiperVoice.benutzt.append(self)
        if FakePiperVoice.barrier is not None:
            FakePiperVoice.barrier.wait(timeout=5)
        yield FakeAudioChunk()


class FakePiperConfig:
    @staticmethod
    def from_dict(config):
        return {"config": config}


class FakeSessionOptions:
    def __init__(self):
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0


class FakeInferenceSession:
    erzeugt: list["FakeInferenceSession"] = []

    def __init__(self, path, sess_options=None, **kwargs):
        self.path = path
        self.sess_options = sess_options
        FakeInferenceSession.erzeugt.append(self)


def _fake_module(name: str, **attrs) -> types.ModuleType:
    modul = types.ModuleType(name)
    for schluessel, wert in attrs.items():
        setattr(modul, schluessel, wert)
    return modul


class PiperTestBasis(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.models_dir = Path(self._tmp.name)
        for name in ("thorsten", "kerstin"):
            info = resolve_voice(name)
            onnx, config = piper_files(self.models_dir, info)
            onnx.parent.mkdir(parents=True, exist_ok=True)
            onnx.write_bytes(b"modell")
            config.write_text(json.dumps({"sample_rate": info.sample_rate}))
        self.settings = AudioSettings(models_dir=self.models_dir)
        FakePiperVoice.erzeugt = []
        FakePiperVoice.benutzt = []
        FakePiperVoice.barrier = None
        FakeInferenceSession.erzeugt = []
        self._module = {
            "piper": _fake_module("piper", voice=None, config=None),
            "piper.voice": _fake_module(
                "piper.voice", PiperVoice=FakePiperVoice,
                SynthesisConfig=FakeSynthesisConfig,
                AudioChunk=FakeAudioChunk),
            "piper.config": _fake_module(
                "piper.config", PiperConfig=FakePiperConfig),
            "onnxruntime": _fake_module(
                "onnxruntime", SessionOptions=FakeSessionOptions,
                InferenceSession=FakeInferenceSession),
        }
        self._alt = {}
        for name, modul in self._module.items():
            self._alt[name] = sys.modules.get(name)
            sys.modules[name] = modul

    def tearDown(self):
        for name, alt in self._alt.items():
            if alt is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = alt
        self._tmp.cleanup()


class TestSatzSplitting(unittest.TestCase):
    def test_drei_saetze_mit_satzzeichen(self):
        self.assertEqual(
            split_sentences("Hallo. Wie geht es dir? Gut!"),
            ["Hallo.", "Wie geht es dir?", "Gut!"])

    def test_satz_ohne_punkt_und_umlaute_bleiben(self):
        self.assertEqual(
            split_sentences("Grüße aus München ohne Punkt"),
            ["Grüße aus München ohne Punkt"])

    def test_leerer_text(self):
        self.assertEqual(split_sentences("  "), [])
        self.assertEqual(split_sentences(""), [])


class TestPiperEngine(PiperTestBasis):
    def test_synthesize_laedt_einmal_und_gibt_chunks_weiter(self):
        engine = PiperEngine(self.settings)
        info = resolve_voice("thorsten")

        erste = list(engine.synthesize("Hallo.", info=info, speed=2.0))
        zweite = list(engine.synthesize("Nochmal.", info=info, speed=1.0))

        self.assertEqual([c.pcm for c in erste], [b"\x01\x00"])
        self.assertEqual(erste[0].sample_rate, 22050)
        self.assertEqual(len(FakePiperVoice.erzeugt), 2)
        self.assertEqual([c.pcm for c in zweite], [b"\x01\x00"])
        self.assertEqual(len(FakePiperVoice.erzeugt), 2)
        stimme = FakePiperVoice.benutzt[0]
        self.assertEqual(stimme.calls[0][1].length_scale, 0.5)
        self.assertEqual(stimme.calls[1][1].length_scale, 1.0)

    def test_stimmenwechsel_laedt_neue_stimme(self):
        engine = PiperEngine(self.settings)
        list(engine.synthesize("Hallo.", info=resolve_voice("thorsten"),
                               speed=1.0))
        list(engine.synthesize("Hallo.", info=resolve_voice("kerstin"),
                               speed=1.0))

        self.assertEqual(len(FakePiperVoice.erzeugt), 4)
        self.assertTrue(any("kerstin" in str(v.session.path)
                            for v in FakePiperVoice.erzeugt))

    def test_parallele_synthesen_nutzen_getrennte_instanzen(self):
        engine = PiperEngine(self.settings)
        info = resolve_voice("thorsten")
        FakePiperVoice.barrier = threading.Barrier(2)

        def lauf(text):
            return list(engine.synthesize(text, info=info, speed=1.0))

        with ThreadPoolExecutor(max_workers=2) as pool:
            ergebnisse = list(pool.map(lauf, ("Eins.", "Zwei.")))

        self.assertEqual(len(ergebnisse), 2)
        self.assertEqual(len({id(v) for v in FakePiperVoice.benutzt}), 2)
        for stimme in FakePiperVoice.benutzt:
            self.assertEqual(stimme.session.sess_options.intra_op_num_threads,
                             2)
            self.assertEqual(stimme.session.sess_options.inter_op_num_threads,
                             1)

    def test_langer_satz_ohne_punkt_ist_ein_aufruf(self):
        engine = PiperEngine(self.settings)
        text = "ein sehr langer satz ohne punkt " * 20
        list(engine.synthesize(text, info=resolve_voice("thorsten"),
                               speed=1.0))
        self.assertEqual(len(FakePiperVoice.benutzt[0].calls), 1)
        self.assertEqual(FakePiperVoice.benutzt[0].calls[0][0], text)


if __name__ == "__main__":
    unittest.main()
