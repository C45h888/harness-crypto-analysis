"""Transport-safe contracts for Binance best-quote microstructure data."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

MICROSTRUCTURE_SCHEMA_VERSION = 1


def decimal(value: Any) -> Decimal:
    """Parse source values without introducing binary floating-point error."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _d(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class BestQuoteState:
    symbol: str
    venue: str
    update_id: int
    exchange_ts_ms: int
    received_ts_ms: int
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def validate(self) -> None:
        if not self.symbol or not self.venue:
            raise ValueError("symbol and venue are required")
        if self.update_id < 0 or self.exchange_ts_ms < 0 or self.received_ts_ms < 0:
            raise ValueError("update and timestamp fields must be non-negative")
        if self.bid_price <= 0 or self.ask_price <= 0:
            raise ValueError("best prices must be positive")
        if self.bid_qty < 0 or self.ask_qty < 0:
            raise ValueError("best quantities must be non-negative")
        if self.bid_price >= self.ask_price:
            raise ValueError("crossed or locked best book")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "venue": self.venue,
            "update_id": self.update_id,
            "exchange_ts_ms": self.exchange_ts_ms,
            "received_ts_ms": self.received_ts_ms,
            "bid_price": _d(self.bid_price),
            "bid_qty": _d(self.bid_qty),
            "ask_price": _d(self.ask_price),
            "ask_qty": _d(self.ask_qty),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BestQuoteState:
        """Rebuild one quote state from its transport form (replay seam)."""
        return cls(
            symbol=str(payload["symbol"]),
            venue=str(payload["venue"]),
            update_id=int(payload["update_id"]),
            exchange_ts_ms=int(payload["exchange_ts_ms"]),
            received_ts_ms=int(payload["received_ts_ms"]),
            bid_price=decimal(payload["bid_price"]),
            bid_qty=decimal(payload["bid_qty"]),
            ask_price=decimal(payload["ask_price"]),
            ask_qty=decimal(payload["ask_qty"]),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class DepthDelta:
    """One Binance depth-update range, retained before any interpretation."""

    symbol: str
    venue: str
    first_update_id: int
    final_update_id: int
    exchange_ts_ms: int
    received_ts_ms: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    @classmethod
    def from_binance(
        cls, payload: dict[str, Any], *, venue: str, received_ts_ms: int,
    ) -> DepthDelta:
        return cls(
            symbol=str(payload["s"]).upper(),
            venue=venue,
            first_update_id=int(payload["U"]),
            final_update_id=int(payload["u"]),
            exchange_ts_ms=int(payload.get("E") or 0),
            received_ts_ms=received_ts_ms,
            bids=tuple((decimal(p), decimal(q)) for p, q in payload.get("b") or ()),
            asks=tuple((decimal(p), decimal(q)) for p, q in payload.get("a") or ()),
        )

    def validate(self) -> None:
        if not self.symbol or not self.venue:
            raise ValueError("symbol and venue are required")
        if self.first_update_id < 0 or self.final_update_id < self.first_update_id:
            raise ValueError("invalid depth update range")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "venue": self.venue,
            "first_update_id": self.first_update_id,
            "final_update_id": self.final_update_id,
            "exchange_ts_ms": self.exchange_ts_ms,
            "received_ts_ms": self.received_ts_ms,
            "bids": [[_d(p), _d(q)] for p, q in self.bids],
            "asks": [[_d(p), _d(q)] for p, q in self.asks],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DepthDelta:
        """Rebuild one delta from its transport form (replay seam)."""
        return cls(
            symbol=str(payload["symbol"]).upper(),
            venue=str(payload["venue"]),
            first_update_id=int(payload["first_update_id"]),
            final_update_id=int(payload["final_update_id"]),
            exchange_ts_ms=int(payload["exchange_ts_ms"]),
            received_ts_ms=int(payload["received_ts_ms"]),
            bids=tuple((decimal(p), decimal(q)) for p, q in payload.get("bids") or ()),
            asks=tuple((decimal(p), decimal(q)) for p, q in payload.get("asks") or ()),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class OrderBookEvent:
    """One deterministic best-quote transition and its paper contribution e_n."""

    previous: BestQuoteState
    current: BestQuoteState
    contribution: Decimal
    source_quality: str = "exact_feed"
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    @property
    def price_changed(self) -> bool:
        """True when the transition moved either best price (not just queue).

        Used by the sensitivity fit that excludes price-changing events from
        OFI (paper tautology caveat): queue-only events keep both best prices.
        """
        return (
            self.current.bid_price != self.previous.bid_price
            or self.current.ask_price != self.previous.ask_price
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": "best_quote_transition",
            "source_quality": self.source_quality,
            "contribution": _d(self.contribution),
            "previous": self.previous.to_dict(),
            "current": self.current.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OrderBookEvent:
        """Rebuild one event from its transport form (replay seam)."""
        return cls(
            previous=BestQuoteState.from_dict(payload["previous"]),
            current=BestQuoteState.from_dict(payload["current"]),
            contribution=decimal(payload["contribution"]),
            source_quality=str(payload.get("source_quality", "exact_feed")),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class OFIInterval:
    """One closed deterministic OFI measurement interval [start, end)."""

    symbol: str
    venue: str
    start_ts_ms: int
    end_ts_ms: int
    event_count: int
    ofi: Decimal
    average_depth: Decimal | None
    first_update_id: int | None
    last_update_id: int | None
    quality: str
    # Best-quote mid at the two interval boundaries, recorded at close time so
    # the price-impact join (ΔP_k) is deterministic and self-contained. For a
    # non-empty interval, mid_start is the mid of the FIRST event's previous
    # quote (the prevailing state at interval start) and mid_end is the mid of
    # the LAST event's current quote (the state at interval end). Both are None
    # when no event landed in the interval (ΔP_k is then undefined).
    mid_start: Decimal | None = None
    mid_end: Decimal | None = None
    # FROZEN label of the average_depth estimator (see ofi.DEPTH_ESTIMATOR).
    # Persisted on every interval so downstream fitting and evidence can prove
    # which definition produced the value; a fit that consumes intervals whose
    # label differs from its own frozen definition must refuse to run.
    depth_estimator: str = ""
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "venue": self.venue,
            "start_ts_ms": self.start_ts_ms,
            "end_ts_ms": self.end_ts_ms,
            "event_count": self.event_count,
            "ofi": _d(self.ofi),
            "average_depth": _d(self.average_depth) if self.average_depth is not None else None,
            "depth_estimator": self.depth_estimator,
            "first_update_id": self.first_update_id,
            "last_update_id": self.last_update_id,
            "quality": self.quality,
            "mid_start": _d(self.mid_start) if self.mid_start is not None else None,
            "mid_end": _d(self.mid_end) if self.mid_end is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OFIInterval:
        """Rebuild one interval from its transport form (replay seam)."""
        return cls(
            symbol=str(payload["symbol"]),
            venue=str(payload["venue"]),
            start_ts_ms=int(payload["start_ts_ms"]),
            end_ts_ms=int(payload["end_ts_ms"]),
            event_count=int(payload["event_count"]),
            ofi=decimal(payload["ofi"]),
            average_depth=decimal(payload["average_depth"]) if payload.get("average_depth") is not None else None,
            first_update_id=payload.get("first_update_id"),
            last_update_id=payload.get("last_update_id"),
            quality=str(payload["quality"]),
            mid_start=decimal(payload["mid_start"]) if payload.get("mid_start") is not None else None,
            mid_end=decimal(payload["mid_end"]) if payload.get("mid_end") is not None else None,
            depth_estimator=str(payload.get("depth_estimator", "")),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Pass 3 — inference artifacts and immutable evidence.
#
# Authority split: these objects are produced ONLY by the deterministic
# fitter (market_service.microstructure.fitting). NOOA agents may read and
# interpret them; they may never recompute any value held here.
# ---------------------------------------------------------------------------

FIT_MODEL_VERSION = "ofi-depth-v1"


@dataclass(frozen=True)
class PriceImpactObservation:
    """One OFI interval joined to its same-window mid-price change.

    ``delta_ticks`` is the paper's ΔP_k expressed in instrument ticks;
    ``delta_quote`` retains the raw quote difference so a tick-size change
    never destroys provenance. ``price_unit`` records which quantity the
    fit consumed.
    """

    interval_start_ts_ms: int
    interval_end_ts_ms: int
    ofi: Decimal
    delta_ticks: Decimal
    delta_quote: Decimal
    mid_start: Decimal
    mid_end: Decimal
    average_depth: Decimal | None
    quality: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval_start_ts_ms": self.interval_start_ts_ms,
            "interval_end_ts_ms": self.interval_end_ts_ms,
            "ofi": _d(self.ofi),
            "delta_ticks": _d(self.delta_ticks),
            "delta_quote": _d(self.delta_quote),
            "mid_start": _d(self.mid_start),
            "mid_end": _d(self.mid_end),
            "average_depth": _d(self.average_depth) if self.average_depth is not None else None,
            "quality": self.quality,
        }


@dataclass(frozen=True)
class PriceImpactFit:
    """OLS of ΔP_k = alpha + beta * OFI_k over one estimation block.

    ``status`` is the trichotomy gate: ``validated`` (all quality gates
    passed), ``provisional`` (fitted but diagnostics incomplete — never a
    trading signal), ``insufficient`` (gates failed; numeric fields are
    estimates of last resort and MUST NOT be interpreted).
    """

    fit_id: str
    symbol: str
    venue: str
    window_start_ms: int
    window_end_ms: int
    interval_seconds: int
    alpha: Decimal
    beta: Decimal
    stderr_beta: Decimal | None
    robust_se_method: str
    n_observations: int
    excluded_observations: int
    r2: Decimal | None
    residual_std: Decimal | None
    heteroskedasticity_flag: bool
    mean_ad: Decimal | None
    price_unit: str
    tick_size: Decimal
    input_hash: str
    model_version: str
    sensitivity: bool
    status: str
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fit_id": self.fit_id,
            "symbol": self.symbol,
            "venue": self.venue,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "interval_seconds": self.interval_seconds,
            "alpha": _d(self.alpha),
            "beta": _d(self.beta),
            "stderr_beta": _d(self.stderr_beta) if self.stderr_beta is not None else None,
            "robust_se_method": self.robust_se_method,
            "n_observations": self.n_observations,
            "excluded_observations": self.excluded_observations,
            "r2": _d(self.r2) if self.r2 is not None else None,
            "residual_std": _d(self.residual_std) if self.residual_std is not None else None,
            "heteroskedasticity_flag": self.heteroskedasticity_flag,
            "mean_ad": _d(self.mean_ad) if self.mean_ad is not None else None,
            "price_unit": self.price_unit,
            "tick_size": _d(self.tick_size),
            "input_hash": self.input_hash,
            "model_version": self.model_version,
            "sensitivity": self.sensitivity,
            "status": self.status,
        }


@dataclass(frozen=True)
class DepthScalingFit:
    """Log-log fit of ln(beta_i) = ln(c) - lambda * ln(AD_i) across blocks.

    Requires at least three estimation blocks with distinct average depths;
    with fewer the relation is unidentified and status is ``insufficient``.
    ``fit_ids`` carries the PriceImpactFit inputs for full provenance.
    """

    fit_id: str
    symbol: str
    venue: str
    c: Decimal | None
    lambda_: Decimal | None
    stderr_lambda: Decimal | None
    n_blocks: int
    r2: Decimal | None
    fit_ids: tuple[str, ...]
    depth_estimator: str
    model_version: str
    status: str
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fit_id": self.fit_id,
            "symbol": self.symbol,
            "venue": self.venue,
            "c": _d(self.c) if self.c is not None else None,
            "lambda": _d(self.lambda_) if self.lambda_ is not None else None,
            "stderr_lambda": _d(self.stderr_lambda) if self.stderr_lambda is not None else None,
            "n_blocks": self.n_blocks,
            "r2": _d(self.r2) if self.r2 is not None else None,
            "fit_ids": list(self.fit_ids),
            "depth_estimator": self.depth_estimator,
            "model_version": self.model_version,
            "status": self.status,
        }


@dataclass(frozen=True)
class MicrostructureEvidence:
    """Immutable Pass-3 evidence object handed to NOOA for read-only reading.

    Contains BOTH fitted models with independent diagnostics. The combined
    expression ΔP = α + c·OFI/AD^λ + (ν·OFI + ε) is deliberately NOT
    materialized here: the ν·OFI term is heteroskedastic, so the combined
    form is a derived diagnostic, never a point prediction.
    """

    symbol: str
    venue: str
    evidence_id: str
    generated_at_ms: int
    interval_seconds: int
    window_start_ms: int
    window_end_ms: int
    tick_size: Decimal
    depth_estimator: str
    input_hash: str
    model_version: str
    price_impact_fit: PriceImpactFit | None
    sensitivity_fit: PriceImpactFit | None
    depth_scaling_fit: DepthScalingFit | None
    block_average_depth: Decimal | None
    coverage: dict[str, Any]
    status: str
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "venue": self.venue,
            "evidence_id": self.evidence_id,
            "generated_at_ms": self.generated_at_ms,
            "interval_seconds": self.interval_seconds,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "tick_size": _d(self.tick_size),
            "depth_estimator": self.depth_estimator,
            "input_hash": self.input_hash,
            "model_version": self.model_version,
            "price_impact_fit": self.price_impact_fit.to_dict() if self.price_impact_fit else None,
            "sensitivity_fit": self.sensitivity_fit.to_dict() if self.sensitivity_fit else None,
            "depth_scaling_fit": self.depth_scaling_fit.to_dict() if self.depth_scaling_fit else None,
            "block_average_depth": _d(self.block_average_depth) if self.block_average_depth is not None else None,
            "coverage": self.coverage,
            "status": self.status,
        }
