"""Shared test fixtures: suite-wide determinism guards (test-only).

The narration extraction-tier flag is process-global observability: a full
narrate_cycle in one test file would otherwise leak its last tier into the
next file's recorded ``narration_tiers`` (order-dependent goldens).
Reset per test; production behavior untouched.
"""

from __future__ import annotations

import pytest

from market_service.nooa_harness.engine import llm as _llm


@pytest.fixture(autouse=True)
def _suite_determinism(monkeypatch):
    monkeypatch.setattr(_llm, "LAST_EXTRACTION_TIER", None)
    yield
