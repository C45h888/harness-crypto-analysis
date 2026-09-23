"""Transport-safe contracts for Binance best-quote microstructure data."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

MICROSTRUCTURE_SCHEMA_VERSION = 1

# FROZEN label of the L2 ladder projection carried on tape events. The
# ladder is ADDITIVE evidence: it never enters the best-quote hash domains
# (OFI intervals, feature vectors, fits), so old payloads replay
# bit-identically and old fits keep their input_hash. A BookL2State without
# this exact label in its own hash domain must not be joined against a fit
# that expects a different projection.
L2_LADDER_VERSION = "l2-topn-v1"


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
    """One Binance depth-update range, retained before any interpretation.

    ``previous_update_id`` is Binance's per-frame ``pu`` field — the ``u``
    (final update id) of the PREVIOUS frame in the stream. On futures at
    100/500ms cadence it is the ONLY reliable continuity link: the internal
    update-ID space between consecutive frames is non-contiguous (Binance
    coalesces many internal updates into each frame), but ``pu`` chains
    exactly to the prior frame's ``u``, proving arrival order.
    """

    symbol: str
    venue: str
    first_update_id: int
    final_update_id: int
    exchange_ts_ms: int
    received_ts_ms: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    previous_update_id: int | None = None
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
            previous_update_id=int(payload["pu"]) if payload.get("pu") is not None else None,
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
            "previous_update_id": self.previous_update_id,
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
            previous_update_id=int(payload["previous_update_id"])
            if payload.get("previous_update_id") is not None else None,
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class BookL2State:
    """Top-of-book-plus-N depth ladder snapshot (frozen transport projection).

    Encapsulates the L2 evidence the analysis planes need at event grain:
    the top ``n`` price levels per side of the reconstructed book, ordered
    canonically (bids price-DESCENDING, asks price-ASCENDING). This is a
    READ-ONLY projection captured at publish time — it is not a fit input
    and carries its own versioned hash domain (``state_hash``).

    Determinism rules:
    - prices/quantities are Decimal, stringified fixed-point in transport;
    - empty sides are legal (degenerate book); ordering validation only
      applies within a non-empty side;
    - ``update_id`` is the book sequence id the projection reflects.
    """

    symbol: str
    venue: str
    update_id: int
    exchange_ts_ms: int
    received_ts_ms: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    ladder_version: str = L2_LADDER_VERSION
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def validate(self) -> None:
        if not self.symbol or not self.venue:
            raise ValueError("symbol and venue are required")
        if self.update_id < 0 or self.exchange_ts_ms < 0 or self.received_ts_ms < 0:
            raise ValueError("update and timestamp fields must be non-negative")
        for side in (self.bids, self.asks):
            for price, qty in side:
                if price <= 0 or qty < 0:
                    raise ValueError("ladder level must have positive price and non-negative qty")
        bid_prices = [p for p, _q in self.bids]
        ask_prices = [p for p, _q in self.asks]
        if any(b <= a for b, a in zip(bid_prices, bid_prices[1:])):
            raise ValueError("ladder bids must be strictly price-descending")
        if any(b >= a for b, a in zip(ask_prices, ask_prices[1:])):
            raise ValueError("ladder asks must be strictly price-ascending")
        if bid_prices and ask_prices and bid_prices[0] >= ask_prices[0]:
            raise ValueError("crossed or locked ladder best book")

    @property
    def state_hash(self) -> str:
        """Versioned hash over the ladder content (own domain — additive)."""
        import hashlib
        import json as _json
        payload = {
            "ladder_version": self.ladder_version,
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "venue": self.venue,
            "update_id": self.update_id,
            "exchange_ts_ms": self.exchange_ts_ms,
            "received_ts_ms": self.received_ts_ms,
            "bids": [[_d(p), _d(q)] for p, q in self.bids],
            "asks": [[_d(p), _d(q)] for p, q in self.asks],
        }
        return hashlib.sha256(
            _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "venue": self.venue,
            "update_id": self.update_id,
            "exchange_ts_ms": self.exchange_ts_ms,
            "received_ts_ms": self.received_ts_ms,
            "bids": [[_d(p), _d(q)] for p, q in self.bids],
            "asks": [[_d(p), _d(q)] for p, q in self.asks],
            "ladder_version": self.ladder_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BookL2State:
        """Rebuild one ladder projection from its transport form (replay seam)."""
        return cls(
            symbol=str(payload["symbol"]),
            venue=str(payload["venue"]),
            update_id=int(payload["update_id"]),
            exchange_ts_ms=int(payload["exchange_ts_ms"]),
            received_ts_ms=int(payload["received_ts_ms"]),
            bids=tuple((decimal(p), decimal(q)) for p, q in payload.get("bids") or ()),
            asks=tuple((decimal(p), decimal(q)) for p, q in payload.get("asks") or ()),
            ladder_version=str(payload.get("ladder_version", L2_LADDER_VERSION)),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class OrderBookEvent:
    """One deterministic best-quote transition and its paper contribution e_n.

    ``l2`` is the ADDITIVE top-N ladder projection of the book state AFTER
    the transition (None when the capture runs without L2 projection or the
    payload predates the projection). It never participates in best-quote
    hash domains; replay of old payloads is unchanged.
    """

    previous: BestQuoteState
    current: BestQuoteState
    contribution: Decimal
    source_quality: str = "exact_feed"
    l2: BookL2State | None = None
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
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_type": "best_quote_transition",
            "source_quality": self.source_quality,
            "contribution": _d(self.contribution),
            "previous": self.previous.to_dict(),
            "current": self.current.to_dict(),
        }
        if self.l2 is not None:
            out["l2"] = self.l2.to_dict()
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OrderBookEvent:
        """Rebuild one event from its transport form (replay seam)."""
        l2_raw = payload.get("l2")
        return cls(
            previous=BestQuoteState.from_dict(payload["previous"]),
            current=BestQuoteState.from_dict(payload["current"]),
            contribution=decimal(payload["contribution"]),
            source_quality=str(payload.get("source_quality", "exact_feed")),
            l2=BookL2State.from_dict(l2_raw) if isinstance(l2_raw, dict) else None,
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


# ---------------------------------------------------------------------------
# Track D (D1-D5) — additive only. Existing dataclasses above are frozen.
# ---------------------------------------------------------------------------

FEATURE_VECTOR_VERSION = "xt-v2"
FORWARD_MODEL_VERSION = "forward-ols-v2"
HYPOTHESIS_VERSION = "hyp-v1"
EVIDENCE_V2_VERSION = "evidence-v2"


@dataclass(frozen=True)
class QuoteMeasurement:
    """Optional D1 microprice measurement attached to one quote (additive)."""

    mid: Decimal | None
    microprice: Decimal | None
    displacement: Decimal | None
    estimator: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mid": _d(self.mid) if self.mid is not None else None,
            "microprice": _d(self.microprice) if self.microprice is not None else None,
            "displacement": _d(self.displacement) if self.displacement is not None else None,
            "estimator": self.estimator,
        }


@dataclass(frozen=True)
class FeatureVector:
    """Versioned D2 feature vector X_t (Decimal canonical strings).

    ``feature_keys`` and ``feature_schema_hash`` make the information set
    explicit.  A model trained on one set of fields must not silently consume
    a vector with a different set of fields.
    """

    symbol: str
    venue: str
    ts_ms: int
    vector_version: str
    fields: dict[str, str]
    def_versions: dict[str, str]
    quality: str
    input_hash: str
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION
    feature_keys: tuple[str, ...] = ()
    feature_schema_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "venue": self.venue,
            "ts_ms": self.ts_ms,
            "vector_version": self.vector_version,
            "fields": dict(self.fields),
            "def_versions": dict(self.def_versions),
            "feature_keys": list(self.feature_keys),
            "feature_schema_hash": self.feature_schema_hash,
            "quality": self.quality,
            "input_hash": self.input_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FeatureVector:
        """Rebuild one vector from its transport form (replay seam)."""
        return cls(
            symbol=str(payload["symbol"]).upper(),
            venue=str(payload["venue"]),
            ts_ms=int(payload["ts_ms"]),
            vector_version=str(payload.get("vector_version", FEATURE_VECTOR_VERSION)),
            fields={str(k): str(v) for k, v in (payload.get("fields") or {}).items()},
            def_versions={str(k): str(v) for k, v in (payload.get("def_versions") or {}).items()},
            quality=str(payload.get("quality", "exact_feed")),
            input_hash=str(payload["input_hash"]),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
            feature_keys=tuple(str(k) for k in (payload.get("feature_keys") or (payload.get("fields") or {}).keys())),
            feature_schema_hash=str(payload.get("feature_schema_hash") or ""),
        )


@dataclass(frozen=True)
class ForwardObservation:
    """D3 (X_t, {Y_t(h)}) pair with exclusion reasons per horizon."""

    x: FeatureVector
    y_ticks: dict[int, str | None]
    y_quote: dict[int, str | None]
    price_source: str
    excluded: dict[int, str]
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "x": self.x.to_dict(),
            "y_ticks": {str(k): v for k, v in self.y_ticks.items()},
            "y_quote": {str(k): v for k, v in self.y_quote.items()},
            "price_source": self.price_source,
            "excluded": {str(k): v for k, v in self.excluded.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ForwardObservation:
        """Rebuild one pair from its transport form (replay seam)."""
        return cls(
            x=FeatureVector.from_dict(payload["x"]),
            y_ticks={int(k): v for k, v in (payload.get("y_ticks") or {}).items()},
            y_quote={int(k): v for k, v in (payload.get("y_quote") or {}).items()},
            price_source=str(payload.get("price_source", "microstructure_mid")),
            excluded={int(k): str(v) for k, v in (payload.get("excluded") or {}).items()},
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class ForwardFit:
    """D4 per-horizon multivariate OLS fit with explicit validation state.

    ``status`` is retained as the compatibility projection used by the older
    inference surface.  The forward plane must use the more precise fields:
    estimation status, validation status, probability status, schema identity,
    and train/OOS metrics.
    """

    fit_id: str
    symbol: str
    venue: str
    horizon_ms: int
    betas: dict[str, str]
    stderr: dict[str, str | None]
    r2: str | None
    resid_std: str | None
    hetero_flag: bool
    n_obs: int
    n_excluded: int
    oos_skill: str | None
    comparator: dict[str, str]
    input_hash: str
    model_version: str
    status: str
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION
    feature_keys: tuple[str, ...] = ()
    feature_schema_hash: str = ""
    feature_definition_versions: dict[str, str] = field(default_factory=dict)
    n_train: int = 0
    n_oos: int = 0
    split_method: str = ""
    train_r2: str | None = None
    oos_r2: str | None = None
    oos_mae: str | None = None
    oos_rmse: str | None = None
    baseline_oos_r2: str | None = None
    baseline_oos_mae: str | None = None
    estimation_status: str = "insufficient"
    validation_status: str = "unvalidated"
    probability_status: str = "not_requested"
    oos_betas: dict[str, str] = field(default_factory=dict)
    oos_resid_std: str | None = None
    oos_cut: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fit_id": self.fit_id,
            "symbol": self.symbol,
            "venue": self.venue,
            "horizon_ms": self.horizon_ms,
            "betas": dict(self.betas),
            "stderr": dict(self.stderr),
            "r2": self.r2,
            "resid_std": self.resid_std,
            "hetero_flag": self.hetero_flag,
            "n_obs": self.n_obs,
            "n_excluded": self.n_excluded,
            "oos_skill": self.oos_skill,
            "comparator": dict(self.comparator),
            "input_hash": self.input_hash,
            "model_version": self.model_version,
            "status": self.status,
            "feature_keys": list(self.feature_keys),
            "feature_schema_hash": self.feature_schema_hash,
            "feature_definition_versions": dict(self.feature_definition_versions),
            "n_train": self.n_train,
            "n_oos": self.n_oos,
            "split_method": self.split_method,
            "train_r2": self.train_r2,
            "oos_r2": self.oos_r2,
            "oos_mae": self.oos_mae,
            "oos_rmse": self.oos_rmse,
            "baseline_oos_r2": self.baseline_oos_r2,
            "baseline_oos_mae": self.baseline_oos_mae,
            "estimation_status": self.estimation_status,
            "validation_status": self.validation_status,
            "probability_status": self.probability_status,
            "oos_betas": dict(self.oos_betas),
            "oos_resid_std": self.oos_resid_std,
            "oos_cut": self.oos_cut,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ForwardFit:
        """Rebuild one fit from its transport form (replay seam)."""
        return cls(
            fit_id=str(payload["fit_id"]),
            symbol=str(payload["symbol"]).upper(),
            venue=str(payload["venue"]),
            horizon_ms=int(payload["horizon_ms"]),
            betas={str(k): str(v) for k, v in (payload.get("betas") or {}).items()},
            stderr={str(k): v for k, v in (payload.get("stderr") or {}).items()},
            r2=payload.get("r2"),
            resid_std=payload.get("resid_std"),
            hetero_flag=bool(payload.get("hetero_flag", False)),
            n_obs=int(payload.get("n_obs", 0)),
            n_excluded=int(payload.get("n_excluded", 0)),
            oos_skill=payload.get("oos_skill"),
            comparator={str(k): str(v) for k, v in (payload.get("comparator") or {}).items()},
            input_hash=str(payload["input_hash"]),
            model_version=str(payload.get("model_version", FORWARD_MODEL_VERSION)),
            status=str(payload.get("status", "insufficient")),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
            feature_keys=tuple(str(k) for k in (payload.get("feature_keys") or ())),
            feature_schema_hash=str(payload.get("feature_schema_hash") or ""),
            feature_definition_versions={str(k): str(v) for k, v in (payload.get("feature_definition_versions") or {}).items()},
            n_train=int(payload.get("n_train") or 0),
            n_oos=int(payload.get("n_oos") or 0),
            split_method=str(payload.get("split_method") or ""),
            train_r2=payload.get("train_r2"),
            oos_r2=payload.get("oos_r2"),
            oos_mae=payload.get("oos_mae"),
            oos_rmse=payload.get("oos_rmse"),
            baseline_oos_r2=payload.get("baseline_oos_r2"),
            baseline_oos_mae=payload.get("baseline_oos_mae"),
            estimation_status=str(payload.get("estimation_status") or ("fitted" if payload.get("status") in ("validated", "provisional") else "insufficient")),
            validation_status=str(payload.get("validation_status") or ("validated" if payload.get("status") == "validated" else "provisional" if payload.get("status") == "provisional" else "unvalidated")),
            probability_status=str(payload.get("probability_status") or "not_requested"),
            oos_betas={str(k): str(v) for k, v in (payload.get("oos_betas") or {}).items()},
            oos_resid_std=payload.get("oos_resid_std"),
            oos_cut=int(payload.get("oos_cut") or 0),
        )


# ---------------------------------------------------------------------------
# Track D (D7/D10) — additive only. Existing dataclasses above are frozen.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HypothesisEvidence:
    """D7 formal evidence object for one pre-registered directional hypothesis.

    No signal/action/execution field exists by construction: p<0.05 is
    evidence, never an execution predicate.
    """

    hypothesis_id: str
    h0: str
    h1: str
    horizon_ms: int
    effect: str
    se: str | None
    ci_lo: str | None
    ci_hi: str | None
    p_value: str | None
    equiv_stat: str | None
    method: str
    n: int
    split: str
    oos_skill: str | None
    multiplicity_adj: str
    m_tests: int
    model_version: str
    input_hash: str
    status: str
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "h0": self.h0,
            "h1": self.h1,
            "horizon_ms": self.horizon_ms,
            "effect": self.effect,
            "se": self.se,
            "ci_lo": self.ci_lo,
            "ci_hi": self.ci_hi,
            "p_value": self.p_value,
            "equiv_stat": self.equiv_stat,
            "method": self.method,
            "n": self.n,
            "split": self.split,
            "oos_skill": self.oos_skill,
            "multiplicity_adj": self.multiplicity_adj,
            "m_tests": self.m_tests,
            "model_version": self.model_version,
            "input_hash": self.input_hash,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HypothesisEvidence:
        """Rebuild one hypothesis record from its transport form (replay seam)."""
        return cls(
            hypothesis_id=str(payload["hypothesis_id"]),
            h0=str(payload.get("h0", "")),
            h1=str(payload.get("h1", "")),
            horizon_ms=int(payload["horizon_ms"]),
            effect=str(payload["effect"]),
            se=payload.get("se"),
            ci_lo=payload.get("ci_lo"),
            ci_hi=payload.get("ci_hi"),
            p_value=payload.get("p_value"),
            equiv_stat=payload.get("equiv_stat"),
            method=str(payload.get("method", "")),
            n=int(payload.get("n", 0)),
            split=str(payload.get("split", "")),
            oos_skill=payload.get("oos_skill"),
            multiplicity_adj=str(payload.get("multiplicity_adj", "")),
            m_tests=int(payload.get("m_tests", 1)),
            model_version=str(payload.get("model_version", FORWARD_MODEL_VERSION)),
            input_hash=str(payload["input_hash"]),
            status=str(payload.get("status", "insufficient")),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class HypothesisLedger:
    """D7 append-only ledger of HypothesisEvidence (idempotent on id)."""

    entries: tuple[HypothesisEvidence, ...]
    ledger_version: str = "hyp-ledger-v1"
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def append(self, entry: HypothesisEvidence) -> HypothesisLedger:
        for existing in self.entries:
            if existing.hypothesis_id == entry.hypothesis_id:
                if existing == entry:
                    return self
                raise ValueError(
                    f"duplicate hypothesis_id with different bytes: {entry.hypothesis_id!r}"
                )
        return HypothesisLedger(entries=(*self.entries, entry))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ledger_version": self.ledger_version,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HypothesisLedger:
        """Rebuild a ledger from its transport form (replay seam)."""
        return cls(
            entries=tuple(HypothesisEvidence.from_dict(e) for e in payload.get("entries") or ()),
            ledger_version=str(payload.get("ledger_version", "hyp-ledger-v1")),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class ForecastResult:
    """Canonical deterministic result consumed by the inference plane.

    Route A, Route B, and the forward model remain separate estimands. The
    composer stores their outputs without asking an agent to merge them.
    """

    symbol: str
    venue: str
    generated_at_ms: int
    horizon_ms: int | None
    forecast_type: str
    horizon_regime: str
    information_set: dict[str, Any]
    route_a: dict[str, Any] | None
    route_b: dict[str, Any] | None
    multivariate: dict[str, Any] | None
    agreement: dict[str, Any]
    diagnostics: dict[str, Any]
    assumptions: dict[str, Any]
    validation_state: str
    model_version: str
    input_hash: str
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION
    forecast_version: str = "forecast-result-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "venue": self.venue,
            "generated_at_ms": self.generated_at_ms,
            "horizon_ms": self.horizon_ms,
            "forecast_type": self.forecast_type,
            "horizon_regime": self.horizon_regime,
            "information_set": self.information_set,
            "route_a": self.route_a,
            "route_b": self.route_b,
            "multivariate": self.multivariate,
            "agreement": self.agreement,
            "diagnostics": self.diagnostics,
            "assumptions": self.assumptions,
            "validation_state": self.validation_state,
            "model_version": self.model_version,
            "input_hash": self.input_hash,
            "forecast_version": self.forecast_version,
        }


@dataclass(frozen=True)
class MicrostructureEvidenceV2:
    """D10 unified analytical evidence object (compose-only, additive)."""

    symbol: str
    venue: str
    evidence_id: str
    generated_at_ms: int
    tick_size: str
    depth_estimator: str
    input_hash: str
    model_version: str
    vector_version: str | None = None
    x_t: dict[str, Any] | None = None
    horizon_ms: int | None = None
    expected_dP_ticks: str | None = None
    variance_ticks: str | None = None
    se: str | None = None
    interval_lo_95: str | None = None
    interval_hi_95: str | None = None
    p_positive: str | None = None
    p_target: dict[str, Any] | None = None
    p_invalidation: dict[str, Any] | None = None
    hypothesis: str | None = None
    effect: str | None = None
    evidence: dict[str, Any] | None = None
    n: int | None = None
    split: str | None = None
    multiplicity_adj: str | None = None
    oos_info: dict[str, Any] | None = None
    events: list[dict[str, Any]] | None = None
    legacy_fit: dict[str, Any] | None = None
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "venue": self.venue,
            "evidence_id": self.evidence_id,
            "generated_at_ms": self.generated_at_ms,
            "tick_size": self.tick_size,
            "depth_estimator": self.depth_estimator,
            "input_hash": self.input_hash,
            "model_version": self.model_version,
            "vector_version": self.vector_version,
            "x_t": self.x_t,
            "horizon_ms": self.horizon_ms,
            "expected_dP_ticks": self.expected_dP_ticks,
            "variance_ticks": self.variance_ticks,
            "se": self.se,
            "interval_lo_95": self.interval_lo_95,
            "interval_hi_95": self.interval_hi_95,
            "p_positive": self.p_positive,
            "p_target": self.p_target,
            "p_invalidation": self.p_invalidation,
            "hypothesis": self.hypothesis,
            "effect": self.effect,
            "evidence": self.evidence,
            "n": self.n,
            "split": self.split,
            "multiplicity_adj": self.multiplicity_adj,
            "oos_info": self.oos_info,
            "events": self.events,
            "legacy_fit": self.legacy_fit,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MicrostructureEvidenceV2:
        """Rebuild a v2 evidence object from its transport form (replay seam)."""
        return cls(
            symbol=str(payload["symbol"]).upper(),
            venue=str(payload["venue"]),
            evidence_id=str(payload["evidence_id"]),
            generated_at_ms=int(payload["generated_at_ms"]),
            tick_size=str(payload["tick_size"]),
            depth_estimator=str(payload.get("depth_estimator", "")),
            input_hash=str(payload["input_hash"]),
            model_version=str(payload.get("model_version", EVIDENCE_V2_VERSION)),
            vector_version=payload.get("vector_version"),
            x_t=payload.get("x_t"),
            horizon_ms=payload.get("horizon_ms"),
            expected_dP_ticks=payload.get("expected_dP_ticks"),
            variance_ticks=payload.get("variance_ticks"),
            se=payload.get("se"),
            interval_lo_95=payload.get("interval_lo_95"),
            interval_hi_95=payload.get("interval_hi_95"),
            p_positive=payload.get("p_positive"),
            p_target=payload.get("p_target"),
            p_invalidation=payload.get("p_invalidation"),
            hypothesis=payload.get("hypothesis"),
            effect=payload.get("effect"),
            evidence=payload.get("evidence"),
            n=payload.get("n"),
            split=payload.get("split"),
            multiplicity_adj=payload.get("multiplicity_adj"),
            oos_info=payload.get("oos_info"),
            events=payload.get("events"),
            legacy_fit=payload.get("legacy_fit"),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )
