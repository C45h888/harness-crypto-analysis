"""Mounted ``nooa`` CLI — the canonical harness CLI exposed at the repo root.

The official ``nooa`` console script (``nooa_cli:main``) only knows the
commands bundled inside its own ``nooa_cli/commands/`` package. The runtime
doctrine forbids mutating that installed package from the harness surface,
so instead of dropping a shim module into the framework's site-packages this
module performs an import-time mount: it attaches the repo's ``market``
harness group (``market_service/commands/nooa_cli_ext.py``) onto the
framework root ``oo`` group and then delegates to the normal CLI entry.

One mount point covers every runtime surface:

    shell (VSCode):      ./nooa market envelope SOLUSDT --latest
    console script:      nooa-market market envelope SOLUSDT --latest
    docker runtime:      docker compose --profile tools run --rm nooa \
                             market envelope SOLUSDT --latest
    through harness.py:  python -m market_service.commands.harness \
                             --nooa market envelope SOLUSDT --latest

The ``market`` group reads/writes canonical state through the python objects
in ``market_service/nooa_harness/`` (engine / runner / memory + the
``pipeline_*`` planes) — one reasoning layer over the deterministic
pipeline, never a second collection pipeline.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Sequence

from nooa_cli import oo as _oo

from market_service.commands.nooa_cli_ext import command as _market_command

# Import-time mount: attach the repo harness group to the framework root.
_oo.add_command(_market_command, name="market")


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _load_repo_env(root: pathlib.Path | None = None) -> None:
    """Non-clobbering load of the repo-root ``.env`` (best-effort).

    Lets a plain shell inside VSCode reach the canonical runtime
    (``REDIS_URL`` / ``DATABASE_URL`` / ``NOOA_MODEL_*``) with no extra
    exports. Environment variables already set in the shell win.
    """
    path = (root or _repo_root()) / ".env"
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]

        load_dotenv(path, override=False)
        return
    except Exception:
        pass
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def make_cli():
    """Return the mounted root ``oo`` click group (test seam)."""
    return _oo


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry for the mounted NOOA CLI."""
    _load_repo_env()
    args = list(sys.argv[1:] if argv is None else argv)
    _oo.main(args=args, prog_name="nooa", standalone_mode=False)
    return 0


__all__ = ["main", "make_cli", "_load_repo_env", "_repo_root"]


if __name__ == "__main__":
    raise SystemExit(main())