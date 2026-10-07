"""Deprecated shim — trashed to ~30 lines (was 516).

Canonical home: ``market_service.interaction_plane``. This module
re-exports the thin CLI adapter so existing imports
(``build_parser``, ``main``, route handlers) and Hermes shell-outs
keep working. Zero logic lives here.
"""

from __future__ import annotations

from market_service.interaction_plane.cli import (
    _poller_control,
    _read_horizon,
    _read_keystone_history,
    _read_market,
    _read_microstructure_status,
    _read_substrates,
    build_parser,
    main,
)

__all__ = [
    "_poller_control",
    "_read_horizon",
    "_read_keystone_history",
    "_read_market",
    "_read_microstructure_status",
    "_read_substrates",
    "build_parser",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
