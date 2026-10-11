"""Cadence — the plan-bound read/calc cadence vocabulary (single owner).

Phase R1 (cadence-into-plan): the cadence constitution lived in four
places with no governance owner (config budgets, reasoning.py inline
decisions, driver ceilings, carrier counters). This module is the ONE owner
of the deterministic read/calc cadence vocabulary — the same charter shape
as ``runtime/horizons.py`` owns horizons and ``engine/gates.py`` owns gate
verdicts.

Phase G1 (governance-boundary pass): the resolver is VERDICT-shaped. A
silent default that maps unknown identities to authorized behavior is a
governance error by the charter's own rule, so the resolver now refuses
what it cannot bind — the FSM adjudicates the verdict at adoption.

OWNERSHIP SPLIT (the approved design):
  - THIS module owns the VOCABULARY (frozen allowed sets) and the PURE
    selection function. No I/O, no LLM, no imports from the runtime loop.
  - ``build_plan`` (task_directive) binds the resolved cadence onto the
    plan — the plan factor is the only writer.
  - The CLI IS the only cadence approval channel: a directive whose
    horizon was parsed from the CLI scenario (source == "cli") may bind a
    cadence; a prompt-parsed or LLM-proposed horizon NEVER binds one (the
    resolver records the unapproved-source refusal). The LLM cannot approve
    or supply cadence — dispose_assessment's CLI-authoritative rule already
    guarantees its proposals cannot override; now they cannot bind cadence
    either.
  - ``CycleController`` ADOPTS the plan's cadence (immutable field) — the
    controller is the sole semantic authority; the FSM adjudicates the
    adoption against this verdict (engine/fsm.py cadence governance).
  - The FSM/tool layer validates agent-side cadence args against the
    frozen sets (out-of-vocabulary values are refused with the supported
    set — never guessed).
  - Logging lands inside the wake-semantic boundary: the
    ``engine.cadence`` capability entry + ``understanding_receipt.cadence``
    (comprehension owns both).

Frozen vocabulary: ``interval_seconds`` {10, 15, 30} × ``window_minutes``
{15, 30, 60}. The plan-bound acquisition cadence is deterministic per plan
regime: native binds its resolved horizon; long binds the long-horizon
bridge horizon; the cycle cadence stays at the deterministic spine
defaults. The AGENT keeps per-tool cadence freedom inside the frozen sets
(existing arg contracts); the cycle cadence is what the deterministic
acquisition runs at, provenance-stamped so floor vs plan-bound is
distinguishable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from market_service.runtime.horizons import (
    LONG_HORIZONS_MS,
    NATIVE_HORIZONS_MS,
)

# --- Frozen cadence vocabulary (the ONE definition) ------------------------
INTERVAL_SECONDS: tuple[int, ...] = (10, 15, 30)
WINDOW_MINUTES: tuple[int, ...] = (15, 30, 60)

DEFAULT_INTERVAL_SECONDS = 10
DEFAULT_WINDOW_MINUTES = 30

#: Native horizon the acquisition binds when the plan carries no resolved
#: horizon (none/unsupported regime). Identity-imported from the horizon
#: charter owner — never a local literal.
DEFAULT_FORECAST_MS: int = NATIVE_HORIZONS_MS["5s"]


@dataclass(frozen=True)
class CadenceVerdict:
    """The resolver's verdict — the FSM adjudicates this at adoption.

    ``cadence`` is populated ONLY when ``bound`` — a refused verdict
    carries ``reasons`` and never a cadence payload, so a consumer cannot
    read a default out of a refusal.
    """

    bound: bool
    cadence: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    provenance: str = "unresolved"  # cli_approved | unapproved_source | unresolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "bound": self.bound,
            "cadence": dict(self.cadence),
            "reasons": list(self.reasons),
            "provenance": self.provenance,
        }


def resolve_cadence(
    plan: Mapping[str, Any] | None,
    *,
    directive_source: str | None = None,
) -> CadenceVerdict:
    """Resolve the plan-bound cadence as a VERDICT (pure, deterministic).

    Same inputs always produce the same verdict. Binding rules:

    - ``directive_source == "cli"`` — the CLI is the only cadence approval
      channel. The regime/horizon select the binding (native binds its
      resolved horizon; long binds the long-horizon bridge horizon; none
      binds the 5s native default — a documented default for an ABSENT
      identity, never a masked refusal).
    - any other source (prompt_parse, llm_proposal, missing) — REFUSED:
      the LLM cannot approve cadence, and a prompt-parsed horizon is not
      an approved cadence. ``bound=False`` with the unapproved-source
      reason; the FSM records the finding at adoption and the cycle runs
      at the deterministic floor instead.
    - a horizon outside the horizon charter (native not in
      NATIVE_HORIZONS_MS, long not in LONG_HORIZONS_MS) — REFUSED: unknown
      identities are never mapped to authorized behavior.
    """
    regime = (plan or {}).get("horizon_regime")
    horizon = (plan or {}).get("forecast_horizon_ms")
    regime = regime if isinstance(regime, str) else "none"

    if directive_source is not None and directive_source != "cli":
        return CadenceVerdict(
            bound=False,
            reasons=(
                f"cadence approval requires the CLI channel; directive "
                f"source {directive_source!r} cannot bind cadence",
            ),
            provenance="unapproved_source",
        )

    horizon_ms: int | None = None
    if regime == "native":
        if (
            isinstance(horizon, int) and not isinstance(horizon, bool)
            and horizon > 0
        ):
            if horizon not in NATIVE_HORIZONS_MS.values():
                return CadenceVerdict(
                    bound=False,
                    reasons=(
                        f"native horizon {horizon}ms is outside the horizon "
                        "charter (NATIVE_HORIZONS_MS) — refused",
                    ),
                    provenance="unresolved",
                )
            horizon_ms = horizon
        # native without a resolved horizon: absence, not overreach —
        # binds the documented 5s default with floor provenance.
        horizon_ms = horizon_ms or DEFAULT_FORECAST_MS
    elif regime == "long":
        if (
            isinstance(horizon, int) and not isinstance(horizon, bool)
            and horizon > 0
        ):
            if horizon not in LONG_HORIZONS_MS.values():
                return CadenceVerdict(
                    bound=False,
                    reasons=(
                        f"long horizon {horizon}ms is outside the horizon "
                        "charter (LONG_HORIZONS_MS) — refused",
                    ),
                    provenance="unresolved",
                )
            horizon_ms = horizon
        else:
            return CadenceVerdict(
                bound=False,
                reasons=(
                    f"long regime requires a resolved long-horizon ms "
                    f"binding; got {horizon!r} — refused",
                ),
                provenance="unresolved",
            )
    elif regime in ("none", "unsupported"):
        horizon_ms = DEFAULT_FORECAST_MS
    else:
        return CadenceVerdict(
            bound=False,
            reasons=(f"unknown horizon regime {regime!r} — refused",),
            provenance="unresolved",
        )

    return CadenceVerdict(
        bound=True,
        cadence={
            "interval_seconds": DEFAULT_INTERVAL_SECONDS,
            "window_minutes": DEFAULT_WINDOW_MINUTES,
            "horizon_ms": horizon_ms,
            "regime": regime,
        },
        provenance="cli_approved",
    )


def cadence_domain_line() -> str:
    """Render the frozen cadence domain for prompt arg-contract blocks."""
    intervals = "|".join(str(v) for v in INTERVAL_SECONDS)
    windows = "|".join(str(v) for v in WINDOW_MINUTES)
    return (
        f"cadence domain: interval_seconds {{{intervals}}}, "
        f"window_minutes {{{windows}}}"
    )


__all__ = [
    "INTERVAL_SECONDS",
    "WINDOW_MINUTES",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_WINDOW_MINUTES",
    "DEFAULT_FORECAST_MS",
    "CadenceVerdict",
    "resolve_cadence",
    "cadence_domain_line",
]