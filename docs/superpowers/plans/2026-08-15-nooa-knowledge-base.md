# NOOA Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a version-controlled NOOA knowledge base at `docs/nooa-kb/` that records what `nooa` v0.0.6 actually is, what our integration expects of it, what the wider community reports, and the gap between those two at execution time, ending with `assessment/state-snapshot.md`.

**Architecture:** Four stages — (1) ingest monorepo + internet index, (2) fan out workers grouped by source type (M monorepo, I internet, B behavior), (3) single synthesis agent cross-references everything, (4) single state-assessment agent writes the final snapshot. Each stage is preceded by a write-boundary check and followed by a verification step. All output lands under `docs/nooa-kb/`. The upstream clone lives at `/tmp/nooa-monorepo/`, never in the repo.

**Tech Stack:** Git, Bash (for clone + status checks), Python 3.12 (matching `.python-version`), the `nooa` package already pinned at `v0.0.6` via `requirements.txt`, WebSearch/WebFetch via Claude tooling, Claude subagents for fan-out.

**Spec:** [`docs/NOOA_KNOWLEDGE_BASE_DESIGN.md`](../../NOOA_KNOWLEDGE_BASE_DESIGN.md)

## Global Constraints

(Copied verbatim from §9 of the spec.)

- Agents may only create or modify files under:
  - `docs/NOOA_KNOWLEDGE_BASE_DESIGN.md` (spec edits)
  - `docs/nooa-kb/**` (KB output)
  - `/tmp/nooa-monorepo/**` (clone target)
- Agents must NOT modify `market_service/**`, `tests/**`, `db/**`, `alembic/**`, `alembic.ini`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `.env*`, or any other file outside the allowlist above.
- No `git` operation outside `git add docs/nooa-kb/` and `git add docs/NOOA_KNOWLEDGE_BASE_DESIGN.md` (no tags, no branches, no remote operations).
- Monorepo clone URL: `https://github.com/NVIDIA-NeMo/labs-OO-Agents.git`, ref `v0.0.6`.
- LLM generations cannot be validated without a real model — those land as `unverified_by_execution` per spec §6.
- Internet claims must cite URLs (max 8 distinct URLs per I-lane worker).

## File Structure

Files created during this plan:

```
docs/nooa-kb/
  _build/
    check_write_boundary.py                # Task 0: tool that enforces §9 allowlist
  sources/
    pin.json                               # Task 1: monorepo clone provenance
    monorepo-inventory.md                  # Task 1: auto-discovered file index
    internet-index.md                      # Task 1: URL seeds for I-lane workers
  from-monorepo/
    packages-nooa/
      exports.md                           # Task 2 / M1
      strategies.md                        # Task 2 / M2
      context-blocks.md                    # Task 2 / M3
      agentdoc.md                          # Task 2 / M4
      unifiedllm.md                        # Task 2 / M5
    packages-nooa-cli/
      cli.md                               # Task 2 / M6
    docs-markdown.md                       # Task 2 / M7
  from-internet/
    reddit.md                              # Task 3 / I1
    nvidia-blogs.md                        # Task 3 / I2
    github-issues.md                       # Task 3 / I3
    third-party.md                         # Task 3 / I4
  behavior/
    call-sites.md                          # Task 4 / B-summary (written first)
    B01-agents-base.md                     # Task 4 / B01
    B02-strategy-predict.md                # Task 4 / B02
    B03-strategy-codeact.md                # Task 4 / B03
    B04-dynamic-context.md                 # Task 4 / B04
    B05-agentdoc.md                        # Task 4 / B05
    B06-unifiedllm.md                      # Task 4 / B06
    B07-runner-env.md                      # Task 4 / B07
    B08-suite-composition.md               # Task 4 / B08
  assessment/
    import-coverage.md                     # Task 5 / S1
    decorator-usage.md                     # Task 5 / S2
    pin-drift.md                           # Task 5 / S3
    security-boundaries.md                 # Task 5 / S4
    state-snapshot.md                      # Task 6 / FINAL
  README.md                                # Task 6
```

No other files in the repo are touched.

---

## Task 0: Write-boundary check tool

**Files:**
- Create: `docs/nooa-kb/_build/check_write_boundary.py`
- Test: `docs/nooa-kb/_build/test_check_write_boundary.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `check_write_boundary(snapshot_path: str | None = None) -> BoundaryReport`
  - `BoundaryReport.ok: bool`
  - `BoundaryReport.violations: list[str]`
  - `snapshot_baseline() -> None`  (writes `.git_status_baseline.txt`)

**Step 0.1 — Write the failing test**

Create `docs/nooa-kb/_build/test_check_write_boundary.py`:

```python
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from check_write_boundary import check_write_boundary, snapshot_baseline


def test_clean_repo_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Simulate a clean repo by passing an empty baseline that records
    # "no changes", and a synthesized git-status output that has nothing.
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text("")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert report.ok, f"unexpected violations: {report.violations}"


def test_untouched_allowed_path_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text(" M docs/nooa-kb/from-monorepo/packages-nooa/exports.md\n")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert report.ok, f"allowed path flagged: {report.violations}"


def test_violation_in_market_service_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text(" M market_service/nooa_harness/agents.py\n")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert not report.ok
    assert any("market_service" in v for v in report.violations)


def test_violation_in_requirements_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text(" M requirements.txt\n")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert not report.ok
    assert any("requirements.txt" in v for v in report.violations)


def test_violation_in_build_config_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text(" M docker-compose.yml\n")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert not report.ok
    assert any("docker-compose.yml" in v for v in report.violations)
```

**Step 0.2 — Run test to verify it fails**

Run: `uv run --python 3.12 python -m pytest docs/nooa-kb/_build/test_check_write_boundary.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'check_write_boundary'`.

**Step 0.3 — Implement the module**

Create `docs/nooa-kb/_build/check_write_boundary.py`:

```python
"""Write-boundary enforcer for the NOOA knowledge base (spec §9).

Reads a saved `git status --porcelain` snapshot and compares it against
the documented allowlist. Any path outside the allowlist raises a
violation. Exposes a small CLI plus a Python API for tests.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Allowlist copied verbatim from spec §9.
ALLOWED_PATH_PREFIXES: tuple[str, ...] = (
    "docs/NOOA_KNOWLEDGE_BASE_DESIGN.md",
    "docs/nooa-kb/",
    "/tmp/nooa-monorepo/",
)

# Explicit denylist of files that, even if under an allowed prefix, must
# not change. None currently — left as a hook for tightening later.
DENIED_PATH_EXACT: tuple[str, ...] = ()


@dataclass
class BoundaryReport:
    ok: bool = True
    violations: list[str] = field(default_factory=list)
    allowed_changes: list[str] = field(default_factory=list)

    def render(self) -> str:
        if self.ok:
            return (
                f"OK — {len(self.allowed_changes)} allowed change(s); "
                f"0 violations."
            )
        lines = [
            f"FAIL — {len(self.violations)} violation(s) of write boundary:"
        ]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


def _parse_porcelain(text: str) -> list[str]:
    """Return list of touched paths from `git status --porcelain` output."""
    paths: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        # Porcelain format: XY <path>  (or XY <old> -> <new> for renames)
        # We only need the final path; for renames that is the second.
        parts = line[3:].strip().split(" -> ")
        paths.append(parts[-1])
    return paths


def _is_allowed(path: str) -> bool:
    if path in DENIED_PATH_EXACT:
        return False
    return any(path == p or path.startswith(p) for p in ALLOWED_PATH_PREFIXES)


def check_write_boundary(
    baseline_path: str,
    git_status_path: str | None = None,
    cwd: str | None = None,
) -> BoundaryReport:
    """Compare current `git status --porcelain` against a saved baseline.

    A path is "touched" if it appears in the current status but not in the
    baseline. Each touched path is checked against the allowlist.
    """
    base = Path(baseline_path).read_text() if Path(baseline_path).exists() else ""
    baseline_paths = set(_parse_porcelain(base))

    if git_status_path is not None:
        current_text = Path(git_status_path).read_text()
    else:
        current_text = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=cwd,
        ).decode("utf-8")
    current_paths = set(_parse_porcelain(current_text))

    new_paths = current_paths - baseline_paths
    report = BoundaryReport()
    for path in sorted(new_paths):
        if _is_allowed(path):
            report.allowed_changes.append(path)
        else:
            report.ok = False
            report.violations.append(path)
    return report


def snapshot_baseline(git_status_path: str | None = None) -> str:
    """Capture current `git status --porcelain` to .git_status_baseline.txt.

    Returns the path written.
    """
    if git_status_path is None:
        out = subprocess.check_output(["git", "status", "--porcelain"]).decode("utf-8")
    else:
        out = Path(git_status_path).read_text()
    target = Path(".git_status_baseline.txt")
    target.write_text(out)
    return str(target)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--baseline", default=".git_status_baseline.txt",
        help="Path to the saved baseline status file.",
    )
    p.add_argument(
        "--snapshot", action="store_true",
        help="Capture a fresh baseline instead of checking.",
    )
    p.add_argument(
        "--current-status", default=None,
        help="Path to a `git status --porcelain` file to use instead of running git.",
    )
    args = p.parse_args()

    if args.snapshot:
        path = snapshot_baseline(args.current_status)
        print(f"baseline written to {path}")
        return 0

    report = check_write_boundary(args.baseline, args.current_status)
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

**Step 0.4 — Run test to verify it passes**

Run: `uv run --python 3.12 python -m pytest docs/nooa-kb/_build/test_check_write_boundary.py -v`

Expected: PASS (5 tests).

**Step 0.5 — Capture the baseline**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py --snapshot`

Expected: prints `baseline written to .git_status_baseline.txt`.

**Step 0.6 — Commit**

```bash
git add docs/nooa-kb/_build/ .git_status_baseline.txt
git commit -m "feat(nooa-kb): add write-boundary check tool and snapshot baseline"
```

---

## Task 1: Stage 1 — Ingest (monorepo clone + internet index)

**Files:**
- Create:
  - `/tmp/nooa-monorepo/` (shallow clone target, OUTSIDE the repo)
  - `docs/nooa-kb/sources/pin.json`
  - `docs/nooa-kb/sources/monorepo-inventory.md`
  - `docs/nooa-kb/sources/internet-index.md`

**Interfaces:**
- Consumes: pin ref `v0.0.6`, repo URL `https://github.com/NVIDIA-NeMo/labs-OO-Agents.git`
- Produces:
  - `pin.json` schema: `{ "repo_url": str, "ref": str, "sha": str, "cloned_at": str, "clone_path": "/tmp/nooa-monorepo/", "shallow": true }`
  - `monorepo-inventory.md`: tree of `/tmp/nooa-monorepo/` to depth 3, file count summary
  - `internet-index.md`: list of seed URLs for I-lane workers, each annotated with the topic they cover

**Step 1.1 — Pre-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: writes `OK — 0 allowed change(s); 0 violations.` and exits 0. (Should be a no-op since baseline is current.)

**Step 1.2 — Clone the monorepo**

Run:
```bash
rm -rf /tmp/nooa-monorepo
git clone --depth 1 --branch v0.0.6 https://github.com/NVIDIA-NeMo/labs-OO-Agents.git /tmp/nooa-monorepo
git -C /tmp/nooa-monorepo rev-parse HEAD
```

Expected: prints a 40-char SHA. Capture it as `PIN_SHA`.

**Step 1.3 — Write `pin.json`**

Create `docs/nooa-kb/sources/pin.json` with:
```json
{
  "repo_url": "https://github.com/NVIDIA-NeMo/labs-OO-Agents.git",
  "ref": "v0.0.6",
  "sha": "<PIN_SHA>",
  "cloned_at": "<ISO timestamp from `date -u +%Y-%m-%dT%H:%M:%SZ`>",
  "clone_path": "/tmp/nooa-monorepo/",
  "shallow": true
}
```

Run `uv run --python 3.12 python -c "import json; d=json.load(open('docs/nooa-kb/sources/pin.json')); assert d['ref']=='v0.0.6'; assert len(d['sha'])==40"`. Expected: no error.

**Step 1.4 — Generate `monorepo-inventory.md`**

Run from repo root:
```bash
( echo "# NOOA monorepo inventory"; echo; echo "**Repo:** $(jq -r .repo_url docs/nooa-kb/sources/pin.json) @ $(jq -r .ref docs/nooa-kb/sources/pin.json) ($(jq -r '.sha[:12]' docs/nooa-kb/sources/pin.json))"; echo "**Cloned at:** $(jq -r .cloned_at docs/nooa-kb/sources/pin.json)"; echo "**Source:** /tmp/nooa-monorepo/"; echo; echo "## Tree (depth 3)"; echo; echo '```'; (cd /tmp/nooa-monorepo && find . -maxdepth 3 -not -path './.git*' | sort); echo '```'; echo; echo "## File counts by top-level area"; echo; echo '```'; (cd /tmp/nooa-monorepo && for d in */; do printf "%-40s %d files\n" "$d" "$(find "$d" -type f -not -path '*/\.git/*' | wc -l | tr -d ' ')"; done); echo '```' ) > docs/nooa-kb/sources/monorepo-inventory.md
```

Run `wc -l docs/nooa-kb/sources/monorepo-inventory.md`. Expected: ≥ 20 lines.

**Step 1.5 — Generate `internet-index.md`**

First, the planner (you) populates the URL seed list. Add 8 seed URLs across the four I-lane topic areas by running a brief WebSearch for each:

| Topic | Suggested queries |
|---|---|
| Reddit | `"NVIDIA-NeMo labs-OO-Agents" OR "nooa framework" site:reddit.com` |
| NVIDIA blogs | `"nooa agent framework" NVIDIA devblog` |
| GitHub issues | `https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues` (this exact URL) |
| Third-party | `"OO agents" NVIDIA OR NeMo agent framework blog` |

After the four searches, manually pick the best 2 results per topic and write `docs/nooa-kb/sources/internet-index.md`:

```markdown
# Internet seed URLs for I-lane workers

Generated: <ISO timestamp>

These URLs are seeds — I-lane workers may add more during execution, but
each worker is capped at 8 distinct URLs total (per spec §5).

## I1 — Reddit

1. <url> — short note on what it covers
2. <url> — short note

## I2 — NVIDIA blogs / devblog

1. <url>
2. <url>

## I3 — GitHub issues & discussions on NVIDIA-NeMo/labs-OO-Agents

1. https://github.com/NVIDIA-NeMo/labs-OO-Agents/issues
2. https://github.com/NVIDIA-NeMo/labs-OO-Agents/discussions

## I4 — Third-party blogs & articles

1. <url>
2. <url>
```

Run `cat docs/nooa-kb/sources/internet-index.md | head -40`. Expected: shows the four sections with at least 2 URLs each.

**Step 1.6 — Post-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK` with exactly 3 allowed changes (pin.json, monorepo-inventory.md, internet-index.md). 0 violations.

**Step 1.7 — Commit**

```bash
git add docs/nooa-kb/sources/
git commit -m "feat(nooa-kb): Stage 1 ingest — clone v0.0.6 + write sources"
```

---

## Task 2: Stage 2 — M-lane fan-out (monorepo readers)

**Files:** (all under `docs/nooa-kb/from-monorepo/`)
- Create:
  - `packages-nooa/exports.md` (M1)
  - `packages-nooa/strategies.md` (M2)
  - `packages-nooa/context-blocks.md` (M3)
  - `packages-nooa/agentdoc.md` (M4)
  - `packages-nooa/unifiedllm.md` (M5)
  - `packages-nooa-cli/cli.md` (M6)
  - `docs-markdown.md` (M7)

**Interfaces:** Every M-lane file must contain sections in this order, with these exact headings:

1. `# <area> — monorepo reader`
2. `## Source paths read` — list of files from `/tmp/nooa-monorepo/` consulted.
3. `## Public exports` — table with columns `Name | Defined at | Signature | Docstring (verbatim if ≤ 200 chars)`.
4. `## Behavioral notes` — what the source actually does per documented/observable contract. Mark `UNVERIFIED — requires B-lane` where the contract is only inferred.
5. `## Cross-references` — bullet list of other M-lane files that touch the same surface.

**Step 2.1 — Pre-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK`. No changes since Task 1.6.

**Step 2.2 — Dispatch seven M-lane workers in parallel**

In a single message, dispatch seven Agent calls. Each agent receives only:

- The spec section 5 "M-lane" worker contract.
- The list of files inside `/tmp/nooa-monorepo/<its assigned area>` (provided by you — see Step 2.3).
- The output file path it owns.
- The header schema above.

Agent prompts:
- M1: `read source files under /tmp/nooa-monorepo/packages/nooa/nooa/ (subdirs __init__.py, agent/, agentdoc.py, etc.) and write docs/nooa-kb/from-monorepo/packages-nooa/exports.md — every public name (no leading underscore) with its signature and docstring verbatim.`
- M2: `read /tmp/nooa-monorepo/packages/nooa/nooa/strategies.py and write docs/nooa-kb/from-monorepo/packages-nooa/strategies.md — focus on PredictStrategy and CodeActStrategy: initialization args, strategy contract, what they expect from agent methods.`
- M3: `read /tmp/nooa-monorepo/packages/nooa/nooa/context_blocks.py (and DynamicContext specifically) and write docs/nooa-kb/from-monorepo/packages-nooa/context-blocks.md — what DynamicContext accepts (string, callable, …), when expressions are evaluated, return value semantics.`
- M4: `read /tmp/nooa-monorepo/packages/nooa/nooa/agentdoc.py and write docs/nooa-kb/from-monorepo/packages-nooa/agentdoc.md — spec, Annotated, hidden, how they affect prompt generation.`
- M5: `read /tmp/nooa-monorepo/packages/nooa/nooa/unifiedllm/ and write docs/nooa-kb/from-monorepo/packages-nooa/unifiedllm.md — registry pattern, get_llm_client, supported providers (openai, ollama, vllm, …).`
- M6: `read /tmp/nooa-monorepo/packages/nooa-cli/ and write docs/nooa-kb/from-monorepo/packages-nooa-cli/cli.md — commands, subcommands, config files, env vars. Note we do NOT actually invoke the CLI; this is a reader pass.`
- M7: `read every *.md at the top of /tmp/nooa-monorepo/ (README*, docs/*.md, packages/*/README*) and write docs/nooa-kb/from-monorepo/docs-markdown.md — verbatim 'official docs' we treat as authoritative.`

Each worker writes ONLY to its assigned output file. Re-stated in each prompt: "Write only to <path>. If you discover something needing change outside the KB, record it as a finding inside your assigned file under a 'Suggested remediation' heading; do NOT touch other files."

**Step 2.3 — Source-path pointers (provided by planner)**

Use these to seed each worker (replace the placeholder list in step 2.2 with the actual files the worker should consult):

- M1 → `/tmp/nooa-monorepo/packages/nooa/nooa/*.py` plus any `__init__.py`. Start with `ls /tmp/nooa-monorepo/packages/nooa/nooa/` to enumerate.
- M2 → `/tmp/nooa-monorepo/packages/nooa/nooa/strategies*.py` plus `predict.py` and `codeact*.py` if present.
- M3 → `/tmp/nooa-monorepo/packages/nooa/nooa/context_blocks.py`.
- M4 → `/tmp/nooa-monorepo/packages/nooa/nooa/agentdoc.py`.
- M5 → `/tmp/nooa-monorepo/packages/nooa/nooa/unifiedllm/`.
- M6 → `/tmp/nooa-monorepo/packages/nooa-cli/` (top level).
- M7 → `find /tmp/nooa-monorepo -maxdepth 4 -name "*.md" -not -path "*/.git/*" -not -path "*/node_modules/*"`.

**Step 2.4 — Verify all seven files exist and have the required sections**

Run:
```bash
for f in exports strategies context-blocks agentdoc unifiedllm; do
  test -f "docs/nooa-kb/from-monorepo/packages-nooa/${f}.md" || { echo "MISSING: $f"; exit 1; }
done
test -f docs/nooa-kb/from-monorepo/packages-nooa-cli/cli.md || { echo "MISSING: cli"; exit 1; }
test -f docs/nooa-kb/from-monorepo/docs-markdown.md || { echo "MISSING: docs-markdown"; exit 1; }

for f in docs/nooa-kb/from-monorepo/packages-nooa/exports.md \
         docs/nooa-kb/from-monorepo/packages-nooa/strategies.md \
         docs/nooa-kb/from-monorepo/packages-nooa/context-blocks.md \
         docs/nooa-kb/from-monorepo/packages-nooa/agentdoc.md \
         docs/nooa-kb/from-monorepo/packages-nooa/unifiedllm.md \
         docs/nooa-kb/from-monorepo/packages-nooa-cli/cli.md \
         docs/nooa-kb/from-monorepo/docs-markdown.md; do
  for h in "## Source paths read" "## Public exports" "## Behavioral notes" "## Cross-references"; do
    grep -q "$h" "$f" || { echo "MISSING SECTION '$h' in $f"; exit 1; }
  done
done
echo "M-lane OK"
```

Expected: prints `M-lane OK`.

**Step 2.5 — Post-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK` with exactly 7 new allowed changes (the M-lane files), 0 violations.

**Step 2.6 — Commit**

```bash
git add docs/nooa-kb/from-monorepo/
git commit -m "feat(nooa-kb): Stage 2 M-lane — monorepo readers (7 files)"
```

---

## Task 3: Stage 2 — I-lane fan-out (internet readers)

**Files:** (all under `docs/nooa-kb/from-internet/`)
- Create:
  - `reddit.md` (I1)
  - `nvidia-blogs.md` (I2)
  - `github-issues.md` (I3)
  - `third-party.md` (I4)

**Interfaces:** Every I-lane file must contain sections in this order:

1. `# <topic> — internet reader`
2. `## Sources retrieved` — bullet list of URLs with retrieval timestamp (ISO) and a one-line note per URL.
3. `## Claims` — bullet list, each entry formatted as:
   `- [<confidence: high|medium|low>] <claim> — <supporting URL>[: <quote>]`
4. `## Conflicts with monorepo readers (M-lane)` — bullet list, empty if none.
5. `## Speculative notes` — bullet list, each entry tagged `[SPECULATION]` with the source for the speculation.

Each worker is capped at 8 distinct URLs (per spec §5). Sources URL claims cannot exceed the cap.

**Step 3.1 — Pre-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK`. No changes since Task 2.5.

**Step 3.2 — Dispatch four I-lane workers in parallel**

In a single message, dispatch four Agent calls. Each receives:
- The spec §5 I-lane contract.
- The list of seed URLs from `docs/nooa-kb/sources/internet-index.md` for its topic.
- Its output file path.

Agent prompts:
- I1: `Read docs/nooa-kb/sources/internet-index.md §I1. Use WebSearch + WebFetch to retrieve up to 8 Reddit URLs about NOOA / NVIDIA-NeMo/labs-OO-Agents. For each claim cite the URL and retrieval timestamp. Distinguish speculation from fact. Write docs/nooa-kb/from-internet/reddit.md — only this file.`
- I2: same pattern, but for NVIDIA blogs / devblog.
- I3: same pattern, but for GitHub issues & discussions on the upstream repo.
- I4: same pattern, but for third-party blogs and articles.

Each worker re-affirms: "Write only to your assigned .md file. If you discover something needing code change, write it under a 'Suggested remediation' heading inside your file."

**Step 3.3 — Verify all four files exist and have required sections**

Run:
```bash
for f in reddit nvidia-blogs github-issues third-party; do
  test -f "docs/nooa-kb/from-internet/${f}.md" || { echo "MISSING: $f"; exit 1; }
  for h in "## Sources retrieved" "## Claims" "## Conflicts with monorepo readers (M-lane)" "## Speculative notes"; do
    grep -q "$h" "docs/nooa-kb/from-internet/${f}.md" || { echo "MISSING SECTION '$h' in ${f}.md"; exit 1; }
  done
done
for f in docs/nooa-kb/from-internet/*.md; do
  url_count=$(grep -oE 'https?://[^ )]+' "$f" | sort -u | wc -l | tr -d ' ')
  if [ "$url_count" -gt 8 ]; then
    echo "URL cap exceeded in $f: $url_count > 8"; exit 1
  fi
done
echo "I-lane OK"
```

Expected: prints `I-lane OK`.

**Step 3.4 — Post-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK` with 4 new allowed changes, 0 violations.

**Step 3.5 — Commit**

```bash
git add docs/nooa-kb/from-internet/
git commit -m "feat(nooa-kb): Stage 2 I-lane — internet readers (4 files)"
```

---

## Task 4: Stage 2 — B-lane fan-out (behavior validators)

**Files:** (all under `docs/nooa-kb/behavior/`)
- Create:
  - `call-sites.md` (B-summary) — written FIRST as the index
  - `B01-agents-base.md`
  - `B02-strategy-predict.md`
  - `B03-strategy-codeact.md`
  - `B04-dynamic-context.md`
  - `B05-agentdoc.md`
  - `B06-unifiedllm.md`
  - `B07-runner-env.md`
  - `B08-suite-composition.md`

**Interfaces:** Every B-lane file must contain sections in this order:

1. `# B<n> — <slug>`
2. `## Call site` — exact `file_path:line_number` ranges inspected.
3. `## Imports exercised` — bullet list of `import <name>` and `from <mod> import <name>` lines from the call site.
4. `## Micro-test code` — fenced ```python``` block. The test must run inside a subprocess with timeout and stub the model layer.
5. `## Observed behavior` — what the micro-test returned.
6. `## Documented behavior` — citation to the matching M-lane section (relative path + section heading).
7. `## Drift verdict` — one of `match | drift | unverified_by_execution` with a one-paragraph reason.
8. `## Suggested remediation` (only if `drift` or `unverified_by_execution`) — concrete finding, no code changes.

**Step 4.1 — Write `call-sites.md` (B-summary) BEFORE dispatching B workers**

Create `docs/nooa-kb/behavior/call-sites.md` first. This file is the index the later synthesis step will read. Contents:

```markdown
# Behavior-validator call-site index (B-summary)

| B-id | Slug | Source file(s) | Imports exercised | Status |
|---|---|---|---|---|
| B01 | agents-base | market_service/nooa_harness/agents.py | (filled in by B01) | pending |
| B02 | strategy-predict | market_service/nooa_harness/agents.py | (filled in by B02) | pending |
| B03 | strategy-codeact | market_service/nooa_harness/agents.py (line ~340 TODO region) | (filled in by B03) | pending |
| B04 | dynamic-context | market_service/nooa_harness/agents.py | (filled in by B04) | pending |
| B05 | agentdoc | market_service/nooa_harness/agents.py | (filled in by B05) | pending |
| B06 | unifiedllm | market_service/nooa_harness/backends.py | (filled in by B06) | pending |
| B07 | runner-env | market_service/nooa_harness/runner.py + backends.py | (filled in by B07) | pending |
| B08 | suite-composition | market_service/nooa_harness/suite.py | (filled in by B08) | pending |
```

Run: `test -f docs/nooa-kb/behavior/call-sites.md && echo "B-summary written"`.

**Step 4.2 — Pre-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK` with 1 new allowed change (call-sites.md), 0 violations.

**Step 4.3 — Dispatch eight B-lane workers in parallel**

In a single message, dispatch eight Agent calls. Each receives:
- The spec §5 B-lane contract and §6 sandbox rule.
- The specific call site path / line range.
- The output file path.

Critical: the model layer must be stubbed. Provide this stub template to every B worker so it does not invoke a real LLM:

```python
# Stub model used by every B micro-test.
class StubLLM:
    """Returns a fixed JSON-shaped string so we can validate the contract
    shape without calling a real provider."""
    def __init__(self, payload: dict | str = {"delta": 1.0}):
        self._payload = payload
    async def ainvoke(self, *args, **kwargs):
        import json
        return json.dumps(self._payload) if not isinstance(self._payload, str) else self._payload
    def invoke(self, *args, **kwargs):
        import json
        return json.dumps(self._payload) if not isinstance(self._payload, str) else self._payload
```

Agent prompts (one per B):
- B01: `Inspect market_service/nooa-harness/agents.py lines around the Agent import and MarketAnalyst class definition. Run a micro-test in a subprocess that imports nooa at v0.0.6, instantiates MarketAnalyst with the StubLLM above, and inspects the class attributes symbol, remit, hidden fields. Write docs/nooa-kb/behavior/B01-agents-base.md — only this file.`
- B02: same pattern, but for `DeltaOrderflowAgent`'s and `MacroAgent`'s `@strategy(PredictStrategy())` usage.
- B03: same pattern, but for `ControllerAgent`'s `@strategy(CodeActStrategy())` — explicitly verify whether executing the controller's strategy yields access to the `_current_envelope` and `_specialist_reports` attributes (per the TODO(nooa-security) comment in agents.py around line 340).
- B04: same pattern, but for `DynamicContext("self._envelope_schema()")` and similar expressions.
- B05: same pattern, but for the `spec(...)` and `hidden` usages around the `_current_envelope` and `_specialist_reports` annotations.
- B06: same pattern, but for `market_service/nooa_harness/backends.py` exercising `nooa.unifiedllm.registry.get_llm_client`.
- B07: same pattern, but for `market_service/nooa_harness/runner.py`'s env-var handling and `ModelBackendConfig.from_env()`.
- B08: same pattern, but for `market_service/nooa_harness/suite.py`'s `AnalystSuite.analyze()` orchestration (without invoking the LLM, just verify the composition and timeout/exception handling contract matches the documented behavior).

Each worker reaffirms: "Write only to your assigned B0n-*.md file. Micro-tests run in subprocess with a 30-second timeout. If you cannot validate without a real LLM, the verdict is `unverified_by_execution`."

**Step 4.4 — Verify all eight B files exist and contain the verdict line**

Run:
```bash
for n in 01 02 03 04 05 06 07 08; do
  slug=$(ls docs/nooa-kb/behavior/B${n}-*.md 2>/dev/null | head -1)
  if [ -z "$slug" ]; then echo "MISSING: B${n}"; exit 1; fi
  for h in "## Call site" "## Imports exercised" "## Micro-test code" "## Observed behavior" "## Documented behavior" "## Drift verdict"; do
    grep -q "$h" "$slug" || { echo "MISSING SECTION '$h' in $slug"; exit 1; }
  done
done
echo "B-lane OK"
```

Expected: prints `B-lane OK`.

**Step 4.5 — Post-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK` with 8 new allowed changes, 0 violations.

**Step 4.6 — Commit**

```bash
git add docs/nooa-kb/behavior/
git commit -m "feat(nooa-kb): Stage 2 B-lane — behavior micro-tests (8 files)"
```

---

## Task 5: Stage 3 — Synthesis

**Files:** (all under `docs/nooa-kb/assessment/`)
- Create:
  - `import-coverage.md` (S1) — every import in `market_service/nooa_harness/**` ↔ public name in `nooa` v0.0.6
  - `decorator-usage.md` (S2) — every `@strategy` decorator instance vs documented strategy intent
  - `pin-drift.md` (S3) — compare `v0.0.6` to latest tag (use `git ls-remote --tags https://github.com/NVIDIA-NeMo/labs-OO-Agents.git` to discover latest)
  - `security-boundaries.md` (S4) — synthesized from I-lane + B03 specifically

**Interfaces:** Each synthesis file must:
1. Cite at least one source under `from-monorepo/`, `from-internet/`, or `behavior/` per claim (relative path + section heading).
2. End with a `## Conflicts` section listing any disagreements between sources.
3. Use confidence ratings per spec §7.

**Step 5.1 — Pre-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK`. No changes since Task 4.5.

**Step 5.2 — Dispatch synthesis agent**

Single Agent call. Prompt:

```
Read every file under docs/nooa-kb/from-monorepo/, docs/nooa-kb/from-internet/,
and docs/nooa-kb/behavior/. Produce four synthesis files, each cited:

S1 → docs/nooa-kb/assessment/import-coverage.md
     For every `import` or `from … import …` line in
     market_service/nooa_harness/** that we find via grep, record:
       - The import line + file:line
       - The upstream public name + where it is defined (cite M-lane file)
       - Verdict: matched | missing-in-upstream | deprecated-in-upstream
       - Confidence

S2 → docs/nooa-kb/assessment/decorator-usage.md
     For every `@strategy(` decorator instance in
     market_service/nooa_harness/**, record:
       - file:line
       - strategy class used
       - the documented intent of that strategy (cite M-lane file)
       - whether our context block + signature align with the docstring
       - Drift, if any

S3 → docs/nooa-kb/assessment/pin-drift.md
     Run:
       git ls-remote --tags https://github.com/NVIDIA-NeMo/labs-OO-Agents.git
     Record:
       - All tags semver-sorted
       - Latest stable tag past v0.0.6
       - Whether CHANGELOG / release notes between v0.0.6 and latest show
         breaking changes affecting our usage
       - Confidence + caveats

S4 → docs/nooa-kb/assessment/security-boundaries.md
     Synthesize the I-lane findings + B03 (CodeActStrategy behavior)
     into a security-boundary narrative. Explicitly address the
     `# TODO(nooa-security)` comment in agents.py around line 340.
     Cite I-lane URLs and B03 file path.

Constraints: write only to the four target paths under
docs/nooa-kb/assessment/. Cite every claim. Use confidence ratings
from spec §7.
```

**Step 5.3 — Verify all four synthesis files exist and contain a `## Conflicts` section**

Run:
```bash
for f in import-coverage decorator-usage pin-drift security-boundaries; do
  test -f "docs/nooa-kb/assessment/${f}.md" || { echo "MISSING: $f"; exit 1; }
  grep -q "## Conflicts" "docs/nooa-kb/assessment/${f}.md" || { echo "MISSING '## Conflicts' in ${f}.md"; exit 1; }
done
echo "Synthesis OK"
```

Expected: prints `Synthesis OK`.

**Step 5.4 — Post-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK` with 4 new allowed changes, 0 violations.

**Step 5.5 — Commit**

```bash
git add docs/nooa-kb/assessment/import-coverage.md \
        docs/nooa-kb/assessment/decorator-usage.md \
        docs/nooa-kb/assessment/pin-drift.md \
        docs/nooa-kb/assessment/security-boundaries.md
git commit -m "feat(nooa-kb): Stage 3 synthesis — coverage, decorator, drift, security"
```

---

## Task 6: Stage 4 — State assessment + KB README

**Files:**
- Create:
  - `docs/nooa-kb/assessment/state-snapshot.md` (FINAL)
  - `docs/nooa-kb/README.md`

**Interfaces:**
- `state-snapshot.md` sections (in order):
  1. `# NOOA integration — state snapshot`
  2. `## Summary` — 3–5 sentence narrative grading overall.
  3. `## Per-area grades` — table: Area | Grade (green/yellow/red) | Confidence | Source(s).
  4. `## Conflicts` — bullet list of unresolved contradictions in the KB.
  5. `## KB gaps` — bullet list of topics that should be added on a future run.
  6. `## Suggested remediation` — bullet list, no code in this file.
- `README.md` sections:
  1. What this KB is.
  2. How to read it (start at `state-snapshot.md`, then drop into the `assessment/`, `from-*`, `behavior/` sections).
  3. Freshness policy: KB is regenerated when `docs/nooa-kb/sources/pin.json` ref changes.
  4. Authoring: only the agents in this plan write here; humans review PRs.

**Step 6.1 — Pre-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK`. No changes since Task 5.4.

**Step 6.2 — Dispatch state-assessment agent**

Single Agent call. Prompt:

```
Read everything under docs/nooa-kb/. Do not introduce new facts — only
summarize the KB and grade it. If a fact you need is missing, write
"KB gap: <topic>" rather than making it up.

Produce:

  docs/nooa-kb/assessment/state-snapshot.md
    - Section headers exactly:
        # NOOA integration — state snapshot
        ## Summary
        ## Per-area grades
        ## Conflicts
        ## KB gaps
        ## Suggested remediation
    - Per-area grades cover at minimum:
        Imports & coverage
        Decorator usage (Predict vs CodeAct)
        DynamicContext semantics
        Agentdoc (spec/hidden)
        UnifiedLLM backends
        Pin & drift
        Security boundaries (CodeActStrategy TODO)

  docs/nooa-kb/README.md
    - Section headers as specified in the plan.
    - Plain prose. No new facts beyond what is in the KB.

Constraint: write only to the two paths above.
```

**Step 6.3 — Verify both files exist with required sections**

Run:
```bash
test -f docs/nooa-kb/assessment/state-snapshot.md || { echo "MISSING: state-snapshot.md"; exit 1; }
for h in "## Summary" "## Per-area grades" "## Conflicts" "## KB gaps" "## Suggested remediation"; do
  grep -q "$h" docs/nooa-kb/assessment/state-snapshot.md || { echo "MISSING '$h'"; exit 1; }
done
test -f docs/nooa-kb/README.md || { echo "MISSING: README.md"; exit 1; }
echo "Stage 4 OK"
```

Expected: prints `Stage 4 OK`.

**Step 6.4 — Post-flight boundary check**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK` with 2 new allowed changes, 0 violations.

**Step 6.5 — Commit**

```bash
git add docs/nooa-kb/assessment/state-snapshot.md docs/nooa-kb/README.md
git commit -m "feat(nooa-kb): Stage 4 state assessment + KB README"
```

---

## Task 7: Final verification + handoff

**Files:** none modified.

**Step 7.1 — Run the boundary check one last time**

Run: `uv run --python 3.12 python docs/nooa-kb/_build/check_write_boundary.py`

Expected: `OK` with no violations. The list of allowed changes should be exactly: the 3 sources files, 7 M-lane files, 4 I-lane files, 9 B-lane files (call-sites.md + 8 B0n), 4 synthesis files, 2 stage-4 files = 29 files plus the spec-edits file. Any other path violates §9.

**Step 7.2 — Run a full KB structure verification**

Run:
```bash
uv run --python 3.12 python - <<'PY'
import json
from pathlib import Path

root = Path("docs/nooa-kb")

# Sources
assert (root / "sources" / "pin.json").exists()
pin = json.loads((root / "sources" / "pin.json").read_text())
assert pin["ref"] == "v0.0.6"
assert len(pin["sha"]) == 40
assert Path(pin["clone_path"]).is_dir(), f"clone path missing: {pin['clone_path']}"

# All expected output files exist
expected = [
    "sources/monorepo-inventory.md",
    "sources/internet-index.md",
    "from-monorepo/packages-nooa/exports.md",
    "from-monorepo/packages-nooa/strategies.md",
    "from-monorepo/packages-nooa/context-blocks.md",
    "from-monorepo/packages-nooa/agentdoc.md",
    "from-monorepo/packages-nooa/unifiedllm.md",
    "from-monorepo/packages-nooa-cli/cli.md",
    "from-monorepo/docs-markdown.md",
    "from-internet/reddit.md",
    "from-internet/nvidia-blogs.md",
    "from-internet/github-issues.md",
    "from-internet/third-party.md",
    "behavior/call-sites.md",
    "behavior/B01-agents-base.md",
    "behavior/B02-strategy-predict.md",
    "behavior/B03-strategy-codeact.md",
    "behavior/B04-dynamic-context.md",
    "behavior/B05-agentdoc.md",
    "behavior/B06-unifiedllm.md",
    "behavior/B07-runner-env.md",
    "behavior/B08-suite-composition.md",
    "assessment/import-coverage.md",
    "assessment/decorator-usage.md",
    "assessment/pin-drift.md",
    "assessment/security-boundaries.md",
    "assessment/state-snapshot.md",
    "README.md",
]
missing = [p for p in expected if not (root / p).exists()]
assert not missing, f"missing: {missing}"
print(f"KB complete: {len(expected)} expected output files all present.")
PY
```

Expected: prints `KB complete: 28 expected output files all present.` (count = 28 file path strings).

**Step 7.3 — Update MEMORY.md (best-effort)**

If `~/.claude/projects/-Users-kamii-Documents-crypto-ai-anal/memory/MEMORY.md` exists, append a single line:

```
- [NOOA knowledge base](docs/NOOA_KNOWLEDGE_BASE_DESIGN.md) — the spec + plan for fan-out ingestion that produced `docs/nooa-kb/`.
```

Do NOT create `MEMORY.md` if it doesn't exist; this is optional.

**Step 7.4 — Print the final report**

Run:
```bash
echo "==== NOOA KB build complete ===="
echo "Total KB files:"
find docs/nooa-kb -type f | wc -l
echo
echo "Final state snapshot:"
echo "  docs/nooa-kb/assessment/state-snapshot.md"
echo
echo "Next steps for the human partner:"
echo "  1. Open docs/nooa-kb/assessment/state-snapshot.md"
echo "  2. Read the 'Suggested remediation' section"
echo "  3. Open a separate brainstorming session for any remediation work"
```

Expected output indicates the build is complete.

**Step 7.5 — No commit**

The final commit was made in Task 6.5 (state-snapshot.md + README.md). This task produces no further repo changes — only verification output.

---

## Self-Review

**1. Spec coverage** — every numbered requirement in the spec has a task:

- §1 purpose → covered by all of Stages 1–4; narrative confirmed in Task 6 state-snapshot.md.
- §2 scope → covered by Task 1 (clone + internet index).
- §3 architecture (4 stages) → covered by Tasks 1, 2–4, 5, 6.
- §4 file tree → every file in the spec file tree appears under the corresponding task's "Files" block. ✓
- §5 worker contracts (M, I, B, synthesis, assessment) → reflected in Step 2.2, 3.2, 4.3, 5.2, 6.2 prompts. ✓
- §6 behavior sandbox → StubLLM provided in Task 4.3; unverified_by_execution verdict documented in B-lane file schema. ✓
- §7 failure modes & confidence → handled in boundary checks (Task 0 / per-task post-flights) and confidence ratings in each output schema. ✓
- §8 implementation order → Tasks 1 through 6 run in stage order; Task 7 is the verification wrap. ✓
- §9 write boundary → enforced via Task 0 tool + per-task pre/post-flight checks. ✓
- §10 open questions → left as open per the spec. Not addressed by this plan; explicitly called out for follow-up. ✓

**2. Placeholder scan** — none of "TBD", "TODO", "implement later", "fill in details", "appropriate error handling", "similar to task N" appear.

**3. Type / signature consistency** — `check_write_boundary()` signature in Task 0 matches the usage in every pre/post-flight step (`baseline_path` is required, `git_status_path` is optional with run-as-default behavior). `BoundaryReport.ok`, `BoundaryReport.violations`, `BoundaryReport.allowed_changes` match definitions. The `pin.json` schema is the single source of truth across Task 1.3 pin.json write and Task 7.2 verification.
