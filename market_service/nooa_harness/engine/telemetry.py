"""Agentic-plane telemetry mirror — observational, never authoritative.

The agentic FSM (``AgenticLoopMembrane`` + ``CycleController``) has ALREADY
adjudicated every loop move by the time this module runs. Cadence was already
resolved by the pure ``engine.cadence`` resolver, offered to the membrane at
``ADOPT_CADENCE``, and adopted (or refused) onto the controller. Settlement
already happened on the durable artifact.

So this adapter records RECEIPTS, after the fact:

- ``agent_cadence``      — provenance + values of the cadence verdict the FSM
                           adjudicated (CLI-approved or refused-as-finding).
- ``agent_loop_transition`` — one event per FSM transition record already in
                           ``controller.transitions`` (before/after loop and
                           sub-loop, kind, verdict, reason).
- ``agent_cycle``        — the cycle's bounded outcome: terminal, gate,
                           artifact identity, durability receipt.

Hard boundaries (the charter this module cannot cross):

- The kernel NEVER resolves, binds, overrides, or schedules cadence — it reads
  the controller's adopted value.
- The kernel NEVER advances the board — it reads ``controller.transitions``.
- No prompt content, model prose, tool arguments, or raw evidence is emitted.
  Only bounded codes, verdicts, identities, and counters.
- A telemetry failure is surfaced on sink health and swallowed: the cycle
  already settled, and telemetry cannot un-settle it.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from market_service.telemetry_kernel import kernel_for_runtime
from market_service.telemetry_kernel.contracts import narration_cadence_state

log = logging.getLogger(__name__)

OWNER = "cycle_controller"
_MAX_REASON = 200
_MAX_REASONS = 8


def _cycle_ms(artifact: Any) -> int:
    """Deterministic cycle timestamp from the artifact's own clock.

    Wall-clock time here would make a re-emission of the same cycle produce a
    different ``event_id``, defeating the kernel's idempotency identity. The
    artifact's ``generated_at`` is fixed at cycle start, so retries collapse.
    """
    raw = str(getattr(artifact, "generated_at", "") or "")
    if not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int(parsed.timestamp() * 1000))


def _bounded(value: Any, limit: int = _MAX_REASON) -> str | None:
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip()
    if not text:
        return None
    return text[:limit]


def _artifact_id(artifact: Any) -> str:
    """The cycle's canonical identity across both output contracts.

    ``InferenceArtifact`` carried ``artifact_id``; ``CanonicalOutput`` carries
    ``run_id``. The telemetry trace view must join to whichever the runtime
    produced, so both are accepted rather than assumed.
    """
    for attribute in ("artifact_id", "run_id"):
        value = str(getattr(artifact, attribute, "") or "").strip()
        if value:
            return value
    return "unknown"


def _kernel_for_engine(engine: Any) -> Any | None:
    """One emitter per engine instance; None when the store cannot host one."""
    cached = getattr(engine, "_telemetry_kernel", None)
    if cached is not None:
        return cached
    try:
        kernel = kernel_for_runtime(
            getattr(engine, "store", None), getattr(engine, "postgres", None),
        )
    except Exception:  # telemetry construction cannot gate a cycle
        log.warning("telemetry kernel unavailable for engine", exc_info=True)
        return None
    try:
        engine._telemetry_kernel = kernel
    except (AttributeError, TypeError):
        # A slotted/frozen engine still gets a kernel — just not a cached one.
        log.debug("telemetry kernel not cacheable on engine")
    return kernel


def _cadence_state(capability_log: Any) -> dict[str, Any] | None:
    """The cadence adoption/refusal receipt, read from the capability log.

    ``engine.cadence`` (adopted) and ``engine.cadence.refused`` (finding) are
    the only two shapes the wake boundary writes. Both are already FSM
    adjudicated; this only restates them.
    """
    log_entries = capability_log or ()
    for entry in log_entries:
        if not isinstance(entry, dict):
            continue
        capability = str(entry.get("capability") or "")
        if capability == "engine.cadence":
            detail = entry.get("detail")
            detail = detail if isinstance(detail, dict) else {}
            return {
                "adopted": True,
                "provenance": "cli_approved",
                "interval_seconds": detail.get("interval_seconds"),
                "window_minutes": detail.get("window_minutes"),
                "horizon_ms": detail.get("horizon_ms"),
                "regime": detail.get("regime"),
                "verdict_reason": _bounded(entry.get("governance")),
            }
        if capability == "engine.cadence.refused":
            detail = entry.get("detail")
            detail = detail if isinstance(detail, dict) else {}
            reasons = detail.get("reasons")
            bounded_reasons = [
                text for text in (
                    _bounded(item) for item in (reasons or ())[:_MAX_REASONS]
                ) if text
            ]
            return {
                "adopted": False,
                "provenance": _bounded(detail.get("provenance"), 64),
                "refusal_reasons": bounded_reasons,
                "verdict_reason": _bounded(entry.get("governance")),
            }
    return None


def _persistence_state(cycle_meta: dict[str, Any]) -> dict[str, Any]:
    """Bounded durability receipt — outcomes and error KINDS, not error text."""
    raw = cycle_meta.get("persistence")
    raw = raw if isinstance(raw, dict) else {}
    final = raw.get("final")
    final = final if isinstance(final, dict) else {}
    return {
        "postgres": bool(raw.get("postgres")),
        "redis": bool(raw.get("redis")),
        "redis_skipped": bool(raw.get("redis_skipped")),
        "final_postgres": bool(final.get("postgres")),
        "error_kinds": sorted(
            key for key in raw if str(key).endswith("_error")
        )[:_MAX_REASONS],
    }


def _transition_state(record: Any) -> dict[str, Any]:
    return {
        "kind": _bounded(getattr(record, "kind", None), 64),
        "target": _bounded(getattr(record, "target", None), 64),
        "allowed": bool(getattr(record, "allowed", False)),
        "reason": _bounded(getattr(record, "reason", None)),
        "before_loop": _bounded(getattr(record, "before_loop", None), 64),
        "before_sub_loop": _bounded(getattr(record, "before_sub_loop", None), 64),
        "after_loop": _bounded(getattr(record, "after_loop", None), 64),
        "after_sub_loop": _bounded(getattr(record, "after_sub_loop", None), 64),
    }


async def emit_cycle_telemetry(
    engine: Any, ctx: Any, artifact: Any, cycle_meta: dict[str, Any] | None,
) -> None:
    """Mirror one settled agentic cycle. Never raises, never changes state."""
    kernel = _kernel_for_engine(engine)
    if kernel is None:
        return
    controller = getattr(ctx, "controller", None)
    if controller is None and artifact is None:
        return

    meta = cycle_meta if isinstance(cycle_meta, dict) else {}
    occurred_at_ms = _cycle_ms(artifact)
    artifact_id = _artifact_id(artifact)
    session_id = str(getattr(engine, "session_id", "") or "unscoped")
    wake = getattr(ctx, "wake", None)
    wake_id = _bounded(getattr(wake, "wake_id", None), 96)

    try:
        cadence_state = _cadence_state(getattr(ctx, "capability_log", ()))
        if cadence_state is not None:
            await kernel.emit_state(
                plane="agentic", owner=OWNER, subject_type="session",
                subject_id=session_id, event_type="agent_cadence",
                source_sequence=0, occurred_at_ms=occurred_at_ms,
                source_ref=wake_id,
                state=cadence_state,
            )

        transitions = tuple(getattr(controller, "transitions", ()) or ())
        for index, record in enumerate(transitions):
            await kernel.emit_state(
                plane="agentic", owner=OWNER, subject_type="cycle",
                subject_id=artifact_id, event_type="agent_loop_transition",
                source_sequence=index, occurred_at_ms=occurred_at_ms,
                source_ref=f"{wake_id or 'cycle'}:{index}",
                state=_transition_state(record),
            )

        terminal = _bounded(
            meta.get("terminal")
            or getattr(artifact, "terminal", None)
            or getattr(getattr(controller, "terminal", None), "value", None),
            64,
        )
        await kernel.emit_state(
            plane="agentic", owner=OWNER, subject_type="cycle",
            subject_id=artifact_id, event_type="agent_cycle",
            source_sequence=len(transitions), occurred_at_ms=occurred_at_ms,
            source_ref=wake_id,
            state={
                "terminal": terminal,
                "gate": _bounded(meta.get("gate"), 64),
                "status": _bounded(getattr(artifact, "status", None), 64),
                "durable": _persistence_state(meta),
                "wake_id": wake_id,
                "session_id": session_id,
                "llm_calls": meta.get("llm_calls"),
                "tool_rounds": meta.get("tool_rounds"),
                "repairs": meta.get("repairs"),
                "transitions": len(transitions),
                "denials": sum(
                    1 for r in transitions if not getattr(r, "allowed", False)
                ),
                "governance_denied": bool(meta.get("governance_denial")),
            },
        )
    except Exception:  # the cycle already settled; telemetry cannot undo it
        log.warning("agentic telemetry emission failed", exc_info=True)
        return
    if kernel.health().get("status") != "healthy":
        await kernel.publish_health(owner=OWNER)


async def emit_narration_heartbeat(
    engine: Any,
    *,
    attempt_id: str,
    turn_ref: str,
    prompt_chars: int,
    deadline_ms: int,
    bytes_in: int,
    elapsed_ms: int,
    outcome: str,
    cause: str | None = None,
    source_sequence: int = 0,
    correlation_id: str | None = None,
) -> bool:
    """Emit one Signal B cadence heartbeat. Never raises, never gates.

    Owner is ``narration_transport`` on the ``agentic`` plane; the subject
    is the narration attempt. Counts and shapes only — the contract refuses
    prompts and outputs. Returns True when the heartbeat projected.
    """
    kernel = _kernel_for_engine(engine)
    if kernel is None:
        return False
    try:
        state = narration_cadence_state(
            attempt_id=attempt_id, turn_ref=turn_ref,
            prompt_chars=prompt_chars, deadline_ms=deadline_ms,
            bytes_in=bytes_in, elapsed_ms=elapsed_ms,
            outcome=outcome, cause=cause,
        )
        result = await kernel.emit_state(
            plane="agentic", owner="narration_transport",
            subject_type="narration_turn", subject_id=str(attempt_id),
            event_type="agent_narration_cadence",
            source_sequence=max(0, int(source_sequence)),
            occurred_at_ms=int(time.time() * 1000),
            correlation_id=correlation_id,
            state=state,
        )
        return bool(result.projected)
    except Exception:
        log.warning("narration heartbeat emission failed", exc_info=True)
        return False


__all__ = ["emit_cycle_telemetry", "emit_narration_heartbeat"]
