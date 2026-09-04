"""Calculation substrates — the decomposed calculation layer.

Each substrate is ONE independently-testable calculation concern. Hard rules:

1. A substrate NEVER imports another substrate. If a substrate needs another
   substrate's output, an orchestrator (``nooa_harness``) composes it and
   passes the value in as an argument.
2. Every substrate is pure and deterministic over normalized inputs (books,
   trades, klines) — no I/O, no Binance, no Redis.
3. Every substrate declares a single-responsibility boundary; a rule change
   lands in exactly one substrate.

Substrate map (canonical address = ``market_service.calculations.substrates``):

    tape             taker-tape aggregation: summarize, bucketed_cvd,
                     correlation, turnover share, price-bucketed flow
    density          order-book density / keystone detection / zone grids
    ladders          cumulative bid/ask stacks (absorption, keystone stack,
                     ask-wall ladder)
    migration        keystone migration over time (hourly + cross-cycle)
    anchors          dynamic round-number anchors + anchor aggregation
    tiers            USD-notional tier buckets + institutional balance
    technicals       time-series technicals: EMA, ATR%, trend slope/drift
    large_print      large-print tape classifiers: tiered flow, seller aggression
    volume_profile   price-bucketed volume profile (POC / value area)
    delta            signed DELTA variable (wall imbalance + flow alignment)
    signals          deterministic state-change signals

The legacy module paths (``calculations.flow``, ``calculations.orderbook``,
...) remain as thin re-export seams for backward-compatible imports; new code
imports from this package.
"""