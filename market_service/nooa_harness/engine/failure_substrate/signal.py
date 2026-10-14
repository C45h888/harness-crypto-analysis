"""signal_failure — the compatibility entry point for the failure chain.

The canonical chain is three layers:

    cycle fails
      -> engine/fsm.py intake edge (CYCLE_FAILED)          [router]
        -> engine/failure_fsm.FAILURE_FSM.classify(kind)   [governance]
          -> failure_substrate.substrate.mediate(verdict)   [mechanics]
            -> failure_substrate.workers.*.invoke()          [execution]

This function is the one-call convenience over the whole chain: it CLASSIFIES
through the Failure FSM and then MEDIATES through the substrate. Runtime code
that wants the layers explicit should call ``FAILURE_FSM.classify`` and
``substrate.mediate`` directly.

It carries NO classification table and NO worker map of its own — those live
in the Failure FSM (classification) and the substrate (worker assignment).
"""

from __future__ import annotations

from typing import Any

from ..failure_fsm import FAILURE_FSM, FailureVerdict
from .contracts import FailureTask, FailureTransport
from .substrate import (
    FAILURE_SUBSTRATE,
    FailureOutcome,
    FailureSubstrate,
)


async def signal_failure(
    kind: str,
    detail: str | None = None,
    *,
    retry_count: int = 0,
    is_transient_override: bool | None = None,
    transport: FailureTransport | None = None,
    substrate: FailureSubstrate | None = None,
) -> FailureOutcome:
    """Classify + dispatch one failure (never raises).

    The runtime must check ``outcome.terminate_now`` / ``outcome.continue_cycle``:
      - ``continue_cycle`` -> resume the loop (re-attempt or go on);
      - ``terminate_now``  -> route to ``run_runtime_failure()`` with
        ``outcome.failure_report`` attached to ``deterministic_state``.
    """
    verdict: FailureVerdict = FAILURE_FSM.classify(
        kind, is_transient_override=is_transient_override,
    )
    target = substrate if substrate is not None else FAILURE_SUBSTRATE
    return await target.dispatch(
        verdict,
        task=FailureTask.from_verdict(verdict, detail=detail),
        transport=transport,
        retry_count=retry_count,
    )


__all__ = [
    "FailureOutcome",
    "signal_failure",
]
