"""Large-print substrate worker — big-taker-print tape classifiers.

Bounded to exactly ONE substrate: ``calculations.substrates.large_print``.
Watches the futures taker tape for institutional-size prints; fires when a
new large print lands or the seller-aggression regime changes; computes the
technical section's large-print half (``tiered_large_flow`` +
``seller_aggression``) that the composition root runs.

Probe (deterministic, substrate tiers only):

  large_print     — a print with qty >= the substrate's large tier (50.0,
                    the first entry of ``tiered_large_flow``'s default
                    tiers) whose trade id was not in the last persisted
                    state. The print rides as the predicate.
  aggression_flip — ``seller_aggression_classify`` classification changed
                    vs last state (HIGH/MEDIUM/LOW — the legacy
                    seller_wall_check.py:233-240 discriminator documented
                    in the substrate docstring). A missing side (None)
                    never fires — no evidence, no decision.
"""

from __future__ import annotations

from typing import Any

from market_service.calculations.substrates.large_print import (
    seller_aggression_classify,
    tiered_large_flow,
)
from market_service.substrate_worker.contracts import CadenceProfile, TriggerDecision
from market_service.substrate_worker.core import SubstrateWorkerCore

# The substrate's large tier: tiered_large_flow's default tiers are
# (large 50.0, huge 200.0, whale 500.0). The probe watches the entry tier.
LARGE_TIER_QTY = 50.0
LARGE_PRINT_KEEP = 50  # persisted prints (bounded; probe needs id recall)


def _large_prints(trades: Any) -> list[dict[str, Any]]:
    prints: list[dict[str, Any]] = []
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        try:
            qty = float(t["qty"])
        except (KeyError, TypeError, ValueError):
            continue
        if qty < LARGE_TIER_QTY:
            continue
        try:
            price = float(t["price"])
        except (KeyError, TypeError, ValueError):
            continue
        prints.append({
            "id": t.get("id"),
            "ts": t.get("ts"),
            "price": price,
            "qty": qty,
            "side": "sell" if t.get("is_buyer_maker") else "buy",
        })
    prints.sort(key=lambda p: (p["ts"] or 0, p["qty"]), reverse=True)
    return prints[:LARGE_PRINT_KEEP]


class LargePrintWorker(SubstrateWorkerCore):
    SUBSTRATE_NAME = "large_print"
    INPUT_STREAMS = ("raw", "microstructure")
    CADENCE = CadenceProfile(cooldown_s=5, staleness_s=60, ws_input=True)

    # ------------------------------------------------------------------
    # L2 — significance probe (new large print / aggression regime flip)
    # ------------------------------------------------------------------

    def probe(
        self, window: dict[str, Any], last_state: dict[str, Any] | None, now_ms: int,
    ) -> TriggerDecision:
        fut_trades = (window.get("futures") or {}).get("trades_normalized") or []
        if not fut_trades:
            return TriggerDecision(fired=False, source="probe",
                                   predicates={"reason": "no_futures_trades"})
        predicates: dict[str, Any] = {}
        prev_output = (last_state or {}).get("output") or {}

        # Event-level: any print at/above the large tier unseen in last state.
        current = _large_prints(fut_trades)
        prev_ids = {
            p.get("id") for p in (prev_output.get("large_prints") or [])
            if isinstance(p, dict) and p.get("id") is not None
        }
        for pr in current:
            if pr["id"] is not None and pr["id"] not in prev_ids:
                predicates[f"large_print:{pr['id']}"] = dict(pr)
            elif pr["id"] is None and not prev_ids:
                # No ids anywhere (cold-ish tape): the top print is news.
                predicates["large_print:unidentified"] = dict(pr)
                break

        # Regime-level: seller-aggression classification flip.
        current_aggression = seller_aggression_classify(fut_trades)
        cur_class = current_aggression.get("classification") if isinstance(
            current_aggression, dict) else None
        prev_class = (prev_output.get("seller_aggression") or {}).get(
            "classification") if isinstance(
            prev_output.get("seller_aggression"), dict) else None
        if cur_class is not None and prev_class is not None and cur_class != prev_class:
            predicates["aggression_flip"] = {"from": prev_class, "to": cur_class}

        return TriggerDecision(fired=bool(predicates), source="probe",
                               predicates=predicates)

    # ------------------------------------------------------------------
    # Compute — large_print substrate functions ONLY
    # ------------------------------------------------------------------

    def compute(self, evidence: dict[str, Any], depth: int) -> dict[str, Any]:
        fut_trades = (evidence.get("futures") or {}).get("trades_normalized") or []
        if not fut_trades:
            # Null discipline: no tape means no print classification.
            return {}
        return {
            "tiered_large_flow": tiered_large_flow(fut_trades),
            "seller_aggression": seller_aggression_classify(fut_trades),
            "large_prints": _large_prints(fut_trades),
        }
