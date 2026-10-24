"""Scope — the ONE identity authority for (symbol, venue).

The runtime analyses one instrument on one venue: SOLUSDT on futures. Before
this module that identity was re-declared in nine places (``.env``,
``docker-compose.yml``, ``Settings.symbols``, the poller's Redis control key,
and eleven CLI literals) with two conflicting defaults, and ``venue`` was not
modelled at all — it entered the engine through a literal ``"spot"`` default
parameter while capture wrote ``futures``. The agent then read
``microstructure:spot:SOLUSDT:*`` for data that lived at
``microstructure:futures:SOLUSDT:*`` and got zero intervals.

``Scope`` is the single place identity is decided. Every surface — CLI
defaults, engine attributes, tool scopes, ``deterministic_state``,
``session_id`` — derives from it and re-declares nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Frozen venue vocabulary. ``perps`` is Binance's word for the futures
# product; it is accepted as an INPUT and normalised to ``futures`` so the
# stored vocabulary and the exchange vocabulary never diverge.
VENUES: tuple[str, ...] = ("futures", "spot", "perps")

# The venue this runtime trades. Confirmed by microstructure/capture.py:
# "Primary venue is now futures (SOL-USDT perps) via fstream WS — spot is
# legacy." The capture container and the engine must agree on this or every
# OFI read silently returns zero intervals.
CANONICAL_VENUE = "futures"

# Alias -> canonical.
_VENUE_ALIASES: dict[str, str] = {"perps": "futures"}

# Fallback when MICROSTRUCTURE_VENUE is unset. Deliberately the canonical
# venue, never "spot": a missing env var must not silently point the engine
# at the legacy venue.
DEFAULT_VENUE = CANONICAL_VENUE

# Fallback when SYMBOLS is unset. Matches the instrument this runtime trades.
DEFAULT_SYMBOL = "SOLUSDT"


def normalize_venue(value: Any) -> str:
    """Normalise a venue token to the canonical vocabulary.

    Unknown values raise — a typo must never quietly resolve to a venue that
    happens to have data from another instrument.
    """
    text = str(value or "").strip().lower()
    if not text:
        raise ValueError(f"venue is required (one of: {', '.join(VENUES)})")
    normalised = _VENUE_ALIASES.get(text, text)
    if normalised not in VENUES:
        raise ValueError(
            f"unknown venue {value!r}: expected one of {', '.join(VENUES)}")
    return normalised


def normalize_symbol(value: Any) -> str:
    """Upper-case and validate a symbol token."""
    text = str(value or "").strip().upper()
    if not text:
        raise ValueError("symbol is required")
    return text


@dataclass(frozen=True)
class Scope:
    """One instrument on one venue — the identity every surface derives from."""

    symbol: str
    venue: str

    def __post_init__(self) -> None:
        # Normalise on construction so a Scope is always already canonical.
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "venue", normalize_venue(self.venue))

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def from_active(cls, active_symbols: tuple[str, ...] | list[str], venue: Any) -> "Scope":
        """Resolve from the LIVE active set (control key wins over env).

        First entry is the scope; empty set raises instead of guessing —
        refusing loudly beats analysing a phantom.
        """
        symbols = tuple(s.strip().upper() for s in (active_symbols or ()) if str(s).strip())
        if not symbols:
            raise ValueError("no active symbols — nothing is captured")
        return cls(symbol=symbols[0], venue=venue)
    @classmethod
    def from_settings(cls, settings: Any) -> "Scope":
        """Resolve the scope the runtime is configured to analyse.

        Symbol precedence: the first entry of ``Settings.symbols`` (the
        capture authority). Venue: ``Settings.venue`` (already normalised and
        validated from ``MICROSTRUCTURE_VENUE`` at Settings construction).
        """
        symbols = tuple(getattr(settings, "symbols", ()) or ())
        if not symbols:
            raise ValueError(
                "Settings.symbols is empty — no instrument is captured")
        venue = getattr(settings, "venue", None) or DEFAULT_VENUE
        return cls(symbol=symbols[0], venue=venue)

    # ------------------------------------------------------------------
    # projections
    # ------------------------------------------------------------------
    def to_state(self) -> dict[str, str]:
        """The chartered ``deterministic_state['scope']`` payload."""
        return {"symbol": self.symbol, "venue": self.venue}

    def as_scope_dict(self) -> dict[str, str]:
        """The capability-log / tool ``scope`` payload."""
        return {"symbol": self.symbol, "venue": self.venue}

    def describe(self) -> str:
        return f"{self.symbol}/{self.venue}"


@dataclass(frozen=True)
class ScopeResolution:
    """A resolved scope plus where each component came from.

    Rendered by ``nooa market scope`` so the operator can see what the
    runtime will use before running a cycle, instead of guessing at env.
    """

    scope: Scope
    symbol_source: str
    venue_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.scope.symbol,
            "venue": self.scope.venue,
            "sources": {
                "symbol": self.symbol_source,
                "venue": self.venue_source,
            },
        }


def resolve_scope(settings: Any) -> ScopeResolution:
    """Resolve the scope and record which authority supplied each half."""
    scope = Scope.from_settings(settings)
    symbol_source = "settings.symbols[0]"
    venue_source = (
        "settings.venue" if getattr(settings, "venue", None)
        else f"default ({DEFAULT_VENUE})"
    )
    return ScopeResolution(
        scope=scope, symbol_source=symbol_source, venue_source=venue_source,
    )


async def resolve_live_scope(settings: Any, store: Any) -> ScopeResolution:
    """Resolve against the LIVE active set (control key > env).

    A present control key wins and is reported as the source, so the
    operator sees a runtime re-point instead of guessing at env. Any
    store failure falls back to :func:`resolve_scope` — liveness must
    never hard-fail the scope read itself.
    """
    try:
        control = await store.read("poller.active", ids={})
    except Exception:  # noqa: BLE001 — fall back to env resolution
        control = None
    if control:
        venue = getattr(settings, "venue", None) or DEFAULT_VENUE
        try:
            return ScopeResolution(
                scope=Scope.from_active(tuple(control), venue),
                symbol_source="redis:poller:active_symbols[0]",
                venue_source=(
                    "settings.venue" if getattr(settings, "venue", None)
                    else f"default ({DEFAULT_VENUE})"
                ),
            )
        except ValueError:
            pass
    return resolve_scope(settings)


__all__ = [
    "CANONICAL_VENUE",
    "DEFAULT_SYMBOL",
    "DEFAULT_VENUE",
    "VENUES",
    "Scope",
    "ScopeResolution",
    "normalize_symbol",
    "normalize_venue",
    "resolve_live_scope",
    "resolve_scope",
]
