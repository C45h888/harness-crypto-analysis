# GitHub issues & discussions on NVIDIA-NeMo/labs-OO-Agents

## Sources retrieved

- https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues?q=is%3Aissue+is%3Aopen — retrieved 2026-08-15 — first-page open-issues listing (12 visible of 25 open).
- https://github.com/NVIDIA-NeMo/labs-OO-Agents/discussions — retrieved 2026-08-15 — Discussions tab; no discussion threads were returned (404 / empty on multiple probes).
- https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30 — retrieved 2026-08-15 — richer listing combining open issues and open PRs with description snippets.
- https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/111 — retrieved 2026-08-15 — full body of capability-aware UnifiedLLM selection enhancement.
- https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/110 — retrieved 2026-08-15 — full body of Windows ShellTools / hardcoded /bin/bash bug.
- https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/109 — retrieved 2026-08-15 — full body of litellm manylinux-only-wheels / Rust compile cost on macOS/Windows.
- https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/125 — retrieved 2026-08-15 — full body of inert TruncationConfig.max_event_tokens / min_preserved_events bug.
- https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/114 — retrieved 2026-08-15 — full body of agent_call middleware skipping synchronous Agent methods.

## Claims

- [confidence: high] Issue #150 (Aug 15, 2026) requests a beginner-friendly tutorial introducing NOOA from a Python OOP perspective (agents, state, methods, tools); labelled `enhancement` — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues?q=is%3Aissue+is%3Aopen
- [confidence: high] Issue #144 (Aug 14, 2026) argues that a pure-Python `run()` orchestrator cannot resume across a process boundary and asks whether a field-ledger pattern is the intended solution — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Issue #129 (Aug 12, 2026) proposes refactoring text skills into ShellTools-backed skill objects — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues?q=is%3Aissue+is%3Aopen
- [confidence: high] Issue #128 (Aug 12, 2026) seeks a safe Python-package reload model when multiple agents coexist in one process — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues?q=is%3Aissue+is%3Aopen
- [confidence: high] Issue #125 reports that `TruncationConfig.max_event_tokens` and `TruncationConfig.min_preserved_events` "are documented, validated, and read by nothing" — no eviction logic uses them and the real budget relies only on `max_context_tokens` — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/125: "the fields are described in documentation as providing token budget enforcement and event preservation guarantees, yet no eviction logic uses these values"
- [confidence: high] Issue #124 (Aug 10, 2026) reports that the TUI user-message preview causes agents to answer twice; addressed by PR #146 which adds a `preview_content` flag — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Issue #114 (bug) reports `agent_call` middleware skips synchronous Agent methods even though it is documented to wrap "entire agent method execution"; comment in code states "middleware is async and would need an event loop" — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/114: "the sync wrapper explicitly skips `agent_call` middleware"
- [confidence: high] Issue #111 (enhancement) requests capability-aware UnifiedLLM client selection so that strategies like CodeAct declare LLM requirements (function tools, structured results, multi-turn tools) and UnifiedLLM resolves these into a plan rather than defaulting to CompletionClient — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/111: "OpenAI rejects the Chat Completions request with function tools, but works with ResponsesClient"
- [confidence: high] Issue #110 reports `ShellTools.run()` cannot start on Windows because `/bin/bash` is hardcoded at `src/nooa/tools/_bash_session.py:164`; also notes the protocol depends on file descriptor 3 which is POSIX-specific — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/110: "/bin/bash is hardcoded as the executable path"
- [confidence: high] Issue #109 reports `litellm==1.93.0` ships manylinux-only wheels, so installing nooa on macOS or Windows falls back to source distribution and compiles a Rust/pyo3 extension; on Apple M1 measured ~39s wall / 145.6s user time producing a ~5.7MB `.so` — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/109: "litellm==1.93.0 only publishes manylinux wheels (Linux-only)"
- [confidence: high] Issue #104 (Aug 6, 2026) reports that `nooa-memory` README cites `examples/memory_bench/` for its measured results but that directory is not present in the repo — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues?q=is%3Aissue+is%3Aopen
- [confidence: high] Issue #97 (enhancement, Aug 4, 2026) requests regression tests for `_classify_worker_death()` and `probe_capabilities()` Windows platform guards — https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues?q=is%3Aissue+is%3Aopen
- [confidence: high] Open PR #147 (feat) introduces `LLMRequirements` and auto-resolves GPT-5-style clients to `ResponsesClient`, addressing issue #111 — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Open PR #146 (fix) adds a `preview_content` flag to interactive chat so that queued user/system messages are reported as metadata instead of being sent to the model twice, addressing issue #124 — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Open PR #143 (feat) introduces a new `nooa-acp` package serving durable coding sessions over the Agent Client Protocol and is the only item with a +1 reaction — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Open PR #137 raises an explicit Unix-only `RuntimeError` for `BashSession` instead of a cryptic `AssertionError` on Windows — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Open PR #135 wraps the Unix-only `resource` module import for Windows compatibility — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Open PR #132 adds cross-platform fallbacks for `fcntl` and `signal.SIGUSR2` to fix fatal Windows import errors in sqlite and debug_handler — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Open PR #126 is a docs-only PR clarifying that CodeActStrategy/PredictStrategy take `config=` and not keyword arguments — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Open PR #120 fixes generator method dispatch in the metaclass (separate wrappers for generator methods) — https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30
- [confidence: high] Discussions tab for `NVIDIA-NeMo/labs-OO-Agents` is either disabled or returns 404 / no listing through multiple probes — https://github.com/NVIDIA-NeMo/labs-OO-Agents/discussions

## Conflicts with monorepo readers (M-lane)

- (none observed in the I3 evidence set)

## Speculative notes

- [SPECULATION] The cluster of open issues #110, #109, #135, #137, #132 plus enhancement #97 suggests Windows/macOS support is an active but unfinished area, and that a v0.0.7-style release will likely bundle PRs #132/#135/#137 to clear most platform blockers — inferred from issue and PR titles on https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30.
- [SPECULATION] The PR-126 docs fix implies recent rename/refactor changed `CodeActStrategy`/`PredictStrategy` to take a `config=` object, which is a plausible source of "missing imports / breaking changes" reports downstream callers may have — inferred from https://api.github.com/repos/NVIDIA-NeMo/labs-OO-Agents/issues?state=open&per_page=30.
- [SPECULATION] Issue #114's middleware gap (sync Agent methods bypass `agent_call`) is a real code-execution-safety concern because CodeAct relies on deterministic sync Python methods whose charges/effects may not be intercepted — inferred from https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/114.
- [SPECULATION] Issue #125's inert TruncationConfig fields likely caused earlier confusion when users saw documented `max_event_tokens` ignored in practice; PR/docs may rename the test rather than remove the fields — inferred from https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues/125.

## Gap: GitHub unreachable

- None: the issues list, the combined issues+PRs API listing, and four individual issue pages all returned content. Only the Discussions tab consistently returned 404 / empty across multiple probes, which is recorded as a claim above rather than as an unreachable gap.
