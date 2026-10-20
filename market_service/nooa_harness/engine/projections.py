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

    out["loop_receipts"] = gates.project_closes(state)
    if controller is not None:
        # Traversal is FSM/telemetry-owned: the artifact view is derived at
        # write time, never a live state authority.
        out["loop_traversal"] = controller.loop_coverage()
        out["congruence"] = controller.congruence()
        out["governance_trace"] = controller.transition_trace()
    return out
