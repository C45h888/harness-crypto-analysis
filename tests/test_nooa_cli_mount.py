"""Tests for the repo-level NOOA CLI mount and the harness ``--nooa`` passthrough.

The mount is the canonical harness seam: instead of writing shim modules into
the installed ``nooa_cli/commands/`` package (site-packages mutation), the
``market`` harness group is attached to the framework root ``oo`` group at
import time from this repository. These tests keep the seam honest — the
mount must register exactly the harness surface, must not write any files
into the framework package, and the harness passthrough must forward its
argv verbatim to the mounted CLI.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

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
        names = set(_MOUNTED.commands["market"].commands)
        self.assertEqual(
            names, {"envelope", "briefing", "memory", "analyst", "microstructure"}
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


class HarnessNooaPassthroughTests(unittest.TestCase):
    def test_parser_captures_nooa_args(self):
        p = harness.build_parser()
        args = p.parse_args(
            ["SOLUSDT", "--nooa", "market", "envelope", "SOLUSDT", "--latest"]
        )
        self.assertEqual(args.nooa, ["market", "envelope", "SOLUSDT", "--latest"])

    def test_parser_remainder_captures_flags_without_separator(self):
        p = harness.build_parser()
        args = p.parse_args(["--nooa", "market", "--help"])
        self.assertEqual(args.nooa, ["market", "--help"])

    @patch("market_service.commands.nooa_cli.main", return_value=0)
    def test_main_delegates_to_mounted_cli(self, mock_main):
        rc = harness.main(["--nooa", "market", "--help"])
        mock_main.assert_called_once_with(["market", "--help"])
        self.assertEqual(rc, 0)

    @patch("market_service.commands.nooa_cli.main", return_value=0)
    def test_main_forwards_verbatim_without_double_dash(self, mock_main):
        rc = harness.main(["--nooa", "market", "refresh", "SOLUSDT", "--all"])
        mock_main.assert_called_once_with(["market", "refresh", "SOLUSDT", "--all"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()