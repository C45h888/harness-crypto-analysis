"""Typed PostgreSQL adapter for durable market snapshots and signals."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg

from .contracts import (
    AgentMemory,
    CanonicalOutput,
    _json_safe,
)


log = logging.getLogger(__name__)


class PostgresRuntimeStore:
    """Connection-pool boundary for the durable ledger.

    ``DATABASE_URL`` is only required here — at the Postgres boundary — and
    nowhere else. Redis-only surfaces (``Settings.from_redis_env``) never
    construct this store, so Redis ops run without Postgres config.
    """

    def __init__(self, database_url: str | None):
        if not database_url:
            raise ValueError(
                "DATABASE_URL is required to construct a PostgresRuntimeStore. "
                "Postgres is only consulted when persistence is explicitly "
                "required; Redis-only surfaces should run Redis alone."
            )
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def ping(self) -> bool:
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        return await self.pool.fetchval("SELECT 1") == 1

    # --- Durable archive reads (dict payload, 2026-08-31) -----------------
    # The envelope dataclass readers were retired with the dataclass. These
    # reconstruct the SAME payload shape ``assemble_envelope`` publishes
    # (runtime.read_paths guards the schema on the consumer side). The
    # legacy `envelope` jsonb column is honoured for pre-dedupe rows.

    _RUN_COLUMNS = (
        "run_id, symbol, generated_at, completed_at, status, data_source, "
        "schema_version, coverage, canonical_state, domain_outputs, errors, "
        "source_metadata, envelope"
    )

    async def insert_agent_memory(self, memory: AgentMemory) -> bool:
        """Durable, idempotent write of one ``AgentMemory``.

        Postgres is the durable ledger for agent memory; Redis is only the
        live projection. The row is schema-versioned and immutable-by-id:
        re-writing the same ``memory_id`` updates fields (used by
        ``forget`` to tombstone) instead of duplicating. ``forgotten``
        rows survive for audit but are excluded from recall.
        """
        memory.validate()
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        try:
            created_at = datetime.fromisoformat(
                memory.created_at.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            created_at = datetime.now()
        updated_at_raw = memory.updated_at
        updated_at: datetime | None = None
        if updated_at_raw:
            try:
                updated_at = datetime.fromisoformat(
                    updated_at_raw.replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                updated_at = None
        row = await self.pool.fetchrow(
            """INSERT INTO agent_memory
               (memory_id, session_id, run_id, kind, title, content,
                importance, tags, evidence_refs, created_at, updated_at,
                forgotten, schema_version, payload)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
               ON CONFLICT (memory_id) DO UPDATE
               SET run_id = EXCLUDED.run_id,
                   kind = EXCLUDED.kind,
                   title = EXCLUDED.title,
                   content = EXCLUDED.content,
                   importance = EXCLUDED.importance,
                   tags = EXCLUDED.tags,
                   evidence_refs = EXCLUDED.evidence_refs,
                   updated_at = EXCLUDED.updated_at,
                   forgotten = EXCLUDED.forgotten,
                   payload = EXCLUDED.payload
               RETURNING memory_id""",
            uuid.UUID(memory.memory_id),
            uuid.UUID(memory.session_id),
            uuid.UUID(memory.run_id) if memory.run_id else None,
            memory.kind,
            memory.title,
            memory.content,
            memory.importance,
            json.dumps(list(memory.tags), default=str),
            json.dumps(list(memory.evidence_refs), default=str),
            created_at,
            updated_at,
            memory.forgotten,
            memory.schema_version,
            memory.to_json(),
        )
        return row is not None

    async def read_recent_memories(
        self, session_id: str, kind: str | None = None, limit: int = 32,
    ) -> list[AgentMemory]:
        """Most recent non-forgotten memories for one session.

        Durable authority is Postgres; Redis is the fallen-back live
        projection. Optional ``kind`` filter mirrors the recall surface
        of the memory node.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        sql = (
            "SELECT payload FROM agent_memory "
            "WHERE session_id = $1 AND NOT forgotten "
            "ORDER BY created_at DESC LIMIT $2"
        )
        params: list[Any] = [uuid.UUID(session_id), limit]
        if kind is not None:
            sql = (
                "SELECT payload FROM agent_memory "
                "WHERE session_id = $1 AND kind = $2::text AND NOT forgotten "
                "ORDER BY created_at DESC LIMIT $3"
            )
            params = [uuid.UUID(session_id), kind, limit]
        rows = await self.pool.fetch(sql, *params)
        out: list[AgentMemory] = []
        for row in rows:
            raw = row["payload"]
            if raw is None:
                continue
            try:
                out.append(AgentMemory.from_mapping(
                    json.loads(raw) if isinstance(raw, str) else raw
                ))
            except (ValueError, json.JSONDecodeError):
                continue
        return out

    async def read_memory(self, memory_id: str) -> AgentMemory | None:
        """Read one durable memory by id (including forgotten rows)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        raw = await self.pool.fetchval(
            "SELECT payload FROM agent_memory WHERE memory_id = $1",
            uuid.UUID(memory_id),
        )
        if raw is None:
            return None
        try:
            return AgentMemory.from_mapping(
                json.loads(raw) if isinstance(raw, str) else raw
            )
        except (ValueError, json.JSONDecodeError):
            return None

    async def forget_memory(self, memory_id: str) -> bool:
        """Tombstone one agent memory (audit row survives)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """UPDATE agent_memory
               SET forgotten = TRUE, updated_at = now()
               WHERE memory_id = $1
               RETURNING memory_id""",
            uuid.UUID(memory_id),
        )
        return row is not None

    async def count_memories(self, session_id: str) -> dict[str, int]:
        """Per-kind counts for one session's durable memories."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT kind, COUNT(*) AS n FROM agent_memory "
            "WHERE session_id = $1 AND NOT forgotten "
            "GROUP BY kind ORDER BY kind",
            uuid.UUID(session_id),
        )
        return {str(row["kind"]): int(row["n"]) for row in rows}

    # ------------------------------------------------------------------
    # Keystone history — cross-cycle keystone-migration ledger
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Pass-3 microstructure evidence ledger (postgres-first durable layer).
    # Redis latest-evidence keys are projections of these rows only.
    # ------------------------------------------------------------------

    async def insert_microstructure_evidence(self, evidence: dict[str, Any]) -> bool:
        """Persist one immutable MicrostructureEvidence row (idempotent on id)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """INSERT INTO microstructure_evidence
               (symbol, venue, evidence_id, generated_at_ms, schema_version,
                interval_seconds, window_start_ms, window_end_ms, tick_size,
                depth_estimator, input_hash, model_version,
                price_impact_fit, sensitivity_fit, depth_scaling_fit,
                block_average_depth, coverage, status, evidence)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
               ON CONFLICT (symbol, venue, evidence_id) DO NOTHING
               RETURNING evidence_id""",
            str(evidence["symbol"]).upper(),
            str(evidence["venue"]),
            str(evidence["evidence_id"]),
            int(evidence["generated_at_ms"]),
            int(evidence.get("schema_version") or 1),
            int(evidence["interval_seconds"]),
            int(evidence["window_start_ms"]),
            int(evidence["window_end_ms"]),
            evidence["tick_size"],
            str(evidence["depth_estimator"]),
            str(evidence["input_hash"]),
            str(evidence["model_version"]),
            json.dumps(evidence.get("price_impact_fit")),
            json.dumps(evidence.get("sensitivity_fit")),
            json.dumps(evidence.get("depth_scaling_fit")),
            evidence.get("block_average_depth"),
            json.dumps(evidence.get("coverage") or {}),
            str(evidence["status"]),
            json.dumps(evidence),
        )
        return row is not None

    async def read_microstructure_evidence(
        self, symbol: str, venue: str, evidence_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Durable read: latest evidence for (symbol, venue) or one exact id."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        if evidence_id:
            row = await self.pool.fetchrow(
                """SELECT evidence FROM microstructure_evidence
                   WHERE symbol = $1 AND venue = $2 AND evidence_id = $3""",
                symbol.upper(), venue, evidence_id,
            )
        else:
            row = await self.pool.fetchrow(
                """SELECT evidence FROM microstructure_evidence
                   WHERE symbol = $1 AND venue = $2
                   ORDER BY window_end_ms DESC LIMIT 1""",
                symbol.upper(), venue,
            )
        if row is None:
            return None
        evidence = row["evidence"]
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except (ValueError, json.JSONDecodeError):
                return None
        return evidence if isinstance(evidence, dict) else None

    async def list_microstructure_evidence(
        self, symbol: str, venue: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Durable read: the most recent evidence rows for (symbol, venue)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            """SELECT evidence FROM microstructure_evidence
               WHERE symbol = $1 AND venue = $2
               ORDER BY window_end_ms DESC LIMIT $3""",
            symbol.upper(), venue, limit,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            evidence = row["evidence"]
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except (ValueError, json.JSONDecodeError):
                    continue
            if isinstance(evidence, dict):
                out.append(evidence)
        return out

    # ------------------------------------------------------------------
    # Durable microstructure EVENT TAPE (postgres-first, append-only).
    # Capture appends every published best-quote transition here (best-effort)
    # so tape reads are no longer bounded by the volatile Redis stream maxlen.
    # The payload is the SAME serialized OrderBookEvent dict the live stream
    # carries — one semantic, two media.
    # ------------------------------------------------------------------

    async def ensure_microstructure_tape(self) -> None:
        """Idempotent tape table creation (capture bootstraps its own seam)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS microstructure_event_tape (
                id BIGSERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                venue TEXT NOT NULL,
                update_id BIGINT NOT NULL,
                exchange_ts_ms BIGINT NOT NULL,
                received_ts_ms BIGINT NOT NULL,
                payload JSONB NOT NULL,
                captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (symbol, venue, update_id, exchange_ts_ms)
            );
            """
        )
        await self.pool.execute(
            """
            CREATE INDEX IF NOT EXISTS microstructure_event_tape_window_idx
                ON microstructure_event_tape (symbol, venue, exchange_ts_ms);
            """
        )

    async def insert_microstructure_tape_event(self, event_payload: dict[str, Any]) -> bool:
        """Append one tape event; returns True on a new row (deduped on conflict)."""
        current = event_payload.get("current") or {}
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """INSERT INTO microstructure_event_tape
               (symbol, venue, update_id, exchange_ts_ms, received_ts_ms, payload)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (symbol, venue, update_id, exchange_ts_ms) DO NOTHING
               RETURNING id""",
            str(event_payload["symbol"]).upper() if event_payload.get("symbol")
                else str(current.get("symbol") or "").upper(),
            str(event_payload.get("venue") or current.get("venue") or ""),
            int(current.get("update_id") or 0),
            int(current.get("exchange_ts_ms") or 0),
            int(current.get("received_ts_ms") or 0),
            json.dumps(event_payload, default=str, separators=(",", ":")),
        )
        return row is not None

    async def read_microstructure_tape_events(
        self, symbol: str, venue: str, *,
        start_ts_ms: int | None = None, end_ts_ms: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Durable tape read: payloads ordered (exchange_ts_ms, update_id)."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        query = """SELECT payload FROM microstructure_event_tape
                   WHERE symbol = $1 AND venue = $2
                   AND ($3::BIGINT IS NULL OR exchange_ts_ms >= $3)
                   AND ($4::BIGINT IS NULL OR exchange_ts_ms <= $4)
                   ORDER BY exchange_ts_ms ASC, update_id ASC"""
        if limit is not None:
            query += " LIMIT $5"
        rows = await self.pool.fetch(
            query, symbol.upper(), venue, start_ts_ms, end_ts_ms, limit,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (ValueError, json.JSONDecodeError):
                    continue
            if isinstance(payload, dict):
                out.append(payload)
        return out

    async def latest_microstructure_tape_ts(self, symbol: str, venue: str) -> int | None:
        """Highest durable exchange_ts_ms — the durable/live merge watermark."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        val = await self.pool.fetchval(
            """SELECT MAX(exchange_ts_ms) FROM microstructure_event_tape
               WHERE symbol = $1 AND venue = $2""",
            symbol.upper(), venue,
        )
        return int(val) if val is not None else None

    # ------------------------------------------------------------------
    # Inference-engine artifact ledger (postgres-first durable layer).
    # Redis latest-inference keys are projections of these rows only.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Canonical output ledger (postgres-first durable layer) — THE
    # deliverable plane: one row per cycle = the frozen answer of record
    # (answer_text) + its run record (bookkeeping). The inference_artifact
    # ledger above is deprecated read-only legacy (pre-canonical cycles).
    # ------------------------------------------------------------------

    async def insert_canonical_output(self, output: CanonicalOutput) -> bool:
        """Durable, idempotent write of one ``CanonicalOutput`` row.

        Primary key is ``run_id``: each cycle produces exactly one immutable
        answer of record. A NULL ``answer_text`` means no authored ship
        (degraded or gate-failed cycle) — the durable record of that, never
        a missing value.
        """
        output.validate()
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """INSERT INTO canonical_output
               (run_id, symbol, venue, generated_at, completed_at,
                schema_version, status, terminal, window_minutes,
                interval_seconds, input_hash, model_version, task, session_id,
                answer_text, answer, interpretation, hypothesis,
                hypothesis_verdict, verdict_reason, calculations, conformance,
                run_record)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
               ON CONFLICT (run_id) DO NOTHING
               RETURNING run_id""",
            uuid.UUID(output.run_id),
            output.symbol,
            output.venue,
            datetime.fromisoformat(output.generated_at.replace("Z", "+00:00")),
            datetime.fromisoformat(output.completed_at.replace("Z", "+00:00")),
            output.schema_version,
            output.status,
            output.terminal,
            output.window_minutes,
            output.interval_seconds,
            output.input_hash,
            output.model_version,
            output.task,
            uuid.UUID(output.session_id) if output.session_id else None,
            output.answer_text,
            json.dumps(output.answer, default=str) if output.answer is not None else None,
            json.dumps(output.interpretation, default=str)
            if output.interpretation is not None else None,
            json.dumps(output.hypothesis, default=str)
            if output.hypothesis is not None else None,
            output.hypothesis_verdict,
            output.verdict_reason,
            json.dumps(output.calculations, default=str)
            if output.calculations is not None else None,
            json.dumps(output.conformance, default=str)
            if output.conformance is not None else None,
            json.dumps(output.run_record, default=str),
        )
        return row is not None

    async def update_canonical_output(self, output: CanonicalOutput) -> bool:
        """Refresh an existing canonical row in place (terminal + trace).

        Same immutable-by-id row: the terminal/state written after SETTLE
        closes the metadata gap the settle itself creates. Never blocks
        settlement — callers record failures as run_record_errors.
        """
        output.validate()
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        status = await self.pool.execute(
            """UPDATE canonical_output SET
                completed_at=$2, status=$3, terminal=$4, answer_text=$5,
                answer=$6, interpretation=$7, hypothesis=$8,
                hypothesis_verdict=$9, verdict_reason=$10, calculations=$11,
                conformance=$12, run_record=$13
               WHERE run_id=$1""",
            uuid.UUID(output.run_id),
            datetime.fromisoformat(output.completed_at.replace("Z", "+00:00")),
            output.status,
            output.terminal,
            output.answer_text,
            json.dumps(output.answer, default=str) if output.answer is not None else None,
            json.dumps(output.interpretation, default=str)
            if output.interpretation is not None else None,
            json.dumps(output.hypothesis, default=str)
            if output.hypothesis is not None else None,
            output.hypothesis_verdict,
            output.verdict_reason,
            json.dumps(output.calculations, default=str)
            if output.calculations is not None else None,
            json.dumps(output.conformance, default=str)
            if output.conformance is not None else None,
            json.dumps(output.run_record, default=str),
        )
        return status.endswith("1")

    async def read_canonical_output(
        self, symbol: str | None = None, run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Durable read: one exact ``run_id``, or the latest for a symbol."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        columns = (
            "run_id, symbol, venue, generated_at, completed_at, status, terminal,"
            " window_minutes, interval_seconds, input_hash, model_version, task,"
            " session_id, answer_text, answer, interpretation, hypothesis,"
            " hypothesis_verdict, verdict_reason, calculations, conformance,"
            " run_record, schema_version"
        )
        if run_id:
            row = await self.pool.fetchrow(
                f"SELECT {columns} FROM canonical_output WHERE run_id = $1",
                uuid.UUID(run_id),
            )
        elif symbol:
            row = await self.pool.fetchrow(
                f"SELECT {columns} FROM canonical_output WHERE symbol = $1"
                " ORDER BY generated_at DESC LIMIT 1",
                symbol.upper(),
            )
        else:
            return None
        if row is None:
            return None
        return self._parse_canonical_row(row)

    async def read_recent_canonical_outputs(
        self, symbol: str, *, venue: str | None = None, limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Durable read: recent FULL canonical rows (with run record), newest first.

        Deterministic consumers (prior fits, cross-cycle recall) reuse these;
        empty list on no rows.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        columns = (
            "run_id, symbol, venue, generated_at, completed_at, status, terminal,"
            " window_minutes, interval_seconds, input_hash, model_version, task,"
            " session_id, answer_text, answer, interpretation, hypothesis,"
            " hypothesis_verdict, verdict_reason, calculations, conformance,"
            " run_record, schema_version"
        )
        if venue is not None:
            rows = await self.pool.fetch(
                f"SELECT {columns} FROM canonical_output"
                " WHERE symbol = $1 AND venue = $2"
                " ORDER BY generated_at DESC LIMIT $3",
                symbol.upper(), venue, limit,
            )
        else:
            rows = await self.pool.fetch(
                f"SELECT {columns} FROM canonical_output WHERE symbol = $1"
                " ORDER BY generated_at DESC LIMIT $2",
                symbol.upper(), limit,
            )
        return [self._parse_canonical_row(row) for row in rows]

    async def read_canonical_history(
        self, symbol: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Durable read: canonical-run headlines for one symbol, newest first."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT run_id, symbol, venue, generated_at, status, terminal,"
            " window_minutes, interval_seconds, input_hash, model_version"
            " FROM canonical_output WHERE symbol = $1"
            " ORDER BY generated_at DESC LIMIT $2",
            symbol.upper(), limit,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            if hasattr(entry.get("generated_at"), "isoformat"):
                entry["generated_at"] = entry["generated_at"].isoformat()
            out.append(entry)
        return out

    @staticmethod
    def _parse_canonical_row(row: Any) -> dict[str, Any]:
        """Parse one canonical_output row into a JSON-safe dict (pure)."""
        result = dict(row)
        for col in ("answer", "interpretation", "hypothesis", "calculations",
                    "conformance", "run_record"):
            value = result.get(col)
            if isinstance(value, str):
                try:
                    result[col] = json.loads(value)
                except (ValueError, json.JSONDecodeError):
                    result[col] = {} if col == "run_record" else None
        for col in ("generated_at", "completed_at"):
            if hasattr(result.get(col), "isoformat"):
                result[col] = result[col].isoformat()
        return result

    # ------------------------------------------------------------------
    # Horizon spans — durable fallback for per-horizon blocks.
    # Latest-per-(symbol,horizon); absent rows read as empty, never errors.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Substrate calculation ledger — Phase 3 PG-first write path
    # ------------------------------------------------------------------

    @staticmethod
    def _ms_to_ts(value: Any) -> Any:
        """Coerce epoch-millis to a TZ-aware datetime (None stays None)."""
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
            except (TypeError, ValueError, OverflowError, OSError):
                return None
        return None

    async def record_substrate_state(
        self, symbol: str, substrate: str, payload: dict[str, Any],
    ) -> int:
        """Durable write of one substrate worker fire.

        PG-first: the worker calls this BEFORE publishing the Redis
        projection. Returns the inserted row id. ``payload`` is the
        ``SubstrateStatePayload`` dict (trigger/freshness/missing_inputs
        map 1:1 onto columns; the full dict is retained in ``payload``).
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        # PG's JSONB is strict JSON: sanitize non-finite floats (inf from
        # one-sided books, NaN from empty math) to null before insert.
        trigger = _json_safe(payload.get("trigger") or {})
        freshness = _json_safe(payload.get("freshness") or {})
        missing = _json_safe(payload.get("missing_inputs") or [])
        body = _json_safe(payload)
        row = await self.pool.fetchrow(
            """INSERT INTO substrate_calculation
               (symbol, substrate, status, observed_at, computed_at,
                trigger, freshness, missing_inputs, payload, schema_version)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               RETURNING id""",
            symbol.upper(), substrate.lower(),
            str(payload.get("status") or "healthy"),
            self._ms_to_ts(payload.get("observed_at_ms")),
            self._ms_to_ts(payload.get("computed_at_ms")) or datetime.now(tz=UTC),
            json.dumps(trigger),
            json.dumps(freshness),
            json.dumps(missing),
            json.dumps(body),
            int(payload.get("schema_version") or 1),
        )
        # Curated ledger: when this fire is a signals substrate state, also
        # persist each tripped signal to ``signal_event`` (the schema has
        # always existed but had no writer). Done after the substrate row so
        # the two ledgers stay 1:1; failures here never abort the substrate
        # write (the signal_event ledger is a derived audit, not the source).
        if (substrate or "").lower() == "signals":
            try:
                await self.record_signal_events(
                    symbol, payload,
                    observed_at_ms=payload.get("observed_at_ms"),
                )
            except Exception:
                # Never let a curated-ledger failure roll back the substrate
                # durable write that already succeeded.
                log.warning("signal_event persist failed for %s", symbol, exc_info=True)
        return int(row["id"])

    async def record_signal_events(
        self, symbol: str, payload: dict[str, Any], *, observed_at_ms: Any = None,
    ) -> int:
        """Persist tripped signals to the ``signal_event`` ledger.

        ``payload`` is a ``SubstrateStatePayload`` dict whose ``output``
        holds ``signals`` (list of {signal_type, severity, summary, evidence})
        and ``snapshot`` (dict). Each tripped signal becomes one ``signal_event``
        row. The ``snapshot_id`` FK is left NULL (nullable) — the embedded
        snapshot is a re-derivable dict and we never fabricate a market_snapshot
        id. Dedup is enforced by the table's UNIQUE (symbol, observed_at,
        signal_type). Returns rows written.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        output = payload.get("output") or {}
        signals = output.get("signals") or []
        if not isinstance(signals, list) or not signals:
            return 0
        observed = self._ms_to_ts(observed_at_ms) or datetime.now(tz=UTC)
        written = 0
        for s in signals:
            if not isinstance(s, dict):
                continue
            sig_type = str(s.get("signal_type") or "").strip()
            if not sig_type:
                continue
            severity = int(s.get("severity") or 1)
            severity = max(1, min(5, severity))
            ev = _json_safe(s.get("evidence") or {})
            await self.pool.execute(
                """INSERT INTO signal_event
                   (observed_at, symbol, signal_type, severity, summary, evidence)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT (symbol, observed_at, signal_type) DO NOTHING""",
                observed, symbol.upper(), sig_type, severity,
                str(s.get("summary") or ""), json.dumps(ev),
            )
            written += 1
        return written

    async def read_substrate_history(
        self, symbol: str, substrate: str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Durable read: substrate calculation rows, newest first.

        Rehydrates each row to the stored payload dict with its ledger
        identity (``ledger_id``, ``ledger_created_at``) attached.
        """
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            """SELECT id, symbol, substrate, status, observed_at, computed_at,
                      trigger, freshness, missing_inputs, payload,
                      schema_version, created_at
               FROM substrate_calculation
               WHERE symbol = $1 AND substrate = $2
               ORDER BY computed_at DESC LIMIT $3""",
            symbol.upper(), substrate.lower(), limit,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            result = dict(row)
            for col in ("trigger", "freshness", "missing_inputs", "payload"):
                v = result.get(col)
                if isinstance(v, str):
                    try:
                        result[col] = json.loads(v)
                    except (ValueError, json.JSONDecodeError):
                        result[col] = {} if col != "missing_inputs" else []
            for col in ("observed_at", "computed_at", "created_at"):
                if col in result and hasattr(result[col], "isoformat"):
                    result[col] = result[col].isoformat()
            result["ledger_id"] = result.pop("id")
            result["ledger_created_at"] = result.pop("created_at")
            out.append(result)
        return out

    # ------------------------------------------------------------------
    # Governance ledger (docs/PLANE_GOVERNANCE_SPEC.md)
    #
    # The durable audit of plane legality: EVERY FSM decision lands here,
    # allowed or denied. A denied decision is evidence about the system and
    # must survive the process that produced it. Written only by
    # GovernanceLedger; read by operators / NOOA.
    # ------------------------------------------------------------------

    async def record_governance_decision(
        self, record: dict[str, Any],
    ) -> int:
        """Durable write of one governance decision. Returns the row id."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        decided_at_ms = record.get("decided_at_ms")
        row = await self.pool.fetchrow(
            """INSERT INTO governance_ledger
               (plane, substrate, symbol, gate, allowed, rule, reason,
                findings, observation, decided_at, schema_version)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
               RETURNING id""",
            str(record.get("plane") or ""),
            str(record.get("substrate") or "").lower(),
            str(record.get("symbol") or "").upper(),
            str(record.get("gate") or ""),
            bool(record.get("allowed")),
            str(record.get("rule") or ""),
            str(record.get("reason") or ""),
            json.dumps(_json_safe(list(record.get("findings") or []))),
            json.dumps(_json_safe(record.get("observation") or {})),
            self._ms_to_ts(decided_at_ms) or datetime.now(tz=UTC),
            int(record.get("schema_version") or 1),
        )
        return int(row["id"])

    async def read_governance_ledger(
        self, *, plane: str = "", symbol: str = "",
        substrate: str = "", allowed: bool | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Durable read: governance decisions, newest first, filtered."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        clauses: list[str] = []
        args: list[Any] = []
        if plane:
            args.append(plane.lower())
            clauses.append(f"plane = ${len(args)}")
        if symbol:
            args.append(symbol.upper())
            clauses.append(f"symbol = ${len(args)}")
        if substrate:
            args.append(substrate.lower())
            clauses.append(f"substrate = ${len(args)}")
        if allowed is not None:
            args.append(bool(allowed))
            clauses.append(f"allowed = ${len(args)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(int(limit))
        rows = await self.pool.fetch(
            f"""SELECT id, plane, substrate, symbol, gate, allowed, rule,
                       reason, findings, observation, decided_at,
                       schema_version
                FROM governance_ledger{where}
                ORDER BY decided_at DESC LIMIT ${len(args)}""",
            *args,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            result = dict(row)
            for col in ("findings", "observation"):
                v = result.get(col)
                if isinstance(v, str):
                    try:
                        result[col] = json.loads(v)
                    except (ValueError, json.JSONDecodeError):
                        result[col] = [] if col == "findings" else {}
            if hasattr(result.get("decided_at"), "isoformat"):
                result["decided_at"] = result["decided_at"].isoformat()
            result["ledger_id"] = result.pop("id")
            out.append(result)
        return out

    # ------------------------------------------------------------------
    # Out-of-band telemetry ledger. This table is separate from canonical
    # market, worker, governance, and inference state; Redis is its projection.
    # ------------------------------------------------------------------

    async def record_telemetry_event(self, event: dict[str, Any]) -> bool:
        """Append one idempotent, state-only telemetry event."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """INSERT INTO runtime_telemetry_event
               (event_id, schema_version, plane, owner, subject_type,
                subject_id, event_type, source_sequence, occurred_at_ms,
                source_ref, correlation_id, parent_event_id, state)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (event_id) DO NOTHING
               RETURNING event_id""",
            str(event["event_id"]), int(event["schema_version"]),
            str(event["plane"]), str(event["owner"]),
            str(event["subject_type"]), str(event["subject_id"]),
            str(event["event_type"]), int(event["source_sequence"]),
            int(event["occurred_at_ms"]), event.get("source_ref"),
            event.get("correlation_id"), event.get("parent_event_id"),
            json.dumps(_json_safe(event.get("state") or {}), separators=(",", ":")),
        )
        return row is not None

    async def read_telemetry_events(
        self, *, plane: str, owner: str, subject_type: str,
        subject_id: str, limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Read one owner's durable event history in deterministic order."""
        if self.pool is None:
            await self.connect()
        assert self.pool is not None
        rows = await self.pool.fetch(
            """SELECT event_id, schema_version, plane, owner, subject_type,
                      subject_id, event_type, source_sequence, occurred_at_ms,
                      source_ref, correlation_id, parent_event_id, state
               FROM runtime_telemetry_event
               WHERE plane = $1 AND owner = $2 AND subject_type = $3
                 AND subject_id = $4
               ORDER BY occurred_at_ms, source_sequence, event_id
               LIMIT $5""",
            plane.lower(), owner.lower(), subject_type.lower(),
            subject_id, max(1, int(limit)),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            result = dict(row)
            state = result.get("state")
            if isinstance(state, str):
                try:
                    result["state"] = json.loads(state)
                except (ValueError, json.JSONDecodeError):
                    result["state"] = {}
            out.append(result)
        return out
