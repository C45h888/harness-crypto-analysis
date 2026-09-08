"""Substrate worker plane — deterministic, substrate-bounded calculation workers.

One worker per calculation substrate (spec: docs/SUBSTRATE_WORKER_SPEC.md).
Each worker is bounded to the Redis plane: it consumes the raw evidence
stream (and in Phase 2, the microstructure WS streams), fires on
deterministic market-semantic conditions defined in ITS OWN file, computes
its ONE substrate's calculations (imported from
``calculations.substrates.*``), and aggregates the result into the
substrate:latest projection + bounded state stream (Postgres-first in
Phase 3).

Hard rules (enforced by tests/test_substrate_graph.py):
* a worker file imports exactly ONE calculation substrate;
* no cross-worker imports;
* workers never touch Binance or the harness layer.

The harness READS these projections (Phase 4 cutover) instead of computing —
the information pipeline is continuously warm instead of computed on pull.
"""

from __future__ import annotations

from market_service.substrate_worker.anchors_worker import AnchorsWorker
from market_service.substrate_worker.core import SubstrateWorkerCore
from market_service.substrate_worker.delta_worker import DeltaWorker
from market_service.substrate_worker.density_worker import DensityWorker
from market_service.substrate_worker.ladders_worker import LaddersWorker
from market_service.substrate_worker.large_print_worker import LargePrintWorker
from market_service.substrate_worker.migration_worker import MigrationWorker
from market_service.substrate_worker.oi_worker import OiWorker
from market_service.substrate_worker.signals_worker import SignalsWorker
from market_service.substrate_worker.tape_worker import TapeWorker
from market_service.substrate_worker.technicals_worker import TechnicalsWorker
from market_service.substrate_worker.tiers_worker import TiersWorker
from market_service.substrate_worker.volume_profile_worker import VolumeProfileWorker

# Registry: substrate name → worker class. The runner instantiates from here
# (SUBSTRATE_WORKERS env selection); Phase 2 fills the remaining substrates.
WORKER_REGISTRY: dict[str, type[SubstrateWorkerCore]] = {
    "anchors": AnchorsWorker,
    "density": DensityWorker,
    "delta": DeltaWorker,
    "ladders": LaddersWorker,
    "large_print": LargePrintWorker,
    "migration": MigrationWorker,
    "oi": OiWorker,
    "signals": SignalsWorker,
    "tape": TapeWorker,
    "technicals": TechnicalsWorker,
    "tiers": TiersWorker,
    "volume_profile": VolumeProfileWorker,
}

__all__ = [
    "WORKER_REGISTRY",
    "AnchorsWorker",
    "DeltaWorker",
    "DensityWorker",
    "LaddersWorker",
    "LargePrintWorker",
    "MigrationWorker",
    "OiWorker",
    "SignalsWorker",
    "SubstrateWorkerCore",
    "TapeWorker",
    "TechnicalsWorker",
    "TiersWorker",
    "VolumeProfileWorker",
]