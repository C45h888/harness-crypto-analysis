"""Task directive — the prompt becomes a plan variable (deterministic).

Pure module: no I/O, no LLM. Parses the user's interactive-plane prompt
into ONE frozen directive object that the plan architecture consumes, with
refusal semantics (what cannot be parsed is null + a recorded finding —
never guessed). The LLM may ASSESS the prompt and PROPOSE amendments at
the primitive intake stage; this module's ``dispose_assessment`` validates
every proposal against the frozen vocabularies and the engine binds only
what survives. The LLM never amends the track itself (controller doctrine).

Deterministic plane contracts consumed by the plan binding:
- native horizons   FORWARD_HORIZONS_MS = (1s, 5s, 30s, 60s)
- long horizons     SCENARIO_HORIZONS   = {15m, 1h, 4h}
Anything between (e.g. "5m") is regime=unsupported with the reason
recorded — never silently downgraded.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

DIRECTIVE_SCHEMA_VERSION = 1

# Frozen horizon vocabularies (mirror the deterministic plane; kept in sync
# by test, by value — the plane remains the source of truth at runtime).
NATIVE_HORIZONS_MS: dict[str, int] = {
    "1s": 1_000, "5s": 5_000, "30s": 30_000, "60s": 60_000,
}
LONG_HORIZONS_SECONDS: dict[str, int] = {"15m": 900, "1h": 3_600, "4h": 14_400}
LONG_HORIZONS_MS: dict[str, int] = {
    key: seconds * 1_000 for key, seconds in LONG_HORIZONS_SECONDS.items()
}

KINDS = ("price_target", "hypothesis", "general")

# Deterministic prompt-side vocabulary (price-target verbs are shared with
# kb.PRICE_TARGET_HINTS semantics; horizon words are parsed structurally).
_TARGET_VERBS = ("target", "hit", "reach", "touch", "break above",
                 "break below", "take-profit", "take profit", "tp")
_STOP_VERBS = ("stop", "invalidation", "invalidate", "abort",
               "cut below", "cut above", "inval")
_HYPOTHESIS_WORDS = ("hypothes", "h0", "h1")

_PRICE_TOKEN = re.compile(r"[$]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_HORIZON_TOKENS = (
    # (regex, canonical key) — word-boundary anchored, case-insensitive.
    (re.compile(r"\b1\s*s(?:ec(?:ond)?s?)?\b", re.I), "1s"),
    (re.compile(r"\b5\s*s(?:ec(?:ond)?s?)?\b", re.I), "5s"),
    (re.compile(r"\b30\s*s(?:ec(?:ond)?s?)?\b", re.I), "30s"),
    (re.compile(r"\b60\s*s(?:ec(?:ond)?s?)?\b", re.I), "60s"),
    (re.compile(r"\ba\s+minute\b|\bone\s+minute\b|\bnext\s+minute\b", re.I), "60s"),
    (re.compile(r"\b15\s*m(?:in(?:ute)?s?)?\b", re.I), "15m"),
    (re.compile(r"\b1\s*h(?:ou?rs?)?\b|\bone\s+hour\b|\bnext\s+hour\b", re.I), "1h"),
    (re.compile(r"\b4\s*h(?:ou?rs?)?\b|\bfour\s+hours\b", re.I), "4h"),
)
# Any number+duration-shape mention — used to DETECT horizon language the
# frozen vocabulary does not support ("5 minutes", "90s") so it is
# recorded as an unsupported-horizon refusal instead of silently ignored
# (and instead of letting its numeral bind as a price).
_GENERIC_HORIZON = re.compile(
    r"\b([0-9]+)\s*(s(?:ec(?:ond)?s?)?|m(?:in(?:ute)?s?)?|h(?:ou?rs?)?)\b", re.I)


def _normalize_horizon(number: str, unit: str) -> str | None:
    """Map a generic duration mention to a canonical key, else None."""
    unit = unit.lower()
    if unit.startswith("h"):
        return f"{number}h" if number in ("1", "4") else None
    if unit.startswith("m"):
        return "15m" if number == "15" else None
    return f"{number}s" if number in ("1", "5", "30", "60") else None


def _parse_price(token: str) -> Decimal | None:
    """Parse a price-like token; None when not a positive Decimal (refusal)."""
    cleaned = token.replace(",", "").replace("$", "").strip()
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


def _resolve_horizon(raw: str) -> tuple[str, int | None, str | None]:
    """Resolve one horizon token → (regime, resolved_ms, refusal_reason).

    Frozen vocabularies only: native {1s,5s,30s,60s}, long {15m,1h,4h}.
    Anything else is (unsupported, None, reason) — recorded, never guessed.
    """
    key = raw.strip().lower()
    if key in NATIVE_HORIZONS_MS:
        return "native", NATIVE_HORIZONS_MS[key], None
    if key in LONG_HORIZONS_MS:
        return "long", LONG_HORIZONS_MS[key], None
    return "unsupported", None, f"horizon {raw!r} outside native/long vocabulary"


def _extract_horizon(blob: str) -> tuple[str | None, list[dict[str, Any]]]:
    """First horizon mention wins; later mentions are recorded, not applied.

    Detection is two-layer: frozen-vocabulary tokens resolve directly; a
    generic number+duration mention outside the vocabulary is returned as
    its raw text so ``_resolve_horizon`` records the unsupported-horizon
    refusal (never silently ignored, never guessed into a supported value).
    """
    refusals: list[dict[str, Any]] = []
    canonical_found: list[str] = []
    for pattern, canonical in _HORIZON_TOKENS:
        if pattern.search(blob):
            canonical_found.append(canonical)
    generic_matches = list(_GENERIC_HORIZON.finditer(blob))
    if canonical_found:
        for extra in canonical_found[1:]:
            refusals.append({"field": "horizon", "value": extra,
                             "reason": "multiple horizon mentions; first wins "
                                      f"({canonical_found[0]}) — not applied"})
        return canonical_found[0], refusals
    if generic_matches:
        first = generic_matches[0]
        mapped = _normalize_horizon(first.group(1), first.group(2))
        for extra in generic_matches[1:]:
            refusals.append({"field": "horizon", "value": extra.group(0),
                             "reason": "multiple horizon mentions — not applied"})
        if mapped is not None:
            return mapped, refusals
        return first.group(0), refusals  # unsupported: resolve records it
    return None, []


def _mask_horizon_spans(blob: str) -> str:
    """Blank out horizon-token spans so their numerals never bind as prices.

    "next 60 seconds" must anchor a horizon, not a $60 target. The masked
    copy is used ONLY for price extraction; the horizon extraction reads the
    original text.
    """
    masked = blob
    for pattern, _canonical in _HORIZON_TOKENS:
        masked = pattern.sub(lambda m: " " * len(m.group(0)), masked)
    masked = _GENERIC_HORIZON.sub(lambda m: " " * len(m.group(0)), masked)
    return masked


def _extract_prices(blob: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract target/invalidation price candidates from the task text.

    Conservative shape: a price token preceded (within one short window) by
    a stop verb becomes an invalidation; otherwise it is a target. Bare
    numbers without any anchoring keyword are ignored — a prompt must SPEAK
    a target to bind one. Never guesses.
    """
    targets: list[dict[str, Any]] = []
    invalidations: list[dict[str, Any]] = []
    lowered = blob.lower()
    window = 60  # chars before the number in which a verb anchors it
    for match in _PRICE_TOKEN.finditer(blob):
        raw = match.group(1)
        value = _parse_price(raw)
        if value is None:
            continue
        start = match.start()
        context = lowered[max(0, start - window):start]
        last_target_verb = max((context.rfind(v) for v in _TARGET_VERBS), default=-1)
        last_stop_verb = max((context.rfind(v) for v in _STOP_VERBS), default=-1)
        kind = "invalidation" if last_stop_verb > last_target_verb else "target"
        anchored = (last_target_verb >= 0 or last_stop_verb >= 0
                    or "$" in blob[start:match.start() + 1])
        if not anchored:
            continue  # bare number: not a directive, ignore (never guess)
        bucket = targets if kind == "target" else invalidations
        if not any(str(value) == entry["value"] for entry in bucket):
            bucket.append({"value": str(value), "kind": kind,
                           "provenance": f"prompt:char{start}"})
    return targets, invalidations


@dataclass(frozen=True)
class TaskDirective:
    """One frozen prompt directive — the variable the plan consumes."""

    kind: str
    horizon_value: str | None
    horizon_regime: str                      # native | long | unsupported | none
    resolved_horizon_ms: int | None
    targets: tuple[dict[str, Any], ...]      # {value, kind, provenance}
    invalidations: tuple[dict[str, Any], ...]
    hypothesis_seed: dict[str, Any] | None
    source: str                              # cli | prompt_parse | llm_proposal
    refusals: tuple[dict[str, Any], ...]
    parse_note: str
    schema_version: int = DIRECTIVE_SCHEMA_VERSION

    @property
    def has_targets(self) -> bool:
        return bool(self.targets or self.invalidations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "horizon": {"value": self.horizon_value,
                        "regime": self.horizon_regime,
                        "resolved_ms": self.resolved_horizon_ms},
            "targets": list(self.targets),
            "invalidations": list(self.invalidations),
            "hypothesis_seed": self.hypothesis_seed,
            "source": self.source,
            "refusals": list(self.refusals),
            "parse_note": self.parse_note,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskDirective":
        horizon = payload.get("horizon") or {}
        return cls(
            kind=str(payload.get("kind") or "general"),
            horizon_value=horizon.get("value"),
            horizon_regime=str(horizon.get("regime") or "none"),
            resolved_horizon_ms=horizon.get("resolved_ms"),
            targets=tuple(payload.get("targets") or ()),
            invalidations=tuple(payload.get("invalidations") or ()),
            hypothesis_seed=payload.get("hypothesis_seed"),
            source=str(payload.get("source") or "prompt_parse"),
            refusals=tuple(payload.get("refusals") or ()),
            parse_note=str(payload.get("parse_note") or ""),
            schema_version=int(payload.get("schema_version") or DIRECTIVE_SCHEMA_VERSION),
        )


def parse_task_directive(
    task: str | None, scenario: dict[str, Any] | None,
) -> TaskDirective:
    """Phase A — deterministic parse (pure). CLI scenario wins when present."""
    refusals: list[dict[str, Any]] = []
    if scenario:
        horizon_raw = str(scenario.get("horizon") or "")
        regime, resolved_ms, reason = (None, None, None)
        if horizon_raw:
            regime, resolved_ms, reason = _resolve_horizon(horizon_raw)
            if reason:
                refusals.append({"field": "horizon", "value": horizon_raw, "reason": reason})
        target_raw = scenario.get("target_price")
        targets: list[dict[str, Any]] = []
        if target_raw is not None:
            value = _parse_price(str(target_raw))
            if value is None:
                refusals.append({"field": "target", "value": str(target_raw),
                                 "reason": "scenario target_price unparseable"})
            else:
                targets.append({"value": str(value), "kind": "target",
                                "provenance": "cli:scenario"})
        invalidations: list[dict[str, Any]] = []
        stop_raw = scenario.get("invalidation_price")
        if stop_raw is not None:
            value = _parse_price(str(stop_raw))
            if value is None:
                refusals.append({"field": "invalidation", "value": str(stop_raw),
                                 "reason": "scenario invalidation_price unparseable"})
            else:
                invalidations.append({"value": str(value), "kind": "invalidation",
                                      "provenance": "cli:scenario"})
        kind = "price_target" if targets or invalidations else "general"
        return TaskDirective(
            kind=kind, horizon_value=horizon_raw or None,
            horizon_regime=regime or "none", resolved_horizon_ms=resolved_ms,
            targets=tuple(targets), invalidations=tuple(invalidations),
            hypothesis_seed=None, source="cli",
            refusals=tuple(refusals),
            parse_note="directive from CLI scenario dict",
        )
    if not task or not task.strip():
        return TaskDirective(
            kind="general", horizon_value=None, horizon_regime="none",
            resolved_horizon_ms=None, targets=(), invalidations=(),
            hypothesis_seed=None, source="prompt_parse", refusals=(),
            parse_note="no task: autonomous microstructure inference",
        )
    blob = task
    horizon_value, horizon_refusals = _extract_horizon(blob)
    refusals.extend(horizon_refusals)
    if horizon_value is not None:
        regime, resolved_ms, reason = _resolve_horizon(horizon_value)
        if reason:
            refusals.append({"field": "horizon", "value": horizon_value, "reason": reason})
    else:
        regime, resolved_ms = "none", None
    targets, invalidations = _extract_prices(_mask_horizon_spans(blob))
    lowered = blob.lower()
    if targets or invalidations:
        kind = "price_target"
    elif any(w in lowered for w in _HYPOTHESIS_WORDS):
        kind = "hypothesis"
    else:
        kind = "general"
    notes: list[str] = []
    if targets:
        notes.append(f"targets={[t['value'] for t in targets]}")
    if invalidations:
        notes.append(f"invalidations={[s['value'] for s in invalidations]}")
    if horizon_value is not None:
        notes.append(f"horizon={horizon_value} ({regime})")
    if not notes:
        notes.append("no directive values extracted")
    return TaskDirective(
        kind=kind, horizon_value=horizon_value,
        horizon_regime=regime, resolved_horizon_ms=resolved_ms,
        targets=tuple(targets), invalidations=tuple(invalidations),
        hypothesis_seed=None, source="prompt_parse",
        refusals=tuple(refusals), parse_note="; ".join(notes),
    )


# ---------------------------------------------------------------------------
# Phase B — LLM assessment disposal (engine validates, engine binds)
# ---------------------------------------------------------------------------

def dispose_assessment(
    directive: TaskDirective, assessment: Any,
) -> tuple[TaskDirective, list[dict[str, Any]]]:
    """Validate an LLM assessment proposal and bind only what survives.

    Returns (successor_directive, rejects[]). Every proposal field is
    checked against the frozen vocabularies; a rejected proposal is
    recorded — never applied. The LLM cannot add chain links, change gates,
    or invent horizons. CLI-sourced fields are NEVER overridden.
    """
    rejects: list[dict[str, Any]] = []
    if not isinstance(assessment, dict):
        return directive, [{"field": "assessment", "value": "non-dict",
                            "reason": "assessment must be a JSON object"}]
    horizon_value = directive.horizon_value
    regime, resolved_ms = directive.horizon_regime, directive.resolved_horizon_ms
    if directive.source == "cli":
        # CLI directive is authoritative; the assessment is recorded only.
        return directive, []
    proposed_horizon = assessment.get("proposed_horizon")
    if proposed_horizon is not None:
        p_regime, p_ms, p_reason = _resolve_horizon(str(proposed_horizon))
        if p_reason:
            rejects.append({"field": "proposed_horizon", "value": str(proposed_horizon),
                            "reason": p_reason})
        elif directive.horizon_value is None or directive.horizon_regime in ("none", "unsupported"):
            horizon_value, regime, resolved_ms = str(proposed_horizon), p_regime, p_ms
        elif str(proposed_horizon).strip().lower() != str(directive.horizon_value).strip().lower():
            rejects.append({"field": "proposed_horizon", "value": str(proposed_horizon),
                            "reason": f"conflicts with parsed horizon "
                                      f"{directive.horizon_value!r}; parsed wins"})

    def _bind_prices(proposals: Any, kind: str) -> tuple[tuple[dict, ...], list[dict]]:
        bound: list[dict[str, Any]] = []
        if proposals is None:
            return (), []
        if not isinstance(proposals, list):
            return (), [{"field": f"proposed_{kind}s", "value": "non-list",
                         "reason": "must be a list of price strings"}]
        existing = {e["value"] for e in (directive.targets if kind == "target"
                                         else directive.invalidations)}
        for raw in proposals:
            value = _parse_price(str(raw))
            if value is None:
                rejects.append({"field": f"proposed_{kind}s", "value": str(raw),
                                "reason": "unparseable price"})
            elif str(value) in existing:
                continue
            else:
                existing.add(str(value))
                bound.append({"value": str(value), "kind": kind,
                              "provenance": "llm_assessment"})
        return tuple(bound), rejects

    new_targets, _ = _bind_prices(assessment.get("proposed_targets"), "target")
    new_invalidations, _ = _bind_prices(assessment.get("proposed_invalidations"), "invalidation")

    kind = directive.kind
    if directive.source != "cli" and (new_targets or new_invalidations) and kind == "general":
        kind = "price_target"
    hypothesis_seed = directive.hypothesis_seed
    proposed_seed = assessment.get("hypothesis_seed")
    if isinstance(proposed_seed, dict) and hypothesis_seed is None:
        h0 = str(proposed_seed.get("H0") or "").strip()
        h1 = str(proposed_seed.get("H1") or "").strip()
        if h0 and h1:
            hypothesis_seed = {"H0": h0, "H1": h1,
                               "provenance": "llm_assessment"}
        else:
            rejects.append({"field": "hypothesis_seed", "value": "H0/H1 missing",
                            "reason": "both H0 and H1 are required to bind a seed"})
    elif proposed_seed is not None and not isinstance(proposed_seed, dict):
        rejects.append({"field": "hypothesis_seed", "value": "non-dict",
                        "reason": "must be an object with H0/H1"})
    notes = [directive.parse_note]
    if horizon_value != directive.horizon_value:
        notes.append(f"assessment bound horizon={horizon_value} ({regime})")
    if new_targets:
        notes.append(f"assessment bound targets={[t['value'] for t in new_targets]}")
    if new_invalidations:
        notes.append(f"assessment bound invalidations={[s['value'] for s in new_invalidations]}")
    if hypothesis_seed is not None and directive.hypothesis_seed is None:
        notes.append("assessment bound hypothesis seed")
    successor = TaskDirective(
        kind=kind, horizon_value=horizon_value, horizon_regime=regime,
        resolved_horizon_ms=resolved_ms,
        targets=directive.targets + new_targets,
        invalidations=directive.invalidations + new_invalidations,
        hypothesis_seed=hypothesis_seed,
        source=("llm_proposal" if (new_targets or new_invalidations
                                   or hypothesis_seed is not None
                                   or horizon_value != directive.horizon_value)
                else directive.source),
        refusals=directive.refusals + tuple(rejects),
        parse_note="; ".join(n for n in notes if n),
    )
    return successor, rejects


def build_plan(directive: TaskDirective) -> dict[str, Any]:
    """Bind the directive into the plan architecture (the plan factor).

    The plan selects required-ness and pre-acquisition; it never invents
    chain links. Track ownership stays with the chain module/controller.
    """
    from .kb import TASK_CHAINS
    kind = directive.kind if directive.kind in KINDS else "general"
    chain = list(TASK_CHAINS.get(kind, TASK_CHAINS["general"]))
    pre_acquire = ["calc.forward.forecast"]
    horizon_for_forecast = directive.resolved_horizon_ms
    horizon_note = None
    if directive.horizon_regime == "native" and directive.resolved_horizon_ms is not None:
        if horizon_for_forecast in (1_000, 5_000, 30_000, 60_000):
            pre_acquire.append("calc.forward.scenario")
        else:
            horizon_note = "native ms outside FORWARD_HORIZONS_MS; default forecast horizon"
    elif directive.horizon_regime == "long" and directive.has_targets:
        pre_acquire.append("calc.scenario.evaluate")
        if directive.resolved_horizon_ms is not None:
            horizon_for_forecast = None  # long regime: forecast stays native-default
    if directive.has_targets and directive.horizon_regime in ("native", "long"):
        if "calc.forward.scenario" not in pre_acquire and directive.horizon_regime == "native":
            pre_acquire.append("calc.forward.scenario")
    return {
        "schema_version": DIRECTIVE_SCHEMA_VERSION,
        "kind": kind,
        "horizon_regime": directive.horizon_regime,
        "resolved_horizon_ms": (
            directive.resolved_horizon_ms
            if directive.horizon_regime == "native" else None),
        "scenario_horizon": (
            directive.horizon_value
            if directive.horizon_regime == "long" else None),
        "targets": [dict(t) for t in directive.targets],
        "invalidations": [dict(s) for s in directive.invalidations],
        "chain": chain,
        "pre_acquire": pre_acquire,
        "directive_refusals": [dict(r) for r in directive.refusals],
        "notes": [n for n in (horizon_note,) if n],
    }


def assessment_prompt(task: str, directive: TaskDirective) -> str:
    """Render the Phase B assessment prompt (bounded, propose/dispose)."""
    return (
        "TASK ASSESSMENT (propose/dispose — the engine disposes):\n"
        "Assess the user's task below and propose, strictly within the "
        "frozen vocabularies, ONLY what the deterministic parse could not "
        "extract. Return ONE JSON object with EXACTLY {intent_assessment, "
        "proposed_horizon, proposed_targets, proposed_invalidations, "
        "hypothesis_seed, confidence}. proposed_horizon must be one of "
        "'1s','5s','30s','60s','15m','1h','4h' or null. Prices must be "
        "plain positive decimal strings. hypothesis_seed must be {H0, H1} "
        "or null. Proposing outside the vocabulary gets rejected and "
        "recorded — it never amends the plan.\n\n"
        f"USER TASK:\n{task[:4_000]}\n\n"
        "DETERMINISTIC PARSE (Phase A result):\n"
        f"{json.dumps(directive.to_dict(), default=str, indent=1)}\n"
    )
