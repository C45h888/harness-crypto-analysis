"""Agentic output synthesis — COLLAPSE → DERIVE on the deterministic foundation.

The final loop's reasoning stage: the agent reads every independent vector of
the cycle, collapses them into a strict deterministically-validated object,
then derives the final artifact JSON from collapse + full material. The
deterministic transport (assembly post-pass, choice alignment, evidence
filter, verdict equality upstream) is untouched — synthesis may reword,
never re-decide.

Failure semantics (relaxed loop): a failed collapse or derive is a DEGRADE,
never a refusal and never terminal — the record is an
``engine.synthesis.degraded`` capability + a ``synthesis_note`` on
deterministic state (the surface a semantics-review agent reads), and the
cycle proceeds on the frame-backed path so a validated cycle is never
killed by its synthesis. No REFUSED composition receipt on a settling
cycle: a refusal record on a settled artifact is a false failure signal.

The object that ships is the agent's AUTHORED output — the engine
fact-checks it (``path_resolves`` / ``_assemble_authored``), completes
integral values from store truth (``complete_evidence`` — fill nulls,
correct mismatches, never touch wording), and compels the
gate data (``check_authored_output``). Values ride the ledger; words stay
the agent's.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import context
from .. import kb, receipts
from ..config import LOOP_PASS_BUDGET, SUMMARY_MIN_CHARS
from ..loop_states import LoopStep, NestedLoop, SubLoop, ReceiptDisposition

log = logging.getLogger(__name__)

COLLAPSE_VECTORS: tuple[str, ...] = (
    "verdict", "base", "chain", "proof", "frame", "directive", "gate",
)

# Relaxed-loop soft floor (OUTPUT GROUND): thin summaries are quality
# findings against the P4 target; only a stub summary blocks the ship.
OUTPUT_SUMMARY_FLOOR = 80


def path_resolves(frame: Any, path: Any) -> bool:
    """Does a cited path resolve against the deterministic frame? (pure)

    Head-level authority: a real tool home from THIS run, a
    ``deterministic_state`` key, or the ledger/digest namespaces. Agent
    memory heads NEVER resolve — memory is context, never evidence.
    """
    head = str(path or "").split("→")[0].strip()
    if not head:
        return False
    if head.startswith("memory."):
        return False
    # Refusal findings are citable under null discipline — they carry a
    # reason string, never a number, and the strict gate already demands
    # they be recorded (a refusal is a finding, never a failure to repair).
    if "refusal" in str(path or ""):
        return True
    if head.split(".")[0] in ("deterministic_state", "ledger", "digest"):
        return True
    if not isinstance(frame, dict):
        return False
    return (head in set(frame.get("citable_heads") or ())
            or head in set(frame.get("state_keys") or ()))


# Evidence shape codes — ONE authority shared by the strict gate
# (``validation.validate_final_turn``) and the shipping filter
# (``output._assemble_authored``). The two used to disagree: assembly dropped
# empty/unbacked paths but never checked ``value``; the gate demanded
# path+value but never checked backing. Entries with a good path and a null
# value sailed through assembly and died at the gate — before assembly could
# run. One classifier, two actions (block vs drop+record).
EVIDENCE_OK = "ok"
EVIDENCE_NO_PATH = "no_path"
EVIDENCE_NULL_VALUE = "null_value"
EVIDENCE_UNBACKED = "unbacked"
EVIDENCE_NO_READING = "no_reading"

#: Codes that make an entry UNSHIPPABLE (removal-only doctrine).
EVIDENCE_DROP_CODES = frozenset(
    {EVIDENCE_NO_PATH, EVIDENCE_NULL_VALUE, EVIDENCE_UNBACKED})
#: Codes that make the strict VALIDATION gate refuse the cycle.
EVIDENCE_BLOCK_CODES = frozenset(
    {EVIDENCE_NO_PATH, EVIDENCE_NULL_VALUE, EVIDENCE_NO_READING})


def classify_evidence(entry: Any, frame: Any = None) -> str:
    """Classify one evidence entry against the doctrine (pure).

    ``frame=None`` is the gate's view: it judges shape only (path + value +
    reading), because backing is assembly's fact-check and the gate has no
    frame. With a frame, unbacked heads (unknown tools, ``memory.*``) are
    classified too. Refusal findings (``→ refusal``) legally carry a null
    value under null discipline.
    """
    if not isinstance(entry, dict):
        return EVIDENCE_NO_PATH
    path = str(entry.get("path") or "").strip()
    if not path:
        return EVIDENCE_NO_PATH
    if entry.get("value") is None and "refusal" not in path:
        return EVIDENCE_NULL_VALUE
    if frame is not None and not path_resolves(frame, path):
        return EVIDENCE_UNBACKED
    if not str(entry.get("interpretation") or "").strip():
        return EVIDENCE_NO_READING
    return EVIDENCE_OK


def _frame_value_is_null(frame: Any, path: Any) -> bool:
    """Does the frame report the cited field as null? (null discipline)."""
    if not isinstance(frame, dict):
        return False
    text = str(path or "").strip()
    known = set(frame.get("known_nulls") or ())
    return text in known or text.split("→")[0].strip() in known


def _decimal_norm(value: Any) -> str | None:
    """Canonical compare key: Decimal text when numeric, stripped text else."""
    if value is None:
        return None
    try:
        from decimal import Decimal
        return str(Decimal(str(value)))
    except Exception:
        text = str(value).strip()
        return text or None


def complete_evidence(
    evidence: Any,
    accumulated: dict[str, Any] | None,
    frame: Any | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    """Deterministic completion over one evidence list (pure).

    The completion half of the locked doctrine (engine completes, never
    kills, model-derived state): integral entries — paths resolving via
    ``resolve_basis_path`` to an accumulated tool field — have null values
    FILLED and mismatched values CORRECTED from store truth (wording
    untouched); non-integral entries (empty/unresolvable paths, non-dicts)
    are DROPPED with record. Refusal findings pass through (null
    discipline). Entries missing only their interpretation are KEPT —
    readings are the agent's to write; repair owns them, completion never
    invents them. Returns ``(entries, filled, corrected, dropped)`` with
    ``filled:<path>`` / ``corrected:<path>`` markers for the audit trail.
    """
    from .chain import resolve_basis_path
    kept: list[dict[str, Any]] = []
    filled: list[str] = []
    corrected: list[str] = []
    dropped: list[str] = []
    if not isinstance(evidence, list):
        return kept, filled, corrected, dropped
    acc = accumulated if isinstance(accumulated, dict) else {}
    has_truth = bool(acc)
    for entry in evidence:
        if not isinstance(entry, dict):
            dropped.append("<non-object>")
            continue
        path = str(entry.get("path") or "").strip()
        if not path:
            dropped.append("<empty>")
            continue
        if "refusal" in path:
            kept.append(dict(entry))
            continue
        if not has_truth:
            # No store to complete from (frame-only callers, tests): keep
            # frame-backed entries exactly as today's assembly does; the
            # gate judges values downstream.
            if frame is not None and not path_resolves(frame, path):
                dropped.append(path)
                continue
            kept.append(dict(entry))
            continue
        head, _field, truth = resolve_basis_path(path, acc)
        if _field is None or truth is None:
            # Unresolvable against the store: the echo check skips these
            # too, so completion cannot judge them. Keep valued entries
            # (preserve model-derived state — never kill what the gate
            # would pass); drop only valueless ones the gate would
            # refuse as unattributed.
            if entry.get("value") is None:
                dropped.append(path)
            else:
                kept.append(dict(entry))
            continue
        out = dict(entry)
        cited = entry.get("value")
        if cited is None:
            out["value"] = truth
            filled.append(f"filled:{path}")
        elif _decimal_norm(cited) != _decimal_norm(truth):
            out["value"] = truth
            corrected.append(f"corrected:{path}")
        kept.append(out)
    return kept, filled, corrected, dropped


def memory_context_block(accumulated: Any) -> str:
    """Relay of the agent's own memory.* reads — context, never evidence (pure)."""
    rows = {k: v for k, v in (accumulated or {}).items()
            if str(k).startswith("memory.")}
    if not rows:
        return ""
    return (
        "YOUR MEMORY (your own memory.* reads this cycle — context for the "
        "narrative, NEVER evidence; never cite these as evidence paths):\n"
        f"{json.dumps(rows, default=str)[:6_000]}\n\n"
    )


def render_frame_block(frame: Any) -> str:
    """Compact prompt rendering of the deterministic frame (pure)."""
    if not isinstance(frame, dict):
        return ""
    lines = [
        "DETERMINISTIC FRAME (the facts of THIS run — cite exactly these "
        "paths/values; memory.* is context, never evidence):",
        f"  citable homes: {', '.join(frame.get('citable_heads') or [])[:1_200]}",
        f"  vectors: {', '.join(str(v) for v in (frame.get('vectors') or ()))}",
    ]
    if frame.get("verdict_choice"):
        lines.append(
            f"  adjudicated choice (carried from the validation gate): "
            f"{frame.get('verdict_choice')}")
    if frame.get("delta_required"):
        lines.append(
            "  ΔP citation is REQUIRED this cycle (the directive bears "
            "targets/invalidations)")
    if frame.get("known_nulls"):
        lines.append(
            "  known-null fields (cite as null findings, never numbers): "
            f"{', '.join(frame.get('known_nulls') or [])[:400]}")
    return "\n".join(lines) + "\n\n"


def validate_collapse(obj: Any, frame: Any = None) -> tuple[bool, list[str]]:
    """Deterministic validation of the collapse object (pure).

    Strict schema: readings non-empty, each naming a vector with a path, a
    present value, and a meaning; contradictions a string list; direction
    non-empty. Returns (ok, reasons) — reasons ride the FSM-visible failure
    record, never a silent drop.

    With ``frame`` (OUTPUT's deterministic frame) the schema relaxes exactly
    where honesty requires it — this is the relaxed loop's collapse gate: a
    ``value`` may be null when the reading cites a refusal finding or the
    frame reports the field as null (null discipline: null = not-provided,
    never zero — a fake placeholder is worse than an honest null), every
    ``path`` must resolve against the frame's citable set (fact integrity),
    and the vector set comes from the frame, not a fixed allowlist. Without
    a frame the legacy strictness stands unchanged.
    """
    reasons: list[str] = []
    if not isinstance(obj, dict):
        return False, ["collapse must be a JSON object"]
    vectors = tuple((frame or {}).get("vectors") or ()) or COLLAPSE_VECTORS
    readings = obj.get("readings")
    if not isinstance(readings, list) or not readings:
        reasons.append("collapse.readings must be a non-empty list")
    else:
        for idx, reading in enumerate(readings):
            if not isinstance(reading, dict):
                reasons.append(f"collapse.readings[{idx}] must be an object")
                continue
            if reading.get("vector") not in vectors:
                reasons.append(
                    f"collapse.readings[{idx}].vector must be one of "
                    f"{list(vectors)} (got {reading.get('vector')!r})"
                )
            path = reading.get("path")
            if not str(path or "").strip():
                reasons.append(f"collapse.readings[{idx}].path required")
            elif frame is not None and not path_resolves(frame, path):
                reasons.append(
                    f"collapse.readings[{idx}].path not citable in the frame "
                    f"({str(path)[:80]!r}) — cite this run's tool/ledger/state homes"
                )
            if reading.get("value") is None:
                honest_null = ("refusal" in str(path or "")
                               or _frame_value_is_null(frame, path))
                if not honest_null:
                    reasons.append(
                        f"collapse.readings[{idx}].value required (never null)")
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


def collapse_coverage_gaps(obj: Any, frame: Any) -> list[str]:
    """Frame vectors neither read nor explicitly ``not_applicable`` (advisory).

    The strict schema policed shape; coverage is the honest question it never
    asked. Gaps ride the derive prompt as information, never as a rejection.
    """
    if not isinstance(frame, dict):
        return []
    declared = [str(v) for v in (frame.get("vectors") or ())]
    read = {str((r or {}).get("vector"))
            for r in (obj or {}).get("readings") or [] if isinstance(r, dict)}
    waived = {str(v) for v in (obj or {}).get("not_applicable") or []}
    return [v for v in declared if v not in read and v not in waived]


def check_authored_output(
    authored: Any, frame: Any, *,
    scenario: Any = None,
    scenario_refused: bool = False,
    final_pass: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """The RELAXED output gate on the AUTHORED object (pure).

    Returns ``(hard, findings, conflicts)``:

    - ``hard`` — the gate-data compulsion + fact integrity; blocks until the
      correction turn has had its chance, then ships FLAGGED (never rewritten)
    - ``findings`` — quality observations that ride the artifact, never block
    - ``conflicts`` — carried-semantics mismatches (adjudicated choice) that
      on the final pass are recorded, never rewritten: the agent's words
      ship, flagged

    The strict VALIDATION gate is untouched — its semantics ride here as
    compulsion on the object that SHIPS, not as re-judgement of a draft.
    """
    hard: list[str] = []
    findings: list[str] = []
    conflicts: list[str] = []
    if not isinstance(authored, dict):
        return (["authored output must be one JSON object"], findings, conflicts)
    # — gate-data compulsion: the adjudication vector carries —
    hypothesis = authored.get("hypothesis")
    if not isinstance(hypothesis, dict) or not str(hypothesis.get("H0") or "").strip():
        hard.append(
            "hypothesis.H0 required: frame H0/H1 (gate data — the output must carry it)")
    probs = authored.get("hypothesis_probabilities")
    if not (isinstance(probs, dict)
            and str(probs.get("chosen") or "") in ("H0", "H1")):
        hard.append(
            "hypothesis_probabilities required with chosen=H0|H1: the "
            "adjudicated choice carried from the validation gate must appear")
    else:
        choice = (frame or {}).get("verdict_choice") if isinstance(frame, dict) else None
        if choice in ("H0", "H1") and probs.get("chosen") != choice:
            item = (
                f"hypothesis_probabilities.chosen must equal the adjudicated "
                f"choice {choice!r} (ledger verdict-chosen — the agent carries "
                "the choice, never re-decides it)")
            (conflicts if final_pass else hard).append(item)
    # — summary: the floor is hard, the P4 target is a finding (relaxed) —
    summary_text = str(authored.get("summary") or "").strip()
    if len(summary_text) < OUTPUT_SUMMARY_FLOOR:
        hard.append(
            f"summary required (≥{OUTPUT_SUMMARY_FLOOR} chars): say what the "
            "fits show AND why it is happening now")
    elif len(summary_text) < SUMMARY_MIN_CHARS:
        findings.append(
            f"summary thin ({len(summary_text)}/{SUMMARY_MIN_CHARS}): the P4 "
            "target is the gate floor — worth the space")
    # — evidence: ≥1 frame-backed citation (fact integrity) —
    raw_evidence = authored.get("evidence")
    evidence = raw_evidence if isinstance(raw_evidence, list) else []
    backed = [e for e in evidence
              if isinstance(e, dict) and path_resolves(frame, e.get("path"))]
    if not backed:
        hard.append(
            "evidence must cite ≥1 frame-backed path from this run (a real "
            "tool/ledger/deterministic home — memory.* is never evidence)")
    else:
        roots = {str(e.get("path") or "").split("→")[0].strip() for e in backed}
        if len(roots) < 2:
            findings.append(
                f"single-root evidence ({sorted(roots)}): multi-root citation "
                "is the target")
    for idx, entry in enumerate(evidence):
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("interpretation") or "").strip():
            findings.append(
                f"evidence[{idx}] has no interpretation: the reading is what "
                "grounds the citation")
        if (entry.get("value") is None
                and "refusal" not in str(entry.get("path") or "")):
            findings.append(
                f"evidence[{idx}] cites a null value: name the null field, "
                "never invent a number")
    # — ΔP compulsion is CONDITIONAL: only when the directive bears targets —
    if (frame or {}).get("delta_required") and not any(
        str(e.get("path") or "").split("→")[0].strip()
        in ("calc.price.delta", "calc.derived_diagnostic")
        for e in backed
    ):
        hard.append(
            "evidence must cite the derived ΔP (calc.price.delta → …): the "
            "directive bears targets/invalidations")
    # — scenario discipline (carried semantics: refusal is a finding, never
    # fabricated; the verdict obeys the deterministic derivation) —
    if scenario is not None:
        scen = authored.get("scenario")
        if not isinstance(scen, dict):
            hard.append(
                "scenario block required: a scenario was given — return the "
                "scenario object with its verdict")
        else:
            verdict = str(scen.get("verdict") or "")
            if scenario_refused and verdict != "unevaluable":
                hard.append(
                    "scenario.verdict must be 'unevaluable': the tool refused "
                    "deterministically (null discipline — reachable/"
                    "not_reachable are forbidden without tool data)")
            if (not scenario_refused) and verdict not in (
                    "reachable", "not_reachable", "unevaluable"):
                hard.append(
                    "scenario.verdict required: reachable|not_reachable|unevaluable")
            if len(str(scen.get("rationale") or "").strip()) < 80:
                findings.append(
                    "scenario.rationale thin (≥80 chars is the target): name "
                    "the cause beside the verdict")
    return hard, findings, conflicts


def compose_collapse_prompt(
    *,
    task: str | None,
    verdict_rows: list[dict[str, Any]],
    base_summary: dict[str, Any] | None,
    chain_line: str,
    frame_steer: str,
    gate_line: str,
    frame_block: str = "",
) -> str:
    """The COLLAPSE turn: read every vector, emit the strict object (pure).

    The relaxed-loop window: the agent may return ``memory.*`` tool_calls
    (its own memory reads — context, never evidence); every other tool is
    dropped before dispatch.
    """
    return (
        "OUTPUT SYNTHESIS — COLLAPSE PASS (return ONLY one JSON object, no "
        "prose; you MAY return memory.* tool_calls — your own memory reads, "
        "context only — every other tool is dropped). Read every independent "
        "vector below and collapse them into the strict object. You decide "
        "NOTHING here (choice, verdict, and values are frozen upstream) — you "
        "read, note contradictions, and state where the vectors point.\n\n"
        f"{frame_block}"
        f"TASK: {(task or '(autonomous)')[:500]}\n\n"
        f"VECTORS:\n"
        f"verdict_rows: {json.dumps(verdict_rows, default=str)[:6_000]}\n"
        f"base_summary: {json.dumps(base_summary or {}, default=str)[:2_000]}\n"
        f"chain: {chain_line[:2_000]}\n"
        f"frame: {frame_steer[:3_000]}\n"
        f"gate: {gate_line[:1_000]}\n\n"
        "EMIT EXACTLY:\n"
        '{"readings": [{"vector": "<one of the frame vectors>", '
        '"path": "<tool> → data.<field> | ledger.<kind>.<stage> | digest.<key>", '
        '"value": <exact cited value (null ONLY for a refusal finding or a '
        'frame-null field)>, "meaning": "<what this reading says>"}], '
        '"not_applicable": ["<frame vector with nothing to read, or omit>"], '
        '"contradictions": ["<where vectors disagree, or []>"], '
        '"direction": "<one sentence: where the vectors point>"}'
    )


def _note_synthesis_failure(
    st: Any, engine: Any, *, stage: str, reason: str,
) -> None:
    """FSM-visible synthesis DEGRADE (never silent, never terminal).

    Relaxed-loop semantics: a failed collapse/derive is a DEGRADE, not a
    refusal — the cycle settles and the record rides the surface a
    semantics-review agent works from. No REFUSED composition receipt is
    emitted on a settling cycle (a refusal record on a settled artifact is a
    false failure signal), and the capability is
    ``engine.synthesis.degraded`` (informational), never an error. The
    caller proceeds on the frame-backed path. Best-effort throughout.
    """
    degraded_to = ("derive-without-collapse" if stage == "collapse"
                   else "staged-final")
    try:
        st.capability_log.append({
            "capability": "engine.synthesis.degraded",
            "scope": {"symbol": engine.symbol, "venue": engine.venue},
            "result": "degraded",
            "detail": {"stage": stage, "reason": str(reason)[:500],
                       "degraded_to": degraded_to,
                       "disposition": "degraded"},
        })
    except Exception:
        pass
    try:
        # First failure wins the note (root cause for the review surface).
        if "synthesis_note" not in st.deterministic_state:
            st.deterministic_state["synthesis_note"] = {
                "stage": stage, "reason": str(reason)[:500],
                "degraded_to": degraded_to,
                "disposition": "degraded",
            }
    except Exception:
        pass


async def _one_call(
    engine: Any, st: Any, prompt: str,
) -> str | None:
    """One tool-free synthesis LLM call with pass accounting (never raises).

    Budget-honest: at most ``LOOP_PASS_BUDGET["output"]`` synthesis passes
    per cycle (collapse + derive + the one correction turn) — beyond that
    the caller degrades/flags without spending another turn.
    """
    try:
        if st.passes_per_loop.get("output", 0) >= LOOP_PASS_BUDGET["output"]:
            log.warning("output pass budget spent; no further synthesis call")
            return None
    except Exception:
        pass
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
    frame: Any = None,
    memory_block: str = "",
) -> dict[str, Any] | None:
    """COLLAPSE → DERIVE over the deterministic frame (relaxed loop).

    Returns the authored dict, or None when every path failed (the caller
    keeps the staged final). The agent's memory window (M1) lives here: the
    collapse turn may return read-only ``memory.*`` calls — dispatched, then
    relayed into the derive material as context, never evidence. Failures
    are FSM-visible per call site above.
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

    # — COLLAPSE (relaxed strict: frame-validated, memory window open) —
    collapse_raw = await _one_call(
        engine, st,
        compose_collapse_prompt(
            task=ctx.task, verdict_rows=verdict_rows,
            base_summary=base_summary if isinstance(base_summary, dict) else {},
            chain_line=chain_line, frame_steer=frame_steer, gate_line=gate_line,
            frame_block=render_frame_block(frame),
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
            # MEMORY WINDOW (M1, recall-only): the agent may pull its own
            # memory.* on the collapse turn — dispatched through the normal
            # governed seam; every non-memory call is dropped here.
            _calls = [c for c in (parsed.get("tool_calls") or [])
                      if isinstance(c, dict)]
            mem_calls = [c for c in _calls
                         if str(c.get("name") or "").startswith("memory.")]
            other_calls = [c for c in _calls
                           if not str(c.get("name") or "").startswith("memory.")]
            if other_calls:
                log.warning("synthesis collapse: dropped %d non-memory tool_calls",
                            len(other_calls))
            if mem_calls:
                try:
                    prev_current = getattr(st, "parsed_current", None)
                    st.parsed_current = {"phase": "P6", "tool_calls": mem_calls}
                    st.dispatched_in_pass = 0
                    await engine._dispatch_turn(ctx, st)
                    memory_block = (
                        memory_context_block(st.accumulated_tool_results)
                        or memory_block)
                except Exception:
                    log.exception(
                        "memory window dispatch failed; synthesizing without it")
                finally:
                    st.parsed_current = prev_current
            ok, reasons = validate_collapse(parsed, frame)
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
        gaps = collapse_coverage_gaps(collapse, frame)
        if gaps:
            collapse_block += (
                "GAPS (frame vectors you did not read — acknowledge them in "
                "limitations if still open):\n" + ", ".join(gaps) + "\n\n")
    derive_prompt = kb.compose_output_prompt(
        directive_block=directive_block,
        synthesis_material=material,
        accumulated_keys=accumulated_keys,
        traversal=traversal,
        chain_line=chain_line,
        collapse_block=collapse_block,
        frame_block=render_frame_block(frame),
        memory_block=memory_block,
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
    "OUTPUT_SUMMARY_FLOOR",
    "check_authored_output",
    "collapse_coverage_gaps",
    "compose_collapse_prompt",
    "memory_context_block",
    "path_resolves",
    "render_frame_block",
    "synthesize_final",
    "validate_collapse",
]
