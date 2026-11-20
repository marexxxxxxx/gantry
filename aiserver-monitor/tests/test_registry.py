"""Tests for the data-driven provider registry and its CLI."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from aiserver_monitor import provider_cli
from aiserver_monitor.registry import (
    Provider,
    RegistryError,
    delete_provider,
    load_provider,
    load_registry,
    rename_provider,
    save_provider,
)


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip_keeps_fields_and_settings(self) -> None:
        provider = Provider(
            id="demo",
            name="Demo (lokal)",
            kind="ollama",
            port=11434,
            model="demo:latest",
            start_cmd=["echo", "start"],
            stop_cmd=["echo", "stop"],
            settings={"context": 8192, "auto_load": True, "custom": "x"},
        )
        path = save_provider(provider, self.dir)
        self.assertTrue(path.exists())
        loaded = load_provider(path)
        self.assertEqual(loaded.id, "demo")
        self.assertEqual(loaded.name, "Demo (lokal)")
        self.assertEqual(loaded.kind, "ollama")
        self.assertEqual(loaded.port, 11434)
        self.assertEqual(loaded.start_cmd, ["echo", "start"])
        self.assertEqual(loaded.settings["context"], 8192)
        self.assertIs(loaded.settings["auto_load"], True)
        self.assertEqual(loaded.settings["custom"], "x")

    def test_defaults_are_filled(self) -> None:
        save_provider(Provider(id="plain", name="Plain"), self.dir)
        loaded = load_provider(self.dir / "plain.toml")
        self.assertEqual(loaded.kind, "custom")
        self.assertEqual(loaded.host, "127.0.0.1")
        self.assertEqual(loaded.settings["parallel"], 1)
        self.assertIs(loaded.settings["auto_load"], True)

    def test_validation_rejects_bad_values(self) -> None:
        with self.assertRaises(RegistryError):
            Provider(id="Bad ID", name="X")
        with self.assertRaises(RegistryError):
            Provider(id="ok", name="X", kind="unknown")
        with self.assertRaises(RegistryError):
            Provider(id="ok", name="X", port=70000)
        with self.assertRaises(RegistryError):
            Provider(id="ok", name="  ")

    def test_load_registry_returns_all(self) -> None:
        save_provider(Provider(id="a", name="A"), self.dir)
        save_provider(Provider(id="b", name="B"), self.dir)
        providers = load_registry(self.dir)
        self.assertEqual(sorted(providers), ["a", "b"])

    def test_rename_and_delete(self) -> None:
        save_provider(Provider(id="old", name="Old"), self.dir)
        renamed = rename_provider("old", "new", self.dir)
        self.assertEqual(renamed.id, "new")
        self.assertFalse((self.dir / "old.toml").exists())
        self.assertTrue((self.dir / "new.toml").exists())
        self.assertTrue(delete_provider("new", self.dir))
        self.assertFalse(delete_provider("new", self.dir))


class ProviderCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"GANTRY_PROVIDERS_DIR": str(self.dir)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = provider_cli.main(list(argv))
        return rc, out.getvalue()

    def test_add_edit_rename_delete(self) -> None:
        rc, _ = self.run_cli(
            "add", "p1", "--name", "P1", "--kind", "custom", "--port", "9999",
            "--context", "4096", "--model", "m1",
        )
        self.assertEqual(rc, 0)
        provider = load_provider(self.dir / "p1.toml")
        self.assertEqual(provider.settings["context"], 4096)

        rc, out = self.run_cli("list")
        self.assertEqual(rc, 0)
        self.assertIn("p1", out)
        self.assertIn("4096", out)

        rc, _ = self.run_cli("edit", "p1", "--context", "8192", "--no-auto-load")
        self.assertEqual(rc, 0)
        provider = load_provider(self.dir / "p1.toml")
        self.assertEqual(provider.settings["context"], 8192)
        self.assertIs(provider.settings["auto_load"], False)

        rc, _ = self.run_cli("rename", "p1", "p2")
        self.assertEqual(rc, 0)
        self.assertTrue((self.dir / "p2.toml").exists())

        rc, _ = self.run_cli("rm", "p2", "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual(load_registry(self.dir), {})

    def test_show_json(self) -> None:
        self.run_cli("add", "p1", "--name", "P1", "--port", "1234")
        rc, out = self.run_cli("show", "p1", "--json")
        self.assertEqual(rc, 0)
        self.assertIn('"id": "p1"', out)
        self.assertIn('"port": 1234', out)


if __name__ == "__main__":
    unittest.main()
