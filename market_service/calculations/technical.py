"""Technical analysis calculations — re-export seam (substrate decomposition).

The technical primitives were split into two individual calculation
substrates: ``technicals`` (time-series math: EMA, ATR%, trend) and
``large_print`` (tape classifiers: tiered large flow, seller aggression).
This module re-exports both so historical import paths keep working. New code
imports from the substrate package.
"""

from market_service.calculations.substrates.large_print import (  # noqa: F401
    seller_aggression_classify,
    tiered_large_flow,
)
from market_service.calculations.substrates.technicals import (  # noqa: F401
    EMA_PERIODS_DEFAULT,
    atr_pct_from_klines,
    ema,
    ema_position,
    ema_series,
    trend_drift,
    trend_slope,
    wick_rejections,
)