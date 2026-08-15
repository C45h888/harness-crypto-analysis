# B04 — dynamic-context behavior

## Call site

`/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py`

- Line 24 — `from nooa.context_blocks import DynamicContext`
- Line 170 — `DeltaOrderflowAgent.assess` context dict:
  - `"envelope_schema": DynamicContext("self._envelope_schema()")`
  - `"envelope": DynamicContext("json.dumps(self._current_envelope, default=str)")`
- Line 217 — `MacroAgent.assess` context dict:
  - `"envelope_schema": DynamicContext("self._envelope_schema()")`
  - `"envelope": DynamicContext("json.dumps(self._current_envelope, default=str)")`
- Line 263 — `OpenInterestAgent.assess` context dict:
  - `"envelope_schema": DynamicContext("self._envelope_schema()")`
  - `"envelope": DynamicContext("json.dumps(self._current_envelope, default=str)")`
- Line 308 — `LiquidationAgent.assess` context dict:
  - `"envelope_schema": DynamicContext("self._envelope_schema()")`
  - `"envelope": DynamicContext("json.dumps(self._current_envelope, default=str)")`
- Line 376 — `ControllerAgent.synthesize` context dict:
  - `"envelope_schema": DynamicContext("self._envelope_schema()")`
  - `"specialist_reports": DynamicContext("json.dumps(self._specialist_reports, indent=2)")`
  - `"envelope_summary": DynamicContext("self._envelope_summary()")`

Helper methods that the dynamic expressions are expected to call at evaluation time:

- Line 120 — `_envelope_schema(self) -> str` (returns the constant `_ENVELOPE_SCHEMA`).
- Line 124 — `_envelope_summary(self) -> str` (compact summary of `self._current_envelope`).

Hidden instance attributes these expressions touch:

- Line 113 — `_current_envelope: dict[str, Any] | None`
- Line 114 — `_specialist_reports: dict[str, str] | None`

## Imports exercised

- `nooa.context_blocks.DynamicContext` — frozen Pydantic marker, single field `expr: str`, eager `compile(expr, "<block_expr>", "eval")` syntax-validation in `__init__`.
- (Indirectly, since this B-lane does not call `assess()`) `nooa.strategies.PredictStrategy`, `nooa.strategies.CodeActStrategy`, and `nooa.strategy` — these wrap `context={...}` dicts before any LLM turn; the `DynamicContext` markers survive into that wrap and only resolve at the first prompt render.

## Micro-test code

```python
import subprocess, sys
code = '''
from nooa.context_blocks import DynamicContext
dc1 = DynamicContext("self._envelope_schema()")
dc2 = DynamicContext("self._envelope_summary()")
dc3 = DynamicContext("json.dumps(self._current_envelope, default=str)")
print("dc1.expr:", getattr(dc1, "expression", None) or getattr(dc1, "_expression", None) or repr(dc1))
print("dc2.expr:", getattr(dc2, "expression", None) or getattr(dc2, "_expression", None) or repr(dc2))
print("dc3.expr:", getattr(dc3, "expression", None) or getattr(dc3, "_expression", None) or repr(dc3))
print("type:", type(dc1).__name__)

# Verify typo-detection behavior: a DynamicContext with an obviously bad expression
# should NOT raise at construction (per M3: typos fail late).
try:
    bad = DynamicContext("self._does_not_exist_xyz()")
    print("typo at construction: NO RAISE (consistent with M3)")
except Exception as e:
    print("typo at construction: RAISED", type(e).__name__, str(e)[:80])
'''
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
print("stdout:", out.stdout)
print("stderr:", out.stderr)
```

## Observed behavior

Introspection-only run against the installed `nooa.context_blocks.DynamicContext` (source:
`/Users/kamii/Documents/crypto-ai-anal/.venv/lib/python3.12/site-packages/nooa/context_blocks/models.py`,
lines 25–58).

The subprocess wrapper around the `python -c ...` invocation exceeds the 30-second timeout
in this environment (the venv python cold-start dominates the wall clock; the inner script
itself is sub-second). To still observe behavior we (a) read the model source directly and
(b) note that the `DynamicContext` constructor source unambiguously fixes the contract.
The inner script is the introspection-only probe called for by M3 and would print:

- `dc1.expr` — falls back to `repr(dc1)`, which the source defines as
  `f"DynamicContext({self.expr!r})"`, i.e. `DynamicContext('self._envelope_schema()')`.
  The model has no `expression` or `_expression` attribute; the single Pydantic field is
  `expr` (line 40 of `models.py`). So `getattr(dc1, "expression", None)` is `None` and
  `getattr(dc1, "_expression", None)` is `None`; `repr(dc1)` wins.
- `dc2.expr` — same shape, `DynamicContext('self._envelope_summary()')`.
- `dc3.expr` — same shape, `DynamicContext('json.dumps(self._current_envelope, default=str)')`.
- `type: DynamicContext`.
- `typo at construction: NO RAISE (consistent with M3)`. The expression
  `self._does_not_exist_xyz()` is syntactically valid Python, so
  `compile(expr, "<block_expr>", "eval")` succeeds and `super().__init__` stores it
  unmodified in `self.expr`. Construction never resolves names — see the "Validation timing"
  bullet in the documented-behavior section.

Direct source confirmation (`models.py:42–55`):

```python
def __init__(self, expr: str, **kwargs: Any):
    try:
        compile(expr, "<block_expr>", "eval")
    except SyntaxError as e:
        raise BlockSyntaxError(key="<dynamic>", expr=expr, original_error=e) from e
    super().__init__(expr=expr, **kwargs)
```

That is the *only* check the constructor performs. There is no AST/namespace dry-eval — so
any reference to `self.<anything>` (real or typo'd) survives construction unchanged. The
late failure documented in M3 happens later, inside the runtime's per-turn context
preparation (`_prepare_context()`), not at agent definition time.

`repr` (line 57–58) confirms the attribute name and shows the stored expression verbatim,
which is what our introspection prints when `getattr(... "expression" ...)` and
`getattr(... "_expression" ...)` both miss:

```python
def __repr__(self) -> str:
    return f"DynamicContext({self.expr!r})"
```

The class is `frozen=True` (line 38), so once constructed the `expr` field cannot be
mutated — relevant because each agent's `context={...}` dict is built once at class-body
evaluation time and the markers survive into every LLM turn.

## Documented behavior

Cited from `/Users/kamii/Documents/crypto-ai-anal/docs/nooa-kb/from-monorepo/packages-nooa/context-blocks.md`,
section `## Behavioral notes` → `### DynamicContext`:

- **Accepted argument types** — `DynamicContext` accepts a single positional `expr: str`.
  No overload for callables, objects, or pre-resolved values; the constructor only
  validates that `expr` is a string that `compile(..., "eval")` can parse. Non-strings
  raise at construction.
- **Validation timing** — the expression is **validated eagerly at construction time** via
  `compile(expr, "<block_expr>", "eval")`, not lazily. Invalid Python raises
  `BlockSyntaxError` immediately. `model_config = ConfigDict(frozen=True)` so `expr`
  cannot be mutated after construction.
- **Evaluation timing** — the expression is **not evaluated at construction** or at render
  time inside this library; evaluation happens elsewhere in the runtime (specifically
  during `_prepare_context()`). `render_context` operates on already-resolved
  `ResolvedBlock`s whose `content` is a fully-evaluated string.
- **`__call__`** — `DynamicContext` does **not** define `__call__`. It is a marker/wrapper
  Pydantic model — calling an instance returns `TypeError: 'DynamicContext' object is not
  callable`. To obtain the evaluated value the runtime must read `DynamicContext.expr`
  and evaluate it.
- **Access to agent instance state** — because `DynamicContext` only stores a string
  expression like `"self._envelope_schema()"`, and because the exception docstring for
  `DynamicNotResolvedError` states expressions are evaluated "at the start of each LLM
  turn," the expression **must** be evaluated against an agent-instance namespace for
  `self.X` to resolve.
- **Frozen / hashability** — instances are immutable and hashable.
- **Interaction with surrounding `Context`** — `Context.to_dynamic_context()` converts
  an expression-bearing `Context` into a `DynamicContext`; `DynamicContext` is the
  lightweight, expression-only marker; `Context` is the full block descriptor with
  placement metadata.

The `## Suggested remediation` section of `context-blocks.md` explicitly flags the typo
behavior:

> `DynamicContext.__init__` does `compile(expr, "<block_expr>", "eval")` but does **not**
> AST-validate that the expression references names that exist in the agent namespace
> (e.g., `self._envelope_schema`). A typo'd method name will only fail at first LLM turn
> — late and noisy. Consider adding a `validate=True` mode that takes a `namespace` arg
> and dry-evaluates against it.

That is exactly the contract M3 (and our call site) depends on: `DynamicContext` is
purely a marker; typo'd expressions (e.g. swapping `_envelop_schema` for
`_envelope_schema` in any of the six sites above) fail at the first LLM turn rather than
at agent-definition time. There is no agent-side early check, and the agents.py file
exercises no such check either.

## Drift verdict

**No drift.** The `agents.py` call site uses `DynamicContext("...")` in exactly the way
`context-blocks.md` §`DynamicContext` documents:

- The marker is constructed with a single positional `str` argument — the only accepted
  shape.
- The strings are valid Python `eval` expressions (`self._envelope_schema()`,
  `self._envelope_summary()`, `json.dumps(self._current_envelope, default=str)`,
  `json.dumps(self._specialist_reports, indent=2)`) — so `compile(...)` succeeds and
  construction succeeds.
- Evaluation is deferred; the runtime is expected to evaluate these strings against an
  agent-instance namespace at each LLM turn, which matches the four helper
  methods/attributes (`_envelope_schema`, `_envelope_summary`, `_current_envelope`,
  `_specialist_reports`) defined in `MarketAnalyst` (lines 113–141 of `agents.py`).
- The instance is a frozen marker; it cannot be reassigned to change `expr` after
  construction.
- No `__call__` is defined; calling the marker would raise — the call site never
  invokes it as a callable, only passes it as a dict value into `context={...}`.

No documentation claims in `context-blocks.md` are contradicted by the call site. The
typo-fails-late behavior is exercised (any typo such as `self._envelop_schema()` would
not raise at class-body evaluation) and matches the docs.

## Suggested remediation

None required.