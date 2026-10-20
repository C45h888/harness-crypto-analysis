"""LLM transport — calling the model and unwrapping what came back.

Semantic authority: HOW WE TALK TO THE LLM. This module owns the
transport-specific concerns: structured-output probing, the 4-tier
content extraction ladder, and the bounded envelope viewer used to
squeeze a possibly-large evidence payload into the model's param limit.

The split:

  - ``call_narration_llm(llm, system_prompt, user_prompt)`` — one narration
    call. Tries ``output_model=NarrationTurn`` first (schema rejects nameless
    tool_calls at the source); on backend refusal, sets the structured probe
    flag to ``False`` for the rest of the process and falls back to raw
    JSON. Returns the raw text regardless — ``narration.py`` owns parsing.
  - ``bounded_envelope_view(payload, roof, list_cap)`` — serialises a
    possibly-large dict into an LLM-bound projection, capping large lists
    with an explicit ``__truncated__`` marker and, as a last resort, the
    run-identity + coverage + errors block.
  - ``response_text(resp)`` — extracts the text content from a NOOA
    unified-LLM response (covers the four transport shapes: parsed
    content, ``.content`` string, ``.content`` list of parts, raw
    ``choices[0].message.content``).
  - Token-budget resolver lives in ``config.py`` (single source of truth
    for budgets).

The structured-probe flag is read+written here; ``config.py`` declares
it because every module that wants to consult it needs to import from
``config``, but the mutation lives at the only call site that knows
the backend behaviour.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from typing import Any

from .config import (
    AGENTIC_MAX_TOOL_ROUNDS,
    _STRUCTURED_OK,
    narration_max_tokens,
    narration_timeout_s,
    structured_output_enabled,
)
from .schemas import NarrationParseError, NarrationTurn

try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover — narration falls back to raw JSON
    BaseModel = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Which extraction tier produced the last narration text (observability):
# when a cycle ends in a thin/empty parsed turn we need to know whether the
# structured probe engaged or the raw ladder scraped a fallback surface.
LAST_EXTRACTION_TIER: str | None = None


def last_tier() -> str | None:
    """The extraction tier of the most recent narration call (observability)."""
    return LAST_EXTRACTION_TIER


def _note_tier(tier: str, text: str) -> str:
    global LAST_EXTRACTION_TIER
    LAST_EXTRACTION_TIER = tier
    log.info("narration extraction tier=%s chars=%d", tier, len(text or ""))
    return text


def _accepts_timeout(fn: Any) -> bool:
    """Does this LLM client's ``acall`` accept a ``timeout`` kwarg? (pure).

    The NOOA unified client takes ``**kwargs`` (forwarded to litellm as the
    native total bound); scripted test fakes pin exact signatures. Detect,
    never assume — an unexpected kwarg would TypeError the call.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in params.values()
    )


async def call_narration_llm(
    llm: Any,
    user_prompt: str,
    *,
    system_prompt: str | None = None,
) -> str:
    """One narration LLM call — structured probe + 4-tier extraction.

    The structured attempt is paid only while the process-level probe flag
    allows it; once a backend refuses ``output_model=NarrationTurn``, the
    flag flips to ``False`` and we never re-pay the failed attempt for the
    rest of the process.

    ``system_prompt`` is the caller's cycle-scoped rendered prompt (the
    engine builds it once per cycle via ``kb.SystemPromptCache``). When
    omitted it is rendered here, which keeps the direct-call shape working
    but re-reads the KB payload on every call — prefer passing it.

    Extraction ladder (in order):
      0. Structured-model surface — already validated, serialise to JSON.
      1. ``.content`` is a non-empty string → use it.
      2. ``.content`` is a list of ``{"text": ...}`` parts → concat.
      3. Reasoning-model fallback — ``.reasoning`` parks the final JSON
         when the generation budget ran out in the preamble.
      4. Legacy raw-transport extraction — direct litellm/OpenAI shapes.
    """
    if llm is None:
        raise NarrationParseError("no LLM client configured for narration")
    if system_prompt is None:
        # Direct-call fallback (tests, ad-hoc surfaces): render on demand.
        from .kb import build_system_prompt

        system_prompt = build_system_prompt("BTCUSDT", "spot",
                                            AGENTIC_MAX_TOOL_ROUNDS)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # Structured attempt first (schema rejects nameless tool_calls at the
    # source); plain-JSON fallback preserves current behavior on backends
    # that refuse response_format. The probe flag avoids paying the failed
    # attempt on every turn once a backend has refused.
    global _STRUCTURED_OK  # noqa: PLW0603 — process-global, set once per backend
    response = None
    # F10: bounded transport. A dropped stream must surface as a transient
    # failure into the retry plane within minutes, never stall a cycle for
    # hours inside the transport's inner retry. A timeout is NOT a probe
    # refusal: it propagates with the probe flag untouched.
    timeout_s = narration_timeout_s()
    # Litellm-native total bound on BOTH attempts: asyncio.wait_for owns the
    # outer bound, but a provider stall that blocks the loop (sync sleep in
    # a transport retry path, executor handoff) can delay cancellation
    # delivery — live 2026-10-19: narration#1 hung 88min against a 150s
    # wait_for. The native timeout fails the call from INSIDE the transport
    # so the cycle degrades in minutes, never hours. Passed only when the
    # client accepts it (the NOOA unified client takes **kwargs; scripted
    # test fakes pin exact signatures) — capability-detected, never assumed.
    if _accepts_timeout(llm.acall):
        _timeout_kw: dict[str, Any] = {"timeout": timeout_s}
    else:
        _timeout_kw = {}
    if (NarrationTurn is not None and _STRUCTURED_OK is not False
            and structured_output_enabled()):
        try:
            response = await asyncio.wait_for(
                llm.acall(
                    messages=messages,
                    output_model=NarrationTurn,
                    max_tokens=narration_max_tokens(),
                    **_timeout_kw,
                ),
                timeout=timeout_s,
            )
            _STRUCTURED_OK = True
        except asyncio.TimeoutError:
            raise
        except Exception:
            log.warning("structured narration call refused; falling back to raw JSON")
            _STRUCTURED_OK = False
            response = None
    if response is None:
        response = await asyncio.wait_for(
            llm.acall(
                messages=messages,
                max_tokens=narration_max_tokens(),
                **_timeout_kw,
            ),
            timeout=timeout_s,
        )
    # 0) Structured-model surface: already validated, serialize to JSON.
    content = getattr(response, "content", None)
    if BaseModel is not None and isinstance(content, BaseModel):
        return _note_tier("structured_model",
                          json.dumps(content.model_dump(), default=str))
    # 1) The client's parsed text surface (nooa LLMResponse.content) is the
    #    authoritative transport-agnostic extraction.
    content = getattr(response, "content", None)
    if isinstance(content, str) and content.strip():
        return _note_tier("content_str", content)
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
        if any(p.strip() for p in parts):
            return _note_tier("content_parts", "".join(parts))
    # 2) Reasoning-model fallback: with a reasoning backend, the final JSON
    #    may be parked in ``reasoning`` when the generation budget ran out
    #    in the reasoning preamble before a content block was emitted.
    #    Extract from reasoning rather than serializing the raw response.
    reasoning = getattr(response, "reasoning", None)
    if isinstance(reasoning, str) and reasoning.strip():
        return _note_tier("reasoning", reasoning)
    # 3) Legacy raw-transport extraction (direct litellm/OpenAI shapes).
    raw = getattr(response, "raw_response", response)
    choices = getattr(raw, "choices", None)
    if choices:
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if isinstance(content, str):
            return _note_tier("raw_choices", content)
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            if parts:
                return _note_tier("raw_choices_parts", "".join(parts))
    if isinstance(response, str):
        return _note_tier("response_str", response)
    return _note_tier("str_fallback", str(raw))


# ---------------------------------------------------------------------------
# Signal B — cadence-aware narration supervision (streaming)
# ---------------------------------------------------------------------------
# The fixed wall-clock ticker cannot tell "slow but thinking" from
# "dangling". The supervision loop below consumes the SSE stream, emits
# throttled heartbeats to the telemetry projection plane, and judges
# cadence from bytes + progressive shape — the deterministic checker the
# failure FSM consumes as cause-carrying kinds.

_STREAMING_ENV = "NOOA_STREAMING"
# Deadline model: base + rate x prompt size, clamped. Calibrated live
# 2026-10-19 — a 35K-char narrate#1 needs ~78s of DeepSeek reasoning
# (~2.2s/Kchar); the rate carries ~2x headroom over measured pace.
_CADENCE_BASE_S = 60.0
_CADENCE_RATE_S_PER_KCHAR = 4.0
_CADENCE_MIN_S = 90.0
# Liveness: no chunk for this long means the provider dangled (mirrors the
# httpx read-gap philosophy — gaps, not totals, catch frozen streams).
_CADENCE_BYTE_GAP_S = 60.0
# Heartbeat throttle: a milestone every N bytes or M seconds, plus always
# the terminal heartbeat. Keeps the projection plane bounded.
_HEARTBEAT_BYTES = 4_096
_HEARTBEAT_S = 5.0


class NarrationStalledError(Exception):
    """A supervised narration stream stalled with its cadence evidence.

    ``verdict`` is one of ``stalled_no_bytes`` (dangle — kill fast),
    ``stalled_thinking`` (bytes without shape past deadline), or
    ``timeout`` (absolute backstop). Callers map verdicts to the
    cause-carrying ``FailureKind`` values; the stats ride the ledger so
    every stall trip teaches the retry what happened.
    """

    def __init__(
        self, verdict: str, *, attempt_id: str, turn_ref: str,
        prompt_chars: int, deadline_ms: int, bytes_in: int, elapsed_ms: int,
    ) -> None:
        super().__init__(
            f"narration {verdict}: attempt {attempt_id} "
            f"({bytes_in} bytes in {elapsed_ms}ms, "
            f"prompt {prompt_chars} chars, deadline {deadline_ms}ms)"
        )
        self.verdict = verdict
        self.attempt_id = attempt_id
        self.turn_ref = turn_ref
        self.prompt_chars = prompt_chars
        self.deadline_ms = deadline_ms
        self.bytes_in = bytes_in
        self.elapsed_ms = elapsed_ms


def streaming_enabled() -> bool:
    """Whether the SSE supervision path may be attempted at all."""
    import os as _os
    return _os.getenv(_STREAMING_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def narration_deadline_s(prompt_chars: int, *, cap: float | None = None) -> float:
    """Model-aware deadline for one narration turn (pure, deterministic).

    ``base + rate x prompt_size``, floored at ``_CADENCE_MIN_S`` and capped
    at the absolute narration bound (the backstop, never the decision).
    Small prompts clear fast; the 90K-char late-cycle follow-ups that died
    under the flat 150s ticker get proportional room.
    """
    absolute = float(cap if cap is not None else narration_timeout_s())
    proportional = _CADENCE_BASE_S + _CADENCE_RATE_S_PER_KCHAR * (max(0, int(prompt_chars)) / 1000.0)
    return max(_CADENCE_MIN_S, min(proportional, absolute))


def judge_cadence(
    *, bytes_in: int, shape_formed: bool, stream_done: bool,
) -> str:
    """Deadline-moment cadence verdict (pure — no I/O, no clock).

    ``complete`` (stream finished), ``stalled_no_bytes`` (nothing arrived),
    ``stalled_thinking`` (bytes but no JSON shape formed), or
    ``progressing`` (bytes + shape — bounded patience applies). Mid-stream
    byte gaps are enforced by the supervision loop's per-chunk bound, not
    here: this judges what the deadline found.
    """
    if stream_done:
        return "complete"
    if bytes_in <= 0:
        return "stalled_no_bytes"
    if not shape_formed:
        return "stalled_thinking"
    return "progressing"


def narration_failure_kind(exc: BaseException) -> str:
    """Map a narration error to its cause-carrying ``FailureKind`` (pure).

    ``NarrationStalledError`` verdicts become the Signal B stall kinds the
    failure FSM classifies (transient narration, retry policy); the
    absolute-cap ``timeout`` verdict and every other error keep the legacy
    ``narration_failed`` kind so existing terminals and fixtures hold.
    """
    from .failure_fsm import FailureKind
    if isinstance(exc, NarrationStalledError):
        if exc.verdict == "stalled_no_bytes":
            return FailureKind.NARRATION_STALLED_NO_BYTES.value
        if exc.verdict == "stalled_thinking":
            return FailureKind.NARRATION_STALLED_THINKING.value
    return FailureKind.NARRATION_FAILED.value


async def call_narration_llm_streaming(
    llm: Any,
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    engine: Any | None = None,
    turn_ref: str = "narration",
    correlation_id: str | None = None,
) -> str:
    """One supervised narration turn over SSE (Signal B transport).

    Streams text deltas, emits throttled ``agent_narration_cadence``
    heartbeats (best-effort; ``engine=None`` disables emission for tests),
    and judges cadence from bytes + progressive JSON shape. Returns the
    assembled text (callers parse as usual). Raises ``RuntimeError`` when
    streaming is unavailable (caller falls back to buffered ``acall``) and
    ``NarrationStalledError`` with cadence evidence on stall/timeout.
    """
    from .narration_stream import stream_deltas
    from .schemas import extract_json_object
    from .telemetry import emit_narration_heartbeat

    # Capability detection (never assume): SSE needs the real NOOA client
    # surface — a routing ``model`` plus ``config``. Scripted test fakes and
    # minimal clients expose only ``acall()``; attempting SSE against them
    # raised AttributeError inside the stream, which the supervision loop
    # reported as a NarrationStalledError (0 bytes) instead of falling back.
    # RuntimeError is the documented "streaming unavailable" signal and the
    # driver's caller falls back to buffered narration.
    if not getattr(llm, "model", None):
        raise RuntimeError(
            "streaming unavailable: client exposes no routing model")

    if system_prompt is None:
        from .kb import build_system_prompt
        system_prompt = build_system_prompt("BTCUSDT", "spot",
                                            AGENTIC_MAX_TOOL_ROUNDS)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    prompt_chars = len(system_prompt or "") + len(user_prompt or "")
    absolute_cap = float(narration_timeout_s())
    deadline = narration_deadline_s(prompt_chars, cap=absolute_cap)
    deadline_ms = int(deadline * 1000)
    attempt_id = uuid.uuid4().hex[:12]
    session = getattr(engine, "session_id", None) if engine is not None else None
    corr = correlation_id or (str(session) if session else None)
    t0 = time.monotonic()
    seq = 0

    async def _beat(outcome: str, bytes_in: int, cause: str | None = None) -> None:
        nonlocal seq
        if engine is None:
            return
        try:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            await emit_narration_heartbeat(
                engine, attempt_id=attempt_id, turn_ref=str(turn_ref),
                prompt_chars=int(prompt_chars), deadline_ms=deadline_ms,
                bytes_in=int(bytes_in), elapsed_ms=elapsed_ms,
                outcome=outcome, cause=cause, source_sequence=seq,
                correlation_id=corr,
            )
        except Exception:
            log.debug("heartbeat %s failed silently", outcome, exc_info=True)
        seq += 1

    def _stalled(verdict: str, bytes_in: int) -> NarrationStalledError:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return NarrationStalledError(
            verdict, attempt_id=attempt_id, turn_ref=str(turn_ref),
            prompt_chars=int(prompt_chars), deadline_ms=deadline_ms,
            bytes_in=int(bytes_in), elapsed_ms=elapsed_ms,
        )

    await _beat("started", 0)
    text_parts: list[str] = []
    bytes_in = 0
    shape_formed = False
    last_beat_at = time.monotonic()
    last_beat_bytes = 0
    try:
        stream = stream_deltas(
            llm, messages=messages,
            max_tokens=narration_max_tokens(), timeout=absolute_cap,
        )
        while True:
            try:
                delta = await asyncio.wait_for(
                    stream.__anext__(), timeout=_CADENCE_BYTE_GAP_S,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                await _beat("stalled_no_bytes", bytes_in,
                            cause=f"no chunk for {_CADENCE_BYTE_GAP_S:.0f}s")
                raise _stalled("stalled_no_bytes", bytes_in) from None
            text_parts.append(delta.text or "")
            bytes_in += len(delta.text or "") + len(delta.thinking or "")
            now = time.monotonic()
            if (bytes_in - last_beat_bytes >= _HEARTBEAT_BYTES
                    or now - last_beat_at >= _HEARTBEAT_S):
                if not shape_formed:
                    try:
                        shape_formed = extract_json_object("".join(text_parts)) is not None
                    except Exception:
                        shape_formed = False
                await _beat("chunk", bytes_in)
                last_beat_at, last_beat_bytes = now, bytes_in
            elapsed = now - t0
            if elapsed >= deadline and not shape_formed:
                await _beat("stalled_thinking", bytes_in,
                            cause=f"{bytes_in} bytes, no JSON shape in {elapsed:.0f}s")
                raise _stalled("stalled_thinking", bytes_in)
            if elapsed >= absolute_cap:
                await _beat("timeout", bytes_in,
                            cause=f"absolute cap {absolute_cap:.0f}s")
                raise _stalled("timeout", bytes_in)
    except NarrationStalledError:
        raise
    except RuntimeError:
        raise
    except Exception as exc:
        # Transport error mid-stream: the heartbeat stream ends without a
        # terminal heartbeat; the caller's failure receipt records the
        # error kind. Never synthesize content from a broken stream.
        raise NarrationStalledError(
            "timeout", attempt_id=attempt_id, turn_ref=str(turn_ref),
            prompt_chars=int(prompt_chars), deadline_ms=deadline_ms,
            bytes_in=int(bytes_in),
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        ) from exc
    text = "".join(text_parts)
    await _beat("complete", bytes_in)
    return _note_tier("streamed", text)


def bounded_envelope_view(
    payload: dict[str, Any] | None,
    roof: int = 180_000,
    list_cap: int = 60,
) -> str:
    """Serialized envelope sized for the LLM param limit (never silent).

    Returns the full envelope when it already fits. Otherwise large lists
    are capped with an explicit ``__truncated__`` marker that records the
    original count, and only as a last resort is the payload reduced to run
    identity + coverage + errors with the trimmed top-level keys listed. The
    complete canonical envelope is never mutated — this is only the LLM-bound
    projection.
    """
    from market_service.runtime.read_paths import json_safe_dumps

    if not payload:
        return "{}"
    rendered = json_safe_dumps(payload)
    if len(rendered) <= roof:
        return rendered

    def _cap(value: Any, max_items: int) -> Any:
        if isinstance(value, dict):
            return {k: _cap(v, max_items) for k, v in value.items()}
        if isinstance(value, list):
            if len(value) > max_items:
                return {
                    "__truncated__": True,
                    "count": len(value),
                    "items": [_cap(item, max_items) for item in value[:max_items]],
                }
            return [_cap(item, max_items) for item in value]
        return value

    rendered = json_safe_dumps(_cap(payload, list_cap))
    if len(rendered) <= roof:
        return rendered

    kept = {
        k: payload.get(k)
        for k in ("run_id", "symbol", "status", "schema_version",
                  "generated_at", "completed_at", "data_source")
    }
    kept["coverage"] = payload.get("coverage")
    kept["errors"] = payload.get("errors")
    kept["_trimmed_keys"] = sorted(payload.get("canonical_state") or {})
    return json_safe_dumps(kept)


def response_text(resp: Any) -> str:
    """Extract the text content from a NOOA unified-LLM response."""
    raw = getattr(resp, "raw_response", resp)
    choices = getattr(raw, "choices", None)
    if choices:
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            if parts:
                return "".join(parts)
    if isinstance(resp, str):
        return resp
    return str(raw)


__all__ = [
    "call_narration_llm",
    "call_narration_llm_streaming",
    "judge_cadence",
    "NarrationStalledError",
    "narration_deadline_s",
    "narration_failure_kind",
    "bounded_envelope_view",
    "response_text",
]
