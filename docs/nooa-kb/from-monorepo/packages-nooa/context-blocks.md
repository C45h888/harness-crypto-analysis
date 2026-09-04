# nooa context_blocks — DynamicContext semantics

## Source paths read

- `/tmp/nooa-monorepo/src/nooa/context_blocks/__init__.py`
- `/tmp/nooa-monorepo/src/nooa/context_blocks/models.py`
- `/tmp/nooa-monorepo/src/nooa/context_blocks/scoped.py`
- `/tmp/nooa-monorepo/src/nooa/context_blocks/renderer.py`
- `/tmp/nooa-monorepo/src/nooa/context_blocks/exceptions.py`

## Public exports

| Name | Defined at | Constructor signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `DynamicContext` | `models.py` | `DynamicContext(expr: str, **kwargs: Any)` | "Marks a context block for dynamic evaluation each turn. Wraps a Python expression string that will be evaluated by the runtime at each LLM turn. The expression is validated at creation time." |
| `Context` | `models.py` | `Context(value: str \| None = None, *, expr: str \| None = None, prefix: bool = False, **kwargs: Any)` | "Unified context block value — controls content and placement. Two orthogonal axes: Content (literal text or re-evaluated expression) and Placement (cacheable prefix or volatile suffix)." |
| `ResolvedBlock` | `models.py` | `ResolvedBlock(key: str, content: str, role: Role = Role.SYSTEM, metadata: BlockMetadata = ..., event: EventBase \| None = None)` | "A fully-resolved block ready for rendering. All content has been evaluated — no expressions, no Dynamic markers." |
| `Role` | `roles.py` (re-exported from `models.py`) | enum | Role enum re-exported for backward compatibility (no factory in this module). |
| `BlockMetadata` | `models.py` | `BlockMetadata(expr=None, tag=None, truncated=False, user_block=False, static=False, source_dynamic=False)` | "Typed metadata for resolved blocks. Replaces the untyped dict[str, Any] with well-defined fields." |
| `ContextWindowStats` | `models.py` | `ContextWindowStats(...)` (extra="forbid") | "Context window utilization snapshot. The single source of truth for token usage is prompt_tokens — the exact prompt-token count reported by the provider in the response usage." |
| `RenderedMessage` | `models.py` | `RenderedMessage(role, content=None, parts=None, tool_call=None, tool_call_id=None, images=None)` | "Neutral message emitted by a BlockFormatter. The BlockFormatter is responsible for ordering the system prompt, the event history, and any additional context messages into a single list[RenderedMessage]." |
| `ToolCallInfo` | `models.py` | `ToolCallInfo(id, name, arguments)` | "Structured tool-call payload on a RenderedMessage. Provider formatters reshape this into the appropriate wire format." |
| `ScopedContext` | `scoped.py` | `ScopedContext(context: dict[str, str \| DynamicContext \| None] \| None = None, events: EventQuery \| None = None)` | "Temporarily override context blocks and event filtering within a scope. Overrides apply to all LLM calls within the scope. Supports nesting." |
| `RenderResult` | `renderer.py` | `NamedTuple(output: Any, stats: ContextWindowStats, messages: list[RenderedMessage])` | "Result of render_context: provider output + utilization stats." |
| `render_context` | `renderer.py` | `render_context(blocks, *, block_formatter, provider_formatter, context_limit=None, count_tokens=None, event_format=None, event_format_resolver=None, model_context_window=None, reserved_output_tokens=None) -> RenderResult` | "Render resolved blocks into provider-specific output with utilization stats." |
| `format_message_content` | `renderer.py` | `format_message_content(block: ResolvedBlock, format_type: str) -> str` | "Wrap an event block's content with a role tag and metadata." |
| `BlockFormatter`, `XMLBlockFormatter`, `MarkdownBlockFormatter` | `formatter.py` | (see formatter module) | Block-level formatter hierarchy. |
| `ProviderFormatter`, `OpenAIProviderFormatter`, `AnthropicProviderFormatter`, `ResponsesProviderFormatter` | `formatter.py` | (see formatter module) | Provider-shape adapter hierarchy. |
| `FormatType`, `FORMAT_XML`, `FORMAT_MARKDOWN` | `formatter.py` | enums/constants | Format type tagging. |
| `RenderConfig` | `render_config.py` | (see render_config module) | Render configuration. |
| `EventBase`, `Metadata`, `Event`, `EventStatus`, `ResultStatus`, `UserEvent`, `AssistantEvent`, `ToolCallEvent`, `ToolResult` | `events.py` | (see events module) | Typed event models for conversation history. |
| `BlockError`, `BlockSyntaxError`, `DynamicNotResolvedError`, `ProtectedBlockError` | `exceptions.py` | (see exceptions module) | Block-related exceptions. |

Note: there is no factory function named `context` in this module — the public surface is the `Context` class only.

## Behavioral notes

### `DynamicContext`

- **Accepted argument types.** `DynamicContext` accepts a single positional `expr: str`. There is no overload for callables, objects, or pre-resolved values — the constructor only validates that `expr` is a string that `compile(..., "eval")` can parse. Calling `DynamicContext(callable)` would raise a `BlockSyntaxError` (or a `TypeError` from `compile`/Pydantic coercion) at construction time. `UNVERIFIED — requires B-lane` for what happens if a non-string object with a `__str__` that returns valid Python is passed.
- **Validation timing.** The expression is **validated eagerly at construction time** (via `compile(expr, "<block_expr>", "eval")`), not lazily. Invalid Python raises `BlockSyntaxError` immediately. The model itself is `frozen=True`, so the `expr` cannot be mutated after construction.
- **Evaluation timing.** The expression itself is **not evaluated at construction** or at render time inside this library — `render_context` operates on `ResolvedBlock`s whose `content` is already a fully-evaluated string. Evaluation happens elsewhere in the runtime (specifically during `_prepare_context()`, as called out in `scoped.py`'s comment and in `DynamicNotResolvedError`'s docstring). UNVERIFIED — requires B-lane: the exact call site that calls `eval(expr, ...)` and the namespace in which the expression runs.
- **`__call__`.** `DynamicContext` does **not** define `__call__`. It is a marker/wrapper Pydantic model — calling an instance returns `TypeError: 'DynamicContext' object is not callable`. To obtain the evaluated value, the runtime must read `DynamicContext.expr` and evaluate it; attempting to access the resolved value before that happens raises `DynamicNotResolvedError`.
- **Scoping interaction.** `DynamicContext` participates in `ScopedContext` overrides: a `ScopedContext` `with` block can replace a context key with a new `DynamicContext("expr")` (or a static `str`, or `None` to suppress). Scoped overrides are propagated via `contextvars.ContextVar` (`_scoped_blocks_var`) so nested `ScopedContext`s inherit + merge; the runtime reads the merged dict during `_prepare_context()` to decide which expression to evaluate. UNVERIFIED — requires B-lane: whether nested scopes can override a dynamic block with a static `str` (the type union `str | DynamicContext | None` permits it on the type level).
- **Interaction with surrounding `Context`.** `Context` is the *richer* sibling that holds `value` (literal) OR `expr` (re-evaluated), plus `prefix`. `Context.to_dynamic_context()` converts an expression-bearing `Context` into a `DynamicContext` (or returns `None` for static blocks). So `DynamicContext` is the lightweight, expression-only marker used by `set_dynamic(...)` and by `ScopedContext` overrides; `Context` is the full block descriptor with placement metadata.
- **Access to agent instance state.** Because `DynamicContext` only stores a string expression like `"self._envelope_schema()"`, and because the exception docstring for `DynamicNotResolvedError` states expressions are evaluated "at the start of each LLM turn," the expression **must** be evaluated against an agent-instance namespace for `self.X` to resolve. UNVERIFIED — requires B-lane: the exact eval namespace (whether it is the agent instance, a curated namespace, a `ContextApi`, or some combination). The `Context` docstring examples `self.format_status()`, `self.todo.show_active()`, `doc(self.shell)`, `self.done`, `self.total` all imply expressions run with `self` bound to the agent and `doc(...)` available as a builtin — but this is inferred from docstrings, not verified in this file.
- **Frozen / hashability.** `model_config = ConfigDict(frozen=True)` — instances are immutable and hashable. Useful as dict keys / set members.

### `Context` (sibling, for contrast)

- Two-axis model: `value` (literal text) vs. `expr` (re-evaluated each turn) on the content axis; `prefix` (cacheable prefix partition) vs. suffix (volatile) on the placement axis. Both `value` and `expr` cannot be set simultaneously — `TypeError` at construction. Setting neither also raises `TypeError`. Like `DynamicContext`, `Context` validates `expr` via `compile(..., "eval")` at construction time and raises `BlockSyntaxError` on bad Python.
- `Context.is_dynamic` returns `True` iff `expr is not None`. `Context.to_dynamic_context()` wraps `expr` in a `DynamicContext`, or returns `None` for static blocks. UNVERIFIED — requires B-lane: whether `Context` with `prefix=True` and `expr` is treated as cacheable across turns (the docstring asserts prefix placement is about caching; whether the dynamic expr is re-evaluated or memoized when prefix-cached is not addressed here).
- `model_config = ConfigDict(frozen=True)`.

### `render_context` (renderer perspective)

- Operates on `list[ResolvedBlock]` — i.e., **already-resolved** blocks. It does **not** see `DynamicContext` objects directly; by the time rendering runs, `DynamicContext.expr` has been evaluated and its result is sitting in a `ResolvedBlock.content` field. So the renderer's only awareness of dynamic-ness is via `ResolvedBlock.metadata.source_dynamic` (which the runtime sets when the block came from `self.context.set_dynamic()`) — and per `BlockMetadata`'s docstring, "only these blocks render their `expr` attribute" (i.e., the metadata records provenance, but the renderer does not re-evaluate).
- Pipeline: partition by role → pre-serialize non-tool events → apply total-context eviction (mark over-budget system blocks as `EVICTED:`) → invoke `block_formatter.format(...)` → invoke `provider_formatter.format(...)` → return `RenderResult(output, stats, messages)`.
- Never mutates input — eviction produces new `ResolvedBlock` instances via `model_copy(update={...})`.
- The renderer enforces no `max_string`/`max_length`/`max_depth` head/tail truncation (per the docstring: "Per-block head/tail truncation has been removed — content passes through verbatim"). Those bounds live on `event_format` / `event_format_resolver` and apply only to event-field rendering inside `block_formatter.format_event`.

### `ScopedContext` (cross-cutting)

- Two-arg ctor: `context` (dict of block overrides) and `events` (`EventQuery`).
- Override semantics inside `with`: `dict` blocks are merged into the parent's scope (child wins on key collision); `events` is the most-specific (child overrides parent) — not merged.
- Stored in `contextvars.ContextVar`s so nested scopes inherit naturally across `await` boundaries.
- Reads happen in the runtime's `_prepare_context()` (referenced by name in `scoped.py`'s comment but not defined in this leaf library). UNVERIFIED — requires B-lane: the exact read site and merge rules for `_prepare_context`.

## Cross-references

- `strategies.md` — strategies wrap context blocks before rendering; specifically, `PredictStrategy` (and other strategies in the `runtime/strategies/` tree) call `render_context(...)` after `_prepare_context()` has resolved any `DynamicContext` blocks into `ResolvedBlock`s with concrete `content`.
- `agentdoc.md` — agent docstrings annotate context blocks (e.g., `Context("self._envelope_schema()")` or `DynamicContext("self._envelope_schema()")`) with their semantics: which keys are cacheable prefixes, which are volatile suffixes, which are expression-driven dynamic blocks re-evaluated each turn.

## Suggested remediation

- The `DynamicContext` class carries no `__call__` / no `__eval__` hook — it is purely a marker. Any caller (like our `harness` code path that does `DynamicContext("self._envelope_schema()")`) must rely on the runtime to evaluate `expr` later. There is no in-module way to test or preview what a dynamic block will produce without invoking the runtime. Consider exposing a small `preview(expr, namespace)` helper for unit tests.
- `DynamicContext.__init__` does `compile(expr, "<block_expr>", "eval")` but does **not** AST-validate that the expression references names that exist in the agent namespace (e.g., `self._envelope_schema`). A typo'd method name will only fail at first LLM turn — late and noisy. Consider adding a `validate=True` mode that takes a `namespace` arg and dry-evaluates against it.
- `Context.to_dynamic_context()` silently drops the `prefix` flag — converting a `Context(expr=..., prefix=True)` to `DynamicContext(expr)` loses the cacheable-prefix placement. If both objects are used interchangeably, this round-trip drops information.
- `ScopedContext` accepts `context: dict[str, str | DynamicContext | None]` but the merging logic in `__enter__` is a shallow `dict.update`. If the same key is set in parent and child to different `DynamicContext` objects, the child replaces wholesale (no composition of expressions). This may be intentional but is undocumented.
- The docstrings of `DynamicContext`, `Context`, and `DynamicNotResolvedError` strongly imply expressions are evaluated against an agent instance (`self.format_status()`, `self._envelope_schema()`), but this is **not encoded in the type system**. A consumer could pass a `DynamicContext` whose expression references `self.foo` outside an agent context and discover the failure only at first LLM turn. The contract lives only in docs.
- `render_context` does not consume `DynamicContext` directly; consumers wiring up the runtime must ensure `_prepare_context()` runs *before* `render_context()`, otherwise dynamic blocks render as empty/missing. This sequencing is not encoded anywhere in this leaf library — it is a runtime-internal invariant. Worth a runtime-side assertion or a `ResolvedBlock`-only type guard.
