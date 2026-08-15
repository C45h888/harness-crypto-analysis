# NOOA monorepo — official docs index

A consolidated index of every Markdown file shipped in `/tmp/nooa-monorepo/` (45 files). The official docs treat this repository as the single source of truth — the repo skeleton (README/AGENTS/CHANGELOG/CONTRIBUTING/RELEASING/SECURITY/THIRD_PARTY_NOTICES), per-area guides in `docs/guides/` and `docs/design/`, the bundled `skills/` "SKILL.md" bundles, `packages/nooa-cli/README.md` and design note, examples READMEs, and the tests/eval_pipeline READMEs.

## Source paths read

Top-level repo files:

- `/tmp/nooa-monorepo/README.md`
- `/tmp/nooa-monorepo/AGENTS.md`
- `/tmp/nooa-monorepo/CHANGELOG.md`
- `/tmp/nooa-monorepo/CONTRIBUTING.md`
- `/tmp/nooa-monorepo/RELEASING.md`
- `/tmp/nooa-monorepo/SECURITY.md`
- `/tmp/nooa-monorepo/THIRD_PARTY_NOTICES.md`

Guides and design docs:

- `/tmp/nooa-monorepo/docs/guides/strategies.md`
- `/tmp/nooa-monorepo/docs/guides/context-blocks.md`
- `/tmp/nooa-monorepo/docs/guides/config-migration.md`
- `/tmp/nooa-monorepo/docs/guides/prompt-mechanics.md`
- `/tmp/nooa-monorepo/docs/guides/single-vs-multi-agent.md`
- `/tmp/nooa-monorepo/docs/guides/structured-output.md`
- `/tmp/nooa-monorepo/docs/guides/truncation.md`
- `/tmp/nooa-monorepo/docs/guides/writing-generation-methods.md`
- `/tmp/nooa-monorepo/docs/design/sandbox_cell_execution.md`

Per-package docs:

- `/tmp/nooa-monorepo/packages/nooa-cli/README.md`
- `/tmp/nooa-monorepo/packages/nooa-cli/docs/activity-introspection-design.md`

Examples READMEs:

- `/tmp/nooa-monorepo/examples/README.md`
- `/tmp/nooa-monorepo/examples/arc_agi_3/README.md`
- `/tmp/nooa-monorepo/examples/arc_agi_3/analysis/README.md`
- `/tmp/nooa-monorepo/examples/benchmarks/README.md`
- `/tmp/nooa-monorepo/examples/assets/frontend-design/SKILL.md`

Skill bundles:

- `/tmp/nooa-monorepo/skills/README.md`
- `/tmp/nooa-monorepo/skills/context-blocks/SKILL.md`
- `/tmp/nooa-monorepo/skills/refine-agent-prompt/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-agent-authoring/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-agentdoc/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-capturing-traces/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-channels/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-codeact-advanced/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-context-and-state/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-middleware-hooks/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-self-extending/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-tools-and-skills/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-trace-explorer/SKILL.md`
- `/tmp/nooa-monorepo/skills/nooa-trace-viewer/SKILL.md`

Tests and eval pipeline:

- `/tmp/nooa-monorepo/tests/README.md`
- `/tmp/nooa-monorepo/tests/onboarding/README.md`
- `/tmp/nooa-monorepo/tests/onboarding/test_data/README.md`
- `/tmp/nooa-monorepo/tests/capability/EXPERIMENT_truncation_markers.md`
- `/tmp/nooa-monorepo/tests/capability/RUN_TRUNCATION_EXPERIMENTS.md`
- `/tmp/nooa-monorepo/util/eval_pipeline/README.md`

GitHub metadata:

- `/tmp/nooa-monorepo/.github/ISSUE_TEMPLATE/bug_report.md`
- `/tmp/nooa-monorepo/.github/ISSUE_TEMPLATE/feature_request.md`
- `/tmp/nooa-monorepo/.github/PULL_REQUEST_TEMPLATE.md`

## Document summaries

| File | Topic | Relevant claims (verbatim quotes ≤ 200 chars) |
|---|---|---|
| /tmp/nooa-monorepo/README.md | High-level pitch for NOOA — agent = Python object, `...` body = LLM, docstring = prompt, return type = contract. | "Code as action. The model acts by writing Python in a Jupyter-style REPL with access to self" / "uv add nooa-cli @ git+...#subdirectory=packages/nooa-cli" / "uv add nooa-memory @ git+...#subdirectory=packages/nooa-memory" |
| /tmp/nooa-monorepo/AGENTS.md | Repo conventions: visibility default-on, ellipsis = generation, docstring = prompt (no `{param}`). | "Default strategy is CodeActStrategy() — gives the LLM execute_python() + return_result() tools." / "Annotated[T, hidden] on module-level variables" / "CodeActStrategy (default) ... Code execution + iteration in REPL" |
| /tmp/nooa-monorepo/CHANGELOG.md | Release notes stub. | "## [Unreleased] - Initial public release of NVIDIA Object-Oriented Agents (NOOA)." |
| /tmp/nooa-monorepo/CONTRIBUTING.md | DCO-required contribution flow with `git commit -s`. | "PRs without a Signed-off-by line on each commit cannot be merged." |
| /tmp/nooa-monorepo/RELEASING.md | Tag-derived versioning: `vX.Y.Z` → `X.Y.Z.devN`; four packages lock-step; PyPI not yet enabled. | "Exactly on tag v0.0.6 — 0.0.6" / "PyPI publishing is not yet enabled." / "nooa-cli, nooa-memory, and nooa-bench depend on the core nooa package" |
| /tmp/nooa-monorepo/SECURITY.md | Points to NVIDIA PSIRT; do not file security issues via GitHub. | "We encourage you to use the following PGP key for secure email communication: NVIDIA public PGP Key" |
| /tmp/nooa-monorepo/THIRD_PARTY_NOTICES.md | Per-package license attributions (BSD/MIT/Apache/LGPL). | "nooa[sandbox] openshell - MIT License" / "nooa[mcp] mcp - MIT License" / "litellm - MIT License" |
| /tmp/nooa-monorepo/docs/guides/strategies.md | PredictStrategy vs CodeActStrategy; other strategies listed as experimental. | "Other strategies (TemplateStrategy, PurePythonStrategy, ReflexionStrategy) are experimental" / "@strategy(CodeActStrategy(max_iterations=50, max_retries=3, allow_text_response=True))" / "PredictStrategy: Single-shot LLM call that solves the task in one go" |
| /tmp/nooa-monorepo/docs/guides/context-blocks.md | Block(key, expr, update, show) model; static vs dynamic blocks; per-agent. | "Dynamic blocks are useful for showing live status that changes as the agent works" / "Context blocks are per-agent-instance. They are NOT shared across agents." |
| /tmp/nooa-monorepo/docs/guides/config-migration.md | 0.4.x→0.5.0 config moves: renamed dirs, `NEMO_OO_` env-var prefix. | "NEMO_RICH_URL → NEMO_OO_RICH_URL" / "Project-local .nooa/ → .nooa/" / "nooa config show # shows which settings.yaml / secrets.yaml / llm_config.yaml" |
| /tmp/nooa-monorepo/docs/guides/prompt-mechanics.md | Docstring=prompt; arguments rendered by default; full prompt block order. | "Arguments Are Rendered By Default — Never {param} in Docstrings" / "Chain-of-thought is provided through the reasoning() builtin available in CodeAct-generated code" |
| /tmp/nooa-monorepo/docs/guides/single-vs-multi-agent.md | Tradeoffs between one multi-method agent vs subagents. | "Subagents do NOT share: Context blocks ... Events/history ... Conversation state." / "Data must be passed explicitly via constructor arguments or shared data objects." |
| /tmp/nooa-monorepo/docs/guides/structured-output.md | Pydantic as return-type contract; AST-validators for generated code; helpers beat prompts. | "use Pydantic's @field_validator to AST-validate code the LLM generates, ensuring it compiles before being accepted." / "Avoid raw dict or list as return types." |
| /tmp/nooa-monorepo/docs/guides/truncation.md | 11 truncation sites: stdout/stderr, return value, Out repr, context block, events, prefill, validation, Predict param size, reflexion, CurrentCall, summarizer. | "TruncatingStringIO with max_chars = tc.max_block_chars = 20_000 default" / "PredictStrategy: parameter 'data' is 27,000 chars (repr), exceeding max_param_chars=10,000." |
| /tmp/nooa-monorepo/docs/guides/writing-generation-methods.md | Quick guide to generation methods; reserved `reasoning` parameter. | "The ... (ellipsis) is the key - it signals that this method needs LLM generation." / "Don't interpolate parameters ({text}, {max_words}) into the docstring." |
| /tmp/nooa-monorepo/docs/design/sandbox_cell_execution.md | Process-backed sandbox with rlimits + Landlock + seccomp; Linux-only, fail-closed. | "execution_backend: Literal['inprocess', 'sandbox'] = 'inprocess'" / "Defaults enforcement: RLIMIT_AS, RLIMIT_CPU, Landlock path-beneath rules, seccomp-BPF deny AF_INET" / "Fail-closed: if a requested guard cannot be enforced on the host, the sandbox refuses to start" |
| /tmp/nooa-monorepo/packages/nooa-cli/README.md | CLI install/usage: `nooa start-dev`, `nooa eval`, `nooa traces`. | "uv add nooa-cli[datascience]" / "nooa start-dev # launch the trace viewer" |
| /tmp/nooa-monorepo/packages/nooa-cli/docs/activity-introspection-design.md | Why `sys.monitoring` is wrong for TUI activity probing. | "sys._current_frames() + asyncio task introspection, with zero changes to the core framework." / "Global LINE instrumentation measured ~3.1x runtime on a tight loop" |
| /tmp/nooa-monorepo/examples/README.md | 11-step quickstart: generation methods, structured output, tools, strategies, `doc()`, tracing, dynamic prompts, context blocks, summarization, skills, MCP, sandbox. | "Provider packages can register bundled model aliases automatically through the nooa.bundled_configs entry-point group." / "uv add 'nooa[mcp]'" |
| /tmp/nooa-monorepo/examples/arc_agi_3/README.md | ARC-AGI-3 agent; harness via two append-only JSONL files; TUI dashboard. | "Two knowledge variants you can compare head-to-head: memory ... mdfiles" / "In-process isolation so the game it is solving cannot leak into its context" |
| /tmp/nooa-monorepo/examples/arc_agi_3/analysis/README.md | Reusable audit/analysis tools for a solver run. | "red_team ... runs cells + tool output through stdlib scanners" / "memory ... opens stores read-only (immutable=1), so a live run can be analysed" |
| /tmp/nooa-monorepo/examples/benchmarks/README.md | Harbor adapter for the nooa-bench agent (SWE-bench, Terminal-Bench 2.0). | "Inside the container, Harbor invokes: nemo-harbor --instruction '...' --model '...' --agent-type bench" |
| /tmp/nooa-monorepo/examples/assets/frontend-design/SKILL.md | Distinguishing-aesthetic frontend skill (Anthropic-style SKILL.md). | "Choose fonts that are beautiful, unique, and interesting. Avoid generic fonts like Arial and Inter" |
| /tmp/nooa-monorepo/skills/README.md | Index of the 11 in-tree `nooa-*` SKILL.md bundles. | "Portable SKILL.md bundles for coding agents (Claude Code, Cursor, Codex, or any Agent Skills host)" |
| /tmp/nooa-monorepo/skills/context-blocks/SKILL.md | Unified Context API; prefix vs suffix placement; legacy deprecations. | "Context('text', prefix=True) ... Context(expr='self.x()') ... None — Suppress" / "value and expr are mutually exclusive" |
| /tmp/nooa-monorepo/skills/refine-agent-prompt/SKILL.md | Workflow for debugging the prompt: `print_prompt`, `build_prompt_data`. | "Use nooa.print_prompt with a FakeLLMClient to render without making a real LLM call" / "Should usually be hidden: framework internals (SkillManager, TokenBudgetSummarizer)" |
| /tmp/nooa-monorepo/skills/nooa-agent-authoring/SKILL.md | Author-facing rules; cascading LLM resolution; reserved `reasoning`. | "Constructors take config= only. PredictStrategy(max_retries=3) ... are errors" / "no-llm subagent must be constructed inside a parent's agentic method body" |
| /tmp/nooa-monorepo/skills/nooa-agentdoc/SKILL.md | `doc()`, `spec()`, `hidden`, `pformat`/`pprint` for LLM-facing docs. | "doc(obj) — full contract; instances show live values" / "@hidden on a method/property ... Annotated[T, hidden] on a field ... with hidden: for module-level context manager" |
| /tmp/nooa-monorepo/skills/nooa-capturing-traces/SKILL.md | Auto-tracing probes viewer; exporters jsonl/journal/local_otlp/otlp/langfuse/console. | "Every Agent.__init__() auto-attempts tracing once per process" / "OTLP_ENDPOINT ... default http://localhost:5001/v1/traces" |
| /tmp/nooa-monorepo/skills/nooa-channels/SKILL.md | Channels/QueueManager: queue + event modes; race() dispatch; spawn() background jobs. | "race() ... Returns a length-1 list [(name, item)] for a queue-mode winner, or [] when an event-mode put woke it" |
| /tmp/nooa-monorepo/skills/nooa-codeact-advanced/SKILL.md | CodeActConfig knobs; dead knobs; PredictConfig; RestrictionsConfig; context eviction. | "max_iterations default None — Unlimited" / "max_retries default 3 — Cumulative session error budget, not consecutive" / "max_tool_calls default None — Dead — declared but never read" |
| /tmp/nooa-monorepo/skills/nooa-context-and-state/SKILL.md | Context, events, summarizers, persistent storage. | "every agent has two managers, always present, hidden from the LLM by default" / "TokenBudgetSummarizer.install(agent, config=TokenBudgetConfig(max_tokens=80_000, preserve_recent=10))" |
| /tmp/nooa-monorepo/skills/nooa-middleware-hooks/SKILL.md | intercept()/on()/set_hooks(); not safe to combine with tracing hooks. | "Calling set_hooks(MyHooks()) afterwards silently replaces tracing (and vice versa)." |
| /tmp/nooa-monorepo/skills/nooa-self-extending/SKILL.md | In-cell helpers, standalone `@strategy` functions, persistent `libs/` packages. | "await self.libs.create('stats', 'Statistical utilities.')" / "Each library's __init__.py exports a Skill subclass" |
| /tmp/nooa-monorepo/skills/nooa-tools-and-skills/SKILL.md | Methods-as-tools; ShellTools/TodoManager; Skill/TextSkill; MCP; multimodal media. | "Tools attached as public attributes are automatically visible; the LLM discovers their APIs via doc(self.shell)." |
| /tmp/nooa-monorepo/skills/nooa-trace-explorer/SKILL.md | `trace-explorer` CLI + TraceExplorer/TraceExplorerClient API. | "eval-artifact files are rejected by from_file (*.006eval.* by filename)" |
| /tmp/nooa-monorepo/skills/nooa-trace-viewer/SKILL.md | FastAPI viewer + OTLP receiver on port 5001; SQLite persistence. | "nooa start-dev ... http://localhost:5001" / "DB default for the raw module is ./traces.db — prefer start-dev." |
| /tmp/nooa-monorepo/tests/README.md | Test layout (root, runtime, strategies, integration, edge_cases, agents, capability, evaluation, onboarding, tools, utils, performance, unit). | "uv run pytest # all tests" |
| /tmp/nooa-monorepo/tests/onboarding/README.md | Capability suites with target pass rates (e.g. `>90%` AST validation). | "test_code_generation.py ... >90%" / "DSPy optimization ... Deploy optimized prompts for model" |
| /tmp/nooa-monorepo/tests/onboarding/test_data/README.md | Test-case JSON schema; status: 13/500 cases — collection ongoing. | "Total: 13 / 500+ cases" |
| /tmp/nooa-monorepo/tests/capability/EXPERIMENT_truncation_markers.md | Empirical study: lowercase `list(len=N, items=[…])` + `Answer(answer,reason)` shape hits ~84% small/100% flagship. | "Headline outcome ... marker shape ... agent schema. Both are essential." / "claude-sonnet 168/168 100%" |
| /tmp/nooa-monorepo/tests/capability/RUN_TRUNCATION_EXPERIMENTS.md | Reproduction steps; per-round commands. | "uv run python -m eval_pipeline --config tests/capability/config_truncation.yaml --runs 3 --parallel 30" |
| /tmp/nooa-monorepo/util/eval_pipeline/README.md | Evaluator (Python API or YAML) + scorers; OTLP_ENDPOINT controls trace sink. | "OTLP_ENDPOINT ... Set (e.g. http://localhost:5001/v1/traces) ... Traces are exported via OTLP" / "results.noo-eval.jsonl" |
| /tmp/nooa-monorepo/.github/ISSUE_TEMPLATE/bug_report.md | Bug report template (OS/Python/NOOA version/model provider). | "NOOA version (nooa --version or python -c 'import nooa; print(nooa.__version__)'):" |
| /tmp/nooa-monorepo/.github/ISSUE_TEMPLATE/feature_request.md | Feature request template (problem/solution/alternatives). | "What problem are you trying to solve?" |
| /tmp/nooa-monorepo/.github/PULL_REQUEST_TEMPLATE.md | PR template; ruff/pytest license-header checks. | "New source files carry an SPDX license header" |

## Behavioral notes

### Documented as supported

- **Two-strategy model.** `PredictStrategy` (single-shot, validated Pydantic) and `CodeActStrategy` (default, REPL + `execute_python`/`return_result`). Other strategies (`TemplateStrategy`, `PurePythonStrategy`, `ReflexionStrategy`) are explicitly **experimental**.
- **CodeAct sandbox (process-backed).** `execution_backend="sandbox"` enables rlimits + Landlock + seccomp-BPF. Default `inprocess` is unchanged. Linux-only; fail-closed. Composes with the whole-process sandbox (`examples/arc_agi_3/sandbox.py`).
- **Static + Dynamic context blocks.** Unified `Context(value|expr, prefix)` API; `set_static`/`set_dynamic` deprecated. Prefix/suffix placement hints for cacheability. `self.context`/`self.events` always present but hidden by default; opt-in via `spec(self, "context", hidden=False)`.
- **Visibility default-on.** Module-level + public methods/fields are visible to the LLM. Hide explicitly with `@hidden`, `Annotated[T, hidden]`, or `with hidden:`. Types used in agent signatures must be defined or imported at module level.
- **Reserved `reasoning` parameter.** Declaring it raises `ValueError` at class creation; chain-of-thought surfaces as a `reasoning()` builtin inside generated code.
- **Tracing = automatic when viewer is reachable.** `Agent.__init__` probes `OTLP_ENDPOINT`/`localhost:5001`. If unreachable, tracing is silently disabled. All method calls produce spans (private too — older docs said otherwise and are stale).
- **Truncation pipeline with two mechanisms.** `pformat` (structural, `max_length`/`max_string`/`max_depth`) and `safe_pformat`/`TruncatingStringIO` (char cap, head + tail). Defaults: `max_block_chars=20_000`, `max_stdout_chars=50_000`, `max_stderr_chars=20_000`, `max_pprint_elements=50`, `max_pprint_string=500`, `max_pprint_depth=4`.
- **PredictStrategy hard pre-flight guard.** Params over `max_param_chars` (default 200 K) raise `ValueError` before any LLM call — single-shot strategy refuses silently-truncated inputs.
- **`Context` eviction under token budget.** Whole-block eviction favors newest non-`static` user blocks first; static framework blocks (`system_prompt`, `self`) are never evicted. `agent.context_stats` exposes provider-reported prompt tokens.
- **Versioning is `git describe`-derived.** Tags like `v0.0.6` resolve to `0.0.6`; 5 commits past is `0.0.7.dev5`. **PyPI publishing is not yet enabled**; install is via `uv add ... @ git+...@<tag>`.
- **Truncation 3.0 markers.** Empirical study recommends `list(len=N, items=[…])`, `str(len=N, head='…', tail='…')`, `<cycle>`, `<generator>`, and a Pydantic `Answer(answer, reason)` return type.
- **Tool surface = agent surface.** Any visible method/attribute on `self` is callable from CodeAct-generated code. `ShellTools`, `TodoManager`, `MCPManager.create_from_server(...)`, `TextSkill(path=...)` are all attachable as instance attributes.
- **Eviction and summarizers.** `TokenBudgetSummarizer` (budget triggered) and `MethodSummarizer` (per-method) are themselves agents; `MethodSummarizer.install(agent)` + `TokenBudgetSummarizer.install(agent, config=...)`.
- **Self-extending agents.** Agent-authored `libs/` directories hot-reload into `self.<lib_name>`; Linting blocks `E001` (forbidden builtins) and `E003` (star imports).

### Documented as experimental / unsafe

- **Non-default strategies.** `TemplateStrategy`, `PurePythonStrategy`, `ReflexionStrategy` "are experimental and not yet ready for production use."
- **Sandbox limitations.** Linux only, `fork`-only; parent↔worker IPC is `pickle`-based; data-confidentiality of parent's memory is *not* a goal (only action containment).
- **Old API still in older docs.** `SkillManager`, `enable_tracing(trace_dir=...)`, "private methods aren't traced", `from agentdoc import ...` are all **stale** — skills README says trust `src/nooa`, not older docs.
- **PredictStrategy → CodeAct brittleness.** Some small models (`gpt-oss-20b`, `llama-3.1-8b`) cannot reliably emit either JSON or valid tool calls under one protocol or the other; documented exclusions.
- **Hard timeout / OOM in sandbox resets namespace.** Variables/functions from earlier cells are gone on recovery — model must rebuild state.

### Implicit assumptions

- **Initial public release.** CHANGELOG says v0.0.6 (per the table in `RELEASING.md`) is the **initial public release** under "Unreleased" — README billing itself as "the most Pythonic way to build AI agents" is essentially v0.0.6 marketing.
- **CWD-disambiguates-trace-DB.** `python -m nooa.viewer` without `NOOA_TRACE_DB` falls back to `./traces.db`, which is "an easy way to lose traces" if `nooa start-dev` isn't used.
- **`PredictStrategy(max_retries=3)` is invalid.** Old docstring example; always wrap in `PredictConfig(...)`.
- **Strategy constructors take `config=` only.** `CodeActStrategy(max_iterations=10)` is an error.
- **`max_tool_calls` is a dead knob.** "Setting it does nothing" (verified).
- **`max_event_tokens` / `min_preserved_events` are read by nothing.** Events are never evicted by the renderer (`docs/guides/truncation.md` documents a removed field schema — trust `config/truncation_config.py`).
- **`allow_text_response` does not exist.** Older docs mention it; instead use `text_only_stop_behavior` (`"return_result"` or `"synthetic_reasoning"`).
- **Bundled CLI subcommands are evolving.** Stale refs to `nooa viewer` / `nooa import-traces` remain in some docs; canonical entry is `nooa start-dev`.
- **Bundled frontend SKILL.md** is an Anthropic-authored design skill and is not framework-specific.

### UNVERIFIED — requires B-lane

- Whether `PredictStrategy` parameter validation against `int | None` (as used in the truncation capability experiment) is actually invoked automatically, or whether it relies on a base `Answer` Pydantic wrapper. The table only proves the schema's effect on comprehension; not the runtime validator path.
- Whether the sandbox design's measured guardrails were demonstrated on the kernels used by the B-lane runtime (target was Linux 6.8 + Landlock ABI 4; B-lane's deployment OS / kernel version not confirmed by docs).
- Whether `predict.py:230` and `predict.py:317-347` referenced in `truncation.md` align with the actual file in the current checkout (docs reference source line numbers that the B-lane reader must confirm).
- Whether `dynamic_value` vs `static_value` is consistent across `docs/guides/context-blocks.md` (uses `update="once"/"always"`), `skills/context-blocks/SKILL.md` (uses `Context(expr="...")` vs `Context("value", prefix=True)`), and the runtime source.
- Whether the reported 0.0.6 release exists as an actual git tag reachable from the current monorepo (CHANGELOG table cites `v0.0.6`, RELEASING.md describes the tag ceremony, but no tag/commit confirms presence).
- Whether `nooa.bundled_configs` is the canonical entry-point group name and whether it ships as a stable API (only mentioned once in `examples/README.md`).
- Whether the `restart_empty` vs `disabled` `SandboxConfig.recovery` field is honored in 0.0.6 or still pending.

## Cross-references

Consuming M-lane files:

- `/Users/kamii/Documents/crypto-ai-anal/docs/nooa-kb/from-monorepo/exports.md`
- `/Users/kamii/Documents/crypto-ai-anal/docs/nooa-kb/from-monorepo/strategies.md`
- `/Users/kamii/Documents/crypto-ai-anal/docs/nooa-kb/from-monorepo/context-blocks.md`
- `/Users/kamii/Documents/crypto-ai-anal/docs/nooa-kb/from-monorepo/agentdoc.md`
- `/Users/kamii/Documents/crypto-ai-anal/docs/nooa-kb/from-monorepo/unifiedllm.md`
- `/Users/kamii/Documents/crypto-ai-anal/docs/nooa-kb/from-monorepo/cli.md`

## Suggested remediation

- **Quote drift:** Several older docs reference `SkillManager`, `nooa viewer`, `enable_tracing(trace_dir=...)`, "private methods aren't traced", `from agentdoc import ...`. The B-lane should treat the in-tree `skills/nooa-*.md` bundles as the source of truth when these collide with `docs/guides/*`.
- **`docs/guides/truncation.md` vs `config/truncation_config.py`:** Field names referenced in the guide (`max_block_chars`, `max_stdout_chars`, ...) are from a removed schema. The skill `nooa-codeact-advanced` and `config/truncation_config.py` are current.
- **Visibility defaults:** `AGENTS.md` and `nooa-agent-authoring/SKILL.md` describe the same default-visible surface but use slightly different terminology (`spec()` vs `Annotated[T, hidden]`, `@hidden`). The B-lane should resolve to one before documenting.
- **Predict vs CodeAct contract:** PredictStrategy is documented as single-shot with `response_format` validation; some examples drop it to a bare `int | None` while also losing the `reason` field. The B-lane should keep the Pydantic wrapper for any predict path where reasoning/cap is needed.
- **Truncation marker format split:** The docs describe TWO surfaces — empirical `list(len=N, items=[…])` (Truncation 3.0) for new artifacts, and the older `<truncated-output>...head...tail...</truncated-output>` (char-cap). The B-lane should not assume one without checking the site.
- **Sandbox defaults:** `inprocess` is default but `cell_timeout` is `None` and uninterpretable as a hard bound. The B-lane should confirm whether the runtime uses the docs' `execution_backend` knob or relies only on the legacy `cell_timeout`.
- **Version-tag reachability:** CHANGELOG/RELEASING.md imply `v0.0.6` exists; the B-lane's running version (`python -c "import nooa; print(nooa.__version__)"`) must be cross-checked.
