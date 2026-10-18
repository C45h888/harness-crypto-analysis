"""Scenario contract — ONE frozen runtime authority for a price-target question.

The interaction plane hands the cycle a target/horizon. Before this module
that value existed in six places with six different beliefs: the CLI dict,
the directive targets, the plan's pre_acquire list, three prompt strings,
the controller field, and the validator's capability-log scan. This module
collapses the question into one immutable value, built ONCE at wake and
carried by the controller; every prompt surface, the dispatch carry, and
the validation oracle read the same shape.

Divergence (answering a different question) is a PROMPT-authored permission:

  forbidden  — the prompt asked this target; any other evaluated target is
               a validation finding.
  declared   — the prompt explicitly invites comparison/alternatives; the
               model may answer a different target only by declaring
               ``scenario_intent.mode="revision"`` (reason + basis), which
               the engine disposes like any other proposal.
  free       — reserved; never derived from a price-target directive.

Pure module: no I/O, no LLM, no engine imports (duck-typed controller reads
in ``render_status_block`` so the controller can depend on THIS module).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from market_service.runtime.contracts import SCHEMA_VERSION_REGISTRY
from market_service.runtime.horizons import LONG_HORIZONS_MS

SCENARIO_TOOL = "calc.scenario.evaluate"
# Horizon identity resolves to the tree-wide charter owner (by identity, not
# a local copy — the state-charter test pins this).
SCENARIO_HORIZONS = tuple(LONG_HORIZONS_MS)
ECHO_FIELDS = (
    "target_price", "horizon", "required_ofi", "exceedance",
    "fit_status", "n_windows_usable",
)
DIVERGENCE_MODES = ("forbidden", "declared", "free")
INTENT_MODES = ("as_asked", "revision", "refuse")
SCENARIO_CONTRACT_SCHEMA_VERSION = SCHEMA_VERSION_REGISTRY["scenario_contract"]

# Conservative prompt vocabulary: only an explicit invitation to compare or
# to answer an alternative authorizes divergence. The CLI --target flag with
# no task is `forbidden` by construction (nothing invited a second question).
_DIVERGENCE_HINTS = (
    "compare", "comparison", "alternative", "alternatively", "what if",
    "what-if", "versus", " vs ", "instead", "either target", "second target",
    "other target", "assess whether", "evaluate whether", "also evaluate",
    "both targets",
)


# ---------------------------------------------------------------------------
# Numeric canonicalization — comparison is Decimal, never string formatting
# ---------------------------------------------------------------------------

def canonical_number(value: Any) -> str | None:
    """Canonical decimal string for a price-like value; None when unparseable.

    ``"104.80"`` and ``"104.8"`` canonicalize to the same text, so execution
    keeps the model's formatting while validation/redundancy compare value.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not dec.is_finite() or dec <= 0:
        return None
    normalized = dec.normalize()
    # Decimal.normalize() can render exponents ("1E+2"); force plain digits.
    return format(normalized, "f")


def numbers_equal(left: Any, right: Any) -> bool:
    """True when both values parse to the same positive Decimal."""
    a, b = canonical_number(left), canonical_number(right)
    return a is not None and b is not None and a == b


def scenario_args_match(
    args: Mapping[str, Any] | None, target: Any, horizon: Any,
) -> bool:
    """Do dispatch args carry the bound target/horizon (numeric compare)?"""
    if not isinstance(args, Mapping):
        return False
    if not numbers_equal(args.get("target_price"), target):
        return False
    if horizon is not None and str(args.get("horizon") or "").strip() != str(horizon):
        return False
    return True


def args_equivalent(
    left: Mapping[str, Any] | None, right: Mapping[str, Any] | None,
) -> bool:
    """Args-aware equivalence with numeric canonicalization for price fields.

    Non-scenario keys compare by equality. ``target_price`` / ``horizon`` /
    ``invalidation_price`` compare by value so ``104.8`` and ``104.80`` are
    the SAME work (suppression must not depend on the model's formatting).
    """
    a = dict(left) if isinstance(left, Mapping) else {}
    b = dict(right) if isinstance(right, Mapping) else {}
    if set(a) != set(b):
        return False
    for key in a:
        if key == "target_price" or key == "invalidation_price":
            if not numbers_equal(a[key], b[key]):
                return False
        elif key == "horizon":
            if str(a[key] or "").strip() != str(b[key] or "").strip():
                return False
        elif a[key] != b[key]:
            return False
    return True


# ---------------------------------------------------------------------------
# The contract value
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScenarioContract:
    """The cycle's frozen price-target question + its divergence permission."""

    asked_target: str
    asked_horizon: str
    target_canonical: str
    source: str                       # cli | prompt | proposed
    allowed_targets: tuple[str, ...]  # canonical targets the prompt named
    divergence: str                   # forbidden | declared | free
    divergence_basis: str | None
    raw: Mapping[str, Any]
    contract_id: str
    schema_version: int = SCENARIO_CONTRACT_SCHEMA_VERSION

    # -- projections -----------------------------------------------------
    def to_scenario_dict(self) -> dict[str, Any]:
        """Backward-compatible scenario dict (ctx.scenario / controllers)."""
        out = dict(self.raw or {})
        out["target_price"] = self.asked_target
        out["horizon"] = self.asked_horizon
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "asked_target": self.asked_target,
            "asked_horizon": self.asked_horizon,
            "target_canonical": self.target_canonical,
            "source": self.source,
            "allowed_targets": list(self.allowed_targets),
            "divergence": self.divergence,
            "divergence_basis": self.divergence_basis,
        }

    def to_digest(self) -> dict[str, Any]:
        """Bounded prompt/envelope projection (no raw payload)."""
        return {
            "contract_id": self.contract_id,
            "asked_target": self.asked_target,
            "asked_horizon": self.asked_horizon,
            "divergence": self.divergence,
        }

    def with_allowed_targets(self, values: Any) -> "ScenarioContract":
        """Union directive/plan targets into the allowed set (pure)."""
        allowed = {self.target_canonical}
        allowed.update(self.allowed_targets)
        for value in values if isinstance(values, (list, tuple, set)) else ():
            canonical = canonical_number(value)
            if canonical is not None:
                allowed.add(canonical)
        if set(allowed) == set(self.allowed_targets):
            return self
        return replace(self, allowed_targets=tuple(sorted(allowed)))

    def permits(self, target: Any) -> bool:
        """Is ``target`` answerable without declaring a revision?"""
        canonical = canonical_number(target)
        if canonical is None:
            return False
        if canonical == self.target_canonical:
            return True
        return canonical in self.allowed_targets


def _divergence_policy(task: str | None) -> tuple[str, str | None]:
    blob = (task or "").lower()
    for hint in _DIVERGENCE_HINTS:
        if hint in blob:
            return "declared", hint.strip()
    return "forbidden", None


def build_contract(
    scenario: Mapping[str, Any] | None,
    task: str | None,
    *,
    source: str = "cli",
) -> ScenarioContract | None:
    """Build the frozen contract from the interaction-plane scenario.

    Returns ``None`` when no scenario was given — a general microstructure
    cycle has no price-target question and therefore no contract.
    """
    if not isinstance(scenario, Mapping):
        return None
    target_raw = scenario.get("target_price")
    if target_raw is None or not str(target_raw).strip():
        return None
    canonical = canonical_number(target_raw) or str(target_raw).strip()
    horizon = str(scenario.get("horizon") or "1h").strip()
    if horizon not in SCENARIO_HORIZONS:
        # Do not guess: the directive already records the refusal; the
        # contract keeps the asked text so validation can name it.
        horizon = horizon or "1h"
    divergence, basis = _divergence_policy(task)
    contract_id = hashlib.sha256(
        f"{canonical}|{horizon}|{source}".encode("utf-8")
    ).hexdigest()[:16]
    return ScenarioContract(
        asked_target=str(target_raw).strip(),
        asked_horizon=horizon,
        target_canonical=canonical,
        source=source,
        allowed_targets=(canonical,),
        divergence=divergence,
        divergence_basis=basis,
        raw=dict(scenario),
        contract_id=contract_id,
    )


# ---------------------------------------------------------------------------
# Prompt surfaces (contract + live status)
# ---------------------------------------------------------------------------

def render_contract_block(
    contract: ScenarioContract | None,
    plan: Mapping[str, Any] | None = None,
) -> str:
    """The opening-loop contract block (replaces the loose scenario prose)."""
    if contract is None:
        return ""
    plan = plan if isinstance(plan, Mapping) else {}
    allowed = ", ".join(contract.allowed_targets) or contract.target_canonical
    if contract.divergence == "declared":
        divergence = (
            "DECLARED — the prompt invites an alternative/comparison. You MAY "
            "answer a different target ONLY by declaring "
            "scenario_intent={target_price, horizon, mode:\"revision\", "
            "reason (>=20 chars), basis_path}. The engine disposes the "
            "declaration; an undeclared different target is a validation "
            "finding."
        )
    elif contract.divergence == "free":
        divergence = "FREE — any target in the allowed set may be answered."
    else:
        divergence = (
            "FORBIDDEN — answer the asked target. A different evaluated "
            "target is a validation finding; if you believe the question is "
            "wrong, do not silently re-scope it."
        )
    horizon_note = ""
    if plan.get("scenario_horizon"):
        horizon_note = (
            f"\n- plan long-horizon bridge: {plan.get('scenario_horizon')} "
            f"({plan.get('resolved_horizon_ms')}ms resolved)"
        )
    return (
        "SCENARIO CONTRACT (runtime-bound at wake — plan against it):\n"
        f"- asked target: {contract.asked_target} (canonical "
        f"{contract.target_canonical}) at horizon {contract.asked_horizon}\n"
        f"- contract_id: {contract.contract_id}; source: {contract.source}; "
        f"allowed targets: [{allowed}]{horizon_note}\n"
        f"- evaluation rule: {SCENARIO_TOOL} must be dispatched with the "
        "asked target/horizon. ABSENT args are carried from this contract "
        "before dispatch; a VALID explicit target executes as given but "
        "validation compares it to the bound target.\n"
        f"- divergence: {divergence}\n"
        "- declaration: return scenario_intent={target_price, horizon, mode: "
        "\"as_asked\"|\"revision\"|\"refuse\", reason, basis_path} every turn "
        "you work the scenario.\n"
        "- final echoes: target_price + horizon + required_ofi + exceedance + "
        "fit_status + n_windows_usable + verdict + rationale; a refusal is "
        f"cited as `{SCENARIO_TOOL} → refusal`.\n"
    )


def render_reminder(contract: ScenarioContract | None) -> str:
    """Compact one-line reminder for follow-up/repair prompts."""
    if contract is None:
        return ""
    return (
        f"SCENARIO CONTRACT reminder: asked {contract.asked_target} @ "
        f"{contract.asked_horizon} (contract {contract.contract_id}, "
        f"divergence {contract.divergence}). ABSENT args are carried; a "
        "different target requires a declared revision when permitted.\n"
    )


def render_status_block(
    contract: ScenarioContract | None, controller: Any,
) -> str:
    """Live scenario state for follow-up/repair/forced-final prompts.

    Duck-typed over the controller: ``scenario_state`` / ``effective_scenario``
    / ``scenario_outcome`` / ``scenario_intent`` / ``scenario_intent_rejects``.
    This is the model-visible counterpart of the artifact ``capability_log``
    carry rows — the model sees state, operators see the audit manifest.
    """
    if contract is None and controller is None:
        return ""
    if contract is None:
        legacy_scenario = getattr(controller, "scenario", None)
        if not isinstance(legacy_scenario, dict):
            return ""
    if contract is not None:
        asked_t, asked_h = contract.asked_target, contract.asked_horizon
    else:
        asked_t, asked_h = None, None
    effective = (None, None)
    state = "unknown"
    try:
        effective = controller.effective_scenario()
        state = controller.scenario_state().value
    except Exception:
        pass
    intent = getattr(controller, "scenario_intent", None)
    rejects = getattr(controller, "scenario_intent_rejects", ()) or ()
    outcome = None
    try:
        outcome = controller.scenario_outcome()
    except Exception:
        outcome = None
    last_args = getattr(outcome, "args", None) if outcome is not None else None
    lines = [
        "SCENARIO STATUS (model-visible runtime state):",
        f"- state: {state}; asked: {asked_t} @ {asked_h}; "
        f"effective bound: {effective[0]} @ {effective[1]}",
    ]
    if isinstance(last_args, dict):
        lines.append(
            "- last dispatch args: "
            + json.dumps(
                {k: last_args.get(k) for k in ("target_price", "horizon")
                 if k in last_args}, default=str)
        )
    if isinstance(intent, Mapping):
        lines.append("- declaration: " + json.dumps(dict(intent), default=str))
    if rejects:
        lines.append(
            "- rejected declarations: " + json.dumps(list(rejects)[-2:], default=str)
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "SCENARIO_CONTRACT_SCHEMA_VERSION",
    "DIVERGENCE_MODES",
    "ECHO_FIELDS",
    "INTENT_MODES",
    "SCENARIO_HORIZONS",
    "SCENARIO_TOOL",
    "ScenarioContract",
    "args_equivalent",
    "build_contract",
    "canonical_number",
    "numbers_equal",
    "render_contract_block",
    "render_reminder",
    "render_status_block",
    "scenario_args_match",
]
