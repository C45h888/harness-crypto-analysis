# nooa-cli — commands and config

## Source paths read

- `/tmp/nooa-monorepo/packages/nooa-cli/pyproject.toml`
- `/tmp/nooa-monorepo/packages/nooa-cli/README.md`
- `/tmp/nooa-monorepo/packages/nooa-cli/docs/activity-introspection-design.md`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/__init__.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/__main__.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/_common.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/completion.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/AGENTS.md`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/__init__.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/_template.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/_otlp_helpers.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/config.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/eval.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/traces.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/start_dev.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/import_traces.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/delete_traces.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/commands/import_harbor.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/tools/__init__.py` (empty placeholder — package marker only)
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/tools/repo_tools.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/tools/_tree_sitter_backend.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/tools/pyp/__init__.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/tools/pyp/sources.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/tools/pyp/stream.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/src/nooa_cli/tools/pyp/errors.py`
- `/tmp/nooa-monorepo/packages/nooa-cli/tests/test_cli_smoke.py`

## Public exports

### CLI entry points

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
|---|---|---|---|
| `nooa` script | `pyproject.toml:20` | `nooa = "nooa_cli:main"` | Console script for the CLI (per `pyproject.toml`). |
| `oo` group | `src/nooa_cli/__init__.py:35` | `@click.group def oo(ctx)` | "OO Agents — agent toolkit. Extensible CLI for running agents, evaluations, and trace management." |
| `main` | `src/nooa_cli/__init__.py:68` | `def main()` | "Entry point for console_scripts: nooa <subcommand>." |
| `completion` group | `src/nooa_cli/completion.py:92` | `@click.group def completion()` | "Generate shell completion scripts for nooa." |
| `completion.bash` | `src/nooa_cli/completion.py:109` | `@completion.command def bash()` | "Print bash completion script." |
| `completion.zsh` | `src/nooa_cli/completion.py:122` | `@completion.command def zsh()` | "Print zsh completion script." |
| `completion.fish` | `src/nooa_cli/completion.py:135` | `@completion.command def fish()` | "Print fish completion script." |
| `completion.install` | `src/nooa_cli/completion.py:146` | `@completion.command def install()` | "Auto-detect shell and install completions." |

### Auto-discovered commands (`nooa_cli/commands/`)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
|---|---|---|---|
| `config` | `src/nooa_cli/commands/config.py:68` | `@click.group def command()` | "Inspect and customize the LLM-registry config chain." |
| `config.show` | `src/nooa_cli/commands/config.py:72` | `@command.command("show") def show_cmd()` | "Print the resolved config-file chain." (truncated — full body covers dedup and API-key reporting) |
| `config.path` | `src/nooa_cli/commands/config.py:259` | `@command.command("path") def path_cmd()` | "Print the user-level config path (where ``eject`` writes)." |
| `config.eject` | `src/nooa_cli/commands/config.py:265` | `@command.command("eject") def eject_cmd(force: bool)` | "Copy a bundled-defaults YAML to the user-level config path." (truncated — explains precedence / multi-bundled refusal) |
| `eval` | `src/nooa_cli/commands/eval.py:26` | `@click.command(..., context_settings={"ignore_unknown_options": True, "allow_extra_args": True}) def command(ctx, args)` | "Run an evaluation pipeline (passthrough to eval_pipeline)." |
| `start-dev` | `src/nooa_cli/commands/start_dev.py:76` | `@click.command @click.option("--port", "-p", type=int, default=5001) @click.option("--host", "-h", default="0.0.0.0") @click.option("--db", ...) def command(port: int, host: str, db_path_opt: str | None)` | "Start the unified trace + evaluation viewer." (file sets `NAME = "start-dev"`) |
| `traces` | `src/nooa_cli/commands/traces.py:121` | `@click.group def command()` | "Manage trace and evaluation files." |
| `traces.delete` | `src/nooa_cli/commands/traces.py:133` | `@command.command @click.argument("directory", type=click.Path(exists=True), default=".") @click.option("--dry-run", "-n") @click.option("--older-than", type=int) @click.option("--all") @click.option("--evals") @click.option("--evals-only") @click.option("-y", "--yes") def delete(...)` | "Delete trace and evaluation files." |
| `traces.list` | `src/nooa_cli/commands/traces.py:239` | `@command.command("list") @click.option("--root", type=click.Path(exists=True)) def list_dirs(root: str | None)` | "List discovered trace directories." |
| `traces.stats` | `src/nooa_cli/commands/traces.py:279` | `@command.command @click.argument("directory", type=click.Path(exists=True), default=".") def stats(directory: str)` | "Show statistics about trace files." |
| `import-traces` | `src/nooa_cli/commands/import_traces.py:82` | `@click.command @click.argument("path", type=click.Path(exists=True)) @click.option("--endpoint", default="http://localhost:5001") @click.option("--batch-id") def command(path, endpoint, batch_id)` | "Import OTLP trace .jsonl files into the viewer." (file sets `NAME = "import-traces"`) |
| `delete-traces` | `src/nooa_cli/commands/delete_traces.py:41` | `@click.command @click.option("--batch-id", required=True) @click.option("--endpoint", default="http://localhost:5001") def command(batch_id, endpoint)` | "Delete all traces belonging to a batch." (file sets `NAME = "delete-traces"`) |
| `import-harbor` | `src/nooa_cli/commands/import_harbor.py:401` | `@click.command @click.argument("path") @click.option("--endpoint") @click.option("--experiment") @click.option("--batch-id") @click.option("--batch-lines", default=1000) @click.option("--batch-bytes", default=4_000_000) @click.option("--eval-only") def command(...)` | "Import NVIDIA OO Agents OTLP traces from a Harbor job directory." (file sets `NAME = "import-harbor"`) |

### Helper functions (selected)

| Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars) |
|---|---|---|---|
| `discover_commands` | `src/nooa_cli/commands/__init__.py:74` | `def discover_commands() -> Iterator[tuple[str, click.Command]]` | "Yield (name, command) pairs from all command modules in this package." (truncated — full body explains `_` prefix skip and `command` attribute requirement) |
| `find_project_root` | `src/nooa_cli/_common.py:13` | `def find_project_root() -> Path` | "Find the user's project root by walking up from the current directory." (truncated — explains fallback to cwd) |
| `format_size` | `src/nooa_cli/_common.py:36` | `def format_size(size_bytes: int) -> str` | "Format a byte count as human-readable (e.g. '4.2 MB')." |
| `validate_endpoint` | `src/nooa_cli/commands/_otlp_helpers.py:12` | `def validate_endpoint(endpoint: str) -> None` | "Validate that the endpoint uses an HTTP(S) scheme." |
| `inject_resource_attrs` | `src/nooa_cli/commands/_otlp_helpers.py:22` | `def inject_resource_attrs(body: dict, attrs: dict[str, str | bool | int]) -> dict` | "Inject additional resource attributes into an OTLP body (skips existing keys)." |
| `post_trace` | `src/nooa_cli/commands/_otlp_helpers.py:43` | `def post_trace(endpoint: str, body: dict, timeout: float = 30) -> bool` | "POST a single OTLP trace body to the viewer endpoint." |
| `post_traces_batch` | `src/nooa_cli/commands/_otlp_helpers.py:64` | `def post_traces_batch(endpoint: str, bodies: list[dict]) -> bool` | "POST multiple OTLP bodies as one request by merging their ``resourceSpans``." |
| `post_annotations` | `src/nooa_cli/commands/_otlp_helpers.py:83` | `def post_annotations(endpoint: str, annotations: list[dict]) -> int` | "POST annotations to the viewer endpoint. Returns count of successfully imported." |
| `session_exists` | `src/nooa_cli/commands/_otlp_helpers.py:105` | `def session_exists(endpoint: str, session_id: str) -> bool` | "Check whether a session already exists in the viewer." |
| `check_endpoint_reachable` | `src/nooa_cli/commands/_otlp_helpers.py:116` | `def check_endpoint_reachable(endpoint: str) -> bool` | "Return True if the viewer API is reachable." |
| `RepoTools` | `src/nooa_cli/tools/repo_tools.py:386` | `class RepoTools(Skill)` (entry-point `nemo.repo`) | "Find code locations as ShellTools ``Match`` anchors." |
| `Pyp` | `src/nooa_cli/tools/pyp/__init__.py:23` | `class Pyp(Skill)` (no entry-point declared) | "Async-native shell piping — source().transforms().sink()." (truncated — full body lists transforms/sinks) |

## Behavioral notes

The CLI is a Click-based group named `oo`, exposed as the `nooa` console script (`pyproject.toml:20`). Subcommands are auto-discovered from `nooa_cli/commands/` (`commands/__init__.py:74-106`) — any module in that directory not starting with `_` must export a `click.BaseCommand` named `command`; the filename (or `NAME = "..."` override) becomes the subcommand name.

The `oo` group callback in `src/nooa_cli/__init__.py:35-57` preloads `secrets.yaml` into `os.environ` via `nooa.secrets.load_secrets_into_env()` before any subcommand runs, except for the `completion` subcommand (which short-circuits to skip the ~1.5s core import cost and secrets preload). Help option supports both `-h` and `--help`.

### `nooa completion`

A Click group with four subcommands (`src/nooa_cli/completion.py`). Emits static shell-completion scripts embedded as templates (`_BASH_SCRIPT`, `_ZSH_SCRIPT`, `_FISH_SCRIPT`) and rendered with `prog_name="nooa"`, `complete_var="_NOOA_COMPLETE"`, `complete_func="_nooa_completion"`.

- `completion.bash`, `completion.zsh`, `completion.fish` — print the rendered script to stdout (`click.echo(_render_script(...))`).
- `completion.install` — auto-detects the shell via `$SHELL` (`_detect_shell`); writes the eval line into `~/.bashrc`, `~/.zshrc`, or `~/.config/fish/completions/nooa.fish`. Refuses to clobber existing `_NOOA_COMPLETE` lines; scrubs stale `_NEMO_COMPLETE` lines (pre-rename) along with any preceding `# nemo...` comment lines to keep profile files clean.

### `nooa eval`

`src/nooa_cli/commands/eval.py`. A passthrough to the `eval_pipeline` package — it forwards every argument verbatim (Click context uses `ignore_unknown_options=True, allow_extra_args=True`, `nargs=-1, type=click.UNPROCESSED`). It mutates `sys.argv` to `["eval_pipeline", *args]` and calls `asyncio.run(eval_pipeline.cli.main_async())`. Enables `faulthandler` and registers `SIGUSR1` (same as `eval_pipeline.__main__`). If `eval_pipeline` isn't importable, prints a hint to install via `uv sync` from the monorepo and exits with code 1. UNVERIFIED — requires B-lane: actual `eval_pipeline` CLI surface (which flags this command exposes by passthrough).

### `nooa start-dev`

`src/nooa_cli/commands/start_dev.py`. NAME override (`NAME = "start-dev"`) so the subcommand is `start-dev`, not `start_dev`. Boots the FastAPI viewer (`from nooa.viewer.main import app`) under `uvicorn` with an access-log filter that suppresses `/v1/traces`, `/api/trace`, `/api/refresh`.

DB-path resolution, in order: `--db` flag → `$NOOA_TRACE_DB` → `$NEMO_OO_TRACE_DB` → `nooa.paths.get_user_dir("traces.db")`. The resolved path is exported into both `NOOA_TRACE_DB` and `NEMO_OO_TRACE_DB` env vars before importing the viewer (so the viewer's module-level env read picks it up). Default port `5001`, default host `0.0.0.0`.

On `EADDRINUSE`, `_find_pid_on_port` probes via `lsof` (macOS) or `ss` (Linux) and prints a hint pointing at the offending PID. UNVERIFIED — requires B-lane: actual viewer endpoints and DB schema; only the bootstrap path is verifiable from this CLI source.

### `nooa config`

`src/nooa_cli/commands/config.py`. A group with three subcommands. The full precedence chain is documented in the module docstring (bundled defaults → user → project → `NEMO_OO_LLM_CONFIG` env var; user dir is `~/.config/nooa/llm_config.yaml`, overridable via `NEMO_OO_USER_DIR`).

- `config.show` — lists each candidate layer, counts `models:` entries (skipping `null` entries), and tags deduplicated/shadowed paths. Resolves the chain once via `nooa.llm_config.llm_config_chain()` and reloads the registry via `nooa.unifiedllm.reload_registry(*resolved_chain)` so the printed total matches. Surfaces every `api_key_env` referenced by the loaded aliases and reports whether each is set in the environment. After llm_config, also shows `settings.yaml` and `secrets.yaml` chains via `nooa.layered_config.layered_paths` (secrets values are redacted; only env-var NAMES are listed by `_summarize_secrets`).
- `config.path` — prints the user-level `llm_config.yaml` path from `nooa.paths.get_user_dir`.
- `config.eject` — refuses (exit 1) if the target already exists (unless `--force`), if no bundled provider is registered, or if multiple bundled providers are registered. Refuses to overwrite a symlink target or a directory at the target path. When it succeeds, copies `bundled[0]` to the user-level path via `shutil.copyfile` and notes that `$NEMO_OO_LLM_CONFIG`, if set, may still override the ejected copy.

### `nooa traces`

`src/nooa_cli/commands/traces.py`. A group with three subcommands that operate on JSONL trace files. Trace-directory discovery uses `_discover_trace_dirs` (project `.nooa/traces` + legacy `./traces` + `$TRACE_DIR`); file walks skip `.venv`, `venv`, `node_modules`, `.git`, `__pycache__`, `.mypy_cache`, `.ruff_cache`. Eval files matched: `*.noo-eval.jsonl`, `*.006eval.json`. Trace files matched: `*.jsonl` (minus `*.annotations.jsonl` and the eval suffixes).

- `traces.delete` — walks the tree, classifies files, preserves traces with a `session.id` (first 4 KB is scanned with `SESSION_ID_RE`) unless `--all` is passed, optionally filters by age with `--older-than DAYS`, prompts unless `-y` is given (or `--dry-run`/`-n`).
- `traces.list` — `--root` overrides project-root auto-detection; reports per-dir trace count and size (excluding `.annotations.jsonl`).
- `traces.stats` — counts and sizes, plus oldest/newest ages in days for trace files.

### `nooa import-traces` and `nooa delete-traces`

`src/nooa_cli/commands/import_traces.py` and `src/nooa_cli/commands/delete_traces.py`. Both override the command name (`NAME = "import-traces"`, `NAME = "delete-traces"`).

`import-traces` walks a file or directory for `.jsonl` traces, derives `session.id` from each file's basename, skips sessions that already exist in the viewer (`session_exists`), injects `session.id` and `batch_id` resource attrs (`inject_resource_attrs`), and posts each line to `<endpoint>/v1/traces`. Annotation payloads (lines containing `annotations` without `resourceSpans`) are buffered per file and posted to `<endpoint>/api/annotations` after the spans land. Legacy format (`span_id`/`trace_id`) is rejected with a "legacy format not supported" error.

`delete-traces` validates the endpoint scheme, checks reachability via `/api/version`, then DELETEs `<endpoint>/api/traces?batch_id=<id>`.

### `nooa import-harbor`

`src/nooa_cli/commands/import_harbor.py`. NAME override. Walks a Harbor job directory (or any parent thereof) for `artifacts/**/*.jsonl` files via `_find_harbor_traces`, then per-file builds viewer resource attrs via `_harbor_resource_attrs` (sets `session.id=trial_name`, `experiment`, `batch_id`, `eval.test_id`, `eval.test_name`, `eval.display_name`, `eval.method="harbor"`, `eval.score`, `eval.weighted_score`, `eval.passed = score >= 1.0`). Reward scalar is read by `_read_score` from a chain of locations (Harbor verifier result shape has changed across versions):

1. `verifier/reward.json["score"]`
2. `verifier/reward.json["reward"]`
3. `result.json["verifier_result"]["rewards"]["score"]`
4. `result.json["verifier_result"]["rewards"]["reward"]`
5. `verifier/reward.txt`

Posts are batched (default `--batch-lines=1000`, `--batch-bytes=4_000_000`); `_import_trace_file` flushes when either threshold is hit. With `--eval-only`, no JSONL is read — instead `_trial_dirs` enumerates Harbor trial directories and posts a single synthetic `eval` span via `_build_eval_only_body`, optionally linking to a live-streamed session found via `/api/eval/match-session`.

### `nooa` registry integration

`__main__.py:5-8` re-exports `main` so `python -m nooa_cli` works. The `oo` group callback (`src/nooa_cli/__init__.py:35`) lazy-loads `nooa.secrets.load_secrets_into_env()` once per invocation (skipped for `completion`). Per-command imports stay lazy — `nooa.paths.get_user_dir` is imported only inside `config.path`/`start-dev`; `nooa.llm_config`/`nooa.unifiedllm` only inside `config.show`; `nooa.viewer.main` only inside `start-dev`. The `eval` command is the one place that imports `eval_pipeline` (separate package). The `import-traces` / `import-harbor` / `delete-traces` commands never import `nooa` — they only talk to the viewer's HTTP API via `_otlp_helpers`.

Skills entry-point group (`pyproject.toml:22-23`): `nooa.skills` → `nemo.repo` → `nooa_cli.tools.repo_tools:RepoTools`. The `pyp` subpackage has no declared entry-point in `pyproject.toml`; it is a Python library only (the file `tools/__init__.py` is empty, leaving the `tools` package without a public re-export at this layer).

Config files read by the CLI / `nooa` registry:
- `llm_config.yaml` (user: `~/.config/nooa/llm_config.yaml` overridable via `NEMO_OO_USER_DIR`; project: `.nooa/llm_config.yaml`).
- `settings.yaml` (same paths; env override `NEMO_OO_SETTINGS`).
- `secrets.yaml` (same paths; env override `NEMO_OO_SECRETS`).

Config-related env vars (set/read by the CLI itself):
- `NEMO_OO_LLM_CONFIG` — comma-separated YAML paths, global highest-priority override.
- `NEMO_OO_SETTINGS`, `NEMO_OO_SECRETS` — same idea for the other layered files.
- `NEMO_OO_USER_DIR` — overrides the XDG user-config directory.
- `TRACE_DIR` — extra JSONL trace search root for `nooa traces`.
- `NOOA_TRACE_DB` / `NEMO_OO_TRACE_DB` — viewer SQLite DB override for `start-dev`.
- `_NOOA_COMPLETE` (with value `bash_source`/`zsh_source`/`fish_source` for shell eval; `bash_complete`/`zsh_complete`/`fish_complete` for in-shell completion).
- `SHELL` — read by `completion.install` to detect which profile to edit.
- `NOOA_CLI_*` flags: none — all CLI surface is passed via Click flags.

### RepoTools skill (`nemo.repo`)

`src/nooa_cli/tools/repo_tools.py`. Registered as `nemo.repo` entry-point under `nooa.skills`. Provides a `RepoTools` Skill (subclass of `nooa.skill.Skill`) with public async methods `symbols(path=".", query="", max_results=50) -> RepoResult` and `refs(name, path=".", max_results=50) -> RepoResult`. Optional `__init__(root, session, require_tree_sitter=False)`; `require_tree_sitter=True` raises if the tree-sitter grammars aren't installed. `_extract_symbols` prefers tree-sitter (`nooa_cli.tools._tree_sitter_backend.ts_extract_symbols`) and falls back to regex patterns per language. `_search_references` likewise prefers tree-sitter (`ts_find_references`) and falls back to `rg -n` plus heuristic filtering (skips definition lines, comment-only lines, returns `ReferenceSearchResult`). Backed by ripgrep when a `BashSession` is wired in (shared with `ShellTools`); falls back to `shutil.which("rg")` when not. Telemetry hooks (`nooa.runtime.harness_metrics.get_harness_metrics().repo_failure(...)`) are emitted on `filemap:file_not_found`, `repo_map:no_files`, and `search_symbol:no_results` failures. `__nosnapshot__ = True` so the skill is excluded from agent snapshots.

### Pyp skill

`src/nooa_cli/tools/pyp/`. No entry-point declared. Re-exports `Stream`, `PipeError`, `Result`, `make_pipe_error` from `errors.py`, plus source functions (`arun`, `cat`, `empty`, `find`, `glob`, `items`, `lines`, `rg`, `run`, `seq`, `stdin`) from `sources.py`. Wraps everything in a `Pyp(Skill)` whose public surface is identical to the source functions plus `Stream` (the `Skill` class doesn't add a new entry-point — discovered manually by callers).

## Cross-references

- `unifiedllm.md` — provider registry (`nooa.unifiedllm.MODELS`, `reload_registry`) consumed by `config show`.
- `docs/nooa-kb/from-monorepo/packages-nooa/exports.md` — top-level `nooa` surface (`nooa.secrets.load_secrets_into_env`, `nooa.paths.get_user_dir`/`get_project_dir`, `nooa.llm_config.llm_config_chain`/`bundled_config_paths`, `nooa.layered_config.layered_paths`, `nooa.viewer.main.app`, `nooa.skill.Skill`, `nooa.agentdoc.hidden`/`spec`).
- `eval_pipeline.cli.main_async` (referenced from `nooa eval`) — passthrough target; not part of `nooa-cli` itself.

## Suggested remediation

- The `tools/__init__.py` file is empty (verified via Read — system reported "file exists but contents are empty"), so the `tools` subpackage has no public `__all__` and no top-level re-exports. Consumers must import directly from `nooa_cli.tools.repo_tools` / `nooa_cli.tools.pyp`. Worth adding a re-export `__init__.py` so `from nooa_cli.tools import RepoTools, Pyp` works.
- `pyproject.toml` lists the `pyp` package only in `tests/`, not under any extra or entry-point. If `Pyp` is intended as a skill, register it under `[project.entry-points."nooa.skills"]` (e.g. `"nemo.pyp" = "nooa_cli.tools.pyp:Pyp"`) for parity with `RepoTools`.
- The `tools/` directory is not declared in the wheel target `[tool.hatch.build.targets.wheel] packages = ["src/nooa_cli"]` — fine because it is a subpackage, but worth verifying the build actually emits `nooa_cli/tools/pyp/` after a wheel build.
- `commands/_otlp_helpers.py` lives alongside auto-discovered command modules and is silently skipped by `discover_commands()` because of the `_` prefix. That works, but adding `__all__ = []` would make the "intentionally not a command" intent explicit.
- `commands/__init__.py` raises `TypeError` if a `command` attribute is set but isn't a `click.Command` — but the discovery also silently `continue`s if `command` is missing. The asymmetry (some misconfigurations are loud, others silent) is mild; consider a startup warning rather than silent skip so a missing `command =` doesn't just produce a no-op install.
- `eval.py` mutates `sys.argv` to call into `eval_pipeline` — a small footgun if any other code in the same Python process is reading `sys.argv`. Comment already explains the choice; consider restoring the original argv in a `try/finally` (it does — verified at `commands/eval.py:67-68`).
- `start-dev.py` sets both `NOOA_TRACE_DB` and `NEMO_OO_TRACE_DB` to keep the legacy alias working; the legacy env var name appears here and in `delete-traces.py`'s `_detect_shell` neighbor (`_NEMO_COMPLETE` scrubbing). Worth a one-time migration to drop the `NEMO_*` aliases once external consumers are migrated.
- `import-harbor.py` defines `_trial_meta_from_dir` which calls `_trial_meta(trial_dir / "artifacts" / "traces" / "_synthetic.jsonl")` — the file is constructed but never written, so when the trial directory lacks the conventional `artifacts/traces/` layout the function still produces metadata purely by walking parent directories. The synthetic path is misleading; rename to a sentinel or refactor `_trial_meta` to not require a path.