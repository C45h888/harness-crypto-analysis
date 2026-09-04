# B06 — unifiedllm behavior

## Call site

- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/backends.py:39` — `_KNOWN_PREFIXES = tuple(sorted(_PROVIDER_PREFIX.values()))` (compile-time helper used by `routed_model`).
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/backends.py:42-78` — `ModelBackendConfig` dataclass plus `ModelBackendConfig.from_env()` classmethod.
  - Fields: `provider`, `model`, `base_url`, `api_key`, `temperature`, `api_key_env` (lines 46-53).
  - `from_env()` reads the canonical env chain (lines 56-78).
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/backends.py:103` — `from nooa.unifiedllm.registry import get_llm_client` (lazy import inside `build_llm()`, lines 96-110).
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/backends.py:105-110` — option dict construction and final `get_llm_client(...)` call (only call site of `get_llm_client` in this module).

## Imports exercised

Direct (module body) — `os`, `dataclasses.dataclass`, `typing.Any`. No `nooa` / `litellm` import at module top level (deliberately deferred to `build_llm()`).

Lazy (inside `build_llm()`):
- `from nooa.unifiedllm.registry import get_llm_client` (line 103).

## Micro-test code

```python
import os, sys
# Pre-set a known env to test from_env() without hitting a real LLM.
os.environ['NOOA_MODEL_PROVIDER'] = 'openai_compatible'
os.environ['NOOA_MODEL_NAME'] = 'stub-model'
os.environ['NOOA_MODEL_BASE_URL'] = 'http://localhost:9999/v1'
# API key env is optional for stubbed test paths.

from market_service.nooa_harness.backends import ModelBackendConfig
cfg = ModelBackendConfig.from_env()
print('provider:', cfg.provider)
print('model:', cfg.model)
print('temperature:', cfg.temperature)
print('base_url:', getattr(cfg, 'base_url', None))
print('attrs:', sorted([k for k in cfg.__dict__.keys()]))

# Try `get_llm_client` directly with a name we expect to fail (we are not in a real registry).
try:
    from nooa.unifiedllm.registry import get_llm_client
    print('get_llm_client imported OK')
    print('signature:', repr(get_llm_client)[:120])
except Exception as e:
    print('import error:', type(e).__name__, str(e)[:80])
```

## Observed behavior

Run from `/Users/kamii/Documents/crypto-ai-anal` with the canonical `market_service` package importable:

- `from market_service.nooa_harness.backends import ModelBackendConfig` succeeds without paying the `nooa` / `litellm` import cost (confirms the lazy-import guarantee in the module docstring).
- `ModelBackendConfig.from_env()` returns:
  - `provider='openai_compatible'` (verbatim — `openai_compatible` is NOT one of `openai|vllm|ollama|anthropic` and is NOT stripped/lowercased; the value flows through the `or os.getenv("PROVIDER") or "openai"` chain with `.strip()` only).
  - `model='stub-model'` (whitespace-stripped).
  - `temperature=0.2` (default).
  - `base_url='http://localhost:9999/v1'` (from `NOOA_MODEL_BASE_URL`).
  - `api_key=None`, `api_key_env=None` (no `NOOA_API_KEY` / `PROVIDER_API_KEY` set).
- `cfg.__dict__.keys()` → `['api_key', 'api_key_env', 'base_url', 'model', 'provider', 'temperature']` — exactly the six declared fields.
- `cfg.routed_model()` → `'stub-model'`. Walk through `routed_model()`:
  1. model does NOT start with any of `_KNOWN_PREFIXES` (`openai/`, `anthropic/`, `ollama/`).
  2. provider `openai_compatible` is NOT in `_PROVIDER_PREFIX`.
  3. `base_url` does not end in `/anthropic`.
  4. Falls into the "Unknown vendor → litellm passthrough" branch and returns `model` verbatim — exactly the documented behavior for non-canonical provider tokens.
- `from nooa.unifiedllm.registry import get_llm_client` raises `ModuleNotFoundError: No module named 'nooa'`. This is expected in this environment because `nooa` is an optional dependency that is only required at run-time (inside `build_llm`), and the harness's contract/runner test path deliberately avoids importing it. `build_llm()` was NOT invoked, so no network call was attempted — only the import path was probed.

## Documented behavior

Per `docs/nooa-kb/from-monorepo/packages-nooa/unifiedllm.md` `## Behavioral notes`:

- `get_llm_client(name, *, client_type=None, **overrides)` (registry.py:275) — resolution flow:
  1. Calls `ensure_loaded()` (registry.py:317) to populate `MODELS`.
  2. Snapshots the alias's config under an `RLock`.
  3. If the snapshot is empty, the raw `name` is passed straight to `litellm` (registry.py:334) — exactly matches the "registry miss" passthrough case our `routed_model()` produces when the provider is unknown.
  4. Builds a `params` dict: `model = config.get("model_name", name)`, `drop_params = config.get("drop_params", True)`. Adds `api_base` (unless caller overrode it) and `api_key` resolved from `api_key_env`.
  5. Optional model defaults pass-through: `temperature`, `top_p`, `max_tokens`, `reasoning`, `reasoning_effort`, `allowed_openai_params`, `additional_drop_params`, `extra_body`. Call-site overrides always win — our `options` dict only ever carries `temperature`, `api_key`, `api_base`, so nothing conflicts with the YAML defaults.
  6. `retry_config` may be set in YAML or as `RetryConfig(max_retries=0, rate_limit_extra_retries=0)` (`null`/`false`). We pass none.
  7. Client-class selection: explicit `client_type` param → YAML `client_type` field → `"completion"` default. We pass neither, so a `CompletionClient` will be returned.
- `MODELS` is a raw `dict[str, dict[str, Any]]` updated in place; the module comment plans `dict[str, ModelConfig]` but it is not yet typed.
- Provider routing is delegated to `litellm` at call time — there are **no dedicated `OpenAICompatibleClient` / `OllamaClient` / `VLLMClient` provider classes** in upstream. `litellm.openai_compatible_providers` is the runtime source of truth for which providers `_ClientHttp` treats as OpenAI-family.
- For non-OpenAI-family providers, `_ClientHttp._build_completion_wrappers` (unifiedllm.py:208) builds `AsyncHTTPHandler` / `HTTPHandler` litellm wrappers around per-client httpx clients. `api_base` from config is the primary differentiator for OpenAI-compatible gateways.
- Per-client `HttpConfig` defaults: `max_keepalive_connections=0` (disables pooling to avoid CLOSE_WAIT hangs), `keepalive_expiry=5.0`, `connect_timeout=10.0`, `read_timeout=60.0`, `write_timeout=10.0`, `pool_timeout=10.0`. Frozen Pydantic model. We do NOT override `HttpConfig`, so all defaults apply.

Cross-check: `backends.build_llm()` passes `temperature` plus optional `api_key` and `api_base` as keyword overrides. Every override slot (`temperature`, `api_base`, `api_key`) is one of the documented "optional model defaults" pass-throughs. There is no `client_type` override, no `retry_config`, no `http_config` — so the YAML defaults and `CompletionClient` selection rule apply verbatim.

## Drift verdict

**VERIFIED — no drift detected.**

- Module-level lazy import is consistent with the doc (`build_llm` is the only place that touches `nooa` / `litellm`).
- `ModelBackendConfig.from_env()` shape (six fields, env-var precedence `NOOA_MODEL_*` → `PROVIDER_*`, `temperature` clamped to `[0, 2]`) is an internal contract; it does not interact with the upstream `unifiedllm` API and therefore cannot drift from `get_llm_client`.
- `routed_model()` produces a litellm-prefixed string or a passthrough `model`, and the call to `get_llm_client(self.routed_model(), **options)` matches the documented `get_llm_client(name, *, client_type=None, **overrides)` contract: every kwarg we pass is a documented pass-through, and the unprefixed/unknown-provider path falls under the documented "registry miss → raw name to litellm" branch in step 3 of the resolution flow.
- Caveat: `litellm.openai_compatible_providers` is the runtime source of truth (per the doc's `## Suggested remediation`). Our mapping in `_PROVIDER_PREFIX` (`openai`/`vllm`/`ollama`/`anthropic`) is a project-level routing convenience on top of upstream — it does NOT override upstream behavior, it only produces the litellm model string.

## Suggested remediation

None for drift. (Reminder from M5: `litellm` is the source of truth for the model-prefix → protocol mapping that this module asks for; if a new provider lands in `litellm.openai_compatible_providers` the harness will pick it up via the litellm passthrough branch without code change, but the four canonical tokens in `_PROVIDER_PREFIX` are local conventions that operators must continue to use.)

Concerns (non-drift):

- `cfg.provider == "openai_compatible"` round-trips verbatim into `build_llm`'s `options` only via `routed_model()` — it is the upstream `litellm` fallback that decides whether the URL is actually used. Operators using `provider="openai_compatible"` need to either prefix their model name (`openai/<model>`) or rely on the `/anthropic` URL heuristic; the harness does not warn when `provider` is unrecognized.
- `cfg.api_key_env` is the var NAME (for observability), never the secret value — confirmed by reading the docstring at lines 51-53 and by the empty-string-vs-None handling on lines 64-67.
- The `temperature` clamp rejects values outside `[0, 2]` only at `from_env()` time. Setting `ModelBackendConfig(...)` directly bypasses the check (frozen dataclass, no `__post_init__`); the clamp is purely an env-validation rule.
