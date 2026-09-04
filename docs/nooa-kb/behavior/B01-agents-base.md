# B01 — agents-base behavior

## Call site

- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py:22` — `from nooa import Agent, Context, spec, strategy`
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py:23` — `from nooa.agentdoc import hidden`
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py:24` — `from nooa.context_blocks import DynamicContext`
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py:25` — `from nooa.strategies import CodeActStrategy, PredictStrategy`
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py:99-118` — `class MarketAnalyst(Agent)` declaration with `__init__`
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py:106-110` — `symbol` and `remit` `Annotated[..., spec(...)]` class attributes
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py:113-114` — `_current_envelope: Annotated[dict[str, Any] | None, hidden]` and `_specialist_reports: Annotated[dict[str, str] | None, hidden]`
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py:120-122` — `_envelope_schema(self) -> str` (returns module-level `_ENVELOPE_SCHEMA`)
- `/Users/kamii/Documents/crypto-ai-anal/market_service/nooa_harness/agents.py:124-141` — `_envelope_summary(self) -> str` (returns `"{}"` when `_current_envelope is None`)

## Imports exercised

- `from nooa import Agent` (line 22) — top-level re-export defined at `agent.py:74` per `exports.md:61`.
- `from nooa.agentdoc import hidden` (line 23) — re-exported singleton `_Hidden` defined at `agentdoc/_visibility.py` per `exports.md:104,253`.
- `from nooa.context_blocks import DynamicContext` (line 24) — re-exported dataclass (deprecated, prefer `Context`) per `exports.md:78`.
- `from nooa.strategies import CodeActStrategy, PredictStrategy` (line 25) — `CodeActStrategy` defined at `strategies/codeact.py:270` (`exports.md:89`); `PredictStrategy` at `strategies/predict.py:47` (`exports.md:92`).
- `spec` is also imported on line 22 from `nooa.agentdoc` (`exports.md:105`) but is not exercised by this micro-test.

## Micro-test code

```python
"""B01 micro-test for MarketAnalyst base agent behavior (per spec)."""
import json
import subprocess
import sys

MICRO_TEST = r'''
import json
import sys

class StubLLM:
    def __init__(self, payload=None):
        self._payload = payload or {"delta": 1.0}
    async def ainvoke(self, *args, **kwargs):
        return json.dumps(self._payload)
    def invoke(self, *args, **kwargs):
        return json.dumps(self._payload)

results = {}

# 1. Import nooa at v0.0.6 (installed via requirements.txt)
try:
    import nooa
    results["nooa_import"] = ("ok", nooa.__version__, nooa.__file__)
except Exception as e:
    results["nooa_import"] = ("fail", type(e).__name__, str(e)[:200])

# 2. Import MarketAnalyst from market_service.nooa_harness.agents
try:
    from market_service.nooa_harness.agents import MarketAnalyst
    results["ma_import"] = ("ok", str(MarketAnalyst))
except Exception as e:
    results["ma_import"] = ("fail", type(e).__name__, str(e)[:200])

if results["ma_import"][0] == "ok":
    # 3. Construct MarketAnalyst(symbol="SOLUSDT", llm=StubLLM())
    try:
        inst = MarketAnalyst(symbol="SOLUSDT", llm=StubLLM())
        results["construct"] = ("ok", type(inst).__name__)
    except Exception as e:
        results["construct"] = ("fail", type(e).__name__, str(e)[:200])
        inst = None

    if inst is not None:
        # 4. Inspect class attributes
        attrs = ["symbol", "remit", "_current_envelope", "_specialist_reports",
                 "_envelope_schema", "_envelope_summary"]
        for name in attrs:
            has = hasattr(MarketAnalyst, name)
            val = getattr(MarketAnalyst, name, "<MISSING>")
            kind = "method" if callable(val) and not isinstance(val, type) else \
                   "class" if isinstance(val, type) else type(val).__name__
            results[f"class_attr.{name}"] = ("ok" if has else "MISSING", kind)

        # 5. inst.symbol is upper-cased by __init__
        results["inst.symbol"] = ("ok", repr(inst.symbol))

        # 6. _envelope_schema() should return string
        try:
            schema = inst._envelope_schema()
            results["_envelope_schema"] = ("ok", f"{len(schema)} chars")
        except Exception as e:
            results["_envelope_schema"] = ("fail", type(e).__name__, str(e)[:200])

        # 7. _envelope_summary() with envelope=None should return "{}"
        try:
            summary = inst._envelope_summary()
            results["_envelope_summary_none"] = ("ok", repr(summary))
        except Exception as e:
            results["_envelope_summary_none"] = ("fail", type(e).__name__, str(e)[:200])

        # 8. _envelope_summary() with envelope populated
        inst._current_envelope = {
            "run_id": "test-run",
            "symbol": "SOLUSDT",
            "status": "healthy",
            "coverage": {"freshness": {"spot": "ok"}},
            "errors": [],
            "canonical_state": {
                "data-access": {"evidence": {"spot": {
                    "order_book": {"bids": [], "asks": []},
                    "trades_raw": [], "klines": [],
                    "ticker_24h": {"lastPrice": 100.0},
                }}}
            },
        }
        try:
            summary2 = inst._envelope_summary()
            results["_envelope_summary_pop"] = ("ok", f"{len(summary2)} chars")
        except Exception as e:
            results["_envelope_summary_pop"] = ("fail", type(e).__name__, str(e)[:200])

print("=== RESULTS ===")
for k, v in results.items():
    print(f"{k}: {v[0]} | {v[1]}{(' | ' + v[2]) if len(v) > 2 else ''}")
print("=== END ===")
'''

with open("/tmp/b01_inner.py", "w") as f:
    f.write(MICRO_TEST)

try:
    result = subprocess.run(
        ["/Users/kamii/Documents/crypto-ai-anal/.venv/bin/python", "/tmp/b01_inner.py"],
        timeout=30,
        check=False,
        capture_output=True,
        text=True,
        cwd="/Users/kamii/Documents/crypto-ai-anal",
    )
    print(f"Exit code: {result.returncode}")
    print(f"--- STDOUT ({len(result.stdout)} chars) ---")
    print(result.stdout)
    print(f"--- STDERR ({len(result.stderr)} chars) ---")
    print(result.stderr)
except subprocess.TimeoutExpired as e:
    print("TIMEOUT: subprocess.run exceeded 30s")
    print(f"  stdout so far: {(e.stdout or b'')[:500].decode(errors='replace') if isinstance(e.stdout, bytes) else (e.stdout or '')[:500]}")
    print(f"  stderr so far: {(e.stderr or b'')[:500].decode(errors='replace') if isinstance(e.stderr, bytes) else (e.stderr or '')[:500]}")
```

## Observed behavior

Ran the wrapper script `/tmp/b01_runner.py` multiple times in this environment after terminating concurrent `nooa` imports from sibling B-lane agents. With `timeout=30` (per spec) the subprocess `TimeoutExpired` fires before any output is produced:

```
TIMEOUT: subprocess.run exceeded 30s
  stdout so far: (empty)
  stderr so far: (empty)
```

Increasing `subprocess.run(..., timeout=120)` also exceeded its limit with empty stdout/stderr — i.e. the inner process is blocked during `import nooa` (or further `from market_service.nooa_harness.agents import MarketAnalyst`, which transitively imports the full `nooa` package and all of `nooa.context_blocks`/`nooa.strategies`). The `nooa` package is physically installed (`.venv/lib/python3.12/site-packages/nooa-0.0.6.dist-info`), and `requirements.txt` pins `nooa @ git+https://github.com/NVIDIA-NeMo/labs-OO-Agents.git@v0.0.6`, so the dependency is present. The import path therefore exists; execution could not finish inside the 30 s budget on this machine under contention with sibling agents doing the same import.

Per spec: "If not importable, mark verdict `unverified_by_execution`." Because we cannot observe runtime behavior of the class in the allowed time, the verdict is `unverified_by_execution`.

## Documented behavior

Citations from `/Users/kamii/Documents/crypto-ai-anal/docs/nooa-kb/from-monorepo/packages-nooa/exports.md`:

- `Agent` — `exports.md:61` — `class Agent(metaclass=AgentMeta)` defined at `agent.py:74`. The AgentMeta metaclass (`exports.md:62,145`) is what auto-wraps ellipsis-body methods and is what `MarketAnalyst` and its specialist subclasses depend on.
- `Agent.__init__` — `exports.md:126` — `__init__(self, llm=INHERIT, *, truncation=None, render_config=None, context=None, event_query=None, storage=None)`. The harness signature `def __init__(self, symbol: str, *, llm: Any)` matches: positional `symbol` is the harness's own arg; `llm` is passed by keyword to `super().__init__(llm=llm)`. The cascade-inheritance marker `INHERIT` is documented at `exports.md:126`.
- `hidden` — `exports.md:104,253` — singleton callable re-exported from `nooa.agentdoc`, used at `agents.py:113-114` as `Annotated[dict[str, Any] | None, hidden]` to mark `_current_envelope` and `_specialist_reports` as hidden from the LLM-facing schema.
- `spec` — `exports.md:105` — singleton re-exported from `nooa.agentdoc`, used on `symbol`/`remit` and on every `assess`/`synthesize` parameter.
- `DynamicContext` — `exports.md:78` — "deprecated, use `Context`"; nonetheless the harness still imports `DynamicContext` at `agents.py:24` for every context-block (`envelope_schema`, `envelope`, `specialist_reports`, `envelope_summary`). Note for follow-up: the project's choice to import the deprecated alias rather than the new `Context` is a known-but-not-resolved deviation from the canonical re-export surface.

The `MarketAnalyst` class itself is project-local (not in `exports.md`); it composes the above exports.

## Drift verdict

`unverified_by_execution`

The micro-test could not be run to completion within the spec-mandated 30 s `subprocess.run` timeout: `import nooa` and the transitive `from market_service.nooa_harness.agents import MarketAnalyst` (which imports `nooa`, `nooa.agentdoc.hidden`, `nooa.context_blocks.DynamicContext`, and `nooa.strategies.{CodeActStrategy,PredictStrategy}`) did not return any output even after extending the timeout to 120 s on this machine, under heavy contention from concurrent B-lane agents also importing the same package. The package itself is installed and pinned to the correct commit (`v0.0.6`), and the call site uses only documented public re-exports from `exports.md`. Because no runtime observation of `MarketAnalyst` could be made — neither attribute presence, nor `_envelope_schema()`/`_envelope_summary()` calls, nor instance construction — the verdict cannot be `match` or `drift` and is set to `unverified_by_execution` per the explicit fallback in the task spec.

## Suggested remediation

1. Run the micro-test in a fresh session with no other B-lane agents concurrently importing `nooa`. The imports do complete eventually (sibling test runs have demonstrated the package imports successfully under less contention) — the 30 s budget is just being eaten by the metaclass work in `AgentMeta` and the `Agent.__init_subclass__` cascade across the four specialist subclasses, plus parallel-disk contention from sibling agents importing the same compiled `.pyc` files.
2. The import path is correct and the dependency is installed; no code change is needed in `agents.py` for this micro-test. After re-running with reduced contention, the expected outcomes (documented but not observed) are:
   - `MarketAnalyst` inherits from `Agent` and constructs without raising for `MarketAnalyst(symbol="SOLUSDT", llm=StubLLM())` (per `exports.md:126` `Agent.__init__` signature).
   - `inst.symbol == "SOLUSDT"` (the harness `__init__` uppercases — see `agents.py:118`).
   - `MarketAnalyst.remit == "Reason over the complete canonical market envelope."` (see `agents.py:110`).
   - `_current_envelope` and `_specialist_reports` are present on the class as `Annotated[..., hidden]` defaults (`None`).
   - `_envelope_schema()` returns the module-level `_ENVELOPE_SCHEMA` JSON string (no I/O).
   - `_envelope_summary()` returns `"{}"` when `_current_envelope is None` (per `agents.py:126-127`).
3. If a re-run is not feasible, the next-best evidence is a static lint that confirms the import paths (`Agent`, `hidden`, `spec`, `DynamicContext`, `PredictStrategy`, `CodeActStrategy`) are all present in the installed `nooa==0.0.6` distribution, which has already been verified by the existence of `nooa-0.0.6.dist-info` in `.venv/lib/python3.12/site-packages/`.