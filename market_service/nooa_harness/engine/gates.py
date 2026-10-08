"""Deterministic gate base layer — the contract gates are validated against.

Gates are GUARDS on the loop, not workflow positions and not sub-loops. A
gate consumes a frozen, deterministic input snapshot and emits a frozen
``GateVerdict``. It performs no I/O, spends no LLM budget, and never mutates
state — so the same inputs always produce the same verdict, and the
controller/membrane routes a failure from a *data contract* instead of
inline code "doing as it pleases".

Authority split (unchanged, now with a named base):

  - THIS module owns the gate VOCABULARY and the pure verdict functions —
    what "the data is sufficient" and "the plan is adequate" mean,
    deterministically.
  - ``engine/fsm.py`` owns where a refused gate TERMINATES (``GATE_REFUSED``
    and any future gate-specific terminals).
  - ``engine/controller.py`` owns classification/credit/exit: it consumes the
    verdict, records it, and asks the membrane for the terminal.
  - ``core/`` owns WHEN a gate runs in the loop body (the ``GATE_DATA`` step
    verdicts the agent-first fetch in ``run_data_gate``; the ``GATE_PLAN``
    step executes post-framing in ``run_comprehension``).

The gate vocabulary is deliberately pure data so it may be replayed,
unit-tested with no stores, and cited as the deterministic fact the loop
refused on. The DATA gate wraps the canonical trichotomy
(``resolve_inference_status``); the PLAN gate is forward-only — a refused
plan routes to a terminal, never to a re-plan edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from market_service.nooa_harness.inference.gate import resolve_inference_status

__all__ = [
    "GateKind",
    "GateStatus",
    "GateVerdict",
    "GateRefused",
    "evaluate_data_gate",
    "evaluate_plan_gate",
    "require_gate",
]

# Workflow kinds a plan may declare (mirror of task_directive.KINDS; duplicated
# here as a frozen gate contract so the gate does not import the directive
# module at load time).
_SUPPORTED_PLAN_KINDS = frozenset({"price_target", "hypothesis", "general"})

# Horizon regimes a plan may declare (task_directive vocabulary).
_SUPPORTED_HORIZON_REGIMES = frozenset({"native", "long", "unsupported", "none"})

# Regimes that REQUIRE a bound forecast horizon for acquisition to proceed.
_HORIZON_BEARING_REGIMES = frozenset({"native", "long"})


class GateKind(str, Enum):
    """Which guard a verdict belongs to."""

    DATA = "data"   # bootstrap data sufficiency (zero-LLM)
    PLAN = "plan"   # post-framing plan adequacy (forward-only)


class GateStatus(str, Enum):
    """The discrete outcome of one gate."""

    PASSED = "passed"
    REFUSED = "refused"


@dataclass(frozen=True)
class GateVerdict:
    """A frozen, deterministic gate outcome.

    ``reasons`` is the ordered tuple of failures/warnings that drove the
    decision; ``facts`` is the ordered input snapshot the verdict was
    computed from (so a refused cycle can cite exactly what it refused on).
    The verdict is intentionally immutable and JSON-renderable via
    ``to_dict``.
    """

    gate: GateKind
    status: GateStatus
    reasons: tuple[str, ...] = ()
    facts: tuple[tuple[str, Any], ...] = field(default=())

    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASSED

    @property
    def refused(self) -> bool:
        return self.status is GateStatus.REFUSED

    def facts_dict(self) -> dict[str, Any]:
        return dict(self.facts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.value,
            "status": self.status.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "facts": dict(self.facts),
        }


class GateRefused(Exception):
    """A refused gate, carrying its full deterministic verdict.

    The controller catches this (or calls ``require_gate``) and routes the
    verdict to the membrane terminal — it never re-plans from a refusal.
    """

    def __init__(self, verdict: GateVerdict):
        self.verdict = verdict
        super().__init__(
            f"gate {verdict.gate.value} refused: "
            + ("; ".join(verdict.reasons) or "no reason recorded")
        )


def require_gate(verdict: GateVerdict) -> GateVerdict:
    """Return ``verdict`` when passed, else raise ``GateRefused`` (fail closed)."""
    if not verdict.passed:
        raise GateRefused(verdict)
    return verdict


# ---------------------------------------------------------------------------
# DATA gate — post-ingestion sufficiency (zero-LLM)
# ---------------------------------------------------------------------------


def evaluate_data_gate(
    *,
    n_observations: int,
    min_observations: int,
    fit_status: str | None,
    capture_state: str | None,
    events_in_window: int,
    degraded_spans_in_window: int = 0,
) -> GateVerdict:
    """Deterministically resolve data sufficiency before any LLM call.

    The canonical trichotomy (validated / provisional / insufficient) is
    resolved by ``resolve_inference_status``; this gate maps ``insufficient``
    to REFUSED and everything else to PASSED (provisional is a pass WITH
    reasons, not a refusal). Zero tokens are spent on a refused gate.
    """
    status, reasons = resolve_inference_status(
        n_observations=n_observations,
        min_observations=min_observations,
        fit_status=fit_status,
        capture_state=capture_state,
        events_in_window=events_in_window,
        degraded_spans_in_window=degraded_spans_in_window,
    )
    gate_status = GateStatus.REFUSED if status == "insufficient" else GateStatus.PASSED
    return GateVerdict(
        gate=GateKind.DATA,
        status=gate_status,
        reasons=tuple(reasons),
        facts=(
            ("resolved_status", status),
            ("n_observations", n_observations),
            ("min_observations", min_observations),
            ("fit_status", fit_status),
            ("capture_state", capture_state),
            ("events_in_window", events_in_window),
            ("degraded_spans_in_window", degraded_spans_in_window),
        ),
    )


# ---------------------------------------------------------------------------
# PLAN gate — post-framing adequacy (forward-only)
# ---------------------------------------------------------------------------


def evaluate_plan_gate(
    plan: Mapping[str, Any] | None,
) -> GateVerdict:
    """Deterministically validate the disposed plan's structural contract.

    Forward-only: a refused plan routes to a terminal (the caller decides
    which), never to a re-plan edge. The gate checks the frozen plan
    vocabulary the acquisition loop depends on:

      - the plan is a mapping;
      - ``kind`` is a supported workflow kind;
      - ``horizon_regime`` is a known regime;
      - a horizon-bearing regime (native/long) binds a positive integer
        ``forecast_horizon_ms`` — acquisition cannot be horizon-bound
        without it.

    Recorded ``directive_refusals`` are carried as facts (findings), not
    automatic refusals: a refusal is a recorded answer, not an invalid plan.
    """
    reasons: list[str] = []
    facts: list[tuple[str, Any]] = []
    if not isinstance(plan, Mapping):
        return GateVerdict(
            gate=GateKind.PLAN,
            status=GateStatus.REFUSED,
            reasons=("plan is not a mapping",),
            facts=(("plan_type", type(plan).__name__),),
        )

    kind = plan.get("kind")
    facts.append(("kind", kind))
    if not isinstance(kind, str) or kind not in _SUPPORTED_PLAN_KINDS:
        reasons.append(f"plan.kind {kind!r} is not a supported workflow kind")

    regime = plan.get("horizon_regime")
    facts.append(("horizon_regime", regime))
    if regime not in _SUPPORTED_HORIZON_REGIMES:
        reasons.append(f"plan.horizon_regime {regime!r} is not a known regime")

    horizon = plan.get("forecast_horizon_ms")
    facts.append(("forecast_horizon_ms", horizon))
    if regime in _HORIZON_BEARING_REGIMES:
        if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon <= 0:
            reasons.append(
                f"plan.forecast_horizon_ms {horizon!r} must be a positive integer "
                f"for a {regime} regime"
            )

    refusals = plan.get("directive_refusals")
    facts.append(("directive_refusals", list(refusals) if isinstance(refusals, list) else []))

    return GateVerdict(
        gate=GateKind.PLAN,
        status=GateStatus.REFUSED if reasons else GateStatus.PASSED,
        reasons=tuple(reasons),
        facts=tuple(facts),
    )
