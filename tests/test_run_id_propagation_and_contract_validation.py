"""Regression tests for the two contracts the user called out:

  1. **Run-ID propagation** - the orchestrator's run_id MUST reach the final
     ``MarketRunEnvelope`` in PostgreSQL / Redis. The previous implementation
     called ``analyze()`` again inside the collator and minted a fresh run_id,
     so the envelope could not be tied back to the cycle that produced it.

  2. **Explicit contract validation** - the calculations and analysis
     adapters MUST NOT silently swallow canonical function failures with
     ``log.warning`` and a fabricated default. When the input shape cannot
     be built, the adapter MUST raise ``ContractViolation`` and the handler
     MUST surface it as a structured ``errors`` entry on the envelope with
     status ``degraded`` or ``invalid``.

These tests fail loudly if either contract is violated.
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from market_service.nodes.contracts import (
    ContractViolation,
    contract_error_entry,
    require_float,
    require_key,
    require_list_of_pairs,
    strict_call,
)
from market_service.runtime.contracts import (
    MARKET_STATE_SCHEMA_VERSION,
    MarketStateEnvelope,
    RefreshCommand,
)
from market_service.config import Settings


# ---------------------------------------------------------------------------
# Contract 1: run_id propagation
# ---------------------------------------------------------------------------

class RunIdPropagationTests(unittest.IsolatedAsyncioTestCase):
    """The orchestrator's run_id MUST reach the persisted MarketRunEnvelope."""

    async def test_domain_aware_collate_uses_orchestrator_run_id_verbatim(self):
        from market_service.commands.collate import build_envelope_from_domain_states

        run_id = "11111111-2222-3333-4444-555555555555"
        store = MagicMock()
        async def _read_run_domain_state(rid, source):
            return MarketStateEnvelope(
                symbol="SOLUSDT", source=source, run_id=rid,
                observed_at="2026-08-12T00:00:00+00:00",
                produced_at="2026-08-12T00:00:00+00:00",
                status="healthy", data={"source_marker": source, "run_id": rid},
            )
        store.read_run_domain_state = AsyncMock(side_effect=_read_run_domain_state)
        store.close = AsyncMock()
        with patch("market_service.commands.collate.RedisRuntimeStore", return_value=store):
            envelope = await build_envelope_from_domain_states(
                "SOLUSDT", run_id,
                Settings(
                    database_url="postgresql://x/y", redis_url="redis://x:6379/0",
                    redis_key_prefix="marketflow", redis_stream_maxlen=1000,
                    symbols=("SOLUSDT",), poll_seconds=30, flow_window_seconds=300,
                    depth_levels=20,
                ),
            )
        # The persisted envelope's run_id MUST equal the orchestrator's run_id.
        self.assertEqual(envelope.run_id, run_id)
        # Every domain envelope's run_id MUST be visible in domain_outputs.
        for source in ("data-access", "calculations", "analysis"):
            self.assertEqual(envelope.domain_outputs[source]["run_id"], run_id)
        self.assertEqual(envelope.canonical_state["data-access"]["source_marker"], "data-access")
        self.assertEqual(envelope.canonical_state["analysis"]["source_marker"], "analysis")
        # Source metadata proves provenance from the same cycle.
        self.assertEqual(envelope.source_metadata["domain_run_ids"]["data-access"], run_id)
        self.assertEqual(envelope.source_metadata["domain_run_ids"]["calculations"], run_id)
        self.assertEqual(envelope.source_metadata["domain_run_ids"]["analysis"], run_id)

    async def test_domain_aware_collate_rejects_run_id_mismatch_as_invalid(self):
        from market_service.commands.collate import build_envelope_from_domain_states

        orchestrator_run_id = "aaaa"
        foreign_run_id = "bbbb"
        store = MagicMock()
        async def _read_run_domain_state(rid, source):
            return MarketStateEnvelope(
                symbol="SOLUSDT", source=source, run_id=foreign_run_id,  # mismatch!
                observed_at="2026-08-12T00:00:00+00:00",
                produced_at="2026-08-12T00:00:00+00:00",
                status="healthy", data={"source_marker": source, "run_id": foreign_run_id},
            )
        store.read_run_domain_state = AsyncMock(side_effect=_read_run_domain_state)
        store.close = AsyncMock()
        with patch("market_service.commands.collate.RedisRuntimeStore", return_value=store):
            envelope = await build_envelope_from_domain_states(
                "SOLUSDT", orchestrator_run_id,
                Settings(
                    database_url="postgresql://x/y", redis_url="redis://x:6379/0",
                    redis_key_prefix="marketflow", redis_stream_maxlen=1000,
                    symbols=("SOLUSDT",), poll_seconds=30, flow_window_seconds=300,
                    depth_levels=20,
                ),
            )
        self.assertEqual(envelope.run_id, orchestrator_run_id)
        self.assertEqual(envelope.status, "invalid")
        self.assertEqual(envelope.canonical_state["domain_status"]["data-access"], "mismatch")
        self.assertEqual(envelope.canonical_state["domain_status"]["calculations"], "mismatch")
        self.assertEqual(envelope.canonical_state["domain_status"]["analysis"], "mismatch")
        # Errors must be structured.
        self.assertTrue(envelope.errors)
        for err in envelope.errors:
            self.assertIn("domain", err)
            self.assertIn("error", err)

    async def test_orchestrator_passes_from_domain_state_flag_to_collator(self):
        """The orchestrator MUST invoke collate with --from-domain-state --run-id so the envelope inherits the cycle's run_id."""
        import sys
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

            class _Proc:
                returncode = 0
                async def communicate(self_inner):
                    # Synthesize a successful collator response.
                    response = {
                        "envelope": {
                            "run_id": "expected-run-id",
                            "status": "healthy",
                            "symbol": "SOLUSDT",
                        },
                        "persistence": {"postgres_inserted": True, "redis_stream_id": "1-0"},
                    }
                    return json.dumps(response).encode("utf-8"), b""
            return _Proc()

        from market_service.nodes import orchestrator as orch_mod

        with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_exec):
            result = await orch_mod.run_collator_subprocess("SOLUSDT", "expected-run-id")

        args = captured["args"]
        self.assertIn("--from-domain-state", args)
        self.assertIn("--run-id", args)
        self.assertIn("expected-run-id", args)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["envelope"]["run_id"], "expected-run-id")

    async def test_orchestrator_detects_collator_run_id_mismatch(self):
        """If the collator persists a different run_id than the orchestrator asked for, the cycle is marked failed."""
        import asyncio

        class _Proc:
            returncode = 0
            async def communicate(self_inner):
                response = {
                    "envelope": {"run_id": "wrong-id", "status": "healthy", "symbol": "SOLUSDT"},
                    "persistence": {},
                }
                return json.dumps(response).encode("utf-8"), b""

        from market_service.nodes import orchestrator as orch_mod
        with patch.object(asyncio, "create_subprocess_exec", return_value=_Proc()):
            result = await orch_mod.run_collator_subprocess("SOLUSDT", "expected-id")
        self.assertFalse(result["ok"])
        self.assertIn("expected-id", result.get("expected", ""))
        self.assertEqual(result.get("got"), "wrong-id")


# ---------------------------------------------------------------------------
# Contract 2: explicit validation, never silent degradation
# ---------------------------------------------------------------------------

class ContractValidationTests(unittest.TestCase):
    """The previous silent-degradation pattern is banned."""

    def test_require_key_raises_contract_violation_for_missing_path(self):
        with self.assertRaises(ContractViolation) as cm:
            require_key({"a": {"b": 1}}, "a", "c", function="foo", where="x")
        self.assertEqual(cm.exception.function, "foo")
        self.assertIn("c", cm.exception.reason)

    def test_require_list_of_pairs_normalises_dict_rows(self):
        rows = [{"price": "100", "qty": "2"}, {"p": "101", "q": "3"}]
        out = require_list_of_pairs(rows, function="f", where="x")
        self.assertEqual(out, [[100.0, 2.0], [101.0, 3.0]])

    def test_require_list_of_pairs_ignores_positional_dict_keys(self):
        # dict.get(0) is not supported; positional dict keys are skipped so
        # the parser falls through to the next row rather than crashing.
        rows = [{"0": "100", "1": "2"}, {"price": "101", "qty": "3"}]
        out = require_list_of_pairs(rows, function="f", where="x")
        self.assertEqual(out, [[101.0, 3.0]])

    def test_require_list_of_pairs_returns_empty_for_none(self):
        self.assertEqual(require_list_of_pairs(None, function="f", where="x"), [])

    def test_require_list_of_pairs_rejects_non_iterable(self):
        with self.assertRaises(ContractViolation):
            require_list_of_pairs(42, function="f", where="x")

    def test_require_float_passes_through_none(self):
        self.assertIsNone(require_float(None, function="f", where="x"))

    def test_require_float_rejects_non_numeric(self):
        with self.assertRaises(ContractViolation):
            require_float("not a number", function="f", where="x")

    def test_strict_call_re_raises_contract_violation_unchanged(self):
        def bad():
            raise ContractViolation("bad", "missing key")
        with self.assertRaises(ContractViolation) as cm:
            strict_call("bad", bad)
        self.assertEqual(cm.exception.function, "bad")

    def test_strict_call_wraps_generic_exception_as_contract_violation(self):
        def boom():
            raise RuntimeError("kaboom")
        with self.assertRaises(ContractViolation) as cm:
            strict_call("boom_fn", boom)
        self.assertEqual(cm.exception.function, "boom_fn")
        self.assertIn("kaboom", cm.exception.reason)
        self.assertIn("RuntimeError", cm.exception.reason)

    def test_contract_error_entry_is_json_safe(self):
        v = ContractViolation("foo", "missing", {"path": "a.b"})
        entry = contract_error_entry(v)
        self.assertEqual(entry["function"], "foo")
        self.assertEqual(entry["error"], "missing")
        self.assertEqual(entry["details"], {"path": "a.b"})
        # JSON-roundtrippable.
        json.dumps(entry)


# ---------------------------------------------------------------------------
# Handler-level: ContractViolation becomes structured errors, not silent logs
# ---------------------------------------------------------------------------

class HandlerContractValidationTests(unittest.IsolatedAsyncioTestCase):
    """When an adapter cannot build the canonical shape, the handler MUST surface a structured error and downgrade the envelope."""

    async def test_calculations_handler_reports_invalid_for_missing_data_access(self):
        from market_service.nodes.calculations import make_handler
        store = MagicMock()
        store.read_run_domain_state = AsyncMock(return_value=None)
        store.read_latest_domain_state = AsyncMock(return_value=None)
        store.close = AsyncMock()
        settings = Settings(
            database_url="postgresql://x/y", redis_url="redis://x:6379/0",
            redis_key_prefix="marketflow", redis_stream_maxlen=1000,
            symbols=("SOLUSDT",), poll_seconds=30, flow_window_seconds=300, depth_levels=20,
        )
        handler = await make_handler(settings, store)
        result = await handler(RefreshCommand(domain="calculations", symbol="SOLUSDT", command_id="r1"))
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(result["errors"])
        # Every error must be structured.
        for err in result["errors"]:
            self.assertIn("function", err)
            self.assertIn("error", err)

    async def test_calculations_handler_collects_structured_contract_errors(self):
        """A data-access payload with empty evidence should produce a degraded envelope with structured errors, never a fabricated healthy one."""
        from market_service.nodes.calculations import make_handler
        store = MagicMock()
        envelope = MarketStateEnvelope(
            symbol="SOLUSDT", source="data-access",
            observed_at="2026-08-12T00:00:00+00:00",
            produced_at="2026-08-12T00:00:00+00:00",
            status="healthy", data={"evidence": {"spot": {}, "futures": {}}},
            run_id="r1",
        )
        store.read_run_domain_state = AsyncMock(return_value=envelope)
        store.read_latest_domain_state = AsyncMock(return_value=envelope)
        store.close = MagicMock()
        settings = Settings(
            database_url="postgresql://x/y", redis_url="redis://x:6379/0",
            redis_key_prefix="marketflow", redis_stream_maxlen=1000,
            symbols=("SOLUSDT",), poll_seconds=30, flow_window_seconds=300, depth_levels=20,
        )
        handler = await make_handler(settings, store)
        result = await handler(RefreshCommand(domain="calculations", symbol="SOLUSDT", command_id="r1"))
        # Even with empty evidence, the calculations handler must NOT silently
        # produce a "healthy" status - it must surface a degraded/invalid state.
        self.assertIn(result["status"], ("degraded", "invalid"))
        # And it must NOT raise - contract violations must be captured as structured errors.
        for err in result["errors"]:
            self.assertIn("function", err)
            self.assertIn("error", err)

    async def test_analysis_handler_reports_invalid_for_missing_calculations(self):
        from market_service.nodes.analysis import make_handler
        store = MagicMock()
        store.read_run_domain_state = AsyncMock(return_value=None)
        store.read_latest_domain_state = AsyncMock(return_value=None)
        store.close = MagicMock()
        settings = Settings(
            database_url="postgresql://x/y", redis_url="redis://x:6379/0",
            redis_key_prefix="marketflow", redis_stream_maxlen=1000,
            symbols=("SOLUSDT",), poll_seconds=30, flow_window_seconds=300, depth_levels=20,
        )
        handler = await make_handler(settings, store)
        result = await handler(RefreshCommand(domain="analysis", symbol="SOLUSDT", command_id="r1"))
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(result["errors"])
        self.assertEqual(result["errors"][0]["function"], "analysis")


if __name__ == "__main__":
    unittest.main()