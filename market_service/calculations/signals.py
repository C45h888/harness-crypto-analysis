"""Deterministic, explainable state-change signals."""

from __future__ import annotations

from typing import Any


def deterministic_signals(snapshot: dict[str, Any], previous: dict[str, Any] | None) -> list[dict[str, Any]]:
    spot_buy = snapshot.get("spot_buy_share", 0.5)
    futures_buy = snapshot.get("futures_buy_share", 0.5)
    signals: list[dict[str, Any]] = []

    def divergence(spot: float, futures: float) -> str | None:
        if spot <= 0.40 and futures >= 0.60:
            return "spot_selling_futures_buying"
        if spot >= 0.60 and futures <= 0.40:
            return "spot_buying_futures_selling"
        return None

    current_divergence = divergence(spot_buy, futures_buy)
    prior_divergence = divergence(
        previous.get("spot_buy_share", 0.5), previous.get("futures_buy_share", 0.5)
    ) if previous else None
    if current_divergence and current_divergence != prior_divergence:
        summary = (
            "Spot takers are selling while futures takers are buying."
            if current_divergence == "spot_selling_futures_buying"
            else "Spot takers are buying while futures takers are selling."
        )
        signals.append({
            "signal_type": "spot_futures_divergence", "severity": 3, "summary": summary,
            "evidence": {
                "rule": "spot_buy_share <= 0.40 and futures_buy_share >= 0.60, or inverse",
                "spot_buy_share": spot_buy, "futures_buy_share": futures_buy,
                "previous": previous,
            },
        })

    obi = snapshot.get("spot_obi_top_n", snapshot.get("spot_obi"))
    prior_obi = previous.get("spot_obi_top_n", 0.0) if previous else 0.0
    if obi is not None and abs(obi) >= 0.45 and abs(prior_obi) < 0.45:
        signals.append({
            "signal_type": "spot_depth_imbalance", "severity": 2,
            "summary": f"Spot top-of-book depth is materially {'bid' if obi > 0 else 'ask'}-heavy.",
            "evidence": {
                "rule": "abs(spot_obi_top_n) >= 0.45 and previous_abs_obi < 0.45",
                "spot_obi_top_n": obi, "previous_spot_obi_top_n": prior_obi,
            },
        })

    current_oi = snapshot.get("open_interest")
    previous_oi = previous.get("open_interest") if previous else None
    if current_oi is not None and previous_oi:
        oi_change = (current_oi - previous_oi) / previous_oi
        if abs(oi_change) >= 0.01:
            signals.append({
                "signal_type": "open_interest_shift", "severity": 2,
                "summary": "Open interest moved by at least 1% between collection intervals.",
                "evidence": {
                    "rule": "abs(current_oi - previous_oi) / previous_oi >= 0.01",
                    "oi_change": oi_change, "previous_oi": previous_oi, "current_oi": current_oi,
                },
            })
    return signals
