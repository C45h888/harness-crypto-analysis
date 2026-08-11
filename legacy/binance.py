"""Backward-compatible import path for the canonical Binance client.

New code should import from ``market_service.clients.binance``.
"""
from market_service.clients.binance import *  # noqa: F401,F403
