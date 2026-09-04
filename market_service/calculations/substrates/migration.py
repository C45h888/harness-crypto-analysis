"""Migration substrate — keystone migration over time.

hourly_keystone_migration (intra-window, over the trade tape) and
keystone_cycle_migration (cross-cycle, over the durable keystone ledger).
Both compare consecutive keystone prices against a width and emit an
UP / DOWN / FLAT verdict. Pure and deterministic.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def hourly_keystone_migration(trades: Iterable[dict], bucket_size: float = 0.05,
                              width: float = 0.20) -> dict:
    """Hourly buyer-keystone migration over the window spanned by ``trades``.

    For each hour, the keystone is the price bucket (of size ``bucket_size``)
    where aggressive taker BUY notional/volume is most concentrated. Migration
    verdict compares consecutive hourly keystones:
      UP when keystone rises, DOWN when it falls, FLAT otherwise.
    Returns keys: ``hourly`` (list), ``verdict``, ``net_buckets``.
    """
    hourly: dict[int, dict] = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "buys_by_px": defaultdict(float)})
    for t in trades:
        hr = (int(t["ts"]) // 3600000) * 3600000
        d = hourly[hr]
        qty = float(t["qty"])
        if t.get("is_buyer_maker"):
            d["sell"] += qty
        else:
            d["buy"] += qty
            d["buys_by_px"][round(float(t["price"]) / bucket_size) * bucket_size] += qty

    rows: list[dict] = []
    prev_k = None
    for hr in sorted(hourly):
        d = hourly[hr]
        buys_by_px = d["buys_by_px"]
        keystone = max(buys_by_px, key=buys_by_px.get) if buys_by_px else None
        direction = None
        if keystone is not None and prev_k is not None:
            diff = keystone - prev_k
            direction = "UP" if diff > width else ("DOWN" if diff < -width else "FLAT")
        rows.append({"hour_start_ms": hr, "keystone": keystone, "buy_vol": d["buy"],
                     "sell_vol": d["sell"], "migration": direction})
        if keystone is not None:
            prev_k = keystone
    ascending = sum(1 for r in rows if r["migration"] == "UP")
    descending = sum(1 for r in rows if r["migration"] == "DOWN")
    verdict = "MIGRATING_UP" if ascending > descending else ("MIGRATING_DOWN" if descending > ascending else "FLAT")
    return {"hourly": rows, "verdict": verdict,
            "net_buckets": ascending - descending}


def keystone_cycle_migration(
    snapshots: Iterable[dict],
    width: float = 0.20,
) -> dict[str, Any]:
    """Cross-cycle keystone migration over the recorded keystone ledger.

    ``snapshots`` are keystone_history rows (Redis stream payloads or
    Postgres rows) in ANY order — they are sorted by ``cycle_ts``
    internally, oldest first. Rows with a missing/None ``keystone_price``
    are skipped (null discipline: no fabricated price). Consecutive
    keystone prices are compared against ``width``:

      delta > +width  → UP
      delta < -width  → DOWN
      otherwise       → FLAT

    Returns ``{cycles, verdict, net_buckets}`` where ``verdict`` is
    MIGRATING_UP / MIGRATING_DOWN / FLAT over the recorded runs. The first
    cycle in the series carries ``migration: None`` (no prior to compare).
    Pure and deterministic — the read-side companion to
    ``hourly_keystone_migration`` (intra-window) and the durable seam to
    ``keystone_history`` (cross-cycle).
    """
    rows: list[tuple[str, float]] = []
    for s in snapshots:
        if not isinstance(s, dict):
            continue
        ts = s.get("cycle_ts") or ""
        kp = s.get("keystone_price")
        try:
            kp_f = float(kp) if kp is not None else None
        except (TypeError, ValueError):
            kp_f = None
        if kp_f is None:
            continue
        rows.append((str(ts), kp_f))
    rows.sort(key=lambda r: r[0])

    cycles: list[dict] = []
    prev: float | None = None
    for ts, kp in rows:
        direction: str | None = None
        if prev is not None:
            diff = kp - prev
            direction = "UP" if diff > width else ("DOWN" if diff < -width else "FLAT")
        cycles.append({"cycle_ts": ts, "keystone": kp, "migration": direction})
        prev = kp

    ascending = sum(1 for c in cycles if c["migration"] == "UP")
    descending = sum(1 for c in cycles if c["migration"] == "DOWN")
    verdict = ("MIGRATING_UP" if ascending > descending
               else ("MIGRATING_DOWN" if descending > ascending else "FLAT"))
    return {"cycles": cycles, "verdict": verdict,
            "net_buckets": ascending - descending}