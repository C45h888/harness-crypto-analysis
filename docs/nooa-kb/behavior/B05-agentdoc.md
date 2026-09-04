# B05 — agentdoc behavior

## Call site

`/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py`

- Line 23 — `from nooa.agentdoc import hidden`
- Lines 106–110 — class attribute annotations on `MarketAnalyst`:
  - `symbol: Annotated[str, spec(description="Trading pair, e.g. SOLUSDT")] = "SOLUSDT"`
  - `remit: Annotated[str, spec(description="What this agent is responsible for analyzing")] = "Reason over the complete canonical market envelope."`
- Line 113 — `_current_envelope: Annotated[dict[str, Any] | None, hidden] = None`
- Line 114 — `_specialist_reports: Annotated[dict[str, str] | None, hidden] = None`
- Lines 176–190 — `DeltaOrderflowAgent.assess(envelope: Annotated[dict[str, Any], spec(description="Complete MarketRunEnvelope. …")])`
- Lines 223–235 — `MacroAgent.assess(envelope: Annotated[dict[str, Any], spec(description="Complete MarketRunEnvelope. …")])`
- Lines 269–279 — `OpenInterestAgent.assess(envelope: Annotated[dict[str, Any], spec(description="Complete MarketRunEnvelope. …")])`
- Lines 314–325 — `LiquidationAgent.assess(envelope: Annotated[dict[str, Any], spec(description="Complete MarketRunEnvelope. …")])`
- Lines 383–399 — `ControllerAgent.synthesize(envelope: Annotated[dict[str, Any], spec(description="Complete MarketRunEnvelope. …")], specialist_reports: Annotated[dict[str, str], spec(description="Specialist assessment reports …")])`

Note: `spec` itself is imported at line 22 from `nooa` (re-export of `nooa.agentdoc.spec`); `hidden` is the dedicated import at line 23.

## Imports exercised

- `nooa.agentdoc.spec` — singleton instance of `Spec` (`_docs.py::spec = Spec()`). Calling `spec(...)` with no positional target returns a `SpecAnnotation(**kwargs)` marker. Imported via `from nooa import spec` (re-export).
- `nooa.agentdoc.hidden` — singleton instance of `_Hidden` (`_visibility.py::hidden = _Hidden()`). Used in three roles: decorator (`@hidden`), annotation marker (`Annotated[T, hidden]` — bare singleton, NOT `hidden()`), and context manager (`with hidden:` — bare singleton, NO parentheses).

## Micro-test code

```python
import subprocess, sys
code = '''
from nooa.agentdoc import spec, hidden
from market_service.nooa_harness.agents import MarketAnalyst

# Inspect the class for annotated attributes.
print("symbol annotations:", getattr(MarketAnalyst, "__annotations__", {}).get("symbol"))
print("remit annotations:", getattr(MarketAnalyst, "__annotations__", {}).get("remit"))
# Hidden attrs may live in class __dict__ but be excluded from prompt formatting.

# Try `spec(description="...")`: is it a dataclass-like marker?
s = spec(description="test description")
print("spec returns:", repr(s)[:80])
print("spec dir:", [x for x in dir(s) if not x.startswith("_")])

# Try `hidden`:
h = hidden()
print("hidden returns:", repr(h)[:80])

# Instantiation test:
class StubLLM:
    def __init__(self): pass
    async def ainvoke(self, *a, **k): return "{}"
    def invoke(self, *a, **k): return "{}"
inst = MarketAnalyst(symbol="SOLUSDT", llm=StubLLM())
print("instantiated:", inst.symbol)
'''
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
print("stdout:", out.stdout)
print("stderr:", out.stderr)
```

## Observed behavior

Source-level analysis (no live Python execution possible in this environment — the `nooa` package import hangs at process startup, see "Drift verdict" for details). All findings below come from static reading of the installed package at `/Users/kamii/Documents/crypto-ai-anal/.venv/lib/python3.12/site-packages/nooa/agentdoc/`.

**`spec(description="…")` invocation:**

- `_docs.py::Spec.__call__` (line 128) accepts `target=_SENTINEL` as the sole positional, with all metadata as keyword-only arguments.
- When called with no positional target (e.g. `spec(description="X")`), it returns a `SpecAnnotation` instance.
- `SpecAnnotation` (`_docs.py` line 69) declares `__slots__ = ("kwargs",)` — the only attribute is `self.kwargs` (the dict of keyword arguments).
- Public attributes/methods of `SpecAnnotation`:
  - `kwargs` (slot)
  - `__call__(target)` — applies as a decorator, sets `_agentdoc_hidden` on the target and calls `set_docs_metadata(target, **self.kwargs)`.
  - `__repr__()` — returns `f"spec({kw})"` where `kw` is the comma-joined `k=v!r` representation of `self.kwargs`. So `spec(description="test description")` will repr as `'spec(description="test description")'`.
- `dir(s)` filtered to non-underscore names will show: `kwargs`. (`__call__`, `__repr__`, `__class__`, `__slots__` are filtered.)

**`hidden()` invocation:**

- `_visibility.py::_Hidden.__call__(self, func)` (line 51) requires a single positional `func` argument — used for the decorator form `@hidden` (equivalent to `hidden(func)`).
- Calling `hidden()` with **no arguments** will raise `TypeError: __call__() missing 1 required positional argument: 'func'`.
- The supported forms for the `hidden` singleton are:
  1. `@hidden` decorator (calls `hidden(func)`).
  2. `Annotated[T, hidden]` annotation marker (uses bare `hidden`).
  3. `with hidden:` context manager (uses bare `hidden`, NO parentheses — invokes `__enter__`/`__exit__`).
- `_Hidden.__repr__()` returns the literal string `"hidden"`.

**`MarketAnalyst` annotations (from static source inspection):**

- `__annotations__["symbol"]` evaluates to `typing.Annotated[str, <SpecAnnotation kwargs={'description': 'Trading pair, e.g. SOLUSDT'}>]`.
- `__annotations__["remit"]` evaluates to `typing.Annotated[str, <SpecAnnotation kwargs={'description': 'What this agent is responsible for analyzing'}>]`.
- `__annotations__["_current_envelope"]` evaluates to `typing.Annotated[dict[str, Any] | None, <hidden singleton>]`.
- `__annotations__["_specialist_reports"]` evaluates to `typing.Annotated[dict[str, str] | None, <hidden singleton>]`.
- The bare `hidden` singleton is used as an `Annotated` marker — this matches the documented contract (`Annotated[T, hidden]`).

**`MarketAnalyst` instantiation:**

- `MarketAnalyst(symbol="SOLUSDT", llm=StubLLM())` would invoke `__init__(self, symbol, *, llm)` (line 116), which calls `super().__init__(llm=llm)` and then `self.symbol = symbol.upper()`.
- The class attributes `symbol`, `remit`, `_current_envelope`, `_specialist_reports` are inherited as class-level defaults; `__init__` overrides only `self.symbol`.
- `inst.symbol` would equal `"SOLUSDT"` (the constructor uppercases input).

## Documented behavior

Per `docs/nooa-kb/from-monorepo/packages-nooa/agentdoc.md`:

### `spec` (§"### spec", lines 75–118)

- "`spec` is a **singleton instance** of `Spec` (defined at the bottom of `_docs.py` as `spec = Spec()`)."
- "Calling `spec(...)` returns a `SpecAnnotation` that is meant to be embedded as `typing.Annotated` metadata on a field, OR used as a decorator `@spec(...)` on a method/class, OR applied imperatively `spec(MyClass, "field", hidden=True)` for third-party types."
- Keyword arguments include `hidden: bool | None`, `description: str | None`, `expand: bool | None`, `max_length`, `max_string`, `max_depth`.
- "When used as `Annotated[T, spec(description="Display name")]`, the marker is **attached as `Annotated` metadata** on the field. At `doc()` render time the extractor walks `typing.get_args(hint)` looking for both `SpecAnnotation` instances and `hidden` instances (see `_visibility.py::is_hidden_field` lines 175–185)."

### `hidden` (§"### hidden", lines 120–146)

- "`hidden` is a **singleton instance** of `_Hidden` (defined at the bottom of `_visibility.py` as `hidden = _Hidden()`). It serves three roles:"
  1. "**Decorator**: `@hidden` on a method or module-level function. Sets `func._agentdoc_hidden = True`."
  2. "**Annotation marker**: `Annotated[T, hidden]` on variables / class fields."
  3. "**Context manager**: `with hidden:` at module level."
- "Filtering that strips hidden members from the LLM-visible context happens in `_visibility.py::filter_module_globals` / `is_hidden_field` / `is_hidden_method`, which are called from `core.py::doc`, `core.py::methods`, and `core.py::variables`. Members stay fully alive at runtime — they remain bound on the object / module and accessible via Python attribute lookup; only the documentation / prompt contract excludes them."

### `Annotated` (§"### Annotated", lines 152–178)

- "`typing.Annotated[T, ...metadata...]` is interpreted by the introspection layer in two ways:"
  - "The metadata is **stripped** when the type is rendered (`format_type` walks to the origin and drops the metadata arg) — only the inner `T` is shown in the prompt."
  - "The metadata **arguments** are inspected at extraction time. Each argument is matched against `hidden` (identity check `arg is hidden`) or against `isinstance(arg, SpecAnnotation)`."

The micro-test's `hidden()` invocation matches none of the three documented roles — it would not behave as a singleton inspector.

## Drift verdict

**Cannot run micro-test live** — the `nooa` package import hangs at process startup in this environment (Python processes were observed consuming CPU but producing no stdout output for >5 minutes when importing `nooa.agentdoc`, exit code 137 / SIGKILL on multiple attempts). All findings above are based on **static source analysis** of the installed `nooa.agentdoc` package files (`_docs.py`, `_visibility.py`, `__init__.py`).

**Micro-test bug** (drift between test expectations and actual API):
- The line `h = hidden()` in the micro-test is **incorrect**. The `hidden` singleton is NOT designed to be called with empty parens. Its `__call__` requires a `func` argument (decorator form). The three documented uses are: `@hidden` (decorator), `Annotated[T, hidden]` (bare singleton as marker), `with hidden:` (bare singleton as context manager — **no parens**).
- Expected behavior if the micro-test were run: `TypeError: __call__() missing 1 required positional argument: 'func'` raised by `_Hidden.__call__`.
- If the intent was to inspect the `hidden` singleton itself, the correct probe is `h = hidden` (no parens) followed by `repr(h)` (returns the string `"hidden"`).

**Call-site (agents.py) usage: NO drift.** All four annotation patterns in `agents.py` (lines 22–23, 106–110, 113–114, 176–190, 223–235, 269–279, 314–325, 383–399) match the documented contract exactly:
- `from nooa.agentdoc import hidden` ✓
- `Annotated[str, spec(description="...")]` for `symbol` and `remit` ✓ (uses `spec(...)` kwargs form, no positional target → returns `SpecAnnotation`)
- `Annotated[dict[str, Any] | None, hidden]` for `_current_envelope` and `_specialist_reports` ✓ (uses bare `hidden` singleton as the metadata arg)
- `Annotated[dict[str, Any], spec(description="Complete MarketRunEnvelope. ...")]` for `assess()` / `synthesize()` parameters ✓

**Parameter-annotation contract** (relevant to `assess(envelope)` / `synthesize(envelope, specialist_reports)`):
- Per `agentdoc.md` §`### spec`: "`max_length`/`max_depth` are *parameter annotations only* (not honored on class fields); `max_string` works on both parameter annotations and class fields."
- The `assess`/`synthesize` parameter annotations use `spec(description=...)` only — within the documented keyword surface.

## Suggested remediation

- Fix the micro-test (in any future B-lane re-run or reproduction):
  - Replace `h = hidden()` with `h = hidden` (bare singleton).
  - Replace `repr(h)` expectation with `repr(h) == "hidden"`.
  - Optionally add a third probe: `print(isinstance(h, _Hidden))` to confirm the singleton identity.
- No remediation required for `agents.py` — all `spec` / `hidden` / `Annotated` usage matches the documented contract verbatim.
- Consider adding a unit test that verifies `is_hidden_field(MarketAnalyst, "_current_envelope") is True` and `is_hidden_field(MarketAnalyst, "symbol") is False` to lock in the hidden/visible split between public and internal attributes. This was called out as UNVERIFIED in `agentdoc.md` and would close the loop.