"""Stream-driven domain service nodes.

Each node is a thin adapter around an existing canonical ``market_service``
module. They communicate exclusively through Redis (``<prefix>:stream:commands``
for triggers and per-domain projection streams for outputs). The orchestrator
service is responsible for sequencing; these nodes stay stateless.

Per the containerization contract:

- ``data-access`` owns network I/O through ``market_service.clients``.
- ``calculations`` consumes a data-access payload and runs pure math.
- ``analysis`` consumes calculations + data-access and produces interpretation.
- ``orchestrator`` is the timer that drives the pipeline and invokes
  ``python -m market_service.commands.collate`` at the end of each cycle.
"""

from ._base import build_envelope, run_command_loop, setup_logging

__all__ = ["build_envelope", "run_command_loop", "setup_logging"]