"""Pure predicate leaves shared by gates and loop bodies.

No I/O, no state, no runtime imports. This is the leaf both ``gates.py`` and
``core/`` import, so the gate substrate never depends on a loop module.
"""

from __future__ import annotations

from typing import Any


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


def unwrap_tool_data(value: Any) -> Any:
    """Unwrap a ToolResult envelope to its ``data`` payload (pure)."""
    if isinstance(value, dict) and "tool" in value and "data" in value:
        return value.get("data")
    return value


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


def resolve_basis_path(
    path: str, accumulated: dict[str, Any] | None,
) -> tuple[str | None, str | None, Any]:
    """Shared citation resolver: ONE place that maps a cited path to truth.

    Returns ``(head, field, truth)`` — ``head`` the tool root, ``field`` the
    exact payload field, ``truth`` its live value. ``(None, None, None)``
    means unresolvable (unknown head, unknown field, or null payload) and
    callers SKIP, never block. Field-segmented throughout: ``comparator.*``
    resolves inside the univariate baseline dict only, top-level names inside
    the multivariate payload only — the run-6 false positive (baseline value
    compared against multivariate truth) is structurally impossible here.
    Owned by chain.py; echo, adjudication, and assembly all resolve through
    this function so the three can never disagree on what a path means.
    """
    try:
        import re as _re
        acc = accumulated if isinstance(accumulated, dict) else {}
        head = str(path or "").split("→")[0].strip()
        segs = [s for s in _re.split(r"[.→/\s]+",
                                     str(path or "").split("→", 1)[-1].lower()) if s]
        data = unwrap_tool_data(acc.get(head))
        if not isinstance(data, dict):
            return None, None, None
        if head == "calc.forward.fit":
            comp = data.get("comparator")
            comp = comp if isinstance(comp, dict) else {}
            if "comparator" in segs:
                for key, val in comp.items():
                    if key.lower() in segs and val is not None:
                        return head, key, val
                return None, None, None
            est = data.get("estimates")
            est = est if isinstance(est, dict) else {}
            top = {"status": data.get("status"),
                   "r2": data.get("r2", est.get("r2")),
                   "train_r2": data.get("train_r2"),
                   "oos_r2": data.get("oos_r2"),
                   "oos_rmse": data.get("oos_rmse"),
                   "oos_mae": data.get("oos_mae"),
                   "baseline_oos_r2": data.get("baseline_oos_r2"),
                   "beta": data.get("beta", est.get("beta"))}
            for key in ("baseline_oos_r2", "train_r2", "oos_rmse",
                        "oos_mae", "oos_r2", "r2", "status", "beta"):
                if key in segs and top.get(key) is not None:
                    return head, key, top.get(key)
            if "betas" in segs:
                betas = data.get("betas")
                if isinstance(betas, dict):
                    for seg in segs:
                        if seg in betas:
                            return head, f"betas.{seg}", betas[seg]
            return None, None, None
        if head == "calc.forward.distribution":
            for key in ("expected_delta", "pi_95_lo", "pi_95_hi",
                        "p_ge_theta", "p_ge_zero", "status"):
                if key in segs and data.get(key) is not None:
                    return head, key, data.get(key)
            return None, None, None
        if head == "calc.forward.forecast":
            ft = data.get("forward_target") or {}
            if "y_pred" in segs and isinstance(ft, dict) \
                    and ft.get("y_pred") is not None:
                return head, "y_pred", ft.get("y_pred")
            return None, None, None
        if head == "calc.scenario.evaluate":
            for key in ("required_ofi", "exceedance"):
                if key in segs and data.get(key) is not None:
                    return head, key, data.get(key)
            return None, None, None
        return None, None, None
    except Exception:
        return None, None, None


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


__all__ = [
    "EVIDENCE_BLOCK_CODES",
    "EVIDENCE_DROP_CODES",
    "EVIDENCE_NO_PATH",
    "EVIDENCE_NO_READING",
    "EVIDENCE_NULL_VALUE",
    "EVIDENCE_OK",
    "EVIDENCE_UNBACKED",
    "classify_evidence",
    "path_resolves",
    "resolve_basis_path",
    "unwrap_tool_data",
]
