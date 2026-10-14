"""Central ledger — the ONE deterministic home for engine-authored memory.

Every piece of ENGINE memory lives here, categorised by the COMMONALTY of the
information the entry holds (not by the write site):

  UNDERSTANDING — what the task IS (comprehension receipt + bound directive)
  PLAN          — what WILL be done (the disposed plan the loop is bound to)
  HANDOFF       — what was GATHERED (evidence coverage + explicit needs/gaps)
  OBSERVATION   — what was DERIVED (base/refined readings from tool results)

The ledger is engine-written, engine-read, and deterministic: entries append in
a fixed order and render BY KIND, never by similarity, recency or importance.
It lives on the chartered ``deterministic_state["ledger"]`` root, is persisted
verbatim on the artifact, and is the single source the LLM reads as context.

Rules:
  - It is CONTEXT, never evidence: it carries no coverage credit and never
    outranks fresh ledger data.
  - It is engine-authored: the agent never writes it.
  - It replaces the engine's use of the episodic MemoryNode; the agent's own
    memory is a separate surface (the NOOA memory skill).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

# Fixed render/append order — the ledger is read by kind, deterministically.
LEDGER_ROOT = "ledger"
_MAX_PAYLOAD_CHARS = 3_000


class LedgerKind(str, Enum):
    """What an entry HOLDS — the commonalty that separates ledger sections.

    Each kind groups information of one nature so a reader (the LLM or a
    deterministic consumer) can pull exactly the section it needs, in a fixed
    order, with no scoring.
    """

    UNDERSTANDING = "understanding"  # what the task is (comprehension)
    PLAN = "plan"                    # what will be done (disposed plan)
    HANDOFF = "handoff"              # what was gathered (coverage + gaps)
    OBSERVATION = "observation"      # what was derived (base/refined readings)


# The canonical section order (append + render).
LEDGER_ORDER: tuple[LedgerKind, ...] = (
    LedgerKind.UNDERSTANDING,
    LedgerKind.PLAN,
    LedgerKind.HANDOFF,
    LedgerKind.OBSERVATION,
)


@dataclass(frozen=True)
class LedgerEntry:
    """One immutable ledger row: its kind, append sequence, and bounded payload."""

    kind: LedgerKind
    seq: int
    payload: Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()
    segment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row = {
            "kind": self.kind.value,
            "seq": self.seq,
            "payload": dict(self.payload) if self.payload else {},
            "evidence_refs": list(self.evidence_refs),
        }
        if self.segment is not None:
            row["segment"] = self.segment
        return row


def record_entry(
    deterministic_state: dict[str, Any],
    kind: LedgerKind,
    payload: Mapping[str, Any],
    *,
    evidence_refs: Sequence[str] = (),
    segment: str | None = None,
) -> LedgerEntry:
    """Append one engine-authored entry to the central ledger (deterministic).

    Append order is the entry's ``seq``; there is no scoring, dedupe or
    importance. Oversized payloads are bounded, never silently dropped.
    """
    rows = list(deterministic_state.get("ledger") or [])
    entry = LedgerEntry(
        kind=kind,
        seq=len(rows),
        payload=_bounded(payload),
        evidence_refs=tuple(str(ref) for ref in evidence_refs),
        segment=segment,
    )
    rows.append(entry.to_dict())
    deterministic_state["ledger"] = rows
    return entry


def ledger_rows(
    deterministic_state: Mapping[str, Any],
    kinds: Sequence[LedgerKind] | None = None,
    *,
    segment: str | None = None,
) -> list[dict[str, Any]]:
    """The ledger rows, optionally filtered to the given kinds (fixed order)."""
    rows = deterministic_state.get(LEDGER_ROOT)
    if not isinstance(rows, list):
        return []
    if kinds is None:
        rows = list(rows)
    else:
        wanted = {k.value for k in kinds}
        rows = [row for row in rows if row.get("kind") in wanted]
    if segment is not None:
        rows = [row for row in rows if row.get("segment") == segment]
    return rows


def render_ledger(
    deterministic_state: Mapping[str, Any],
    kinds: Sequence[LedgerKind] | None = None,
    *,
    segment: str | None = None,
    budget: int = 3_000,
) -> str:
    """Render the central ledger as a compact, deterministic prompt block.

    Sections are emitted in ``LEDGER_ORDER`` (not write order across kinds) so
    the same state always renders the same text.
    """
    rows = ledger_rows(deterministic_state, kinds, segment=segment)
    if not rows:
        return ""
    lines = ["## Engine ledger (central, deterministic — context, never evidence)"]
    order = kinds or LEDGER_ORDER
    for kind in order:
        section = [r for r in rows if r.get("kind") == kind.value]
        if not section:
            continue
        lines.append(f"[{kind.value}]")
        for row in section:
            rendered = json.dumps(row.get("payload") or {}, default=str)
            lines.append(f"  - {rendered}")
    text = "\n".join(lines)
    if len(text) > budget:
        text = text[:budget].rstrip() + "\n…"
    return text


def _bounded(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Keep a payload small and JSON-safe (mark oversized rather than drop)."""
    if not payload:
        return {}
    try:
        rendered = json.dumps(dict(payload), default=str)
    except Exception:
        return {"unrenderable": True}
    if len(rendered) > _MAX_PAYLOAD_CHARS:
        return {"truncated": True, "chars": len(rendered)}
    return dict(payload)


__all__ = [
    "LEDGER_ORDER",
    "LEDGER_ROOT",
    "LedgerEntry",
    "LedgerKind",
    "ledger_rows",
    "record_entry",
    "render_ledger",
]
