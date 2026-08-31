# HANDOFF — post-envelope deviation: state of the tree (2026-08-31)

Read this + plan/market-read-deviation.md before touching anything.

## DONE + LIVE-VERIFIED (inner CLI / OO-agent read plane)

1. NEW `market_service/runtime/read_paths.py` — the single post-envelope
   read module. Public surface:
   - `read_collated(store, symbol)` / `read_collated_by_run(store, run_id)`
     — one GET + json.loads + schema guard (raises ValueError on
     schema_version != 1; never coerces). Store duck-typed: `.redis` +
     `.collated_latest_key`.
   - `market_snapshot(payload)` — 34 bounded headline fields, merged from
     the old contracts `_envelope_summary` (~22) + harness `_projection`
     (orderbook/wall/anchor set). Includes `cvd_sign_series`.
   - `market_inventory(payload)` — section key lists + snapshot.
   - `cvd_sign_series(payload)` — sign of summed futures bucket deltas
     over 900/300/120/60/30s from `calculations.calculations.bucketed_cvd.
     futures_bucketed_cvd` (institutional delta-flip workflow). NaN-safe:
     non-finite buckets excluded, empty window = null sign (never zero).
   - `json_safe(payload)` / `json_safe_dumps(payload)` — tree-walk
     non-finite -> null, Decimal/datetime -> str. STRICT-JSON guarantee at
     any consumer seam (default=str CANNOT catch NaN — bare NaN tokens are
     invalid JSON and crash engine json.loads round trips).
   - `path_read`, `MARKET_RUN_SCHEMA_VERSION = 1`.

2. `nooa_harness/inference.py` — `market.envelope` FULLY REPLACED by
   `market.read` (TOOL_NAMES, descriptions, execute_tool dispatch, new
   `dispatch_market_read(store, symbol, *, mode)` with modes
   snapshot|inventory|full; full = the explicit deep-dive tool; result
   passed through `json_safe` at the seam). `dispatch_read_envelope`
   deleted. Bad mode -> structured `denied`; schema breach -> `error`;
   out-of-scope symbol -> `denied` (scope still frozen BTCUSDT via
   `_INITIAL_SYMBOLS`).

3. `nooa_harness/agents.py` — `bounded_envelope_view` renders via
   `json_safe_dumps` (was `default=str`, latent NaN leak). The
   microstructure probe at ~:194 was LEFT (it's load-bearing for the
   micro agent's assess fallback, not envelope debt).

4. `commands/nooa_cli_ext.py` — `market envelope` -> `market read SYM
   [--run-id] [--mode snapshot|inventory|full]` through read_paths
   (Redis-first, PG fallback still via store.read_run dataclass for now).
   `market briefing` command + `_read_briefing` DELETED (briefing plane is
   deviation-approved dead). Module docstring rewritten.

5. `docs/nooa-kb/behavior/tool-manifest.md` — `market.envelope` section
   replaced with exact `market.read` contract (modes, full snapshot field
   list, citation path `market.read → fut_keystone_bid`, schema_mismatch
   rule). KB doc IS the prompt contract — kept in lockstep.

6. `plan/market-read-deviation.md` — the spec + decisions (D-A1..3:
   replace-only + full-mode deep dive, all fields + CVD series merged,
   name = `market.read`).

VERIFIED LIVE (Docker, rebuilt images): harness `--analyze` cycle ->
`dispatch_market_read` snapshot = 1578 chars strict JSON (no NaN tokens);
inventory/full/bogus-mode/out-of-scope all behave; ghost `market.envelope`
-> denied (unknown tool); `nooa market read` CLI emits snapshot+inventory;
briefing command gone. pytest suite NOT touched (user: disregarded).

## NOT DONE — the deletion pass (user ordered stop; handoff target)

The envelope WRITER system is intact and still the runtime authority for
persistence. Tree is consistent: consumers above read raw payloads that
`MarketRunEnvelope.to_json()` publishes (same JSON shape; read paths are
format-compatible by construction, schema guard checks it).

Remaining, in order (each verified with the Docker harness after):
A. pipeline.py: `assemble_envelope` return dict (keep the to_dict() field
   names EXACT — Redis payloads already stored use them; run_cycle
   returns dict `.get("run_id")` not `.run_id`, lines ~2351-2376), then
   delete dead `build_group_envelopes` (:1902), `build_controller_view`
   (:1967), `SPECIALIST_GROUP_KINDS` (:1841), lying comments (:1829-41),
   `__all__` entries.
B. redis_store.py: `publish_run(payload: dict)` — json.dumps(read_paths.
   json_safe(payload)) replaces envelope.to_json(); KEEP the 3-key Lua
   atomicity + stream MAXLEN. Delete `read_latest_run`/`read_run`/
   `read_runs` envelope readers (harness route3 + nooa_cli_ext PG-fallback
   are the last callers — switch them to read_paths first), delete
   `publish_briefing`/`read_recent_briefings` (zero live callers).
C. postgres_store.py: `insert_run(dict)`; delete `_envelope_from_row`,
   `read_run`/`latest_run` envelope readers, whole briefing section
   (insert/read/read_list), double `_json_safe` at :86-88.
D. harness.py route 3 (:348-368): `read_paths.read_collated/by_run` ->
   emit. NOTE: harness importing runtime.read_paths is OK (boundary rule
   is vs nooa_harness, not runtime). `--analyze` path (:507-540) already
   dict-shaped via `.to_dict()` — keep `_projection` but source snapshot
   fields from read_paths.market_snapshot to kill the dup. Fix ALL
   "MarketRunEnvelope v2" doc drift (schema is 1; :9,160,175,189,206,212,411).
E. contracts.py: delete `MarketRunEnvelope` (:479), `_envelope_summary`
   (:675), `AnalystBriefing` + friends (`EvidenceEntry`, `Consensus`,
   `KeyEvidence`, `Disagreement`, `SpecialistReport`? — CHECK
   SpecialistReport callers first, it may still be live in the agent
   loop), briefing comment block. KEEP: WakeEnvelope, InferenceArtifact,
   AgentMemory, MarketStateEnvelope (out of scope per spec §2).
F. runtime/__init__.py exports sweep + final `grep -rn "MarketRunEnvelope
   \|AnalystBriefing" market_service` must be empty.

## OPEN DECISIONS FOR THE INTEGRATION PASS
- D1 (PG runs): still insert column-split rows from the dict (recommended
  — durable ledger keeps its schema) or retire PG runs entirely.
- Wake plane, memory, InferenceArtifact: untouched by design.
- Tests remain disregarded per user; expect `test_runtime_contracts`,
  `test_harness_commands`, `test_domain_redis_contract` to break on the
  deletion pass — delete/adjust opportunistically, not as a gate.

## PITFALLS LEARNED THIS PASS
- `json.dumps(default=str)` does NOT sanitize NaN — float('nan') bypasses
  `default` and emits bare `NaN` (invalid JSON). Use read_paths.json_safe.
- Engine tool-result gate: json.dumps(result) < 40k else json.loads
  round-trip fails -> collapse to `preview[:4000]` string. Snapshot mode
  (1.5k chars) always passes; `full` mode (~10MB live) ALWAYS collapses —
  deep-dive output is a 4k prefix. Acceptable per spec; do not "fix" by
  raising the gate.
- capability_log entry key is `result`, not `outcome`.
- execute_tool takes `symbol` inside args (frozen scope BTCUSDT).
- `nooa market` shim isn't mounted in the nooa container image (pre-
  existing); verify via CliRunner on the module or `python -c` instead.
- pipeline emits live NaN in flow math (not only at envelope seams) —
  readers must stay NaN-tolerant until the writer is fixed.
