"""InferenceEngine — the statistical inference agent (Pass B3).

One cycle of the engine, from wake to durable artifact:

    receive in-memory WakeEnvelope (event-driven worker → run_cycle)
    → gather (capabilities) → recall memory (MemoryNode)
    → HARD GATE (resolve_inference_status, deterministic, pre-LLM)
        ├─ insufficient → artifact w/ NULL interpretation, 0 LLM tokens,
        │                 quality observation remembered
        └─ else → NARRATE#1 (deterministic_state + memory + tool manifest)
                  → execute tool_calls (ONE round max)
                  → NARRATE#2 (final; tool results included)
    → assemble InferenceArtifact → Postgres-first → Redis projection
    → resolve memory proposals (LLM proposes, MemoryNode disposes)

The wake arrives as an IN-MEMORY ``WakeEnvelope`` handed over by the
wake worker (``wake_worker.WakeSupervisor``) — never by draining a
stream. The retired ``publish_wake``/``read_pending_wakes`` transport is
gone; ``run_cycle`` takes the assertion object directly. The only manual
wake factory kept is ``acquire_manual_wake`` (outer-CLI force trigger).

The narration LLM call budget is TWO per cycle (narrate + one tool round).
Gate-failed cycles spend ZERO tokens. The LLM client is INJECTED (built by
the caller via ``backends.build_llm``) so module import stays nooa-free and
contract tests never pay the litellm cost.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from market_service.nooa_harness.inference import (
    CounterSnapshot,
    WakeConfig,
    execute_tool,
    resolve_inference_status,
)
from market_service.runtime.contracts import InferenceArtifact, WakeEnvelope
from market_service.runtime.postgres_store import PostgresRuntimeStore
from market_service.runtime.redis_store import RedisRuntimeStore

log = logging.getLogger(__name__)

SESSION_TEMPLATE = "inference-engine-{symbol}-{venue}"

# Bounded memory recall per cycle (spec §3).
MEMORY_RECALL_LIMIT = 8
MEMORY_CONTEXT_BUDGET = 3_000
MAX_MEMORY_PROPOSALS = 3
# Hard output bound for the prior-artifact headline block.
_PRIOR_HEADLINE_MAX_CHARS = 2_000

_TOOL_MANIFEST = Path(__file__).resolve().parents[2] / (
    "docs/nooa-kb/behavior/tool-manifest.md"
)
_MEMORY_PROTOCOL = Path(__file__).resolve().parents[2] / (
    "docs/nooa-kb/behavior/memory-protocol.md"
)

_SYSTEM_PROMPT_TEMPLATE = """You are the statistical inference engine for {symbol} ({venue}).

You receive ONE deterministic_state object: microstructure fits, coverage,
and gate reasons computed by deterministic Python from the Redis ledgers.
You may also receive recalled memory (your own prior conclusions) and a
deterministic diff vs the prior artifact.

YOUR JOB: interpret the fitted models — sign, magnitude, r2, stderr, and
status of price_impact_fit; the depth-scaling relation (c, lambda) with its
own status; what changed vs the prior cycle. You are the primary
interpretation plane: be specific, numeric, and grounded.

ABSOLUTE RULES:
1. NEVER recompute any value in your reasoning. If you need data you do not
   have, COMMAND a tool (see manifest) — results arrive in a follow-up turn.
2. The two fitted models (price impact beta; depth scaling c/lambda) are
   NEVER merged into one prediction. The combined expression carries a
   heteroskedastic nu*OFI term and is a derived diagnostic at most.
3. Respect deterministic status: an 'insufficient' fit must not be
   interpreted; 'provisional' must be caveated. You cannot override status.
4. Null means not-provided — never substitute zero.
5. Cite exact deterministic_state paths for every numeric claim.

MEMORY: recalled entries are provenance-tagged priors, subordinate to fresh
ledger data. If you contradict a prior conclusion, say so explicitly.
{memory_protocol_section}
TOOL MANIFEST (commandable, deterministic):
{tool_manifest}
"""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _load_kb(path: Path) -> str:
    """Bounded KB doc read; empty string on any failure (never raises)."""
    try:
        return path.read_text(encoding="utf-8")[:12_000]
    except OSError:
        return ""


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Extract the first JSON object from LLM text (fenced or embedded)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # fenced block first
    if "```" in raw:
        for chunk in raw.split("```"):
            candidate = chunk.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue
    # first brace-balanced object
    start = raw.find("{")
    while start != -1:
        depth = 0
        for idx in range(start, len(raw)):
            if raw[idx] == "{":
                depth += 1
            elif raw[idx] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start:idx + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
                    break
        start = raw.find("{", start + 1)
    return None


class NarrationParseError(ValueError):
    """Narration output was not parseable into the required JSON contract."""


class InferenceEngine:
    """The statistical inference agent: wake → compute → narrate → persist.

    Composition: owns a RedisRuntimeStore (data plane), a PostgresRuntimeStore
    (durable ledger), a MemoryNode (episodic memory), and one injected LLM
    client (narration only). All deterministic computation happens through
    the capability registry / tool base — never inside the LLM.
    """

    def __init__(
        self,
        store: RedisRuntimeStore,
        postgres: PostgresRuntimeStore,
        memory: Any | None,
        llm: Any | None,
        *,
        symbol: str = "BTCUSDT",
        venue: str = "spot",
        config: WakeConfig | None = None,
        session_id: str | None = None,
    ) -> None:
        self.store = store
        self.postgres = postgres
        self.memory = memory
        self.llm = llm
        self.symbol = symbol.upper()
        self.venue = venue
        self.config = config or WakeConfig()
        self.session_id = session_id or SESSION_TEMPLATE.format(
            symbol=self.symbol.lower(), venue=self.venue,
        )

    # ------------------------------------------------------------------
    # Wake plane adapters (Redis counter collection + two-phase gate)
    # ------------------------------------------------------------------

    async def collect_snapshot(self) -> CounterSnapshot:
        """Live counters + last-artifact high-water marks (pure reads)."""
        status = await self.store.read_microstructure_status(self.venue, self.symbol)
        stream_len = int(
            await self.store.redis.xlen(
                self.store.microstructure_event_stream(self.venue, self.symbol),
            ) or 0
        )
        prior = None
        if self.postgres is not None:
            try:
                prior = await self.postgres.read_inference_artifact(self.symbol)
            except Exception:
                log.exception("collect_snapshot: prior-artifact read failed")
        prior_state = self._prior_headline(prior)
        return CounterSnapshot(
            event_stream_len=stream_len,
            capture_state=(status or {}).get("state"),
            last_artifact_events_total=prior_state.get("events_total"),
            last_artifact_capture_state=prior_state.get("capture_state"),
            last_artifact_completed_at_ms=prior_state.get("completed_at_ms"),
        )

    async def acquire_manual_wake(self) -> tuple[WakeEnvelope, dict[str, Any]]:
        """Synthesize a MANUAL wake (the outer-CLI ``--force`` trigger).

        The human trigger IS the wake — no stream drain, no predicate
        evaluation, no two-phase revalidation. Event-driven wakes arrive as
        in-memory envelopes directly from the worker and never pass through
        here.
        """
        snapshot = await self.collect_snapshot()
        envelope = WakeEnvelope.create(
            symbol=self.symbol, venue=self.venue, trigger_source="manual",
            predicates_fired={"manual": {}},
            counter_snapshot={
                "event_stream_len": snapshot.event_stream_len,
                "capture_state": snapshot.capture_state,
                "last_artifact_events_total": snapshot.last_artifact_events_total,
            },
            high_water={
                "events_total": snapshot.last_artifact_events_total,
                "completed_at_ms": snapshot.last_artifact_completed_at_ms,
            },
        )
        return envelope, {"decision": "fire", "forced": True,
                          "source": "manual",
                          "consumed_wake_ids": [envelope.wake_id]}

    @staticmethod
    def _prior_headline(prior: dict[str, Any] | None) -> dict[str, Any]:
        """Extract the bounded headline of a prior artifact (provenance + last numbers)."""
        if not prior:
            return {}
        deterministic = prior.get("deterministic_state") or {}
        coverage = deterministic.get("coverage") or {}
        pif = deterministic.get("price_impact_fit") or {}
        completed_at = prior.get("completed_at")
        try:
            completed_ms = int(
                datetime.fromisoformat(str(completed_at)).timestamp() * 1000
            ) if completed_at else None
        except (ValueError, TypeError):
            completed_ms = None
        return {
            "artifact_id": prior.get("artifact_id"),
            "status": prior.get("status"),
            "events_total": coverage.get("events_total"),
            "capture_state": coverage.get("capture_state"),
            "completed_at_ms": completed_ms,
            "beta": pif.get("beta"),
            "fit_status": pif.get("status"),
        }

    # ------------------------------------------------------------------
    # Memory (episodic layer)
    # ------------------------------------------------------------------

    async def _recall_memory(self) -> tuple[list[Any], str]:
        """Recall the engine's own prior conclusions, rendered as a block."""
        if self.memory is None:
            return [], ""
        try:
            query = f"{self.symbol} beta fit regime capture"
            memories = await self.memory.recall(
                self.session_id, query=query, limit=MEMORY_RECALL_LIMIT,
            )
        except Exception:
            log.exception("memory recall failed; continuing without memory")
            return [], ""
        block = ""
        try:
            block = self.memory.render_context_block(
                memories, budget=MEMORY_CONTEXT_BUDGET,
            )
        except Exception:
            log.exception("memory render failed; continuing without block")
        return memories, block

    async def _prior_change_note(self) -> str:
        """Deterministic self-consistency diff vs the prior artifact."""
        prior = None
        if self.postgres is not None:
            try:
                prior = await self.postgres.read_inference_artifact(self.symbol)
            except Exception:  # noqa: BLE001 — ledger read must never kill the cycle
                prior = None
        if prior is None:
            prior = await self.store.read_latest_inference_artifact(
                self.symbol, self.venue,
            )
        headline = self._prior_headline(prior)
        if not headline:
            return "No prior artifact: this is the first inference cycle."
        return (
            "Prior artifact headline (deterministic diff basis):\n"
            + json.dumps(headline, default=str)[:_PRIOR_HEADLINE_MAX_CHARS]
        )

    # ------------------------------------------------------------------
    # Narration (max TWO LLM calls per cycle; ZERO when gate refuses)
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        return _SYSTEM_PROMPT_TEMPLATE.format(
            symbol=self.symbol,
            venue=self.venue,
            memory_protocol_section=(
                "MEMORY PROTOCOL (propose, never write):\n"
                + _load_kb(_MEMORY_PROTOCOL)
                if _MEMORY_PROTOCOL.exists() else ""
            ),
            tool_manifest=(
                "TOOL MANIFEST:\n" + _load_kb(_TOOL_MANIFEST)
                if _TOOL_MANIFEST.exists() else ""
            ),
        )

    @staticmethod
    def _output_format() -> str:
        return (
            "Return ONLY one JSON object:\n"
            "{\n"
            '  "summary": "2-4 sentence statistical interpretation",\n'
            '  "evidence": [{"path": "deterministic_state...", "value": ..., '
            '"interpretation": "...", "metric_name": "..."}],\n'
            '  "confidence": "low|medium|high",\n'
            '  "limitations": ["..."],\n'
            '  "model_separation": "one sentence on why beta and c/lambda are read separately",\n'
            '  "tool_calls": [{"name": "micro.fit_beta", "args": {"symbol": "BTCUSDT", '
            '"venue": "spot", "interval_seconds": 10, "window_minutes": 30}}],\n'
            '  "memory_proposals": [{"kind": "observation|hypothesis", "content": "...", '
            '"importance": 5.0, "tags": ["..."]}]\n'
            "}\n"
            "tool_calls: only when you genuinely need data not in deterministic_state "
            "(max 3). memory_proposals: max 3, kind fact forbidden. Omit empty lists."
        )

    async def _call_llm(self, user_prompt: str) -> str:
        if self.llm is None:
            raise NarrationParseError("no LLM client configured for narration")
        response = await self.llm.acall(
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=4_000,
        )
        raw = getattr(response, "raw_response", response)
        choices = getattr(raw, "choices", None)
        if choices:
            content = getattr(getattr(choices[0], "message", None), "content", None)
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                if parts:
                    return "".join(parts)
        if isinstance(response, str):
            return response
        return str(raw)

    # ------------------------------------------------------------------
    # Memory proposal resolution (LLM proposes; engine disposes)
    # ------------------------------------------------------------------

    def resolve_memory_proposals(
        self, proposals: Any, *, artifact_status: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Deterministically resolve LLM memory proposals.

        Returns (accepted, dispositions). Rules (spec §3):
        - non-dict / unknown kind / empty content → rejected
        - kind 'fact' is NEVER accepted from the LLM
        - more than MAX_MEMORY_PROPOSALS → the excess is rejected
        - proposal never carries run numbers as prose claims — tagging only
        """
        accepted: list[dict[str, Any]] = []
        dispositions: list[dict[str, Any]] = []
        if not isinstance(proposals, list):
            dispositions.append({"proposal": None, "disposition": "rejected",
                                 "reason": "proposals_not_a_list"})
            return accepted, dispositions
        valid_kinds = {"observation", "hypothesis", "note"}
        for index, proposal in enumerate(proposals):
            if len(accepted) >= MAX_MEMORY_PROPOSALS:
                dispositions.append({"proposal": proposal, "disposition": "rejected",
                                     "reason": "budget_exceeded"})
                continue
            if not isinstance(proposal, dict):
                dispositions.append({"proposal": proposal, "disposition": "rejected",
                                     "reason": "not_an_object"})
                continue
            kind = str(proposal.get("kind", ""))
            content = str(proposal.get("content", "")).strip()
            if kind == "fact":
                dispositions.append({"proposal": proposal, "disposition": "rejected",
                                     "reason": "fact_kind_is_llm_forbidden"})
                continue
            if kind not in valid_kinds or not content:
                dispositions.append({"proposal": proposal, "disposition": "rejected",
                                     "reason": "invalid_kind_or_empty_content"})
                continue
            try:
                importance = float(proposal.get("importance", 5.0))
            except (TypeError, ValueError):
                importance = 5.0
            importance = min(max(importance, 0.0), 10.0)
            tags = tuple(str(t) for t in (proposal.get("tags") or ()))
            accepted.append({
                "kind": kind,
                "content": content,
                "importance": importance,
                "tags": tags + (f"{self.symbol.lower()}-{self.venue}",),
            })
        return accepted, dispositions

    async def _remember(
        self, accepted: list[dict[str, Any]], run_meta: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Persist accepted proposals through MemoryNode (engine disposes)."""
        written: list[dict[str, Any]] = []
        if self.memory is None:
            return written
        for proposal in accepted:
            try:
                memory = await self.memory.remember(
                    self.session_id,
                    proposal["kind"],
                    proposal["content"],
                    run_id=run_meta.get("artifact_id"),
                    importance=proposal["importance"],
                    tags=proposal["tags"],
                    evidence_refs=(run_meta.get("artifact_id"),),
                )
                written.append({"memory_id": memory.memory_id,
                                "kind": memory.kind})
            except Exception:
                log.exception("memory write failed for proposal %s", proposal["kind"])
                written.append({"error": "write_failed", "kind": proposal["kind"]})
        return written

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------

    async def run_cycle(
        self, wake: WakeEnvelope, wake_meta: dict[str, Any],
    ) -> tuple[InferenceArtifact, dict[str, Any]]:
        """One full inference cycle. Returns (artifact, cycle_meta)."""
        generated_at = _utc_now_iso()
        capability_log: list[dict[str, Any]] = [{
            "capability": "engine.wake",
            "scope": {"symbol": self.symbol, "venue": self.venue},
            "result": "ok",
            "detail": {
                "trigger_source": wake.trigger_source,
                "predicates_fired": wake.predicates_fired,
                "decision": wake_meta.get("decision"),
                "consumed_wake_ids": wake_meta.get("consumed_wake_ids", []),
            },
        }]

        # --- GATHER: capture status + deterministic fit via the tool base ---
        status, status_log = await execute_tool(
            self.store, "micro.capture_status",
            {"symbol": self.symbol, "venue": self.venue},
        )
        capability_log.append(status_log)
        evidence, fit_log = await execute_tool(
            self.store, "micro.fit_beta",
            {"symbol": self.symbol, "venue": self.venue,
             "interval_seconds": 10, "window_minutes": 30},
        )
        capability_log.append(fit_log)

        fit_status = None
        n_observations = 0
        if evidence is not None:
            pif = evidence.get("price_impact_fit") or {}
            fit_status = pif.get("status")
            n_observations = int(pif.get("n_observations") or 0)
        events_in_window = int((evidence or {}).get("coverage", {}).get(
            "events_in_window", 0) or 0)
        status_obj = status or {}
        gate_status, gate_reasons = resolve_inference_status(
            n_observations=n_observations,
            min_observations=30,
            fit_status=fit_status,
            capture_state=status_obj.get("state"),
            events_in_window=events_in_window,
            sequence_gaps=int(status_obj.get("sequence_gaps") or 0),
        )

        deterministic_state: dict[str, Any] = {
            "wake": {
                "trigger_source": wake.trigger_source,
                "predicates_fired": wake.predicates_fired,
            },
            "capture_status": status,
            "microstructure_evidence": evidence,
            "gate": {"status": gate_status, "reasons": list(gate_reasons)},
        }

        # --- HARD GATE ---
        if gate_status == "insufficient":
            artifact = InferenceArtifact.create(
                symbol=self.symbol, venue=self.venue,
                generated_at=generated_at, completed_at=_utc_now_iso(),
                status="insufficient", window_minutes=30, interval_seconds=10,
                deterministic_state=deterministic_state,
                capability_log=capability_log,
                input_hash=str((evidence or {}).get("input_hash") or "gate-refused"),
                model_version="inference-engine-v1",
                interpretation=None,
                session_id=self.session_id,
                errors=[{"source": "gate", "error": "; ".join(gate_reasons)}],
            )
            await self._persist(artifact)
            # Null discipline with learning: remember the quality observation
            # (deterministic write, not an LLM proposal).
            if self.memory is not None:
                try:
                    await self.memory.remember(
                        self.session_id, "observation",
                        f"Cycle refused by gate: {'; '.join(gate_reasons)}",
                        run_id=artifact.artifact_id, importance=2.0,
                        tags=("gate", "insufficient", self.symbol.lower()),
                        evidence_refs=(artifact.artifact_id,),
                    )
                except Exception:
                    log.exception("gate-cycle memory write failed")
            return artifact, {"llm_calls": 0, "gate": gate_status,
                              "reasons": list(gate_reasons)}

        # --- CONTEXT: memory + prior diff ---
        _memories, memory_block = await self._recall_memory()
        prior_note = await self._prior_change_note()

        wake_block = json.dumps(
            {"trigger_source": wake.trigger_source,
             "predicates": wake.predicates_fired},
            default=str,
        )
        user_prompt_1 = (
            f"WAKE: {wake_block}\n\n"
            "DETERMINISTIC STATE (computed; never recomputed by you):\n"
            f"{json.dumps(deterministic_state, default=str)[:60_000]}\n\n"
            f"{prior_note}\n\n"
        )
        if memory_block:
            user_prompt_1 += (
                "RECALLED MEMORY (provenance-tagged priors; subordinate to the "
                f"ledger):\n{memory_block}\n\n"
            )
        user_prompt_1 += self._output_format()

        # --- NARRATE#1 ---
        llm_calls = 0
        try:
            raw_1 = await self._call_llm(user_prompt_1)
        except Exception as exc:
            log.exception("narration#1 failed")
            return await self._degraded_artifact(
                deterministic_state, capability_log,
                f"narration_failed: {type(exc).__name__}: {exc}",
            ), {"llm_calls": llm_calls}
        llm_calls += 1
        parsed_1 = _extract_json_object(raw_1)
        if parsed_1 is None:
            return await self._degraded_artifact(
                deterministic_state, capability_log,
                "narration_parse_failed: no JSON object in output",
            ), {"llm_calls": llm_calls}

        # --- TOOL ROUND (max one) ---
        tool_results: dict[str, Any] = {}
        tool_calls = parsed_1.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            for call in tool_calls[:3]:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name", ""))
                args = dict(call.get("args") or {})
                args.setdefault("symbol", self.symbol)
                args.setdefault("venue", self.venue)
                result, tool_log = await execute_tool(self.store, name, args)
                capability_log.append(tool_log)
                result_payload: Any
                if result is None:
                    result_payload = None
                else:
                    rendered = json.dumps(result, default=str)
                    if len(rendered) < 40_000:
                        result_payload = json.loads(rendered)
                    else:
                        result_payload = {"_truncated": True,
                                          "preview": rendered[:4_000]}
                tool_results[name] = result_payload
            # --- NARRATE#2 (final; includes tool results) ---
            user_prompt_2 = (
                "TOOL RESULTS (deterministic; cite paths):\n"
                f"{json.dumps(tool_results, default=str)[:40_000]}\n\n"
                "Produce your FINAL interpretation JSON now (same format; "
                "tool_calls must be empty or omitted — the budget is spent)."
            )
            try:
                raw_2 = await self._call_llm(user_prompt_2)
            except Exception:
                log.exception("narration#2 failed; falling back to narrate#1 output")
                parsed_final = parsed_1
            else:
                llm_calls += 1
                parsed_final = _extract_json_object(raw_2) or parsed_1
        else:
            parsed_final = parsed_1

        interpretation = {
            "summary": parsed_final.get("summary"),
            "evidence": parsed_final.get("evidence"),
            "confidence": parsed_final.get("confidence"),
            "limitations": parsed_final.get("limitations"),
            "model_separation": parsed_final.get("model_separation"),
        }

        artifact = InferenceArtifact.create(
            symbol=self.symbol, venue=self.venue,
            generated_at=generated_at, completed_at=_utc_now_iso(),
            status=gate_status, window_minutes=30, interval_seconds=10,
            deterministic_state=deterministic_state,
            capability_log=capability_log,
            input_hash=str((evidence or {}).get("input_hash") or "no-evidence"),
            model_version="inference-engine-v1",
            interpretation=interpretation,
            session_id=self.session_id,
        )
        await self._persist(artifact)

        # --- MEMORY PROPOSAL RESOLUTION (LLM proposes; engine disposes) ---
        accepted, dispositions = self.resolve_memory_proposals(
            parsed_final.get("memory_proposals"), artifact_status=gate_status,
        )
        written = await self._remember(
            accepted, {"artifact_id": artifact.artifact_id},
        )
        cycle_meta = {
            "llm_calls": llm_calls,
            "gate": gate_status,
            "tool_round": bool(tool_results),
            "memory": {"accepted": accepted, "dispositions": dispositions,
                       "written": written},
        }
        return artifact, cycle_meta

    async def _degraded_artifact(
        self,
        deterministic_state: dict[str, Any],
        capability_log: list[dict[str, Any]],
        error: str,
    ) -> InferenceArtifact:
        """Narration-failure artifact: deterministic state preserved, interpretation NULL."""
        artifact = InferenceArtifact.create(
            symbol=self.symbol, venue=self.venue,
            generated_at=_utc_now_iso(), completed_at=_utc_now_iso(),
            status="provisional", window_minutes=30, interval_seconds=10,
            deterministic_state=deterministic_state,
            capability_log=capability_log,
            input_hash=str((deterministic_state.get("microstructure_evidence") or {}).get(
                "input_hash") or "narration-failed"),
            model_version="inference-engine-v1",
            interpretation=None,
            session_id=self.session_id,
            errors=[{"source": "narration", "error": error}],
        )
        await self._persist(artifact)
        return artifact

    async def _persist(self, artifact: InferenceArtifact) -> dict[str, Any]:
        """Postgres-first durable write, then the Redis projection."""
        persistence: dict[str, Any] = {"postgres": False, "redis": False}
        if self.postgres is not None:
            try:
                persistence["postgres"] = bool(
                    await self.postgres.insert_inference_artifact(artifact)
                )
            except Exception:
                log.exception("postgres artifact insert failed")
        try:
            await self.store.publish_inference_artifact(artifact)
            persistence["redis"] = True
        except Exception:
            log.exception("redis artifact publish failed")
        return persistence

    async def close(self) -> None:
        await self.store.close()
        if self.postgres is not None:
            await self.postgres.close()
        if self.memory is not None:
            try:
                await self.memory.postgres.close()
                await self.memory.redis.close()
            except Exception:  # close is best-effort on shutdown
                log.debug("memory store close failed", exc_info=True)


__all__ = ["SESSION_TEMPLATE", "InferenceEngine", "NarrationParseError"]
