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
    extra: dict[str, Any] | None = None,
    substrate: FailureSubstrate | None = None,
) -> FailureOutcome:
    """Classify + mediate one failure (never raises).

    The runtime must check ``outcome.resolved`` / ``outcome.terminate_now``:
      - ``resolved`` and not ``terminate_now`` -> continue the loop;
      - ``terminate_now`` -> route to ``run_runtime_failure()`` with
        ``outcome.failure_report`` attached to ``deterministic_state``.
    """
    verdict: FailureVerdict = FAILURE_FSM.classify(
        kind, is_transient_override=is_transient_override,
    )
    target = substrate if substrate is not None else FAILURE_SUBSTRATE
    return await target.mediate(
        verdict, detail=detail, retry_count=retry_count, extra=extra,
    )


__all__ = [
    "FailureOutcome",
    "signal_failure",
]
