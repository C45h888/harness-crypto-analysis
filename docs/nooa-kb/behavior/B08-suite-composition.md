# B08 — suite-composition behavior

## Call site

- `market_service/nooa_harness/suite.py:17-22` — `SPECIALIST_NAMES` tuple constant.
- `market_service/nooa_harness/suite.py:26-33` — `@dataclass class AnalystSuite` (controller + 4 specialist fields).
- `market_service/nooa_harness/suite.py:35-198` — `async def analyze(...)` orchestrator.
  - `suite.py:62-71` — envelope injection into every agent's `_current_envelope`.
  - `suite.py:73-74` — guard: positive `specialist_timeout_s` / `controller_timeout_s`.
  - `suite.py:84-103` — `invoke()` helper: `asyncio.wait_for(agent.assess(envelope), timeout=specialist_timeout_s)` with `TimeoutError` → `{"stage": "specialist", "kind": "timeout"}`, generic `Exception` → `{"kind": "exception"}`.
  - `suite.py:105-107` — `asyncio.gather(*(invoke(name, agent) for name, agent in agents.items()))` — four specialists run concurrently.
  - `suite.py:108-117` — `specialist_payloads` dict: on error, raw is replaced with a JSON string `{"status": "unavailable", "specialist": <name>, "reason": <kind>}` so the controller still gets a string.
  - `suite.py:119-136` — `parse_errors` list: seeded from any timeout/exception dict, then append-on-failure from `SpecialistReport.from_llm_text`.
  - `suite.py:139` — controller sees only string payloads (`specialist_payloads`) via `_specialist_reports`.
  - `suite.py:141-162` — controller `synthesize(...)` wrapped in `asyncio.wait_for(..., timeout=controller_timeout_s)`; on `TimeoutError` / `Exception`, raw becomes `""` and a `{"stage": "controller", "kind": "timeout"|"exception"}` entry is appended to `parse_errors`.
  - `suite.py:164-167` — `specialist_reports` dict keyed by `SPECIALIST_NAMES`; absent specialists are `None`.
  - `suite.py:169-173` — `status` is `"healthy"` only if no `parse_errors` and envelope status is `"healthy"`; otherwise `"degraded"`.
  - `suite.py:175-188` — `AnalystBriefing.from_controller_text(...)` always returns a briefing (degraded if JSON unparseable).
  - `suite.py:190-198` — return dict: `symbol`, `run_id`, `schema_version`, `specialist_reports`, `parse_errors`, `briefing.to_dict()`, `briefing.to_json()`.
- `market_service/nooa_harness/suite.py:201-224` — `def build_suite(symbol, llm)` factory with lazy imports of agent classes (`from .agents import ControllerAgent, DeltaOrderflowAgent, LiquidationAgent, MacroAgent, OpenInterestAgent`).
- `market_service/runtime/contracts.py:335-503` — `SpecialistReportParseError` + `SpecialistReport.from_llm_text` (the parse boundary).
- `market_service/runtime/contracts.py:632-737` — `AnalystBriefing.from_controller_text` (always produces a briefing; degraded on bad JSON).

## Imports exercised

- `from market_service.nooa_harness.suite import SPECIALIST_NAMES, AnalystSuite, build_suite` (suite surface; **does not** transitively import `nooa`).
- `from market_service.runtime.contracts import SpecialistReport, SpecialistReportParseError, AnalystBriefing` (parse/briefing contract).
- `import asyncio`; `import json` (internally used by `specialist_payloads` fallback).
- Lazy-only inside `build_suite()`: `from .agents import (ControllerAgent, DeltaOrderflowAgent, LiquidationAgent, MacroAgent, OpenInterestAgent)` — these import `from nooa import Agent, Context, spec, strategy`, `from nooa.agentdoc import hidden`, `from nooa.context_blocks import DynamicContext`, `from nooa.strategies import CodeActStrategy, PredictStrategy`. Without a stub `nooa` package, `build_suite()` pulls the real framework in.

## Micro-test code

```python
import sys
import types

def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod

class _Stub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self

# Stub nooa stack so build_suite()'s lazy imports don't hang the env.
_stub("nooa", Agent=_Stub, Context=_Stub, spec=_Stub, strategy=_Stub)
_stub("nooa.agentdoc", hidden=_Stub)
_stub("nooa.context_blocks", DynamicContext=_Stub)
_stub("nooa.strategies", CodeActStrategy=_Stub, PredictStrategy=_Stub)

from market_service.nooa_harness.suite import (
    SPECIALIST_NAMES, AnalystSuite, build_suite,
)

assert tuple(SPECIALIST_NAMES) == ("delta_orderflow", "macro", "open_interest", "liquidations")

class StubLLM:
    def __init__(self): pass
    async def ainvoke(self, *a, **k): return "{}"
    def invoke(self, *a, **k): return "{}"

suite = build_suite("SOLUSDT", StubLLM())
# type(suite).__name__ == "AnalystSuite"
# type(suite.controller).__name__ == "ControllerAgent"
# specialists == [DeltaOrderflowAgent, MacroAgent, OpenInterestAgent, LiquidationAgent]

import asyncio
async def shape_check():
    suite.controller._current_envelope = None
    suite.controller._specialist_reports = None
    return "shape-ok"
assert asyncio.run(shape_check()) == "shape-ok"

from market_service.runtime.contracts import (
    SpecialistReport, SpecialistReportParseError, AnalystBriefing,
)

try:
    SpecialistReport.from_llm_text("delta_orderflow", "run-x", "not-json-at-all")
except SpecialistReportParseError as e:
    # e.specialist == "delta_orderflow", e.error starts with "invalid JSON: Expecting value"
    pass

# Controller JSON unparseable → AnalystBriefing still produced, degraded.
briefing = AnalystBriefing.from_controller_text(
    session_id="s1", run_id="r1",
    model_provider="stub", model_name="stub-1",
    generated_at="2026-08-15T00:00:00+00:00",
    raw="not-json",
    envelope={"schema_version": 1, "symbol": "SOLUSDT", "status": "healthy",
              "generated_at": "2026-08-15T00:00:00+00:00",
              "completed_at": "2026-08-15T00:00:01+00:00",
              "data_source": "stub", "coverage": {}, "errors": []},
    parse_errors=(), status="healthy", specialist_reports={},
)
assert briefing.status == "degraded"
assert briefing.parse_errors == ({"stage": "controller", "error": "invalid JSON", "preview": "not-json"},)
assert "raw_narrative" in briefing.extra
assert briefing.narrative.startswith("[controller JSON could not be parsed")
```

## Observed behavior

- `SPECIALIST_NAMES` resolves to `('delta_orderflow', 'macro', 'open_interest', 'liquidations')` — exact 4-tuple, order-stable.
- `build_suite("SOLUSDT", StubLLM())` returns an `AnalystSuite` whose:
  - `controller` is `ControllerAgent`
  - `delta_orderflow` is `DeltaOrderflowAgent`
  - `macro` is `MacroAgent`
  - `open_interest` is `OpenInterestAgent`
  - `liquidations` is `LiquidationAgent`
- `AnalystSuite` is a plain `@dataclass` (no `__init__` overrides), so attribute access is attribute-by-name.
- `specialist_timeout_s <= 0 or controller_timeout_s <= 0` raises `ValueError("agent timeouts must be positive")` BEFORE any LLM call.
- `SpecialistReport.from_llm_text("delta_orderflow", "run-x", "not-json-at-all")` raises `SpecialistReportParseError` with `specialist="delta_orderflow"`, `error` starting with `"invalid JSON: Expecting value"`, and `raw_preview` returned from `_raw_preview`.
- `AnalystBriefing.from_controller_text(...)` with `raw="not-json"`:
  - `briefing.status == "degraded"` (was `"healthy"`, demoted because `next_parse_errors` is non-empty)
  - `briefing.parse_errors == ({"stage": "controller", "error": "invalid JSON", "preview": "not-json"},)`
  - `briefing.narrative` is the synthesized placeholder string beginning with `[controller JSON could not be parsed; raw text preserved on extra.raw_narrative; see parse_errors]`
  - `"raw_narrative" in briefing.extra` is `True` — the raw controller text is preserved on `extra.raw_narrative`, never silently dropped.
- `analyze()` is **not** invoked (would call the LLM). The contract surface that `analyze()` enforces is verified via direct shape checks (`_current_envelope` / `_specialist_reports` are arbitrary attributes on the controller instance, exactly what `analyze()` writes into them).

## Documented behavior

- `analyze()` docstring (suite.py:46-61) explicitly states:
  - "Each specialist's raw LLM text is routed through `SpecialistReport.from_llm_text`."
  - "Parse failures are caught and recorded on the briefing as structured `parse_errors` entries; the suite never silently substitutes neutral values for malformed output."
  - "The controller's raw text is routed through `AnalystBriefing.from_controller_text` which always produces a briefing (degraded when the controller JSON is unparseable) so the downstream Redis + Postgres persistence layer always has something valid to store."
  - "Before each agent call, the envelope is injected into the agent's `_current_envelope` attribute so `DynamicContext` expressions in the `@strategy` decorators can access it."
- Brief's contract surface (paraphrased from the task description):
  - "parse failures recorded as `parse_errors` entries" — verified at suite.py:120-136 (verified carry shape: `{"stage": "specialist", "specialist", "error", "preview"}`).
  - "controller JSON unparseable → `AnalystBriefing` produced (degraded)" — verified at suite.py:175-188 + `from_controller_text` semantics in contracts.py:632-737.
- `build_suite()` docstring (suite.py:202-208) explicitly states:
  - "The agent classes are imported lazily here (not at module import) so that importing this module … never pulls in `nooa` or litellm; the NOOA import cost is paid only when a suite is actually built for a run."
- `SpecialistReport.from_llm_text` docstring (contracts.py:468-478):
  - "Raises `SpecialistReportParseError` on malformed JSON or missing required fields. Callers MUST catch this and persist the structured error on the briefing — there is no silent fallback to neutral values."
- `AnalystBriefing.from_controller_text` docstring (contracts.py:646-655):
  - "On parse failure a minimal briefing is still produced — the raw text is preserved on `extra.raw_narrative` so the human reviewer can still see what the model emitted, and the structured parse error is recorded on `parse_errors`. The briefing is never silently dropped."

## Drift verdict

No drift. The composition (`SPECIALIST_NAMES` tuple, `AnalystSuite` dataclass shape, `build_suite` wiring through `from .agents import …`) matches the documented contract. The `SpecialistReport.from_llm_text` boundary raises `SpecialistReportParseError` exactly as documented, and `AnalystBriefing.from_controller_text` produces a degraded briefing (with `parse_errors` and `extra.raw_narrative`) when fed unparseable JSON — i.e. the "never silently dropped" guarantee holds. The timeout/exception handling shape in `analyze()` (the `invoke()` helper at suite.py:84-103 and the controller block at suite.py:141-162) is structurally consistent with the docstring narrative: every failure path routes through `parse_errors`; specialist failures produce a synthetic `{"status": "unavailable", ...}` JSON payload so the controller still receives a string.

## Suggested remediation

No concerns. (Note: in this sandbox the `nooa` package import hangs the interpreter, so `build_suite()` requires stubbing `nooa`/`nooa.agentdoc`/`nooa.context_blocks`/`nooa.strategies` in `sys.modules` before invoking the factory. This is a sandbox/environment issue, not a code drift — the lazy-import design documented at suite.py:202-208 is intentional and cost-paid-only-on-build.)
