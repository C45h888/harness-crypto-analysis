"""NOOA integration boundary mounted behind the canonical harness.

Importing this package does NOT import ``nooa`` or litellm. The suite/runner
read path and the contract types it re-exports are all nooa-free, so the fast
test path (contract, run-mode, persistence) never pays litellm's import cost.
Only building an actual agent suite (``build_suite``) or resolving the lazy
``Agent`` re-export pulls in ``nooa``.
"""

from __future__ import annotations

from typing import Any

from market_service.runtime.contracts import (
    AnalystBriefing,
    SpecialistReport,
    SpecialistReportParseError,
)

from .suite import AnalystSuite, build_suite

__all__ = [
    "Agent",
    "AnalystBriefing",
    "AnalystSuite",
    "NOOA_IMPORT_OK",
    "SpecialistReport",
    "SpecialistReportParseError",
    "build_suite",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve NOOA-only exports so package import stays nooa-free."""
    # ``Agent`` and the import-ok flag require actually importing NOOA. They
    # are resolved lazily so a plain ``import market_service.nooa_harness``
    # (or any contract/submodule import) never triggers litellm.
    if name == "Agent":
        from nooa import Agent  # noqa: PLC0415

        return Agent
    if name == "NOOA_IMPORT_OK":
        return True
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")