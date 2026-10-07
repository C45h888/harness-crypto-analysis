"""Interaction-plane contract tests — shapes, budgets, prompts, manifest.

No live Redis/Postgres/Binance: segment reads are mocked at the
``interaction_plane.reads`` seam; parsing/prompts/manifest are pure.
"""

from __future__ import annotations

import unittest

from market_service.interaction_plane import (
    budgets,
    parsing,
    prompts,
    segments,
)
from market_service.interaction_plane.manifest import describe


class SegmentTableTests(unittest.TestCase):
    def test_every_tool_belongs_to_a_segment(self):
        self.assertIn("warm", segments.SEGMENTS)
        self.assertIn("ledger", segments.SEGMENTS)
        self.assertIn("micro", segments.SEGMENTS)
        self.assertIn("history", segments.SEGMENTS)
        self.assertIn("inference", segments.SEGMENTS)
        self.assertIn("control", segments.SEGMENTS)

    def test_full_is_human_only(self):
        self.assertIn("full", segments.HUMAN_ONLY_MODES)

    def test_all_tools_listed_once(self):
        tools = segments.all_tool_names()
        self.assertEqual(len(tools), len(set(tools)))
        self.assertIn("market.read", tools)
        self.assertIn("substrate.read", tools)


class ParsingEnvelopeTests(unittest.TestCase):
    def test_wrap_result_names_nulls_and_receipt(self):
        env = parsing.wrap_result(
            "market.read", {"a": 1, "b": None}, segment="ledger", mode="snapshot")
        self.assertEqual(env["tool"], "market.read")
        self.assertEqual(env["status"], "ok")
        self.assertIn("b", env["null_fields"])
        self.assertIn("truncated", env["budget_receipt"])
        self.assertNotIn("parse_warning", env)

    def test_full_mode_carries_human_only_warning(self):
        env = parsing.wrap_result(
            "market.read", {"a": 1}, segment="ledger", mode="full")
        self.assertIn("parse_warning", env)
        self.assertIn("human_only_warning", env["budget_receipt"])

    def test_truncation_is_explicit(self):
        big = {"rows": list(range(10_000))}
        data, receipt = parsing.truncate_data(big, segment="ledger", mode="snapshot")
        self.assertTrue(receipt["truncated"])
        self.assertTrue(data.get("_truncated") or data.get("rows", {}).get("__truncated__"))

    def test_empty_read_returns_surfaces_as_data(self):
        surfaces = {"surfaces": [{"surface": "collated_latest", "present": False}],
                    "available_surfaces": [], "read_tools": {"market.read": "x"}}
        env = parsing.empty_read(
            tool="market.read", segment="ledger", mode="snapshot",
            symbol="SOLUSDT", surfaces=surfaces)
        self.assertEqual(env["status"], "empty")
        self.assertIn("surfaces", env["data"])
        self.assertIn("read_tools", env["data"])


class PromptHeaderTests(unittest.TestCase):
    def test_prompt_is_frozen_and_versioned(self):
        first = prompts.build_interaction_prompt("ledger", "snapshot")
        second = prompts.build_interaction_prompt("ledger", "snapshot")
        self.assertEqual(first, second)
        self.assertIn(prompts.PROMPT_VERSION, first)
        self.assertIn("NULL DISCIPLINE", first)
        self.assertIn("CITATION", first)

    def test_attach_header_adds_interaction_once(self):
        out = prompts.attach_header({"a": 1}, segment="warm", mode="compact",
                                    source="redis")
        self.assertIn("interaction", out)
        self.assertEqual(out["interaction"]["segment"], "warm")
        self.assertIn("prompt", out["interaction"])
        self.assertEqual(out["a"], 1)


class ManifestTests(unittest.TestCase):
    def test_describe_lists_segments_modes_budgets(self):
        manifest = describe()
        self.assertIn("segments", manifest)
        self.assertIn("modes", manifest)
        self.assertIn("char_roofs", manifest)
        self.assertIn("read_tools", manifest)
        self.assertEqual(
            manifest["interaction_version"], budgets.INTERACTION_SCHEMA_VERSION)


class CliCompatTests(unittest.TestCase):
    def test_parser_keeps_hermes_flags(self):
        from market_service.interaction_plane.cli import build_parser

        p = build_parser()
        args = p.parse_args(["SOLUSDT", "--read", "--mode", "snapshot"])
        self.assertTrue(args.read)
        self.assertEqual(args.mode, "snapshot")
        args = p.parse_args(["SOLUSDT", "--substrate-read", "--substrate", "tape"])
        self.assertTrue(args.substrate_read)
        self.assertIn("describe_interaction", vars(args))


if __name__ == "__main__":
    unittest.main()
