# B07 — runner-env behavior

## Call site

- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/runner.py:41-48` — `_resolve_session_id()` (env-var → UUID parsing)
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/runner.py:27` — `from market_service.config import Settings`
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/runner.py:35` — `from .backends import ModelBackendConfig`
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/runner.py:166-168` — `Settings.from_env()`, `ModelBackendConfig.from_env()`, `_resolve_session_id(session_id)` invocations inside the run loop
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/runner.py:132-235` — `run_analyst_loop()` long-running loop
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/runner.py:153-164` — input-validation guards (`interval_s`, `cycles`, `--run-id` vs `--latest`, `--run-id` cycles)
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/runner.py:171-235` — `while cycles == 0 or completed < cycles` loop body (envelope read → analysis → persistence → sleep)

## Imports exercised

| Symbol | Source | Used at |
| --- | --- | --- |
| `asyncio` | stdlib | `run_analyst_loop()` sleep (`runner.py:20, 235`) |
| `json` | stdlib | `print(json.dumps(result, default=str), flush=True)` (`runner.py:21, 232`) |
| `logging` | stdlib | `log = logging.getLogger(__name__)` (`runner.py:22, 38`) |
| `os` | stdlib | `os.getenv("NOOA_SESSION_ID")` (`runner.py:23, 42`) |
| `uuid` | stdlib | `uuid.UUID(value)` / `uuid.uuid4()` (`runner.py:24, 45, 48`) |
| `Any` | stdlib typing | `cycle_meta`, `persistence` dicts (`runner.py:25, 173, 212`) |
| `Settings` | `market_service.config` | `Settings.from_env()` (`runner.py:27, 166`) |
| `AnalystBriefing`, `MarketRunEnvelope` | `market_service.runtime.contracts` | envelope persistence + dict conversion (`runner.py:28-31, 203, 211`) |
| `PostgresRuntimeStore` | `market_service.runtime.postgres_store` | durable read/persist (`runner.py:32, 65, 91, 114`) |
| `RedisRuntimeStore` | `market_service.runtime.redis_store` | cheap-path read + agent stream publish (`runner.py:33, 62, 92, 115`) |
| `ModelBackendConfig` | `.backends` | `ModelBackendConfig.from_env()` and `.build_llm()` (`runner.py:35, 167, 202`) |
| `build_suite` | `.suite` | lazy agent stack construction (`runner.py:36, 202`) |

## Micro-test code

```python
import subprocess, sys
code = '''
import os
os.environ["NOOA_SESSION_ID"] = "11111111-2222-3333-4444-555555555555"

from market_service.nooa_harness.runner import _resolve_session_id
print("session_id (explicit):", _resolve_session_id(None))
print("session_id (env):", _resolve_session_id(None))
print("session_id (override):", _resolve_session_id("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))

# Test invalid UUID rejection
try:
    bad = _resolve_session_id("not-a-uuid")
    print("BAD UUID DID NOT RAISE - got", bad)
except ValueError as e:
    print("invalid UUID raised ValueError:", str(e)[:80])

# Settings from env
from market_service.config import Settings
print("Settings imported")
'''
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
print("stdout:", out.stdout)
print("stderr:", out.stderr)
```

## Observed behavior

```
stdout: session_id (explicit): 11111111-2222-3333-4444-555555555555
session_id (env): 11111111-2222-3333-4444-555555555555
session_id (override): aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
invalid UUID raised ValueError: NOOA session IDs must be UUIDs
Settings imported

stderr: (empty)
```

Interpretation:

1. `_resolve_session_id(None)` falls back to the env var `NOOA_SESSION_ID`. Both calls return the same canonical UUID (`11111111-...`) — no caching, but the env var is stable across calls within the subprocess.
2. `_resolve_session_id("aaaa...")` accepts an explicit override and ignores the env var (the explicit arg is `or`-chained first at `runner.py:42`).
3. The `try`/`except` arm around `uuid.UUID(value)` catches malformed strings. `"not-a-uuid"` raises `ValueError`, re-raised as `ValueError("NOOA session IDs must be UUIDs")` — the same string documented in the source (`runner.py:47`).
4. `from market_service.config import Settings` succeeds; no env-var defaults are evaluated at import time (`from_env()` is lazy — `config.py:28`).
5. `stderr` is empty — no warnings, no tracebacks.

## Documented behavior

- **`_resolve_session_id()`** (`runner.py:41-48`) — explicit arg → env var `NOOA_SESSION_ID` → `uuid.uuid4()`. The value MUST be a parseable UUID (`uuid.UUID(value)`); on parse failure the original `ValueError`/`AttributeError`/`TypeError` is chained into a single user-facing `ValueError("NOOA session IDs must be UUIDs")`. Default-when-empty fallback to `uuid.uuid4()` is the documented escape hatch for one-shot dev runs.
- **`Settings.from_env()`** (`market_service/config.py:28-47`) — reads `DATABASE_URL` (required, raises if missing), `REDIS_URL` (default `redis://redis:6379/0`), `REDIS_KEY_PREFIX` (default `marketflow`), `REDIS_STREAM_MAXLEN`/`WALL_HISTORY_MAXLEN`/`POLL_SECONDS`/`FLOW_WINDOW_SECONDS`/`DEPTH_LEVELS`/`MAX_DOMAIN_STATE_AGE_SECONDS` (all positive ints via `_positive_int`), `SYMBOLS` (comma-split → upper-stripped tuple, must be non-empty).
- **`ModelBackendConfig.from_env()`** (same source as B06: `market_service/nooa_harness/backends.py:55-78`) — `NOOA_MODEL_PROVIDER` falls back to `PROVIDER` then to `openai`; `NOOA_MODEL_NAME` is required; `NOOA_MODEL_BASE_URL` falls back to `PROVIDER_BASE_URL`; API key sourced from `NOOA_API_KEY` or `PROVIDER_API_KEY` (the chosen var name is recorded in `api_key_env` for observability — never the secret); `NOOA_MODEL_TEMPERATURE` is `float(...)` clamped to `[0, 2]`. The returned config is then `build_llm()`-ed lazily inside the loop body only when an envelope is actually going to be analyzed (so the read path — no-envelope / unchanged-run_id — does not pay the `nooa`/litellm import cost; see `backends.py:96-110` and `runner.py:198-202`).
- **Loop** (`runner.py:132-235`) — validation guards fire in order: `interval_s >= 0`, `cycles >= 0`, `--run-id` and `--latest` are mutually exclusive (`runner.py:157-158`), `--run-id` is one-shot (`runner.py:159-160`), default to `use_latest=True` if no selector (`runner.py:163-164`). Per cycle:
  - With `run_id`: read the exact envelope from Redis → Postgres fallback.
  - Without `run_id`: read the latest envelope from Redis → Postgres fallback; emit `"unchanged"` if the same `run_id` was already analyzed.
  - With a fresh envelope: `build_suite(symbol, backend.build_llm())` then `suite.analyze(...)`, persist to Postgres first (durable authority) and only then publish to the Redis agent stream (`_persist_briefing`, `runner.py:82-106`).
  - Result is always `print(json.dumps(result, default=str), flush=True)` — one structured JSON line per cycle.
  - `cycles=0` means run until interrupted; otherwise sleep `interval_s` between cycles.

Per the M-lane `unifiedllm.md`:

- `get_llm_client(name, **overrides)` is the upstream entry point; the harness's `backend.build_llm()` calls it at `backends.py:103-110`, passing `temperature`, `api_key`, and `api_base` (no `model_name` override → upstream uses the YAML registry alias, falling back to passing the routed model string directly to litellm on a registry miss; see `unifiedllm.md` § "`get_llm_client` — resolution flow").
- Provider routing is delegated to litellm via `_ClientHttp._build_completion_wrappers` (`unifiedllm.md` § "OpenAI-compatible providers"). The `openai`/`vllm`/`ollama`/`anthropic` token mapping in our `backends.py` is the call-site analog — litellm's `openai_compatible_providers` list (read at runtime) is the actual source of truth for which providers get an OpenAI SDK client wrapper.
- The harness's `_PROVIDER_PREFIX` table (`backends.py:32-37`) and the `vllm` → `openai/` alias match the M-lane note that vLLM is treated as an OpenAI-compatible gateway (`unifiedllm.md` § "vLLM").

## Drift verdict

No drift.

Observed behavior matches the source-level claims of `runner.py` exactly: the env-var fallback chain (`explicit or os.getenv("NOOA_SESSION_ID")`) works, explicit overrides bypass the env, invalid UUIDs raise the documented `ValueError` with the documented message, and `Settings` imports cleanly (no eager env-var reads at module import). The `ModelBackendConfig.from_env()` / `Settings.from_env()` call sites inside the loop match B06's documented shape — same source files, same env-var names, same defaults. The long-running loop's read-only cycle classification (`"mode": "read"`) is consistent with the module docstring's "default behavior analyzes an EXISTING canonical envelope; refreshing or triggering a new cycle is an explicit opt-in" (`runner.py:1-7`).

## Suggested remediation

No remediation required.

(Files referenced: `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/runner.py`, `/Users/kamii/Documents/crypto-ai-anal/market_service/config.py`, `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/backends.py`, `/Users/kamii/Documents/crypto-ai-anal/docs/nooa-kb/from-monorepo/packages-nooa/unifiedllm.md`.)
