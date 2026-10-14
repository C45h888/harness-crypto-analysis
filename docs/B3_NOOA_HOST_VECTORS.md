# B3 — NOOA Agent host vectors (assessment, not implemented)

Status: **recorded for decision.** B1 (`memory.*` tools) and B2
(`nooa_harness/memory_skill.py`) are the implemented memory surface. B3 is the
question of whether the harness should adopt a real **NOOA Agent host** so a
`nooa.skill.Skill` has somewhere to attach.

This document records what that would mean, verified against the installed
packages — not from memory.

---

## 1. The gap B3 exists to close

`market_service/nooa_harness/memory.py` names *"the optional in-agent
`nooa-memory` `MemorySkill` attachment (`memory_skill.py`)"*. That attachment
has never existed, and cannot attach today — because **there is no NOOA Agent
in this system** to attach it to.

The entire `nooa` surface this harness touches is two things:

| Used | For |
|---|---|
| `nooa.unifiedllm.registry.get_llm_client(...) -> UnifiedLLM` | the LLM client (`market_service/nooa_harness/backends.py` — the single import site) |
| `nooa_cli` | the CLI shell (`nooa market …`) |

Everything else — the loop, transitions, budgets, gates, coverage,
persistence — is ours.

## 2. What a NOOA Agent actually is

`nooa.agent.Agent` (metaclass `AgentMeta`), verified in the installed package:

- **agent = Python object; `...` body = LLM; docstring = prompt; return type =
  contract.** The class docstring *is* the system prompt: *"You are
  {type(self).__name__}, a Python agent working in an interactive session."*
- The base owns the runtime: `runtime` (ActorRuntime), `event_manager`,
  `context_manager`, `llm`, `_storage`, plus `context`/`events` APIs and
  truncation rendering.
- Execution is selected by a **strategy**:
  - `CodeActStrategy` — **the default.** *"gives the LLM `execute_python()` +
    `return_result()` tools"*, iterating in a Jupyter-style REPL
    (`max_iterations` typically 50).
  - `PredictStrategy` — *"single-shot LLM call that solves the task in one go."*
  - `GenerationStrategy`.
- Skills attach as attributes (`self.memory = MemorySkill(...)`), discovered by
  the LLM via `doc(self.<skill>)`; context blocks and events are per agent.

## 3. What OUR "agent" actually is

**Not a NOOA Agent.** The agent is `InferenceEngine.narrate_cycle`, a bespoke
deterministic orchestration loop. One turn is:

```
llm.acall(messages=[system, user], output_model=NarrationTurn, max_tokens=…)
```

and the reply is a **JSON object** with `tool_calls`, parsed by `coerce_turn`.
The **engine** — not the LLM — decides the stage traversal, dispatch, phase
coverage, budgets, FSM legality, validation, receipts and persistence.

Honest name: a **governed narration engine** — a deterministic workflow engine
with an LLM narrator. It is not autonomous: the agent cannot choose its next
stage.

## 4. Why we do not run the NOOA host today

The system's core invariant (canonical runtime doctrine / charter):

> *The LLM proposes; every proposal crosses a deterministic boundary before it
> can touch state.*

NOOA's default host is the opposite: the model **executes code directly**. The
engine therefore re-implemented the host's responsibilities deterministically:

| Host concern | NOOA | This harness |
|---|---|---|
| runtime / loop | `ActorRuntime` + strategy | `engine/core/runner.py` `narrate_cycle` |
| events | `event_manager` | FSM transitions + loop receipts |
| context | context blocks | `deterministic_state` + central ledger |
| budgets | strategy iterations | `LOOP_PASS_BUDGET`, `AGENTIC_MAX_*` |
| legality | none | `engine/fsm.py` + gates |

Running NOOA's host alongside would mean **two competing runtimes** — the same
failure class as two competing memory stores.

## 5. The B3 vectors

### B3-CodeAct (default NOOA)
Adopt the REPL agent; the LLM writes Python.

| Impact | Detail |
|---|---|
| Turn contract | becomes *code*, not JSON → `build_output_format`, `coerce_turn`, `TOOL_PHASE` coverage, `validate_final_turn` all incompatible |
| Determinism | FSM legality, gates, phase coverage, replayable artifacts are lost or must be re-implemented |
| Budget | CodeAct iterates (~50 steps) vs our 16 turns / 11 tool rounds per cycle |
| Cost | rewrites the agentic surface; the characterization fixtures and most of the suite become meaningless |

**Verdict: not recommended.** It trades away the deterministic governor, which
is the product.

### B3-Predict (host our engine inside one method)
Introduce a thin NOOA `Agent` whose body *is* the existing engine:

```python
class InferenceAgent(nooa.Agent):
    async def run_cycle(self, wake) -> InferenceArtifact:
        ...   # body = the existing deterministic engine cycle
```

| Impact | Detail |
|---|---|
| Loop | unchanged — the engine still drives every turn |
| Boundary | unchanged — the engine remains the governor |
| Memory | `MemorySkill` now has a host and can attach natively |
| Skills/context | NOOA's `Skill`, context blocks and events become available |
| Cost | one shell class + a strategy choice (`PredictStrategy`); small and reversible |

**Verdict: the only B3 worth considering** — it is a shell, not a rewrite.

## 6. What B3 buys, and what it costs

**Buys (B3-Predict):** native `nooa.skill.Skill` attachment for the memory
skill; NOOA context blocks/events; community packaging alignment.

**Costs (any B3):** the `nooa-memory` package still does not exist in this
environment (`packages/nooa-memory` is an external monorepo package; not
installed, not vendored), so a `MemorySkill` would still be ours. B3 buys the
*host*, not the memory implementation.

## 7. Preconditions before any B3 work

1. Decide the loop host **intentionally** — B3-Predict only; never CodeAct
   while the deterministic boundary is a requirement.
2. Keep `engine/fsm.py`, gates, receipts and the central ledger as the
   authority — the NOOA Agent must wrap the engine, never replace it.
3. Re-verify the budget economy (`AGENTIC_MAX_LLM_TURNS`, `LOOP_PASS_BUDGET`)
   under a host that may add its own turns.
4. Decide whether `nooa-memory` is vendored later; if so, `MemorySkill` becomes
   a thin adapter over it rather than a re-implementation.

## 8. Current decision

**B1 + B2 only.** Agent memory is the `memory.*` tool family backed by
`nooa_harness/memory_skill.py`, segmented to the cycle's task segment, with no engine
disposition. B3 stays recorded here and is not scheduled.
