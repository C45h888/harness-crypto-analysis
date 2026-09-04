 /ster# Plan — market.read: OO-agent deviation from the envelope format

Slice: inner CLI / OO agent loop (first of the envelope-debt passes).
User decisions (locked):
- `market.read` fully REPLACES `market.envelope` in the codebase.
- Deep-dive becomes an explicit tool mode (`mode="full"`, raw payload,
  40k-gated) — not a hidden second tool.
- Projection merges ALL projected fields from contracts._envelope_summary
  (22 scalars) + harness._projection (inventory + 8 headlines) + the
  CVD multi-window sign series (15m/5m/2m/60s/30s — institutional
  delta-flip read, carried by neither projection today).

## Edits

1. NEW `market_service/runtime/read_paths.py` — the single shared
   read-side module:
   - `MARKET_RUN_SCHEMA_VERSION` re-export (guard source)
   - `path_read(d, *keys)` safe traversal
   - `cvd_sign_series(envelope)` — sign of CVD per window across
     15m/5m/2m/60s/30s from canonical_state.calculations.calculations.flow
     (bucketed_cvd first, flow fallback), null-preserving. Pure
     arithmetic-free sign reads where the calc layer already emits the
     series; derives nothing beyond sign/sum of existing buckets.
   - `market_snapshot(payload)` — headline projection: all _envelope_summary
     fields + merged fut_keystone/keystone_qty/ladder/bid_stack headlines +
     cvd_sign_series. Nulls preserved, never zero-substituted.
   - `market_inventory(payload)` — analysis_keys/calculations_keys/
     orderbook_keys/technical_keys + coverage + error_count.
   - `read_collated(store, symbol)` — Redis GET → json.loads → schema-version
     guard (raise on mismatch, per contract). NO dataclass hop.

2. `market_service/nooa_harness/inference.py`
   - import from runtime.read_paths.
   - NEW `dispatch_market_read(store, symbol, *, mode="snapshot")`:
     mode in {snapshot, inventory, full}; scope-validated; capability
     `market.read` registered in `_MARKET_TOOLS`.
     full = raw payload (engine's 40k gate remains the bound).
   - DELETE `dispatch_read_envelope` + MarketRunEnvelope coupling.
   - `TOOL_NAMES`: `market.envelope` → `market.read` ("market.read").
   - `execute_tool`: route `market.read` with `mode` arg
     (`args.get("mode") or "snapshot"`).

3. `market_service/nooa_harness/engine.py`
   - `market.envelope` appears only in a comment block — update naming.

4. `market_service/nooa_harness/agents.py`
   - `_call_model_once`: `bounded_envelope_view` already stringifies via
     `json.dumps(default=str)` → NaN crash. Switch to
     `default=_json_default` from runtime.contracts (NaN→null, Decimal
     str) so the micro agent's evidence payload can never emit invalid JSON.
   - DELETE `_evidence_from_envelope` + the `market.envelope`-era envelope
     probing path (speculative microstructure key that assemble_envelope
     never writes; caller micro_interpret passes a persisted
     MicrostructureEvidence dict directly).

5. `docs/nooa-kb/behavior/tool-manifest.md`
   - Replace the `market.envelope` section with `market.read`: modes,
     exact snapshot/inventory/full shapes, citation path examples
     (`market.read → snapshot.fut_keystone_bid`,
     `market.read → snapshot.cvd_sign_series[0]`).

6. `market_service/commands/nooa_cli_ext.py` (inner CLI — same loop)
   - DELETE `briefing_cmd` + `_read_briefing` (AnalystBriefing retired per
     user ruling; producer path already dead).
   - `micro_envelope` command (`--envelope`/`--run-id`): route through
     `read_collated` + `market_snapshot`/inventory instead of
     from_mapping/to_dict.

## Not in this slice
- Outer harness.py / pipeline persistence / redis_store.publish_run /
  postgres_store (next slices).
- MarketStateEnvelope, WakeEnvelope, InferenceArtifact — untouched.

## Verify
- parser/import smoke via `uv run --python 3.12 python -c ...`
- unit-level pure probe: build a fake envelope dict → assert snapshot/inventory
  shapes + NaN-safe dumps.
- Docker runtime: `docker compose --profile tools run --rm harness --nooa
  market read SOLUSDT --mode snapshot` (after nooa_cli_ext wiring) and
  engine cycle audit via existing artifact stream.
- pytest NOT run per user direction; do not break test infra.
