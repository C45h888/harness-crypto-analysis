# nooa agentdoc — annotations + spec/hidden

`nooa.agentdoc` is the runtime introspection layer that turns a Python class
into a prompt-ready **API contract**. The two-step mental model is
`spec()` (declare rendering rules) followed by `doc()` (render the contract).

## Source paths read

- `/tmp/nooa-monorepo/src/nooa/agentdoc/__init__.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/core.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/doc_config.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/ext.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/format.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/introspect.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/protocols.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/registry.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/visibility.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/_docs.py` (`spec` defined here)
- `/tmp/nooa-monorepo/src/nooa/agentdoc/_visibility.py` (`hidden` defined here)
- `/tmp/nooa-monorepo/src/nooa/agentdoc/_metadata.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/_info.py` (`FieldInfo`, `TypeInfo`, …)

## Public exports

`__all__` of `nooa.agentdoc` plus the names re-exported through
`nooa.agentdoc.introspect` / `nooa.agentdoc.visibility` / `nooa.agentdoc.ext`:

| Name | Defined at | Constructor signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `spec` | `_docs.py::Spec` (singleton) | `Spec() -> spec` | `The ``spec`` singleton — step 1: specify how a type renders.` |
| `SpecAnnotation` | `_docs.py::SpecAnnotation` | `SpecAnnotation(**kwargs)` | `A specification returned by ``spec(**kwargs)`` when no positional target is given.` |
| `hidden` | `_visibility.py::_Hidden` (singleton) | `_Hidden() -> hidden` | `Exclude fields, methods, or imports from documentation.` |
| `doc` | `core.py::doc` | `doc(*objs, concise=False, inline_depth=None) -> str` | `Get the documentation for one or more objects — step 2: render the API contract.` |
| `DocConfig` | `doc_config.py::DocConfig` | `@spec(expand=False) class DocConfig(BaseModel)` | `Configuration for documentation generation.` |
| `pformat` | `__init__.py::pformat` | `pformat(obj, *, console=None, indent_guides=True, max_length=None, max_string=None, max_depth=None, expand_all=False, concise=False, instance_mode="repr", unquote_strings=False) -> str` | `Format an object as a string with smart truncation.` |
| `pprint` | `__init__.py::pprint` | `pprint(obj, *, ...) -> None` | `Pretty-print an object with smart truncation. Prints to stdout.` |
| `truncating_pformat` | `__init__.py::truncating_pformat` | `truncating_pformat(obj, *, max_chars=None, **kwargs) -> str` | `Format *obj* as a string. Strings pass through verbatim; non-strings go through :func:`pformat` with the supplied structural kwargs.` |
| `FileBackedTruncatingStringIO` | `_truncating_stream.py` | `FileBackedTruncatingStringIO(...)` | `<truncated>` (re-exported from `_truncating_stream.py`) |
| `TruncatingStringIO` | `_truncating_stream.py` | `TruncatingStringIO(limit=...)` | `<truncated>` (re-exported from `_truncating_stream.py`) |
| `methods` | `core.py::methods` (re-exported by `introspect`) | `methods(obj, detail="summary", config=None) -> str` | `List methods with signatures — lower-level alternative to ``doc()``.` |
| `variables` | `core.py::variables` (re-exported by `introspect`) | `variables(obj, config=None) -> str` | `List non-callable attributes with current values — lower-level alternative to ``pformat()``.` |
| `TypeInfo` | `_info.py::TypeInfo` | `TypeInfo(name, base, fields, methods, docstring)` | `Information about a class or type.` |
| `FieldInfo` | `_info.py::FieldInfo` | `FieldInfo(name, type, default=REQUIRED, description=None, repr=True)` | `Information about a class field/attribute.` |
| `CallableInfo` | `_info.py::CallableInfo` | `CallableInfo(name, signature, return_type, docstring, is_async=False, is_classmethod=False)` | `Information about a function or method.` |
| `ModuleInfo` | `_info.py::ModuleInfo` | `ModuleInfo(name, docstring, functions, classes=[], values=[], ordered_names=[], submodules=[])` | `<truncated>` |
| `REQUIRED` | `_info.py` | `REQUIRED = ...` (sentinel) | (no docstring) |
| `extract_type_info` | `_structured.py::extract_type_info` | (re-exported by `ext`) | (lower-level; see `_structured.py`) |
| `extract_callable_info` | `_structured.py::extract_callable_info` | (re-exported by `ext`) | (lower-level; see `_structured.py`) |
| `extract_module_info` | `_structured.py::extract_module_info` | (re-exported by `ext`) | (lower-level; see `_structured.py`) |
| `format_type` | `_structured.py::format_type` | (re-exported by `ext`) | (lower-level; see `_structured.py`) |
| `register_type_info_extractor` | `registry.py::register_type_info_extractor` | `register_type_info_extractor(target_type) -> Callable[[…], …]` | `Decorator to register a TypeInfo extractor for a type.` |
| `get_type_info_extractor` | `registry.py::get_type_info_extractor` | `get_type_info_extractor(obj) -> Optional[Callable]` | `Get the registered TypeInfo extractor for an object's type.` |
| `unregister_type_info_extractor` | `registry.py::unregister_type_info_extractor` | `unregister_type_info_extractor(target_type) -> None` | `Remove a type from the extractor registry. Mainly useful for testing.` |
| `clear_registry` | `registry.py::clear_registry` | `clear_registry() -> None` | `Clear all registered extractors. Mainly useful for testing.` |
| `register_module_info_extractor` | `registry.py::register_module_info_extractor` | `register_module_info_extractor(target_module) -> Callable` | `Decorator to register a ModuleInfo extractor for a module.` |
| `get_module_info_extractor` | `registry.py::get_module_info_extractor` | `get_module_info_extractor(module) -> Optional[Callable]` | `Get the registered ModuleInfo extractor for a module.` |
| `unregister_module_info_extractor` | `registry.py::unregister_module_info_extractor` | `unregister_module_info_extractor(target_module) -> None` | `Remove a module from the extractor registry. Mainly useful for testing.` |
| `has_type_info` | `ext.py::has_type_info` | `has_type_info(obj) -> bool` | `Return True if obj implements the __type_info__ protocol.` |
| `SupportsTypeInfo` | `protocols.py::SupportsTypeInfo` | `@runtime_checkable Protocol` | `Protocol for types that provide custom type info extraction.` |
| `SupportsCallableInfo` | `protocols.py::SupportsCallableInfo` | `@runtime_checkable Protocol` | `Protocol for callables that provide custom callable info extraction.` |
| `SupportsInstanceValues` | `protocols.py::SupportsInstanceValues` | `@runtime_checkable Protocol` | `Protocol for instances that control their value extraction.` |
| `filter_module_globals` | `_visibility.py::filter_module_globals` | `filter_module_globals(module) -> dict[str, Any]` | `Return the module's globals filtered to names visible in documentation.` |
| `filter_mro_module_globals` | `_visibility.py::filter_mro_module_globals` | `filter_mro_module_globals(agent_class) -> dict[str, Any]` | `Merge :func:`filter_module_globals` across the agent class MRO.` |
| `is_hidden_field` | `_visibility.py::is_hidden_field` | `is_hidden_field(cls, name) -> bool` | `Check if a field is hidden via Annotated[T, hidden] or spec() imperative annotation.` |
| `is_hidden_method` | `_visibility.py::is_hidden_method` | `is_hidden_method(func) -> bool` | `Check if a method is marked with @hidden.` |
| `is_hidden_module_variable` | `_visibility.py::is_hidden_module_variable` | `is_hidden_module_variable(module, name) -> bool` | `Check if a module-level variable is annotated with Annotated[T, hidden].` |
| `iter_agent_mro_modules` | `_visibility.py::iter_agent_mro_modules` | `iter_agent_mro_modules(agent_class) -> list[ModuleType]` | `Ordered (base -> leaf) list of distinct user-defined modules across the MRO.` |

`Context` is referenced by the task brief but is NOT a public export of
`nooa.agentdoc`. It lives in `nooa.context_blocks` — see cross-references
below.

## Behavioral notes

### `spec`

`spec` is a **singleton instance** of `Spec` (defined at the bottom of
`_docs.py` as `spec = Spec()`). Calling `spec(...)` returns a
`SpecAnnotation` that is meant to be embedded as `typing.Annotated`
metadata on a field, OR used as a decorator `@spec(...)` on a method/class,
OR applied imperatively `spec(MyClass, "field", hidden=True)` for
third-party types.

Keyword arguments supported by `Spec.__call__`:

- `hidden: bool | None` — `True` strips the field/method from documentation,
  `False` opts a `_`-prefixed name back in, `None` is a no-op.
- `description: str | None` — rendered as an inline `#` comment in `doc()`
  output for the field/type.
- `expand: bool | None` — `False` collapses a sub-type to a one-liner
  (`ClassName()`) instead of expanding it inline. Used via `@spec(expand=False)`
  on a class — `DocConfig` itself is decorated this way.
- `max_length`, `max_string`, `max_depth` — parameter-annotation overrides
  for `pformat`. `max_length`/`max_depth` are *parameter annotations only*
  (not honored on class fields); `max_string` works on both parameter
  annotations and class fields.

When used as `Annotated[T, spec(description="Display name")]`, the marker
is **attached as `Annotated` metadata** on the field. At `doc()` render
time the extractor walks `typing.get_args(hint)` looking for both
`SpecAnnotation` instances and `hidden` instances (see
`_visibility.py::is_hidden_field` lines 175–185). Multiple
`Annotated` markers are merged: each `SpecAnnotation` contributes its own
`kwargs` dict, and `hidden` contributes the `_agentdoc_hidden = True`
flag. Class-level imperative `spec(MyClass, "field", ...)` calls are
stored on `_agentdoc_fields_docs` (see `_metadata.py`) and walk the MRO
leaf→base with most-derived-wins semantics.

For prompt generation this means: the agent's signature description is
populated from each visible field's `description`, in declaration order.
Hidden fields drop out entirely — they contribute to the runtime object
but are absent from the LLM-visible contract rendered by `doc(MyAgent)`.

UNVERIFIED — requires B-lane: the exact rendering path that joins
multiple `Annotated` markers when more than one `SpecAnnotation` is
attached to the same field (e.g.
`Annotated[str, spec(description="X"), spec(hidden=True)]`) — confirmed
in code for hidden detection but not exercised for description merging.

### `hidden`

`hidden` is a **singleton instance** of `_Hidden` (defined at the bottom
of `_visibility.py` as `hidden = _Hidden()`). It serves three roles:

1. **Decorator**: `@hidden` on a method or module-level function. Sets
   `func._agentdoc_hidden = True`. For descriptors like `@property` /
   `@cached_property`, the marker is stored on `property.fget` /
   `cached_property.func` because those wrappers have no `__dict__`.
2. **Annotation marker**: `Annotated[T, hidden]` on variables / class
   fields. Detection goes through `is_hidden_field` /
   `is_hidden_module_variable` which walk
   `typing.get_args(typing.get_origin(hint) is Annotated)`.
3. **Context manager**: `with hidden:` at module level. Snapshots
   `module.__dict__.keys()` on `__enter__`, diffs on `__exit__`, and adds
   new names to `module._agentdoc_hidden_names`. `__enter__` validates
   that it runs at module level (frame check) and raises `RuntimeError`
   otherwise.

Filtering that strips hidden members from the LLM-visible context
happens in `_visibility.py::filter_module_globals` /
`is_hidden_field` / `is_hidden_method`, which are called from
`core.py::doc`, `core.py::methods`, and `core.py::variables`. Members
stay fully alive at runtime — they remain bound on the object / module
and accessible via Python attribute lookup; only the documentation /
prompt contract excludes them.

UNVERIFIED — requires B-lane: whether `Context`/`DynamicContext` consumers
re-derive the hidden set from agentdoc helpers or carry their own copy;
this affects whether a hidden field can still leak through a context-block
payload.

### `Annotated`

`typing.Annotated[T, ...metadata...]` is interpreted by the
introspection layer in two ways:

- The metadata is **stripped** when the type is rendered (`format_type`
  walks to the origin and drops the metadata arg) — only the inner `T`
  is shown in the prompt.
- The metadata **arguments** are inspected at extraction time. Each
  argument is matched against `hidden` (identity check `arg is hidden`)
  or against `isinstance(arg, SpecAnnotation)`. This is the contract
  behind `Annotated[..., spec(description=...)]` and
  `Annotated[..., hidden]`.

`from __future__ import annotations` (used in `_docs.py`,
`_visibility.py`, `_metadata.py`, `_introspect.py`) stores annotations
as strings, so resolution goes through `typing.get_type_hints(klass,
include_extras=True)`. When full resolution fails (e.g. parent
annotations reference types not importable in the subclass module),
`_visibility.py::_resolve_single_annotation` falls back to `eval` of the
single field's raw annotation in the declaring class's namespace.

UNVERIFIED — requires B-lane: behavior when `Annotated` is used on
parameters of dynamically generated callables (e.g. agent tools) versus
plain class fields — the contract is documented but the runtime split
between `_structured.py` (extractor) and `_visibility.py` (filter) was
not exercised end-to-end here.

## Cross-references

- `context-blocks.md` — `DynamicContext` and `Context` consume the agentdoc
  contract produced by `doc()` / `pformat()`. Hidden fields are filtered
  out via the same `is_hidden_field` / `filter_module_globals` helpers,
  so a `Context` payload matches what the LLM sees in `doc()`.
- `strategies.md` — `PredictStrategy` / `CodeActStrategy` consult
  agentdoc metadata when building the prompt: `description` becomes the
  signature comment, `expand=False` decides whether referenced types are
  inlined or collapsed, and hidden members are absent from the
  exec_globals exposed to `CodeActStrategy.execute_python`.

## Suggested remediation

- `Spec.__call__`'s docstring is long (~1.7 KB) and contains the internal
  `_SENTINEL` placeholder exposed via `Annotated[Any, ...]` on every
  parameter; rendering `doc(spec)` will show every param with its
  `Annotated` description. Consider splitting the public-facing summary
  from the implementation-detail paragraph.
- `_Hidden.__call__` stores `_agentdoc_hidden = True` on the wrapped
  object via plain `setattr`; classes with `__slots__` that don't include
  `_agentdoc_hidden` will `AttributeError`. The current
  `contextlib.suppress(AttributeError, TypeError)` path in
  `set_docs_metadata` masks this, but `is_hidden_method` then reads
  `False` from the missing attribute — a silent leak. Worth either
  raising loudly or fixing the class layout.
- `_visibility.py::is_hidden_field` uses `eval(raw, ns)` on string
  annotations (lines 200–205 and 219–222). It is gated by `# noqa: S307`
  but is still an arbitrary-code-execution surface if the annotation
  string ever contains attacker-controlled content. Consider restricting
  `ns` to `__builtins__` + safe dunders.
- Multiple `Annotated` markers on the same field are detected for
  `hidden` but not yet exercised for `description` merging; the public
  contract is unclear. Either document the merge order or pin it with a
  test before B-lane integrates it.
- `Context` was named in the task brief as if it were a public agentdoc
  export; it is not. Confirm whether the task intended
  `nooa.context_blocks.Context` (separate package) — if so, the
  cross-reference should point there explicitly.
