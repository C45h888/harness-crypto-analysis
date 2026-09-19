"""Canonical inference runtime package.

The package exposes the driver and its phase modules; governance remains
owned by ``engine.fsm`` and ``engine.controller`` rather than this package.
"""

from . import chain, context, driver, gather, output, reasoning  # noqa: F401
from ..fsm import GOVERNANCE_MEMBRANE
from .driver import InferenceEngine

__all__ = ["InferenceEngine", "GOVERNANCE_MEMBRANE"]
