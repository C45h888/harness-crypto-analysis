"""Tape flow calculations — re-export seam (substrate decomposition pass).

The tape aggregation substrate now lives at
``market_service.calculations.substrates.tape``. This module re-exports it so
historical import paths keep working. New code imports from the substrate
package.
"""

from market_service.calculations.substrates.tape import *
from market_service.calculations.substrates.tape import (  # noqa: F401
    _bucket,
    bucketed_cvd,
    correlate,
    cvd_series_corr,
    microprice_skew_bps,
    price_bucketed_flow,
    spot_turnover_share,
    summarize,
)