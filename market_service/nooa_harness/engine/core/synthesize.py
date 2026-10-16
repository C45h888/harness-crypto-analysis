"""Agentic output synthesis — COLLAPSE → DERIVE on the deterministic foundation.

The final loop's reasoning stage: the agent reads every independent vector of
the cycle, collapses them into a strict deterministically-validated object,
then derives the final artifact JSON from collapse + full material. The
deterministic transport (assembly post-pass, choice alignment, evidence
filter, verdict equality upstream) is untouched — synthesis may reword,
never re-decide.

Failure semantics go to the FSM (never silent): a failed collapse or derive
records a REFUSED composition receipt + an ``engine.synthesis.failed``
capability + a ``synthesis_note`` on deterministic state (the surface a
semantics-review agent reads), then degrades to the legacy single-call
compose so a validated cycle is never killed by its synthesis.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import context
from .. import kb
from ..loop_states import LoopStep, NestedLoop, SubLoop
from .. import receipts
from ..receipts import ReceiptDisposition

log = logging.getLogger(__name__)

COLLAPSE_VECTORS: tuple[str, ...] = (
    "verdict", "base", "chain", "proof", "frame", "directive", "gate",
)


def validate_collapse(obj: Any) -> tuple[bool, list[str]]:
    """Deterministic validation of the collapse object (pure).

    Strict schema: readings non-empty, each naming an allowlisted vector with
    a path, a present value, and a meaning; contradictions a string list;
    direction non-empty. Returns (ok, reasons) — reasons ride the
    FSM-visible failure record, never a silent drop.
    """
    reasons: list[str] = []
    if not isinstance(obj, dict):
        return False, ["collapse must be a JSON object"]
    readings = obj.get("readings")
    if not isinstance(readings, list) or not readings:
        reasons.append("collapse.readings must be a non-empty list")
    else:
        for idx, reading in enumerate(readings):
            if not isinstance(reading, dict):
                reasons.append(f"collapse.readings[{idx}] must be an object")
                continue
            if reading.get("vector") not in COLLAPSE_VECTORS:
                reasons.append(
                    f"collapse.readings[{idx}].vector must be one of "
                    f"{list(COLLAPSE_VECTORS)} (got {reading.get('vector')!r})"
                )
            if not str(reading.get("path") or "").strip():
                reasons.append(f"collapse.readings[{idx}].path required")
            if reading.get("value") is None:
                reasons.append(f"collapse.readings[{idx}].value required (never null)")
            if not str(reading.get("meaning") or "").strip():
                reasons.append(f"collapse.readings[{idx}].meaning required")
    contradictions = obj.get("contradictions")
    if contradictions is not None and (
        not isinstance(contradictions, list)
        or not all(isinstance(c, str) for c in contradictions)
    ):
        reasons.append("collapse.contradictions must be a string list when present")
    if not str(obj.get("direction") or "").strip():
        reasons.append("collapse.direction required (where the vectors point)")
    return (not reasons), reasons


def compose_collapse_prompt(
    *,
    task: str | None,
    verdict_rows: list[dict[str, Any]],
    base_summary: dict[str, Any] | None,
    chain_line: str,
    frame_steer: str,
    gate_line: str,
) -> str:
    """The COLLAPSE turn: read every vector, emit the strict object (pure)."""
    return (
        "OUTPUT SYNTHESIS — COLLAPSE PASS (no tools available; return ONLY one "
        "JSON object, no prose). Read every independent vector below and collapse "
        "them into the strict object. You decide NOTHING here (choice, verdict, "
        "and values are frozen upstream) — you read, note contradictions, and "
        "state where the vectors point.\n\n"
        f"TASK: {(task or '(autonomous)')[:500]}\n\n"
        f"VECTORS:\n"
        f"verdict_rows: {json.dumps(verdict_rows, default=str)[:6_000]}\n"
        f"base_summary: {json.dumps(base_summary or {}, default=str)[:2_000]}\n"
        f"chain: {chain_line[:2_000]}\n"
        f"frame: {frame_steer[:3_000]}\n"
        f"gate: {gate_line[:1_000]}\n\n"
        "EMIT EXACTLY:\n"
        '{"readings": [{"vector": "verdict|base|chain|proof|frame|directive|gate", '
        '"path": "<tool> → data.<field> | ledger.<kind>.<stage> | digest.<key>", '
        '"value": <exact cited value>, "meaning": "<what this reading says>"}], '
        '"contradictions": ["<where vectors disagree, or []>"], '
        '"direction": "<one sentence: where the vectors point>"}'
    )


def _note_synthesis_failure(
    st: Any, engine: Any, *, stage: str, reason: str,
) -> None:
    """FSM-visible synthesis failure (never silent, never terminal).

    Records the REFUSED composition receipt + capability + deterministic-state
    note — the surface a semantics-review agent works from — then the caller
    degrades to the legacy single-call compose. Best-effort throughout.
    """
    try:
        receipts.emit_receipt(
            st, loop=NestedLoop.OUTPUT, sub_loop=SubLoop.COMPOSITION,
            disposition=ReceiptDisposition.REFUSED,
            steps=(LoopStep.COLLAPSE,) if stage == "collapse"
            else (LoopStep.DERIVE,),
            payload={"stage": stage, "reason": str(reason)[:300]},
        )
    except Exception:
        log.exception("synthesis failure receipt failed")
    try:
        st.capability_log.append({
            "capability": "engine.synthesis.failed",
            "scope": {"symbol": engine.symbol, "venue": engine.venue},
            "result": "error",
            "detail": {"stage": stage, "reason": str(reason)[:500],
                       "degraded_to": "single-call compose"},
        })
    except Exception:
        pass
    try:
        # First failure wins the note (root cause for the review surface);
        # every failure still lands a receipt + capability below.
        if "synthesis_note" not in st.deterministic_state:
            st.deterministic_state["synthesis_note"] = {
                "stage": stage, "reason": str(reason)[:500],
                "degraded_to": "single-call compose",
            }
    except Exception:
        pass


async def _one_call(
    engine: Any, st: Any, prompt: str,
) -> str | None:
    """One tool-free synthesis LLM call with pass accounting (never raises)."""
    try:
        raw = await engine._call_llm(prompt)
        context.record_tier(st)
    except Exception as exc:
        log.warning("synthesis call failed: %s", exc)
        return None
    try:
        st.llm_calls += 1
    except Exception:
        pass
    try:
        st.passes_per_loop["output"] = st.passes_per_loop.get("output", 0) + 1
    except Exception:
        pass
    return raw


async def synthesize_final(
    engine: Any, ctx: Any, st: Any,
    *,
    directive_block: str,
    synthesis_material: dict[str, Any],
    accumulated_keys: list[str],
    traversal: dict[str, Any],
    chain_line: str,
) -> dict[str, Any] | None:
    """COLLAPSE → DERIVE, degrading to legacy single-call compose.

    Returns the composed dict, or None when every path failed (the caller
    keeps the staged final). Failures are FSM-visible per call site above.
    """
    from ..schemas import coerce_turn, extract_json_object
    from .chain import build_hypothesis_frame as _bhf, hypothesis_steer as _hs

    material = dict(synthesis_material or {})
    verdict_rows = material.get("verdict_rows") or []
    base_summary = material.get("base_summary")
    gate = (st.deterministic_state or {}).get("gate") or {}
    try:
        _dir = getattr(st, "task_directive", None)
        frame_steer = _hs(_bhf(
            ctx.task, _dir if isinstance(_dir, dict) else None,
            st.accumulated_tool_results,
        ))
    except Exception:
        frame_steer = ""
    gate_line = f"status={gate.get('status')} reasons={list(gate.get('reasons') or [])}"

    # — COLLAPSE (strict, deterministically validated) —
    collapse_raw = await _one_call(
        engine, st,
        compose_collapse_prompt(
            task=ctx.task, verdict_rows=verdict_rows,
            base_summary=base_summary if isinstance(base_summary, dict) else {},
            chain_line=chain_line, frame_steer=frame_steer, gate_line=gate_line,
        ),
    )
    collapse: dict[str, Any] | None = None
    if collapse_raw is not None:
        parsed = None
        try:
            parsed = coerce_turn(extract_json_object(collapse_raw))
        except Exception:
            parsed = None
        if parsed is not None:
            ok, reasons = validate_collapse(parsed)
            if ok:
                collapse = parsed
            else:
                _note_synthesis_failure(
                    st, engine, stage="collapse",
                    reason="; ".join(reasons)[:500],
                )
        else:
            _note_synthesis_failure(st, engine, stage="collapse",
                                    reason="unparseable collapse turn")
    else:
        _note_synthesis_failure(st, engine, stage="collapse",
                                reason="collapse call transport failure")

    # — DERIVE (collapse + FULL material; collapse omission = degraded) —
    collapse_block = ""
    if collapse is not None:
        collapse_block = (
            "COLLAPSED VECTORS (the synthesis you performed — derive FROM this "
            "plus the full material below; reword, never re-decide):\n"
            f"{json.dumps(collapse, default=str)[:8_000]}\n\n"
        )
    derive_prompt = kb.compose_output_prompt(
        directive_block=directive_block,
        synthesis_material=material,
        accumulated_keys=accumulated_keys,
        traversal=traversal,
        chain_line=chain_line,
        collapse_block=collapse_block,
    )
    derive_raw = await _one_call(engine, st, derive_prompt)
    if derive_raw is None:
        _note_synthesis_failure(st, engine, stage="derive",
                                reason="derive call transport failure")
        return None
    try:
        derived = coerce_turn(extract_json_object(derive_raw))
    except Exception:
        derived = None
    if not isinstance(derived, dict):
        _note_synthesis_failure(st, engine, stage="derive",
                                reason="unparseable derive turn")
        return None
    if derived.pop("tool_calls", None):
        log.warning("synthesis derive: tool_calls dropped (non-agentic)")
    return derived


__all__ = [
    "COLLAPSE_VECTORS",
    "compose_collapse_prompt",
    "synthesize_final",
    "validate_collapse",
]
