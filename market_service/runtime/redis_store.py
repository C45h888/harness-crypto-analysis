"""Redis operational store for latest state, streams, and refresh commands."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from redis.asyncio import Redis

from .scope import CANONICAL_VENUE, normalize_symbol, normalize_venue

from .contracts import (
    AGENT_MEMORY_SCHEMA_VERSION, AgentMemory, CanonicalOutput,
    SCHEMA_VERSION_REGISTRY,
)


# Schema version for stream-entries on the agent-artifact namespaces.
# Each artifact_type maps to the contract version its payload validates
# against so downstream tooling can read version from the stream-field
# instead of having to decode the JSON payload first.
_AGENT_ARTIFACT_SCHEMA_VERSION: dict[str, int] = {
    "observations": 1,
    "hypotheses": 1,
    "memory": AGENT_MEMORY_SCHEMA_VERSION,
}


def _slim_stream_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Slim a raw-evidence snapshot down to what STREAM consumers read.

    Phase H1 (docs/HORIZON_RETENTION_SPEC.md §1.2): ``build_raw_window``
    reads ONLY ``trades_normalized`` from the stream — book, tickers, and
    the raw Binance trade arrays all come from the single ``latest``
    snapshot. Carrying them in every 5s stream entry is the memory cost
    that made wide (12h) retention impossible. The slimmed body keeps:
    observed_at(_ms), depth_levels, errors, coverage, trades_normalized,
    and the tiny funding/OI objects. ``read_raw_latest`` keeps the FULL
    payload — point-in-time readers are unaffected.
    """
    def _side(side: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v for k, v in side.items()
            if k in ("trades_normalized", "funding", "open_interest")
        }

    return {
        **{k: v for k, v in payload.items() if k not in ("spot", "futures")},
        "spot": _side(payload.get("spot") or {}),
        "futures": _side(payload.get("futures") or {}),
    }


def normalize_payload(namespace: str, payload: Any) -> Any:
    """Canonicalise identity tokens before any contract check.

    NORMALISATION LAYER (write-side, central): ``symbol`` becomes UPPER,
    ``venue`` becomes the canonical vocabulary (``perps`` -> ``futures``).
    Only the two identity keys are touched; every other field passes
    through byte-identical so writer payloads are never reshaped here.
    Unknown venue tokens are left lowercased so the capability gate (not
    the store) owns the refusal - the store never invents an identity.
    """
    if not isinstance(payload, Mapping):
        return payload
    out = dict(payload)
    symbol = out.get("symbol")
    if isinstance(symbol, str) and symbol.strip():
        try:
            out["symbol"] = normalize_symbol(symbol)
        except ValueError:
            pass
    venue = out.get("venue")
    if isinstance(venue, str) and venue.strip():
        try:
            out["venue"] = normalize_venue(venue)
        except ValueError:
            out["venue"] = venue.strip().lower()
    return out


def pare_stream_body(namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
    """PARING LAYER (write-side, central): slim stream bodies.

    ``latest`` keys keep the FULL payload (point-in-time readers are
    unaffected). Stream entries carry only what stream consumers read:
    today that is just ``raw.stream`` (book / tickers / raw trade arrays
    stripped, ``trades_normalized`` kept). Every other namespace pares to
    identity so no second slimming site can drift. Add a new pared family
    here, never in a publisher.
    """
    if namespace == "raw.stream":
        return _slim_stream_body(payload)
    return payload


# ===========================================================================
# NAMESPACE CONFIGURATION — the store's spine.
#
# A namespace is the ONLY declaration of a Redis key family: its key template,
# what kind of value it holds, which plane writes it, the payload contract the
# writer must satisfy, and the retention it is stored under. Write paths derive
# their key from this table (``key(ns_id, ...)``) and are validated against it
# (``_contract``) — there is no second place where a key or a payload shape is
# decided.
#
# The worker families (substrate.*, analysis.*) are written out EXPLICITLY
# below. They are configured, not discovered: importing a worker registry here
# would make the surface depend on import order, and a new worker would silently
# acquire (or fail to acquire) keys. ``tests/test_store_namespaces.py`` pins the
# literal lists against the worker registries so drift fails loudly.
# ===========================================================================

class StoreContractError(RuntimeError):
    """A payload violated its namespace contract (strict write-side gate)."""


#: Age precedence for read surfaces — first present field wins; unknown is
#: None, never 0.
DEFAULT_AGE_FIELDS: tuple[str, ...] = (
    "computed_at_ms", "observed_at_ms", "received_ts_ms", "ts_ms",
    "updated_at_ms", "generated_at", "decided_at_ms",
)

Kind = Literal["latest", "stream", "text", "sequence", "marker"]


@dataclass(frozen=True)
class Retention:
    """Storage policy for one namespace family."""

    maxlen: int | None = None          # stream cap (None = store default)
    ttl_s: int | None = None           # latest-key TTL (None = persist)
    retention_ms: int | None = None    # time-based trim floor (raw stream)
    guardrail_maxlen: int | None = None  # pathological-burst count cap


@dataclass(frozen=True)
class Namespace:
    """One Redis key family, declared as configuration."""

    id: str
    template: str                       # prefix-relative, ``{token}`` slots
    kind: Kind
    writer: str                         # owning plane (module path)
    schema: str | None = None           # contracts.SCHEMA_VERSION_REGISTRY key
    required: tuple[str, ...] = ()      # payload keys a valid write must carry
    age_fields: tuple[str, ...] = DEFAULT_AGE_FIELDS
    retention: Retention = Retention()
    description: str = ""


#: Worker names — CONFIGURED here (see module note above); pinned to the
#: worker registries by test.
SUBSTRATE_NAMES: tuple[str, ...] = (
    "anchors", "delta", "density", "ladders", "large_print", "migration",
    "oi", "signals", "tape", "technicals", "tiers", "volume_profile",
)
ANALYSIS_NAMES: tuple[str, ...] = (
    "demand", "liquidation", "oi_analysis", "regime", "stage",
    "wall_migration",
)


def _fixed_namespaces() -> tuple[Namespace, ...]:
    return (
        Namespace(
            id="raw.latest", template="latest:{symbol}:raw", kind="latest",
            writer="poller", required=("observed_at_ms",),
            description="Latest raw poller evidence snapshot.",
        ),
        Namespace(
            id="raw.stream", template="stream:raw:{symbol}", kind="stream",
            writer="poller", required=("observed_at_ms",),
            retention=Retention(retention_ms=43_200_000,
                                guardrail_maxlen=20_000),
            description="Time-trimmed raw evidence stream.",
        ),
        Namespace(
            id="raw.dedupe", template="raw-dedupe:{symbol}:{observed_at_ms}",
            kind="latest", writer="poller",
            description="Idempotency guard for one raw snapshot.",
        ),
        Namespace(
            id="derivatives.latest", template="latest:{symbol}:derivatives",
            kind="latest", writer="poller", retention=Retention(ttl_s=300),
            description="TTL-bounded derivative evidence projection.",
        ),
        Namespace(
            id="derivatives.stream", template="stream:domain:derivatives:{symbol}",
            kind="stream", writer="poller",
            description="Derivative evidence fetch history.",
        ),
        Namespace(
            id="micro.raw", template="stream:microstructure:raw:{venue}:{symbol}",
            kind="stream", writer="microstructure.capture",
            schema="microstructure", required=("symbol", "venue"),
            description="Depth-delta frames from the exchange stream.",
        ),
        Namespace(
            id="micro.events", template="stream:microstructure:events:{venue}:{symbol}",
            kind="stream", writer="microstructure.capture",
            schema="microstructure", required=("event_type", "current"),
            description="Best-quote transition ledger (replay source).",
        ),
        Namespace(
            id="micro.ofi", template="stream:microstructure:ofi:{venue}:{symbol}",
            kind="stream", writer="microstructure.capture",
            schema="microstructure", required=("symbol", "venue", "end_ts_ms"),
            description="Completed deterministic OFI/AD intervals.",
        ),
        Namespace(
            id="micro.book", template="latest:microstructure:{venue}:{symbol}:book",
            kind="latest", writer="microstructure.capture",
            schema="microstructure",
            required=("symbol", "venue", "bid_price", "ask_price"),
            description="Best-quote projection (live top of book).",
        ),
        Namespace(
            id="micro.status", template="latest:microstructure:{venue}:{symbol}:status",
            kind="latest", writer="microstructure.capture", required=("state",),
            description="Capture worker state (liveness, gaps, reconnects).",
        ),
        Namespace(
            id="micro.status.events",
            template="stream:microstructure:status:{venue}:{symbol}",
            kind="stream", writer="microstructure.capture", required=("state",),
            description="Capture state transitions (written on change only).",
        ),
        Namespace(
            id="micro.evidence", template="latest:microstructure:{venue}:{symbol}:evidence",
            kind="latest", writer="microstructure.fitting", schema="microstructure",
            required=("symbol", "venue"),
            description="Immutable fitted microstructure evidence.",
        ),
        Namespace(
            id="inference.latest", template="latest:inference:{venue}:{symbol}",
            kind="latest", writer="engine (canonical output)",
            schema="canonical_output", required=("run_id", "symbol", "venue"),
            description="Latest canonical run record projection.",
        ),
        Namespace(
            id="inference.stream", template="stream:inference:{venue}:{symbol}",
            kind="stream", writer="engine (canonical output)",
            schema="canonical_output", required=("run_id", "symbol", "venue"),
            description="Canonical output history for one identity.",
        ),
        Namespace(
            id="poller.active", template="poller:active_symbols", kind="sequence",
            writer="poller / operator control key",
            description="The LIVE active-symbol set.",
        ),
        Namespace(
            id="poller.status", template="poller:status", kind="latest",
            writer="poller", description="Poller heartbeat / per-symbol status.",
        ),
        Namespace(
            id="agent.artifacts", template="agent:{session}:{artifact}", kind="text",
            writer="nooa_harness.memory",
            description="Agent-owned memory stream (Postgres is the ledger).",
        ),
        Namespace(
            id="governance.latest",
            template="latest:governance:{plane}:{name}:{symbol}", kind="latest",
            writer="governance.ledger", schema="governance_ledger",
            required=("plane", "symbol", "gate"),
            description="Latest governance decision per plane+substrate.",
        ),
        Namespace(
            id="governance.stream", template="stream:governance:{plane}:{symbol}",
            kind="stream", writer="governance.ledger", schema="governance_ledger",
            required=("plane", "symbol", "gate"),
            description="Governance decision ledger (Redis projection).",
        ),
        Namespace(
            id="governance.denials",
            template="stream:governance:denials:{plane}:{symbol}", kind="stream",
            writer="governance.ledger", schema="governance_ledger",
            required=("plane", "symbol", "gate"),
            description="Denied decisions only (fast audit read).",
        ),
        Namespace(
            id="governance.supervisor",
            template="governance:{plane}:{symbol}:supervisor", kind="latest",
            writer="governance.ledger",
            description="Governance supervisor liveness marker.",
        ),
        # -- telemetry plane -------------------------------------------
        # The isolated telemetry namespace (telemetry_kernel/store.py). It
        # owns no market/worker/governance/inference key, and its keys are
        # configured here like every other family so the surface stays whole.
        Namespace(
            id="telemetry.events",
            template="telemetry:events:{plane}:{owner}:{subject_type}:{subject_id}",
            kind="stream", writer="telemetry_kernel.store",
            schema="telemetry_event",
            required=("event_id", "plane", "owner", "subject_type",
                      "subject_id", "occurred_at_ms"),
            description="Isolated telemetry event stream per subject.",
        ),
        Namespace(
            id="telemetry.latest",
            template="telemetry:latest:{plane}:{owner}:{subject_type}:{subject_id}",
            kind="latest", writer="telemetry_kernel.store",
            schema="telemetry_event",
            required=("plane", "owner", "subject_type", "subject_id",
                      "last_event_id", "last_event_at_ms"),
            description="Telemetry projection: newest accepted state per subject.",
        ),
        Namespace(
            id="telemetry.health", template="telemetry:health:{owner}",
            kind="latest", writer="telemetry_kernel.store",
            description="Per-owner sink health (never routed through events).",
        ),
        Namespace(
            id="telemetry.cursor", template="telemetry:cursor:{consumer}",
            kind="latest", writer="telemetry_kernel.store",
            description="Durable replay cursor for one read consumer.",
        ),
        Namespace(
            id="telemetry.dedupe", template="telemetry:event-id:{event_id}",
            kind="marker", writer="telemetry_kernel.store",
            retention=Retention(ttl_s=2_592_000),
            description="Idempotency marker, one per accepted event id.",
        ),
    )


def _worker_namespaces() -> tuple[Namespace, ...]:
    """Concrete namespaces for the CONFIGURED worker families (eager)."""
    specs: list[Namespace] = []
    for name in SUBSTRATE_NAMES:
        specs.extend((
            Namespace(
                id=f"substrate.{name}",
                template=f"latest:substrate:{name}:{{symbol}}", kind="latest",
                writer=f"substrate_worker.{name}", schema="substrate_state",
                required=("substrate", "symbol", "computed_at_ms"),
                description=f"{name} calculation projection (always fresh).",
            ),
            Namespace(
                id=f"substrate.{name}.events",
                template=f"stream:substrate:{name}:{{symbol}}", kind="stream",
                writer=f"substrate_worker.{name}", schema="substrate_state",
                required=("substrate", "symbol", "computed_at_ms"),
                description=f"{name} calculation state history.",
            ),
            Namespace(
                id=f"substrate.{name}.supervisor",
                template=f"substrate:{name}:{{symbol}}:supervisor", kind="latest",
                writer=f"substrate_worker.{name}",
                description=f"{name} worker heartbeat marker.",
            ),
        ))
    for name in ANALYSIS_NAMES:
        specs.extend((
            Namespace(
                id=f"analysis.{name}",
                template=f"latest:analysis:{name}:{{symbol}}", kind="latest",
                writer=f"analysis_worker.{name}", schema="substrate_state",
                required=("substrate", "symbol", "computed_at_ms"),
                description=f"{name} analysis projection (always fresh).",
            ),
            Namespace(
                id=f"analysis.{name}.events",
                template=f"stream:analysis:{name}:{{symbol}}", kind="stream",
                writer=f"analysis_worker.{name}", schema="substrate_state",
                required=("substrate", "symbol", "computed_at_ms"),
                description=f"{name} analysis state history.",
            ),
            Namespace(
                id=f"analysis.{name}.supervisor",
                template=f"analysis:{name}:{{symbol}}:supervisor", kind="latest",
                writer=f"analysis_worker.{name}",
                description=f"{name} worker heartbeat marker.",
            ),
        ))
    return tuple(specs)


#: The whole configured surface. Built once, at import, from the literals above.
NAMESPACES: dict[str, Namespace] = {
    spec.id: spec for spec in _fixed_namespaces() + _worker_namespaces()
}


def _namespace_spec(namespace: str) -> Namespace:
    """Return the configured namespace (raises on an undeclared id)."""
    try:
        return NAMESPACES[namespace]
    except KeyError:
        raise StoreContractError(
            f"unknown namespace {namespace!r}; configured: "
            f"{', '.join(sorted(NAMESPACES))}") from None


def namespace_key(prefix: str, namespace: str, **ids: Any) -> str:
    """Render one configured key. THE key constructor (single template)."""
    spec = _namespace_spec(namespace)
    try:
        body = spec.template.format(**ids)
    except KeyError as exc:
        raise StoreContractError(
            f"{namespace}: missing key token {exc}") from None
    return f"{prefix.strip(':')}:{body}"


def namespace_contract(namespace: str, payload: Any) -> Any:
    """Strict write-side contract shared by every writer of ``namespace``."""
    spec = _namespace_spec(namespace)
    if spec.kind == "text":
        if not isinstance(payload, str) or not payload:
            raise StoreContractError(
                f"{namespace}: payload must be a non-empty string")
        return payload
    if spec.kind == "sequence":
        if not isinstance(payload, (list, tuple)) or not payload:
            raise StoreContractError(
                f"{namespace}: payload must be a non-empty sequence")
        return list(payload)
    if spec.kind == "marker":
        if not isinstance(payload, str) or not payload:
            raise StoreContractError(
                f"{namespace}: marker must be a non-empty string")
        return payload
    if not isinstance(payload, Mapping):
        raise StoreContractError(
            f"{namespace}: payload must be an object, "
            f"got {type(payload).__name__}")
    missing = [key for key in spec.required if key not in payload]
    if missing:
        raise StoreContractError(
            f"{namespace}: payload missing required keys {missing} "
            f"(writer: {spec.writer})")
    if spec.schema:
        expected = SCHEMA_VERSION_REGISTRY.get(spec.schema)
        version = payload.get("schema_version")
        if version is None:
            payload = {**payload, "schema_version": expected}
        elif int(version) != expected:
            raise StoreContractError(
                f"{namespace}: schema_version {version} != {expected} "
                f"for {spec.schema!r} (stale writer?)")
    return payload


def namespace_retention(namespace: str) -> Retention:
    """Storage policy declared for ``namespace``."""
    return _namespace_spec(namespace).retention

#: Key families from RETIRED planes. Nothing reads or writes them; they are
#: listed here so ``purge_retired()`` can drop their leftovers from a live
#: Redis and so a stale reader can never be pointed at them again.
RETIRED_KEY_PATTERNS: tuple[str, ...] = (
    "{prefix}:latest:*:collated",
    "{prefix}:stream:collated:*",
    "{prefix}:latest:*:data-access",
    "{prefix}:latest:*:calculations",
    "{prefix}:latest:*:analysis",
    "{prefix}:run:*",
    "{prefix}:runtime-run:*",
    "{prefix}:latest:horizon:*",
    "{prefix}:stream:horizon:*",
    "{prefix}:horizon:*",
)


class RedisRuntimeStore:
    """Small typed adapter; callers never need to know Redis key names."""

    def __init__(self, url: str, prefix: str = "marketflow", stream_maxlen: int = 10_000,
                 postgres_store: Any | None = None,
                 raw_retention_ms: int = 43_200_000,
                 raw_guardrail_maxlen: int = 20_000):
        self.redis: Redis = Redis.from_url(url, decode_responses=True)
        self.prefix = prefix.strip(":")
        self.stream_maxlen = stream_maxlen
        # Phase H1 — time-based retention for the raw evidence stream
        # (docs/HORIZON_RETENTION_SPEC.md). The raw stream is trimmed by
        # MINID (now - retention) so the observable horizon is TIME-
        # predictable in an unbounded market; ``stream_maxlen`` remains
        # only as a pathological-burst memory guardrail. Default 12h
        # serves the widest declared worker horizon (4h) with margin.
        self.raw_retention_ms = int(raw_retention_ms)
        # Phase H1 — the raw stream's count guardrail is SEPARATE from
        # ``stream_maxlen`` (which caps the substrate/analysis state
        # streams and defaults to ~100min at the 5s cadence — far below
        # the 12h retention). A count cap SMALLER than the time retention
        # would silently defeat the MINID contract, so the raw guardrail
        # defaults to the spec §1.3 burst ceiling (20 000 entries ≈ 28h
        # at 5s cadence).
        self.raw_guardrail_maxlen = int(raw_guardrail_maxlen)
        # Layer C: optional PostgresRuntimeStore companion for
        # durable wall-history reads/writes. When present, the
        # analysis adapter uses postgres in preference to Redis.
        self._postgres_store = postgres_store

    @staticmethod
    def _prefixed(prefix: str, key: str) -> str:
        return f"{prefix.strip(':')}:{key}"

    # ------------------------------------------------------------------
    # Namespace configuration — the spine every write path resolves through.
    # ------------------------------------------------------------------
    @staticmethod
    def ns(namespace: str) -> Namespace:
        """Return the configured namespace (raises on an undeclared id)."""
        return _namespace_spec(namespace)

    def key(self, namespace: str, **ids: Any) -> str:
        """Build one namespace key from the configuration (the ONLY way)."""
        return namespace_key(self.prefix, namespace, **ids)

    def _contract(self, namespace: str, payload: Any) -> Any:
        """Strict write-side gate: shape, required keys, schema version.

        A violation raises ``StoreContractError`` — a writer that drifts from
        its namespace contract fails at the write, not silently in a reader
        three planes away. A payload missing ``schema_version`` for a
        schema-bearing namespace is STAMPED, so the contractual layer is kept
        in harmony at exactly one place.
        """
        return namespace_contract(namespace, payload)

    def _maxlen(self, namespace: str, override: int | None = None) -> int:
        """Stream cap: the namespace config wins, then the caller, then default."""
        spec = self.ns(namespace)
        if spec.retention.maxlen is not None:
            return int(spec.retention.maxlen)
        return int(override if override is not None else self.stream_maxlen)

    def _stream_cap(self, namespace: str, override: int | None = None) -> int:
        """Count cap for one stream append (guardrail-aware)."""
        spec = self.ns(namespace)
        if spec.retention.maxlen is not None:
            return int(spec.retention.maxlen)
        if spec.retention.guardrail_maxlen is not None:
            return int(spec.retention.guardrail_maxlen)
        if namespace == "raw.stream":
            return int(self.raw_guardrail_maxlen)
        return int(override if override is not None else self.stream_maxlen)

    def _ttl(self, namespace: str, override: int | None = None) -> int | None:
        """TTL for one latest write: namespace config wins, then caller."""
        spec = self.ns(namespace)
        if spec.retention.ttl_s is not None:
            return int(spec.retention.ttl_s)
        if override is not None:
            if int(override) <= 0:
                raise ValueError("ttl_s must be positive")
            return int(override)
        return None

    @staticmethod
    def _encode(payload: Any) -> str:
        return json.dumps(payload, default=str, separators=(",", ":"))

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any] | None:
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            return None
        try:
            value = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    async def publish_latest(
        self, namespace: str, payload: Any, *, ids: dict[str, Any],
        ttl_s: int | None = None,
    ) -> str:
        """Shared latest-only write: normalise -> contract -> SET.

        The model later reads this key verbatim (rawest pared form); no
        shaping happens here beyond normalisation + contract stamping.
        Returns the Redis key written.
        """
        payload = self._contract(namespace, normalize_payload(namespace, payload))
        key = self.key(namespace, **ids)
        body = self._encode(payload)
        ttl = self._ttl(namespace, ttl_s)
        if ttl is not None:
            await self.redis.set(key, body, ex=int(ttl))
        else:
            await self.redis.set(key, body)
        return key

    async def publish_stream(
        self, namespace: str, payload: Any, *, ids: dict[str, Any],
        maxlen: int | None = None, extra_fields: dict[str, Any] | None = None,
        ts_field: str | None = None,
    ) -> str:
        """Shared stream-only append: normalise -> contract -> pare -> XADD."""
        payload = self._contract(namespace, normalize_payload(namespace, payload))
        guard = dict(payload) if isinstance(payload, Mapping) else {}
        stream_doc = pare_stream_body(
            namespace, guard if isinstance(guard, dict) else {},
        ) if isinstance(payload, Mapping) else payload
        key = self.key(namespace, **ids)
        body = self._encode(stream_doc)
        fields: dict[str, Any] = {"payload": body}
        if ts_field is not None and isinstance(payload, Mapping):
            fields["ts"] = str(payload.get(ts_field, "") or "")
        elif isinstance(payload, Mapping):
            for candidate in ("ts", "end_ts_ms", "received_ts_ms",
                              "observed_at_ms", "updated_at_ms"):
                if payload.get(candidate) is not None:
                    fields["ts"] = str(payload.get(candidate))
                    break
        if extra_fields:
            fields.update({k: str(v) for k, v in extra_fields.items()})
        cap = self._stream_cap(namespace, maxlen)
        return str(await self.redis.xadd(
            key, fields, maxlen=max(1, int(cap)), approximate=True,
        ))

    async def publish_latest_and_stream(
        self, latest_ns: str, stream_ns: str, payload: Any, *,
        ids_latest: dict[str, Any], ids_stream: dict[str, Any] | None = None,
        ttl_s: int | None = None, maxlen: int | None = None,
        extra_fields: dict[str, Any] | None = None, ts_field: str | None = None,
        slim_stream: bool = False,
    ) -> str:
        """Shared atomic SET(latest) + XADD(stream) via one Lua script.

        Closes the ordering hole every paired publisher used to close on its
        own: no reader observes a latest snapshot whose stream entry is
        missing. ``slim_stream`` routes the stream body through the paring
        layer (today only ``raw.stream`` pares; other families are identity).
        """
        payload = self._contract(latest_ns, normalize_payload(latest_ns, payload))
        if stream_ns != latest_ns:
            self._contract(stream_ns, payload)
        latest_key = self.key(latest_ns, **ids_latest)
        stream_key = self.key(stream_ns, **(ids_stream or ids_latest))
        latest_body = self._encode(payload)
        stream_doc = pare_stream_body(stream_ns, dict(payload)) \
            if slim_stream and isinstance(payload, Mapping) else payload
        if stream_ns != latest_ns and isinstance(payload, Mapping):
            stream_doc = pare_stream_body(stream_ns, dict(payload))
        stream_body = self._encode(stream_doc)
        ttl = self._ttl(latest_ns, ttl_s)
        cap = self._stream_cap(stream_ns, maxlen)
        ts = ""
        if isinstance(payload, Mapping):
            if ts_field is not None:
                ts = str(payload.get(ts_field, "") or "")
            else:
                for candidate in ("computed_at_ms", "observed_at_ms", "ts",
                                  "received_ts_ms", "updated_at_ms",
                                  "decided_at_ms", "generated_at"):
                    if payload.get(candidate) is not None:
                        ts = str(payload.get(candidate))
                        break
        fields = {"payload": stream_body, "ts": ts}
        if extra_fields:
            fields.update({k: str(v) for k, v in extra_fields.items()})
        field_names = list(fields.keys())
        field_vals = [fields[n] for n in field_names]
        # ARGV layout: [1]=latest_body, [2..1+N]=stream field values.
        pairs = ", ".join(
            f"'{name}', ARGV[{i + 2}]" for i, name in enumerate(field_names)
        )
        if ttl is not None:
            script = (
                f"redis.call('SET', KEYS[1], ARGV[1], 'EX', {int(ttl)})\n"
                f"return redis.call('XADD', KEYS[2], 'MAXLEN', '~', "
                f"{int(cap)}, '*', {pairs})"
            )
        else:
            script = (
                "redis.call('SET', KEYS[1], ARGV[1])\n"
                f"return redis.call('XADD', KEYS[2], 'MAXLEN', '~', "
                f"{int(cap)}, '*', {pairs})"
            )
        return str(await self.redis.eval(
            script, 2, latest_key, stream_key, latest_body, *field_vals,
        ))

    async def read_latest(self, namespace: str, *, ids: dict[str, Any]) -> Any:
        """Shared latest read: resolve key -> GET -> decode verbatim.

        Returns the stored document in its rawest pared form (dict / str /
        list) or ``None`` when absent or undecodable. Never shapes, never
        fabricates - a missing key is ``None``, not an empty object.
        """
        spec = self.ns(namespace)
        key = self.key(namespace, **ids)
        raw = await self.redis.get(key)
        if not raw:
            return None
        if spec.kind == "text":
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            return raw if isinstance(raw, str) and raw else None
        if spec.kind == "sequence":
            try:
                value = json.loads(raw) if isinstance(raw, str) else None
            except (ValueError, json.JSONDecodeError):
                return None
            if isinstance(value, bytes):
                try:
                    value = json.loads(value.decode("utf-8", errors="replace"))
                except (ValueError, json.JSONDecodeError, AttributeError):
                    return None
            return value if isinstance(value, list) and value else None
        return self._decode(raw)

    async def read_stream(
        self, namespace: str, *, ids: dict[str, Any], count: int | None = None,
        newest_first: bool = True, start: str = "-", end: str = "+",
    ) -> list[dict[str, Any]]:
        """Shared stream read: resolve key -> XRANGE/XREVRANGE -> decode.

        Each entry's ``payload`` field carries one JSON document; entries
        that do not decode are skipped (never fabricated). Newest-first by
        default (live reads); oldest-first when replaying a window.
        """
        self.ns(namespace)
        key = self.key(namespace, **ids)
        if newest_first:
            rows = await self.redis.xrevrange(key, count=count)
        else:
            rows = await self.redis.xrange(key, min=start, max=end, count=count)
        out: list[dict[str, Any]] = []
        for _entry_id, fields in rows or []:
            raw = (fields or {}).get("payload")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if not isinstance(raw, str):
                continue
            try:
                value = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                out.append(value)
        return out

    @staticmethod
    def _normalize_ids(ids: dict[str, Any]) -> dict[str, Any]:
        """Canonicalise identity tokens inside key ids (symbol/venue)."""
        out = dict(ids or {})
        if isinstance(out.get("symbol"), str):
            try:
                out["symbol"] = normalize_symbol(out["symbol"])
            except ValueError:
                pass
        if isinstance(out.get("venue"), str):
            try:
                out["venue"] = normalize_venue(out["venue"])
            except ValueError:
                out["venue"] = str(out["venue"]).strip().lower()
        if isinstance(out.get("substrate"), str):
            out["substrate"] = out["substrate"].strip().lower()
        if isinstance(out.get("analysis"), str):
            out["analysis"] = out["analysis"].strip().lower()
        if isinstance(out.get("plane"), str):
            out["plane"] = out["plane"].strip().lower()
        if isinstance(out.get("name"), str):
            out["name"] = out["name"].strip().lower()
        return out

    def _paired_stream(self, namespace: str) -> str | None:
        """Sibling stream for a latest namespace, or None (latest-only)."""
        if namespace == "derivatives.latest":
            return "derivatives.stream"
        if namespace == "raw.latest":
            return "raw.stream"
        if namespace == "inference.latest":
            return "inference.stream"
        if namespace == "governance.latest":
            return "governance.stream"
        for prefix, suffix in (("substrate.", ".events"), ("analysis.", ".events")):
            if namespace.startswith(prefix) and "." not in namespace[len(prefix):]:
                return f"{namespace}{suffix}"
        return None

    async def publish(
        self, namespace: str, payload: Any, *, ids: dict[str, Any],
        ttl_s: int | None = None, maxlen: int | None = None,
        extra_fields: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> str | None:
        """SINGULAR WRITE PATH: normalise -> contract -> pare -> store.

        The ONLY publisher with Redis logic. ``namespace`` selects behaviour
        from configuration (``Namespace.kind`` + ``_paired_stream`` + the
        namespace's ``Retention``); ``ids`` selects the key. No caller names
        a Redis key and no second publish path exists.

        - latest (+paired stream) -> atomic SET+XADD (derivatives, raw,
          substrate/analysis bare, inference, governance)
        - latest-only -> SET (+EX from config); stream-only -> XADD (pared)
        - ``raw.latest`` additionally dedupes on ``observed_at_ms`` and
          MINID-trims to the configured retention horizon
        - ``governance.latest`` additionally fans out denials + refreshes
          the supervisor heartbeat
        - ``agent.artifacts`` (kind=text used as a stream) appends the
          advisory string with its schema/session/artifact fields
        - ``CanonicalOutput`` objects are accepted for ``inference.latest``
          (``to_dict``) so the engine passes the row, not a dict copy
        Returns the stream entry id, the key written, or ``None`` for a
        raw dedupe-hit (idempotent re-delivery).
        """
        spec = self.ns(namespace)
        ids = self._normalize_ids(ids)
        if namespace == "poller.active":
            if not isinstance(payload, (list, tuple)):
                raise StoreContractError(
                    f"{namespace}: payload must be a non-empty sequence")
            cleaned = sorted({s.strip().upper() for s in payload if isinstance(s, str) and s.strip()})
            if not cleaned:
                raise ValueError("poller.active: at least one symbol required")
            payload = cleaned
        # CanonicalOutput convenience: the engine hands the row itself.
        if hasattr(payload, "to_dict") and callable(getattr(payload, "to_dict")):
            try:
                payload = payload.to_dict()  # type: ignore[union-attr]
            except Exception:
                pass
        if namespace == "agent.artifacts":
            if not isinstance(payload, str) or not payload:
                raise StoreContractError(
                    f"{namespace}: payload must be a non-empty string")
            payload = self._contract(namespace, payload)
            artifact = str(ids.get("artifact", ""))
            session_id = str(ids.get("session", ""))
            if artifact not in {"observations", "hypotheses", "memory"}:
                raise ValueError(
                    f"unsupported agent artifact type: {artifact}")
            schema_version = _AGENT_ARTIFACT_SCHEMA_VERSION.get(artifact, 1)
            fields: dict[str, Any] = {
                "schema_version": str(schema_version),
                "session_id": session_id,
                "artifact_type": artifact,
                "payload": payload,
            }
            if run_id is not None:
                fields["run_id"] = run_id
            if extra_fields:
                fields.update({k: str(v) for k, v in extra_fields.items()})
            return str(await self.redis.xadd(
                self.key(namespace, **ids), fields,
                maxlen=max(1, int(maxlen or self.stream_maxlen)),
                approximate=True,
            ))
        if namespace == "raw.latest":
            return await self._publish_raw(ids.get("symbol", ""), payload)  # type: ignore[arg-type]
        if namespace == "governance.latest":
            return await self._publish_governance(payload, ids)
        paired = self._paired_stream(namespace)
        if paired is not None and spec.kind == "latest":
            # CanonicalOutput validation lives on the object; dicts validate
            # through the contract inside publish_latest_and_stream.
            if hasattr(payload, "validate") and callable(getattr(payload, "validate")):
                try:
                    payload.validate()  # type: ignore[union-attr]
                except Exception:
                    pass
            return await self.publish_latest_and_stream(
                namespace, paired, payload,
                ids_latest=ids, maxlen=maxlen, ttl_s=ttl_s,
                extra_fields=extra_fields,
            )
        if spec.kind == "latest":
            await self.publish_latest(
                namespace, payload, ids=ids, ttl_s=ttl_s,
            )
            return self.key(namespace, **ids)
        if spec.kind == "stream":
            return await self.publish_stream(
                namespace, payload, ids=ids, maxlen=maxlen,
                extra_fields=extra_fields,
            )
        # sequence / marker / text (latest-shaped): single SET path.
        await self.publish_latest(namespace, payload, ids=ids, ttl_s=ttl_s)
        return self.key(namespace, **ids)

    async def _publish_raw(self, symbol: Any, payload: Any) -> str | None:
        """Raw evidence write: dedupe guard + SET latest + slim XADD + trim."""
        symbol = normalize_symbol(symbol)
        ns_raw = self.ns("raw.stream")
        payload = normalize_payload("raw.latest", payload)
        payload = self._contract("raw.latest", payload)
        self._contract("raw.stream", payload)
        full_body = self._encode(payload)
        stream_body = self._encode(pare_stream_body("raw.stream", dict(payload)))
        ts = str(payload.get("observed_at_ms", ""))  # type: ignore[union-attr]
        maxlen = str(int(ns_raw.retention.guardrail_maxlen or self.raw_guardrail_maxlen))
        minid = str(int(time.time() * 1000) - int(
            ns_raw.retention.retention_ms or self.raw_retention_ms))
        script = """
        if ARGV[2] ~= '' then
          local guard = redis.call('SET', KEYS[3], '1', 'NX', 'EX', ARGV[3])
          if guard == false then return 'duplicate' end
        end
        redis.call('SET', KEYS[1], ARGV[1])
        local id = redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[5], '*',
            'ts', ARGV[2], 'payload', ARGV[4])
        redis.call('XTRIM', KEYS[2], 'MINID', '~', ARGV[6])
        return id
        """
        result = await self.redis.eval(
            script, 3,
            self.key("raw.latest", symbol=symbol),
            self.key("raw.stream", symbol=symbol),
            self.key("raw.dedupe", symbol=symbol, observed_at_ms=ts),
            full_body, ts, "600", stream_body, maxlen, minid,
        )
        if result == "duplicate":
            return None
        return str(result)

    async def _publish_governance(self, payload: Any, ids: dict[str, Any]) -> str:
        """Governance write: latest + stream + denials fan-out + supervisor."""
        payload = normalize_payload("governance.latest", payload)
        payload = self._contract("governance.latest", payload)
        body = self._encode(payload)
        plane = str(ids.get("plane", "")).lower()
        substrate = str(ids.get("substrate", ids.get("name", ""))).lower()
        symbol = str(ids.get("symbol", "")).upper()
        ts = str(payload.get("decided_at_ms", "") or "")  # type: ignore[union-attr]
        allowed = "1" if payload.get("allowed") else "0"  # type: ignore[union-attr]
        maxlen = str(int(self.stream_maxlen))
        ttl_ms = str(int(os.getenv("GOVERNANCE_SUPERVISOR_MS") or 900_000))
        script = """
        redis.call('SET', KEYS[1], ARGV[1])
        local id = redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[5], '*',
            'plane', ARGV[2], 'substrate', ARGV[3], 'symbol', ARGV[4],
            'ts', ARGV[6], 'allowed', ARGV[7], 'payload', ARGV[1])
        if ARGV[7] == '0' then
            redis.call('XADD', KEYS[3], 'MAXLEN', '~', ARGV[5], '*',
                'plane', ARGV[2], 'substrate', ARGV[3], 'symbol', ARGV[4],
                'ts', ARGV[6], 'allowed', ARGV[7], 'payload', ARGV[1])
        end
        redis.call('SET', KEYS[4], ARGV[6], 'PX', ARGV[8])
        return id
        """
        return str(await self.redis.eval(
            script, 4,
            self.key("governance.latest", plane=plane, name=substrate, symbol=symbol),
            self.key("governance.stream", plane=plane, symbol=symbol),
            self.key("governance.denials", plane=plane, symbol=symbol),
            self.key("governance.supervisor", plane=plane, symbol=symbol),
            body, plane, substrate, symbol, maxlen, ts, allowed, ttl_ms,
        ))

    async def read(
        self, namespace: str, *, ids: dict[str, Any], count: int | None = None,
        newest_first: bool = True, start: str = "-", end: str = "+",
    ) -> Any:
        """SINGULAR READ PATH: resolve key -> GET/XRANGE -> decode verbatim.

        The ONLY reader with Redis logic. Returns the stored document in its
        rawest pared form (dict / str / list) or ``None`` when absent — never
        shaped, never fabricated. Stream namespaces return newest-first
        payload lists; ``agent.artifacts`` returns ``AgentMemory`` rows
        (deduped, tombstones dropped) to preserve the forget contract.
        """
        spec = self.ns(namespace)
        ids = self._normalize_ids(ids)
        if namespace == "agent.artifacts":
            return await self._read_agent_memories(ids, count=count)
        if namespace == "governance.latest" and "name" not in ids and "substrate" not in ids:
            rows = await self.read_stream(
                "governance.stream",
                ids={"plane": ids.get("plane", ""), "symbol": ids.get("symbol", "")},
                count=1 if count is None else count,
            )
            return rows[0] if rows else None
        if spec.kind == "latest":
            return await self.read_latest(namespace, ids=ids)
        if spec.kind == "stream":
            return await self.read_stream(
                namespace, ids=ids, count=count,
                newest_first=newest_first, start=start, end=end,
            )
        # sequence / text / marker: single-key verbatim read.
        return await self.read_latest(namespace, ids=ids)

    async def _read_agent_memories(
        self, ids: dict[str, Any], count: int | None = 32,
    ) -> list[AgentMemory]:
        """Agent memory stream: newest-first, deduped, tombstones dropped."""
        key = self.key("agent.artifacts", **ids)
        rows = await self.redis.xrevrange(key, count=count or 32)
        out: list[AgentMemory] = []
        for _entry_id, fields in rows:
            raw = (fields or {}).get("payload")
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if not isinstance(raw, str):
                continue
            try:
                memory = AgentMemory.from_mapping(json.loads(raw))
            except (ValueError, json.JSONDecodeError):
                continue
            out.append(memory)
        newest: dict[str, AgentMemory] = {}
        for m in out:
            newest.setdefault(m.memory_id, m)
        return [m for m in newest.values() if not m.forgotten]

    async def purge_retired(self, *, dry_run: bool = True) -> dict[str, Any]:
        """Drop leftovers of RETIRED key families (old planes, no writer).

        ``dry_run`` (the default) reports what WOULD be removed. Nothing in the
        live namespace set is matched: the patterns name only the collated,
        domain-state, per-run and horizon families, all of which the runtime no
        longer reads or writes.
        """
        matched: dict[str, list[str]] = {}
        for template in RETIRED_KEY_PATTERNS:
            pattern = template.format(prefix=self.prefix)
            keys = [key async for key in self.redis.scan_iter(
                match=pattern, count=500)]
            if keys:
                matched[pattern] = sorted(keys)
        deleted = 0
        if not dry_run:
            for keys in matched.values():
                for start in range(0, len(keys), 200):
                    chunk = keys[start:start + 200]
                    deleted += int(await self.redis.unlink(*chunk) or 0)
        return {"dry_run": dry_run, "matched": matched,
                "matched_count": sum(len(v) for v in matched.values()),
                "deleted": deleted}

    def agent_stream(self, session_id: str, artifact_type: str) -> str:
        return self.key("agent.artifacts", session=session_id,
                        artifact=artifact_type)

    async def close(self) -> None:
        await self.redis.aclose()

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def ping_with_retry(self, attempts: int = 12, delay_s: float = 1.0) -> bool:
        """Tolerate transient Docker DNS/network readiness during startup."""
        for attempt in range(attempts):
            try:
                if await self.ping():
                    return True
            except Exception:
                if attempt == attempts - 1:
                    raise
            await asyncio.sleep(delay_s)
        return False

    def derivatives_key(self, symbol: str) -> str:
        """Latest projection for command-fetched derivative evidence."""
        return self.key("derivatives.latest", symbol=symbol.upper())

    def derivatives_stream(self, symbol: str) -> str:
        """Append-only audit trail of every derivative evidence fetch."""
        return self.key("derivatives.stream", symbol=symbol.upper())

    async def derivative_cache_ttl(self, symbol: str) -> int:
        """Diagnostic: remaining TTL on the derivative evidence key."""
        return int(await self.redis.ttl(self.derivatives_key(symbol)))

    # ------------------------------------------------------------------
    # Raw evidence stream — poller writes, harness reads
    # ------------------------------------------------------------------

    def raw_stream(self, symbol: str) -> str:
        return self.key("raw.stream", symbol=symbol.upper())

    def raw_latest_key(self, symbol: str) -> str:
        return self.key("raw.latest", symbol=symbol.upper())

    def raw_dedupe_key(self, symbol: str, observed_at_ms: str) -> str:
        """Idempotency guard key for one raw evidence snapshot."""
        return self.key("raw.dedupe", symbol=symbol.upper(),
                        observed_at_ms=observed_at_ms)

    async def redis_maxmemory_bytes(self) -> int | None:
        """Server ``maxmemory`` in bytes (Phase H1 budget guard seam).

        ``None`` when unlimited (0) or the read fails — the guard treats
        both as "cannot check", never as "budget available".
        """
        try:
            cfg = await self.redis.config_get("maxmemory")
            raw = (cfg or {}).get("maxmemory") if isinstance(cfg, dict) else None
            value = int(raw) if raw is not None else 0
            return value if value > 0 else None
        except Exception:
            return None

    async def read_raw_oldest_ms(self, symbol: str) -> int | None:
        """Event-time of the OLDEST entry still in the raw stream.

        Phase H1 coverage seam: the retention horizon a window reader can
        honestly observe ends at this timestamp — anything older has been
        evicted by the MINID trim. ``None`` when the stream is empty.
        """
        rows = await self.redis.xrange(self.raw_stream(symbol), min="-", max="+", count=1)
        if not rows:
            return None
        entry_id = rows[0][0]
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode("utf-8", errors="replace")
        try:
            return int(str(entry_id).split("-", 1)[0])
        except (TypeError, ValueError):
            return None

    def microstructure_raw_stream(self, venue: str, symbol: str) -> str:
        return self.key("micro.raw", venue=venue.lower(),
                        symbol=symbol.upper())

    def microstructure_event_stream(self, venue: str, symbol: str) -> str:
        return self.key("micro.events", venue=venue.lower(),
                        symbol=symbol.upper())

    def microstructure_ofi_stream(self, venue: str, symbol: str) -> str:
        return self.key("micro.ofi", venue=venue.lower(),
                        symbol=symbol.upper())

    def microstructure_book_key(self, venue: str, symbol: str) -> str:
        return self.key("micro.book", venue=venue.lower(),
                        symbol=symbol.upper())

    def microstructure_status_key(self, venue: str, symbol: str) -> str:
        return self.key("micro.status", venue=venue.lower(),
                        symbol=symbol.upper())

    def microstructure_status_stream(self, venue: str, symbol: str) -> str:
        """Status-TRANSITION ledger stream (written only on state change).

        This is the event-driven companion to the latest-key status payload:
        the capture node appends one entry here whenever ``state`` *changes*
        (running -> gap -> reconnecting -> running ...). Event-time is
        carried in the entry id, so age checks need no extra payload fields.
        """
        return self.key("micro.status.events", venue=venue.lower(),
                        symbol=symbol.upper())

    def microstructure_evidence_key(self, venue: str, symbol: str) -> str:
        return self.key("micro.evidence", venue=venue.lower(),
                        symbol=symbol.upper())

    def inference_latest_key(self, symbol: str, venue: str) -> str:
        return self.key("inference.latest", symbol=symbol.upper(),
                        venue=venue)

    def inference_stream(self, symbol: str, venue: str) -> str:
        return self.key("inference.stream", symbol=symbol.upper(),
                        venue=venue)

    def poller_control_key(self) -> str:
        return self.key("poller.active")

    def poller_status_key(self) -> str:
        return self.key("poller.status")

    async def clear_poller_symbols(self) -> None:
        """Delete the override so the poller falls back to env defaults."""
        await self.redis.delete(self.poller_control_key())

    def substrate_stream(self, substrate: str, symbol: str) -> str:
        return self.key(f"substrate.{substrate.lower()}.events",
                        symbol=symbol.upper())

    def substrate_latest_key(self, substrate: str, symbol: str) -> str:
        return self.key(f"substrate.{substrate.lower()}",
                        symbol=symbol.upper())

    def substrate_supervisor_key(self, substrate: str, symbol: str) -> str:
        return self.key(f"substrate.{substrate.lower()}.supervisor",
                        symbol=symbol.upper())

    # ------------------------------------------------------------------
    # Horizon spans — per-horizon deterministic blocks (spec: interaction
    # plane horizon-vector shape). Sibling projections of the collated
    # ledger, NOT replacements: the audit unit stays run_id, each span
    # carries its own run back-pointer. Latest-only streams are bounded;
    # collated-stream-style unbounded replay is deliberately NOT kept per
    # horizon (7 unbounded streams would 7x the AOF cost).
    # ------------------------------------------------------------------

    async def read_substrate_history_count(self, substrate: str, symbol: str) -> int:
        """Return XLEN of the substrate state stream for diagnostics."""
        return int(await self.redis.xlen(self.substrate_stream(substrate, symbol)))

    # ------------------------------------------------------------------
    # Analysis worker plane (orthogonal to the substrate plane)
    # ------------------------------------------------------------------

    # Same atomic publish discipline as the substrate plane, its own
    # keyspace: analysis workers consume substrate state streams and
    # publish THEIR projections here. Worker identity is (analysis,
    # symbol) — never the process. Spec: docs/ANALYSIS_WORKER_SPEC.md.

    def analysis_stream(self, analysis: str, symbol: str) -> str:
        return self.key(f"analysis.{analysis.lower()}.events",
                        symbol=symbol.upper())

    def analysis_latest_key(self, analysis: str, symbol: str) -> str:
        return self.key(f"analysis.{analysis.lower()}",
                        symbol=symbol.upper())

    def analysis_supervisor_key(self, analysis: str, symbol: str) -> str:
        return self.key(f"analysis.{analysis.lower()}.supervisor",
                        symbol=symbol.upper())

    def governance_latest_key(self, plane: str, substrate: str, symbol: str) -> str:
        return self.key("governance.latest", plane=plane.lower(),
                        name=substrate.lower(), symbol=symbol.upper())

    def governance_stream(self, plane: str, symbol: str) -> str:
        return self.key("governance.stream", plane=plane.lower(),
                        symbol=symbol.upper())

    def governance_denial_stream(self, plane: str, symbol: str) -> str:
        return self.key("governance.denials", plane=plane.lower(),
                        symbol=symbol.upper())

    def governance_supervisor_key(self, plane: str, symbol: str) -> str:
        return self.key("governance.supervisor", plane=plane.lower(),
                        symbol=symbol.upper())

    async def governance_alive(self, plane: str, symbol: str) -> bool:
        """Liveness: has the plane's FSM decided recently? (decision-driven)"""
        try:
            return bool(await self.redis.get(
                self.governance_supervisor_key(plane, symbol)))
        except Exception:
            return False

