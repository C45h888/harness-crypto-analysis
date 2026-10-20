"""C2 answer surface — the pure answer composer.

``docs/CLI_ANSWER_SURFACE_PLAN.md``: the cycle's deliverable is THE ANSWER —
the agent's authored prose with the gate data embedded as compact structured
blocks and an ``OUTPUT CONFORMANCE`` footer. This module is the ONE composer
both CLI surfaces render from (cycle-close emission + the
``nooa market inference answer`` re-render of a past run record), so the
format has a single source of truth.

Doctrine (binding):

- The composer RENDERS; it never authors. Every prose block is the agent's
  own words verbatim. A block the agent left empty renders
  ``missing (flagged)`` — the renderer never fills it.
- The composer's contract IS the compulsion list
  (``synthesize.check_authored_output`` hard tier): every hard-tier item
  appears as a block or as an explicit missing/refusal line. Structurally
  absent blocks (no scenario given; no directive targets) render
  ``not applicable …`` and never flag.
- The footer (``OUTPUT CONFORMANCE``) is never inline: hard_misses /
  conflicts / quality_findings / correction_used come from the conformance
  record; ``terminal`` is the ship state (``settled`` / ``flagged``) derived
  from that record plus the composer's own missing-block detection.
- Null discipline: a null value renders ``null`` — never zero, never a
  placeholder.

Pure: dicts in, string out. No I/O, no engine imports, no clock — the
run-record view resolves through the charter owner
(``runtime.contracts.run_record_of``), so both record shapes (canonical
rows + deprecated legacy) read as one.
"""

from __future__ import annotations

from typing import Any

from market_service.runtime.contracts import run_record_of

__all__ = [
    "MISSING",
    "NOT_APPLICABLE",
    "compose_answer",
    "compose_answer_structured",
    "answer_inputs_from_record",
    "answer_from_record",
    "render_answer",
]

#: The honest marker for a block the agent left empty (never filled).
MISSING = "missing (flagged)"

#: A block that does not apply to this run (never flagged).
NOT_APPLICABLE = "not applicable"

_RULE = "=" * 70
_DASH = "-" * 66


# ---------------------------------------------------------------------------
# Input assembly from the run record (pure) — the parity seam
# ---------------------------------------------------------------------------
#
# Both render points (cycle close and the ``answer`` subcommand) build the
# composer's four inputs from THE RUN RECORD through this one function, so
# re-rendering a past cycle is exact by construction.


def _text(value: Any) -> str | None:
    if value is None:
        return None
    as_str = str(value).strip()
    return as_str or None


def _citation(entry: Any) -> dict[str, Any] | None:
    """One evidence entry reduced to the composer's citation shape (pure)."""
    if not isinstance(entry, dict):
        return None
    return {
        "path": _text(entry.get("path")),
        "value": entry.get("value"),
        "interpretation": _text(entry.get("interpretation")),
    }


def _root(path: Any) -> str:
    return str(path or "").split("→")[0].strip()


def answer_inputs_from_record(
    record: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build ``(authored, gate_data, conformance, run_meta)`` from a run record.

    The record is the ``InferenceArtifact.to_dict()`` shape (the run record
    written to PG/Redis). Everything read here is engine-recorded state —
    nothing is derived from outside the record.
    """
    record = record if isinstance(record, dict) else {}
    interp = record.get("interpretation")
    interp = interp if isinstance(interp, dict) else {}
    ds = run_record_of(record).get("deterministic_state") or {}
    hypothesis = record.get("hypothesis")
    hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
    directive = ds.get("task_directive")
    directive = directive if isinstance(directive, dict) else {}
    conformance_record = ds.get("conformance")
    conformance_record = conformance_record if isinstance(conformance_record, dict) else {}

    evidence = [c for c in (_citation(e) for e in (interp.get("evidence") or []))
                if c is not None]
    scenario_block = interp.get("scenario")
    scenario_block = scenario_block if isinstance(scenario_block, dict) else {}

    scenario_cfg = ds.get("scenario")
    scenario_cfg = scenario_cfg if isinstance(scenario_cfg, dict) else {}
    scenario_contract = ds.get("scenario_contract")
    scenario_contract = scenario_contract if isinstance(scenario_contract, dict) else {}
    scenario_applicable = bool(scenario_cfg or scenario_contract or scenario_block)

    def _has(path: Any, *tokens: str) -> bool:
        text = str(path or "")
        return any(token in text for token in tokens)

    scenario_cited = [
        c for c in evidence
        if _has(c.get("path"), "calc.scenario.evaluate", "forward_scenario",
                "calc.forward.scenario")
    ]
    directive_cited = [
        c for c in evidence
        if _has(c.get("path"), "task_directive", "forward_scenario",
                "calc.forward.scenario", "calc.scenario.evaluate")
    ]
    delta_cited = [
        c for c in evidence
        if _root(c.get("path")) in ("calc.price.delta", "calc.derived_diagnostic")
    ]

    refusals: list[dict[str, str]] = []
    for refusal in (directive.get("refusals") or []):
        if isinstance(refusal, dict):
            refusals.append({
                "source": "directive.{}={}".format(
                    refusal.get("field") or "?", refusal.get("value") or "?"),
                "reason": _text(refusal.get("reason")) or "",
            })
    for citation in evidence:
        if "refusal" in str(citation.get("path") or ""):
            refusals.append({
                "source": str(citation.get("path") or ""),
                "reason": citation.get("interpretation") or "",
            })

    adjudication = interp.get("hypothesis_probabilities")
    adjudication = adjudication if isinstance(adjudication, dict) else None

    raw_limitations = interp.get("limitations")
    authored = {
        "summary": _text(interp.get("summary")),
        "evidence": evidence,
        "scenario_rationale": _text(scenario_block.get("rationale")),
        "model_separation": _text(interp.get("model_separation")),
        "limitations": [str(item) for item in raw_limitations
                        if _text(item) is not None]
        if isinstance(raw_limitations, (list, tuple)) else [],
    }
    gate_data = {
        "hypothesis": {"H0": _text(hypothesis.get("H0")),
                       "H1": _text(hypothesis.get("H1"))},
        "adjudication": adjudication,
        "scenario": {
            "applicable": scenario_applicable,
            "verdict": _text(scenario_block.get("verdict")),
            "target": _text(scenario_cfg.get("target_price")
                            or scenario_contract.get("target_price")
                            or scenario_contract.get("target")),
            "horizon": _text(scenario_cfg.get("horizon")
                             or scenario_contract.get("horizon")),
            "cited": scenario_cited,
            "rationale": _text(scenario_block.get("rationale")),
        },
        "directive": {
            "kind": _text(directive.get("kind")) or "general",
            "horizon": _text((directive.get("horizon") or {}).get("value"))
            if isinstance(directive.get("horizon"), dict) else None,
            "horizon_regime": _text((directive.get("horizon") or {}).get("regime"))
            if isinstance(directive.get("horizon"), dict) else None,
            "targets": [str(t.get("value")) for t in (directive.get("targets") or [])
                        if isinstance(t, dict)],
            "invalidations": [str(s.get("value"))
                              for s in (directive.get("invalidations") or [])
                              if isinstance(s, dict)],
            "parse_note": _text(directive.get("parse_note")),
            "answer": directive_cited,
        },
        "delta": {
            "required": bool(directive.get("targets") or directive.get("invalidations")),
            "cited": delta_cited,
        },
        "refusals": refusals,
        "confidence": _text(interp.get("confidence")),
    }
    conformance = {
        "hard_misses": [str(m) for m in (conformance_record.get("hard_misses") or [])],
        "conflicts": [str(c) for c in (conformance_record.get("conflicts") or [])],
        "quality_findings": [str(f) for f in
                             (conformance_record.get("quality_findings") or [])],
        "correction_used": bool(conformance_record.get("correction_used")),
    }
    run_meta = {
        "symbol": _text(record.get("symbol")),
        "venue": _text(record.get("venue")),
        "run_id": _text(record.get("run_id") or record.get("artifact_id")),
        "generated_at": _text(record.get("generated_at")),
        "session_id": _text(record.get("session_id")),
        "task": _text(ds.get("task")),
        "gate": _text(record.get("status")),
    }
    return authored, gate_data, conformance, run_meta


def answer_from_record(record: dict[str, Any] | None) -> str | None:
    """Render the answer of a past cycle from its run record (pure).

    Returns ``None`` when the record carries no AUTHORED ship (degraded /
    failed cycles have no authored answer — the renderer never authors one).
    """
    if not isinstance(record, dict) or record.get("interpretation") is None:
        return None
    ds = run_record_of(record).get("deterministic_state") or {}
    conformance = ds.get("conformance")
    conformance = conformance if isinstance(conformance, dict) else {}
    if conformance.get("tier") != "authored":
        return None
    authored, gate_data, conformance_in, run_meta = answer_inputs_from_record(record)
    return compose_answer(authored, gate_data, conformance_in, run_meta)


# ---------------------------------------------------------------------------
# The composer (pure)
# ---------------------------------------------------------------------------


def _missing(missing_blocks: list[str], name: str) -> str:
    missing_blocks.append(name)
    return MISSING


def compose_answer_structured(
    authored_interpretation: dict[str, Any] | None,
    gate_data: dict[str, Any] | None,
    conformance: dict[str, Any] | None,
    run_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Structured variant of the answer (pure) — same content, machine shape.

    ``conformance["missing_blocks"]`` names every block the agent left empty
    and ``conformance["terminal"]`` is the honest ship state (``flagged``
    whenever the gate flagged, a conflict was carried, or a block is
    missing).
    """
    authored = dict(authored_interpretation or {})
    gate = dict(gate_data or {})
    conf = dict(conformance or {})
    meta = dict(run_meta or {})
    missing_blocks: list[str] = []

    summary = _text(authored.get("summary"))
    if summary is None:
        summary = _missing(missing_blocks, "summary")
    evidence = [c for c in (authored.get("evidence") or []) if isinstance(c, dict)]
    evidence_note = None
    if not evidence:
        evidence_note = _missing(missing_blocks, "evidence")

    scenario = dict(gate.get("scenario") or {})
    if scenario.get("applicable"):
        scenario_rationale = _text(authored.get("scenario_rationale"))
        if scenario_rationale is None:
            scenario_rationale = _missing(missing_blocks, "scenario_rationale")
    else:
        scenario_rationale = f"{NOT_APPLICABLE} (no scenario given)"

    model_separation = _text(authored.get("model_separation"))
    if model_separation is None:
        model_separation = _missing(missing_blocks, "model_separation")
    limitations = list(authored.get("limitations") or [])
    if not limitations:
        limitations = [_missing(missing_blocks, "limitations")]

    hypothesis = dict(gate.get("hypothesis") or {})
    h0 = _text(hypothesis.get("H0"))
    if h0 is None:
        h0 = _missing(missing_blocks, "hypothesis.H0")
    h1 = _text(hypothesis.get("H1"))
    if h1 is None:
        h1 = _missing(missing_blocks, "hypothesis.H1")

    adjudication = gate.get("adjudication")
    if isinstance(adjudication, dict) and (
            _text((adjudication.get("H0") or {}).get("probability"))
            or _text((adjudication.get("H1") or {}).get("probability"))
            or _text(adjudication.get("chosen"))):
        adjudication_out = {
            "H0": dict(adjudication.get("H0") or {}),
            "H1": dict(adjudication.get("H1") or {}),
            "chosen": _text(adjudication.get("chosen")),
            "tiebreak": adjudication.get("tiebreak"),
        }
    else:
        adjudication_out = {"H0": {}, "H1": {}, "chosen": None, "tiebreak": None}
        adjudication_out["chosen"] = _missing(missing_blocks, "adjudication")

    if scenario.get("applicable"):
        scenario_out = {
            "verdict": _text(scenario.get("verdict"))
            or _missing(missing_blocks, "scenario.verdict"),
            "target": _text(scenario.get("target"))
            or _missing(missing_blocks, "scenario.target"),
            "horizon": _text(scenario.get("horizon"))
            or _missing(missing_blocks, "scenario.horizon"),
            "cited": [str(c.get("path") or "") for c in
                      (scenario.get("cited") or []) if isinstance(c, dict)]
            or [_missing(missing_blocks, "scenario.cited")],
        }
    else:
        scenario_out = {
            "verdict": f"{NOT_APPLICABLE} (no scenario given)",
            "target": None, "horizon": None, "cited": [],
        }

    directive = dict(gate.get("directive") or {})
    directive_answer = [c for c in (directive.get("answer") or [])
                        if isinstance(c, dict)]
    bears_targets = bool(directive.get("targets") or directive.get("invalidations"))
    directive_answer_note = None
    if not directive_answer:
        if bears_targets:
            directive_answer_note = _missing(missing_blocks, "directive answer")
        else:
            directive_answer_note = f"{NOT_APPLICABLE} (no targets bound)"

    delta = dict(gate.get("delta") or {})
    if delta.get("required"):
        delta_cited = [c for c in (delta.get("cited") or []) if isinstance(c, dict)]
        delta_note = None
        if not delta_cited:
            delta_note = _missing(missing_blocks, "delta")
        delta_out: dict[str, Any] | None = {
            "cited": delta_cited, "note": delta_note}
    else:
        delta_out = None  # ΔP block renders ONLY over a target-bearing directive

    confidence = _text(gate.get("confidence"))
    if confidence is None:
        confidence = _missing(missing_blocks, "confidence")

    hard_misses = [str(m) for m in (conf.get("hard_misses") or [])]
    conflicts = [str(c) for c in (conf.get("conflicts") or [])]
    quality_findings = [str(f) for f in (conf.get("quality_findings") or [])]
    correction_used = bool(conf.get("correction_used"))
    terminal = ("flagged" if (hard_misses or conflicts or missing_blocks)
                else "settled")

    return {
        "run": meta,
        "answer": {
            "summary": summary,
            "evidence": evidence,
            "evidence_note": evidence_note,
            "scenario_rationale": scenario_rationale,
            "model_separation": model_separation,
            "limitations": limitations,
        },
        "gate_data": {
            "hypothesis": {"H0": h0, "H1": h1},
            "adjudication": adjudication_out,
            "scenario": scenario_out,
            "directive": {
                "kind": directive.get("kind") or "general",
                "horizon": directive.get("horizon"),
                "horizon_regime": directive.get("horizon_regime"),
                "targets": [str(t) for t in (directive.get("targets") or [])],
                "invalidations": [str(s) for s in (directive.get("invalidations") or [])],
                "parse_note": directive.get("parse_note"),
                "answer": directive_answer,
                "answer_note": directive_answer_note,
            },
            "delta": delta_out,
            "refusals": [dict(r) for r in (gate.get("refusals") or [])
                         if isinstance(r, dict)],
            "confidence": confidence,
        },
        "conformance": {
            "hard_misses": hard_misses,
            "conflicts": conflicts,
            "quality_findings": quality_findings,
            "correction_used": correction_used,
            "missing_blocks": missing_blocks,
            "terminal": terminal,
        },
    }


def _value_line(citation: dict[str, Any], lines: list[str],
                indent: str = "") -> None:
    value = citation.get("value")
    lines.append(f"{indent}{citation.get('path') or '?'} = "
                 f"{'null' if value is None else value}")
    interpretation = _text(citation.get("interpretation"))
    lines.append(f"{indent}  "
                 f"{interpretation if interpretation is not None else MISSING}")


def _citations_block(citations: list[dict[str, Any]], note: str | None,
                     lines: list[str], indent: str = "") -> None:
    if note is not None:
        lines.append(f"{indent}{note}")
        return
    for citation in citations:
        _value_line(citation, lines, indent=indent)


def render_answer(structured: dict[str, Any]) -> str:
    """Render the structured answer as the plain-text CLI surface (pure)."""
    lines: list[str] = []
    meta = structured.get("run") or {}
    lines.append("ANSWER")
    lines.append(_RULE)
    lines.append("run: {} {} · {}".format(
        meta.get("symbol") or "?", meta.get("venue") or "?",
        meta.get("generated_at") or "?"))
    lines.append(f"run id: {meta.get('run_id') or '?'}")
    lines.append(f"task: {meta.get('task') or '(none)'}")
    lines.append("")

    answer = structured["answer"]
    lines.append("--- summary ---")
    lines.append(str(answer["summary"]))
    lines.append("")
    lines.append("--- evidence ---")
    _citations_block(answer["evidence"], answer.get("evidence_note"), lines)
    lines.append("")
    lines.append("--- scenario rationale ---")
    lines.append(str(answer["scenario_rationale"]))
    lines.append("")
    lines.append("--- model separation ---")
    lines.append(str(answer["model_separation"]))
    lines.append("")
    lines.append("--- limitations ---")
    for item in answer["limitations"]:
        lines.append(f"- {item}")
    lines.append("")

    gate = structured["gate_data"]
    lines.append("GATE DATA")
    lines.append(_RULE)
    lines.append("--- hypothesis ---")
    lines.append(f"H0: {gate['hypothesis']['H0']}")
    lines.append(f"H1: {gate['hypothesis']['H1']}")
    lines.append("")
    lines.append("--- adjudication ---")
    adjudication = gate["adjudication"]
    for side in ("H0", "H1"):
        row = adjudication.get(side) or {}
        probability = _text(row.get("probability"))
        p_value = row.get("p")
        basis = _text(row.get("basis_path"))
        lines.append(
            f"{side}: {probability if probability is not None else MISSING}"
            f" · p: {'null' if p_value is None else p_value}"
            f" · basis: {basis if basis is not None else MISSING}")
    tiebreak = adjudication.get("tiebreak")
    lines.append(f"chosen: {adjudication.get('chosen') or MISSING}"
                 f" · tiebreak: {'none' if tiebreak is None else tiebreak}")
    lines.append("")
    scenario = gate["scenario"]
    lines.append("--- scenario ---")
    lines.append(f"verdict: {scenario['verdict']}")
    if scenario.get("target") is not None or scenario.get("horizon") is not None:
        lines.append("target: {} · horizon: {}".format(
            scenario.get("target") or MISSING, scenario.get("horizon") or MISSING))
    for path in scenario.get("cited") or []:
        lines.append(f"cited: {path}")
    lines.append("")
    directive = gate["directive"]
    lines.append("--- directive ---")
    horizon = directive.get("horizon")
    regime = directive.get("horizon_regime")
    lines.append("kind: {} · horizon: {}".format(
        directive.get("kind") or "general",
        f"{horizon} ({regime})" if horizon else (regime or "none")))
    lines.append("targets: {}".format(
        ", ".join(directive.get("targets") or []) or "none"))
    lines.append("invalidations: {}".format(
        ", ".join(directive.get("invalidations") or []) or "none"))
    if directive.get("parse_note"):
        lines.append(f"note: {directive['parse_note']}")
    lines.append("answer:")
    _citations_block(directive.get("answer") or [], directive.get("answer_note"),
                     lines, indent="  ")
    lines.append("")
    if gate.get("delta") is not None:
        lines.append("--- delta (required by directive targets/invalidations) ---")
        _citations_block(gate["delta"].get("cited") or [],
                         gate["delta"].get("note"), lines)
        lines.append("")
    lines.append("--- refusals ---")
    refusals = gate.get("refusals") or []
    if refusals:
        for refusal in refusals:
            lines.append(f"- {refusal.get('source') or '?'}: "
                         f"{refusal.get('reason') or MISSING}")
    else:
        lines.append("none")
    lines.append("")
    lines.append("--- confidence ---")
    lines.append(str(gate["confidence"]))
    lines.append("")

    conf = structured["conformance"]
    lines.append("OUTPUT CONFORMANCE")
    lines.append(_RULE)
    for name in ("hard_misses", "conflicts", "quality_findings"):
        lines.append(f"{name}:")
        items = conf.get(name) or []
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("(none)")
    lines.append(f"correction_used: "
                 f"{'true' if conf.get('correction_used') else 'false'}")
    lines.append(f"terminal: {conf.get('terminal')}")
    return "\n".join(lines)


def compose_answer(
    authored_interpretation: dict[str, Any] | None,
    gate_data: dict[str, Any] | None,
    conformance: dict[str, Any] | None,
    run_meta: dict[str, Any] | None,
) -> str:
    """Compose the answer surface (pure): the agent's words + gate data +
    the OUTPUT CONFORMANCE footer. The composer renders — it never authors.
    """
    return render_answer(
        compose_answer_structured(
            authored_interpretation, gate_data, conformance, run_meta))
