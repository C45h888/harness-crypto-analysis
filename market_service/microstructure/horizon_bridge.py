"""Long-horizon bridge — project native forward fits to 15m / 1h / 4h.

Semantic authority: HOW A NATIVE FORWARD FIT BECOMES A LONG-HORIZON ANSWER.

The deterministic forward stack labels only native horizons
(``FORWARD_HORIZONS_MS`` = 1s/5s/30s/60s — a 30-minute tape window cannot
carry a 4h label), while the task directive resolves long horizons
(15m/1h/4h) into milliseconds the tools are then asked to evaluate. Before
this module the seam simply raised ``ValueError: unsupported horizon_ms``
and the whole forward chain (fit → distribution → P(T)/P(S) → hypothesis
test) died on the very horizon the question asked.

This module is the adapter: a native fit (fitted at the source horizon)
is projected to the requested long horizon by a deterministic,
stated-not-proven rule set:

    sigma_H = sigma_h0 * sqrt(H / h0)      diffusive widening of uncertainty
    mu_H    = mu_h0                         drift expectation carried unchanged
    skill_H = skill_h0 * sqrt(h0 / H)       skill decay, sqrt law
    se_H    = se_h0 * sqrt(H / h0)          band widening with sigma

The bridged ``ForwardFit`` carries an explicit ``bridge`` block naming the
source horizon, the scaling rule and the assumptions — never silently
relabelled as a native fit. ``estimation_status`` / ``validation_status``
become ``extrapolated`` and a native ``validated`` status degrades to
``provisional`` (an extrapolation cannot be validated on in-window labels).
The null discipline is unchanged: a native fit that is ``insufficient``
stays insufficient when bridged.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .contracts import ForwardFit

# Phase S-2 (docs/STATE_CHARTER_SPEC.md): the native + long horizon numbers
# resolve to the tree-wide charter owner (runtime/horizons) by IDENTITY —
# this module no longer mirrors them locally, and the charter test
# (test_state_charter.TestHorizonNamespace) pins the resolution. Route C
# remains the plane that fits at the native horizons; task_directive parses
# the directive vocabulary against the same owner's dict form.
from market_service.runtime.horizons import LONG_HORIZON_ORDER, NATIVE_HORIZON_ORDER

NATIVE_HORIZONS_MS: tuple[int, ...] = NATIVE_HORIZON_ORDER
LONG_HORIZONS_MS: tuple[int, ...] = LONG_HORIZON_ORDER

BRIDGE_MODEL_VERSION = "forward-ols-v2+long-bridge-v1"
BRIDGE_METHOD = "native-bridge-v1"
BRIDGE_ASSUMPTIONS: dict[str, Any] = {
    "sigma_scaling": "sqrt(H/h0) — diffusive",
    "drift_scaling": "carried unchanged from source horizon (no drift inflation)",
    "skill_decay": "sqrt(h0/H)",
    "gaussian_probability": True,
    "stated_not_proven": True,
}

# The bridge always projects from the LONGEST native horizon: it carries the
# most information about the conditional drift for the current feature vector.
SOURCE_HORIZON_MS: int = max(NATIVE_HORIZONS_MS)


class UnsupportedHorizonError(ValueError):
    """A horizon outside the deterministic vocabulary — carries the domain.

    Raised by the forward core instead of a bare ``ValueError`` so every
    tool refusal can tell the agent WHICH horizons are evaluable and re-call
    with a supported value instead of burning rounds on the same wall.
    """

    def __init__(self, horizon_ms: Any, supported: tuple[int, ...]) -> None:
        self.horizon_ms = horizon_ms
        self.supported = tuple(supported)
        super().__init__(
            f"unsupported horizon_ms: {horizon_ms!r} — supported: "
            f"{list(self.supported)} (native {list(NATIVE_HORIZONS_MS)} + "
            f"long-horizon bridge {list(LONG_HORIZONS_MS)})"
        )


def classify_horizon(horizon_ms: int) -> str | None:
    """``"native"`` / ``"long"`` / ``None`` (unsupported)."""
    if horizon_ms in NATIVE_HORIZONS_MS:
        return "native"
    if horizon_ms in LONG_HORIZONS_MS:
        return "long"
    return None


def supported_horizons() -> tuple[int, ...]:
    """Every horizon the deterministic forward base can answer for."""
    return tuple(sorted(NATIVE_HORIZONS_MS + LONG_HORIZONS_MS))


def require_supported(horizon_ms: int) -> str:
    """Validate a horizon against the deterministic vocabulary.

    Returns its regime (``native`` / ``long``) or raises
    :class:`UnsupportedHorizonError` carrying the supported set.
    """
    regime = classify_horizon(int(horizon_ms))
    if regime is None:
        raise UnsupportedHorizonError(horizon_ms, supported_horizons())
    return regime


def _scale(value: str | None, factor: Decimal) -> str | None:
    """Scale a decimal-string field; ``None`` stays ``None`` (null means
    not-provided — never zero-substitute)."""
    if value is None:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 28
            return format(Decimal(str(value)) * factor, "f")
    except (InvalidOperation, ValueError):
        return None


def bridge_forward_fit(fit: ForwardFit, horizon_ms: int) -> ForwardFit:
    """Project a NATIVE ``ForwardFit`` onto a LONG horizon (deterministic).

    Returns the input unchanged when the horizon is already the fit's own.
    Otherwise the fit is re-stamped to the requested horizon with the
    bridge rule set applied and an explicit ``bridge`` block attached.
    """
    horizon_ms = int(horizon_ms)
    if horizon_ms == fit.horizon_ms:
        return fit
    regime = classify_horizon(horizon_ms)
    if regime != "long":
        raise UnsupportedHorizonError(horizon_ms, supported_horizons())
    source = fit.horizon_ms
    if source not in NATIVE_HORIZONS_MS:
        raise UnsupportedHorizonError(
            f"bridge source must be a native horizon, got {source!r}",
            supported_horizons(),
        )
    with localcontext() as ctx:
        ctx.prec = 28
        ratio = Decimal(horizon_ms) / Decimal(source)
        sigma_scale = ratio.sqrt()
        skill_decay = Decimal(1) / sigma_scale

    status = fit.status
    if status in ("validated", "provisional"):
        status = "provisional"
    bridge_block: dict[str, Any] = {
        "method": BRIDGE_METHOD,
        "source_horizon_ms": source,
        "requested_horizon_ms": horizon_ms,
        "ratio": format(ratio, "f"),
        "sigma_scale": format(sigma_scale, "f"),
        "skill_decay": format(skill_decay, "f"),
        "assumptions": dict(BRIDGE_ASSUMPTIONS),
    }
    return replace(
        fit,
        horizon_ms=horizon_ms,
        fit_id=f"{fit.fit_id}+bridge{horizon_ms}",
        model_version=BRIDGE_MODEL_VERSION,
        betas=dict(fit.betas),  # drift expectation carried unchanged
        stderr={k: _scale(v, sigma_scale) for k, v in fit.stderr.items()},
        resid_std=_scale(fit.resid_std, sigma_scale),
        oos_skill=_scale(fit.oos_skill, skill_decay),
        status=status,
        estimation_status="extrapolated",
        validation_status="extrapolated",
        bridge=bridge_block,
    )
