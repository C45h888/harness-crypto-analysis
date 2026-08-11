"""Backward-compatible import path for canonical calculations.

New code should import from ``market_service.calculations.flow``.
"""
from market_service.calculations.flow import *  # noqa: F401,F403
