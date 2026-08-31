"""Tests for the poller dynamic symbol selection control plane.

Covers the Redis control-key surface (set/read/clear/status), the
``resolve_active_symbols`` precedence logic, and the harness
``--poller-symbols`` / ``--poller-symbols-reset`` / ``--poller-status``
routes. No live Redis or Binance — all I/O is mocked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from market_service.commands.harness import _poller_control, build_parser
from market_service.config import Settings
from market_service.poller import resolve_active_symbols
from market_service.runtime.redis_store import RedisRuntimeStore


def _settings(poll_symbols: tuple[str, ...] | None = None) -> Settings:
    """Build a minimal Settings with a controlled SYMBOLS / POLL_SYMBOLS split."""
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    return Settings(
        database_url=None,
        redis_url="redis://localhost:6379/0",
        redis_key_prefix="marketflow",
        redis_stream_maxlen=1200,
        symbols=symbols,
        poll_symbols=poll_symbols if poll_symbols is not None else symbols,
        poll_seconds=5,
        flow_window_seconds=300,
        depth_levels=500,
    )


def _store_with_redis(fake_redis: MagicMock) -> RedisRuntimeStore:
    """RedisRuntimeStore without a live connection — injected fake redis."""
    store = RedisRuntimeStore.__new__(RedisRuntimeStore)
    store.redis = fake_redis
    store.prefix = "marketflow"
    store.stream_maxlen = 1200
    store.collated_stream_maxlen = 5000
    store._postgres_store = None
    return store


class PollerControlKeyTests(unittest.TestCase):
    """Round-trip + validation of the control key on a mocked redis client."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_set_poller_symbols_writes_sorted_upper_json(self):
        fake = MagicMock()
        fake.set = AsyncMock()
        store = _store_with_redis(fake)
        self._run(store.set_poller_symbols(["solusdt", " BTCUSDT "]))
        fake.set.assert_awaited_once()
        key, value = fake.set.call_args.args
        self.assertEqual(key, "marketflow:poller:active_symbols")
        self.assertEqual(json.loads(value), ["BTCUSDT", "SOLUSDT"])

    def test_set_poller_symbols_rejects_empty(self):
        fake = MagicMock()
        fake.set = AsyncMock()
        store = _store_with_redis(fake)
        with self.assertRaises(ValueError):
            self._run(store.set_poller_symbols([]))
        with self.assertRaises(ValueError):
            self._run(store.set_poller_symbols(["", "  "]))
        fake.set.assert_not_awaited()

    def test_read_poller_symbols_returns_list(self):
        fake = MagicMock()
        fake.get = AsyncMock(return_value=json.dumps(["SOLUSDT"]))
        store = _store_with_redis(fake)
        self.assertEqual(self._run(store.read_poller_symbols()), ["SOLUSDT"])

    def test_read_poller_symbols_none_when_absent(self):
        fake = MagicMock()
        fake.get = AsyncMock(return_value=None)
        store = _store_with_redis(fake)
        self.assertIsNone(self._run(store.read_poller_symbols()))

    def test_read_poller_symbols_none_on_garbage(self):
        for bad in ("not-json", json.dumps({"oops": 1}), json.dumps([])):
            fake = MagicMock()
            fake.get = AsyncMock(return_value=bad)
            store = _store_with_redis(fake)
            self.assertIsNone(self._run(store.read_poller_symbols()), f"bad={bad!r}")

    def test_clear_poller_symbols_deletes_key(self):
        fake = MagicMock()
        fake.delete = AsyncMock()
        store = _store_with_redis(fake)
        self._run(store.clear_poller_symbols())
        fake.delete.assert_awaited_once_with("marketflow:poller:active_symbols")

    def test_status_round_trip(self):
        payload = {"symbols": ["SOLUSDT"], "source": "redis_control", "poll_seconds": 5}
        fake = MagicMock()
        written: dict[str, str] = {}

        async def _set(key, value):
            written[key] = value

        async def _get(key):
            return written.get(key)

        fake.set = _set
        fake.get = _get
        store = _store_with_redis(fake)
        self._run(store.publish_poller_status(payload))
        self.assertEqual(self._run(store.read_poller_status()), payload)

    def test_read_poller_status_none_when_absent(self):
        fake = MagicMock()
        fake.get = AsyncMock(return_value=None)
        store = _store_with_redis(fake)
        self.assertIsNone(self._run(store.read_poller_status()))


class ResolveActiveSymbolsTests(unittest.TestCase):
    """Precedence: Redis control key > POLL_SYMBOLS env > SYMBOLS env."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_redis_control_key_wins(self):
        store = MagicMock()
        store.read_poller_symbols = AsyncMock(return_value=["SOLUSDT"])
        symbols, source = self._run(resolve_active_symbols(store, _settings()))
        self.assertEqual(symbols, ("SOLUSDT",))
        self.assertEqual(source, "redis_control")

    def test_poll_symbols_env_used_when_narrower(self):
        store = MagicMock()
        store.read_poller_symbols = AsyncMock(return_value=None)
        settings = _settings(poll_symbols=("SOLUSDT",))
        symbols, source = self._run(resolve_active_symbols(store, settings))
        self.assertEqual(symbols, ("SOLUSDT",))
        self.assertEqual(source, "poll_symbols_env")

    def test_symbols_env_fallback(self):
        store = MagicMock()
        store.read_poller_symbols = AsyncMock(return_value=None)
        symbols, source = self._run(resolve_active_symbols(store, _settings()))
        self.assertEqual(symbols, ("BTCUSDT", "ETHUSDT", "SOLUSDT"))
        self.assertEqual(source, "symbols_env")


class PollerConfigEnvTests(unittest.TestCase):
    """Settings._resolve derives poll_symbols from POLL_SYMBOLS env."""

    def test_poll_symbols_defaults_to_symbols(self):
        env = {"SYMBOLS": "BTCUSDT,ETHUSDT,SOLUSDT", "POLL_SYMBOLS": "",
               "REDIS_URL": "redis://localhost:6379/0"}
        with patch.dict(os.environ, env, clear=False):
            settings = Settings.from_redis_env()
        self.assertEqual(settings.poll_symbols, settings.symbols)

    def test_poll_symbols_override(self):
        env = {"SYMBOLS": "BTCUSDT,ETHUSDT,SOLUSDT", "POLL_SYMBOLS": "solusdt",
               "REDIS_URL": "redis://localhost:6379/0"}
        with patch.dict(os.environ, env, clear=False):
            settings = Settings.from_redis_env()
        self.assertEqual(settings.poll_symbols, ("SOLUSDT",))
        self.assertEqual(settings.symbols, ("BTCUSDT", "ETHUSDT", "SOLUSDT"))


class HarnessPollerControlParserTests(unittest.TestCase):
    """The parser accepts the new control-plane flags."""

    def test_poller_symbols_flag(self):
        p = build_parser()
        args = p.parse_args(["--poller-symbols", "SOLUSDT"])
        self.assertEqual(args.poller_symbols, "SOLUSDT")
        self.assertFalse(args.poller_symbols_reset)
        self.assertFalse(args.poller_status)

    def test_poller_symbols_csv(self):
        p = build_parser()
        args = p.parse_args(["--poller-symbols", "SOLUSDT,BTCUSDT"])
        self.assertEqual(args.poller_symbols, "SOLUSDT,BTCUSDT")

    def test_poller_symbols_reset_flag(self):
        p = build_parser()
        args = p.parse_args(["--poller-symbols-reset"])
        self.assertTrue(args.poller_symbols_reset)

    def test_poller_status_flag(self):
        p = build_parser()
        args = p.parse_args(["--poller-status"])
        self.assertTrue(args.poller_status)

    def test_defaults_are_inert(self):
        p = build_parser()
        args = p.parse_args(["SOLUSDT"])
        self.assertIsNone(args.poller_symbols)
        self.assertFalse(args.poller_symbols_reset)
        self.assertFalse(args.poller_status)


class PollerControlHandlerTests(unittest.TestCase):
    """_poller_control writes/reads through the RedisRuntimeStore seam."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(poller_symbols=None, poller_symbols_reset=False, poller_status=False)
        base.update(overrides)
        return argparse.Namespace(**base)

    @patch("market_service.commands.harness.RedisRuntimeStore")
    def test_set_symbols(self, MockStore):
        store = MockStore.return_value
        store.set_poller_symbols = AsyncMock()
        store.close = AsyncMock()
        store.poller_control_key.return_value = "marketflow:poller:active_symbols"
        with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
            result = self._run(_poller_control(self._args(poller_symbols="solusdt")))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "set")
        self.assertEqual(result["active_symbols"], ["SOLUSDT"])
        store.set_poller_symbols.assert_awaited_once_with(["SOLUSDT"])

    @patch("market_service.commands.harness.RedisRuntimeStore")
    def test_set_symbols_rejects_empty_string(self, MockStore):
        store = MockStore.return_value
        store.set_poller_symbols = AsyncMock()
        store.close = AsyncMock()
        with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
            result = self._run(_poller_control(self._args(poller_symbols=" , ")))
        self.assertEqual(result["status"], "error")
        store.set_poller_symbols.assert_not_awaited()

    @patch("market_service.commands.harness.RedisRuntimeStore")
    def test_reset_clears_control_key(self, MockStore):
        store = MockStore.return_value
        store.clear_poller_symbols = AsyncMock()
        store.close = AsyncMock()
        store.poller_control_key.return_value = "marketflow:poller:active_symbols"
        with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
            result = self._run(_poller_control(self._args(poller_symbols_reset=True)))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "reset")
        store.clear_poller_symbols.assert_awaited_once()

    @patch("market_service.commands.harness.RedisRuntimeStore")
    def test_status_returns_payload(self, MockStore):
        store = MockStore.return_value
        store.read_poller_status = AsyncMock(
            return_value={"symbols": ["SOLUSDT"], "source": "redis_control"})
        store.close = AsyncMock()
        with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
            result = self._run(_poller_control(self._args(poller_status=True)))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "status")
        self.assertEqual(result["symbols"], ["SOLUSDT"])

    @patch("market_service.commands.harness.RedisRuntimeStore")
    def test_status_missing_reports_error(self, MockStore):
        store = MockStore.return_value
        store.read_poller_status = AsyncMock(return_value=None)
        store.close = AsyncMock()
        with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
            result = self._run(_poller_control(self._args(poller_status=True)))
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
