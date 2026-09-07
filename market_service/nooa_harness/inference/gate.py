"""Hard status gate — the deterministic trichotomy (validated / provisional / insufficient).

Resolved from the inputs BEFORE any LLM call is considered. ``insufficient``
forces a NULL interpretation: the engine persists the refusal as durable
state and spends zero tokens narrating gate-failed data. Null discipline —
an empty interpretation means "not produced", never "nothing to say".

Moved verbatim from the inference.py monolith (decomposition Phase 0).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Hard status gate
# ---------------------------------------------------------------------------

# Capture states that count as "established" — the capture produced a usable
# tape. Any other state (starting, stopped, None) refuses inference.
_ESTABLISHED_CAPTURE_STATES = frozenset({"running", "gap", "reconnecting", "connected"})


class GateInputs:
    """Named input bundle for the hard gate (plain object, no validation)."""


def resolve_inference_status(
    *,
    n_observations: int,
    min_observations: int,
    fit_status: str | None,
    capture_state: str | None,
    events_in_window: int,
    sequence_gaps: int = 0,
) -> tuple[str, tuple[str, ...]]:
    """Deterministically resolve the artifact status trichotomy.

    Returns ``(status, reasons)`` where reasons is the ordered tuple of gate
    failures/warnings that drove the decision. Identical inputs always yield
    identical outputs — this is a pure function, safe to re-run on replay.

    Rules (evaluated in order):
    - insufficient: fewer usable observations than the minimum; the
      underlying fit is insufficient; capture never established; or fewer
      than 2 events in the window.
    - provisional: the fit is provisional; sequence gaps occurred during
      capture; or observation count is below twice the minimum.
    - validated: none of the above.
    """
    reasons: list[str] = []

    if n_observations < min_observations:
        reasons.append(
            f"observations {n_observations} < minimum {min_observations}"
        )
    if fit_status == "insufficient":
        reasons.append("underlying price-impact fit is insufficient")
    if capture_state not in _ESTABLISHED_CAPTURE_STATES:
        reasons.append(f"capture state {capture_state!r} is not established")
    if events_in_window < 2:
        reasons.append(f"only {events_in_window} events in window")
    if reasons:
        return "insufficient", tuple(reasons)

    provisional_reasons: list[str] = []
    if fit_status == "provisional":
        provisional_reasons.append("underlying fit is provisional")
    if sequence_gaps > 0:
        provisional_reasons.append(f"{sequence_gaps} sequence gap(s) during capture")
    if n_observations < 2 * min_observations:
        provisional_reasons.append(
            f"observations {n_observations} < 2x minimum {2 * min_observations}"
        )
    if provisional_reasons:
        return "provisional", tuple(provisional_reasons)

    return "validated", ()


def gate_interpretation(status: str, interpretation: dict[str, Any] | None) -> dict[str, Any] | None:
    """Enforce the NULL-interpretation discipline at the constructor boundary.

    An ``insufficient`` artifact can never carry an interpretation — the hard
    gate guarantees no LLM call happened over gate-failed data. Passing one
    through here neutralizes it instead of raising, so engine code paths can
    be written uniformly.
    """
    if status == "insufficient":
        return None
    return interpretation
