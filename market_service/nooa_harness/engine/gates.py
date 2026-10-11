"""Deterministic gate base layer — the contract gates are validated against.

Gates are GUARDS on the loop, not workflow positions and not sub-loops. A
gate consumes a frozen, deterministic input snapshot and emits a frozen
``GateVerdict``. It performs no I/O, spends no LLM budget, and never mutates
state — so the same inputs always produce the same verdict, and the
controller/membrane routes a failure from a *data contract* instead of
inline code "doing as it pleases".

Authority split (unchanged, now with a named base):

  - THIS module owns the gate VOCABULARY and the pure verdict functions.
  - ``engine/fsm.py`` owns where a refused gate TERMINATES (``GATE_REFUSED``
    and any future gate-specific terminals).
  - ``engine/controller.py`` owns classification/credit/exit: it consumes the
    verdict, records it, and asks the membrane for the terminal.
  - ``core/`` owns WHEN a gate runs in the loop body (the ``GATE_PLAN``
    step executes post-framing in ``run_comprehension``).

Phase R7 (fetch semantic removed): the DATA gate is RETIRED. It was the
judgement layer for the agent-fetch semantic — it verdicted model-chosen
cadence data against the tape-quality trichotomy, and its zero-LLM
insufficient-tape refusal was the fetch turn's only reason to exist.
With comprehension as the first agentic step and plan-bound acquisition,
per-tool deterministic refusals plus the validation gate carry the same
guarantees without a pre-receipt judgement layer. The PLAN gate is
forward-only — a refused plan routes to a terminal, never to a re-plan
edge. The gate vocabulary is deliberately pure data so it may be replayed
and unit-tested with no stores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "GateKind",
    "GateStatus",
    "GateVerdict",
    "GateRefused",
    "evaluate_plan_gate",
    "evaluate_budget_gate",
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

    PLAN = "plan"     # post-framing plan adequacy (forward-only)
    BUDGET = "budget"  # LLM-call/tool ceiling adjudication (G4)


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


# ---------------------------------------------------------------------------
# BUDGET gate — ceiling adjudication before each dispatch round (G4)
# ---------------------------------------------------------------------------


def evaluate_budget_gate(
    *,
    tool_rounds_used: int,
    dispatched_in_pass: int,
    max_tool_rounds: int,
    max_dispatches_per_pass: int,
) -> GateVerdict:
    """Deterministically adjudicate the LLM-call/tool budgets before a round.

    The NUMBERS are config-owned (engine.config ceilings); the VERDICT is
    governance-owned (this module, same shape as the plan gate). The driver
    consults this verdict before every round — it never decides budget
    inline. Overflow is a finding that names what will not run (the caller
    moves the names to ``unexecuted``); it is a refusal of the WORK, not a
    terminal — the loop continues its closing semantics.

    Rules (evaluated in order):
    - rounds exhausted (``tool_rounds_used >= max_tool_rounds``): nothing
      further executes this cycle;
    - pass ceiling reached (``dispatched_in_pass >= max_dispatches_per_pass``):
      nothing further executes in THIS pass (a later pass may still run).

    Identical inputs always yield identical verdicts (pure).
    """
    reasons: list[str] = []
    facts: list[tuple[str, Any]] = [
        ("tool_rounds_used", tool_rounds_used),
        ("dispatched_in_pass", dispatched_in_pass),
        ("max_tool_rounds", max_tool_rounds),
        ("max_dispatches_per_pass", max_dispatches_per_pass),
    ]
    if tool_rounds_used >= max_tool_rounds:
        reasons.append(
            f"cycle tool-round budget exhausted "
            f"({tool_rounds_used}/{max_tool_rounds})"
        )
    if dispatched_in_pass >= max_dispatches_per_pass:
        reasons.append(
            f"pass dispatch ceiling reached "
            f"({dispatched_in_pass}/{max_dispatches_per_pass})"
        )
    return GateVerdict(
        gate=GateKind.BUDGET,
        status=GateStatus.REFUSED if reasons else GateStatus.PASSED,
        reasons=tuple(reasons),
        facts=tuple(facts),
    )
