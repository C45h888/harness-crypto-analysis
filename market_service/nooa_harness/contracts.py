"""Explicit contract validation for canonical analysis/calculation adapters.

Moved from ``market_service.nodes.contracts``. Used by the harness-owned
pipeline to validate input shapes before calling pure math/analysis functions.

Each canonical function in ``market_service.analysis`` and
``market_service.calculations`` documents an input shape. Adapters MUST
build that exact shape before calling the function. When the shape cannot
be built, the adapter MUST raise :class:`ContractViolation` rather than
silently swallowing the failure.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable


class ContractViolation(Exception):
    """Raised when an adapter cannot produce the shape a canonical function requires."""

    def __init__(self, function: str, reason: str, details: dict[str, Any] | None = None):
        self.function = function
        self.reason = reason
        self.details = details or {}
        super().__init__(f"{function}: {reason}")


def as_contract_error(exc: BaseException, function: str) -> ContractViolation:
    if isinstance(exc, ContractViolation):
        return exc
    return ContractViolation(function=function, reason=f"{type(exc).__name__}: {exc}", details={})


def strict_call(function_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ContractViolation:
        raise
    except Exception as exc:
        raise as_contract_error(exc, function_name) from exc


def require_key(value: Any, *keys: str, function: str, where: str = "") -> Any:
    cursor: Any = value
    path = where or "input"
    for key in keys:
        if not isinstance(cursor, dict) or key not in cursor:
            raise ContractViolation(
                function=function,
                reason=f"missing required key '{'.'.join(keys[: keys.index(key) + 1])}' in {path}",
                details={"path": path, "missing_key": key, "seen_type": type(value).__name__},
            )
        cursor = cursor[key]
    return cursor


def require_list_of_pairs(
    levels: Any, *, function: str, where: str, max_items: int | None = None,
) -> list[list[float]]:
    if levels is None:
        return []
    if not isinstance(levels, Iterable):
        raise ContractViolation(
            function=function,
            reason=f"{where} must be iterable of [price, qty] pairs",
            details={"where": where, "got_type": type(levels).__name__},
        )
    out: list[list[float]] = []
    for i, row in enumerate(levels):
        if max_items is not None and i >= max_items:
            break
        if isinstance(row, dict):
            price = row.get("price") or row.get("p")
            qty = row.get("qty") or row.get("q")
            if price is None or qty is None:
                continue
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, qty = row[0], row[1]
        else:
            continue
        try:
            out.append([float(price), float(qty)])
        except (TypeError, ValueError):
            continue
    return out


def require_float(
    value: Any, *, function: str, where: str, default: float | None = None,
) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(
            function=function,
            reason=f"{where} must be a float-compatible number",
            details={"where": where, "got_type": type(value).__name__},
        ) from exc


def contract_error_entry(violation: ContractViolation) -> dict[str, Any]:
    return {
        "function": violation.function,
        "error": violation.reason,
        "details": violation.details,
    }