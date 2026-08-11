"""Backward-compatible CLI wrapper for the canonical market analysis."""

from market_service.analysis.market import analyze, main, render

__all__ = ["analyze", "render", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
