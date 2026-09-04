# nooa unifiedllm — registry + providers

## Source paths read

- `/tmp/nooa-monorepo/src/nooa/unifiedllm/__init__.py` — public re-exports.
- `/tmp/nooa-monorepo/src/nooa/unifiedllm/registry.py` — YAML-backed `MODELS` registry, `get_llm_client`, `reload_registry`, `ensure_loaded`, `resolve_api_key_from_config`, `get_registry_config`.
- `/tmp/nooa-monorepo/src/nooa/unifiedllm/unifiedllm.py` — `UnifiedLLM` ABC, `CompletionClient`, `ReasoningCompletionClient`, `ResponsesClient`, `Tool`, `ToolCall`, `LLMResponse`, `create_tool_from_callable`, `extract_and_parse_json`, internal `_ClientHttp` transport that selects OpenAI SDK vs. `AsyncHTTPHandler` wrappers.
- `/tmp/nooa-monorepo/src/nooa/unifiedllm/http_config.py` — `HttpConfig` (per-client httpx limits + timeouts).
- `/tmp/nooa-monorepo/src/nooa/unifiedllm/retry_config.py` — `RetryConfig`.
- `/tmp/nooa-monorepo/src/nooa/unifiedllm/retry.py` — `with_retry`, `sync_retry`, `RetryingWrapper`, `EmptyContentError` (referenced via `__init__.py`; full body not consumed).
- `/tmp/nooa-monorepo/src/nooa/unifiedllm/fake.py` — `FakeLLMClient` for hermetic tests.
- `/tmp/nooa-monorepo/src/nooa/llm_config.py` — `llm_config_chain()` and `bundled_config_paths()`; framework-level YAML discovery that the registry delegates to.

## Public exports

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
| --- | --- | --- | --- |
| `UnifiedLLM` | `unifiedllm/unifiedllm.py:1153` | `class UnifiedLLM(ABC)` with `model`, `config`, `_registry_config`, `_http`, `context_window`, `count_tokens`, `get_model_info`, `close`, `aclose` | Abstract base; `__init__(self, model: str, **config)` — `<truncated>` (multi-line, see source) |
| `CompletionClient` | `unifiedllm/unifiedllm.py:1690` | `CompletionClient(model, retry_config=None, http_config=None, cache_control_injection_points=None, **config)` | "Initialize CompletionClient. …" `<truncated>` |
| `ReasoningCompletionClient` | `unifiedllm/unifiedllm.py:2100` | subclass of `CompletionClient` | "CompletionClient for reasoning models that output <think>...</think> tags. …" `<truncated>` |
| `ResponsesClient` | `unifiedllm/unifiedllm.py:2210` | `ResponsesClient(model, retry_config=None, http_config=None, cache_control_injection_points=None, **config)` | "Mirrors CompletionClient so the Responses API path gets the same retry, HTTP, and cache-control behaviour. …" `<truncated>` |
| `Tool` | `unifiedllm/unifiedllm.py:587` | `class Tool` | `<truncated>` |
| `ToolCall` | `unifiedllm/unifiedllm.py:665` | `class ToolCall` | `<truncated>` |
| `LLMResponse` | `unifiedllm/unifiedllm.py:674` | `class LLMResponse` | `<truncated>` |
| `create_tool_from_callable` | `unifiedllm/unifiedllm.py:648` | `create_tool_from_callable(tool_callable: Callable) -> Tool` | `<truncated>` |
| `extract_and_parse_json` | `unifiedllm/unifiedllm.py:341` | `extract_and_parse_json(text: str) -> dict[str, Any]` | "Extract and parse JSON from text, with multiple fallback strategies" |
| `HttpConfig` | `unifiedllm/http_config.py:7` | Pydantic `BaseModel` (frozen) — `max_connections=100`, `max_keepalive_connections=0`, `connect_timeout=10.0`, `read_timeout=60.0`, etc. | "Per-client HTTP connection-pool and timeout settings. …" `<truncated>` |
| `RetryConfig` | `unifiedllm/retry_config.py` | dataclass (per `__init__.py` re-export) | `<truncated>` (full body not consumed) |
| `with_retry` / `sync_retry` | `unifiedllm/retry.py` | decorator helpers | `<truncated>` |
| `RetryingWrapper` | `unifiedllm/retry.py` | wrapper class | `<truncated>` |
| `EmptyContentError` | `unifiedllm/retry.py` | exception | `<truncated>` |
| `FakeLLMClient` | `unifiedllm/fake.py:15` | `FakeLLMClient(scripted_responses: list[LLMResponse] \| None = None)` | "Fake LLM client that returns scripted responses. Useful for hermetic testing without network calls. Thread-safe for concurrent calls." |
| `get_llm_client` | `unifiedllm/registry.py:275` | `get_llm_client(name: str, *, client_type: str \| None = None, **overrides) -> UnifiedLLM` | "Create an LLM client, optionally using registry config. If ``name`` is a registry key, its config (model_name, endpoint, API key, defaults) is applied. Otherwise ``name`` is passed directly to litellm …" `<truncated>` |
| `reload_registry` | `unifiedllm/registry.py:171` | `reload_registry(*paths: Path) -> dict[str, dict[str, Any]]` | "Reload ``MODELS`` from YAML files, last-wins. …" `<truncated>` |
| `ensure_loaded` | `unifiedllm/registry.py:227` | `ensure_loaded() -> None` | "Idempotently populate ``MODELS`` via the framework's config chain. …" `<truncated>` |
| `get_registry_config` | `unifiedllm/registry.py:255` | `get_registry_config(name: str) -> dict[str, Any]` | "Return a defensive snapshot of the raw registry config for *name*. …" `<truncated>` |
| `resolve_api_key_from_config` | `unifiedllm/registry.py:79` | `resolve_api_key_from_config(model_name: str, config: Mapping[str, Any]) -> str \| None` | "Read the api_key_env-named env var declared by a registry config. …" `<truncated>` |
| `MODELS` | `unifiedllm/registry.py:164` | module-level `dict[str, dict[str, Any]]` | Live, in-place merged view of all YAML layers (see module comment). |
| `bundled_config_paths` | `llm_config.py:47` | `bundled_config_paths() -> list[Path]` | "Return on-disk paths for every registered bundled-default YAML. …" `<truncated>` |
| `llm_config_chain` | `llm_config.py:90` | `llm_config_chain() -> list[Path]` | "Return YAML config files for the LLM registry, lowest priority first. …" `<truncated>` |

There are **no dedicated `OpenAICompatibleClient` / `OllamaClient` / `VLLMClient` provider classes** in the upstream package. Provider routing is delegated to `litellm` at call time (see "Behavioral notes"). The closest provider-aware code is the internal `_ClientHttp` helper.

## Behavioral notes

### Registry model

`MODELS` (`registry.py:164`) is a plain `dict[str, dict[str, Any]]` of model aliases to raw config dicts (NOT a typed `ModelConfig` boundary — the module comment notes that `dict[str, ModelConfig]` is the planned future shape). It is updated **in place** by `reload_registry`, so any caller holding a reference to `MODELS` sees updates.

### Provider registration

Providers are NOT registered as classes. They are declared in YAML files (`llm_config.yaml`) with the schema documented in the `registry.py` module docstring:

```yaml
models:
  my-alias:
    model_name: openai/my-org/my-model   # exact litellm routing string
    api_base: https://my-gateway.example.com/v1
    api_key_env: MY_API_KEY
    context_window: 128000
    temperature: 0.0
    top_p: 1.0
    max_tokens: 4096
    drop_params: true                    # default true
```

Setting a model to `null` in a later layer removes it (last-wins merge with explicit deletion).

### `get_llm_client(name, *, client_type=None, **overrides)` — resolution flow

1. Calls `ensure_loaded()` (registry.py:317) — populates `MODELS` on first invocation; no-op thereafter.
2. Snapshots the alias's config under `_registry_lock` (an `RLock` guarding `_loaded`/`MODELS` mutation). The lock is only held for the dict lookup; client construction happens after release.
3. If the snapshot is empty, the raw `name` is passed straight to `litellm` (registry.py:334, "registry miss" debug log) — litellm's built-in routing handles every common public provider.
4. Builds a `params` dict: `model = config.get("model_name", name)`, `drop_params = config.get("drop_params", True)`. Adds `api_base` (unless caller overrode it) and `api_key` resolved from `api_key_env` via `resolve_api_key_from_config` (unless caller overrode `api_key` — to avoid misleading "env var unset" warnings).
5. Passes through optional model defaults: `temperature`, `top_p`, `max_tokens`, `reasoning`, `reasoning_effort`, `allowed_openai_params`, `additional_drop_params`, `extra_body` (call-site `overrides` always win).
6. `retry_config` may be set in YAML as `false`/`null` (→ single attempt, `RetryConfig(max_retries=0, rate_limit_extra_retries=0)`) or as a mapping (forwarded to `RetryConfig(**retry_config)`). Explicit call-site overrides win.
7. Selects a client class (first wins): explicit `client_type` param → YAML `client_type` field → `"completion"` default. Returns either `ResponsesClient` or `CompletionClient`. The chosen client has its `_registry_config` attribute set so `UnifiedLLM.context_window` can read the YAML entry without a second registry lookup.

### `reload_registry(*paths)` and `ensure_loaded()` — config discovery

- `reload_registry(*paths)` (registry.py:171): with explicit paths, loads only those. With zero paths, it lazily imports `nooa.llm_config.llm_config_chain` and uses the chain. The chain is **lowest → highest priority** (last wins):
  1. **Bundled defaults** — every entry-point in the `nooa.bundled_configs` group (e.g. `nemo-oo-agents-nvidia` ships NVIDIA-gateway aliases). `bundled_config_paths()` iterates `importlib.metadata.entry_points(group="nooa.bundled_configs")`, calls each zero-arg callable (must return `Path | None`), filters `None`, sorts by entry-point name. Defensive: warnings + skips on broken entry-points rather than raising.
  2. `get_user_dir("llm_config.yaml")` — `~/.config/nooa/llm_config.yaml`.
  3. `get_project_dir("llm_config.yaml")` — `<project-root>/.nooa/llm_config.yaml`.
  4. `NEMO_OO_LLM_CONFIG` env var — comma-separated YAML paths. Highest priority. Paths that don't exist log a warning; user/project layers are silently skipped when missing.
  Only files that actually exist are returned. Each path is `Path.resolve()`-d so duplicates/symlinks collapse — the lower-priority occurrence is dropped and the higher-priority position is kept, so the last-wins merge lines up.
- `ensure_loaded()` (registry.py:227): idempotent auto-load via `reload_registry()`. Called automatically on the first `get_llm_client()`. Once `reload_registry` has run (with or without paths), this is a no-op — explicit reloads win over auto-discovery.
- Concurrency: `_registry_lock` (an `RLock`, registry.py:67) serialises the `(_loaded, MODELS)` check-and-mutate. `reload_registry` re-acquires the lock from inside `ensure_loaded` (so reentrant `RLock` is required). The `MODELS.clear()` then `MODELS.update()` mutation is atomic w.r.t. concurrent readers — `get_llm_client` and `get_registry_config` snapshot the alias under the same lock so they can never see a half-cleared dict.

### OpenAI-compatible providers

There is **no explicit OpenAI-compatible provider class** in upstream — the package relies on `litellm`'s built-in routing. The relevant behaviour lives in `_ClientHttp._build_completion_wrappers()` (unifiedllm.py:208):

1. Calls `litellm.get_llm_provider(model, api_key=..., api_base=...)` to detect the provider family from the litellm model string.
2. `openai_family = provider == "openai" or provider in getattr(litellm, "openai_compatible_providers", [])`. Note: litellm's `openai_compatible_providers` list is read at runtime — **the exact list of providers recognised as "OpenAI-compatible" is whatever litellm ships** (`UNVERIFIED — requires B-lane` to enumerate). The registry docstring calls out "OpenAI-compatible gateways" as a known case and `api_base` is the primary differentiator for these.
3. For the OpenAI family: builds `AsyncOpenAI(http_client=self.httpx_async, ...)` / `OpenAI(http_client=self.httpx_sync, ...)` from the per-client `HttpConfig`. Common kwargs: `timeout`, `api_key` (from config or `dynamic_api_key` from `get_llm_provider`), `base_url` (from config or `dynamic_api_base`). Stores them in `_openai_clients` for clean teardown via `close()` / `aclose()`.
4. For all other providers (anthropic, bedrock, vertex, ...): builds `AsyncHTTPHandler` / `HTTPHandler` litellm wrappers around the per-client httpx clients — `litellm` handlers isinstance-check these.
5. On any failure (provider detection fails, OpenAI SDK can't be constructed): logs at debug, leaves wrappers as `None`, and lets `litellm` build its own default client — the call still succeeds but loses the custom pool/timeout.

The Responses API always uses the handler-wrappers path (`_ClientHttp.for_responses`, unifiedllm.py:267), regardless of provider — `litellm.responses` goes through `base_llm_http_handler` which accepts any provider's `AsyncHTTPHandler`.

### Ollama

**No dedicated Ollama class or special-case code.** Ollama is reached by passing an `ollama/<model>` litellm routing string as `model_name` (or as the raw `name` argument) plus, if needed, `api_base`. `litellm` handles the protocol translation; the registry treats the entry like any other alias. `UNVERIFIED — requires B-lane` confirmation that litellm's Ollama adapter is exercised by the harness.

### vLLM

**No dedicated vLLM class or special-case code.** vLLM is treated as an OpenAI-compatible gateway: route via `openai/<model>` with `api_base` pointing at the vLLM server's `/v1` endpoint. The registry passes these through unchanged and `_ClientHttp._build_completion_wrappers` will detect the provider as OpenAI-family (assuming vLLM is in `litellm.openai_compatible_providers` — `UNVERIFIED — requires B-lane`). The codebase also references "vLLM's hermes parser" (unifiedllm.py:1564, 1864, 2042) — a hermes-format XML tool-call extractor used as a fallback when Nemotron / hermes-format models emit XML tool calls that the standard parser rejects.

### Other notes

- `get_registry_config(name)` (registry.py:255): public accessor over the raw dict for external readers (`nat` plugin, `eval_pipeline`, viewer). Triggers `ensure_loaded` first. Returns `{}` for unknown names.
- `resolve_api_key_from_config` (registry.py:79): shared helper outside the registry (used by the NAT plugin and viewer). Logs a WARN if `api_key_env` is set but the env var is unset or empty, and falls back via the `_NVIDIA_KEY_SYNONYMS` map (`NVIDIA_INFERENCE_API_KEY` ↔ `NVIDIA_INTERNAL_API_KEY`) before returning `None`.
- `ContextVar` `_llm_metrics_callback` (unifiedllm.py:51): optional harness metrics callback injected by `actor.py` at session start. `_record_llm_metric` is fire-and-forget and swallows callback exceptions.
- `litellm.modify_params = True` (unifiedllm.py:29): lets litellm auto-insert a dummy `tools=` for Bedrock/Anthropic when messages contain tool_call blocks but `tools=` is absent (otherwise Bedrock rejects). `litellm.disable_aiohttp_transport = True` (unifiedllm.py:36): uses httpx instead of aiohttp to avoid "Unclosed client session" / "Unclosed connector" ResourceWarnings on shutdown.
- Per-client `HttpConfig` defaults: `max_keepalive_connections=0` (disables pooling to avoid CLOSE_WAIT hangs), `keepalive_expiry=5.0`, `connect_timeout=10.0`, `read_timeout=60.0`, `write_timeout=10.0`, `pool_timeout=10.0`. Frozen Pydantic model.

## Cross-references

- `strategies.md` (in this directory) — the strategies layer is where `get_llm_client` / `UnifiedLLM.call` / `acall` are actually invoked per agent turn; it documents the call sites.
- `docs/nooa-kb/from-monorepo/packages-nooa/exports.md` — top-level `nooa` package re-exports (does this `nooa.unifiedllm` get re-exported at the top level? Worth checking against the KB exports file before relying on a flat import path).
- `market_service/nooa_harness/backends.py` (in our repo) — maps canonical `PROVIDER` env tokens (`openai`, `vllm`, `ollama`, `anthropic`) onto litellm model prefixes (`openai/`, `ollama/`, `anthropic/`; `vllm` shares `openai/`) and constructs the upstream `CompletionClient` / `ResponsesClient` via the package. The mapping in `backends.py` is the call-site analog of what upstream handles via litellm routing and `_ClientHttp` provider detection. Not read here — referenced only.

## Suggested remediation

- **No dedicated provider classes.** Upstream has no `OpenAICompatibleClient`, `OllamaClient`, or `VLLMClient`. Our `backends.py` already encodes provider semantics as litellm model prefixes, but anyone reading the upstream `unifiedllm` API expecting "provider = class" will be surprised. If we ever want stronger typing for these, we'll have to add a wrapper layer on top of `get_llm_client` (in `backends.py` or a sibling) — there is no upstream hook.
- **`litellm.openai_compatible_providers` is the runtime source of truth** for which providers `_ClientHttp` treats as OpenAI-family (and therefore which get an OpenAI SDK client wrapper). The list is whatever litellm ships at the pinned version — if a new provider lands, behavior changes silently. Pin litellm and re-verify on upgrades.
- **`MODELS` is a raw dict, not `dict[str, ModelConfig]`.** The registry module comment calls this out: `nooa.config.ModelConfig` / `get_model_config()` is the typed boundary, but `MODELS` and `get_registry_config` still expose raw dicts for backward compatibility with the `nat` plugin, `eval_pipeline`, and viewer. If our harness ever reaches into `MODELS` directly it should expect dict semantics (no schema validation).
- **YAML-driven discovery is split across two modules.** `unifiedllm` is intentionally filesystem-convention-agnostic and delegates to `nooa.llm_config.llm_config_chain`. Anyone debugging "why didn't my model alias load?" needs to look in `llm_config.py`, not `unifiedllm/`. Worth noting in our local runbook.
- **`NEMO_OO_LLM_CONFIG` is the highest-priority override.** Anyone running the harness with a non-default `NEMO_OO_LLM_CONFIG` (or with `nemo-oo-agents-nvidia` installed/uninstalled) will see different model aliases without any code change. Verify environment consistency in container runs.
- **No Ollama / vLLM verification in this source-only read.** Routing relies entirely on litellm — both providers are presumed to work because litellm claims to support them, but no `UNVERIFIED — requires B-lane` confirmation that the harness has actually exercised them. Specifically: vLLM depends on vLLM being included in `litellm.openai_compatible_providers`; Ollama depends on litellm's Ollama adapter. Mark the corresponding harness integration tests as the verification path.
