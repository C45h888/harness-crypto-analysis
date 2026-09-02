#!/usr/bin/env python
"""Seed Cont-Kukanov-Stoikov paper KB into MemoryNode as real working memory.

Paper: https://arxiv.org/pdf/1011.6402
Tags: paper, cont1011, ofi, depth_scaling, heteroskedastic
Session: paper-kb://cont1011 (stable UUID, recallable by agent via memory.recall_paper)
"""

import asyncio
import uuid

from market_service.config import Settings
from market_service.nooa_harness.memory import MemoryNode

PAPER_SESSION = str(uuid.uuid5(uuid.NAMESPACE_URL, "paper-kb://cont1011"))

FACTS = [
    ("e_n definition: e_n = 1{PB_n >= PB_{n-1}} qB_n - 1{PB_n <= PB_{n-1}} qB_{n-1} - 1{PA_n <= PA_{n-1}} qA_n + 1{PA_n >= PA_{n-1}} qA_{n-1} — best Level I only, price change OR queue change counts", 9.0, ("paper","cont1011","ofi","e_n")),
    ("OFI_k = sum_{t_{k-1} <= tau_n < t_k} e_n — half-open, clock-bound interval, not poll-bound;OFI interval quality exact_feed vs snapshot_approximation", 9.0, ("paper","cont1011","ofi","interval","clock")),
    ("Empirical: ΔP_k = α_i + β_i·OFI_k + ε_k — β_i fitted per block i, OLS HC0 SE, separate k sub-intervals within i", 9.0, ("paper","cont1011","beta","price_impact","regression")),
    ("Depth scaling: β_i = c·AD_i^{-λ} + ν_i ⇔ ln β_i = ln c - λ·ln AD_i — AD_i = event-average (qB+qA)/2, needs ≥3 distinct AD_i blocks, min_blocks=3", 9.0, ("paper","cont1011","depth_scaling","AD","lambda")),
    ("Substituted combined ΔP_k = α_i + c·OFI_k/AD_i^λ + (ν_i·OFI_k + ε_k) — DERIVED hypothesis with heteroskedastic ν·OFI, never a shortcut single calculation. Use as diagnostic only.", 9.0, ("paper","cont1011","heteroskedastic","derived_diagnostic")),
    ("Price unit: ticks via tick_size; quantity_unit matters (spot base vs perps contracts); sequence gaps invalidate reconstruction", 8.5, ("paper","cont1011","tick","quantity_unit","quality")),
    ("Quality gates: exact_feed vs snapshot_approximation, excluded_observations, heteroskedasticity_flag via White, R², SE/β", 8.5, ("paper","cont1011","quality","heteroskedasticity")),
    ("Mechanical constant-depth ΔP≈OFI/(2D) is motivation only, not fitted rule — empirical β captures deeper levels/rounding", 8.0, ("paper","cont1011","mechanical_model")),
    ("Depth estimator event_mean_best_bid_ask_v1, AD_i null → depth scaling insufficient, c/lambda null discipline", 8.0, ("paper","cont1011","depth_estimator","null_discipline")),
    ("SOL perps venue: use raw poller flow/wall for AD/OFI validation, micro tape is spot-only until perps WS added", 7.5, ("paper","cont1011","sol_perps","venue")),
]

async def main():
    settings = Settings.from_env()
    node = MemoryNode.from_settings(settings)
    print(f"Seeding paper KB session {PAPER_SESSION}")
    for content, importance, tags in FACTS:
        mem = await node.remember(PAPER_SESSION, "fact", content, importance=importance, tags=tags)
        print(f"  remembered {mem.memory_id[:8]} {tags[1]} {content[:60]}...")
    # verify recall
    from market_service.runtime.redis_store import RedisRuntimeStore
    # recall test
    mems = await node.recall(PAPER_SESSION, query="OFI AD beta heteroskedastic", limit=8)
    print(f"Recall test: {len(mems)} facts")
    for m in mems[:3]:
        print(f"  - {m.content[:80]}")
    await node.postgres.close()
    await node.redis.close()
    print("Done. Agent can now call memory.recall_paper -> MemoryNode.recall(paper-kb session)")

if __name__ == "__main__":
    asyncio.run(main())
