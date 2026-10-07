"""Deterministic parsing layer — Python parses, the model cites.

No model-run parsing scripts: every projection below runs Python-side
(``read_paths`` / ``substrate_tools`` / pure migration fns) and the model
receives a ``ToolResult`` envelope::

    {tool, status, reason, data, null_fields, budget_receipt}

* ``data`` — the deterministically projected view (never raw state).
* ``null_fields`` — explicit None paths (model must not fill them).
* ``budget_receipt`` — {truncated, roof, mode, human_only_warning?}.
* Empty reads return the surface inventory as DATA (status "empty"),
  never a bare null the model re-calls against.
* Schema mismatch raises — never coerced.
"""

from __future__ import annotations

import json
from typing import Any

from market_service.interaction_plane.budgets import (
    TOOLRESULT_INLINE_LIMIT,
    char_roof,
    list_cap,
)
from market_service.interaction_plane.segments import HUMAN_ONLY_MODES

__all__ = [
    "EMPTY_STATUSES",
    "cap_lists",
    "empty_read",
    "enforce_mode",
    "find_nulls",
    "truncate_data",
    "wrap_result",
]

EMPTY_STATUSES = ("empty",)


def find_nulls(obj: dict[str, Any], prefix: str = "") -> list[str]:
    """Recursively name every None path (model must not fill these)."""
    nulls: list[str] = []
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if value is None:
            nulls.append(path)
        elif isinstance(value, dict):
            nulls.extend(find_nulls(value, path))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    nulls.extend(find_nulls(item, f"{path}[{i}]"))
                elif item is None:
                    nulls.append(f"{path}[{i}]")
    return nulls


def cap_lists(value: Any, max_items: int) -> tuple[Any, bool]:
    """Cap every list at max_items with an explicit marker. Returns (capped, truncated)."""
    truncated = False

    def _cap(node: Any) -> Any:
        nonlocal truncated
        if isinstance(node, dict):
            return {k: _cap(v) for k, v in node.items()}
        if isinstance(node, list):
            if len(node) > max_items:
                truncated = True
                return {
                    "__truncated__": True,
                    "count": len(node),
                    "items": [_cap(item) for item in node[:max_items]],
                }
            return [_cap(item) for item in node]
        return node

    return _cap(value), truncated


def truncate_data(data: Any, *, segment: str, mode: str) -> tuple[Any, dict[str, Any]]:
    """Enforce the segment/mode byte roof. Returns (data, budget_receipt)."""
    roof = char_roof(mode)
    cap = list_cap(segment)
    capped, list_truncated = cap_lists(data, cap)
    try:
        rendered = json.dumps(capped, default=str)
    except Exception:
        rendered = str(capped)
    if len(rendered) <= roof and len(rendered) <= TOOLRESULT_INLINE_LIMIT:
        return capped, {
            "truncated": bool(list_truncated),
            "roof": roof,
            "mode": mode,
            "chars": len(rendered),
        }
    preview = rendered[:roof]
    return (
        {"_truncated": True, "preview": preview, "chars": len(rendered)},
        {"truncated": True, "roof": roof, "mode": mode, "chars": len(rendered)},
    )


def enforce_mode(segment: str, mode: str) -> tuple[str, str | None]:
    """Gate human-only modes. Returns (effective_mode, warning_or_None)."""
    if mode in HUMAN_ONLY_MODES:
        return mode, (
            f"mode {mode!r} is human-only: unbounded raw payload for deep-dives, "
            "never a working view. Prefer snapshot/inventory/compact and cite "
            "data.* paths; null_fields names what was not computed."
        )
    return mode, None


def wrap_result(
    tool: str,
    data: Any,
    *,
    segment: str,
    mode: str,
    status: str = "ok",
    reason: str | None = None,
) -> dict[str, Any]:
    """Wrap a deterministically projected view in the ToolResult envelope."""
    effective_mode, warning = enforce_mode(segment, mode)
    capped, receipt = truncate_data(data, segment=segment, mode=effective_mode)
    if warning:
        receipt = {**receipt, "human_only_warning": warning}
    null_fields = find_nulls(capped) if isinstance(capped, dict) else []
    envelope: dict[str, Any] = {
        "tool": tool,
        "status": status,
        "reason": reason,
        "data": capped,
        "null_fields": null_fields,
        "budget_receipt": receipt,
    }
    if warning:
        envelope["parse_warning"] = warning
    return envelope


def empty_read(
    *,
    tool: str,
    segment: str,
    mode: str,
    symbol: str,
    surfaces: dict[str, Any] | None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Empty-read contract: surface inventory as DATA, never a bare null."""
    data = {
        "symbol": symbol,
        "surfaces": (surfaces or {}).get("surfaces", []),
        "available_surfaces": (surfaces or {}).get("available_surfaces", []),
        "read_tools": (surfaces or {}).get("read_tools", {}),
        "errors": list(errors or ["no data persisted (redis miss, postgres absent/empty)"]),
    }
    return wrap_result(
        tool, data, segment=segment, mode=mode,
        status="empty", reason="no data persisted",
    )
