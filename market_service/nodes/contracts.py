"""Explicit contract validation for canonical analysis/calculation adapters.

Each canonical function in ``market_service.analysis`` and
``market_service.calculations`` documents an input shape. Adapters in the
domain nodes (calculations.py, analysis.py) MUST build that exact shape
before calling the function. When the shape cannot be built from the
available evidence, the adapter MUST raise :class:`ContractViolation` rather
than silently swallowing the failure or returning a fabricated default.

Why this module exists
----------------------
The containerization contract says:

    "A source failure produces ``degraded`` or ``invalid``, never fabricated
     healthy data."

The previous adapter code used ``_safe_call`` that logged ``WARN`` and
returned a sentinel default - that *is* fabricated silent degradation. This
module replaces that pattern: every canonical call goes through
:func:`strict_call` which validates the input shape, runs the function, and
re-raises as :class:`ContractViolation` on any failure. The handler in
``_base._handle_one`` then converts the violation into a structured
``errors`` entry on the envelope (status ``invalid`` or ``degraded``),
preserving ``null`` semantics where the canonical docstring already uses them.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

_REQUIRED_KEY_MARKER = object()


class ContractViolation(Exception):
    """Raised when an adapter cannot produce the shape a canonical function requires.

    Attributes:
        function: name of the canonical function whose contract was violated.
        reason: short human-readable explanation.
        details: structured context (missing keys, types seen, etc.) that the
            collator / orchestrator can surface as an ``errors`` entry.
    """

    def __init__(self, function: str, reason: str, details: dict[str, Any] | None = None):
        self.function = function
        self.reason = reason
        self.details = details or {}
        super().__init__(f"{function}: {reason}")


def as_contract_error(exc: BaseException, function: str) -> ContractViolation:
    """Wrap any exception raised by a canonical function as a ``ContractViolation``."""
    if isinstance(exc, ContractViolation):
        return exc
    return ContractViolation(
        function=function,
        reason=f"{type(exc).__name__}: {exc}",
        details={},
    )


def strict_call(function_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a canonical function under explicit contract validation.

    On success, returns the function's result unchanged.

    On failure:

      * If the function raised :class:`ContractViolation`, it is re-raised.
      * Any other exception is re-raised as :class:`ContractViolation` so the
        node handler can record a structured error without swallowing it.

    This is intentionally NOT a soft-fail helper: there is no ``default=``
    argument. The handler is responsible for catching
    :class:`ContractViolation` and either:

      * aggregating it into a per-section structured error list, or
      * promoting the whole envelope to ``invalid`` when the violated
        function is on the critical path.

    The shape of each function's expected input is enforced by
    :func:`require_shape` which MUST be called by the adapter BEFORE this
    function. :func:`strict_call` does not re-validate the shape - that is
    the adapter's responsibility.
    """
    try:
        return fn(*args, **kwargs)
    except ContractViolation:
        raise
    except Exception as exc:  # noqa: BLE001
        raise as_contract_error(exc, function_name) from exc


def require_key(value: Any, *keys: str, function: str, where: str = "") -> Any:
    """Walk a nested dict (``value``) following ``keys``; raise ``ContractViolation`` if any key is missing.

    ``where`` is included in the error context for diagnostics
    (e.g. ``where="evidence.futures"``).
    """
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


def require_list_of_pairs(levels: Any, *, function: str, where: str, max_items: int | None = None) -> list[list[float]]:
    """Validate that ``levels`` is an iterable of ``[price, qty]`` pairs and normalise to ``list[list[float]]``.

    Returns an empty list when ``levels`` is ``None`` (the canonical
    functions treat missing as "no evidence" rather than zero).
    """
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
            # Skip rows that look positional ("0", "1") - dict.get(0) is invalid.
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


def require_float(value: Any, *, function: str, where: str, default: float | None = None) -> float | None:
    """Validate that ``value`` is a float-compatible number; return ``None`` for missing.

    ``None`` is propagated (canonical docstring: ``null`` means unavailable,
    not zero). ``default`` is used ONLY when ``value`` is explicitly absent
    AND ``default`` is provided; otherwise ``None`` is returned for missing.
    """
    if value is None:
        return default
    try:
        result = float(value)
        return result
    except (TypeError, ValueError) as exc:
        raise ContractViolation(
            function=function,
            reason=f"{where} must be a float-compatible number",
            details={"where": where, "got_type": type(value).__name__},
        ) from exc


def contract_error_entry(violation: ContractViolation) -> dict[str, Any]:
    """Render a :class:`ContractViolation` as a structured ``errors`` entry."""
    return {
        "function": violation.function,
        "error": violation.reason,
        "details": violation.details,
    }