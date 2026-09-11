"""Narration interpretation — what the LLM said and what it means.

Semantic authority: PURE-FUNCTION SEMANTICS OVER LLM OUTPUT. Every
function here is deterministic and side-effect-free: given the same
LLM text + the same coverage state, it produces the same answer. No
DB, no Redis, no LLM client, no logger mutation of state. This makes
them cheap to unit-test in isolation (the test suite already exercises
them).

The split:

  - ``extract_json_object(raw)`` — pull the first JSON object out of an
    LLM text blob (handles raw JSON, fenced ```json blocks, brace-balanced
    embedded objects).
  - ``coerce_turn(parsed)`` — normalise a parsed turn: drop tool_calls
    without a registry name (they can never dispatch), accept ``tool`` as
    an alias for ``name``, wrap non-dict ``hypothesis`` in ``{"raw": …}``.
  - ``validate_final_turn(parsed, coverage, scenario)`` — the final gate:
    phase coverage, H0, ≥200-char summary, dual-root evidence, ΔP citation,
    scenario block rules.
  - ``scenario_verdict(scenario_result, capability_log, gate_status,
    gate_reasons)`` — deterministic reachability verdict from the
    accumulated ``calc.scenario.evaluate`` payload. Never from LLM text.
  - ``PHASE_GUIDANCE`` — per-phase steering fragments (P1→P6), appended
    to every follow-up turn.
  - ``next_uncovered_phase(coverage)`` — first required phase with no
    executed tool yet; then P6.

The validator and the scenario verdict are the heart of the cycle's
correctness; this module is where the contract enforcement lives.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import SUMMARY_MIN_CHARS

log = logging.getLogger(__name__)


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """Extract the first JSON object from LLM text (fenced or embedded)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # fenced block first
    if "```" in raw:
        for chunk in raw.split("```"):
            candidate = chunk.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue
    # first brace-balanced object
    start = raw.find("{")
    while start != -1:
        depth = 0
        for idx in range(start, len(raw)):
            if raw[idx] == "{":
                depth += 1
            elif raw[idx] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start:idx + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
                    break
        start = raw.find("{", start + 1)
    return None


def coerce_turn(parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize one narration turn: drop undispatchable tool_calls entries.

    Entries without a non-empty string ``name`` can never dispatch — drop
    them here (logged) instead of burning a tool round on tool.unknown.
    Raw-JSON fallbacks (structured output refused) frequently emit the key
    ``tool`` instead of ``name`` (proven live: a full 8-turn cycle with
    every call dropped); accept it as an alias, ``name`` winning on
    conflict. Missing/invalid ``args`` become {}. A non-dict hypothesis
    is wrapped.
    """
    if not isinstance(parsed, dict):
        return None
    calls = parsed.get("tool_calls")
    if calls is None:
        return parsed
    if not isinstance(calls, list):
        parsed["tool_calls"] = []
        return parsed
    kept: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if (not isinstance(name, str) or not name.strip()) and isinstance(
            call.get("tool"), str
        ) and call.get("tool").strip():
            name = call.get("tool")
            log.warning("coercing tool_call key 'tool' → 'name': %.120r", call)
        if not isinstance(name, str) or not name.strip():
            log.warning("dropping tool_call without a registry name: %.120r", call)
            continue
        args = call.get("args")
        kept.append({"name": name.strip(),
                     "args": dict(args) if isinstance(args, dict) else {}})
    parsed["tool_calls"] = kept
    hypothesis = parsed.get("hypothesis")
    if hypothesis is not None and not isinstance(hypothesis, dict):
        parsed["hypothesis"] = {"raw": hypothesis}
    return parsed


def validate_final_turn(
    parsed: dict[str, Any],
    coverage: dict[str, set[str]],
    scenario: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Phase-aware final gate: depth is structural, not advisory.

    A FINAL turn passes only when every required phase family has at least
    one executed tool (P1 OFI, P2 AD/fits, P3 market correlation, P5 paper
    + derived ΔP), the hypothesis carries H0, the P4 explanation meets the
    length floor, confidence is the contract enum, every evidence entry
    carries a non-empty interpretation, and evidence cites at least two
    distinct roots with at least one fresh tool result (not just
    deterministic_state). Returns (passed, missing[]) — missing drives
    the repair prompt.
    """
    from market_service.nooa_harness.inference import _REQUIRED_PHASES
    from market_service.runtime.contracts import normalize_confidence

    missing: list[str] = []
    for phase in _REQUIRED_PHASES:
        if not coverage.get(phase):
            missing.append(
                f"phase {phase} uncovered: execute its tools before finalizing "
                f"(covered so far: {sorted(coverage.get(phase) or [])})"
            )
    if not coverage.get("P6"):
        missing.append(
            "P6 output-generation turn required: synthesize the primary inference "
            "output from this run's reasoning (declare phase P6, no tools)"
        )
    hypothesis = parsed.get("hypothesis")
    if not isinstance(hypothesis, dict) or not str(hypothesis.get("H0") or "").strip():
        missing.append("hypothesis.H0 required: frame H0/H1 grounded in recalled paper facts")
    summary = parsed.get("summary") or ""
    if not isinstance(summary, str) or len(summary.strip()) < SUMMARY_MIN_CHARS:
        missing.append(
            f"P4 explanation too thin ({len(summary.strip())}/{SUMMARY_MIN_CHARS} chars): "
            "say what the fits show AND why it is happening now"
        )
    confidence = parsed.get("confidence")
    if confidence is not None and normalize_confidence(confidence) is None:
        missing.append(
            f"confidence must be one of low|medium|high (got {confidence!r}); "
            "hyphenated blends coerce conservatively (low-medium → low)"
        )
    roots: set[str] = set()
    evidence = parsed.get("evidence")
    if isinstance(evidence, list):
        for idx, entry in enumerate(evidence):
            if not isinstance(entry, dict):
                missing.append(f"evidence[{idx}] must be an object with path + interpretation")
                continue
            if not str(entry.get("interpretation") or "").strip():
                missing.append(
                    f"evidence[{idx}] missing interpretation: say what "
                    f"{entry.get('path')!r} shows (paths+values cited, readings empty is a violation)"
                )
            path = str(entry.get("path") or "")
            head = path.split("→")[0].strip().split(".")[0].strip()
            if head:
                roots.add(head)
    if len(roots) < 2 or not (roots - {"deterministic_state"}):
        missing.append(
            "evidence must cite ≥2 distinct roots including ≥1 fresh tool result "
            f"(got roots: {sorted(roots) or 'none'}" + ")"
        )
    delta_paths = [
        str(entry.get("path") or "")
        for entry in (evidence if isinstance(evidence, list) else [])
        if isinstance(entry, dict)
    ]
    if not any(
        head in ("calc.price.delta", "calc.derived_diagnostic")
        for path in delta_paths
        for head in [path.split("→")[0].strip()]
    ):
        missing.append(
            "evidence must cite the derived ΔP via a calc.price.delta → … path"
        )
    if scenario is not None:
        scen = parsed.get("scenario")
        if not isinstance(scen, dict):
            missing.append(
                "scenario block required: a scenario was given — return the "
                "scenario{target_price,horizon,direction,required_ofi,exceedance,"
                "probability,verdict,rationale} object evaluated via calc.scenario.evaluate"
            )
        else:
            if str(scen.get("verdict") or "") not in (
                    "reachable", "not_reachable", "unevaluable"):
                missing.append(
                    "scenario.verdict required: reachable|not_reachable|unevaluable"
                )
            if normalize_confidence(scen.get("probability")) is None:
                missing.append(
                    "scenario.probability required: low|medium|high "
                    "(qualitative read of the exceedance + band + tape quality)"
                )
            for field in ("required_ofi", "exceedance"):
                if not str(scen.get(field) or "").strip():
                    missing.append(
                        f"scenario.{field} required: echo the calc.scenario.evaluate "
                        f"→ … value, never compute it yourself"
                    )
            # Thin-tape context travels WITH the verdict: a 0% exceedance
            # without fit_status + window count reads as "impossible" when
            # it means "the tape couldn't speak". Both are deterministic
            # echoes, so the check is structural, never semantic.
            for field in ("fit_status", "n_windows_usable"):
                if not str(scen.get(field) or "").strip():
                    missing.append(
                        f"scenario.{field} required: echo the calc.scenario.evaluate "
                        f"→ … value so the verdict carries its tape context"
                    )
            rationale = scen.get("rationale")
            if not isinstance(rationale, str) or len(rationale.strip()) < 80:
                missing.append(
                    "scenario.rationale required (≥80 chars): name fit_status + "
                    "usable windows + r2 beside the verdict — a bare verdict "
                    "without its tape context is rejected"
                )
        if not any(
            head == "calc.scenario.evaluate"
            for path in delta_paths
            for head in [path.split("→")[0].strip()]
        ):
            missing.append(
                "evidence must cite the scenario evaluation via a "
                "calc.scenario.evaluate → … path"
            )
        hypothesis_sc = parsed.get("hypothesis")
        if (not isinstance(hypothesis_sc, dict)
                or not str(hypothesis_sc.get("H0") or "").strip()
                or not str(hypothesis_sc.get("H1") or "").strip()):
            missing.append(
                "scenario cycles frame TWO hypotheses: H0 (target NOT reachable) "
                "and H1 (target reachable), both non-empty"
            )
    return (not missing), missing


def scenario_verdict(
    scenario_result: dict[str, Any] | None,
    capability_log: list[dict[str, Any]],
    gate_status: str,
    gate_reasons: list[str],
) -> tuple[str, str]:
    """Deterministic reachability verdict from the scenario tool payload.

    Decided from the accumulated ``calc.scenario.evaluate`` result — never
    from LLM text. Band-aware cut points: max exceedance across the band
    == 0 → invalidated (unreachable even on the friendly edge); min
    exceedance ≥ 0.5 → validated (preponderance through the full band);
    anything between → inconclusive. The provisional gate always ceilings
    at inconclusive (reason carries the directional read); refusals and
    missing evaluations are inconclusive with the cause named.
    """
    entry: dict[str, Any] | None = None
    for row in reversed(capability_log):
        if isinstance(row, dict) and row.get("capability") == "calc.scenario.evaluate":
            entry = row
            break
    if not isinstance(scenario_result, dict):
        detail = (entry or {}).get("detail") or {}
        cause = detail.get("reason", "scenario tool never called") \
            if isinstance(detail, dict) else "scenario tool never called"
        return ("inconclusive",
                f"scenario unevaluated ({cause}); reachability undecided")
    direction = str(scenario_result.get("direction") or "?")
    target = str(scenario_result.get("target_price") or "?")
    windows = scenario_result.get("n_windows_usable")
    horizon = str(scenario_result.get("horizon") or "?")
    context = (f"{direction} {target} at {horizon} over {windows} usable windows")
    if gate_status == "provisional":
        exc = str(scenario_result.get("exceedance") or "?")
        return ("inconclusive",
                f"Gate provisional ({';'.join(gate_reasons)}); scenario reads "
                f"{context} at {exc} exceedance but held inconclusive — "
                f"promotion waits for a validated fit")
    if gate_status != "validated":
        return ("inconclusive", "; ".join(gate_reasons) or "gate not validated")
    rng = scenario_result.get("exceedance_range")
    try:
        edges = [Decimal(str(v)) for v in rng] if isinstance(rng, list) and len(rng) == 2 \
            else [Decimal(str(scenario_result.get("exceedance")))]
    except (InvalidOperation, ValueError, TypeError):
        return ("inconclusive", f"scenario exceedance unparseable ({context})")
    if max(edges) == 0:
        return ("invalidated",
                f"H0 holds: {context} at 0 exceedance across the full band — "
                f"required flow outside the observed regime")
    if min(edges) >= Decimal("0.5"):
        return ("validated",
                f"H1 holds: {context} at ≥0.5 exceedance through the full band")
    return ("inconclusive",
            f"flow regime straddles the requirement ({context}); "
            f"band-range exceedance undecided")


# Per-phase steering fragments — appended to follow-up prompts so each
# turn knows what the next uncovered phase demands. Window freedom is
# stated once here: the agent may vary interval_seconds (10/15/30) and
# window_minutes (15/30/60) in calc/fit tool args; deterministic code
# executes, the agent never recomputes.
PHASE_GUIDANCE: dict[str, str] = {
    "P1": ("PHASE P1 — OFI INFERENCE: call calc.ofi.intervals "
           "(and/or micro.ofi_intervals). Judge tape quality: n vs minimum, "
           "capture gaps, hetero flag. Verdict: is this OFI tape usable or degraded, and why. "
           "You may vary interval_seconds (10/15/30) and window_minutes (15/30/60) in tool args."),
    "P2": ("PHASE P2 — AD INFERENCE: call calc.depth.average + "
           "calc.observation.build (and/or micro.fit_beta, calc.fit.price_impact). "
           "Judge AD stability and observation count separately from OFI — never merge. "
           "You may vary interval_seconds/window_minutes in tool args."),
    "P3": ("PHASE P3 — CORRELATE (substrate.read is the PRIMARY evidence; market.read is context): "
           "first invoke >=1 substrate.* worker (substrate.tape/density/delta/... for a bounded "
           "fire-tick), THEN substrate.read IN THE SAME tool_calls array with invoke listed BEFORE read "
           "(dispatches run sequentially in order, so the read sees the fresh projection). "
           "An invoke is a REQUEST to the calculation plane, not a guaranteed compute: that plane "
           "applies its own cooldown gates and may decline. "
           "Judge freshness from age_ms in the compact projection, AGAINST THAT WORKER'S OWN CADENCE — "
           "large_print refreshes in ~60s, most workers ~120s, oi/technicals/volume_profile ~300s, "
           "migration ~900s — never one global threshold, or you will mislabel the slow workers as stale. "
           "available:false, fired:0, or invoked:false "
           "(cooldown-dormant) are FINDINGS — report them, never re-invoke the same worker in one cycle. "
           "An unreachable calculation plane is a FINDING of the same kind: report it and continue from "
           "substrate.read; never treat a failed invoke as a reason to retry or as neutral evidence. "
           "An empty upstream (no projections — capture down) is itself the finding: cite "
           "substrate.read -> substrates.<name>.available. "
           "Cross-validate P1/P2 against the warm plane and name agreements AND contradictions explicitly. "
           "market.read (snapshot) plus derivatives/keystone/wall histories are regime context, not the correlation verdict."),
    "P4": ("PHASE P4 — EXPLAIN: no new tools required. Write the synthesis: what the fits show "
           f"(≥{SUMMARY_MIN_CHARS} chars in summary) AND why it is happening now — regime, capture quality, "
           "flow/positioning drivers. Then proceed to P5."),
    "P5": ("PHASE P5 — DERIVE: call memory.recall_paper FIRST (ground H0/H1 in Cont 1011.6402 facts), "
           "then calc.price.delta (alias calc.derived_diagnostic) with an OFI value — scenario arg or "
           "latest-interval default — for the NUMERIC derived ΔP (route A direct + route B when c/λ exist, "
           "with 95% band). A refusal (insufficient fit) is a finding, not a failure: report it."),
    "P6": ("PHASE P6 — OUTPUT GENERATION (final): no tools. Synthesize the PRIMARY inference output "
           "strictly from this run's reasoning: H0/H1 verdict, numeric ΔP with band, regime explanation, "
           "confidence, limitations. Every numeric claim cites its tool path, including a "
           "calc.price.delta → … path for the ΔP. Return FINAL JSON: tool_calls=[], full summary/evidence/"
           "confidence/limitations/model_separation/hypothesis{H0,H1,paper_refs,evidence_refs}."),
}


def next_uncovered_phase(coverage: dict[str, set[str]]) -> str:
    """First required phase family with no executed tool yet; then P6."""
    from market_service.nooa_harness.inference import _REQUIRED_PHASES

    for phase in _REQUIRED_PHASES:
        if not coverage.get(phase):
            return phase
    if not coverage.get("P6"):
        return "P6"
    return "P5"


__all__ = [
    "extract_json_object",
    "coerce_turn",
    "validate_final_turn",
    "scenario_verdict",
    "PHASE_GUIDANCE",
    "next_uncovered_phase",
]
