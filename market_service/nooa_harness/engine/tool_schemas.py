"""Tool output schemas — declared per-tool output structures for the LLM layer.

Semantic authority: WHAT THE LLM CAN EXPECT FROM EACH TOOL. Every dispatch
function produces a raw dict; this module declares the expected schema as a
compact JSON-like string that gets injected into the system prompt so the
model knows exactly what fields to cite. Each schema also has a corresponding
``wrap_tool_result`` function that normalises raw results into a uniform
``ToolResult`` envelope with null fields explicitly named.

The split:
  - ``TOOL_OUTPUT_SCHEMAS`` — per-tool schema descriptions injected into
    the prompt (``kb.py`` reads them).
  - ``wrap_result(tool_name, raw)`` — wrap a raw dict into a ``ToolResult``
    with ``{tool, status, reason, data, null_fields}``.
  - ``extract_phase_summary(results, phase)`` — pull key fields per P1/P2/P3/P5
    from wrapped results for structured summary blocks.

Every schema is a single-line string so the prompt doesn't bloat. Null
discipline: ``null_fields`` is an array of field paths that are ``None``,
so the LLM never has to guess whether a missing key means "not computed"
or "zero".
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Tool output schemas — compact one-liners for prompt injection
# ---------------------------------------------------------------------------
# Each entry maps a tool name to a human-readable schema string. The LLM
# reads these as "what keys exist and what types they are". Decimal values
# appear as strings in the raw JSON but represent numeric measurements —
# the schema notes the semantic type.

TOOL_OUTPUT_SCHEMAS: dict[str, str] = {
    # ---- micro package (capture / tape / fit) ----
    "micro.capture_status": (
        "{state: str(running|stopped|degraded), sequence_gaps: int, "
        "reconnects: int, first_event_ts_ms: int|null, last_event_ts_ms: int|null}"
    ),
    "micro.events": (
        "array[{event_type: str, source_quality: str, contribution: str(decimal), "
        "previous: {bid_price,ask_price,bid_qty,ask_qty}, "
        "current: {bid_price,ask_price,bid_qty,ask_qty}}] — bounded to 200"
    ),
    "micro.ofi_intervals": (
        "array[{start_ts_ms: int, end_ts_ms: int, ofi: str(decimal), "
        "event_count: int, quality: str(exact_feed|estimated|degraded)}] — "
        "AD excluded by design (call calc.depth.average separately)"
    ),
    "micro.replay": (
        "{events: int, intervals: int, dropped: int, "
        "interval_ms: int, agreement: bool}"
    ),
    "micro.fit_beta": (
        "{price_impact_fit: {beta: str|null, se: str|null, r2: str|null, "
        "n_observations: int, status: str(validated|provisional|insufficient)}, "
        "depth_scaling_fit: {c: str|null, lambda: str|null, status: str|null}, "
        "coverage: {events_in_window: int, n_intervals: int, ...}}"
    ),
    "micro.evidence": (
        "last persisted evidence object (prior cycle, NOT a fresh fit) — "
        "may be null if no prior cycle"
    ),

    # ---- calc-base package (split OFI/AD + OLS + derived ΔP + scenario) ----
    "calc.ofi.intervals": (
        "array[{start_ts_ms: int, end_ts_ms: int, ofi: str(decimal), "
        "event_count: int, quality: str}] — OFI-only, AD excluded"
    ),
    "calc.depth.average": (
        "{window_minutes: int, n_intervals: int, "
        "ad_per_interval: array[str(decimal)|null], mean_ad: str(decimal)|null, "
        "depth_estimator: str}"
    ),
    "calc.observation.build": (
        "array[{intervals: array[{ofi: str, delta_ticks: str, "
        "average_depth: str|null}], excluded: int}]"
    ),
    "calc.fit.price_impact": (
        "{beta: str(decimal), se: str(decimal), t_stat: str(decimal), "
        "r2: str(decimal), n: int, f_stat: str(decimal)|null, "
        "status: str(validated|provisional|insufficient)}"
    ),
    "calc.fit.depth_scaling": (
        "{c: str(decimal)|null, lambda: str(decimal)|null, "
        "r2: str(decimal)|null, n_blocks: int, status: str}"
    ),
    "calc.price.delta": (
        "{route_a_direct: {delta_ticks: str(decimal), band_lo: str(decimal), "
        "band_hi: str(decimal)}|null, "
        "route_b_depth_scaled: {delta_ticks: str, c: str, lambda: str}|null, "
        "ofi_used: str(decimal)} — null when fit gate not validated"
    ),
    "calc.derived_diagnostic": (
        "alias for calc.price.delta — same shape"
    ),
    "calc.scenario.evaluate": (
        "{target_price: str, horizon: str, direction: str(up|down), "
        "required_ofi: str(decimal), required_ofi_range: "
        "[str(decimal),str(decimal)]|null, exceedance: str(decimal), "
        "exceedance_range: [str,str]|null, fit_status: str, "
        "n_windows_usable: int|null, r2: str(decimal)|null, "
        "n_windows_checked: int, tape_tested: bool, "
        "finder_verdict: str, route_b: {...}|null}"
    ),

    # ---- calc-forward package (horizon-native stack) ----
    "calc.forward.forecast": (
        "{feature_vector: {vector_version: str, fields: {...}}, "
        "forward_target: {horizon_ms: int, y_pred: str(decimal), "
        "y_actual: str(decimal)|null, y_actual_ts_ms: int|null}, "
        "train_oos: {train_n: int, oos_n: int, oos_rmse: str|null}, "
        "calibration: {gated: bool, probability: str|null}, "
        "regime: str(native|long_horizon), "
        "assumptions: str, route_a_b_disagreement: {delta_ticks: str, ...}|null}"
    ),
    "calc.feature.build": (
        "{vector_version: str, fields: dict[str, str], schema_hash: str, "
        "x_values: {ofi: str, average_depth: str|null, spread_bps: str|null, "
        "dmu: str|null, obi: str|null, skew: str|null, cvd: str|null}}"
    ),
    "calc.forward.join": (
        "{n_pairs: int, exclusion_log: list[str], "
        "sample: array[{...}] — first 5 pairs}"
    ),
    "calc.forward.fit": (
        "{horizon_ms: int, feature_schema: str, "
        "estimates: {coef: array[str], se: array[str], r2: str}, "
        "train_n: int, oos_n: int, oos_metrics: {rmse: str|null, mae: str|null}, "
        "status: str(validated|provisional|insufficient), "
        "cont_univariate: {beta: str, r2: str}|null}"
    ),
    "calc.forward.distribution": (
        "{horizon_ms: int, expected_delta: str(decimal), "
        "pi_95_lo: str(decimal), pi_95_hi: str(decimal), "
        "p_ge_theta: str(decimal)|null, p_ge_zero: str(decimal)|null, "
        "status: str(validated|calibrated|insufficient)} — "
        "null when fit insufficient"
    ),
    "calc.forward.scenario": (
        "{horizon_ms: int, targets: array[{T: str, p_ge: str|null, "
        "p_lo: str, p_hi: str}], invalidations: array[{S: str, "
        "p_le: str|null}], agreement_vs_legacy: str, fit_id: str}"
    ),
    "calc.hypothesis.test": (
        "{hypothesis_id: str, effect: str(decimal), se: str(decimal), "
        "ci_95_lo: str(decimal), ci_95_hi: str(decimal), "
        "p_value: str(decimal), n: int, test_split: str(train|oos), "
        "m_tests: int, multiplicity_adj: str|null, "
        "status: str(ok|insufficient|refused)}"
    ),

    # ---- typed events ----
    "calc.events.absorption": (
        "{events: array[{event_type: str, trigger_ts_ms: int, "
        "price_impact_ticks: str, volume: str, duration_ms: int, ...}], "
        "log: {...}, replay_agreement: {agreement: bool}}"
    ),
    "calc.events.walls": (
        "{events: array[{...}], log: {...}, "
        "replay_agreement: {agreement: bool}}"
    ),

    # ---- decay + discipline ----
    "calc.decay.report": (
        "{horizons: array[{horizon_ms: int, oos_skill: str|null, "
        "n_train: int, regime: str, finalized: bool}], "
        "nulls: array[str]}"
    ),
    "calc.discipline.audit": (
        "{locks: {leakage: bool, time_order: bool, horizon: bool, "
        "regime: bool, baseline: bool, cost: bool, calibration: bool, "
        "multiplicity: bool, pins: bool}, "
        "go: bool, memo: str}"
    ),

    # ---- market package (regime context) ----
    "market.read": (
        "{symbol: str, run_id: str|null, status: str, "
        "coverage: {...}, canonical_state: {...}} — "
        "truncated to snapshot by default; mode=full for deep-dive"
    ),
    "market.derivatives": (
        "{oi_history: {...}|null, taker_buy_sell: {...}|null, "
        "funding: {...}|null, cross_asset: {...}} — "
        "null fields mean unavailable (not zero)"
    ),
    "market.keystone_history": (
        "array[{cycle_ts: str, keystone_price: str|null, "
        "tight_lo: str, tight_hi: str, ...}] — bounded to count arg"
    ),
    "market.wall_history": (
        "array[{cycle_ts: str, fuel_ratio: str|null, "
        "bid_pool: str|null, ask_pool: str|null, ...}]"
    ),

    # ---- substrate plane (always-fresh workers) ----
    "substrate.read": (
        "{symbol: str, substrates: {<name>: {status: str, "
        "trigger_source: str|null, age_ms: int|null, "
        "available: bool}}} — compact mode default"
    ),
    "substrate.invoke": (
        "{substrate: str, symbol: str, invoked: bool, "
        "fired: int|null, trigger_source: str|null, "
        "status: str|null, dormant_reason: str|null, "
        "last_error: str|null, available: bool} — "
        "unknown substrate returns invoked=false with error"
    ),
    "substrate.tape": "same as substrate.invoke — invokes the tape worker",
    "substrate.density": "same as substrate.invoke — invokes the density worker",
    "substrate.delta": "same as substrate.invoke — invokes the delta worker",
    "substrate.ladders": "same as substrate.invoke — invokes the ladders worker",
    "substrate.anchors": "same as substrate.invoke — invokes the anchors worker",
    "substrate.tiers": "same as substrate.invoke — invokes the tiers worker",
    "substrate.volume_profile": "same as substrate.invoke — invokes the volume_profile worker",
    "substrate.technicals": "same as substrate.invoke — invokes the technicals worker",
    "substrate.migration": "same as substrate.invoke — invokes the migration worker",
    "substrate.oi": "same as substrate.invoke — invokes the oi worker",
    "substrate.signals": "same as substrate.invoke — invokes the signals worker",
    "substrate.large_print": "same as substrate.invoke — invokes the large_print worker",

    # ---- memory ----
    "memory.recall_paper": (
        "array[{kind: str, content: str, importance: float, "
        "tags: array[str], session_id: str, created_at: str}] — "
        "empty array when unseeded"
    ),
}

# ---------------------------------------------------------------------------
# ToolResult envelope — uniform wrapper every tool result passes through
# ---------------------------------------------------------------------------

def wrap_tool_result(
    tool_name: str,
    raw: Any,
    status: str = "ok",
    reason: str | None = None,
) -> dict[str, Any]:
    """Wrap a raw tool output into the uniform ``ToolResult`` envelope.

    The envelope gives the LLM a consistent structure to parse regardless
    of the tool:
      - ``tool`` — the canonical tool name
      - ``status`` — one of ok, refused, denied, error
      - ``reason`` — refusal/denial/error message (null when ok)
      - ``data`` — the tool's actual output (dict, list, or null)
      - ``null_fields`` — explicitly named field paths that are None

    ``null_fields`` is computed by recursively scanning ``data`` for None
    values. This lets the LLM distinguish "field not computed" (explicitly
    listed) from "field absent from schema" (which shouldn't happen with
    the declared schemas).
    """
    null_fields = _find_nulls(raw) if isinstance(raw, dict) else []
    return {
        "tool": tool_name,
        "status": status,
        "reason": reason,
        "data": raw,
        "null_fields": null_fields,
    }


def _find_nulls(obj: dict[str, Any], prefix: str = "") -> list[str]:
    """Recursively find all None values in a nested dict, returning path strings."""
    nulls: list[str] = []
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if value is None:
            nulls.append(path)
        elif isinstance(value, dict):
            nulls.extend(_find_nulls(value, path))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    nulls.extend(_find_nulls(item, f"{path}[{i}]"))
    return nulls


def tool_schema_block() -> str:
    """Render the tool schemas block for injection into the system prompt.

    Returns a compact string listing each tool and its output schema,
    plus a note about the ToolResult envelope.
    """
    lines = [
        "TOOL OUTPUT SCHEMAS (every tool returns a ToolResult envelope; "
        "cite fields from the ``data`` key; ``null_fields`` names what "
        "was not computed):",
    ]
    for tool_name in sorted(TOOL_OUTPUT_SCHEMAS):
        schema = TOOL_OUTPUT_SCHEMAS[tool_name]
        lines.append(f"  {tool_name}: {schema}")
    lines.append(
        "ToolResult envelope: ``{tool: str, status: str(ok|refused|denied|error), "
        "reason: str|null, data: <per-tool schema>, null_fields: array[str]}``. "
        "A ``status`` of ``refused`` means the tool declined deterministically "
        "(cite the reason); ``denied`` means the tool was out of scope or "
        "unauthorized; ``error`` means an exception occurred. Every numeric "
        "value you cite must name the exact path from ``data``."
    )
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Phase-level summary extraction
# ---------------------------------------------------------------------------
# These functions pull the key fields from wrapped tool results that each
# phase cares about, producing a compact summary block the prompt can show
# alongside the raw JSON.

def extract_p1_summary(results: dict[str, Any]) -> list[str]:
    """Extract P1-significant fields: OFI value, quality, intervals count."""
    lines: list[str] = []
    ofi = results.get("calc.ofi.intervals")
    if ofi and isinstance(ofi, dict) and ofi.get("data"):
        data = ofi["data"]
        if isinstance(data, list) and data:
            n = len(data)
            qualities = set(d.get("quality") for d in data if isinstance(d, dict))
            qual_str = ",".join(sorted(q for q in qualities if q)) if qualities else "?"
            lines.append(f"OFI: {n} intervals, quality=[{qual_str}]")
    cap = results.get("micro.capture_status")
    if cap and isinstance(cap, dict) and cap.get("data"):
        d = cap["data"]
        lines.append(f"capture: state={d.get('state')}, gaps={d.get('sequence_gaps')}")
    return lines


def extract_p2_summary(results: dict[str, Any]) -> list[str]:
    """Extract P2-significant fields: mean AD, depth scaling, fit status."""
    lines: list[str] = []
    ad = results.get("calc.depth.average")
    if ad and isinstance(ad, dict) and ad.get("data"):
        d = ad["data"]
        lines.append(f"AD: mean={d.get('mean_ad')}, n={d.get('n_intervals')}")
    pif = results.get("calc.fit.price_impact")
    if pif and isinstance(pif, dict) and pif.get("data"):
        d = pif["data"]
        lines.append(f"ΔP~OFI: beta={d.get('beta')}, r2={d.get('r2')}, status={d.get('status')}")
    dsf = results.get("calc.fit.depth_scaling")
    if dsf and isinstance(dsf, dict) and dsf.get("data"):
        d = dsf["data"]
        lines.append(f"depth: c={d.get('c')}, λ={d.get('lambda')}, status={d.get('status')}")
    return lines


def extract_p3_summary(results: dict[str, Any]) -> list[str]:
    """Extract P3-significant fields: substrate availability, age, freshness."""
    lines: list[str] = []
    sub_read = results.get("substrate.read")
    if sub_read and isinstance(sub_read, dict) and sub_read.get("data"):
        substrates = (sub_read["data"] or {}).get("substrates") or {}
        for name, entry in substrates.items():
            if isinstance(entry, dict):
                avail = entry.get("available", False)
                age = entry.get("age_ms", "?")
                lines.append(f"  {name}: {'✓' if avail else '✗'}, age={age}ms")
    market = results.get("market.read")
    if market and isinstance(market, dict) and market.get("data"):
        d = market["data"]
        lines.append(f"market: status={d.get('status')}, run_id={d.get('run_id')}")
    return lines


def extract_p5_summary(results: dict[str, Any]) -> list[str]:
    """Extract P5-significant fields: ΔP, scenario verdict, hypothesis test."""
    lines: list[str] = []
    delta = results.get("calc.price.delta") or results.get("calc.derived_diagnostic")
    if delta and isinstance(delta, dict) and delta.get("data"):
        d = delta["data"]
        rA = d.get("route_a_direct") if isinstance(d, dict) else None
        if rA:
            lines.append(f"ΔP: {rA.get('delta_ticks')}t "
                         f"band=[{rA.get('band_lo')}, {rA.get('band_hi')}]")
    fore = results.get("calc.forward.forecast")
    if fore and isinstance(fore, dict) and fore.get("data"):
        d = fore["data"]
        ft = d.get("forward_target", {})
        lines.append(f"forward: Y({ft.get('horizon_ms')}ms)={ft.get('y_pred')}")
    scen = results.get("calc.scenario.evaluate")
    if scen and isinstance(scen, dict) and scen.get("data"):
        d = scen["data"]
        lines.append(f"scenario: target={d.get('target_price')}, "
                     f"exceedance={d.get('exceedance')}, "
                     f"verdict={d.get('finder_verdict')}")
    ht = results.get("calc.hypothesis.test")
    if ht and isinstance(ht, dict) and ht.get("data"):
        d = ht["data"]
        lines.append(f"test: H0={d.get('hypothesis_id')}, "
                     f"p={d.get('p_value')}, status={d.get('status')}")
    return lines


def build_phase_summary_block(results: dict[str, Any]) -> str:
    """Build a compact per-phase summary block from wrapped tool results.

    The output is a short section like::

        PHASE SUMMARIES:
          P1: OFI 180 intervals (estimated), capture=running
          P2: mean AD=2.1M, ΔP~OFI beta=0.042(r2=0.31), c=0.5(provisional)
          P3: tape ✓ age=12s, density ✓ age=45s, ...
          P5: ΔP=-1.2t band=[-3.1,+0.7], scenario exceedance=0.42

    This block is appended to the prompt AFTER the raw PASS RESULTS section
    so the model sees both the full detail and the curated summary.
    """
    p1 = extract_p1_summary(results)
    p2 = extract_p2_summary(results)
    p3 = extract_p3_summary(results)
    p5 = extract_p5_summary(results)
    sections = []
    if p1:
        sections.append("P1: " + "; ".join(p1))
    if p2:
        sections.append("P2: " + "; ".join(p2))
    if p3:
        sections.append("P3:\n" + "\n".join(p3))
    if p5:
        sections.append("P5: " + "; ".join(p5))
    if not sections:
        return ""
    return "PHASE SUMMARIES (key fields extracted from tool outputs):\n" + "\n".join(sections) + "\n\n"


__all__ = [
    "TOOL_OUTPUT_SCHEMAS",
    "wrap_tool_result",
    "tool_schema_block",
    "extract_p1_summary",
    "extract_p2_summary",
    "extract_p3_summary",
    "extract_p5_summary",
    "build_phase_summary_block",
]