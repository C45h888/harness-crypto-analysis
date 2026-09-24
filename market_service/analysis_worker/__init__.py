"""Analysis worker plane — deterministic, analysis-bounded workers.

Orthogonal sibling of the substrate worker plane. Each analysis worker
consumes SUBSTRATE STATE STREAMS (the calculation plane's own publishes ARE
the deterministic trigger — never raw REST evidence, never Binance), fires on
market-semantic conditions defined in its own file, computes its ONE analysis
domain (imported from ``analysis.*``), and projects into the ``analysis:``
keyspace (latest + bounded stream + PG ledger under ``analysis:<name>``).

Hard rules (enforced by tests/test_analysis_graph.py):
* an analysis worker file imports exactly ONE analysis module;
* no cross-worker, cross-plane worker imports (substrate worker CORE is the
  only substrate_worker import — the machinery is shared, not forked);
* workers never touch Binance or any transport client.

The reasoning plane reads these projections (``analysis.read`` cutover)
instead of recomputing — the analysis layer is continuously warm like the
substrates it reads. Spec: docs/ANALYSIS_WORKER_SPEC.md.
"""

from __future__ import annotations

from market_service.analysis_worker.core import AnalysisPlaneMixin
from market_service.analysis_worker.demand_worker import DemandWorker
from market_service.analysis_worker.liquidation_worker import LiquidationWorker
from market_service.analysis_worker.oi_analysis_worker import OIAnalysisWorker
from market_service.analysis_worker.regime_worker import RegimeWorker
from market_service.analysis_worker.stage_worker import StageWorker
from market_service.analysis_worker.wall_migration_worker import WallMigrationWorker

# Registry: analysis name → worker class. The runner instantiates from here
# (ANALYSIS_WORKERS env selection); Pass 2 fills the remaining domains.
ANALYSIS_WORKER_REGISTRY: dict[str, type] = {
    "regime": RegimeWorker,
    "wall_migration": WallMigrationWorker,
    "oi_analysis": OIAnalysisWorker,
    "liquidation": LiquidationWorker,
    "demand": DemandWorker,
    "stage": StageWorker,
}

__all__ = [
    "ANALYSIS_WORKER_REGISTRY",
    "AnalysisPlaneMixin",
    "RegimeWorker",
    "WallMigrationWorker",
    "OIAnalysisWorker",
    "LiquidationWorker",
    "DemandWorker",
    "StageWorker",
]