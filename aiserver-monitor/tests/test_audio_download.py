import io
import os
import tempfile
import unittest
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from aiserver_audio.catalog import (
    PIPER_VOICES,
    WHISPER_MODELS,
    piper_files,
    whisper_dir,
)
from aiserver_audio.download import Selection, download, main

_GIB = 1024 ** 3
_Disk = namedtuple("_Disk", "total used free")


def _disk(free=_GIB * 10):
    return _Disk(total=_GIB * 100, used=_GIB * 90, free=free)


def _install_whisper(base, info):
    ziel = whisper_dir(base, info)
    ziel.mkdir(parents=True, exist_ok=True)
    for name in info.required_files:
        (ziel / name).write_bytes(b"x")


def _install_voice(base, info):
    onnx, cfg = piper_files(base, info)
    onnx.parent.mkdir(parents=True, exist_ok=True)
    onnx.write_bytes(b"x")
    cfg.write_bytes(b"{}")


def _snapshot(calls, *, create=True, fehler=None):
    def fake(repo_id, local_dir, allow_patterns):
        calls.append({"repo_id": repo_id,
                      "local_dir": Path(local_dir),
                      "allow_patterns": allow_patterns})
        if fehler is not None:
            raise fehler
        if not create:
            return str(local_dir)
        if create:
            for info in WHISPER_MODELS:
                if info.repo == repo_id:
                    ziel = Path(local_dir)
                    ziel.mkdir(parents=True, exist_ok=True)
                    for name in info.required_files:
                        (ziel / name).write_bytes(b"x")
            for info in PIPER_VOICES:
                if info.repo == repo_id and any(
                        info.repo_path in str(p) for p in allow_patterns):
                    ziel = Path(local_dir) / info.repo_path
                    ziel.mkdir(parents=True, exist_ok=True)
                    (ziel / f"{info.id}.onnx").write_bytes(b"x")
                    (ziel / f"{info.id}.onnx.json").write_bytes(b"{}")
        return str(local_dir)
    return fake


class TestCheck(unittest.TestCase):
    def test_check_meldet_fehlende_und_installierte(self):
        with tempfile.TemporaryDirectory() as tmp:
            basis = Path(tmp)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = download(Selection(), basis, check=True,
                              snapshot=mock.Mock(),
                              disk_usage=lambda _: _disk())
            self.assertEqual(rc, 1)
            text = out.getvalue()
            for info in WHISPER_MODELS:
                self.assertIn(info.id, text)
            for info in PIPER_VOICES:
                self.assertIn(info.id, text)
            self.assertIn("missing", text)

            for info in WHISPER_MODELS:
                _install_whisper(basis, info)
            for info in PIPER_VOICES:
                _install_voice(basis, info)

            out = io.StringIO()
            with redirect_stdout(out):
                rc = download(Selection(), basis, check=True,
                              snapshot=mock.Mock(),
                              disk_usage=lambda _: _disk())
            self.assertEqual(rc, 0)
            self.assertIn("installed", out.getvalue())

    def test_check_laedt_nichts_und_braucht_keinen_platz(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            rc = download(Selection(), Path(tmp), check=True,
                          snapshot=_snapshot(calls),
                          disk_usage=lambda _: _disk(free=0))
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [])


class TestDownload(unittest.TestCase):
    def test_whisper_laedt_nur_auswahl(self):
        with tempfile.TemporaryDirectory() as tmp:
            basis = Path(tmp)
            calls = []
            rc = download(Selection(whisper=("small",)), basis,
                          snapshot=_snapshot(calls),
                          disk_usage=lambda _: _disk())
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["repo_id"],
                             "Systran/faster-whisper-small")
            self.assertEqual(calls[0]["local_dir"], basis / "whisper" / "small")
            self.assertTrue(calls[0]["allow_patterns"])
            self.assertTrue((basis / "whisper" / "small" / "model.bin").is_file())
            self.assertFalse((basis / "whisper" / "medium").exists())

    def test_piper_landet_flach(self):
        with tempfile.TemporaryDirectory() as tmp:
            basis = Path(tmp)
            calls = []
            rc = download(Selection(voices=("kerstin",)), basis,
                          snapshot=_snapshot(calls),
                          disk_usage=lambda _: _disk())
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["repo_id"], "rhasspy/piper-voices")
            onnx, cfg = piper_files(
                basis, next(v for v in PIPER_VOICES if v.id ==
                            "de_DE-kerstin-low"))
            self.assertTrue(onnx.is_file())
            self.assertTrue(cfg.is_file())
            self.assertEqual(onnx.parent, basis / "piper")
            self.assertEqual({p.name for p in onnx.parent.iterdir()},
                             {onnx.name, cfg.name})

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            basis = Path(tmp)
            small = next(m for m in WHISPER_MODELS if m.id == "whisper-small")
            thorsten = next(v for v in PIPER_VOICES
                            if v.id == "de_DE-thorsten-medium")
            _install_whisper(basis, small)
            _install_voice(basis, thorsten)
            calls = []
            rc = download(Selection(whisper=("small",),
                                    voices=("thorsten",)), basis,
                          snapshot=_snapshot(calls),
                          disk_usage=lambda _: _disk())
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [])

    def test_zu_wenig_platz(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            err = io.StringIO()
            with redirect_stderr(err):
                rc = download(Selection(whisper=("small",)), Path(tmp),
                              snapshot=_snapshot(calls),
                              disk_usage=lambda _: _disk(free=4 * _GIB))
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [])
            self.assertIn("5", err.getvalue())

    def test_fehlende_datei_nach_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            err = io.StringIO()
            with redirect_stderr(err):
                rc = download(Selection(whisper=("small",)), Path(tmp),
                              snapshot=_snapshot(calls, create=False),
                              disk_usage=lambda _: _disk())
            self.assertEqual(rc, 1)
            self.assertIn("fehlt", err.getvalue())

    def test_snapshot_fehler(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stderr(err):
                rc = download(Selection(whisper=("small",)), Path(tmp),
                              snapshot=_snapshot([], fehler=RuntimeError("boom")),
                              disk_usage=lambda _: _disk())
            self.assertEqual(rc, 1)
            self.assertIn("boom", err.getvalue())


class TestCli(unittest.TestCase):
    def test_auswahl_ueber_argumente(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            with mock.patch.dict(os.environ,
                                 {"AUDIO_MODELS_DIR": tmp}):
                with mock.patch("aiserver_audio.download.snapshot_download",
                                side_effect=_snapshot(calls)), \
                        mock.patch("aiserver_audio.download.disk_usage",
                                   return_value=_disk()):
                    rc = main(["--whisper", "small,medium",
                               "--voices", "thorsten,eva"])
            self.assertEqual(rc, 0)
            self.assertEqual(
                sorted(c["repo_id"] for c in calls),
                ["Systran/faster-whisper-medium",
                 "Systran/faster-whisper-small",
                 "rhasspy/piper-voices",
                 "rhasspy/piper-voices"])

    def test_ohne_argumente_alles(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            with mock.patch.dict(os.environ,
                                 {"AUDIO_MODELS_DIR": tmp}):
                with mock.patch("aiserver_audio.download.snapshot_download",
                                side_effect=_snapshot(calls)), \
                        mock.patch("aiserver_audio.download.disk_usage",
                                   return_value=_disk()):
                    rc = main([])
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 6)

    def test_unbekannte_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with mock.patch.dict(os.environ,
                                 {"AUDIO_MODELS_DIR": tmp}):
                with redirect_stderr(err):
                    rc = main(["--whisper", "gigantisch"])
            self.assertEqual(rc, 2)
            self.assertIn("gigantisch", err.getvalue())


if __name__ == "__main__":
    unittest.main()
