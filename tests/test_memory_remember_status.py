"""Memory remember status mapping (no silent success)."""

import asyncio


def test_remember_not_configured_is_error_not_ok():
    from market_service.nooa_harness.inference.tooling.tools_market import (
        dispatch_memory_remember,
    )
    result, log = asyncio.run(dispatch_memory_remember(
        "SOLUSDT", "spot", args={"kind": "observation", "content": "hello"},
        memory=None, session_id=None, segment="test"))
    assert result["stored"] is False
    assert log["result"] == "error"
    assert log["capability"] == "memory.remember"


def test_remember_empty_content_is_denied_not_ok():
    from market_service.nooa_harness.inference.tooling.tools_market import (
        dispatch_memory_remember,
    )

    class _Mem:
        async def remember(self, *a, **k):
            raise AssertionError("must not reach store on empty content")

    result, log = asyncio.run(dispatch_memory_remember(
        "SOLUSDT", "spot", args={"kind": "observation", "content": "   "},
        memory=_Mem(), session_id="s1", segment="test"))
    assert result["stored"] is False
    assert result["reason"] == "empty content"
    assert log["result"] == "denied"


def test_remember_bad_kind_is_denied_not_ok():
    from market_service.nooa_harness.inference.tooling.tools_market import (
        dispatch_memory_remember,
    )

    class _Mem:
        async def remember(self, *a, **k):
            raise AssertionError("must not reach store on bad kind")

    result, log = asyncio.run(dispatch_memory_remember(
        "SOLUSDT", "spot", args={"kind": "fact", "content": "x"},
        memory=_Mem(), session_id="s1", segment="test"))
    assert result["stored"] is False
    assert log["result"] == "denied"


def test_remember_stored_true_is_ok():
    from market_service.nooa_harness.inference.tooling.tools_market import (
        dispatch_memory_remember,
    )

    class _Row:
        memory_id = "m1"

    class _Mem:
        async def remember(self, *a, **k):
            return _Row()

    result, log = asyncio.run(dispatch_memory_remember(
        "SOLUSDT", "spot", args={"kind": "note", "content": "real observation"},
        memory=_Mem(), session_id="s1", segment="test"))
    assert result["stored"] is True
    assert log["result"] == "ok"


def test_authorize_dispatch_accepts_dry_run():
    import inspect
    from market_service.governance.gates import GovernanceGovernor
    sig = inspect.signature(GovernanceGovernor.authorize_dispatch)
    assert "dry_run" in sig.parameters
