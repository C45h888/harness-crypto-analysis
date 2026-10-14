"""Loop receipts — the canonical, engine-authored record of a loop close.

Every loop/sub-loop close emits an engine-authored ``LoopReceipt``: a frozen,
structural record of *what ran, what it covered, how it closed, with what
provenance*. A receipt answers *what happened*; it never carries narration or
model prose.

Authority contract:

  - Receipts are written to ``CycleRuntimeState.loop_receipts`` and mirrored
    into the chartered ``deterministic_state["loop_receipts"]`` root, so a
    cycle's traversal is replayable from the artifact.
  - Receipts are ENGINE-authored. The LLM never authors one, and no receipt is
    derived from narration text.
  - Receipts are NOT written into the agent's episodic memory node. The memory
    node belongs to the agent (it calls and writes it); the engine does not
    project its bookkeeping into it.
  - Emission is a deterministic side effect: it adds NO LLM calls.

This module intentionally does NOT feed the validation gate and does NOT alter
output synthesis; wiring those is a later pass (see docs/NESTED_LOOP_SPEC §8).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .loop_states import (
    LoopReceipt,
    LoopStep,
    NestedLoop,
    ReceiptDisposition,
    SubLoop,
)

# The chartered deterministic_state root the receipt vector lives under. Kept
# as a public constant for readers; the write below uses the literal key so the
# state-charter AST scan (tests/test_state_charter.py) can pin it.
RECEIPTS_ROOT = "loop_receipts"

_MAX_PAYLOAD_CHARS = 4_000


def emit_receipt(
    st: Any,
    *,
    loop: NestedLoop,
    sub_loop: SubLoop,
    disposition: ReceiptDisposition = ReceiptDisposition.COMPLETED,
    steps: Sequence[LoopStep] = (),
    payload: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[str] = (),
) -> LoopReceipt:
    """Record one loop/sub-loop close on the cycle's canonical receipt vector.

    ENGINE-authored and deterministic. ``payload`` carries bounded STRUCTURAL
    facts only — never narration.
    """
    receipt = LoopReceipt(
        loop=loop,
        sub_loop=sub_loop,
        disposition=disposition,
        steps=tuple(steps),
        payload=_bounded(payload),
        evidence_refs=tuple(str(ref) for ref in evidence_refs),
    )
    try:
        st.loop_receipts.append(receipt)
    except Exception:  # pragma: no cover — carrier always has the field
        pass
    rows = list(st.deterministic_state.get("loop_receipts") or [])
    rows.append(receipt.to_dict())
    st.deterministic_state["loop_receipts"] = rows
    return receipt


def append_receipt(
    deterministic_state: dict[str, Any], receipt: LoopReceipt,
) -> None:
    """Append a receipt to a state dict (for call sites without the carrier).

    The OUTPUT loop holds no ``CycleRuntimeState``; it shares the same state
    dict by reference, so this writes the same canonical vector.
    """
    rows = list(deterministic_state.get("loop_receipts") or [])
    rows.append(receipt.to_dict())
    deterministic_state["loop_receipts"] = rows


def receipts_of(deterministic_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The canonical receipt rows currently on a state dict."""
    rows = deterministic_state.get("loop_receipts")
    return list(rows) if isinstance(rows, list) else []


def render_receipts_block(
    receipts: Sequence[Mapping[str, Any]], *, budget: int = 3_000,
) -> str:
    """Render the canonical receipt vector as a compact prompt block."""
    if not receipts:
        return ""
    lines = ["## Loop receipts (canonical — what ran, engine-authored)"]
    for row in receipts:
        loop = row.get("loop", "?")
        sub_loop = row.get("sub_loop", "?")
        disposition = row.get("disposition", "?")
        steps = ", ".join(row.get("steps") or ())
        suffix = f" — {steps}" if steps else ""
        lines.append(f"- [{loop}/{sub_loop}] {disposition}{suffix}")
    text = "\n".join(lines)
    if len(text) > budget:
        text = text[:budget].rstrip() + "\n…"
    return text


def _bounded(payload: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Keep the canonical payload small and JSON-safe (drop oversized)."""
    if not payload:
        return None
    try:
        rendered = json.dumps(dict(payload), default=str)
    except Exception:
        return None
    if len(rendered) > _MAX_PAYLOAD_CHARS:
        return {"truncated": True, "chars": len(rendered)}
    return dict(payload)


__all__ = [
    "RECEIPTS_ROOT",
    "append_receipt",
    "emit_receipt",
    "receipts_of",
    "render_receipts_block",
]
