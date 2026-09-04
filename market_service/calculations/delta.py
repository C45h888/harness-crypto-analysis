"""DELTA variable calculation — re-export seam (substrate decomposition).

The DELTA substrate now lives at
``market_service.calculations.substrates.delta``. This module re-exports it
so historical import paths keep working. New code imports from the substrate
package.
"""

from market_service.calculations.substrates.delta import *
from market_service.calculations.substrates.delta import (  # noqa: F401
    _pairs,
    delta_state,
    delta_variable,
    flow_alignment,
    range_imbalance,
    tbr_avg_pct,
    tbr_last_pct,
    wall_imbalance,
)