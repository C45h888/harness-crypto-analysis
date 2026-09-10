"""harness.py is a read/interpret client — it neither computes nor invokes.

The outer harness is the operator + terminal-agent surface. It has no semantic
authority to decide a calculation must run (that belongs to the inference
plane) and no business opening a Binance session, so its computation and
egress flags are gone:

* ``--invoke``            → the calculation container owns every fire-tick.
* ``--refresh-derivatives`` → the poller's warm loop owns the cache.
* ``--live``              → opened its own Binance session and computed.

It also has to speak the runtime's venue. Everything canonical resolves
``MICROSTRUCTURE_VENUE`` (default ``futures``); the harness used to hardcode
``"spot"``, so it read empty keys and triggered inference on a venue the rest
of the system was not running.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from market_service.commands.harness import build_parser


class ComputationFlagsRemovedTests(unittest.TestCase):
    def test_invoke_flag_is_removed(self):
        p = build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["SOLUSDT", "--invoke", "tape,density"])

    def test_derivative_refresh_flags_are_removed(self):
        p = build_parser()
        for flag in ("--refresh-derivatives", "--with-cross-asset"):
            with self.assertRaises(SystemExit, msg=flag):
                p.parse_args(["SOLUSDT", flag])
        with self.assertRaises(SystemExit):
            p.parse_args(["SOLUSDT", "--deriv-ttl", "60"])

    def test_live_waveform_flags_are_removed(self):
        p = build_parser()
        for flag in ("--live",):
            with self.assertRaises(SystemExit, msg=flag):
                p.parse_args(["SOLUSDT", flag])
        for flag, value in (("--trades", "500"), ("--bucket-window", "60")):
            with self.assertRaises(SystemExit, msg=flag):
                p.parse_args(["SOLUSDT", flag, value])

    def test_build_helper_is_gone(self):
        """``build()`` opened its own Binance session inside the harness."""
        import market_service.commands.harness as harness
        self.assertFalse(hasattr(harness, "build"))

    def test_read_surface_survives(self):
        p = build_parser()
        args = p.parse_args(["SOLUSDT", "--substrate-read", "--json"])
        self.assertTrue(args.substrate_read)
        self.assertTrue(args.json)
        self.assertEqual(p.parse_args(["--run-id", "abc-123"]).run_id, "abc-123")

    def test_task_directed_inference_survives(self):
        p = build_parser()
        args = p.parse_args(["SOLUSDT", "--inference", "--inference-force",
                             "--task", "is sell pressure exhausting?"])
        self.assertTrue(args.inference)
        self.assertTrue(args.inference_force)
        self.assertEqual(args.task, "is sell pressure exhausting?")


class VenueResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_microstructure_status_uses_configured_venue(self):
        from market_service.commands.harness import _read_microstructure_status

        args = build_parser().parse_args(["SOLUSDT", "--microstructure-status"])
        store = AsyncMock()
        store.read_microstructure_status.return_value = {"state": "running"}
        store.microstructure_status_key.return_value = "k"
        store.microstructure_raw_stream.return_value = "r"
        store.microstructure_event_stream.return_value = "e"
        store.microstructure_ofi_stream.return_value = "o"

        with patch.dict("os.environ", {"MICROSTRUCTURE_VENUE": "futures"}), \
                patch("market_service.commands.harness.RedisRuntimeStore",
                      return_value=store):
            result = await _read_microstructure_status(args)

        self.assertEqual(result["venue"], "futures")
        store.read_microstructure_status.assert_awaited_once_with("futures", "SOLUSDT")

    def test_inference_route_passes_the_configured_venue(self):
        from market_service.commands.harness import main

        with patch.dict("os.environ", {"MICROSTRUCTURE_VENUE": "futures"}), \
                patch("market_service.nooa_harness.inference_runner.run_inference_once",
                      new_callable=AsyncMock) as run_once:
            run_once.return_value = {"status": "ok"}
            main(["SOLUSDT", "--inference", "--inference-force", "--task", "why?"])

        self.assertEqual(run_once.await_args.kwargs.get("venue"), "futures")


if __name__ == "__main__":
    unittest.main(verbosity=2)
