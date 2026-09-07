"""Tests for the repo-level NOOA CLI mount.

The mount attaches the ``market`` harness group to the framework root ``oo``
group at import time from this repository (instead of writing shim modules
into the installed ``nooa_cli/commands/`` package). These tests keep the seam
honest — the mount must register exactly the harness surface and must not
write any files into the framework package.

The former harness ``--nooa`` passthrough was removed as legacy debt; the
inner CLI is invoked directly, never through harness.py.
"""

from __future__ import annotations

import unittest

import click

from market_service.commands import harness
from market_service.commands.nooa_cli_ext import command as market_group

try:  # nooa-cli present (dev venv / docker image)
    from market_service.commands.nooa_cli import make_cli, main as mount_main

    _MOUNTED = make_cli()
except Exception as exc:  # pragma: no cover - framework absent
    _MOUNTED = None
    _MOUNT_IMPORT_ERROR = exc
else:
    _MOUNT_IMPORT_ERROR = None


@unittest.skipUnless(_MOUNTED is not None, "nooa-cli not installed; mount tests skipped")
class NooaCliMountTests(unittest.TestCase):
    def test_market_group_is_registered_on_root(self):
        self.assertIn("market", _MOUNTED.commands)

    def test_market_group_has_harness_subcommands(self):
        # The envelope dataclass read path was retired 2026-08-30 in favour
        # of ``market read`` (runtime.read_paths): the surface is now read /
        # memory / microstructure / inference.
        names = set(_MOUNTED.commands["market"].commands)
        self.assertEqual(
            names,
            {"read", "memory", "microstructure", "inference"},
        )

    def test_mount_does_not_write_into_framework_package(self):
        # The mount must be import-time attaching, never file installation.
        import pathlib

        import nooa_cli.commands as _commands_pkg

        shim = pathlib.Path(_commands_pkg.__file__).parent / "market.py"
        self.assertFalse(shim.exists(), "a market.py shim was written into nooa_cli")

    def test_market_group_is_a_click_group(self):
        self.assertIsInstance(market_group, click.Group)
        self.assertEqual(market_group.name, "market")


class HarnessNooaPassthroughRemovedTests(unittest.TestCase):
    """The --nooa router was removed as legacy debt — the outer CLI must
    not accept or forward it again."""

    def test_parser_rejects_nooa_flag(self):
        p = harness.build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["SOLUSDT", "--nooa", "market", "envelope"])

    def test_main_rejects_nooa_flag(self):
        with self.assertRaises(SystemExit):
            harness.main(["--nooa", "market", "--help"])


if __name__ == "__main__":
    unittest.main()