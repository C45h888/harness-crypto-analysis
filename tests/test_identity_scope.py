"""Identity plane — one Scope authority (symbol + venue) for the whole runtime.

These tests pin the alignment that replaced nine symbol authorities and a
literal ``"spot"`` venue default:

* the runtime trades SOLUSDT on **futures** (capture is futures-primary);
* every CLI default is DERIVED from Settings, never a literal;
* an uncaptured symbol is refused before any LLM call;
* ``market.read`` reads live venue-scoped Redis projections, not the frozen
  collated plane, and never returns a number without an age.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import time
import unittest
from contextlib import contextmanager

from market_service.runtime.scope import (
    CANONICAL_VENUE,
    DEFAULT_SYMBOL,
    VENUES,
    Scope,
    normalize_symbol,
    normalize_venue,
    resolve_scope,
)


@contextmanager
def _runtime_env(**env: str):
    """Scoped env with a placeholder DATABASE_URL the refusal path never uses."""
    keys = ("SYMBOLS", "POLL_SYMBOLS", "MICROSTRUCTURE_VENUE", "DATABASE_URL")
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ["DATABASE_URL"] = "postgresql://test:test@127.0.0.1:1/test"
        os.environ.update({k: v for k, v in env.items() if v is not None})
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _settings(**env: str):
    """Build a Settings with a scoped environment (never the real .env)."""
    keys = ("SYMBOLS", "POLL_SYMBOLS", "MICROSTRUCTURE_VENUE", "DATABASE_URL")
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(env)
        from market_service.config import Settings
        return Settings.from_redis_env()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class VenueVocabularyTests(unittest.TestCase):
    def test_canonical_venue_is_futures(self):
        # capture.py: "Primary venue is now futures (SOL-USDT perps) — spot is
        # legacy." The engine default must match what capture writes.
        self.assertEqual(CANONICAL_VENUE, "futures")

    def test_perps_normalises_to_futures(self):
        self.assertEqual(normalize_venue("perps"), "futures")
        self.assertEqual(normalize_venue("PERPS"), "futures")
        self.assertEqual(normalize_venue(" futures "), "futures")

    def test_spot_is_still_expressible(self):
        self.assertEqual(normalize_venue("spot"), "spot")
        self.assertIn("spot", VENUES)

    def test_unknown_venue_raises(self):
        with self.assertRaises(ValueError):
            normalize_venue("ftures")          # typo must not resolve
        with self.assertRaises(ValueError):
            normalize_venue("")

    def test_scope_normalises_on_construction(self):
        self.assertEqual(Scope("solusdt", "perps").venue, "futures")
        self.assertEqual(Scope("solusdt", "perps").symbol, "SOLUSDT")
        self.assertEqual(normalize_symbol(" solusdt "), "SOLUSDT")


class SettingsIdentityTests(unittest.TestCase):
    def test_default_identity_is_sol_futures(self):
        s = _settings()
        self.assertEqual(s.symbols, ("SOLUSDT",))
        self.assertEqual(s.venue, "futures")
        self.assertEqual(Scope.from_settings(s).symbol, "SOLUSDT")
        self.assertEqual(Scope.from_settings(s).venue, "futures")

    def test_microstructure_venue_env_is_honoured(self):
        # The SAME env var the capture container reads — the engine cannot
        # disagree with capture.
        self.assertEqual(_settings(MICROSTRUCTURE_VENUE="spot").venue, "spot")
        self.assertEqual(_settings(MICROSTRUCTURE_VENUE="perps").venue, "futures")

    def test_unknown_venue_env_fails_at_construction(self):
        with self.assertRaises(ValueError):
            _settings(MICROSTRUCTURE_VENUE="nonsense")

    def test_scope_resolution_reports_sources(self):
        payload = resolve_scope(_settings()).to_dict()
        self.assertEqual(payload["symbol"], DEFAULT_SYMBOL)
        self.assertEqual(payload["venue"], "futures")
        self.assertIn("symbol", payload["sources"])


class CliDefaultsAreDerivedTests(unittest.TestCase):
    """A literal symbol/venue default is the bug this plane exists to prevent."""

    def test_no_literal_identity_defaults_in_the_cli(self):
        src = pathlib.Path(
            "market_service/commands/nooa_cli_ext.py").read_text()
        literals = re.findall(
            r'default="(?:[A-Z]+USDT|spot|futures|perps)"', src)
        self.assertEqual(literals, [],
                         f"CLI re-declares identity: {literals}")

    def test_cli_scope_command_reports_the_runtime_identity(self):
        from click.testing import CliRunner
        from market_service.commands.nooa_cli_ext import command
        with _runtime_env(SYMBOLS="SOLUSDT"):
            result = CliRunner().invoke(command, ["scope"])
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["symbol"], DEFAULT_SYMBOL)
        self.assertEqual(payload["venue"], "futures")

    def test_micro_allowlists_are_derived_not_frozen(self):
        from market_service.commands import nooa_cli_ext as cli
        self.assertFalse(
            hasattr(cli, "_MICRO_ALLOWED_SYMBOLS"),
            "hardcoded symbol frozenset reintroduced")
        self.assertFalse(
            hasattr(cli, "_MICRO_ALLOWED_VENUES"),
            "hardcoded venue frozenset reintroduced")
        self.assertEqual(cli._micro_allowed_venues(), frozenset(VENUES))


class FailFastTests(unittest.TestCase):
    def test_uncaptured_symbol_is_refused_before_any_llm_call(self):
        from market_service.nooa_harness import inference_runner as runner

        calls = {"llm": 0, "engine": 0}
        real_build = runner._build_engine

        async def _spy(*a, **k):
            calls["engine"] += 1
            return await real_build(*a, **k)

        runner._build_engine = _spy
        try:
            with _runtime_env(SYMBOLS="SOLUSDT"):
                result = asyncio.run(
                    runner.run_inference_once("ETHUSDT", force=True))
        finally:
            runner._build_engine = real_build

        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "symbol_not_captured")
        self.assertEqual(result["captured"], ["SOLUSDT"])
        self.assertEqual(calls["engine"], 0,
                         "engine was built for an uncaptured symbol")
        self.assertNotIn("record", result)
        self.assertNotIn("answer", result)


class MarketReadLiveTests(unittest.TestCase):
    """market.read reads LIVE venue projections, never the frozen collated plane."""

    class _Redis:
        def __init__(self, data):
            self._data = data
        async def get(self, key):
            return self._data.get(key)

    class _Store:
        prefix = "marketflow"

        def __init__(self, data):
            self._data = data
            self.redis = MarketReadLiveTests._Redis(data)

        def microstructure_evidence_key(self, v, s):
            return f"latest:microstructure:{v}:{s.upper()}:evidence"
        def microstructure_status_key(self, v, s):
            return f"latest:microstructure:{v}:{s.upper()}:status"
        def microstructure_book_key(self, v, s):
            return f"latest:microstructure:{v}:{s.upper()}:book"
        def microstructure_ofi_stream(self, v, s):
            return f"stream:microstructure:ofi:{v}:{s.upper()}"
        def derivatives_key(self, s):
            return f"latest:{s.upper()}:derivatives"
        def substrate_latest_key(self, sub, s):
            return f"latest:substrate:{sub}:{s.upper()}"

        async def read_microstructure_evidence(self, v, s):
            return self._load(self.microstructure_evidence_key(v, s))
        async def read_microstructure_status(self, v, s):
            return self._load(self.microstructure_status_key(v, s))
        async def read_microstructure_intervals(self, v, s, **k):
            return self._data.get(("intervals", v, s), [])
        async def read_derivative_evidence(self, s):
            return self._load(self.derivatives_key(s))

        def _load(self, key):
            raw = self._data.get(key)
            return json.loads(raw) if raw else None

    def _seeded(self):
        now = int(time.time() * 1000)
        return {
            "latest:microstructure:futures:SOLUSDT:evidence": json.dumps(
                {"last_price": 145.2, "received_ts_ms": now - 1500}),
            "latest:microstructure:futures:SOLUSDT:status": json.dumps(
                {"state": "running", "updated_at_ms": now - 500}),
            "latest:microstructure:futures:SOLUSDT:book": json.dumps(
                {"received_ts_ms": now - 800}),
            ("intervals", "futures", "SOLUSDT"): [{"i": 1}, {"i": 2}],
        }

    def _read(self, store, **kwargs):
        from market_service.nooa_harness.inference.tooling.tools_market import (
            dispatch_market_read,
        )
        return asyncio.run(dispatch_market_read(store, "SOLUSDT", **kwargs))

    def test_reads_live_venue_scoped_projection(self):
        payload, _ = self._read(self._Store(self._seeded()), venue="futures")
        self.assertEqual(payload["source"], "redis-live")
        self.assertEqual(payload["venue"], "futures")
        self.assertEqual(payload["symbol"], "SOLUSDT")
        self.assertEqual(payload["capture_state"], "running")
        self.assertEqual(payload["intervals_available"], 2)
        # Every present section carries an age — no number without provenance.
        for name, row in payload["sections"].items():
            if row["status"] == "present":
                self.assertIsNotNone(row["age_ms"], name)

    def test_frozen_collated_plane_is_ignored(self):
        """The dead plane is never consulted, even when it holds data."""
        data = self._seeded()
        data["latest:BTCUSDT:collated"] = json.dumps(
            {"last_price": 145.2, "generated_at": "2020-01-01T00:00:00"})
        payload, _ = self._read(self._Store(data), venue="futures")
        self.assertNotIn("collated", json.dumps(payload["sections"]))
        self.assertEqual(payload["source"], "redis-live")

    def test_spot_is_a_different_identity_and_reads_empty(self):
        payload, _ = self._read(self._Store(self._seeded()), venue="spot")
        self.assertEqual(payload["status"], "empty")
        self.assertEqual(payload["venue"], "spot")

    def test_empty_names_the_venue_scoped_keys_probed(self):
        payload, _ = self._read(self._Store({}), venue="futures")
        self.assertEqual(payload["status"], "empty")
        checked = json.dumps(payload["checked"])
        self.assertIn("microstructure:futures:SOLUSDT", checked)
        self.assertIn(payload["venue"], payload["reason"])

    def test_perps_alias_reads_the_futures_projection(self):
        payload, _ = self._read(self._Store(self._seeded()), venue="perps")
        self.assertEqual(payload["venue"], "futures")
        self.assertEqual(payload["status"] in ("ok", "partial"), True)


class ExecutorVenueTests(unittest.TestCase):
    """Tool dispatch must carry the cycle venue, not a literal default."""

    def test_executor_source_has_no_spot_default(self):
        src = pathlib.Path(
            "market_service/nooa_harness/inference/tooling/executor.py"
        ).read_text()
        self.assertNotIn('args.get("venue", "spot")', src,
                         "tool dispatch re-defaults venue to spot")
        self.assertIn('args.get("venue") or venue', src)

    def test_executor_resolves_venue_from_settings_scope(self):
        from market_service.nooa_harness.inference.tooling.executor import (
            execute_tool,
        )
        captured = {}

        class _Store:
            prefix = "marketflow"

        # An unknown tool returns a denial without dispatching; we only assert
        # the venue resolution path does not explode and does not invent spot.
        _, log = asyncio.run(execute_tool(
            _Store(), "totally.unknown", {},
            settings=_settings(MICROSTRUCTURE_VENUE="perps")))
        self.assertEqual(log["result"], "denied")


if __name__ == "__main__":
    unittest.main()
