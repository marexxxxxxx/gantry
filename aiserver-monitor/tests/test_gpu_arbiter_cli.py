"""Tests fuer die gantry-gpu CLI (argparse-Parsing, ohne Netz)."""

import unittest

from aiserver_monitor import gpu_arbiter_cli


class TestParse(unittest.TestCase):
    def test_parse_lease_run_ohne_optionen(self):
        args = gpu_arbiter_cli.parse(["lease", "run", "--", "echo", "hi"])
        self.assertEqual(args.lease_action, "run")
        self.assertEqual(args.command, ["echo", "hi"])
        self.assertIsNone(args.timeout_s)
        self.assertIsNone(args.vram_budget_mib)

    def test_parse_lease_run_mit_timeout_und_vram(self):
        args = gpu_arbiter_cli.parse(
            ["lease", "run", "--timeout", "30", "--vram", "4096",
             "--", "blender", "-b", "scene.blend"]
        )
        self.assertEqual(args.timeout_s, 30.0)
        self.assertEqual(args.vram_budget_mib, 4096)
        self.assertEqual(args.command, ["blender", "-b", "scene.blend"])

    def test_parse_lease_acquire(self):
        args = gpu_arbiter_cli.parse(["lease", "acquire"])
        self.assertEqual(args.lease_action, "acquire")

    def test_parse_lease_release_mit_id(self):
        args = gpu_arbiter_cli.parse(["lease", "release", "lease-3"])
        self.assertEqual(args.lease_id, "lease-3")

    def test_parse_state(self):
        args = gpu_arbiter_cli.parse(["state"])
        self.assertEqual(args.subcommand, "state")


if __name__ == "__main__":
    unittest.main()
