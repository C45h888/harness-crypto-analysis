"""Permanent artifact projections over the engine record plane.

These are the legacy deterministic_state keys external consumers rely on.
They are DERIVED at artifact write (never live authorities) from the two
canonical homes: ledger rows (FINDING / VERDICT) and the carrier's own
working fields. Nothing here mutates engine behaviour.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import gates
from .ledger import LedgerKind


def _rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = state.get("ledger")
    return list(rows) if isinstance(rows, list) else []


def _findings(state: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        row.get("payload") or {}
        for row in _rows(state)
        if row.get("kind") == LedgerKind.FINDING.value
        and (row.get("payload") or {}).get("finding_kind") == kind
    ]


def _messages(state: Mapping[str, Any], kind: str) -> list[str]:
    return [str(payload.get("message") or "") for payload in _findings(state, kind)]


def build_artifact_state(
    state: Mapping[str, Any], controller: Any | None = None,
) -> dict[str, Any]:
    """Return a copy of ``state`` with every projected legacy key filled."""
    out: dict[str, Any] = dict(state)

    out["validation_findings"] = _messages(state, "validation")
    out["chain_gaps"] = _messages(state, "chain_gap")
    out["assembly_dropped"] = _messages(state, "assembly_dropped")

    out["principle_findings"] = [
        {"principle": (payload.get("detail") or {}).get("principle"),
         "detail": payload.get("message")}
        for payload in _findings(state, "principle")
    ]

    assessment = _messages(state, "assessment")
    if assessment:
        out["assessment_note"] = assessment[0]

    for payload in _findings(state, "synthesis"):
        detail = payload.get("detail") or {}
        message = str(payload.get("message") or "")
        reason = message.split("synthesis degraded:", 1)[-1].strip() or message
        out["synthesis_note"] = {
            "stage": detail.get("stage"),
            "reason": reason,
            "degraded_to": detail.get("degraded_to"),
            "disposition": "degraded",
        }
        break

    salvaged: list[Any] = []
    for payload in _findings(state, "evidence_salvaged"):
        salvaged.extend((payload.get("detail") or {}).get("dropped") or [])
    if salvaged:
        out["evidence_salvaged"] = salvaged

    for projected in ("conformance", "validation_issue", "failure_report"):
        for payload in _findings(state, projected):
            out[projected] = dict(payload.get("detail") or {})
            break

    # The canonical answer turn rides the conformance view (ledger truth —
    # recorded by output.run_output after the verbalization): the composer
    # renders answer_fallback beside the engine verdict line.
    for payload in _findings(state, "answer"):
        detail = payload.get("detail") or {}
        if "answer_fallback" in detail and isinstance(out.get("conformance"), dict):
            out["conformance"] = {
                **out["conformance"],
                "answer_fallback": detail["answer_fallback"],
            }
        break

    _plan = state.get("plan")
    if isinstance(_plan, dict):
        out["task_directive"] = _plan.get("directive")
        out["task_plan"] = _plan.get("task_plan")
        out["task_workflow"] = _plan.get("workflow")
        out["seeded_plan"] = _plan.get("seeded")

    _acq = state.get("acquisition")
    if isinstance(_acq, dict):
        out["wake"] = _acq.get("wake")
        out["capture_status"] = _acq.get("capture_status")
        out["microstructure_evidence"] = _acq.get("microstructure_evidence")
        out["data_quality"] = _acq.get("data_quality")

    _sf = state.get("scenario_facts")
    if isinstance(_sf, dict):
        out["scenario_contract"] = _sf.get("contract")
        out["scenario_evaluation"] = _sf.get("evaluation")
        out["scenario_divergence"] = _sf.get("divergence")
        out["scenario_digest"] = _sf.get("digest")

    _acq2 = state.get("acquisition")
    if isinstance(_acq2, dict):
        out["forecast_result"] = _acq2.get("forecast_result")
        out["forward_scenario"] = _acq2.get("forward_scenario")
        out["forward_scenario_note"] = _acq2.get("forward_scenario_note")
        out["forward_gate"] = _acq2.get("forward_gate")

    _u = state.get("understanding")
    if isinstance(_u, dict):
        out["comprehension"] = _u.get("comprehension")
        out["understanding_receipt"] = _u.get("receipt")

    _data = gates.latest_verdict(state, gates.GateKind.DATA)
    if _data:
        _facts = _data.get("facts") or {}
        out["gate"] = {"status": _facts.get("status"),
                       "reasons": _facts.get("reasons") or []}
    _plan_gate = gates.latest_verdict(state, gates.GateKind.PLAN)
    if _plan_gate:
        out["plan_gate"] = {k: v for k, v in _plan_gate.items()
                            if k in ("gate", "status", "passed", "disposition",
                                     "reasons", "facts")}

    for row in _rows(state):
        payload = row.get("payload") or {}
        if (row.get("kind") == LedgerKind.OBSERVATION.value
                and payload.get("stage") == "narration_tiers"):
            out["narration_tiers"] = list(payload.get("tiers") or ())
            break

    chain = out.get("statistical_chain")
    if isinstance(chain, dict) and "chain_halt" in chain:
        out["chain_halt"] = chain.get("chain_halt")
    if isinstance(chain, dict) and "base_summary" in chain:
        out["base_summary"] = chain.get("base_summary")

    out["loop_receipts"] = gates.project_closes(state)
    if controller is not None:
        # Traversal is FSM/telemetry-owned: the artifact view is derived at
        # write time, never a live state authority.
        out["loop_traversal"] = controller.loop_coverage()
        out["congruence"] = controller.congruence()
        out["governance_trace"] = controller.transition_trace()
    return out
