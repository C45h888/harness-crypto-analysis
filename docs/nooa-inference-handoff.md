# NOOA Inference Plane — Contextual Handoff

**Date:** 2026-09-07 · **Branch:** `feature/raw-evidence-json-contract` (ahead of origin by 3+ commits, uncommitted work present)
**Scope:** `market_service/nooa_harness/` OO agent (InferenceEngine) + deterministic microstructure stack.
**Live scope:** SOLUSDT / futures (`.env`: `MICROSTRUCTURE_SYMBOL=SOLUSDT`, `MICROSTRUCTURE_VENUE=futures`).
**Model:** `openrouter/meta/muse-spark-1.2-contributor` via OpenRouter, 50k tokens/turn (`NOOA_MODEL_MAX_TOKENS`).

> Read this whole document before touching code. It records what was DONE
> (do not redo), what is PROPOSED (do not start unapproved), and the traps
> (parallel worker, pre-broken tests, wrapper scripts).

---

## 1. System map (all paths repo-relative)

### Bedrock files — the agent

| File | Role | Current state |
|---|---|---|
| `market_service/nooa_harness/engine.py` | `InferenceEngine.run_cycle(wake)`: gather → gate → recall → narrate#1 → **staged P1→P6 loop** → persist → memory dispose | Staged loop live; see §3 |
| `market_service/nooa_harness/inference.py` | `resolve_inference_status` (hard gate) + `TOOL_NAMES` (**19 tools**) + `execute_tool` (sole dispatch) + `TOOL_PHASE` map + alias normalization | Numeric ΔP live; see §3 |
| `market_service/nooa_harness/backends.py` | `NOOA_MODEL_*` → litellm routing; only nooa import site | Working |
| `market_service/nooa_harness/memory.py` | `MemoryNode` over `agent_memory` (PG durable → Redis stream); exports `paper_kb_session_id()` | Working; paper KB seeded (10 facts) |
| `market_service/nooa_harness/wake_worker.py` | XREADGROUP BLOCK event/status streams → `evaluate_triggers` (event_delta 1800 / cold_start / recovery) → dispatches `run_cycle` as task | Working |
| `market_service/nooa_harness/inference_runner.py` | `run_inference_once(force/envelope)` + event loop; lazy LLM/memory builds | Working |

### Deterministic stack (agent must CALL, never recompute)

| File | Role |
|---|---|
| `microstructure/fitting.py` | ONLY producer of β/α/SE/R² (`fit_price_impact` OLS HC0), c/λ (`fit_depth_scaling`, needs ≥3 AD blocks), **plus `derive_price_delta()` (pure numeric ΔP, new)** |
| `microstructure/ofi.py` | e_n → OFI_k half-open clock aggregation; `DEPTH_ESTIMATOR = event_mean_best_bid_ask_v1` (frozen) |
| `microstructure/orderbook.py` | Snapshot bootstrap + `BookGapError` gap detection |
| `microstructure/capture.py` | WS → Redis (futures: `wss://fstream.binance.com`). Flaps running↔gap every ~3s on futures (12k+ gaps) |
| `nooa_harness/pipeline.py` | `GROUP_MAP` behind `market.group` (wall/flow/structure/positioning) |
| `runtime/contracts.py` | `InferenceArtifact` (+hypothesis/verdict/calculations; `validate()` forbids `combined_prediction` and non-null interpretation on `insufficient`) |
| `runtime/postgres_store.py` | `insert/read_inference_artifact` + **`read_recent_inference_artifacts()` (full rows, new)** |
| `runtime/redis_store.py` | Artifact projection, event/status streams, memory streams |
| `scripts/seed_paper_kb.py` | Seeds 10 Cont-1011.6402 facts; shares `paper_kb_session_id()` with `memory.py` |

### Docs / KB on disk (loaded into prompt every cycle)

- `docs/nooa-kb/behavior/tool-manifest.md` — registry + staged P1→P6 protocol (update when tools/phases change)
- `docs/nooa-kb/behavior/memory-protocol.md` — propose-never-write, recall 12 / 8k budget
- `docs/nooa-kb/nooa-micro-structure-archieture.md` — paper prose (fallback; live paper source is MemoryNode)

---

## 2. COMPLETED work (verified — do not redo)

### Pass 1 — Fixes (approved, integrated, green)
1. **Paper KB seeded**: 10 facts in PG, session `f5ce900c-8602-5029-805a-21fde7503dd0`, recall verified 8/8.
2. **`_output_format` rewritten**: exact registry names (the old pipe-joined pseudo-name caused `tool.unknown ×3` per cycle with `name:""`).
3. **Structured output**: `NarrationTurn` Pydantic model via `acall(output_model=…)`; nameless calls die at schema (`ValidationError` proven). Plain-JSON fallback with probe flag.
4. **Alias normalization**: `_normalize_tool_name` (fully-normalized lookup + truncations); denied logs carry `{attempted, allowed}`.
5. **Depth-scaling unblocked**: `read_recent_inference_artifacts()` → `_load_prior_block_fits()` → `assemble_evidence(prior_block_fits=…)` (was permanent n_blocks=1). Hetero gate rule deliberately UNTOUCHED.
6. **Docs synced** (scope, recall limit). **4 stale tests** updated to futures scope + 19 tools.

### Pass 2 — Staged loop P1→P5 (approved, integrated, green)
- 5 tool rounds / 8 LLM turns; coverage credited from executed tool families (`TOOL_PHASE`), not declarations.
- `_validate_final_turn`: P1+P2+P3+P5 coverage + H0 + ≥200-char summary + dual-root evidence → **repair-while-budget** (thin finals rejected, e.g. old *"Initiating validation loop…"* artifact impossible now).
- Per-call try/except (`tool.error:*` never kills cycle); `cycle_meta` gained `phase_coverage/final_validation/tool_rounds/repairs`; `data_quality` block in deterministic state; free interval/window args.

### Pass 3 — Numeric ΔP + P6 (approved, integrated, green, LIVE-FIRED)
- `fitting.derive_price_delta()`: route A ΔP=α+β·OFI ±1.96·SE·|OFI| + quote; route B via c·AD⁻ˡ when identified; ValueError refusal on `insufficient`.
- Tool `calc.price.delta` (old `calc.derived_diagnostic` name kept as dispatch alias to the numeric impl; was static text, computed nothing).
- **P6 output-generation phase**: synthesis-only final; validator requires P6 declared + `calc.price.delta → …` citation. Credit rule: denials/errors/refusals never count; legitimate null reads do.
- **Live proof** (2026-09-07, artifact `1d4921b4`, SOLUSDT/futures, manual force): `final_validation.passed`, phases P1/P2/P3/P5/P6 covered, 8 paper facts recalled, proper H0 (β=0) / H1, numeric ΔP (−0.0403 ticks, band 0.00005), memory written (6.5), verdict inconclusive-on-provisional by design. Fit has since improved to **validated n=174, hetero false**; depth scaling at **2/3 blocks** (route B one cycle away).

### Test state
- 449/449 green (`tests/`, excluding 3 pre-broken files below). Harness slice 196.
- New: derive math ×4, dispatch ×4, P6/repair/validator/error/structured-path cases; staged scripts across `test_engine.py`.

---

## 3. PROPOSED next round (NOT approved — owner picks)

From the live artifact dissection, in suggested priority:

1. **Confidence enum violation** — agent emitted `low-medium`; contract allows low|medium|high only. Add enum check to `_validate_final_turn` (or coerce `moderate/low-medium` → nearest, mirroring `_CONFIDENCE_ALIASES` in `runtime/contracts.py`).
2. **Evidence `interpretation` null** on all entries — paths+values cited, readings empty. Require non-empty `interpretation` per entry in validator (contract `EvidenceEntry` already mandates it; engine passes through).
3. **P4 never declared** — final passed without the explanation turn. Decide: require P4 declared-turn, or accept synthesis riding in the P6 summary (current behavior).
4. **`micro.ofi_intervals` 0 rows** while recompute found 181 — persisted interval ledger likely unpopulated on futures. Data-plane investigation (`read_microstructure_intervals` source vs capture writers).
5. **`verdict_reason` contains `|`** (engine appends `" | H0 paper-grounded…"`) — breaks pipe-delimited reads; switch to `;` separator.
6. **Route B watch** — no action; depth scaling at 2/3 blocks, next cycles should flip it `derived_ok`. Verify agreement_ticks appears.

Open design defaults currently in force (reconfirm if revisiting): OFI = arg-else-latest-interval; routes reported neutrally; refuse only on `insufficient`; band = HC0-only with `residual_std` alongside.

---

## 4. Traps — read before acting

1. **A parallel worker is restructuring this tree.** Uncommitted, NOT ours: `pipeline.py` gutted (−2640), `agents.py` deleted, new `bedrock.py`/`pipeline_inference.py`/`pipeline_interpretation.py`, two-plane `memory=`/`settings=` injection (compatible — build on it, don't fight it). Coordinate before touching those files.
2. **Pre-broken tests (not ours, verified via stash):** `test_calculation_groups.py`, `test_pipeline_formatting.py` (collection `ImportError`), `test_keystone_history.py` (2 failures). Exclude or fix only with owner approval.
3. **`redis-cli` on PATH is a wrapper** (`~/bin/redis-cli` → `docker exec instagram-test-redis`, nonexistent). Use `docker exec crypto-ai-anal-redis-1 redis-cli` and `docker exec crypto-ai-anal-postgres-1 psql -U marketflow -d marketflow`.
4. **Live runs cost real API calls.** `./nooa market inference run SOLUSDT --venue futures --force` ≈ 4–8 LLM turns, ~50s. `tail` full stdout — `cycle_meta` (phase coverage, repairs) is NOT persisted, only the artifact is.
5. **Behavioral contract with the owner:** discussion passes are discussion-only. Never implement fromassumptions — spec first, approval second. (A rushed numeric-derivation pass was fully reverted for this reason; the approved version above is its disciplined successor.)
6. **Paper session stability:** `paper_kb_session_id()` = uuid5(`paper-kb://cont1011`); never change the URI or 10 seeded facts orphan.

## 5. Quick commands

```bash
set -a; source .env; set +a; source .venv/bin/activate
python3 -m pytest tests/ -q -p no:cacheprovider --ignore=tests/test_calculation_groups.py --ignore=tests/test_pipeline_formatting.py --ignore=tests/test_keystone_history.py
./nooa market inference run SOLUSDT --venue futures --force
docker exec crypto-ai-anal-postgres-1 psql -U marketflow -d marketflow -c "select artifact_id,status,hypothesis_verdict,completed_at from inference_artifact order by completed_at desc limit 3;"
docker logs crypto-ai-anal-wake-worker-1 --tail 5
```
