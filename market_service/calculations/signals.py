"""Deterministic signals — re-export seam (substrate decomposition).

The signals substrate now lives at
``market_service.calculations.substrates.signals``. This module re-exports it
so historical import paths keep working. New code imports from the substrate
package.
"""

from market_service.calculations.substrates.signals import *
from market_service.calculations.substrates.signals import (  # noqa: F401
    deterministic_signals,
)