"""Composition of the long-running NOOA analyst suite."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from market_service.runtime.contracts import (
    AgentMemory,
    AnalystBriefing,
    SpecialistReport,
    SpecialistReportParseError,
)
from market_service.nooa_harness.memory import MemoryNode

SPECIALIST_NAMES: tuple[str, ...] = (
    "delta_orderflow",
    "macro",
    "open_interest",
    "liquidations",
)


@dataclass
class AnalystSuite:
    """One controller and the specialist agents for a symbol."""

    controller: ControllerAgent
    delta_orderflow: DeltaOrderflowAgent
    macro: MacroAgent
    open_interest: OpenInterestAgent
    liquidations: LiquidationAgent

    async def analyze(
        self,
        envelope: dict[str, Any],
        *,
        session_id: str,
        model_provider: str,
        model_name: str,
        specialist_timeout_s: float = 120.0,
        controller_timeout_s: float = 180.0,
        prompt_version: str = "nooa-harness-v1",
        prior_memories: list[AgentMemory] | None = None,
    ) -> dict[str, Any]:
        """Run specialists over one canonical envelope and synthesize the result.

        Each specialist's raw LLM text is routed through
        ``SpecialistReport.from_llm_text``. Parse failures are caught and
        recorded on the briefing as structured ``parse_errors`` entries;
        the suite never silently substitutes neutral values for malformed
        output. The controller's raw text is routed through
        ``AnalystBriefing.from_controller_text`` which always produces a
        briefing (degraded when the controller JSON is unparseable) so the
        downstream Redis + Postgres persistence layer always has something
        valid to store.

        Before each agent call, the envelope is injected into the agent's
        ``_current_envelope`` attribute so ``DynamicContext`` expressions
        in the ``@strategy`` decorators can access it.
        """
        run_id = str(envelope.get("run_id") or "")
        # Inject envelope into all agents so DynamicContext can read it
        agents_all = (
            self.delta_orderflow,
            self.macro,
            self.open_interest,
            self.liquidations,
            self.controller,
        )
        for agent in agents_all:
            agent._current_envelope = envelope

        # NOOA v0.0.8 PredictStrategy enforces max_param_chars on every
        # strategy PARAMETER (not just context blocks); the live raw-evidence
        # envelope routinely exceeds 200k chars. All agents share ONE bounded,
        # explicitly-marked LLM view built from the canonical envelope so both
        # the method parameter and the DynamicContext blocks stay under the
        # limit. The complete canonical envelope is never mutated — it remains
        # the immutable input; this is only the LLM-bound projection.
        llm_envelope: dict[str, Any] | None = None
        if envelope is not None:
            llm_envelope = json.loads(agents_all[0]._bounded_envelope())
            for agent in agents_all:
                agent._current_envelope = llm_envelope

        # Seed prior session memory as a context block (cross-run coherence).
        # The analyst sees its OWN prior conclusions labeled by kind+id —
        # advisory context only, never canonical state. Empty block clears.
        prior_block = MemoryNode.render_context_block(
            prior_memories or [], budget=3000
        )
        for agent in agents_all:
            if prior_block:
                agent.context["prior_session_memory"] = prior_block
            else:
                agent.context.pop("prior_session_memory", None)

        if specialist_timeout_s <= 0 or controller_timeout_s <= 0:
            raise ValueError("agent timeouts must be positive")

        agents = {
            "delta_orderflow": self.delta_orderflow,
            "macro": self.macro,
            "open_interest": self.open_interest,
            "liquidations": self.liquidations,
        }
        started_at = _now_iso()

        async def invoke(name: str, agent: Any) -> tuple[str, str | None, dict[str, Any] | None]:
            try:
                raw = await asyncio.wait_for(
                    agent.assess(llm_envelope), timeout=specialist_timeout_s,
                )
                return name, raw, None
            except asyncio.TimeoutError:
                return name, None, {
                    "stage": "specialist",
                    "specialist": name,
                    "error": f"timed out after {specialist_timeout_s:g}s",
                    "kind": "timeout",
                }
            except Exception as exc:
                return name, None, {
                    "stage": "specialist",
                    "specialist": name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "kind": "exception",
                }

        specialist_results = await asyncio.gather(*(
            invoke(name, agent) for name, agent in agents.items()
        ))
        specialist_payloads = {
            name: (
                raw if raw is not None else json.dumps({
                    "status": "unavailable",
                    "specialist": name,
                    "reason": error.get("kind", "error") if error else "error",
                })
            )
            for name, raw, error in specialist_results
        }

        reports: dict[str, SpecialistReport] = {}
        parse_errors: list[dict[str, Any]] = [
            error for _, _, error in specialist_results if error is not None
        ]
        for name, raw, error in specialist_results:
            if raw is None or error is not None:
                continue
            try:
                reports[name] = SpecialistReport.from_llm_text(name, run_id, raw)
            except SpecialistReportParseError as exc:
                parse_errors.append(
                    {
                        "stage": "specialist",
                        "specialist": exc.specialist,
                        "error": exc.error,
                        "preview": exc.raw_preview,
                    }
                )

        # Inject explicit report/unavailable payloads for controller context.
        self.controller._specialist_reports = specialist_payloads

        controller_error: dict[str, Any] | None = None
        try:
            controller_raw = await asyncio.wait_for(
                self.controller.synthesize(llm_envelope, specialist_payloads),
                timeout=controller_timeout_s,
            )
        except asyncio.TimeoutError:
            controller_raw = ""
            controller_error = {
                "stage": "controller",
                "error": f"timed out after {controller_timeout_s:g}s",
                "kind": "timeout",
            }
        except Exception as exc:
            controller_raw = ""
            controller_error = {
                "stage": "controller",
                "error": f"{type(exc).__name__}: {exc}",
                "kind": "exception",
            }
        if controller_error is not None:
            parse_errors.append(controller_error)

        specialist_reports = {
            name: reports[name].to_dict() if name in reports else None
            for name in SPECIALIST_NAMES
        }
        completed_at = _now_iso()
        status = (
            "healthy"
            if not parse_errors and envelope.get("status") == "healthy"
            else "degraded"
        )

        briefing = AnalystBriefing.from_controller_text(
            session_id=session_id,
            run_id=run_id,
            model_provider=model_provider,
            model_name=model_name,
            generated_at=started_at,
            raw=controller_raw,
            envelope=envelope,
            parse_errors=tuple(parse_errors),
            prompt_version=prompt_version,
            completed_at=completed_at,
            status=status,
            specialist_reports=specialist_reports,
        )

        return {
            "symbol": self.controller.symbol,
            "run_id": run_id,
            "schema_version": envelope.get("schema_version"),
            "specialist_reports": specialist_reports,
            "parse_errors": list(briefing.parse_errors),
            "briefing": briefing.to_dict(),
            "briefing_json": briefing.to_json(),
        }


def build_suite(symbol: str, llm: Any) -> AnalystSuite:
    """Instantiate the complete analyst suite without starting model work.

    The agent classes are imported lazily here (not at module import) so that
    importing this module — and running the contract/runner/persistence tests —
    never pulls in ``nooa`` or litellm; the NOOA import cost is paid only when
    a suite is actually built for a run.
    """
    from .agents import (
        ControllerAgent,
        DeltaOrderflowAgent,
        LiquidationAgent,
        MacroAgent,
        OpenInterestAgent,
    )

    symbol = symbol.upper()
    return AnalystSuite(
        controller=ControllerAgent(symbol, llm=llm),
        delta_orderflow=DeltaOrderflowAgent(symbol, llm=llm),
        macro=MacroAgent(symbol, llm=llm),
        open_interest=OpenInterestAgent(symbol, llm=llm),
        liquidations=LiquidationAgent(symbol, llm=llm),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["AnalystSuite", "SPECIALIST_NAMES", "build_suite"]
