"""Control client — the one way to ask the calculation plane to fire a worker.

Worker identity is ``(substrate, symbol)`` and never the process, so a process
that builds its own ``SubstrateWorkerCore`` collides with the live calculation
container three ways: it overwrites the supervisor heartbeat (and with it the
fire-dedupe high-water state, which shares that key), and it consumes
raw-stream entries from the shared consumer group with ``noack=True`` —
unrecoverably.

This module is the alternative. It POSTs to the calculation container's
control plane, which runs the SAME ``substrate_worker.tools`` code path inside
the workers' own process. The caller keeps full authority to decide a
calculation must run; only the execution site moves.

**An unreachable plane raises.** It never falls back to in-process invocation —
that would reintroduce exactly the collision this seam removes. Under the repo
null discipline, "the calculation plane is down" is a legitimate observation
for the caller to report, not something to paper over.

Usage:
    from market_service.substrate_worker.control_client import request_invoke

    report = await request_invoke("SOLUSDT", ["tape", "density"])
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://calculation:8041"
DEFAULT_TIMEOUT_S = 30.0


class CalcPlaneError(RuntimeError):
    """Base: the calculation plane did not return a report."""


class CalcPlaneUnreachable(CalcPlaneError):
    """The plane could not be reached (down, wrong URL, timed out).

    Callers report this as a finding. They must NOT fall back to building a
    worker in their own process.
    """


class CalcPlaneRejected(CalcPlaneError):
    """The plane refused the request (4xx) — a bad ask, not a dormant worker."""


def control_base_url() -> str:
    """Resolve the calculation control plane URL (env, else compose default)."""
    return (os.getenv("CALC_CONTROL_URL") or DEFAULT_BASE_URL).rstrip("/")


def _timeout_s() -> float:
    raw = os.getenv("CALC_CONTROL_TIMEOUT_S")
    try:
        return float(raw) if raw else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


async def request_invoke(
    symbol: str,
    substrates: list[str] | tuple[str, ...] | None = None,
    *,
    base_url: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Ask the calculation plane for one bounded fire-tick per named worker.

    Returns the plane's report verbatim — the ``invoke_many`` shape
    ``{symbol, invoked, fired, reports: [...]}``. ``substrates=None`` means
    every registered worker.

    Raises ``CalcPlaneUnreachable`` when the plane cannot be reached or the
    request times out, and ``CalcPlaneRejected`` (carrying the plane's own
    message) on a 4xx. Never degrades to in-process invocation.
    """
    import aiohttp

    url = f"{base_url.rstrip('/') if base_url else control_base_url()}/invoke"
    payload: dict[str, Any] = {"symbol": symbol.upper()}
    if substrates:
        payload["substrates"] = [str(name).strip().lower() for name in substrates]

    timeout = aiohttp.ClientTimeout(total=timeout_s if timeout_s is not None else _timeout_s())
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                if 400 <= response.status < 500:
                    detail = await _error_detail(response)
                    raise CalcPlaneRejected(
                        f"calculation plane rejected the request ({response.status}): {detail}")
                if response.status >= 500:
                    detail = await _error_detail(response)
                    raise CalcPlaneUnreachable(
                        f"calculation plane failed ({response.status}): {detail}")
                return await response.json()
    except CalcPlaneError:
        raise
    except Exception as exc:  # noqa: BLE001 — every transport failure is one finding
        raise CalcPlaneUnreachable(
            f"calculation plane unreachable at {url}: {exc!r}") from exc


async def _error_detail(response: Any) -> str:
    try:
        body = await response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is still a detail
        try:
            return (await response.text())[:200]
        except Exception:  # noqa: BLE001
            return "<unreadable body>"
    if isinstance(body, dict):
        return str(body.get("error") or body)
    return str(body)


__all__ = [
    "CalcPlaneError",
    "CalcPlaneRejected",
    "CalcPlaneUnreachable",
    "control_base_url",
    "request_invoke",
]
