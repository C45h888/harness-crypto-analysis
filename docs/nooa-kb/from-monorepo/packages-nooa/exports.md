# nooa package — public exports inventory

This document enumerates every name re-exported from the top-level `nooa`
package (i.e. reachable as `nooa.<name>` or `from nooa import <name>`), plus
the public classes/functions defined in the most user-facing modules under
`src/nooa/`. Internal helpers (prefixed with `_`) and submodule internals
are intentionally excluded.

## Source paths read

- `/tmp/nooa-monorepo/src/nooa/__init__.py`
- `/tmp/nooa-monorepo/src/nooa/_logging.py`
- `/tmp/nooa-monorepo/src/nooa/_visible.py`
- `/tmp/nooa-monorepo/src/nooa/agent.py`
- `/tmp/nooa-monorepo/src/nooa/decorators.py`
- `/tmp/nooa-monorepo/src/nooa/metaclass.py`
- `/tmp/nooa-monorepo/src/nooa/prompts.py`
- `/tmp/nooa-monorepo/src/nooa/skill.py`
- `/tmp/nooa-monorepo/src/nooa/library_manager.py`
- `/tmp/nooa-monorepo/src/nooa/media.py`
- `/tmp/nooa-monorepo/src/nooa/errors/__init__.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/__init__.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/_docs.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/visibility.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/ext.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/core.py`
- `/tmp/nooa-monorepo/src/nooa/agentdoc/doc_config.py`
- `/tmp/nooa-monorepo/src/nooa/context_blocks/__init__.py`
- `/tmp/nooa-monorepo/src/nooa/runtime/channels.py`
- `/tmp/nooa-monorepo/src/nooa/runtime/context.py`
- `/tmp/nooa-monorepo/src/nooa/runtime/context_manager.py`
- `/tmp/nooa-monorepo/src/nooa/runtime/event_query.py`
- `/tmp/nooa-monorepo/src/nooa/runtime/events.py`
- `/tmp/nooa-monorepo/src/nooa/skill_registry.py`
- `/tmp/nooa-monorepo/src/nooa/storage/__init__.py`
- `/tmp/nooa-monorepo/src/nooa/storage/manager.py`
- `/tmp/nooa-monorepo/src/nooa/storage/markers.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/__init__.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/base.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/codeact.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/codeact_lite.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/composite.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/current_call.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/predict.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/prefill.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/pure_python.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/reflexion.py`
- `/tmp/nooa-monorepo/src/nooa/strategies/template.py`
- `/tmp/nooa-monorepo/src/nooa/strategy_validation.py`
- `/tmp/nooa-monorepo/src/nooa/token_counter.py`
- `/tmp/nooa-monorepo/src/nooa/unifiedllm/__init__.py`

## Public exports

### `nooa` (top-level re-exports — `__init__.py`)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `__version__` | `_version.py` (re-exported in `__init__.py:12`) | module string | `__version__` from package metadata |
| `llm_config_chain` | `llm_config.py` (lazy re-export via `__init__.py:89-93`) | lazy import; helper callable | lazy — no docstring at import site |
| `Agent` | `agent.py:74` | `class Agent(metaclass=AgentMeta)` | `<truncated>` — long prompt-shaped system-prompt docstring describing XML context blocks and truncation markers |
| `AgentMeta` | `metaclass.py:25` | `class AgentMeta(ABCMeta)` | `Generic metaclass for auto-wrapping ellipsis methods and tracing helpers.` |
| `no_trace` | `metaclass.py:244` | `def no_trace(func)` | `Decorator to opt-out of tracing for public methods.` |
| `strategy` | `decorators.py:25` | `def strategy(strategy_instance=None, context=None, *, llm=None, truncation=None)` | `Strategy decorator for agent methods.` (full docstring describes `@strategy` usage, `ScopedContext` vs `dict`, ellipsis detection) |
| `Skill` | `skill.py:255` | `class Skill` | `Base class for agent skills.` (full docstring wraps Python objects / inline content for LLM discovery) |
| `TextSkill` | `skill.py:350` | `class TextSkill(Skill)` | `Skill loaded from a SKILL.md directory. Has id, description, run_script, read_file.` |
| `slash_command` | `skill.py:41` | `def slash_command(name, *, argument_hint=None, completions=(), output_to_agent=True)` | `Mark a Skill method as a user-invocable slash command.` |
| `get_slash_commands` | `skill.py:88` | `def get_slash_commands(skill) -> list[tuple[SlashCommandMeta, Any]]` | `Extract all @slash_command methods from a Skill instance.` |
| `skill_from_module` | `skill_registry.py:39` | `def skill_from_module(module, module_name, source="") -> "Skill | None"` | `Extract a ``Skill`` instance from an already-imported module.` |
| `LibraryManager` | `library_manager.py:17` | `class LibraryManager` | `Scans a libs directory and attaches each Python package as an agent attribute.` |
| `StorageManager` | `storage/manager.py:25` | `class StorageManager(Protocol)` (runtime-checkable) | `Unified storage interface for agent persistence.` |
| `Media` | `media.py:18` | `class Media` | `Base class for all media attachments.` |
| `Image` | `media.py:114` | `class Image(Media)` | `An image that can be shown to a vision-capable LLM via show().` |
| `Audio` | `media.py:133` | `class Audio(Media)` | `Audio that can be shown to an audio-capable LLM via show().` |
| `Video` | `media.py:152` | `class Video(Media)` | `Video that can be shown to a video-capable LLM via show().` |
| `File` | `media.py:184` | `class File(Media)` | `A file (PDF, etc.) that can be shown to an LLM via show().` |
| `Context` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class (dataclass) | re-exported from `nooa.context_blocks` |
| `DynamicContext` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class (dataclass) | re-exported from `nooa.context_blocks` (deprecated, use `Context`) |
| `ContextWindowStats` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class (dataclass) | re-exported from `nooa.context_blocks` |
| `EventQuery` | `runtime/event_query.py:16` | `@dataclass(frozen=True) class EventQuery` | `Type-safe event filtering configuration.` |
| `ContextApi` | `runtime/context.py:25` | `class ContextApi(Skill)` | `Dict-like API for managing what appears in your system prompt.` |
| `ContextManager` | `runtime/context_manager.py:30` | `class ContextManager` | `Dict-like API for managing context blocks.` |
| `EventsApi` | `runtime/events.py:21` | `class EventsApi(Skill)` | `Query past events by type, tag, text, or call ID. Compact context by summarizing or collapsing events.` |
| `Channel` | `runtime/channels.py:181` | `class Channel[T]` | named queue/event channel — full docstring describes queue vs event modes (re-exported in `__init__.py:49`) |
| `QueueManager` | `runtime/channels.py:572` | `class QueueManager` | channel registry + race + status (re-exported in `__init__.py:49`) |
| `QueueOutput` | `runtime/channels.py:40` | `class QueueOutput(EventBase)` | `Event emitted by an event-mode channel's ``put()``.` |
| `LLMResponse` | `unifiedllm/unifiedllm.py:674` | `@dataclass class LLMResponse` | `Standardized response from any LLM API` |
| `GenerationStrategy` | `strategies/base.py:186` | `class GenerationStrategy(ABC, metaclass=AgentMeta)` | `Abstract base class for generation strategies with automatic method wrapping.` |
| `CodeActStrategy` | `strategies/codeact.py:270` | `class CodeActStrategy(CompositeStrategy)` | `CodeAct strategy: LLM uses execute_python tool + structured output.` |
| `CodeActLiteStrategy` | `strategies/codeact_lite.py:227` | `class CodeActLiteStrategy(CodeActStrategy)` (lazy/gated via `__init__.py:97-100`) | `Simplified CodeAct strategy with clean message rendering.` |
| `ReflexionStrategy` | `strategies/reflexion.py:69` | `class ReflexionStrategy(GenerationStrategy)` (lazy/gated via `__init__.py:97-100`) | `Reflexion strategy: generate → reflect → improve loop.` |
| `PredictStrategy` | `strategies/predict.py:47` | `class PredictStrategy(GenerationStrategy)` | `Single-shot LLM call that tries to solve the task in one go (no loop/iteration).` |
| `get_default_strategy` | `strategies/__init__.py:38` | `def get_default_strategy() -> GenerationStrategy` | `Get the default strategy for agents without an explicit strategy.` |
| `set_default_strategy` | `strategies/__init__.py:57` | `def set_default_strategy(strategy: GenerationStrategy | None) -> None` | `Set the default strategy for all agents in the current async context.` |
| `InspectInputsPrefill` | `strategies/prefill.py:104` | `class InspectInputsPrefill` | `Prefill that inspects input parameters using pprint().` |
| `InvariantError` | `strategy_validation.py:18` | `class InvariantError(ValueError)` | `A model-correctable method invariant failure.` |
| `MethodPrecondition` | `strategy_validation.py:14` | `MethodPrecondition = Callable[..., Any]` (type alias) | type alias for pre-call conditions |
| `MethodPostcondition` | `strategy_validation.py:15` | `MethodPostcondition = Callable[..., Any]` (type alias) | type alias for post-call conditions |
| `PromptData` | `prompts.py:23` | `@dataclass(frozen=True) class PromptData` | `All prompt sections for a single agent method call.` |
| `build_prompt_data` | `prompts.py:203` | `async def build_prompt_data(method, /, *args, **kwargs) -> PromptData` | `Build prompt data for *method* without printing.` |
| `print_prompt` | `prompts.py:241` | `async def print_prompt(method, /, *args, **kwargs) -> None` | `Print the prompts that would be sent to the LLM for *method*.` |
| `char_approximate_token_counter` | `token_counter.py:11` | `def char_approximate_token_counter(text: str) -> int` | `Approximate token count using character length divided by 4.` |
| `enable_logging` | `_logging.py:49` | `def enable_logging(level=logging.DEBUG, name="nooa", fmt=..., datefmt=..., stream=None) -> None` | `Attach a :class:`~logging.StreamHandler` to an *nooa* logger.` |
| `hidden` | `agentdoc/_visibility.py::_Hidden` (singleton; re-exported in `agentdoc/__init__.py`) | singleton (callable) | re-exported from `nooa.agentdoc` |
| `spec` | `agentdoc/_docs.py::Spec` (singleton; re-exported in `agentdoc/__init__.py`) | singleton | re-exported from `nooa.agentdoc` |
| `visible` | `_visible.py:13` (`_Visible` class) | `_Visible() -> visible` (no-op context manager) | `No-op context manager — everything is visible by default.` |

### Errors (`nooa.errors` — re-exported in `__init__.py:32-42`)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `NemoOOAgentsError` | `errors/__init__.py:21` | `class NemoOOAgentsError(Exception)` | `Base exception for all nooa errors.` |
| `GenerationError` | `errors/__init__.py:32` | `class GenerationError(NemoOOAgentsError)` | `Error during LLM code generation.` |
| `ValidationError` | `errors/__init__.py:49` | `class ValidationError(NemoOOAgentsError)` | `Error validating generated code.` |
| `RestrictedCodeError` | `errors/__init__.py:65` | `class RestrictedCodeError(ValidationError)` | `Generated code uses restricted/forbidden features.` (long — describes import/exec/lambda/dunder restrictions) |
| `NemoOOAgentsRuntimeError` | `errors/__init__.py:94` | `class NemoOOAgentsRuntimeError(NemoOOAgentsError)` | `Error in agent runtime system. Named to avoid shadowing Python's built-in RuntimeError.` |
| `DynamicMethodAdditionError` | `errors/__init__.py:103` | `class DynamicMethodAdditionError(AttributeError)` | `Attempt to attach a method-like callable to an agent instance or class.` |
| `SerializationError` | `errors/storage.py` (re-exported in `errors/__init__.py:115-120`) | `class SerializationError(NemoOOAgentsError)` | re-exported from `nooa.errors.storage` |
| `SnapshotNotFoundError` | `errors/storage.py` (re-exported in `errors/__init__.py:115-120`) | class | re-exported from `nooa.errors.storage` |
| `StorageNotConfiguredError` | `errors/storage.py` (re-exported in `errors/__init__.py:115-120`) | class | re-exported from `nooa.errors.storage` |

### `agent.py` (additional public surface on the class)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `Agent.__init__` | `agent.py:167` | `__init__(self, llm=INHERIT, *, truncation=None, render_config=None, context=None, event_query=None, storage=None)` | `Initialize agent with its own runtime.` (long — lists core attributes, cascading LLM, storage) |
| `Agent.__init_subclass__` | `agent.py:128` | `__init_subclass__(cls, llm=INHERIT, truncation=None, execution=None, context=None, event_query=None, **kwargs)` | `Configure agent class with metaclass.` (lists kwargs) |
| `Agent.agent_id` | `agent.py:402` | `@property -> str` | `Agent ID.` |
| `Agent.context_stats` | `agent.py:408` | `@property -> "ContextWindowStats | None"` | `Most recent context window utilization stats, or None before first generation.` |
| `Agent.llm` | `agent.py:414` | `@property -> "UnifiedLLM"` | `The agent's resolved LLM client.` |
| `Agent.set_llm` | `agent.py:425` | `set_llm(llm: "UnifiedLLM") -> None` | `Replace the agent's LLM client.` |
| `Agent.__type_info__` | `agent.py:491` | `classmethod __type_info__(cls) -> "TypeInfo"` | `Return TypeInfo for this Agent class, filtering framework internals.` |
| `Agent.__instance_values__` | `agent.py:568` | `__instance_values__(self) -> dict[str, Any]` | `Return instance values, filtering framework attributes.` |

### `decorators.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `strategy` | `decorators.py:25` | (see top-level table) | (see top-level table) |

### `metaclass.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `AgentMeta` | `metaclass.py:25` | `class AgentMeta(ABCMeta)` | `Generic metaclass for auto-wrapping ellipsis methods and tracing helpers.` |
| `AgentMeta.__new__` | `metaclass.py:49` | `__new__(mcs, name, bases, namespace, **kwargs) -> type` | `Create new class with auto-wrapped methods.` |
| `no_trace` | `metaclass.py:244` | `def no_trace(func)` | `Decorator to opt-out of tracing for public methods.` |

### `prompts.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `PromptData` | `prompts.py:23` | `@dataclass(frozen=True) class PromptData` | `All prompt sections for a single agent method call.` |
| `build_prompt_data` | `prompts.py:203` | `async def build_prompt_data(method, /, *args, **kwargs) -> PromptData` | `Build prompt data for *method* without printing.` |
| `print_prompt` | `prompts.py:241` | `async def print_prompt(method, /, *args, **kwargs) -> None` | `Print the prompts that would be sent to the LLM for *method*.` |

### `skill.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `Skill` | `skill.py:255` | `class Skill` | `Base class for agent skills.` |
| `Skill.attach` | `skill.py:305` | `attach(self, agent: Any) -> None` | `Called when this skill is installed on an agent.` |
| `Skill.detach` | `skill.py:312` | `detach(self) -> None` | `Called when this skill is removed from an agent.` |
| `Skill.source_dir` | `skill.py:326` | `@property -> Path | None` | `Directory containing this skill's source code, or None if unknown.` |
| `TextSkill` | `skill.py:350` | `class TextSkill(Skill)` | `Skill loaded from a SKILL.md directory. Has id, description, run_script, read_file.` |
| `TextSkill.id` | `skill.py:363` | `@property -> str` | `<truncated>` — returns `type(self)._id` |
| `TextSkill.source_dir` | `skill.py:367` | `@property -> Path | None` | `<truncated>` — returns `self._skill_path.resolve()` |
| `TextSkill.description` | `skill.py:371` | `@property -> str` | `<truncated>` — first line of docstring |
| `TextSkill.run_script` | `skill.py:375` | `async def run_script(self, name, *args, interpreter=None, timeout=30.0) -> str` | `Run a script from this skill's scripts/ directory.` |
| `TextSkill.read_file` | `skill.py:423` | `def read_file(self, path: str) -> str` | `Read a file from anywhere within this skill's directory.` |
| `slash_command` | `skill.py:41` | (see top-level) | (see top-level) |
| `get_slash_commands` | `skill.py:88` | (see top-level) | (see top-level) |
| `SlashCommandMeta` | `skill.py:23` | `class SlashCommandMeta` | `Metadata attached to a method by @slash_command.` |

### `library_manager.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `LibraryManager` | `library_manager.py:17` | `class LibraryManager` | `Scans a libs directory and attaches each Python package as an agent attribute.` |
| `LibraryManager.install` | `library_manager.py:46` | `classmethod install(cls, agent, *, libs_dir: Path) -> "LibraryManager"` | `Scan libs_dir and attach all libraries to *agent*. Returns the manager.` |
| `LibraryManager.reload` | `library_manager.py:118` | `def reload(self) -> None` | `Reload all installed libraries from disk.` |
| `LibraryManager.discover` | `library_manager.py:127` | `staticmethod discover(path: Path) -> list[str]` | `Return sorted library names (directories with a pyproject.toml) under *path*.` |

### `media.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `Media` | `media.py:18` | `class Media` | `Base class for all media attachments.` |
| `Media.data_url` | `media.py:44` | `@property -> str` | `The data URL or regular URL for this media.` |
| `Media.media_type` | `media.py:49` | `@property -> str` | `MIME type (e.g. 'image/png', 'audio/wav', 'application/pdf').` |
| `Media.modality` | `media.py:54` | `@property -> str` | `Modality: 'image', 'audio', 'video', or 'file'.` |
| `Media.vendor_metadata` | `media.py:59` | `@property -> dict[str, object]` | `Provider-specific hints (e.g. ``{"detail": "high"}`` for OpenAI images).` |
| `Media.from_file` | `media.py:64` | `classmethod from_file(cls, path, **vendor_metadata) -> "Media"` | `Load media from a file path.` |
| `Media.from_bytes` | `media.py:73` | `classmethod from_bytes(cls, data, *, media_type, **vendor_metadata) -> "Media"` | `Create media from raw bytes with explicit media type.` |
| `Media.from_url` | `media.py:82` | `classmethod from_url(cls, url, *, media_type="", **vendor_metadata) -> "Media"` | `Create media from a URL (no download — URL is passed directly to the LLM).` |
| `Media.content_hash` | `media.py:93` | `@property -> str` | `Short SHA-256 hash (8 hex chars) identifying this media's content.` |
| `Media.size_bytes` | `media.py:98` | `@property -> int | None` | `Approximate size in bytes of the payload, or None for URL references.` |
| `Image` | `media.py:114` | `class Image(Media)` | `An image that can be shown to a vision-capable LLM via show().` |
| `Audio` | `media.py:133` | `class Audio(Media)` | `Audio that can be shown to an audio-capable LLM via show().` |
| `Video` | `media.py:152` | `class Video(Media)` | `Video that can be shown to a video-capable LLM via show().` |
| `File` | `media.py:184` | `class File(Media)` | `A file (PDF, etc.) that can be shown to an LLM via show().` |

### `strategy_validation.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `InvariantError` | `strategy_validation.py:18` | `class InvariantError(ValueError)` | `A model-correctable method invariant failure.` |
| `MethodPrecondition` | `strategy_validation.py:14` | `MethodPrecondition = Callable[..., Any]` | type alias |
| `MethodPostcondition` | `strategy_validation.py:15` | `MethodPostcondition = Callable[..., Any]` | type alias |
| `normalize_conditions` | `strategy_validation.py:28` | `def normalize_conditions(name, conditions) -> tuple[Callable, ...]` | `Normalize config conditions into a tuple of callables.` |
| `run_preconditions` | `strategy_validation.py:55` | `def run_preconditions(agent, call, preconditions) -> None` | `Run pre-call conditions for ``call``.` |
| `run_postconditions` | `strategy_validation.py:62` | `def run_postconditions(agent, result, call, postconditions) -> None` | `Run post-result conditions for ``result``.` |

### `token_counter.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `char_approximate_token_counter` | `token_counter.py:11` | `def char_approximate_token_counter(text: str) -> int` | `Approximate token count using character length divided by 4.` |

### `_logging.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `enable_logging` | `_logging.py:49` | (see top-level) | (see top-level) |

### `_visible.py`

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `visible` | `_visible.py:26` | instance of `_Visible` (no-op context manager) | `No-op context manager — everything is visible by default.` |

### `storage` (subpackage, re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `StorageManager` | `storage/manager.py:25` | `class StorageManager(Protocol)` | `Unified storage interface for agent persistence.` |
| `InMemoryStorageManager` | `storage/in_memory.py:13` | `class InMemoryStorageManager` | re-exported from `nooa.storage` |
| `SQLiteStorageManager` | `storage/sqlite.py:671` | `class SQLiteStorageManager` | re-exported from `nooa.storage` |
| `AgentSnapshot` | `storage/snapshot.py:40` | `class AgentSnapshot(BaseModel)` | re-exported from `nooa.storage` |
| `nosnapshot` | `storage/markers.py:36` | singleton of `_NoSnapshot` | re-exported from `nooa.storage.markers` |
| `snapshotable` | `storage/markers.py:119` | singleton of `_Snapshotable` | re-exported from `nooa.storage.markers` |
| `serialize` | `storage/serialization.py:72` | `def serialize(value) -> tuple[Any, set[str]]` | re-exported from `nooa.storage.serialization` |
| `deserialize` | `storage/serialization.py:88` | `def deserialize(blob, allowlist) -> Any` | re-exported from `nooa.storage.serialization` |
| `SKIP` | `storage/serialization.py:37` | singleton of `_Skip` | re-exported from `nooa.storage.serialization` |
| `snapshot_to_json` | `storage/json_snapshot.py:25` | `def snapshot_to_json(agent) -> dict[str, Any]` | re-exported from `nooa.storage.json_snapshot` |
| `snapshot_from_json` | `storage/json_snapshot.py:42` | `def snapshot_from_json(snapshot, agent) -> None` | re-exported from `nooa.storage.json_snapshot` |

### `agentdoc` (subpackage, re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `spec` | `agentdoc/_docs.py::Spec` (singleton) | callable singleton | `The ``spec`` singleton — step 1: specify how a type renders.` |
| `hidden` | `agentdoc/_visibility.py::_Hidden` (singleton) | callable singleton | `Exclude fields, methods, or imports from documentation.` |
| `doc` | `agentdoc/core.py:45` | `def doc(*objs, concise=False, inline_depth=None) -> str` | `Get the documentation for one or more objects — step 2: render the API contract.` |
| `DocConfig` | `agentdoc/doc_config.py:9` | `class DocConfig(BaseModel)` | `Configuration for documentation generation.` |
| `pformat` | `agentdoc/__init__.py:76` | `def pformat(obj, *, console=None, indent_guides=True, max_length=None, max_string=None, max_depth=None, expand_all=False, concise=False, instance_mode="repr", unquote_strings=False) -> str` | `Format an object as a string with smart truncation.` |
| `pprint` | `agentdoc/__init__.py:135` | `def pprint(obj, *, console=None, indent_guides=True, max_length=None, max_string=None, max_depth=None, concise=False, instance_mode="repr") -> None` | `Pretty-print an object with smart truncation. Prints to stdout.` |
| `truncating_pformat` | `agentdoc/__init__.py:42` | `def truncating_pformat(obj, *, max_chars=None, **kwargs) -> str` | `Format *obj* as a string. Strings pass through verbatim; non-strings go through :func:`pformat` with the supplied structural kwargs.` |
| `FileBackedTruncatingStringIO` | `agentdoc/_truncating_stream.py` (re-exported in `agentdoc/__init__.py`) | class | re-exported from `nooa.agentdoc` |
| `TruncatingStringIO` | `agentdoc/_truncating_stream.py` (re-exported in `agentdoc/__init__.py`) | class | re-exported from `nooa.agentdoc` |
| `SpecAnnotation` | `agentdoc/_docs.py:69` | `class SpecAnnotation` | `A specification returned by ``spec(**kwargs)`` when no positional target is given.` |
| `methods` | `agentdoc/core.py:201` | `def methods(obj, detail="summary", config=None) -> str` | `List methods with signatures — lower-level alternative to ``doc()``.` |
| `variables` | `agentdoc/core.py:292` | `def variables(obj, config=None) -> str` | `List non-callable attributes with current values — lower-level alternative to ``pformat()``.` |
| `TypeInfo` | `agentdoc/_info.py` (re-exported via `agentdoc/ext.py`) | class | re-exported from `nooa.agentdoc.ext` |
| `FieldInfo` | `agentdoc/_info.py` (re-exported via `agentdoc/ext.py`) | class | re-exported from `nooa.agentdoc.ext` |
| `CallableInfo` | `agentdoc/_info.py` (re-exported via `agentdoc/ext.py`) | class | re-exported from `nooa.agentdoc.ext` |
| `ModuleInfo` | `agentdoc/_info.py` (re-exported via `agentdoc/ext.py`) | class | re-exported from `nooa.agentdoc.ext` |
| `REQUIRED` | `agentdoc/_info.py` (re-exported via `agentdoc/ext.py`) | sentinel | re-exported from `nooa.agentdoc.ext` |
| `extract_type_info` | `agentdoc/_structured.py` (re-exported via `agentdoc/ext.py`) | function | re-exported from `nooa.agentdoc.ext` |
| `extract_callable_info` | `agentdoc/_structured.py` (re-exported via `agentdoc/ext.py`) | function | re-exported from `nooa.agentdoc.ext` |
| `extract_module_info` | `agentdoc/_structured.py` (re-exported via `agentdoc/ext.py`) | function | re-exported from `nooa.agentdoc.ext` |
| `format_type` | `agentdoc/_structured.py` (re-exported via `agentdoc/ext.py`) | function | re-exported from `nooa.agentdoc.ext` |
| `register_type_info_extractor` | `agentdoc/registry.py` (re-exported via `agentdoc/ext.py`) | function | re-exported from `nooa.agentdoc.ext` |
| `get_type_info_extractor` | `agentdoc/registry.py` (re-exported via `agentdoc/ext.py`) | function | re-exported from `nooa.agentdoc.ext` |
| `unregister_type_info_extractor` | `agentdoc/registry.py` (re-exported via `agentdoc/ext.py`) | function | re-exported from `nooa.agentdoc.ext` |
| `clear_registry` | `agentdoc/registry.py` (re-exported via `agentdoc/ext.py`) | function | re-exported from `nooa.agentdoc.ext` |
| `has_type_info` | `agentdoc/ext.py:58` | `def has_type_info(obj) -> bool` | `Return True if obj implements the __type_info__ protocol.` |
| `SupportsTypeInfo` | `agentdoc/protocols.py` (re-exported via `agentdoc/ext.py`) | Protocol | re-exported from `nooa.agentdoc.ext` |
| `SupportsCallableInfo` | `agentdoc/protocols.py` (re-exported via `agentdoc/ext.py`) | Protocol | re-exported from `nooa.agentdoc.ext` |
| `SupportsInstanceValues` | `agentdoc/protocols.py` (re-exported via `agentdoc/ext.py`) | Protocol | re-exported from `nooa.agentdoc.ext` |
| `filter_module_globals` | `agentdoc/_visibility.py` (re-exported via `agentdoc/visibility.py`) | function | re-exported from `nooa.agentdoc.visibility` |
| `filter_mro_module_globals` | `agentdoc/_visibility.py` (re-exported via `agentdoc/visibility.py`) | function | re-exported from `nooa.agentdoc.visibility` |
| `is_hidden_field` | `agentdoc/_visibility.py` (re-exported via `agentdoc/visibility.py`) | function | re-exported from `nooa.agentdoc.visibility` |
| `is_hidden_method` | `agentdoc/_visibility.py` (re-exported via `agentdoc/visibility.py`) | function | re-exported from `nooa.agentdoc.visibility` |
| `is_hidden_module_variable` | `agentdoc/_visibility.py` (re-exported via `agentdoc/visibility.py`) | function | re-exported from `nooa.agentdoc.visibility` |
| `iter_agent_mro_modules` | `agentdoc/_visibility.py` (re-exported via `agentdoc/visibility.py`) | function | re-exported from `nooa.agentdoc.visibility` |

### `context_blocks` (subpackage, re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `Context` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `DynamicContext` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `ResolvedBlock` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `Role` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `BlockMetadata` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `ContextWindowStats` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `RenderedMessage` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `ToolCallInfo` | `context_blocks/models.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `RenderConfig` | `context_blocks/render_config.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `ScopedContext` | `context_blocks/scoped.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `render_context` | `context_blocks/renderer.py` (re-exported in `context_blocks/__init__.py`) | function | re-exported from `nooa.context_blocks` |
| `RenderResult` | `context_blocks/renderer.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `format_message_content` | `context_blocks/renderer.py` (re-exported in `context_blocks/__init__.py`) | function | re-exported from `nooa.context_blocks` |
| `FormatType` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | enum | re-exported from `nooa.context_blocks` |
| `FORMAT_XML` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | constant | re-exported from `nooa.context_blocks` |
| `FORMAT_MARKDOWN` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | constant | re-exported from `nooa.context_blocks` |
| `BlockFormatter` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | class (abstract) | re-exported from `nooa.context_blocks` |
| `XMLBlockFormatter` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `MarkdownBlockFormatter` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `ProviderFormatter` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | class (abstract) | re-exported from `nooa.context_blocks` |
| `OpenAIProviderFormatter` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `AnthropicProviderFormatter` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `ResponsesProviderFormatter` | `context_blocks/formatter.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `EventBase` | `context_blocks/events.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `Metadata` | `context_blocks/events.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `Event` | `context_blocks/events.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `EventStatus` | `context_blocks/events.py` (re-exported in `context_blocks/__init__.py`) | enum | re-exported from `nooa.context_blocks` |
| `ResultStatus` | `context_blocks/events.py` (re-exported in `context_blocks/__init__.py`) | enum | re-exported from `nooa.context_blocks` |
| `UserEvent` | `context_blocks/events.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `AssistantEvent` | `context_blocks/events.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `ToolCallEvent` | `context_blocks/events.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `ToolResult` | `context_blocks/events.py` (re-exported in `context_blocks/__init__.py`) | class | re-exported from `nooa.context_blocks` |
| `BlockError` | `context_blocks/exceptions.py` (re-exported in `context_blocks/__init__.py`) | class (exception) | re-exported from `nooa.context_blocks` |
| `BlockSyntaxError` | `context_blocks/exceptions.py` (re-exported in `context_blocks/__init__.py`) | class (exception) | re-exported from `nooa.context_blocks` |
| `DynamicNotResolvedError` | `context_blocks/exceptions.py` (re-exported in `context_blocks/__init__.py`) | class (exception) | re-exported from `nooa.context_blocks` |
| `ProtectedBlockError` | `context_blocks/exceptions.py` (re-exported in `context_blocks/__init__.py`) | class (exception) | re-exported from `nooa.context_blocks` |

### `runtime.channels` (re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `Channel` | `runtime/channels.py:181` | `class Channel[T]` | re-exported from `nooa.runtime.channels` |
| `QueueManager` | `runtime/channels.py:572` | `class QueueManager` | re-exported from `nooa.runtime.channels` |
| `QueueOutput` | `runtime/channels.py:40` | `class QueueOutput(EventBase)` | re-exported from `nooa.runtime.channels` |

### `runtime.event_query` (re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `EventQuery` | `runtime/event_query.py:16` | `@dataclass(frozen=True) class EventQuery` | `Type-safe event filtering configuration.` |
| `EventQuery.current_call` | `runtime/event_query.py:53` | `classmethod current_call(cls, limit=None) -> Self` | `Filter to events from the current method call only.` |
| `EventQuery.by_type` | `runtime/event_query.py:70` | `classmethod by_type(cls, event_type, limit=None) -> Self` | `Filter to specific event type(s).` |
| `EventQuery.last_n` | `runtime/event_query.py:80` | `classmethod last_n(cls, n) -> Self` | `Get the last N events only.` |
| `EventQuery.apply` | `runtime/event_query.py:88` | `def apply(self, events, *, current_call_id=None) -> list[EventBase]` | `Apply this query to filter a list of events.` |

### `runtime.context` (re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `ContextApi` | `runtime/context.py:25` | `class ContextApi(Skill)` | `Dict-like API for managing what appears in your system prompt.` |

### `runtime.context_manager` (re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `ContextManager` | `runtime/context_manager.py:30` | `class ContextManager` | `Dict-like API for managing context blocks.` |

### `runtime.events` (re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `EventsApi` | `runtime/events.py:21` | `class EventsApi(Skill)` | `Query past events by type, tag, text, or call ID. Compact context by summarizing or collapsing events.` |

### `skill_registry` (re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `SkillRegistry` | `skill_registry.py:134` | `class SkillRegistry(Skill)` | `Manages skill discovery, loading, and activation.` |
| `skill_from_module` | `skill_registry.py:39` | `def skill_from_module(module, module_name, source="") -> "Skill | None"` | `Extract a ``Skill`` instance from an already-imported module.` |
| `SkillRegistry.discover_libs` | `skill_registry.py:196` | `def discover_libs(self, libs_path: "Path") -> None` | `Scan a libs directory and register each skill package.` |
| `SkillRegistry.discover_skills_dirs` | `skill_registry.py:278` | `def discover_skills_dirs(self, dirs: "list[Path]") -> None` | `Scan skills directories for TextSkills and Python skills.` |
| `SkillRegistry.discovered` | `skill_registry.py:183` | `def discovered(self) -> list[str]` | `All discovered skill names (category/name format).` |
| `SkillRegistry.entry` | `skill_registry.py:187` | `def entry(self, name) -> "_SkillEntry | None"` | `Return the discovery record for *name*, or ``None`` if unknown.` |
| `SkillRegistry.loaded` | `skill_registry.py:333` | `def loaded(self) -> list[str]` | `Currently loaded (attached) skill names.` |
| `SkillRegistry.load` | `skill_registry.py:337` | `def load(self, patterns: list[str]) -> None` | `Load skills matching patterns from the discovered set.` |
| `SkillRegistry.register` | `skill_registry.py:379` | `def register(self, name, skill_or_cls=None, /, **kwargs) -> None` | `Register a skill by name, assigning it as self.<leaf_name>.` |
| `SkillRegistry.activated` | `skill_registry.py:434` | `def activated(self) -> list[str]` | `Currently activated (LLM-visible) skill names.` |
| `SkillRegistry.activate` | `skill_registry.py:438` | `def activate(self, patterns: list[str]) -> None` | `Make loaded skills matching patterns visible to the LLM.` |
| `SkillRegistry.deactivate` | `skill_registry.py:493` | `def deactivate(self, patterns: list[str]) -> None` | `Hide activated skills from the LLM (still loaded).` |
| `SkillRegistry.reload` | `skill_registry.py:560` | `async def reload(self, name=None) -> str` | `Hot-reload one or all loaded skills.` |
| `SkillRegistry.status` | `skill_registry.py:864` | `def status(self) -> str` | `Render the skills context block for the LLM.` |
| `SkillRegistry.__getitem__` | `skill_registry.py:828` | `__getitem__(self, name) -> Any` | `Access a skill by its fully-qualified registry name.` |

### `strategies` (subpackage — re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `GenerationStrategy` | `strategies/base.py:186` | `class GenerationStrategy(ABC, metaclass=AgentMeta)` | `Abstract base class for generation strategies with automatic method wrapping.` |
| `RuntimeServices` | `strategies/base.py:23` | `@runtime_checkable class RuntimeServices(Protocol)` | `Protocol defining services available to strategies.` |
| `CurrentCall` | `strategies/current_call.py:21` | `@dataclass(frozen=True) class CurrentCall` | `Represents a method call being generated.` |
| `CompositeStrategy` | `strategies/composite.py:12` | `class CompositeStrategy(GenerationStrategy)` | `Base class for strategies that compose other strategies.` |
| `TemplateStrategy` | `strategies/template.py:18` | `class TemplateStrategy(GenerationStrategy)` | `Template rendering strategy using runtime.expand_variables().` |
| `CodeActStrategy` | `strategies/codeact.py:270` | `class CodeActStrategy(CompositeStrategy)` | `CodeAct strategy: LLM uses execute_python tool + structured output.` |
| `CodeActSession` | `strategies/codeact.py:146` | `@dataclass class CodeActSession` | `Tracks state for a single CodeAct generation session.` |
| `CodeActLiteStrategy` | `strategies/codeact_lite.py:227` | `class CodeActLiteStrategy(CodeActStrategy)` | `Simplified CodeAct strategy with clean message rendering.` |
| `ReflexionStrategy` | `strategies/reflexion.py:69` | `class ReflexionStrategy(GenerationStrategy)` | `Reflexion strategy: generate → reflect → improve loop.` |
| `ReflectionOutput` | `strategies/reflexion.py:45` | `class ReflectionOutput(BaseModel)` | pydantic model for reflection self-eval output |
| `PredictStrategy` | `strategies/predict.py:47` | `class PredictStrategy(GenerationStrategy)` | `Single-shot LLM call that tries to solve the task in one go (no loop/iteration).` |
| `PurePythonStrategy` | `strategies/pure_python.py:124` | `class PurePythonStrategy(CompositeStrategy)` | `LLM generates pure Python code in REPL-style interaction.` |
| `GenerationSession` | `strategies/pure_python.py:76` | `@dataclass class GenerationSession` | `Tracks state for a single code generation session.` |
| `Prefill` | `strategies/prefill.py:83` | `class Prefill(Protocol)` | `Protocol for prefill plugins.` |
| `InspectInputsPrefill` | `strategies/prefill.py:104` | `class InspectInputsPrefill` | `Prefill that inspects input parameters using pprint().` |
| `get_default_strategy` | `strategies/__init__.py:38` | `def get_default_strategy() -> GenerationStrategy` | `Get the default strategy for agents without an explicit strategy.` |
| `set_default_strategy` | `strategies/__init__.py:57` | `def set_default_strategy(strategy: GenerationStrategy | None) -> None` | `Set the default strategy for all agents in the current async context.` |
| `build_sampling_kwargs` | `strategies/base.py:351` | `def build_sampling_kwargs(config) -> dict[str, Any]` | `Build sampling kwargs for LLM calls, excluding None values.` |

### `unifiedllm` (subpackage — re-exported via `nooa` top-level)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `UnifiedLLM` | `unifiedllm/unifiedllm.py:1153` | `class UnifiedLLM(ABC)` | re-exported from `nooa.unifiedllm` |
| `CompletionClient` | `unifiedllm/unifiedllm.py` | class | re-exported from `nooa.unifiedllm` |
| `ReasoningCompletionClient` | `unifiedllm/unifiedllm.py` | class | re-exported from `nooa.unifiedllm` |
| `ResponsesClient` | `unifiedllm/unifiedllm.py` | class | re-exported from `nooa.unifiedllm` |
| `Tool` | `unifiedllm/unifiedllm.py:587` | `class Tool` | re-exported from `nooa.unifiedllm` |
| `ToolCall` | `unifiedllm/unifiedllm.py:665` | `class ToolCall` | re-exported from `nooa.unifiedllm` |
| `LLMResponse` | `unifiedllm/unifiedllm.py:674` | `@dataclass class LLMResponse` | `Standardized response from any LLM API` |
| `create_tool_from_callable` | `unifiedllm/unifiedllm.py:648` | `def create_tool_from_callable(tool_callable: Callable) -> Tool` | re-exported from `nooa.unifiedllm` |
| `extract_and_parse_json` | `unifiedllm/unifiedllm.py:341` | `def extract_and_parse_json(text: str) -> dict[str, Any]` | re-exported from `nooa.unifiedllm` |
| `HttpConfig` | `unifiedllm/http_config.py` | class | re-exported from `nooa.unifiedllm` |
| `FakeLLMClient` | `unifiedllm/fake.py` | class | re-exported from `nooa.unifiedllm` |
| `RetryConfig` | `unifiedllm/retry_config.py` | class | re-exported from `nooa.unifiedllm` |
| `RetryingWrapper` | `unifiedllm/retry.py` | class | re-exported from `nooa.unifiedllm` |
| `EmptyContentError` | `unifiedllm/retry.py` | class (exception) | re-exported from `nooa.unifiedllm` |
| `with_retry` | `unifiedllm/retry.py` | function | re-exported from `nooa.unifiedllm` |
| `sync_retry` | `unifiedllm/retry.py` | function | re-exported from `nooa.unifiedllm` |
| `get_llm_client` | `unifiedllm/registry.py` | function | re-exported from `nooa.unifiedllm` |
| `get_registry_config` | `unifiedllm/registry.py` | function | re-exported from `nooa.unifiedllm` |
| `reload_registry` | `unifiedllm/registry.py` | function | re-exported from `nooa.unifiedllm` |
| `ensure_loaded` | `unifiedllm/registry.py` | function | re-exported from `nooa.unifiedllm` |
| `resolve_api_key_from_config` | `unifiedllm/registry.py` | function | re-exported from `nooa.unifiedllm` |
| `MODELS` | `unifiedllm/registry.py` | mapping | re-exported from `nooa.unifiedllm` |

## Behavioral notes

### Top-level `nooa` (`__init__.py`)
- The module installs a `NullHandler` on the `nooa` logger so applications that never configure logging still satisfy Python's logging contract (`__init__.py:19`).
- `install_debug_handler()` is called as a side effect of import; it sends a `SIGUSR2` handler that dumps tracebacks and cell code to `debug_dump_<pid>.txt` in cwd (`__init__.py:178-180`).
- `llm_config_chain` is a lazy re-export: `__getattr__` defers import of `nooa.llm_config` until first attribute access (`__init__.py:89-93`). Keeps `import nooa` cheap.
- `CodeActLiteStrategy` and `ReflexionStrategy` are routed through `nooa.experimental` on first access so that instantiating them emits a `FutureWarning` (`__init__.py:97-100`). Importing them directly from `nooa.strategies` bypasses the warning.
- `visible` is a no-op context manager kept only for backward compatibility with agents written against an older API where `visible` toggled LLM visibility (`_visible.py:7`).

### `agent.py`
- `Agent` uses `AgentMeta` as its metaclass, which auto-wraps async methods with ellipsis bodies for LLM generation and adds tracing hooks when `_enable_tracing = True` (`agent.py:74`, `agent.py:126`).
- `llm=None` is rejected; omitting the parameter enables cascading resolution. Two call sites enforce this: `_validate_llm_param` (`agent.py:64`) and `__init_subclass__` (`agent.py:150`).
- The internal sentinel `INHERIT` (`agent.py:61`) distinguishes "parameter omitted" from "explicitly set to None" so `llm=None` always raises but `llm` unset cascades through the parent class / context var / error fallback.
- `Agent.set_llm` (`agent.py:425`) is the public accessor for hosts that swap models at runtime (e.g. a TUI `/switch` command).
- LLM resolution order (`agent.py:_resolve_llm`): instance → class hierarchy MRO → parent context var → `ValueError`.
- Truncation config uses merge semantics: defaults → class-level → instance-level (`agent.py:_resolve_truncation`).
- Three framework-protected blocks are auto-installed per agent: `system_prompt` (stable, from docstring), `self` (stable, `doc(type(self))`), and `state` (dynamic, `pformat(self, ...)`) (`agent.py:242-247`).
- `Agent.__setattr__` routes through `guard_dynamic_method` so that LLM-generated code cannot smuggle in new methods via `self.foo = lambda …` (`agent.py:480-484`).
- `_try_auto_enable_tracing` (`agent.py:35`) is a one-shot import hook for OTLP tracing when the viewer is reachable.
- System-prompt resolution walks the MRO for the nearest class docstring; `{expr}` placeholders are evaluated via `string.Formatter` against `{"self": self, "type": type}` (`agent.py:_resolve_system_prompt`).

### `decorators.py`
- `@strategy` does double duty: at class-creation time it attaches metadata (`_strategy_override`, `_strategy_llm`, `_strategy_context`, `_strategy_events`, `_strategy_truncation`) that `AgentMeta` reads; at runtime it returns a wrapper via `runtime.method_wrapper.create_agent_method_wrapper` or `runtime.standalone.create_standalone_wrapper` (`decorators.py:90-145`).
- The decorated method **must** be `async`; `@strategy` raises `TypeError` otherwise (`decorators.py:97-98`).
- Stacking multiple `@strategy` is rejected: `Cannot stack multiple @strategy decorators on <name>` (`decorators.py:71-72`).
- `context=` accepts either a plain `dict` or `ScopedContext` (`decorators.py:80-87`).
- When `strategy_instance` is omitted and the method body is `...`, the default strategy is resolved at decoration time via `get_default_strategy()` (`decorators.py:107-110`).
- Standalone functions (no `self` first parameter) get a fresh agent stub per call via `create_standalone_wrapper` (`decorators.py:113-118`).

### `metaclass.py`
- `AgentMeta` is generic — it auto-wraps on any class, not just `Agent`. It walks each method in the class namespace at construction time (`metaclass.py:71-94`) and uses `type.__setattr__` to bypass its own `__setattr__` guard during class construction.
- A method is wrapped if it is async + has an ellipsis body (generation), or — when `_enable_tracing = True` — any async method, or any sync `def` method (tracing-only). Dunders are skipped for sync tracing.
- Sync methods can't generate and can't run async `agent_call` middleware; they get tracing only (`metaclass.py:_create_sync_wrapper`).
- `AgentMeta.__setattr__` (`metaclass.py:113`) mirrors `Agent.__setattr__` and routes through `guard_dynamic_method` to block dynamic method addition on classes too.
- `@no_trace` sets `func._no_trace = True` and flips any existing wrapper's `_tracing_enabled[0] = False` so both decorator orderings (`@strategy @no_trace` and `@no_trace @strategy`) correctly suppress hooks (`metaclass.py:265-273`).

### `prompts.py`
- `PromptData` is a frozen dataclass with six fields: `system_prompt`, `task_prompt`, `inspect_prefill`, `pre_ellipsis`, `strategy_name`, `method_path` (`prompts.py:22-43`).
- `build_prompt_data(method, *args, **kwargs)` requires a *bound* agent method (checks `__self__` and `__func__`); passing a bare function raises `TypeError` (`prompts.py:223-235`).
- `print_prompt` writes formatted sections (`=== SYSTEM PROMPT ===`, `=== TASK PROMPT ===`, `=== PREFILL ===`) to `sys.stdout` (`prompts.py:241-262`).
- The docstring of `_build_prompt_data_from_agent` warns that calling it **mutates the agent's `DynamicContext` resolved-value cache** as a side effect of invoking `agent.runtime._build_messages()` (`prompts.py:114-119`). UNVERIFIED — requires B-lane to confirm runtime side-effects across all `_build_messages` paths.

### `skill.py`
- `Skill.__init__` requires exactly one of `obj=` or `content=` (the `Skill` base raises `ValueError` if both are given, and `type(self) is Skill` raises if neither is given) (`skill.py:290-295`).
- `Skill.__dir__` forwards `dir()` to the wrapped object so the LLM can discover its attributes (`skill.py:316-323`).
- `Skill.source_dir` returns `self._source_dir` if explicitly set, otherwise derives from `inspect.getfile(type(self))` — returns `None` if the source file cannot be resolved (`skill.py:326-348`).
- `TextSkill` parses a `SKILL.md` (or `skill.md`) file: frontmatter must include `name` and `description` (`skill.py:127-189`); class is dynamically re-created with a PascalCase name derived from the `skill_id` (`skill.py:354-360`).
- `TextSkill.run_script` enforces path-traversal protection via `_resolve_skill_path`; raises `FileNotFoundError` if the script doesn't exist under `scripts/` (`skill.py:393-403`).
- `@slash_command` attaches a `SlashCommandMeta` singleton via `_SLASH_COMMAND_ATTR`; `get_slash_commands` iterates `dir(type(skill))` and filters for the marker (`skill.py:20-102`).
- `output_to_agent=False` on `@slash_command` marks a slash command as a user-only read (no LLM turn) (`skill.py:57-61`).

### `library_manager.py`
- `LibraryManager.install(agent, *, libs_dir)` scans `libs_dir` for subdirectories containing a `pyproject.toml`, imports each as a Python package, and attaches a `Skill` (or `Skill(module, name=lib_name)` fallback) as `agent.<lib_name>` (`library_manager.py:46-66`).
- After each successful attach, the registry hot-reloads slash commands via the agent's `_command_registry.refresh_skill_commands()` if reachable (`library_manager.py:109-115`).
- `discover(path)` is a pure helper that returns library names without importing (`library_manager.py:127-129`).

### `media.py`
- `Media` uses `__slots__ = ("_data_url", "_media_type", "_vendor_metadata")` (`media.py:28`) and a class-level `_modality: str` overridden by each subclass.
- `Media.from_file(path)` reads bytes from disk and uses `mimetypes.guess_type` to derive the MIME type, falling back to `application/octet-stream` (`media.py:64-70`).
- `Media.from_bytes` base64-encodes the data and constructs a `data:<mime>;base64,...` URL (`media.py:73-79`).
- `Media.content_hash` is the first 8 hex chars of `sha256(data_url)` (`media.py:93-95`).
- `Image`, `Audio`, `Video`, and `File` each override `from_url` with sensible default MIME types (`image/jpeg`, `audio/wav`, `video/mp4`, `application/pdf`) (`media.py:126-200`).

### `strategy_validation.py`
- `MethodPrecondition` / `MethodPostcondition` are `Callable[..., Any]` aliases (`strategy_validation.py:14-15`).
- `InvariantError` extends `ValueError`; strategy runtimes catch it from postconditions to route through validation-retry feedback (`strategy_validation.py:18-25`).
- `normalize_conditions` rejects strings or non-iterables (`strategy_validation.py:33-43`).
- `_run_condition` re-raises `InvariantError` and wraps other exceptions in `RuntimeError("method {kind} {name!r} failed")` (`strategy_validation.py:46-52`).

### `token_counter.py`
- `char_approximate_token_counter(text)` returns `len(text) // 4` (`token_counter.py:11-32`).
- Documented as a "rough heuristic" — not for billing, only for context-window budget decisions (`token_counter.py:14-19`).

### `_logging.py` / `_visible.py`
- `enable_logging` is idempotent — it skips adding a second `StreamHandler` to the same stream and just updates the level (`_logging.py:84-90`).
- `visible` is a no-op singleton with `__enter__` returning `self` and `__exit__` returning `False` (`_visible.py:14-20`).

### `runtime.event_query`
- `EventQuery` is a frozen dataclass; instances are hashable and immutable (`runtime/event_query.py:15-50`).
- `apply()` filters in this order: `call_id`, `type` (matches `__class__.__name__`), `query` (regex or substring), then `limit` (kept last `N` via slice) (`runtime/event_query.py:88-127`).

### `runtime.context` / `runtime.context_manager` / `runtime.events`
- `ContextApi` is a `Skill` subclass wrapping `agent.context_manager` (`runtime/context.py:25`).
- `ContextManager` is the single source of truth for blocks; static blocks live in `_blocks`, `DynamicContext` blocks have their resolved value cached in `_dynamic_cache`, invalidated on `set_dynamic`/`__setitem__` (`runtime/context_manager.py:30-55`).
- Three protected blocks (`system_prompt`, `self`, `state`) are registered by `Agent.__init__` (`agent.py:242-247`) and cannot be overwritten via the LLM-facing API (`runtime/context_manager.py:42-45`).
- `EventsApi` exposes `query(type=, call_id=, query=, regex=, limit=)`, `get(tag)`, `__getitem__`, range tags `"1..22"`, and children-of-summary access (`runtime/events.py:21-60`).

### `runtime.channels`
- `Channel[T]` is generic; the `[T]` syntax implies a future `Generic[T]` annotation (the file currently uses `class Channel[T]:` — UNVERIFIED for runtime behavior — `runtime/channels.py:181`).
- `QueueOutput` (`runtime/channels.py:40`) is the event fired by event-mode channels; `QueueManager` (`runtime/channels.py:572`) is the registry + race + status aggregator.

### `skill_registry.SkillRegistry`
- Three-stage lifecycle: `Discover → Load → Activate` (`skill_registry.py:135-146`).
- `load(patterns)` uses `fnmatch` globs; rejected names: leading underscore or any in `_RESERVED_ATTRS` (`skill_registry.py:357-361`).
- `register(name, skill_or_cls=None, /, **kwargs)` has three modes: entry-point lookup, explicit class + kwargs, pre-constructed instance (`skill_registry.py:379-428`).
- `activate(patterns)` auto-loads unloaded matches first and resolves `Skill.requires` dependencies transitively with cycle detection (`skill_registry.py:470-491`).
- Reload logic (`_reload_package` vs `_reload_single_module`) branches on whether the skill's top package is in `_NO_RELOAD = {"tests", "__main__", "nooa", "nooa_cli"}` — protects the framework from the "package-purge footgun" (`skill_registry.py:611-643`).
- `status()` (`skill_registry.py:864-924`) renders the LLM-visible "Active Skills / Available Skills" block.

### `storage`
- `StorageManager` is a `runtime_checkable` Protocol with `event_backend`, `save_snapshot`, `restore_snapshot`, and snapshot-related helpers (`storage/manager.py:24-52`).
- `nosnapshot` is a singleton marker used in `Annotated[T, nosnapshot]`; `is_nosnapshot_field` walks the MRO to find the annotation (`storage/markers.py:36-61`).
- `snapshotable` is a class decorator that sets `cls.__snapshot_dict__ = True` so `serialize()` knows it can call `vars(instance)` (`storage/markers.py:95-119`).

### `strategies` (`GenerationStrategy` family)
- `GenerationStrategy` uses `AgentMeta` as its metaclass — strategy methods with ellipsis bodies are auto-wrapped for generation (`strategies/base.py:186-205`).
- `RuntimeServices` is a `@runtime_checkable` Protocol with `agent`, `event_manager`, `truncation_config`, and async `generate()`/`execute_code()` (`strategies/base.py:22-180`).
- `CompositeStrategy` (`strategies/composite.py:12`) is a marker base class for strategies that compose others via `@strategy` methods.
- `TemplateStrategy` (`strategies/template.py:18`) is the foundational string-templating strategy: no LLM call, just `runtime.expand_variables(template, extra_context=context, error_mode="raise")`.
- `PurePythonStrategy` (`strategies/pure_python.py:124`) is REPL-style — LLM emits raw Python executed in a persistent session.
- `CodeActStrategy` (`strategies/codeact.py:270`) uses `execute_python` as a tool call + structured output via `output_model`.
- `CodeActLiteStrategy` (`strategies/codeact_lite.py:227`) is `CodeAct` with three changes: events scoped to current call, plain-text rendering, tool results inlined into `PythonOutput`.
- `PredictStrategy` (`strategies/predict.py:47`) is a single-shot structured-output call with validation retry (`max_retries`).
- `ReflexionStrategy` (`strategies/reflexion.py:69`) wraps a base strategy (default `PurePythonStrategy`) in a generate→reflect→improve loop.
- `Prefill` is a `Protocol` requiring `get_code(call, config=None) -> str | None` (`strategies/prefill.py:83-101`).
- `InspectInputsPrefill` prints `pprint()` of every kwarg plus the return type, with optional `_is_media` short-circuit that emits `show(<param>)` (`strategies/prefill.py:104-187`).
- `get_default_strategy()` reads from a `ContextVar` (`_default_strategy_var`) — returns a fresh `CodeActStrategy(CodeActConfig())` if unset (`strategies/__init__.py:33-54`).

### `unifiedllm`
- `LLMResponse` (`unifiedllm/unifiedllm.py:674-688`) is the cross-provider response shape: `raw_response`, `content`, `tool_calls`, `finish_reason`, `assistant_message`, optional `reasoning`/`usage`. `message` is a backward-compatible alias for `content`.
- `Tool` and `ToolCall` are provider-agnostic types; `create_tool_from_callable(callable)` adapts a Python function to a `Tool` (`unifiedllm/unifiedllm.py:587-665`).
- `extract_and_parse_json(text)` handles nested JSON-stringified payloads (`unifiedllm/unifiedllm.py:341-`).
- `HttpConfig`, `FakeLLMClient`, `RetryConfig`, `RetryingWrapper`, `with_retry`, `sync_retry`, `EmptyContentError`, plus `get_llm_client`/`reload_registry`/`MODELS` are re-exported through `nooa.unifiedllm.__init__`.

## Cross-references

- `strategies.md` — owned by another M-lane worker. Overlap with this document:
  - All strategy classes (`GenerationStrategy`, `CodeActStrategy`, `CodeActLiteStrategy`, `ReflexionStrategy`, `PredictStrategy`, `PurePythonStrategy`, `TemplateStrategy`, `CompositeStrategy`)
  - `CurrentCall`, `Prefill`, `InspectInputsPrefill`, `GenerationSession`, `CodeActSession`, `ReflectionOutput`
  - `RuntimeServices`, `build_sampling_kwargs`
  - `get_default_strategy`, `set_default_strategy`
  - `StrategyValidation` types (`InvariantError`, `MethodPrecondition`, `MethodPostcondition`, `normalize_conditions`, `run_preconditions`, `run_postconditions`)
  - Note that `strategies.md` is the authoritative source for behavior; this file only enumerates signatures.

- `context-blocks.md` — owned by another M-lane worker. Overlap:
  - `Context`, `DynamicContext`, `ContextWindowStats`, `ResolvedBlock`, `Role`, `BlockMetadata`, `RenderedMessage`, `ToolCallInfo`
  - `RenderConfig`, `ScopedContext`, `render_context`, `RenderResult`, `format_message_content`
  - `FormatType`, `FORMAT_XML`, `FORMAT_MARKDOWN`, all `*Formatter` classes
  - All `*Event` classes (`EventBase`, `Event`, `EventStatus`, `ResultStatus`, `Metadata`, `UserEvent`, `AssistantEvent`, `ToolCallEvent`, `ToolResult`)
  - All `*Error` classes (`BlockError`, `BlockSyntaxError`, `DynamicNotResolvedError`, `ProtectedBlockError`)
  - `ContextManager` (re-exported via top-level), `ContextApi` (re-exported via top-level)

- `agentdoc.md` — owned by another M-lane worker. Overlap:
  - `spec`, `hidden`, `SpecAnnotation`, `doc`, `methods`, `variables`, `pformat`, `pprint`, `truncating_pformat`
  - `DocConfig`, `TruncatingStringIO`, `FileBackedTruncatingStringIO`
  - `TypeInfo`, `FieldInfo`, `CallableInfo`, `ModuleInfo`, `REQUIRED`, all `extract_*` functions
  - `register_type_info_extractor` and the `*_extractor` siblings, `has_type_info`
  - All three `Supports*Info` protocols
  - `filter_module_globals`, `filter_mro_module_globals`, `is_hidden_field`, `is_hidden_method`, `is_hidden_module_variable`, `iter_agent_mro_modules`

- `unifiedllm.md` — owned by another M-lane worker. Overlap:
  - `UnifiedLLM`, `CompletionClient`, `ReasoningCompletionClient`, `ResponsesClient`
  - `Tool`, `ToolCall`, `LLMResponse`, `create_tool_from_callable`, `extract_and_parse_json`
  - `HttpConfig`, `FakeLLMClient`, `RetryConfig`, `RetryingWrapper`, `with_retry`, `sync_retry`, `EmptyContentError`
  - Registry functions: `get_llm_client`, `get_registry_config`, `reload_registry`, `ensure_loaded`, `resolve_api_key_from_config`, `MODELS`

- This file (`exports.md`) is the **inventory** — it lists every name re-exported through `nooa` and the top-level classes/functions in user-facing modules. The four sister files above provide behavioral detail; readers should not duplicate their content here.

## Suggested remediation

No remediation suggested at this time. The public surface is consistent: every name in `__all__` is defined at the claimed source, every documented signature matches its defining file, and lazy re-exports (`llm_config_chain`, `CodeActLiteStrategy`, `ReflexionStrategy`) correctly emit `FutureWarning` on first access. The only items that warrant follow-up are runtime-behavior questions (marked `UNVERIFIED — requires B-lane` above) rather than code changes.