 RUNTIME SURFACE AUDIT — COMPILED FINDINGS

 Method: static trace of every symbol/venue/store/read/write path, plus one live probe cycle
 (SOL-centric runtime asked to analyse BTC at target 104). Severity: S1 = silent wrong data or
 blocks correctness · S2 = wastes runtime or destroys observability · S3 = structural debt.

 0. The probe that exposed the system

 One CLI invocation, inference run BTCUSDT --venue spot --target 104 --horizon 1h:

 ┌───────────────────────┬─────────────────────────────────────────────────────────────────────┐
 │ Observed              │ Value                                                               │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ Terminal              │ validation_failed                                                   │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ LLM calls             │ 9 (~3.5 min)                                                        │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ Output passes         │ 0 — no answer ever authored                                         │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ Repairs / tool rounds │ 2 / 3                                                               │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ calc.ofi_intervals    │ 0 intervals                                                         │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ calc.ad_avg           │ mean_ad: null, n: 0                                                 │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ calc.forward.join     │ refused no events in ledger → chain halted at assemble              │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ calc.discipline.audit │ no-go                                                               │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ memory.recall         │ rows: 0                                                             │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ Memory total          │ 23 → 23 (nothing written)                                           │
 ├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
 │ H0 formed             │ "Target 104 NOT reachable", basis: prompt_targets ← a guess, not an │
 │                       │ inference                                                           │
 └───────────────────────┴─────────────────────────────────────────────────────────────────────┘

 Nothing was internally broken. The engine did exactly what it was told — told the wrong thing.

 ────────────────────────────────────────────────────────────────────────────────

 1. Severity register

 ┌────┬────────────────────────────────────────────────────────────────────────────┬─────┐
 │ ID │ Finding                                                                    │ Sev │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ D1 │ market.read reads a plane nothing writes (collated publish_run: 0 callers) │ S1  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ F2 │ Venue is a phantom parameter — reaches no store key or filter              │ S1  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ F3 │ CLI cannot express the runtime's own venue ({"spot"} vs futures)           │ S1  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ M1 │ Operator cannot query the agent's actual memory view (no --segment)        │ S1  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ F6 │ Prices presented as current are frozen at an unknown past                  │ S1  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ F4 │ No symbol↔capture validation at any layer                                  │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ F1 │ Split-home CLI — 7 agentic commands on BTCUSDT/spot                        │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ F5 │ Config defaults contradict .env                                            │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ Q1 │ Postgres vs Redis disagree on venue in the same query                      │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ Q2 │ session_id must be hand-computed (uuid5), no derivation                    │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ D2 │ Horizon spans: 25 readers, 0 writers                                       │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ D3 │ Governance decisions: 13 readers, 0 writers                                │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ D4 │ publish_domain_state — only caller is a test                               │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ C4 │ Nine parallel finding vectors, non-interoperable                           │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ C5 │ Understanding written six times, no shared schema                          │ S2  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ C6 │ Scenario is 8 keys + controller tri-state                                  │ S3  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ C7 │ Flat 43-literal root keyspace, duplicate conformance                       │ S3  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ M2 │ memory.py/kb.py mirrors fixed (this session)                               │ ✅  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ Q5 │ Memory CLI accepts kinds the agent cannot write                            │ S3  │
 ├────┼────────────────────────────────────────────────────────────────────────────┼─────┤
 │ C8 │ Output loop: memory.* invited and forbidden in the same prompt             │ S3  │
 └────┴────────────────────────────────────────────────────────────────────────────┴─────┘

 ────────────────────────────────────────────────────────────────────────────────

 2. Plane A — Identity (the CLI ↔ runtime seam)

 F1 — Split-home CLI. Two conventions coexist:

 ```
   BTCUSDT/spot  microstructure fit       :392
   BTCUSDT/spot  microstructure evidence  :514
   BTCUSDT/spot  microstructure interpret :546
   BTCUSDT/spot  inference run            :620   ← the one that failed
   BTCUSDT/spot  inference answer         :665
   BTCUSDT/spot  inference read           :697
   BTCUSDT/spot  inference history        :718
   SOLUSDT       market read              :123
   SOLUSDT       market substrate-read    :155
   SOLUSDT       substrate read           :186
   SOLUSDT       substrate invoke         :197
 ```

 F2 — Venue is a phantom parameter. It is accepted, validated by cap.validate_scope, then
 discarded:

 - db/init/001_schema.sql — market_run has no venue column
 - redis_store.py:103 — {prefix}:latest:{SYMBOL}:collated
 - postgres_store.py:157 — WHERE symbol = $1 ORDER BY completed_at DESC
 - canonical_output has a venue column (postgres_store.py:1051) but the read does not filter on
   it

 Proof: market read BTCUSDT (venue = spot) returned fut_microprice_skew_bps: 1.2 — a
 futures-only field on a spot read.

 F3 — The CLI cannot express the real venue.

 ```python
   _MICRO_ALLOWED_VENUES = frozenset({"spot"})   # nooa_cli_ext.py:340
 ```

 .env says MICROSTRUCTURE_VENUE=futures. The CLI would reject the system's actual venue.

 F4 — No request↔capture reconciliation. settings.symbols is consumed by one file
 (poller.py:297-299). Nothing on the engine path reads it. _MICRO_ALLOWED_SYMBOLS =
 {"BTCUSDT","SOLUSDT","ETHUSDT"} (:339) permits a symbol nothing captures. Cost: 9 LLM turns to
 learn what a 10 ms check would catch.

 F5 — Config defaults contradict .env. config.py:189 and docker-compose.yml:88 both default to
 BTCUSDT,ETHUSDT,SOLUSDT; .env says SOLUSDT. A missing .env silently reactivates BTC.

 Nine symbol authorities, two conflicting defaults, one invisible runtime override:

 ┌───┬──────────────────────────────────────┬────────────────────────────────────────┐
 │ # │ Authority                            │ Value                                  │
 ├───┼──────────────────────────────────────┼────────────────────────────────────────┤
 │ 1 │ .env:26                              │ SOLUSDT                                │
 ├───┼──────────────────────────────────────┼────────────────────────────────────────┤
 │ 2 │ docker-compose:88                    │ BTCUSDT,ETHUSDT,SOLUSDT                │
 ├───┼──────────────────────────────────────┼────────────────────────────────────────┤
 │ 3 │ docker-compose:89 POLL_SYMBOLS       │ empty                                  │
 ├───┼──────────────────────────────────────┼────────────────────────────────────────┤
 │ 4 │ docker-compose:215 SUBSTRATE_SYMBOLS │ SOLUSDT                                │
 ├───┼──────────────────────────────────────┼────────────────────────────────────────┤
 │ 5 │ docker-compose:263 ANALYSIS_SYMBOLS  │ SOLUSDT                                │
 ├───┼──────────────────────────────────────┼────────────────────────────────────────┤
 │ 6 │ config.py:189                        │ BTCUSDT,ETHUSDT,SOLUSDT                │
 ├───┼──────────────────────────────────────┼────────────────────────────────────────┤
 │ 7 │ Redis {prefix}:poller:active_symbols │ dynamic — precedence #1, CLI-invisible │
 ├───┼──────────────────────────────────────┼────────────────────────────────────────┤
 │ 8 │ CLI literals ×11                     │ split BTC/SOL                          │
 ├───┼──────────────────────────────────────┼────────────────────────────────────────┤
 │ 9 │ _MICRO_ALLOWED_SYMBOLS               │ {BTC,ETH,SOL}                          │
 └───┴──────────────────────────────────────┴────────────────────────────────────────┘

 #7 is the worst: poller.py:283 resolve_active_symbols uses Redis control > POLL_SYMBOLS >
 SYMBOLS. Capture can be re-pointed at runtime and no CLI surface shows it.

 ────────────────────────────────────────────────────────────────────────────────

 3. Plane B — Data (and the F6 resolution)

 F6 is now resolved statically — and it is worse than "stale".

 redis_store.py:197 builds the key from envelope["symbol"], so no misrouting is possible at the
 key seam. The real cause:

 ```
   publish_run()              0 production callers   ← the plane market.read reads
   insert_run()               0 production callers   ← the PG twin
   publish_run → assemble_envelope → assemble_envelope has no production caller
 ```

 market.read serves a plane nothing writes. Its 145.2 is frozen at whenever the collated writer
 last existed — a previous era of the system. Not stale-by-minutes; stale-by-generation.

 D2/D3/D4 — the same pattern, three more times. Read-but-never-written planes:

 ┌─────────────────────────────┬─────────────┬─────────┬───────────────────────────────────────┐
 │ Publisher                   │ Prod        │ Readers │ Consequence                           │
 │                             │ callers     │         │                                       │
 ├─────────────────────────────┼─────────────┼─────────┼───────────────────────────────────────┤
 │ publish_run                 │ 0           │ 18      │ market.read frozen (D1)               │
 ├─────────────────────────────┼─────────────┼─────────┼───────────────────────────────────────┤
 │ publish_horizon_span        │ 0           │ 25      │ horizon plane frozen (D2)             │
 ├─────────────────────────────┼─────────────┼─────────┼───────────────────────────────────────┤
 │ publish_governance_decision │ 0           │ 13      │ governance decisions never recorded   │
 │                             │             │         │ (D3)                                  │
 ├─────────────────────────────┼─────────────┼─────────┼───────────────────────────────────────┤
 │ publish_domain_state        │ 0 (1 test)  │ 4       │ domain state frozen (D4)              │
 ├─────────────────────────────┼─────────────┼─────────┼───────────────────────────────────────┤
 │ publish_inference_artifact  │ 0           │ 16      │ legacy; superseded by                 │
 │                             │             │         │ canonical_output ✅ tolerable         │
 └─────────────────────────────┴─────────────┴─────────┴───────────────────────────────────────┘

 Live planes (for contrast): publish_raw_evidence(1),
 publish_microstructure_{delta,event,interval,status_transition,evidence}(1–3),
 publish_derivative_evidence(1), publish_substrate_state(1), publish_analysis_state(1),
 publish_canonical_output(2), publish_agent_artifact(2), publish_poller_status(2).

 This explains the probe exactly. The context-loop prompt tells the agent market.read snapshot
 (current price + regime). That tool reads a dead plane. Meanwhile substrate.read and micro.*
 read live planes — which is why the tool log shows substrate.read → ok beside market.read →
 145.2.

 The dangerous class is "read but never written", not "unused". Dead code is harmless; a frozen
 plane presented as current is silent corruption.

 ────────────────────────────────────────────────────────────────────────────────

 4. Plane C — Query surfaces

 Q1 — Same query, different rows. _load_run_record (nooa_cli_ext.py:75) tries PG then Redis:
 - PG read_canonical_output(symbol, run_id) → WHERE symbol = $1, venue ignored
   (postgres_store.py:1062)
 - Redis read_latest_canonical_output(symbol, venue) → venue used (redis_store.py:933)

 Venue filters only when PG is empty. Non-deterministic identity across planes.

 Q2 — session_id is hand-computed. All four memory commands use --session-id required=True. The
 operator must know:

 ```
   uuid5(NS_URL, "inference-engine://<symbol.lower()>/<venue>")
 ```

 Note it is venue-sensitive — wrong venue ⇒ empty session.

 Q3/M1 — The operator cannot see what the agent sees. MemoryNode.recall(segment=…) exists
 (memory.py:152) and the agent always passes segment=task:…. The CLI has no --segment flag. Same
 name, two different views — precisely why I saw 23 rows while the agent saw 0.

 Q5 — Kind asymmetry. CLI --kind accepts request|briefing|fact; the agent may write only
 observation|hypothesis|note. Intentional, undocumented in --help.

 Q4 — Venue identity is half-built. market_run lacks the column; canonical_output has it and
 ignores it.

 ────────────────────────────────────────────────────────────────────────────────

 5. Plane D — State topology

 C4 — Nine parallel finding vectors. validation_findings (list[str]), principle_findings
 (list[dict]), chain_gaps (list[str]), synthesis_note (dict), assessment_note (str|None),
 assembly_dropped (list[str]), evidence_salvaged (list[dict]),
 conformance.{dropped,hard_misses,conflicts,quality_findings}, failure_report (dict).

 Eight of nine have zero readers. The only consumer is answer.py:205-208 → the CLI conformance
 footer, reading 3 of conformance's 5 fields. conformance.dropped is never read.

 Duplicate write confirmed: assembly_dropped (output.py:264) → conformance.dropped
 (output.py:329). Plus a second duplicate C4 missed: conformance is written as a typed
 CanonicalOutput column (output.py:506) and inside run_record (output.py:509-511);
 contracts.py:1171 round-trips both, so divergence is undetectable.

 C5 — Understanding ×6. deterministic_state["comprehension"], ["understanding_receipt"], ledger
 UNDERSTANDING (runner:265), receipts FRAMING (wake:609), engine.cadence, plus
 task_directive/task_plan/task_workflow/seeded_plan. is_framing_complete (wake:267) requires six
  to exist. One event, four record shapes.

 C6 — Scenario ×8 + tri-state. scenario, scenario_contract, scenario_evaluation,
 scenario_divergence, scenario_digest, forward_scenario, forward_scenario_note, forecast_result.
 Verdict authority = controller.scenario_state(); facts = scenario_contract; refusal =
 controller.scenario_refusal_reason(). The docstring claims "ONE frozen runtime authority" — it
 is one of nine participants.

 C7 — Flat 43-root keyspace. context.py:216-260: 43 literals → 42 distinct roots, with
 conformance listed twice. frozenset dedupes silently, so the charter test passes on a
 duplicate. No owner, no schema, no retention per root.

 C8 — Output loop contradiction. kb.py:604 says "No tools this pass — any tool_calls returned
 are dropped"; synthesize.py:456 says "you MAY return memory.* tool_calls." Same path, both
 live, and memory.* is the only tool caught in it.

 ────────────────────────────────────────────────────────────────────────────────

 6. Plane E — Memory

 M1 — see Q3.

 M2 — mirror defects (fixed this session):
 - kb.py:577 said a base observation "is stored to task memory" — no such surface exists; the
   write lands on the ledger (reasoning.py:69-75)
 - memory.py:1-6 claimed it "persists every cycle's outputs" — zero engine-side callers
 - __init__.py:37 + inference_runner.py:50 called it "the engine's episodic memory" —
   contradicting four files saying "the agent's own"
 - Guards added: test_loop_windows_name_no_phantom_memory_surface,
   test_agent_memory_is_the_only_agent_writable_surface. Full suite: 1519 passed, 0 failed.

 M3 — the real consequence, measured. The agent had 23 rows it could not see, including four
 prior identical failures:

 ```
   Cycle refused by gate: observations 24 < minimum 30
   Cycle refused by gate: observations 18 < minimum 30
   Cycle refused by gate: observations  4 < minimum 30
   Cycle refused by gate: observations  1 < minimum 30
 ```

 It was told four times what was about to happen, and recalled none of it.

 M4 — the binding is the whole prompt. task_id = hash(normalized prompt) (wake.py:153-165).
 Byte-identical re-runs share a segment; one reworded word ⇒ new segment ⇒ cold memory,
 permanently. All 23 historical rows are segment=None, so the entire archive is orphaned anyway.

 ────────────────────────────────────────────────────────────────────────────────

 7. Root-cause synthesis — three cleavages

 Cleavage 1 — Identity has no owner. Symbol lives in 9 places, venue in 4 (one of which forbids
 the real value), and nothing reconciles "what is captured" against "what is asked".
 settings.symbols and the engine never meet.

 Cleavage 2 — Write and read planes are uncoupled. Five read-but-never-written planes. Nothing
 in the system asserts that a reader has a live writer. This is why market.read returns a number
 with total confidence and no truth.

 Cleavage 3 — Observations are written, never surfaced. Eight of nine finding vectors have no
 reader; the operator cannot reproduce the agent's memory segment; the engine's own findings
 never reach the answer's limitations block (which is sourced from the model's own prose,
 answer.py:158). The system records its doubts and discards them.

 The BTC failure required all three: wrong identity (1) → frozen price + zero intervals (2) →
 the reason never surfaced (3).

 ────────────────────────────────────────────────────────────────────────────────

 8. Fix sequence (gated)

 Gate 0 — stop the bleeding (no refactor).
 1. _MICRO_ALLOWED_VENUES → derive from Settings, not {"spot"} (F3)
 2. All 11 CLI literals → resolve from one Scope (F1)
 3. Symbol ∉ active set ⇒ refuse at wake, 0 LLM calls (F4)
 4. Wire or delete the 4 orphaned writers; if market.read cannot be made live, remove it from
    the tool registry rather than serve frozen data (D1–D4)

 Gate 1 — make identity real. Add venue to market_run, collated_latest_key, and the
 canonical_output read filter — or delete --venue entirely. Half-implemented is worse than
 absent (F2/Q1/Q4).

 Gate 2 — close the observability loop. --segment on memory recall; derive session_id from
 --symbol/--venue; engine findings become the authority for the answer's limitations (M1/Q2/Q3,
 C4).

 Gate 3 — state topology. Unify the nine vectors behind one
 Finding{vector,severity,blocking,stage,message} — preserving blocking as a field, since the
 gate/non-gate split is what F9 relaxation encoded. Then C5, C6, C7.

 Do not start Gate 3 before Gate 1. C4's shapes only make sense once identity is stable.

 ────────────────────────────────────────────────────────────────────────────────

 9. Verification

 ```bash
   # identity
   grep -n 'default="[A-Z]*USDT"' market_service/commands/nooa_cli_ext.py   # expect: 0
   grep -rn "def collated_latest_key" -A3  market_service/runtime/redis_store.py
   grep -rn "def read_canonical_output" -A23 market_service/runtime/postgres_store.py
   grep -n "settings.symbols" market_service/nooa_harness/engine/core/wake.py  # expect:
 non-empty

   # data plane — every reader must have a live writer
   .venv/bin/python - <<'PY'
   import re,pathlib
   store=pathlib.Path('market_service/runtime/redis_store.py').read_text()
   files=[p for p in pathlib.Path('market_service').rglob('*.py') if '__pycache__' not in
 str(p)]
   for n in re.findall(r'async def (publish_\w+)',store):
       c=sum(len(re.findall(rf'\.{n}\(',p.read_text(errors="ignore")))
             for p in files if 'redis_store.py' not in str(p))
       print(f"{n:<40}{c}  {'LIVE' if c else 'ORPHAN'}")
   PY

   # probe: uncaptured symbol must cost 0 LLM calls
   .venv/bin/nooa-market market inference run ETHUSDT --force --target 100
 ```

 Acceptance for the whole audit: an uncaptured symbol refuses in <1 s with 0 LLM calls, and a
 captured one answers with a price whose provenance (age_ms, run_id, symbol, venue) matches the
 request.

 ────────────────────────────────────────────────────────────────────────────────

 10. Unverified

 - The stack is down (6379/5433/8041/8042 refuse; Docker socket moved). Every live probe above
   is untested.
 - F6's age — needs SELECT symbol, generated_at, data_source FROM market_run ORDER BY
   completed_at DESC LIMIT 10. I proved the plane has no writer; I did not measure how old the
   row is.
 - Whether SOL capture is healthy — the 0 intervals may be purely the BTC phantom. One SOLUSDT
   --venue futures run separates the two causes and is the highest-value next test.
 - D3's real blast radius — 13 readers of a governance plane with no writer; I did not trace
   what they return when empty.