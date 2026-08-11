from __future__ import annotations

from typing import Any


def deterministic_signals(snapshot: dict[str, Any], previous: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return explainable alerts only; this module never creates orders or trade instructions."""
    spot_buy = snapshot["spot_buy_share"]
    futures_buy = snapshot["futures_buy_share"]
    signals: list[dict[str, Any]] = []

    divergence = None
    if spot_buy <= 0.40 and futures_buy >= 0.60:
        divergence = "spot_selling_futures_buying"
    elif spot_buy >= 0.60 and futures_buy <= 0.40:
        divergence = "spot_buying_futures_selling"
    prior_divergence = None
    if previous:
        if previous.get("spot_buy_share", 0.5) <= 0.40 and previous.get("futures_buy_share", 0.5) >= 0.60:
            prior_divergence = "spot_selling_futures_buying"
        elif previous.get("spot_buy_share", 0.5) >= 0.60 and previous.get("futures_buy_share", 0.5) <= 0.40:
            prior_divergence = "spot_buying_futures_selling"

    if divergence == "spot_selling_futures_buying" and divergence != prior_divergence:
        signals.append({
            "signal_type": "spot_futures_divergence",
            "severity": 3,
            "summary": "Spot takers are selling while futures takers are buying.",
            "evidence": {"spot_buy_share": spot_buy, "futures_buy_share": futures_buy},
        })
    elif divergence == "spot_buying_futures_selling" and divergence != prior_divergence:
        signals.append({
            "signal_type": "spot_futures_divergence",
            "severity": 3,
            "summary": "Spot takers are buying while futures takers are selling.",
            "evidence": {"spot_buy_share": spot_buy, "futures_buy_share": futures_buy},
        })

    prior_obi = abs(previous.get("spot_obi_top_n", 0.0)) if previous else 0.0
    if abs(snapshot["spot_obi_top_n"]) >= 0.45 and prior_obi < 0.45:
        side = "bid" if snapshot["spot_obi_top_n"] > 0 else "ask"
        signals.append({
            "signal_type": "spot_depth_imbalance",
            "severity": 2,
            "summary": f"Spot top-of-book depth is materially {side}-heavy.",
            "evidence": {"spot_obi_top_n": snapshot["spot_obi_top_n"]},
        })

    if previous and previous.get("open_interest"):
        oi_change = (snapshot["open_interest"] - previous["open_interest"]) / previous["open_interest"]
        if abs(oi_change) >= 0.01:
            signals.append({
                "signal_type": "open_interest_shift",
                "severity": 2,
                "summary": "Open interest moved by at least 1% between collection intervals.",
                "evidence": {"oi_change": oi_change, "previous_oi": previous["open_interest"], "current_oi": snapshot["open_interest"]},
            })
    return signals
