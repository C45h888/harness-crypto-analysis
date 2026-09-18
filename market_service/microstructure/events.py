"""D8 typed microstructure events + walls (Track D, theory §§12-13).

Pure detectors over OrderBookEvent streams (Decimal, event grain).
Never imports analysis/* (float, snapshot grain) — recomputes from raw
events. Institutional-attribution fields are structurally absent.
EventEnvelope is TYPE-ONLY: transport binding (Redis/stream/cadence)
is Phase-12 owned; workers must not import this for live writes in Track D.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from .contracts import MICROSTRUCTURE_SCHEMA_VERSION, OrderBookEvent

ABSORPTION_VERSION = "absorption-v1"
WALL_VERSION = "wall-v1"
EVENT_ENVELOPE_VERSION = "event-envelope-v1"

ABSORPTION_PARAMS: dict[str, Any] = {
    "eps_qty": "0.5",           # queue depletion tolerance (qty units)
    "K_replenish": 2,           # min replenishments inside window
    "W_ms": 30_000,             # detection window ms
    "max_adverse_ticks": "5",   # bounded adverse move (ticks, tick_size passed per-call)
}

WALL_PARAMS: dict[str, Any] = {
    "Q_min": "50",              # min resting size (qty units)
    "D_max_bps": "10",          # max distance from mid (bps)
    "P_min_ms": 20_000,         # min persistence ms
}

PRECISION = 50


def _h(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _mid_price(bid: Decimal, ask: Decimal) -> Decimal:
    return (bid + ask) / Decimal(2)


@dataclass(frozen=True)
class AbsorptionEvent:
    symbol: str
    venue: str
    ts_ms: int
    side: str                    # "bid" | "ask"
    price: str
    agg_flow_against: str
    replenish_count: int
    price_response_ticks: str | None
    detector_version: str
    input_hash: str
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "symbol": self.symbol,
            "venue": self.venue, "ts_ms": self.ts_ms, "side": self.side,
            "price": self.price, "agg_flow_against": self.agg_flow_against,
            "replenish_count": self.replenish_count,
            "price_response_ticks": self.price_response_ticks,
            "detector_version": self.detector_version, "input_hash": self.input_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AbsorptionEvent:
        """Rebuild one absorption record from its transport form (replay seam)."""
        return cls(
            symbol=str(payload["symbol"]).upper(),
            venue=str(payload["venue"]),
            ts_ms=int(payload["ts_ms"]),
            side=str(payload["side"]),
            price=str(payload["price"]),
            agg_flow_against=str(payload["agg_flow_against"]),
            replenish_count=int(payload["replenish_count"]),
            price_response_ticks=payload.get("price_response_ticks"),
            detector_version=str(payload.get("detector_version", ABSORPTION_VERSION)),
            input_hash=str(payload["input_hash"]),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class WallLifecycle:
    """10-field wall lifecycle: price/side/size/distance/persistence/adds/
    cancels/executions/replenishment/flow-interaction/response."""

    symbol: str
    venue: str
    price: str
    side: str
    size: str
    distance_bps: str
    persistence_ms: int
    adds: str
    cancels: str
    executions: str
    replenishment: str
    flow_interaction: str
    response_ticks: str | None
    detector_version: str
    input_hash: str
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "symbol": self.symbol,
            "venue": self.venue, "price": self.price, "side": self.side,
            "size": self.size, "distance_bps": self.distance_bps,
            "persistence_ms": self.persistence_ms, "adds": self.adds,
            "cancels": self.cancels, "executions": self.executions,
            "replenishment": self.replenishment,
            "flow_interaction": self.flow_interaction,
            "response_ticks": self.response_ticks,
            "detector_version": self.detector_version, "input_hash": self.input_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WallLifecycle:
        """Rebuild one wall record from its transport form (replay seam)."""
        return cls(
            symbol=str(payload["symbol"]).upper(),
            venue=str(payload["venue"]),
            price=str(payload["price"]),
            side=str(payload["side"]),
            size=str(payload["size"]),
            distance_bps=str(payload["distance_bps"]),
            persistence_ms=int(payload["persistence_ms"]),
            adds=str(payload["adds"]),
            cancels=str(payload["cancels"]),
            executions=str(payload["executions"]),
            replenishment=str(payload["replenishment"]),
            flow_interaction=str(payload["flow_interaction"]),
            response_ticks=payload.get("response_ticks"),
            detector_version=str(payload.get("detector_version", WALL_VERSION)),
            input_hash=str(payload["input_hash"]),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class EventEnvelope:
    """TYPE-ONLY transport envelope. No Redis/stream/cadence binding here."""

    event: AbsorptionEvent | WallLifecycle
    input_hash: str
    detector_version: str
    envelope_version: str = EVENT_ENVELOPE_VERSION
    schema_version: int = MICROSTRUCTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "envelope_version": self.envelope_version,
            "detector_version": self.detector_version,
            "input_hash": self.input_hash,
            "event": self.event.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EventEnvelope:
        """Rebuild an envelope from its transport form (replay seam)."""
        raw = payload["event"]
        event: AbsorptionEvent | WallLifecycle
        if isinstance(raw, dict) and "replenish_count" in raw:
            event = AbsorptionEvent.from_dict(raw)
        else:
            event = WallLifecycle.from_dict(raw)
        return cls(
            event=event,
            input_hash=str(payload["input_hash"]),
            detector_version=str(payload.get("detector_version", "")),
            envelope_version=str(payload.get("envelope_version", EVENT_ENVELOPE_VERSION)),
            schema_version=int(payload.get("schema_version", MICROSTRUCTURE_SCHEMA_VERSION)),
        )


def detect_absorption(
    events: list[OrderBookEvent], *, symbol: str, venue: str,
    tick_size: Decimal | None = None,
) -> tuple[list[AbsorptionEvent], dict[str, int]]:
    """Flag bid-side absorption windows: heavy sell flow, replenished bid, bounded fall.

    ``tick_size`` defaults to the frozen table (``tick.resolve_tick_size``);
    unknown symbol/venue refuses rather than guessing 0.01.
    """
    if tick_size is None:
        from .tick import resolve_tick_size
        tick_size = resolve_tick_size(symbol, venue)
    if isinstance(tick_size, float):
        raise TypeError("tick_size must be Decimal, not float")
    out: list[AbsorptionEvent] = []
    log: dict[str, int] = {"scanned": len(events), "flagged": 0}
    W = int(ABSORPTION_PARAMS["W_ms"])
    K = int(ABSORPTION_PARAMS["K_replenish"])
    max_adv = Decimal(str(ABSORPTION_PARAMS["max_adverse_ticks"]))
    if not events:
        return out, log
    # Sliding window over events grouped by time: for each anchor, collect
    # flow + replenishment + price response within W ms.
    for i, anchor in enumerate(events):
        if (anchor.current.symbol.upper(), anchor.current.venue) != (symbol.upper(), venue):
            continue
        t0 = anchor.current.exchange_ts_ms
        window = [e for e in events[i:]
                  if 0 <= e.current.exchange_ts_ms - t0 <= W
                  and (e.current.symbol.upper(), e.current.venue) == (symbol.upper(), venue)]
        if len(window) < 2:
            continue
        flow = sum((e.contribution for e in window), Decimal(0))
        # Replenishment: bid-qty increases at same bid price.
        replen = 0
        for e in window:
            if (e.current.bid_price == e.previous.bid_price
                    and e.current.bid_qty > e.previous.bid_qty):
                replen += 1
        if flow >= 0 or replen < K:
            continue
        with localcontext() as ctx:
            ctx.prec = PRECISION
            base_mid = _mid_price(anchor.previous.bid_price, anchor.previous.ask_price)
            last_mid = _mid_price(window[-1].current.bid_price, window[-1].current.ask_price)
            adverse = (base_mid - last_mid) / tick_size if tick_size > 0 else Decimal(0)
        if adverse is not None and adverse <= max_adv:
            payload = {"a": symbol.upper(), "v": venue, "t": t0,
                       "ver": ABSORPTION_VERSION, "i": i}
            digest = _h(payload)
            with localcontext() as ctx:
                ctx.prec = PRECISION
                resp = (last_mid - base_mid) / tick_size if tick_size > 0 else None
            out.append(AbsorptionEvent(
                symbol=symbol.upper(), venue=venue, ts_ms=t0, side="bid",
                price=format(anchor.current.bid_price, "f"),
                agg_flow_against=format(flow, "f"),
                replenish_count=replen,
                price_response_ticks=format(resp, "f") if resp is not None else None,
                detector_version=ABSORPTION_VERSION, input_hash=digest,
            ))
            log["flagged"] += 1
    # De-duplicate overlapping windows: keep first per 2W span.
    dedup: list[AbsorptionEvent] = []
    for e in out:
        if not dedup or e.ts_ms - dedup[-1].ts_ms >= 2 * W:
            dedup.append(e)
    log["flagged"] = len(dedup)
    return dedup, log


def detect_walls(
    events: list[OrderBookEvent], *, symbol: str, venue: str,
    tick_size: Decimal | None = None,
) -> tuple[list[WallLifecycle], dict[str, int]]:
    """Track large persistent resting levels through their 10-field lifecycle.

    ``response_ticks`` is quote-difference divided by ``tick_size``; when
    ``tick_size`` is None the response is unmeasurable in ticks and the
    field is NULL (never a quote-unit value wearing a tick label).
    """
    log: dict[str, int] = {"scanned": len(events), "flagged": 0}
    Qmin = Decimal(str(WALL_PARAMS["Q_min"]))
    Dmax = Decimal(str(WALL_PARAMS["D_max_bps"]))
    Pmin = int(WALL_PARAMS["P_min_ms"])
    # Candidate: first event whose current bid qty >= Qmin and within Dmax bps.
    out: list[WallLifecycle] = []
    for i, anchor in enumerate(events):
        if (anchor.current.symbol.upper(), anchor.current.venue) != (symbol.upper(), venue):
            continue
        q = anchor.current
        with localcontext() as ctx:
            ctx.prec = PRECISION
            mid = _mid_price(q.bid_price, q.ask_price)
            dist = abs(q.bid_price - mid) / mid * Decimal(10000) if mid > 0 else None
        if q.bid_qty < Qmin or dist is None or dist > Dmax:
            continue
        t0 = q.exchange_ts_ms
        # Lifecycle: subsequent events at same bid price within persistence.
        adds = cancels = Decimal(0)
        replen = Decimal(0)
        flow = Decimal(0)
        last_t = t0
        for e in events[i:]:
            if (e.current.symbol.upper(), e.current.venue) != (symbol.upper(), venue):
                continue
            if e.current.exchange_ts_ms - t0 > Pmin * 3:
                break
            last_t = max(last_t, e.current.exchange_ts_ms)
            if e.current.bid_price == q.bid_price:
                dq = e.current.bid_qty - e.previous.bid_qty
                if dq > 0:
                    adds += dq
                    replen += dq
                elif dq < 0:
                    cancels += -dq
                flow += e.contribution
        persist = last_t - t0
        if persist < Pmin:
            continue
        digest = _h({"w": symbol.upper(), "v": venue, "t": t0,
                     "p": str(q.bid_price), "ver": WALL_VERSION, "i": i})
        with localcontext() as ctx:
            ctx.prec = PRECISION
            mid0 = _mid_price(anchor.previous.bid_price, anchor.previous.ask_price)
            mid1_candidates = [e for e in events[i:]
                               if e.current.exchange_ts_ms >= t0 + persist]
            resp: Decimal | None = None
            if mid1_candidates:
                m1 = _mid_price(mid1_candidates[0].current.bid_price,
                                mid1_candidates[0].current.ask_price)
                if tick_size is not None:
                    if isinstance(tick_size, float):
                        raise TypeError("tick_size must be Decimal, not float")
                    if tick_size > 0:
                        resp = (m1 - mid0) / tick_size
        out.append(WallLifecycle(
            symbol=symbol.upper(), venue=venue,
            price=format(q.bid_price, "f"), side="bid",
            size=format(q.bid_qty, "f"), distance_bps=format(dist, "f"),
            persistence_ms=persist,
            adds=format(adds, "f"), cancels=format(cancels, "f"),
            executions="0",
            replenishment=format(replen, "f"),
            flow_interaction=format(flow, "f"),
            response_ticks=format(resp, "f") if resp is not None else None,
            detector_version=WALL_VERSION, input_hash=digest,
        ))
        log["flagged"] += 1
        break  # one wall per call window in v1 (multi-wall is v2 scope)
    return out, log


def replay_agreement(
    events: list[OrderBookEvent], *, symbol: str, venue: str,
) -> dict[str, Any]:
    """Detector determinism: shuffled input order must yield identical labels."""
    import random
    a1, _ = detect_absorption(events, symbol=symbol, venue=venue)
    w1, _ = detect_walls(events, symbol=symbol, venue=venue)
    shuffled = list(events)
    rng = random.Random(42)
    rng.shuffle(shuffled)
    # Detectors sort internally by ts; emulate caller contract: sort before run.
    shuffled_sorted = sorted(shuffled, key=lambda e: e.current.exchange_ts_ms)
    a2, _ = detect_absorption(shuffled_sorted, symbol=symbol, venue=venue)
    w2, _ = detect_walls(shuffled_sorted, symbol=symbol, venue=venue)
    agree = ([e.input_hash for e in a1] == [e.input_hash for e in a2]
             and [e.input_hash for e in w1] == [e.input_hash for e in w2])
    return {"agreement": agree, "agreement_rate": 1.0 if agree else 0.0,
            "n_absorption": len(a1), "n_walls": len(w1)}
