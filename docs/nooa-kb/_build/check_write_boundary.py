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