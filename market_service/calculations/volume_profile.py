"""Volume-profile calculations — re-export seam (substrate decomposition).

The volume-profile substrate now lives at
``market_service.calculations.substrates.volume_profile``. This module
re-exports it so historical import paths keep working. New code imports from
the substrate package.
"""

from market_service.calculations.substrates.volume_profile import *
from market_service.calculations.substrates.volume_profile import (  # noqa: F401
    VALUE_AREA_RATIO,
    build_volume_profile,
    side_split,
    volume_profile_summary,
)